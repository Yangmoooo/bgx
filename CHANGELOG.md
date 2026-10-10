# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `info NAME` shows task metadata, current status and available diagnostic reasons,
  recorded process IDs, local start time, uptime, and log path without changing state.
- `log NAME -f`/`--follow` prints existing log output and then streams appended bytes
  from the same opened log, staying through task exit and name reuse until interrupted
  or the output closes. `--tail N` still selects the initial lines; observed truncation
  restarts the view from that file's beginning, and a closed output pipe exits 0.

### Changed

- `stop` retains recorded process identity and marks confirmed completion separately;
  completed tasks no longer depend on their historical process numbers for status.

### Fixed

- `list` now shows invalid or unreadable records as `unknown`, reports errors by
  name, and continues listing other tasks before exiting nonzero.
- `log` no longer creates state directories or lock files, changes their
  permissions, or waits for a name lock; concurrent name reuse cannot redirect
  the read to another task's log.

## [0.1.0] - 2026-10-10

### Added

- Start, list, and stop named background tasks on macOS and Linux, even after closing the terminal.
- View task logs, remove task records, and reuse names after tasks exit.
