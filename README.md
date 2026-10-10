# bgx

Run non-interactive commands in the background, then find and stop them by name
from any terminal—even after closing the original one.

```sh
bgx start web -- python3 -m http.server 8000
bgx list
bgx log web
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
| `bgx start NAME -- PROGRAM [ARGS...]` | Start a command; silent on success |
| `bgx list` | Show names, status, PIDs, uptime, and commands |
| `bgx log NAME [--tail N]` | Print the full log or its last N lines, then exit |
| `bgx stop NAME` | Stop a task, keeping its record and log |
| `bgx remove NAME` | Delete an exited task's record and log |
| `bgx remove NAME --force` | Delete data only; **does not stop processes** |
| `bgx help [COMMAND]` / `bgx version` | Show help or version |

Names are case-sensitive, unique within a state directory, and contain 1–128 ASCII
letters, digits, `_`, or `-`, beginning with a letter or digit. Active names cannot
be reused. Reusing an exited name deletes its old record and log, even if the new
command fails to start.

The `--` separator is required. Commands inherit the current directory and
environment, receive no terminal input, and combine stdout and stderr in one log.

## Behavior and limits

- Successful `start` confirms execution, not readiness or eventual success.
- `stop` sends TERM to the process group, waits up to 5 seconds, then sends KILL
  and waits up to 2 more. Processes that leave the group are outside bgx's control.
- Status is `running`, `exited`, `stale` (identity mismatch), or `unknown`.
  `stop` refuses `stale` and `unknown` tasks; inspect them before clearing records.
- `list` reports unreadable or invalid records as `unknown`, continues listing
  other tasks, and exits nonzero with errors on stderr.
- Commands that change effective user, such as `sudo`, may need to be stopped
  manually before removing their records.
- No automatic restarts, reboot recovery, interactive terminals, or final exit codes.

## Data

State directory, in priority order: `BGX_STATE_HOME`, `$XDG_STATE_HOME/bgx`,
`~/.local/state/bgx`. Each task has a `meta.json` record and `output.log` under
`instances/`; `locks/` coordinates changes. Data remains until removed or reused.
The storage format may change during development.

## Development

C17, built with Zig; Linux binaries statically link musl. Tests require Python 3;
ASan/UBSan checks also require Clang.

```sh
make            # Release binary: build/bgx
make debug      # Debug binary: build/debug/bgx
make test       # Integration tests
make sanitize   # ASan/UBSan checks
make check      # Both test suites
make clean      # Remove build/ and dist/, not task data
make TARGET_OS=linux TARGET_ARCH=arm64 BUILD=build/linux-arm64
make release TAG=vX.Y.Z
```

Use `make MODE=debug test` to test a debug build.
