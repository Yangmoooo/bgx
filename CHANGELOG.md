# Changelog

Notable changes, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-10-11

### Added

- `restart NAME` reruns the recorded command in its recorded directory.
- `replace NAME -- PROGRAM [ARGS...]` adopts a new command and directory on
  successful launch. Both commands stop any running execution first.
- `prune [--dry-run]` removes exited tasks or previews the selection.
- `info NAME` shows task details, status diagnostics, and log paths.
- `log -f`/`--follow` streams the opened log across task exit and name replacement;
  `--tail N` selects initial output.

### Changed

- `start` rejects all existing names; use `restart` or `replace` instead.
- Tasks retain their last confirmed execution and at most one latest launch
  attempt. Failed restart/replace preserves the old definition and log; uncertain
  outcomes block further launches and ordinary removal. `info` shows attempt details.
- **Storage schema 2 is incompatible with earlier formats.** Stop tasks with the
  old version, verify exit, delete the selected state directory, and recreate them.
  No automatic migration is provided.
- `stop` preserves process identity and records confirmed completion separately.

### Fixed

- `list` reports invalid records by name and continues listing, returning nonzero.
- `log` reads without creating state, changing permissions, or acquiring name locks;
  concurrent name reuse cannot redirect a read to another execution's log.

## [0.1.0] - 2026-10-10

### Added

- Start, list, and stop named background tasks on macOS and Linux, even after closing the terminal.
- View task logs, remove task records, and reuse names after tasks exit.
