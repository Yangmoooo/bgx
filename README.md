# bgx

Run non-interactive commands in the background, then find and stop them by name
from any terminal—even after closing the original one.

```sh
bgx start web -- python3 -m http.server 8000
bgx log web --tail 20
bgx restart web
bgx replace web -- python3 -m http.server 9000
bgx stop web
bgx remove web
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
| `bgx start NAME -- PROGRAM [ARGS...]` | Create and start a new task |
| `bgx restart NAME` | Run the recorded command again in its recorded directory |
| `bgx replace NAME -- PROGRAM [ARGS...]` | Run a new command for an existing task |
| `bgx list` | Show names, status, PIDs, uptime, and commands |
| `bgx info NAME` | Show task details, launch diagnostics, and log paths |
| `bgx log NAME [--tail N] [-f]` | Print all or the last N lines; `-f`/`--follow` streams new output |
| `bgx stop NAME` | Stop a task, keeping its record and log |
| `bgx remove NAME` | Delete an exited task's record and log |
| `bgx remove NAME --force` | Delete data only; **does not stop processes** |
| `bgx prune [--dry-run]` | Delete all exited tasks, or preview the selection |
| `bgx help [COMMAND]` / `bgx version` | Show help or version |

Names are case-sensitive, unique within a state directory, and contain 1–128 ASCII
letters, digits, `_`, or `-`, beginning with a letter or digit. `start` rejects
existing names, including exited tasks; use `restart` or `replace` instead.
Both stop any running execution before launching another, and return silently on
success, as does `start`.

`start` and `replace` require `--` and use the current directory; `restart` uses
the recorded directory. All inherit the current environment, disable terminal
input, and combine stdout and stderr in one log. To fix variables across restarts:

```sh
bgx start web -- env MODE=prod python3 server.py
```

## Behavior and limits

- Successful launch confirms execution, not service readiness or eventual success.
- A successful restart/replace replaces the prior execution's record and log.
  Definite failure preserves the old definition and log; `info` shows the latest
  failed attempt and its retained log path. A new attempt replaces earlier failure
  diagnostics. The old process is never automatically revived.
- Status is `running`, `exited`, `stale` (identity mismatch), or `unknown`.
  Uncertain launches remain `unknown` and block relaunch. Inspect and stop any
  remaining processes before clearing records with `remove --force`.
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
`~/.local/state/bgx`. `instances/` holds task records, logs, and attempt/recovery
data; `locks/` coordinates changes.

**Schema 2 is incompatible with earlier formats.** Before upgrading, use the old
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
