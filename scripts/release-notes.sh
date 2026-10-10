#!/usr/bin/env bash
set -euo pipefail

tag=${1:?usage: release-notes.sh vX.Y.Z [CHANGELOG.md]}
changelog=${2:-CHANGELOG.md}

# Initially support stable releases only, with no leading zeroes or suffixes.
if [[ ! $tag =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
  echo "Expected a stable version tag such as v0.1.0: $tag" >&2
  exit 1
fi

awk -v version="${tag#v}" '
  /^## / {
    active = index($0, "## [" version "] - ") == 1
    if (active) found++
    next
  }
  active {
    notes = notes $0 "\n"
    if ($0 ~ /[^[:space:]]/ && $0 !~ /^#+ /) has_content = 1
  }
  END {
    if (found != 1 || !has_content) {
      print "Expected exactly one nonempty dated changelog section for " version > "/dev/stderr"
      exit 1
    }
    sub(/^\n+/, "", notes)
    sub(/\n+$/, "", notes)
    print notes
  }
' "$changelog"
