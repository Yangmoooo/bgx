#!/usr/bin/env bash
set -euo pipefail

# Shared by normal and release builds; only the optimization/debug flags differ.
zig=${ZIG:-zig}
expected=$(cat .zig-version)
if [[ $("$zig" version) != "$expected" ]]; then
  echo "bgx builds require Zig $expected (see .zig-version)" >&2
  exit 1
fi

target_os=${TARGET_OS:-$(uname -s | tr '[:upper:]' '[:lower:]')}
target_arch=${TARGET_ARCH:-$(uname -m)}
case "$target_arch" in
  amd64|x86_64) cpu=x86_64 ;;
  arm64|aarch64) cpu=aarch64 ;;
  *) echo "Unsupported target architecture: $target_arch" >&2; exit 1 ;;
esac

case "$target_os" in
  linux)
    exec "$zig" cc -target "$cpu-linux-musl" -static "$@"
    ;;
  darwin)
    if [[ $(uname -s) != Darwin ]]; then
      echo "Build macOS targets on macOS with its system SDK" >&2
      exit 1
    fi
    sdk=$(xcrun --show-sdk-path)
    exec "$zig" cc -target "$cpu-macos.12.0" -isysroot "$sdk" "$@"
    ;;
  *) echo "Unsupported target platform: $target_os" >&2; exit 1 ;;
esac
