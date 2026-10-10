# bgx

Run non-interactive commands quietly in the background. Find and stop them by name
from another terminal, even after closing the original one.

```sh
bgx start web -- python3 -m http.server 8000
bgx list
bgx log web
bgx stop web
bgx remove web
```

## Installation

Requires Make and the Zig version specified in [.zig-version](.zig-version).
On macOS, Command Line Tools are also required.

```sh
make
make install PREFIX="$HOME/.local"
```

Make sure `~/.local/bin` is in your PATH. You can also run `./build/bgx` directly.

To uninstall, use the same installation path, for example:
`make uninstall PREFIX="$HOME/.local"`.

## Usage

| Command | Description |
| --- | --- |
| `bgx start NAME -- PROGRAM [ARGS...]` | Start a background command; return silently on success |
| `bgx list` | List names, status, PIDs, uptime, and commands |
| `bgx log NAME [--tail N]` | Print the full log or its last N lines, then exit |
| `bgx stop NAME` | Stop a task, keeping its record and log |
| `bgx remove NAME` | Delete an exited task's record and log |
| `bgx remove NAME --force` | Force deletion of the record and log; **does not stop processes** |
| `bgx help [COMMAND]` / `bgx version` | Show help or version information |

Aliases: `start` → `s`, `list` → `ls` / `ps`, `remove` → `rm`, `log` → `logs`,
`help` → `-h` / `--help`, `version` → `-V` / `--version`.
Use `bgx help COMMAND` or `bgx COMMAND --help` for command-specific help.
Aliases work with help too.

Names are shared within the same user's state directory. They may contain 1–128
ASCII letters, digits, underscores, or hyphens, and must begin with a letter or
digit. Names are case-sensitive: `Web` and `web` are different tasks.
An active name cannot be reused. Once a task exits, reusing its name deletes the
old record and log first; they are not restored if the new command fails to start.

The `--` separator is required when starting a command. Tasks inherit the current
directory and environment, receive no terminal input, and write both stdout and
stderr to the same log.

```sh
bgx start build -- sh -c 'make && make test'
bgx log build | less
bgx log build --tail 100
```

## Limitations

- A successful start means the program was executed; it does not guarantee
  service readiness or eventual success.
- `stop` requests a graceful exit, waits up to 5 seconds, then forces termination
  if needed and waits up to 2 more seconds. Child processes in the same process
  group are included; processes that leave the group are outside bgx's control.
- Status is `running`, `exited`, `stale` (process identity mismatch), or `unknown`
  (unable to determine status). Stopping is refused for the latter two states.
  Inspect the processes first, then use `remove --force` to clear records if needed.
- bgx does not restart tasks, restore them after a reboot, take over interactive
  terminals, or record final exit codes.

## Data location

Data is stored in `~/.local/state/bgx` by default, or `$XDG_STATE_HOME/bgx` when
`XDG_STATE_HOME` is set. Set `BGX_STATE_HOME` to use a specific state directory.

```text
<state-directory>/
  instances/<encoded-name>/
    meta.json       # Task record
    output.log      # Combined log
  locks/            # Locks for concurrent operations
```

Data remains after a task exits until its name is removed or reused.
Data format compatibility is not guaranteed during development.

## Development

The C17 source is built with Zig. Tests require Python 3; additional memory and
undefined behavior checks use Clang.

```sh
make build      # Optimized, stripped binary at build/bgx; same as make
make debug      # Unoptimized binary with debug info at build/debug/bgx
make test       # Build and run integration tests
make sanitize   # Run Clang ASan/UBSan checks
make check      # Run both test suites
make clean      # Remove the default build/ and dist/ directories
make TARGET_OS=linux TARGET_ARCH=arm64 BUILD=build/linux-arm64
```

Linux builds always link musl statically. Use `make MODE=debug test` to test a debug
build; Clang checks always retain debug information.
`clean` leaves custom `BUILD` paths outside those two directories and task data alone.
`make release TAG=vX.Y.Z` creates release packages using optimized, stripped binaries.
