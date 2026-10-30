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
| `bgx rename OLD NEW` | Change a run's name without restarting it or replacing its log |
| `bgx remove NAME` | Delete an exited task's record and log |
| `bgx remove NAME --force` | Delete data only; **does not stop processes** |
| `bgx prune [--dry-run]` | Delete all exited tasks, or preview the selection |
| `bgx help [COMMAND]` / `bgx version` | Show help or version |

Use `bgx help COMMAND` or add `-h`/`--help` anywhere in a command's arguments
before `--`. Arguments after `--` in `start` are passed to the launched program;
your shell still interprets unquoted shell syntax before bgx receives them.

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

Use `rename` to give an automatically named run a memorable name, or change an
explicit name. Running and exited runs can be renamed. The destination must be
unused, even by an exited run; stale, unknown, or invalid source records are
refused. The old name stops working immediately, while existing `log -f` viewers
continue following the same file. Renaming a valid run to its current name is a no-op.

```sh
bgx rename python3-k7m2 worker
bgx stop worker
bgx remove worker
```

### Shell commands

bgx executes a program directly. For pipelines, redirection, or command chains,
use an explicit shell or a script:

```sh
bgx start pipeline -- sh -c 'producer | filter | consumer'
bgx start build -- sh -c 'make && ./app'
bgx start import -- sh -c 'processor < input.txt > output.txt'
```

In `bgx start -- producer | consumer`, only `producer` runs under bgx;
`consumer` receives the name printed by bgx, not the producer's output.
Likewise, an outer `> file` redirects bgx's name output, and an outer `< file`
does not replace the background command's disabled stdin. Put these operators
inside the shell command when they belong to the background work.

Your current shell expands unquoted wildcards, variables, and command
substitutions first. Single quotes around a `sh -c` command defer those
expansions to that shell. For values containing spaces or shell characters,
pass arguments instead of inserting them into shell code:

```sh
bgx start -- sh -c 'processor < "$1"' sh "$input_file"
```

Shell aliases, functions, and builtins are not standalone executables. Use a
script or explicitly load their definitions in the appropriate shell; `sh -c`
does not automatically inherit them. Keep commands in the foreground inside
that shell; bgx already detaches them. `stop` targets the process group, normally
including pipeline members; programs that detach themselves can escape it.
When starting a shell, launch success confirms the shell executed, not that its
pipeline or later commands succeeded. Check the log for their errors.

### Environment variables

Background commands inherit bgx's environment. To set variables for a command,
use `env`, or set them before invoking bgx in your shell:

```sh
bgx start worker -- env MODE=prod TOKEN="$TOKEN" python3 worker.py
MODE=prod bgx start worker -- python3 worker.py
```

Run these as alternatives. The first sets variables for the launched command;
the second also sets them for bgx itself. Exported variables are inherited
automatically; unexported shell variables are not.

`bgx start worker -- MODE=prod python3 worker.py` does not work: bgx treats
`MODE=prod` as the program name. For shell syntax, assignments can go inside the
shell command instead:

```sh
bgx start worker -- sh -c 'MODE=prod python3 worker.py'
```

bgx records argv, but does not save a separate environment snapshot. Values
passed through `env KEY=value` are part of argv and appear in saved metadata and
command listings. Prefer inherited environment variables for sensitive values.

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

Names are encoded to stay distinct on case-insensitive filesystems. The directory
is the sole source of the name; `meta.json` does not duplicate it. Records and
logs remain until removal or explicit name reuse; `prune --dry-run` previews
cleanup of exited runs. Lock files remain to coordinate concurrent commands.

**Schema 4 is incompatible with earlier formats.** Before upgrading, use the old
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
