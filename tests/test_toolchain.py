"""Check the compiler wrapper without invoking a real compiler."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


class ToolchainTest(unittest.TestCase):
    def test_minimum_zig_version_and_diagnostics(self):
        with tempfile.TemporaryDirectory(prefix="bgx-toolchain-") as directory:
            base = Path(directory)
            (base / ".zig-version").write_text("0.17.0\n")
            zig = base / "fake zig"
            zig.write_text(
                '#!/bin/sh\nif [ "$1" = version ]; then\n'
                '  printf "%s\\n" "$TEST_ZIG_VERSION"\n'
                'else\n  printf "%s\\n" "$@"\nfi\n'
            )
            zig.chmod(0o700)
            for version in ["0.17.0", "0.17.1", "0.18.0", "0.100.0", "1.0.0",
                            "0.16.9", "0.9.99", "0.17.0-dev.1", "invalid", None]:
                with self.subTest(version=version):
                    env = dict(os.environ, ZIG=str(zig if version else base / "missing"),
                               TEST_ZIG_VERSION=version or "", TARGET_OS="linux", TARGET_ARCH="arm64")
                    result = subprocess.run(
                        ["bash", str(ROOT / "scripts/zig-cc.sh"), "-c", "source with spaces.c"],
                        cwd=base, env=env, capture_output=True, text=True, timeout=5,
                    )
                    if version in ["0.17.0", "0.17.1", "0.18.0", "0.100.0", "1.0.0"]:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stderr, "")
                        self.assertEqual(result.stdout.splitlines(),
                                         ["cc", "-target", "aarch64-linux-musl", "-static",
                                          "-c", "source with spaces.c"])
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertEqual(result.stdout, "")
                        self.assertIn("0.17.0", result.stderr)
                        self.assertIn(env["ZIG"], result.stderr)
                        if version:
                            self.assertIn(version, result.stderr)
