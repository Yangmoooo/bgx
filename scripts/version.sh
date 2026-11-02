#!/usr/bin/env bash
# Local default only. Release packaging supplies VERSION explicitly.
set -eu
cd -- "${BASH_SOURCE[0]%/*}/.."

description=
# A source archive nested inside another repository must not inherit its version.
# Linked worktrees have a .git file rather than a directory.
if [[ -e .git ]]; then
  description=$(git describe --tags --match 'v[0-9]*' --abbrev=7 --always --dirty 2>/dev/null) || description=
fi

# Only emit characters safe for embedding in the compiler's version define.
if [[ $description =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9]+-g[0-9a-f]+)?(-dirty)?$ ]]; then
  printf '%s\n' "${description#v}"
elif [[ $description =~ ^[0-9a-f]+(-dirty)?$ ]]; then
  printf 'dev-g%s\n' "$description"
else
  printf 'dev\n'
fi
