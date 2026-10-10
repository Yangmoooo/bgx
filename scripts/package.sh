#!/usr/bin/env bash
set -euo pipefail

tag=${1:?usage: package.sh vX.Y.Z [linux|darwin]}
target_os=${2:-$(uname -s | tr "[:upper:]" "[:lower:]")}
case "$target_os" in
  linux) ;;
  darwin)
    if [[ $(uname -s) != Darwin ]]; then
      echo "Build macOS packages on macOS with its system SDK" >&2
      exit 1
    fi
    ;;
  *) echo "Unsupported platform: $target_os" >&2; exit 1 ;;
esac
bash scripts/release-notes.sh "$tag" >/dev/null
version=${tag#v}
mkdir -p dist
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
cp README.md CHANGELOG.md "$stage/"
cp vendor/cjson/LICENSE "$stage/cJSON-LICENSE"

for arch in amd64 arm64; do
  case "$arch" in
    amd64) cpu=x86_64 ;;
    arm64) cpu=aarch64 ;;
  esac
  make CC='bash scripts/zig-cc.sh' MODE=release TESTING=0 BUILD="$stage" VERSION="$version" \
    TARGET_OS="$target_os" TARGET_ARCH="$arch" \
    CFLAGS='-std=c17 -Os -Wall -Wextra -Wpedantic -Werror' \
    LDFLAGS= build

  host_os=$(uname -s | tr "[:upper:]" "[:lower:]")
  host_arch=$(uname -m)
  if [[ $target_os == "$host_os" && ( $host_arch == "$cpu" || $host_arch == "$arch" ) ]]; then
    [[ $("$stage/bgx" version) == "bgx $version" ]]
    [[ $("$stage/bgx" --version) == "bgx $version" ]]
    BGX_BIN="$stage/bgx" python3 -m unittest discover -s tests -v
  fi
  archive="dist/bgx-${target_os}-${arch}.tar.gz"
  tar -czf "$archive" -C "$stage" bgx README.md CHANGELOG.md cJSON-LICENSE
  echo "$archive"
done
