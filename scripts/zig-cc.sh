#!/usr/bin/env bash
set -euo pipefail

# Shared by normal and release builds; only the optimization/debug flags differ.
zig=${ZIG:-zig}
# Local minimum; CI also uses this exact version for tests and release packages.
minimum=$(cat .zig-version)
if ! zig_path=$(command -v "$zig"); then
  echo "bgx builds require Zig >= $minimum (see .zig-version); executable not found: $zig" >&2
  exit 1
fi
if ! actual=$("$zig_path" version); then
  echo "Cannot read Zig version at $zig_path; bgx requires Zig >= $minimum" >&2
  exit 1
fi
version_error() {
  echo "bgx builds require a stable Zig release >= $minimum (see .zig-version); found '$actual' at $zig_path" >&2
  exit 1
}
[[ $actual =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] || version_error
installed=("${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}")
IFS=. read -r -a required <<< "$minimum"
for i in 0 1 2; do
  ((10#${installed[i]} < 10#${required[i]})) && version_error
  if ((10#${installed[i]} > 10#${required[i]})); then break; fi
done

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
