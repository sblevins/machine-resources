---
name: machine-resources
description: Coordinate memory and CPU reservations before running heavy builds, test suites, fuzzers, benchmarks, browsers, dev servers, local chains, or models on a shared Linux, macOS, or Windows machine. Use before heavy work or when investigating resource contention.
---

# Machine resources

Use the `machine-resources` CLI to reserve resources before heavy work.
If it is not on PATH, invoke `scripts/machine-resources` relative to this skill directory with a Python interpreter that has the package dependencies installed.
On Windows, use the installed `machine-resources.exe` or invoke the source launcher with Python; do not depend on Unix shebang execution.
Read `README.md` in this directory for installation, platform requirements, accounting limitations, and privacy details.

## Workflow

1. Run `machine-resources status` before heavy work.
   Heavy work is anything likely to use more than 1 GiB of memory or more than two CPU cores for more than a few seconds.
2. Consult `machine-resources history -s <keyword>` for previous measurements.
   Reserve the expected peak memory of the command and all its children, allowing for uncertainty and missed short peaks.
3. Start work through the registry:

   ```sh
   machine-resources run -m 8G -c 4 -d "project tests" -e 15m -- make test -j4
   ```

   Set the command's own worker count to fit available CPU capacity.
   For a long-lived server, keep it in the foreground under the wrapper and background the wrapper if needed.
4. If capacity is unavailable (exit 75), use `--wait` (30 minutes by default), `--wait 2h`, or fewer workers and a suitably smaller reservation.
   Never start the work outside the registry to bypass refusal.
   Exit 2 means invalid arguments or a request that cannot fit; inspect the error.
5. Inspect history after completion to improve future estimates.

Reservations track planned usage but do not normally impose memory or CPU limits.
Plain version: each agent records what it expects to need so other agents know whether there is room to start.
Jobs can still use more than their estimates, so this does not guarantee the machine will never run out.

## Hard limits are exceptional

Run without `--hard-limit` normally; reservations remain mandatory.
Use it only when current availability and outstanding reservations indicate that a plausible overrun could exhaust the machine and trigger the system-wide OOM killer.
Check status immediately before deciding and record why that risk is realistic.
When uncertain, reserve a conservative higher estimate rather than automatically adding a hard limit.

`--hard-limit` is Linux-only and is rejected on macOS and Windows before launching work.
On those systems, wait for capacity or reduce the workload if proceeding without a hard limit would risk exhausting memory.

On Linux, `--hard-limit` enforces the reserved memory through a systemd cgroup and disables swap for that job.
Plain version: this can kill the job at its allowance even while memory is still available elsewhere.
Only impose that cutoff when extra usage could make the whole machine run out and cause Linux to kill another process.

## Other agents and cleanup

- Check status before delegating heavy work and tell each subagent to reserve its own resources.
  Avoid overlapping reservations for a wrapper and the same children; reserve a job once.
- Use `claim -p <pid> -m <size> -c <cores> -d <description>` for a process already started another way.
- Stale claims clean themselves up after process exit, PID reuse, or reboot.
  Never edit registry files or delete entries by hand.
- A live reservation is not yours to remove, even if marked `OVERDUE`.
  Do not kill another agent's process; report its claim ID, PID, description, and directory to the user if it blocks work.
- Keep all participating agents on the same registry directory.
  Do not publish local registry/history files or include secrets in recorded command arguments.
