"""Check Make's default version in isolated repositories, without compiling."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


class VersionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bgx-version-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "source"
        self.repo.mkdir()
        self.copy_source(self.repo)
        self.git("init", "-q")
        self.git("config", "user.name", "Version Test")
        self.git("config", "user.email", "version-test@example.invalid")
        self.commit()

    def copy_source(self, destination):
        shutil.copy(ROOT / "Makefile", destination / "Makefile")
        script = ROOT / "scripts/version.sh"
        if script.exists():
            (destination / "scripts").mkdir()
            shutil.copy(script, destination / "scripts/version.sh")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True).strip()

    def commit(self):
        self.git("add", ".")
        self.git("-c", "commit.gpgsign=false", "commit", "-qm", "fixture")

    def version(self, *args, directory=None, **variables):
        env = os.environ.copy()
        for key in ["VERSION", "MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES"]:
            env.pop(key, None)
        env.update(variables)
        result = subprocess.run(["make", "--no-print-directory", "-s", "-f", "Makefile", "-f", "-",
                                 "print-version", *args], cwd=directory or self.repo, env=env,
                                input=".PHONY: print-version\nprint-version:\n\t@printf '%s\\n' '$(VERSION)'\n",
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout.strip()

    def test_tag_commits_and_dirty_worktree(self):
        self.git("tag", "v1.2.3")
        self.assertEqual(self.version(), "1.2.3")
        marker = self.repo / "tracked"
        marker.write_text("one")
        self.commit()
        expected = "1.2.3-1-g" + self.git("rev-parse", "--short=7", "HEAD")
        self.assertEqual(self.version(), expected)
        marker.write_text("two")
        self.assertEqual(self.version(), expected + "-dirty")
        self.assertEqual(self.version("VERSION=9.8.7"), "9.8.7")
        self.assertEqual(self.version(VERSION="custom"), "custom")
        self.assertEqual(self.version("VERSION=9.8.7", VERSION="custom"), "9.8.7")

    def test_no_tag_and_no_git_metadata(self):
        self.assertEqual(self.version(), "dev-g" + self.git("rev-parse", "--short=7", "HEAD"))
        # An unpacked source archive must not pick up an enclosing repo's tags.
        archive = self.repo / "archive"
        archive.mkdir()
        self.copy_source(archive)
        self.git("tag", "v9.9.9")
        self.assertEqual(self.version(directory=archive), "dev")

    def test_dirty_tag_and_untracked_files(self):
        self.git("tag", "v1.2.3")
        (self.repo / "untracked").write_text("not part of the build")
        self.assertEqual(self.version(), "1.2.3")
        with (self.repo / "Makefile").open("a") as stream:
            stream.write("\n# local edit\n")
        self.assertEqual(self.version(), "1.2.3-dirty")

    def test_linked_worktree(self):
        self.git("tag", "v1.2.3")
        worktree = self.base / "worktree"
        self.git("worktree", "add", "--detach", "-q", str(worktree))
        self.assertEqual(self.version(directory=worktree), "1.2.3")

    def test_explicit_version_bypasses_git_detection(self):
        script = self.repo / "scripts/version.sh"
        script.parent.mkdir(exist_ok=True)
        script.write_text("echo 'unexpected version detection' >&2\nexit 1\n")
        self.assertEqual(self.version("VERSION=0.4.0"), "0.4.0")
        self.assertEqual(self.version("VERSION=dev"), "dev")

    def test_missing_git_executable_falls_back_to_dev(self):
        script = self.repo / "scripts/version.sh"
        self.assertTrue(script.exists())
        result = subprocess.run([shutil.which("bash"), str(script)],
                                env=dict(os.environ, PATH=str(self.base / "empty-path")),
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "dev\n")
        self.assertEqual(result.stderr, "")
