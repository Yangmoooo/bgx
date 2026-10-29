# bgx

Run non-interactive commands in the background, then find and stop them by name
from any terminal—even after closing the original one.

```sh
bgx start web -- python3 -m http.server 8000
bgx log web --tail 20
bgx stop web
bgx start web -- python3 -m http.server 9000
bgx stop web
bgx remove web
```

Or let bgx choose a name for each run:

```sh
name=$(bgx start -- python3 worker.py)
bgx log "$name" --tail 20 -f
bgx stop "$name"
```

## Installation

Requires Make and the Zig version in [.zig-version](.zig-version), plus Command
Line Tools on macOS.

```sh
make
make install PREFIX="$HOME/.local"
```

Add `~/.local/bin` to `PATH`, or run `./build/bgx` directly.
Uninstall with `make uninstall PREFIX="$HOME/.local"` (use your installation prefix).

## Usage

| Command | Description |
| --- | --- |
| `bgx start [NAME] -- PROGRAM [ARGS...]` | Start a command and print its name |
| `bgx list` | Show names, status, PIDs, uptime, and commands |
| `bgx info NAME` | Show task details, launch diagnostics, and log paths |
| `bgx log NAME [--tail N] [-f]` | Print all or the last N lines; `-f`/`--follow` streams new output |
| `bgx stop NAME` | Stop a task, keeping its record and log |
| `bgx remove NAME` | Delete an exited task's record and log |
| `bgx remove NAME --force` | Delete data only; **does not stop processes** |
| `bgx prune [--dry-run]` | Delete all exited tasks, or preview the selection |
| `bgx help [COMMAND]` / `bgx version` | Show help or version |

Use `bgx help COMMAND` or add `-h`/`--help` anywhere in a command's arguments
before `--`. Everything after `--` in `start` belongs to the launched program.

Names are case-sensitive, unique within a state directory, and contain 1–128 ASCII
letters, digits, `_`, or `-`, beginning with a letter or digit. Without a name,
bgx uses the program's basename (normalized to lowercase ASCII, up to 24
characters, falling back to `task`) plus four random letters/digits, such as
`python3-k7m2`. Generated names avoid all existing entries, including exited runs.
Wrappers keep their own name: `env ...` produces `env-xxxx`.

An explicit name can be reused after its run has exited. **Reuse discards the
previous record and log, even if the new command fails to start.** Running,
stale, unknown, or unreadable records block reuse. There is no saved task
definition, restart command, or run history.

Every start uses the current directory and environment, disables terminal input,
and combines stdout and stderr in one log. Success prints only the name to
stdout. Errors go to stderr; launch errors include the name so retained records
can be inspected. `s` is an alias for `start`.

## Behavior and limits

- Successful launch confirms execution, not service readiness or eventual success.
- A definite startup failure retains this run's diagnostics and log; `info`
  explains the failure. Its name can be reused. There is no rollback to an older run.
- Status is `running`, `exited`, `stale` (identity mismatch), or `unknown`.
  Uncertain launches remain `unknown` and block name reuse. Inspect and stop any
  remaining processes before clearing records with `remove --force`.
- An error does not always mean no process ran. If execution and its record are
  confirmed but a later directory sync fails, bgx reports that the command
  started; use its name to inspect or stop it.
- `stop` refuses `stale`/`unknown` tasks. It sends TERM to the process group, waits
  up to 5 seconds, then sends KILL and waits up to 2 more. Processes that leave
  the group are outside bgx's control; commands using `sudo` may need manual stopping.
- `list` continues past invalid records, showing `unknown` and returning nonzero.
  `info` is read-only and returns success for readable records regardless of status.
  Recorded PIDs are historical information, not proof of a live process.
- `log -f` follows the opened log even after task exit or name replacement; run it
  again to view a new execution. `--tail 0 -f` shows only new output. Observed
  truncation restarts reading at the beginning; Ctrl-C stops only the viewer.
- `prune` deletes only tasks currently assessed as `exited`, rechecking under each
  name's lock. It preserves running, stale, unknown, and unreadable tasks; errors
  are reported without blocking other candidates and cause a nonzero exit.
  `--dry-run` changes nothing and does not wait on name locks.
- No automatic restarts, reboot recovery, interactive terminals, or final exit codes.

## Data

State directory, in priority order: `BGX_STATE_HOME`, `$XDG_STATE_HOME/bgx`,
`~/.local/state/bgx`.

```text
instances/<encoded-name>/
  meta.json       # This run's command, directory, process identity, and launch result
  output.log      # Combined stdout and stderr
locks/<encoded-name>.lock
```

Names are encoded to stay distinct on case-insensitive filesystems. Records and
logs remain until removal or explicit name reuse; `prune --dry-run` previews
cleanup of exited runs. Lock files remain to coordinate concurrent commands.

**Schema 3 is incompatible with earlier formats.** Before upgrading, use the old
version to stop tasks, verify their processes ended, delete the selected state
directory, and recreate tasks. This discards old configurations and logs; there
is no automatic migration or reset.

## Development

C17, built with Zig; Linux binaries statically link musl. Tests require Python 3;
ASan/UBSan checks also require Clang.

```sh
make            # Release binary: build/bgx
make debug      # Debug binary: build/debug/bgx
make test       # Integration tests
make sanitize   # ASan/UBSan checks
make check      # Full production and fault-injection test suites
make clean      # Remove build/ and dist/, not task data
make TARGET_OS=linux TARGET_ARCH=arm64 BUILD=build/linux-arm64
make release TAG=vX.Y.Z
```

Use `make MODE=debug test` to test a debug build.
CI runs `make check`, builds both architectures, and verifies the packages on every
push and pull request. It checks that the native packaged binary matches the one
tested. Tag builds inject the release version; other builds use `dev`.
After a successful tag build, CI calls the Release workflow to publish those exact
packages. Release does not rebuild or run tests. `make release TAG=dev` can be used
to check packaging locally without preparing release notes.
