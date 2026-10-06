# machine-resources

A Linux, macOS, and Windows resource-reservation CLI and agent skill for coordinating builds, tests, browsers, and other heavy jobs on a shared machine.

Agents reserve expected memory and CPU capacity before launching work.
In plain English: each agent checks what everyone else has already reserved before starting another big job.

Reservations are cooperative, not a guarantee against running out of memory.
**No hard memory limits are applied by default.**

## Requirements

- Linux, macOS, or Windows with Python 3.9 or newer.
- A shared OS user and registry directory for the agents you want to coordinate.
- Optional: Linux with systemd and a working user session for `--hard-limit`.

Installation includes `psutil` for portable process and memory information and `portalocker` for cross-platform file locking.
Plain version: these libraries let the tool check running jobs and coordinate agents on each operating system.

## Install

Clone the repository, then install the CLI into an isolated environment with [pipx](https://pipx.pypa.io/stable/installation/):

```sh
git clone https://github.com/sblevins/machine-resources.git
cd machine-resources
pipx install .
machine-resources status
```

These commands work in a Unix shell or Windows PowerShell once Git, Python, and pipx are installed and on `PATH`.
After updating the checkout, run `pipx install --force .` to reinstall it.

Alternatively, install into a virtual environment.
On Linux or macOS:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/machine-resources status
```

On Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\machine-resources.exe status
```

Use the installed CLI path or activate that environment before following the examples below.
The source launcher `scripts/machine-resources` also works when invoked with a Python interpreter that has the dependencies installed.
Copying only that launcher into `~/.local/bin` is no longer sufficient.

### Install the skill

The root [`SKILL.md`](SKILL.md) contains the reusable agent instructions.
Copy this checkout into your agent's skill directory, or symlink it there using an absolute path.
For example, for Claude Code:

```sh
mkdir -p "$HOME/.claude/skills"
ln -s "$(pwd)" "$HOME/.claude/skills/machine-resources"
```

On Windows, copy the checkout instead if symlink creation is not enabled:

```powershell
New-Item -ItemType Directory -Force "$HOME/.claude/skills" | Out-Null
Copy-Item -Recurse -Path . -Destination "$HOME/.claude/skills/machine-resources"
```

Install the CLI separately as above; copying the skill does not install Python dependencies.
Use your agent's equivalent skill directory for other clients.
For clients without skills, add the workflow from `SKILL.md` to their agent instructions.
For consistent use, also add an instruction to always read this skill before heavy work.

## Usage

```sh
machine-resources status
machine-resources history -s tests
machine-resources run -m 8G -c 4 -d "project tests" -e 15m -- make test -j4
machine-resources run -m 8G -c 4 -d "project build" --wait 2h -- make -j4
```

`-m` reserves the expected peak memory of the command and its children.
`-c` reserves CPU capacity but does not set affinity or limit worker counts; set those on the command itself.
The examples use `make`; replace it with your project's test or build command on any platform.
`-e` marks overdue work in the status report, not a timeout.
The wrapper releases the reservation when the command exits and forwards its exit status.
Keep servers in the foreground under the wrapper; do not have the wrapped command daemonize or leave detached children behind.

For a process already started outside the wrapper:

```sh
machine-resources claim -p <pid> -m 4G -c 2 -d "existing server"
```

Use an actual process ID in place of `<pid>`.
Stale claims are cleaned up on later registry operations after a process exits, its PID is reused, or the machine reboots.
`release <claim-id-or-pid>` is available for manual bookkeeping, but never release another agent's live reservation.

`status --json` and `history --json` provide machine-readable output.
Exit code 75 means capacity is temporarily unavailable; wait or reduce concurrency, never bypass the refusal.
Exit code 2 indicates a usage error or a request that cannot fit the machine's reservable capacity.
A wrapped command can also return those exit codes itself; use stderr to distinguish the source.

## Hard-limit policy

Normally reserve a conservative estimate and run **without** `--hard-limit`.
Only add it when current memory availability and outstanding reservations indicate that a plausible overrun could exhaust the machine.
Check `status` immediately before deciding and explain why that risk is realistic.

**Hard limits are supported only on Linux.**
On macOS and Windows, requesting `--hard-limit` fails before starting the command.
If a job is unsafe without enforcement there, wait for more capacity or reduce its workload; do not simply bypass that protection.

With `--hard-limit` on Linux, the wrapper uses `systemd-run --user --scope` with `MemoryMax` set to the reservation and `MemorySwapMax=0`.
In plain English: Linux can kill the job when it reaches its allowance, even if the machine has plenty of memory left, and that job cannot use swap.
Use this protection deliberately, not merely because your estimate is uncertain.

## Accounting and limitations

Available capacity is the operating system's available-memory estimate, minus the unused portion of live memory reservations, minus safety headroom.
The default headroom is the larger of 4 GiB and 10% of total RAM.
Checking capacity, launching a command, and registering its reservation happen under one file lock.
In plain English: participating agents cannot simultaneously reserve the same remaining capacity.

- This is cooperative coordination, not isolation or an OOM-proof scheduler.
  Unregistered jobs and underestimated jobs can still exhaust memory.
- Memory estimates use process-tree resident memory and can count shared pages more than once.
  History samples roughly every two seconds and can miss brief peaks or very short processes.
  Treat recorded peaks as estimates, not exact maximums.
- CPU reservations are bookkeeping, not enforcement.
- Accounting uses operating-system memory and CPU affinity where available (otherwise logical CPU count), not container memory limits or CPU quotas.
  Do not rely on it for capacity admission inside a memory-limited container.
- Detached or reparented children may escape process-tree accounting.
- The default registry is per user, not automatically shared between different OS users.

## Local state and privacy

On Linux and macOS, state defaults to `$XDG_STATE_HOME/machine-resources`, or `~/.local/state/machine-resources`.
On Windows, it defaults to `%LOCALAPPDATA%\machine-resources`.
`MACHINE_RESOURCES_DIR` overrides the directory; all cooperating agents must use the same one.
`MACHINE_RESOURCES_HEADROOM` overrides headroom with a size such as `8G`.
Do not use a separate directory to bypass reservations for real work.

The registry and last 1,000 history entries are local files.
They can contain command arguments, working directories, descriptions, and agent identifiers.
Do not put secrets in command arguments or publish these files.
The tool does not upload telemetry.

## Development

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

Tests use temporary registries and small child processes, not real memory exhaustion.
Systemd hard-limit command construction is mocked; the suite does not test kernel OOM enforcement.

GitHub Actions runs the suite on Linux, macOS, and Windows with Python 3.9 and 3.13.
