"""Linux-only regression tests; every registry is private to a temporary directory."""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "machine-resources"
LINUX = sys.platform == "linux"


@unittest.skipUnless(LINUX, "machine-resources requires Linux /proc and flock")
class IsolatedRegistryTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.registry_directory = Path(self.temporary_directory.name)
        self.environment = {
            **os.environ,
            "MACHINE_RESOURCES_DIR": str(self.registry_directory),
            "MACHINE_RESOURCES_HEADROOM": "0",
        }
        loader = importlib.machinery.SourceFileLoader("machine_resources_under_test", str(SCRIPT))
        specification = importlib.util.spec_from_loader(loader.name, loader)
        self.tool = importlib.util.module_from_spec(specification)
        with mock.patch.dict(os.environ, self.environment):
            loader.exec_module(self.tool)

    def cli(self, *arguments):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            env=self.environment,
            cwd=self.registry_directory,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def registry(self):
        return json.loads((self.registry_directory / "registry.json").read_text())

    def history(self):
        return [
            json.loads(line)
            for line in (self.registry_directory / "history.jsonl").read_text().splitlines()
        ]

    def run_arguments(self, *extra):
        return self.tool.build_parser().parse_args(
            ["run", "-m", "64M", "-c", "0", "-d", "unit command", *extra,
             "--", "example-command", "argument with spaces"]
        )


class CliTests(IsolatedRegistryTest):
    def test_run_forwards_output_exit_code_and_records_release(self):
        for exit_code in (0, 7):
            with self.subTest(exit_code=exit_code):
                result = self.cli(
                    "run", "-m", "64M", "-c", "0", "-d", f"sample {exit_code}",
                    "--", sys.executable, "-c",
                    f"import sys; print('child stdout'); print('child stderr', file=sys.stderr); sys.exit({exit_code})",
                )
                self.assertEqual(result.returncode, exit_code, result.stderr)
                self.assertEqual(result.stdout, "child stdout\n")
                self.assertIn("child stderr\n", result.stderr)
                self.assertIn("released claim", result.stderr)
                self.assertEqual(self.registry()["claims"], [])
                entry = self.history()[-1]
                self.assertEqual(entry["outcome"], "finished")
                self.assertEqual(entry["exit_code"], exit_code)
                self.assertEqual(entry["reserved_memory_bytes"], 64 * 1024**2)
                self.assertGreater(entry["peak_memory_bytes"], 0)
                self.assertEqual(entry["working_directory"], str(self.registry_directory))
        result = self.cli("history", "--json", "-s", "SAMPLE", "-n", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([entry["description"] for entry in json.loads(result.stdout)], ["sample 7"])
        result = self.cli("history", "--json", "-s", "does-not-match")
        self.assertEqual(json.loads(result.stdout), [])

    def test_signal_exit_is_reported_as_shell_exit_code(self):
        result = self.cli(
            "run", "-m", "64M", "-c", "0", "-d", "signal exit", "--",
            sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
        )
        self.assertEqual(result.returncode, 128 + signal.SIGTERM, result.stderr)
        self.assertEqual(self.registry()["claims"], [])
        self.assertEqual(self.history()[0]["exit_code"], 128 + signal.SIGTERM)

    def test_claim_duplicate_and_explicit_release_by_id_or_pid(self):
        for release_by_pid in (False, True):
            with self.subTest(release_by_pid=release_by_pid):
                result = self.cli("claim", "-p", str(os.getpid()), "-m", "1M", "-c", "0", "-d", "test owner")
                self.assertEqual(result.returncode, 0, result.stderr)
                claim_id = result.stdout.strip()
                duplicate = self.cli("claim", "-p", str(os.getpid()), "-m", "1M", "-c", "0", "-d", "duplicate")
                self.assertEqual(duplicate.returncode, 2)
                self.assertIn("already has a claim", duplicate.stderr)
                target = str(os.getpid()) if release_by_pid else claim_id
                released = self.cli("release", target)
                self.assertEqual(released.returncode, 0, released.stderr)
                self.assertEqual(self.registry()["claims"], [])
                self.assertEqual(self.history()[-1]["outcome"], "released")
                self.assertEqual(self.cli("release", target).returncode, 1)

    def test_stale_claims_are_removed_for_exit_pid_reuse_and_reboot(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=10)
        arguments = self.tool.build_parser().parse_args(
            ["claim", "-p", str(os.getpid()), "-m", "1M", "-c", "0", "-d", "stale test"]
        )
        base = self.tool.new_claim(os.getpid(), arguments, "test command")
        stale_claims = [
            {**base, "id": "exited", "pid": child.pid},
            {**base, "id": "reused", "process_start_ticks": base["process_start_ticks"] + 1},
            {**base, "id": "reboot", "boot_id": "different-boot"},
        ]
        self.tool.save_registry({"claims": stale_claims})
        result = self.cli("status", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual(status["claims"], [])
        reasons = {claim["id"]: claim["stale_reason"] for claim in status["removed_stale_claims"]}
        self.assertIn("no longer running", reasons["exited"])
        self.assertIn("different process", reasons["reused"])
        self.assertIn("rebooted", reasons["reboot"])
        self.assertEqual(self.registry()["claims"], [])
        self.assertEqual([entry["outcome"] for entry in self.history()], ["stale"] * 3)
        self.cli("status", "--json")
        self.assertEqual(len(self.history()), 3, "stale history must not be duplicated")

    def test_invalid_requests_do_not_launch_command(self):
        marker = self.registry_directory / "must-not-exist"
        command = ["--", sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]
        for flags in (["-m", "0"], ["-m", "1M", "-c", "-1"], ["-m", "invalid"],
                      ["-m", "1M", "-e", "invalid"], ["-m", "999999999T"]):
            with self.subTest(flags=flags):
                result = self.cli("run", "-d", "invalid request", *flags, *command)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(marker.exists())
        missing_command = self.cli("run", "-m", "1M", "-d", "missing command")
        self.assertEqual(missing_command.returncode, 2)
        self.assertIn("give the command", missing_command.stderr)
        missing_executable = self.cli("run", "-m", "1M", "-c", "0", "-d", "missing executable", "--", str(marker))
        self.assertEqual(missing_executable.returncode, 2)
        self.assertIn("cannot start", missing_executable.stderr)
        self.assertFalse((self.registry_directory / "history.jsonl").exists())


class CapacityTests(IsolatedRegistryTest):
    def capacity_mocks(self, process_table=None, available=800, headroom=100, cpus=4):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(self.tool, "read_meminfo_bytes", return_value={"MemTotal": 1000, "MemAvailable": available}))
        stack.enter_context(mock.patch.object(self.tool, "snapshot_process_table", return_value=process_table or {}))
        stack.enter_context(mock.patch.object(self.tool, "headroom_bytes", return_value=headroom))
        stack.enter_context(mock.patch.object(self.tool, "usable_cpu_count", return_value=cpus))
        return stack

    def test_capacity_counts_only_unused_reservations_and_all_descendants(self):
        claims = [{"pid": 10, "memory_bytes": 500, "cpus": 2}, {"pid": 20, "memory_bytes": 100, "cpus": 1}]
        process_table = {10: (1, 100), 11: (10, 50), 12: (11, 50), 20: (1, 150), 30: (1, 999)}
        with self.capacity_mocks(process_table):
            capacity = self.tool.compute_capacity(claims)
        self.assertEqual([claim["used_memory_bytes"] for claim in capacity["claims"]], [200, 150])
        self.assertEqual(capacity["reserved_memory_bytes"], 600)
        self.assertEqual(capacity["reserved_but_unused_memory_bytes"], 300)
        self.assertEqual(capacity["free_memory_bytes"], 400)
        self.assertEqual(capacity["reservable_memory_ceiling_bytes"], 900)
        self.assertEqual(capacity["free_cpus"], 1)
        self.assertTrue(self.tool.request_fits(capacity, 400, 1))
        self.assertFalse(self.tool.request_fits(capacity, 401, 1))
        self.assertFalse(self.tool.request_fits(capacity, 400, 2))
        self.assertNotIn("used_memory_bytes", claims[0])

    def test_capacity_never_reports_negative_free_resources(self):
        with self.capacity_mocks(available=50):
            capacity = self.tool.compute_capacity([{"pid": 10, "memory_bytes": 500, "cpus": 8}])
        self.assertEqual(capacity["free_memory_bytes"], 0)
        self.assertEqual(capacity["free_cpus"], 0)
        for memory, cpus in ((901, 0), (1, 5)):
            with self.subTest(memory=memory, cpus=cpus), self.assertRaises(self.tool.UsageError):
                self.tool.check_request_is_possible(capacity, memory, cpus)
        self.tool.check_request_is_possible(capacity, 900, 4)

    def test_default_and_configured_headroom(self):
        gibibyte = 1024**3
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.tool.headroom_bytes(8 * gibibyte), 4 * gibibyte)
            self.assertEqual(self.tool.headroom_bytes(100 * gibibyte), 10 * gibibyte)
        with mock.patch.dict(os.environ, {"MACHINE_RESOURCES_HEADROOM": "512M"}):
            self.assertEqual(self.tool.headroom_bytes(100 * gibibyte), 512 * 1024**2)

    def test_claim_admission_accounts_for_existing_process_memory(self):
        arguments = self.tool.build_parser().parse_args(["claim", "-p", str(os.getpid()), "-m", "600", "-c", "0", "-d", "existing"])
        with self.capacity_mocks({os.getpid(): (1, 500)}, available=200), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.tool.command_claim(arguments), 0)
        self.assertEqual(self.registry()["claims"][0]["memory_bytes"], 600)

    def test_concurrent_admission_checks_and_saves_under_real_file_lock(self):
        start_barrier = threading.Barrier(2)

        def request(pid):
            arguments = self.tool.build_parser().parse_args(["claim", "-p", str(pid), "-m", "600", "-c", "1", "-d", "concurrent"])
            start_barrier.wait(timeout=5)
            return self.tool.command_claim(arguments)

        with self.capacity_mocks(available=1000, headroom=0, cpus=1), \
             mock.patch.object(self.tool, "process_start_ticks", return_value=123), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
             ThreadPoolExecutor(max_workers=2) as executor:
            requests = [executor.submit(request, pid) for pid in (101, 102)]
            outcomes = [future.result(timeout=10) for future in requests]
        self.assertEqual(sorted(outcomes), [0, 75])
        self.assertEqual(len(self.registry()["claims"]), 1)

    def test_run_refusal_does_not_launch_or_record_history(self):
        arguments = self.tool.build_parser().parse_args(
            ["run", "-m", "600", "-c", "0", "-d", "refused", "--", "example-command"]
        )
        with self.capacity_mocks(available=200), \
             mock.patch.object(self.tool.subprocess, "Popen") as launch, \
             contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(self.tool.command_run(arguments), 75)
        launch.assert_not_called()
        self.assertIn("not enough free resources", errors.getvalue())
        self.assertEqual(self.registry()["claims"], [])
        self.assertFalse((self.registry_directory / "history.jsonl").exists())

    def test_hard_limit_only_changes_launch_command(self):
        for hard_limit in (False, True):
            with self.subTest(hard_limit=hard_limit):
                self.tool.save_registry({"claims": []})
                arguments = self.run_arguments(*(["--hard-limit"] if hard_limit else []))
                with mock.patch.object(self.tool, "compute_capacity", return_value={
                    "reservable_memory_ceiling_bytes": 128 * 1024**2,
                    "free_memory_bytes": 128 * 1024**2, "total_cpus": 1, "free_cpus": 1,
                }), mock.patch.object(self.tool.subprocess, "Popen", return_value=mock.Mock(pid=os.getpid())) as launch, \
                     mock.patch.object(self.tool, "supervise_child", return_value=23) as supervise, \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(self.tool.command_run(arguments), 23)
                expected_command = ["example-command", "argument with spaces"]
                if hard_limit:
                    expected_command = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "-p", "MemoryMax=67108864", "-p", "MemorySwapMax=0", "--", *expected_command]
                launch.assert_called_once_with(expected_command)
                claim = self.registry()["claims"][0]
                self.assertEqual(claim["hard_memory_limit"], hard_limit)
                self.assertEqual(claim["command"], "example-command argument with spaces")
                supervise.assert_called_once_with(os.getpid(), claim)


if __name__ == "__main__":
    unittest.main()
