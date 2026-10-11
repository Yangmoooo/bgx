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
fault_build=$(mktemp -d)
trap 'rm -rf "$stage" "$fault_build"' EXIT
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
    # The fault-test binary never ships; it only strengthens validation of the
    # packaged binary by running the handshake and recovery suites against it.
    make TESTING=1 BUILD="$fault_build" VERSION="$version" \
      TARGET_OS="$target_os" TARGET_ARCH="$arch" build
    [[ $("$stage/bgx" version) == "bgx $version" ]]
    [[ $("$stage/bgx" --version) == "bgx $version" ]]
    BGX_BIN="$stage/bgx" BGX_FAULT_BIN="$fault_build/bgx" \
      python3 -m unittest discover -s tests -v
  fi
  archive="dist/bgx-${target_os}-${arch}.tar.gz"
  tar -czf "$archive" -C "$stage" bgx README.md CHANGELOG.md cJSON-LICENSE
  echo "$archive"
done
