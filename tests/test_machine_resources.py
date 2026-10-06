"""Portable regression tests; every registry is private to a temporary directory."""

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
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "machine-resources"
SOURCE = SCRIPT.parents[1] / "src" / "machine_resources.py"


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
        loader = importlib.machinery.SourceFileLoader("machine_resources_under_test", str(SOURCE))
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
            timeout=30,
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
                self.assertGreaterEqual(entry["peak_memory_bytes"], 0)
                self.assertEqual(Path(entry["working_directory"]).resolve(), self.registry_directory.resolve())
        result = self.cli("history", "--json", "-s", "SAMPLE", "-n", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([entry["description"] for entry in json.loads(result.stdout)], ["sample 7"])
        result = self.cli("history", "--json", "-s", "does-not-match")
        self.assertEqual(json.loads(result.stdout), [])

    @unittest.skipIf(sys.platform == "win32", "POSIX signal exit conventions")
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

    def test_claim_rejects_nonpositive_process_ids(self):
        for process_id in (0, -1, -123):
            with self.subTest(process_id=process_id):
                result = self.cli(
                    "claim", "-p", str(process_id), "-m", "1M", "-c", "0",
                    "-d", "invalid process id",
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("pid", result.stderr.lower())
                self.assertIn("positive", result.stderr.lower())
                self.assertFalse((self.registry_directory / "registry.json").exists())
                self.assertFalse((self.registry_directory / "history.jsonl").exists())

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


class PlatformFactsTests(IsolatedRegistryTest):
    def test_live_process_facts_are_available_and_stable(self):
        process_id = os.getpid()
        identity = self.tool.process_start_ticks(process_id)
        self.assertIsNotNone(identity)
        self.assertEqual(identity, self.tool.process_start_ticks(process_id))
        boot_identity = self.tool.current_boot_id()
        self.assertTrue(boot_identity)
        self.assertEqual(boot_identity, self.tool.current_boot_id())
        self.assertTrue(self.tool.process_name(process_id))
        self.assertTrue(self.tool.process_command_text(process_id))
        process_table = self.tool.snapshot_process_table()
        self.assertIn(process_id, process_table)
        parent_id, resident_bytes = process_table[process_id]
        self.assertEqual(parent_id, os.getppid())
        self.assertGreater(resident_bytes, 0)
        memory = self.tool.read_meminfo_bytes()
        self.assertGreater(memory["MemTotal"], 0)
        self.assertGreaterEqual(memory["MemAvailable"], 0)
        self.assertLessEqual(memory["MemAvailable"], memory["MemTotal"])
        self.assertGreaterEqual(self.tool.usable_cpu_count(), 1)

    def test_non_linux_identity_ignores_boot_time_drift(self):
        for platform in ("win32", "darwin"):
            with self.subTest(platform=platform), \
                 mock.patch.object(self.tool, "IS_LINUX", False), \
                 mock.patch.object(self.tool.sys, "platform", platform), \
                 mock.patch.object(self.tool.psutil, "boot_time", side_effect=[1000.1, 1000.9]) as boot_time, \
                 mock.patch.object(self.tool.psutil, "Process") as process:
                process.return_value.create_time.return_value = 2000.25
                original_identity = self.tool.current_boot_id()
                claim = {
                    "pid": 123, "boot_id": original_identity,
                    "process_start_ticks": self.tool.process_start_ticks(123),
                }
                subsequent_identity = self.tool.current_boot_id()
                self.assertEqual(original_identity, subsequent_identity)
                self.assertEqual(original_identity, f"creation-time:{platform}")
                self.assertTrue(self.tool.claim_process_is_alive(claim, subsequent_identity))
                # A reused PID has a different absolute creation time, including after reboot.
                process.return_value.create_time.return_value = 3000.25
                self.assertFalse(self.tool.claim_process_is_alive(claim, subsequent_identity))
                boot_time.assert_not_called()

    def test_exited_process_has_no_identity(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=10)
        self.assertIsNone(self.tool.process_start_ticks(child.pid))

    def test_default_registry_paths_and_explicit_override(self):
        state_home = str(self.registry_directory / "state")
        windows_home = str(self.registry_directory / "local-app-data")
        override = str(self.registry_directory / "explicit-registry")
        for platform in ("linux", "darwin", "win32"):
            for explicit in (False, True):
                with self.subTest(platform=platform, explicit=explicit):
                    environment = {"XDG_STATE_HOME": state_home, "LOCALAPPDATA": windows_home}
                    if explicit:
                        environment["MACHINE_RESOURCES_DIR"] = override
                    specification = importlib.util.spec_from_file_location("path_test_tool", SOURCE)
                    module = importlib.util.module_from_spec(specification)
                    with mock.patch.dict(os.environ, environment, clear=True), \
                         mock.patch.object(sys, "platform", platform):
                        specification.loader.exec_module(module)
                    expected = override if explicit else os.path.join(
                        windows_home if platform == "win32" else state_home, "machine-resources"
                    )
                    self.assertEqual(module.REGISTRY_DIRECTORY, expected)
                    self.assertFalse(Path(expected).exists(), "import must not create state directories")

    def test_default_state_home_falls_back_to_user_home(self):
        for platform in ("linux", "darwin", "win32"):
            with self.subTest(platform=platform):
                specification = importlib.util.spec_from_file_location("fallback_path_tool", SOURCE)
                module = importlib.util.module_from_spec(specification)
                fallback = str(self.registry_directory / "home-state")
                with mock.patch.dict(os.environ, {}, clear=True), \
                     mock.patch.object(sys, "platform", platform), \
                     mock.patch.object(os.path, "expanduser", return_value=fallback) as expand_home:
                    specification.loader.exec_module(module)
                expand_home.assert_called_once_with(
                    "~/AppData/Local" if platform == "win32" else "~/.local/state"
                )
                self.assertEqual(module.REGISTRY_DIRECTORY, os.path.join(fallback, "machine-resources"))


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

    def test_process_tree_cycles_terminate_without_double_counting(self):
        # Run separately so a regression cannot hang the test suite indefinitely.
        child_source = """
import importlib.util
import json
import sys

specification = importlib.util.spec_from_file_location("cycle_tool", sys.argv[1])
tool = importlib.util.module_from_spec(specification)
specification.loader.exec_module(tool)
process_table = {10: (30, 10), 20: (10, 20), 30: (20, 30), 40: (40, 7), 50: (1, 999)}
print(json.dumps([
    tool.process_tree_memory_bytes(process_id, process_table)
    for process_id in (10, 40, 999)
]))
"""
        result = subprocess.run(
            [sys.executable, "-c", child_source, str(SOURCE)],
            env=self.environment, cwd=self.registry_directory,
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [60, 7, 0])

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

    def test_separate_processes_serialize_capacity_admission(self):
        worker_source = """
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

specification = importlib.util.spec_from_file_location("worker_tool", sys.argv[1])
tool = importlib.util.module_from_spec(specification)
specification.loader.exec_module(tool)
tool.read_meminfo_bytes = lambda: {"MemTotal": 1000, "MemAvailable": 1000}
tool.headroom_bytes = lambda total: 0
tool.usable_cpu_count = lambda: 1

def snapshot():
    time.sleep(0.1)
    return {}

tool.snapshot_process_table = snapshot
ready_path = Path(sys.argv[2])
result_path = Path(sys.argv[3])
arguments = tool.build_parser().parse_args([
    "claim", "-p", str(os.getpid()), "-m", "600", "-c", "1", "-d", "cross-process"
])
ready_path.write_text("ready", encoding="utf-8")
outcome = tool.command_claim(arguments)
temporary_result = result_path.with_suffix(".tmp")
temporary_result.write_text(json.dumps(outcome), encoding="utf-8")
os.replace(temporary_result, result_path)
# Keep the owner alive until the parent has checked both decisions.
sys.stdin.readline()
"""
        workers = []
        ready_paths = [self.registry_directory / f"ready-{index}" for index in range(2)]
        result_paths = [self.registry_directory / f"result-{index}.json" for index in range(2)]

        def wait_for_files(paths):
            deadline = time.monotonic() + 20
            while not all(path.exists() for path in paths):
                self.assertTrue(all(worker.poll() is None for worker in workers),
                                "an admission worker exited before publishing its result")
                if time.monotonic() >= deadline:
                    self.fail("admission workers did not publish metadata before timeout")
                time.sleep(0.01)

        try:
            with self.tool.locked_registry():
                for ready_path, result_path in zip(ready_paths, result_paths):
                    workers.append(subprocess.Popen(
                        [sys.executable, "-c", worker_source, str(SOURCE),
                         str(ready_path), str(result_path)],
                        env=self.environment, cwd=self.registry_directory,
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True,
                    ))
                wait_for_files(ready_paths)
                # Neither independent interpreter can admit while our lock is held.
                time.sleep(0.2)
                self.assertFalse(any(path.exists() for path in result_paths))
            wait_for_files(result_paths)
            outcomes = [json.loads(path.read_text()) for path in result_paths]
            self.assertEqual(sorted(outcomes), [0, 75])
            self.assertEqual(len(self.registry()["claims"]), 1)
            self.assertIn(self.registry()["claims"][0]["pid"],
                          [worker.pid for worker in workers])
            for worker in workers:
                _, errors = worker.communicate("done\n", timeout=10)
                self.assertEqual(worker.returncode, 0, errors)
        finally:
            for worker in workers:
                if worker.poll() is None:
                    worker.kill()
                worker.communicate(timeout=10)

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

    def test_unsupported_hard_limit_never_launches_command(self):
        for platform in ("win32", "darwin"):
            with self.subTest(platform=platform), \
                 mock.patch.object(self.tool.sys, "platform", platform), \
                 mock.patch.object(self.tool, "IS_LINUX", False), \
                 mock.patch.object(self.tool.sys, "argv", [
                     "machine-resources", "run", "-m", "64M", "-c", "0",
                     "-d", "unsupported limit", "--hard-limit", "--", "example-command",
                 ]), \
                 mock.patch.object(self.tool.subprocess, "Popen") as launch, \
                 contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(self.tool.main(), 2)
            launch.assert_not_called()
            self.assertIn("--hard-limit", errors.getvalue())
            self.assertFalse((self.registry_directory / "registry.json").exists())
            self.assertFalse((self.registry_directory / "history.jsonl").exists())

    def test_hard_limit_only_changes_launch_command(self):
        for hard_limit in (False, True):
            with self.subTest(hard_limit=hard_limit):
                self.tool.save_registry({"claims": []})
                arguments = self.run_arguments(*(["--hard-limit"] if hard_limit else []))
                with mock.patch.object(self.tool.sys, "platform", "linux"), \
                     mock.patch.object(self.tool, "IS_LINUX", True), \
                     mock.patch.object(self.tool, "process_start_ticks", return_value=123), \
                     mock.patch.object(self.tool, "current_boot_id", return_value="test-boot"), \
                     mock.patch.object(self.tool, "compute_capacity", return_value={
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
                supervise.assert_called_once_with(launch.return_value, claim)


if __name__ == "__main__":
    unittest.main()
