import json
from datetime import datetime, timezone
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

BINARY = Path(os.environ.get("BGX_BIN", "build/bgx")).resolve()


class BgxTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bgx-test-")
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        self.cwd = self.base / "project with spaces"
        self.cwd.mkdir()
        self.env = os.environ.copy()
        self.env["BGX_STATE_HOME"] = str(self.state)
        self.env["BGX_TEST_VALUE"] = "inherited value"
        self.groups = set()

    def tearDown(self):
        for pgid in self.groups:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.temp.cleanup()

    def cli(self, *args, expect=0, cwd=None, env=None, **kwargs):
        result = subprocess.run(
            [str(BINARY), *args], cwd=cwd or self.cwd,
            env=env or self.env, capture_output=True, timeout=15, **kwargs
        )
        self.assertNotIn(b"AddressSanitizer", result.stderr)
        self.assertNotIn(b"runtime error:", result.stderr)
        if expect is not None:
            self.assertEqual(result.returncode, expect, (args, result.stdout, result.stderr))
        return result

    def directory(self, name="web"):
        return self.state / "instances" / name

    def record(self, name="web"):
        return json.loads((self.directory(name) / "meta.json").read_text())

    def save(self, value, name="web"):
        (self.directory(name) / "meta.json").write_text(json.dumps(value))

    def track(self, name="web"):
        record = self.record(name)
        self.groups.add(record["process_identity"]["process_group"])
        return record

    def start(self, name="web", code=None):
        if code is None:
            code = "import time; print('ready', flush=True); time.sleep(120)"
        result = self.cli("start", name, "--", sys.executable, "-c", code)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")
        record = self.track(name)
        self.wait_log(b"ready", name)
        return record

    def wait_log(self, needle, name="web"):
        deadline = time.monotonic() + 5
        path = self.directory(name) / "output.log"
        while time.monotonic() < deadline:
            if path.exists() and needle in path.read_bytes():
                return path.read_bytes()
            time.sleep(0.02)
        self.fail(f"log never contained {needle!r}")

    def wait_exited(self, name="web"):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.cli("ls").stdout.decode()
            if any(line.split()[:2] == [name, "exited"] for line in result.splitlines()):
                return
            time.sleep(0.02)
        self.fail("instance did not exit")

    def test_help_and_grammar(self):
        for args in [(), ("help",), ("--help",), ("help", "start"), ("start", "--help")]:
            self.assertIn(b"bgx start NAME", self.cli(*args).stdout)
        self.assertIn(b"bgx ", self.cli("version").stdout)
        self.assertEqual(self.cli("version").stdout, self.cli("--version").stdout)
        self.assertEqual(self.cli("version").stdout, self.cli("-V").stdout)
        for args in [
            ("web", "--", "true"), ("run", "web", "--", "true"),
            ("start",), ("start", "web"), ("start", "web", "true"),
            ("start", "web", "--"), ("start", "web", "--", ""),
            ("start", "../escape", "--", "true"),
            ("start", "Upper", "--", "true"),
            ("start", "a" * 129, "--", "true"),
            ("ls", "extra"), ("rm", "--force"), ("log", "web", "-f"),
            ("restart", "web"), ("forget", "web"),
            ("completion", "bash"), ("logpath", "web"),
            ("-v",), ("up", "web", "--", "true"), ("down", "web"),
        ]:
            with self.subTest(args=args):
                self.cli(*args, expect=2)

    def test_empty_list_does_not_create_state(self):
        self.assertIn(b"NAME", self.cli("ls").stdout)
        self.assertFalse(self.state.exists())

    def test_command_help_is_specific_and_descriptive(self):
        general = self.cli("help").stdout
        for command in ["start", "stop", "list", "remove", "log", "help", "version"]:
            with self.subTest(command=command):
                specific = self.cli("help", command).stdout
                self.assertNotEqual(specific, general)
                self.assertEqual(specific, self.cli(command, "--help").stdout)
                self.assertEqual(specific, self.cli(command, "-h").stdout)
                self.assertIn(specific.splitlines()[0], general)
                self.assertIn(f"bgx {command}".encode(), specific)
        self.assertNotIn(b"bgx start", self.cli("help", "log").stdout)
        self.assertIn(b"--tail N", self.cli("help", "log").stdout)
        self.cli("help", "missing", expect=2)
        self.assertFalse(self.state.exists())

    def test_alias_help_uses_canonical_command(self):
        aliases = {"s": "start", "ls": "list", "ps": "list", "rm": "remove",
                   "logs": "log", "-h": "help", "--help": "help",
                   "-V": "version", "--version": "version"}
        for alias, command in aliases.items():
            with self.subTest(alias=alias):
                expected = self.cli("help", command).stdout
                self.assertEqual(self.cli("help", alias).stdout, expected)
                self.assertEqual(self.cli(alias, "--help").stdout, expected)
                self.assertEqual(self.cli(alias, "-h").stdout, expected)
        for alias in ["-h", "--help"]:
            self.assertEqual(self.cli(alias).stdout, self.cli("help").stdout)
            self.assertEqual(self.cli(alias, "logs").stdout, self.cli("help", "log").stdout)
        self.assertFalse(self.state.exists())

    def test_command_aliases_manage_same_task(self):
        for removal in ["remove", "rm"]:
            self.cli("s", "web", "--", sys.executable, "-c",
                     "import time; print('ready', flush=True); time.sleep(120)")
            self.track()
            self.wait_log(b"ready")
            self.cli(removal, "web", expect=1)
            self.cli("stop", "web")
            listed = self.cli("list").stdout
            self.assertIn(b"web", listed)
            for alias in ["ls", "ps"]:
                self.assertEqual(self.cli(alias).stdout, listed)
            self.assertEqual(self.cli("logs", "web").stdout, self.cli("log", "web").stdout)
            self.assertEqual(self.cli("logs", "web", "--tail", "1").stdout, b"ready\n")
            self.cli(removal, "web")
            self.assertFalse(self.directory().exists())

    def test_stopped_name_can_be_restarted_and_removed(self):
        previous = None
        for _ in range(3):
            self.cli("s", "web", "--", "/bin/sleep", "120")
            current = self.track()["process_identity"]["pid"]
            self.assertNotEqual(current, previous)
            row = self.cli("list").stdout.decode().splitlines()[1].split()
            self.assertEqual(row[:3], ["web", "running", str(current)])
            self.cli("stop", "web")
            row = self.cli("list").stdout.decode().splitlines()[1].split()
            self.assertEqual(row[:2], ["web", "exited"])
            previous = current
        self.cli("rm", "web")
        self.assertFalse(self.directory().exists())
        self.assertEqual(len(self.cli("list").stdout.decode().splitlines()), 1)

    def test_names_can_be_subcommands(self):
        for name in ["start", "s", "stop", "list", "ls", "ps", "remove", "rm", "log", "logs", "help", "version"]:
            self.start(name)
            self.cli("stop", name)
            self.cli("rm", name)

    def test_detached_argv_cwd_environment_and_stdio(self):
        arg = 'two words "quoted"\n中文\\path'
        code = (
            "import json,os,sys,time;"
            "print(json.dumps([sys.argv[1:],os.getcwd(),os.getenv('BGX_TEST_VALUE'),sys.stdin.read()]),flush=True);"
            "print('stderr-line',file=sys.stderr,flush=True);"
            "print('ready',flush=True);time.sleep(120)"
        )
        before = time.monotonic()
        self.cli("start", "web", "--", sys.executable, "-c", code, arg, "", "--force")
        self.assertLess(time.monotonic() - before, 5)
        record = self.track()
        output = self.wait_log(b"ready")
        argv, cwd, value, stdin = json.loads(output.splitlines()[0])
        self.assertEqual(argv, [arg, "", "--force"])
        self.assertEqual(Path(cwd).resolve(), self.cwd.resolve())
        self.assertEqual(value, "inherited value")
        self.assertEqual(stdin, "")
        self.assertIn(b"stderr-line", output)
        self.assertEqual(record["argv"][-3:], [arg, "", "--force"])
        self.assertNotIn("inherited value", (self.directory() / "meta.json").read_text())
        self.assertIn(b"running", self.cli("ls", cwd=self.base).stdout)
        self.cli("stop", "web", cwd=self.base)
        self.assertEqual(self.cli("log", "web").stdout, output)
        self.assertNotIn("process_identity", self.record())
        self.cli("stop", "web")  # idempotent
        self.cli("rm", "web")
        self.assertFalse(self.directory().exists())
        self.assertTrue((self.state / "locks" / "web.lock").exists())

    def test_active_duplicate_preserves_record_and_log(self):
        self.start()
        meta = (self.directory() / "meta.json").read_bytes()
        output = (self.directory() / "output.log").read_bytes()
        self.cli("start", "web", "--", "/bin/echo", "other", expect=1)
        self.assertEqual(meta, (self.directory() / "meta.json").read_bytes())
        self.assertEqual(output, (self.directory() / "output.log").read_bytes())
        self.cli("rm", "web", expect=1)

    def test_concurrent_name_claim(self):
        args = [str(BINARY), "start", "same", "--", "/bin/sleep", "120"]
        children = [subprocess.Popen(args, env=self.env, cwd=self.cwd,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
        results = []
        for child in children:
            out, err = child.communicate(timeout=10)
            results.append(child.returncode)
            self.assertNotIn(b"AddressSanitizer", err)
        self.track("same")
        self.assertEqual(sorted(results), [0, 1, 1, 1])
        self.cli("stop", "same")

    def test_exec_failures_leave_no_artifacts(self):
        script = self.base / "not-executable"
        script.write_text("#!/bin/sh\nexit 0\n")
        for command in [str(self.base / "missing"), str(script)]:
            self.cli("start", "broken", "--", command, expect=1)
            self.assertFalse(self.directory("broken").exists())

    def test_path_lookup_and_no_implicit_shell(self):
        script = self.cwd / "custom-command"
        script.write_text("#!/bin/sh\nprintf 'path-ok\\n'\n")
        script.chmod(0o700)
        env = self.env.copy()
        env["PATH"] = str(self.cwd)
        self.cli("start", "web", "--", "custom-command", env=env)
        self.track()
        self.wait_exited()
        self.assertEqual(self.cli("log", "web").stdout, b"path-ok\n")
        marker = self.cwd / "should-not-exist"
        script.write_text("touch should-not-exist\n")
        self.cli("start", "web", "--", "custom-command", env=env, expect=1)
        self.assertFalse(marker.exists())
        self.assertFalse(self.directory().exists())

    def test_instant_exit_and_reuse_resets_log(self):
        self.cli("start", "web", "--", "/bin/echo", "old")
        self.track()
        self.wait_exited()
        self.assertEqual(self.cli("log", "web").stdout, b"old\n")
        self.cli("start", "web", "--", "/bin/echo", "new")
        self.track()
        self.wait_exited()
        self.assertEqual(self.cli("log", "web").stdout, b"new\n")
        self.assertEqual(sorted(p.name for p in self.directory().iterdir()), ["meta.json", "output.log"])
        self.cli("start", "web", "--", str(self.base / "missing"), expect=1)
        self.assertFalse(self.directory().exists())

    def test_log_is_full_raw_snapshot_and_missing_log_errors(self):
        self.start()
        self.cli("stop", "web")
        output = b"line\n" * 500 + b"\x00raw\xffwithout-final-newline"
        path = self.directory() / "output.log"
        path.write_bytes(output)
        self.assertEqual(self.cli("log", "web").stdout, output)
        path.unlink()
        self.cli("log", "web", expect=1)
        self.cli("log", "missing", expect=1)

    def test_slow_log_reader_does_not_hold_lock(self):
        self.start()
        self.cli("stop", "web")
        output = b"a" * (2 * 1024 * 1024)
        (self.directory() / "output.log").write_bytes(output)
        child = subprocess.Popen([str(BINARY), "log", "web"], env=self.env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        try:
            first = child.stdout.read(1)
            self.assertEqual(first, b"a")
            self.cli("rm", "web")  # should not wait for stdout consumer
            rest, errors = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, errors)
            self.assertEqual(first + rest, output)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_log_tail_preserves_lines_and_raw_bytes(self):
        self.start()
        self.cli("stop", "web")
        path = self.directory() / "output.log"
        samples = [b"", b"\n", b"\n\n", b"one", b"one\n", b"one\ntwo",
                   b"one\ntwo\n", b"one\n\n", b"a\r\nb\x00\xff\r\n", "一\n二".encode()]
        for size in [32766, 32767, 32768, 32769, 65536]:
            samples.extend([b"prefix\n" + b"x" * size + b"\nlast\n",
                            b"prefix\n" + b"x" * size])
        for data in samples:
            path.write_bytes(data)
            lines = data.split(b"\n")
            if data.endswith(b"\n"):
                lines.pop()
            for count in [0, 1, 2, 10, 18446744073709551615]:
                with self.subTest(length=len(data), count=count):
                    expected = b"\n".join(lines[-count:]) if count else b""
                    if count and data.endswith(b"\n"):
                        expected += b"\n"
                    self.assertEqual(self.cli("log", "web", "--tail", str(count)).stdout, expected)
        self.assertEqual(self.cli("log", "--tail", "1", "web").stdout, b"x" * 65536)
        self.cli("log", "missing", "--tail", "0", expect=1)

    def test_log_tail_rejects_invalid_counts(self):
        for count in ["", "-1", "+1", "1.5", " 1", "1 ", "1x", "1_000", "9" * 100]:
            self.cli("log", "web", "--tail", count, expect=2)
        for args in [("log", "web", "--tail"), ("log", "--tail", "1"),
                     ("log", "web", "--tail", "1", "--tail", "2")]:
            self.cli(*args, expect=2)
        self.assertFalse(self.state.exists())

    def test_log_tail_snapshot_releases_lock_and_ignores_appends(self):
        self.start()
        self.cli("stop", "web")
        path = self.directory() / "output.log"
        output = b"x" * (2 * 1024 * 1024) + b"\n"
        path.write_bytes(b"skip\n" + output)
        child = subprocess.Popen([str(BINARY), "log", "web", "--tail", "1"], env=self.env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        try:
            first = child.stdout.read(1)
            self.assertEqual(first, b"x")
            with path.open("ab") as stream:
                stream.write(b"new data\n")
            self.cli("rm", "web")
            rest, errors = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, errors)
            self.assertEqual(first + rest, output)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_identity_mismatch_and_starting_refuse_signals(self):
        original = self.start()
        variants = []
        wrong_birth = json.loads(json.dumps(original))
        wrong_birth["process_identity"]["start_marker"] += "-wrong"
        variants.append(wrong_birth)
        pending = json.loads(json.dumps(original))
        del pending["process_identity"]
        pending["starting"] = True
        variants.append(pending)
        for value in variants:
            with self.subTest(value=value):
                self.save(value)
                self.cli("stop", "web", expect=1)
                self.cli("rm", "web", expect=1)
                self.cli("start", "web", "--", "/bin/echo", "new", expect=1)
                os.kill(original["process_identity"]["pid"], 0)
        self.save(original)
        self.cli("stop", "web")

    def test_previous_boot_is_exited_and_never_signals_reused_pid(self):
        # Simulate an old record whose PID/PGID now refer to a live, unrelated
        # process. The boot mismatch must bypass all signaling of that process.
        for operation in ["stop", "rm", "reuse"]:
            with self.subTest(operation=operation):
                original = self.start()
                old = json.loads(json.dumps(original))
                old["process_identity"]["boot_id"] = "previous-boot"
                self.save(old)
                row = self.cli("ls").stdout.decode().splitlines()[1].split()
                self.assertEqual(row[:4], ["web", "exited", "-", "-"])
                if operation == "stop":
                    self.cli("stop", "web")
                    self.assertNotIn("process_identity", self.record())
                elif operation == "rm":
                    self.cli("rm", "web")
                    self.assertFalse(self.directory().exists())
                else:
                    self.cli("start", "web", "--", "/bin/echo", "reused")
                    self.track()
                    self.wait_exited()
                    self.assertEqual(self.cli("log", "web").stdout, b"reused\n")
                os.kill(original["process_identity"]["pid"], 0)
                self.cli("rm", "web", "--force")

    def test_uptime_is_hours_minutes_seconds_without_day_wrap(self):
        original = self.start()
        for elapsed in [0, 59, 60, 3599, 3600, 86400, 100 * 3600 + 125, -3600]:
            with self.subTest(elapsed=elapsed):
                started = int(time.time()) - elapsed
                record = json.loads(json.dumps(original))
                record["started_at"] = datetime.fromtimestamp(started, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                self.save(record)
                before = int(time.time())
                row = self.cli("ls").stdout.decode().splitlines()[1].split()
                after = int(time.time())
                self.assertRegex(row[3], r"^\d{2,}:\d{2}:\d{2}$")
                h, m, s = map(int, row[3].split(":"))
                self.assertLess(m, 60)
                self.assertLess(s, 60)
                self.assertGreaterEqual(h * 3600 + m * 60 + s, max(0, before - started))
                self.assertLessEqual(h * 3600 + m * 60 + s, max(0, after - started))
        self.save(original)
        self.cli("stop", "web")
        self.assertEqual(self.cli("ls").stdout.decode().splitlines()[1].split()[3], "-")

    def test_force_remove_does_not_stop_target_and_reuse_is_independent(self):
        original = self.start()
        (self.directory() / "meta.json").write_text("broken")
        self.cli("rm", "web", expect=1)
        result = self.cli("rm", "web", "--force")
        self.assertIn(b"no processes will be stopped", result.stderr)
        self.assertFalse(self.directory().exists())
        os.kill(original["process_identity"]["pid"], 0)
        self.start()
        self.cli("stop", "web")
        os.kill(original["process_identity"]["pid"], 0)
        self.cli("rm", "--force", "missing")

    def test_invalid_metadata_is_never_reset(self):
        original = self.start()
        path = self.directory() / "meta.json"
        base = json.dumps(original)
        variants = [
            base + "{}",
            base.replace('"schema_version": 1', '"schema_version": 2'),
            base.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1'),
            base.replace('"name": "web"', '"name": "other"'),
            base.replace('"name": "web"', '"name": "web\\u0000suffix"'),
            base[:-1] + ', "surprise": true}',
        ]
        for text in variants:
            with self.subTest(text=text):
                path.write_text(text)
                self.cli("stop", "web", expect=1)
                self.cli("start", "web", "--", "/bin/echo", "new", expect=1)
                self.assertEqual(path.read_text(), text)
        self.save(original)
        self.cli("stop", "web")

    def test_permissions_and_readonly_inspection(self):
        self.start()
        path = self.directory() / "meta.json"
        before = path.read_bytes()
        self.cli("ls")
        self.assertEqual(before, path.read_bytes())
        for directory in [self.state, self.state / "instances", self.directory(), self.state / "locks"]:
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        for file in [path, self.directory() / "output.log", self.state / "locks" / "web.lock"]:
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)

    def test_symlinks_are_not_followed(self):
        self.start()
        self.cli("stop", "web")
        outside = self.base / "evidence"
        outside.write_text("keep")
        meta = self.directory() / "meta.json"
        meta.unlink()
        meta.symlink_to(outside)
        self.cli("start", "web", "--", "/bin/echo", "new", expect=1)
        self.cli("rm", "web", "--force")
        self.assertEqual(outside.read_text(), "keep")
        self.directory().symlink_to(self.cwd, target_is_directory=True)
        self.cli("start", "web", "--", "/bin/echo", "new", expect=1)
        self.cli("rm", "web", "--force")
        self.assertTrue(self.cwd.exists())

    def test_stop_escalates_and_kills_descendant_after_root_exit(self):
        code = (
            "import os,signal,time;"
            "pid=os.fork();"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN) if pid==0 else None;"
            "print('ready-child' if pid==0 else 'ready-root',flush=True);"
            "time.sleep(120)"
        )
        self.start(code=code)
        self.wait_log(b"ready-child")
        before = time.monotonic()
        self.cli("stop", "web")
        elapsed = time.monotonic() - before
        self.assertGreaterEqual(elapsed, 4.8)
        self.assertLess(elapsed, 9)
        self.assertIn(b"exited", self.cli("ls").stdout)

    def test_state_root_precedence(self):
        env = self.env.copy()
        env.pop("BGX_STATE_HOME")
        env["HOME"] = str(self.base / "home")
        env["XDG_STATE_HOME"] = str(self.base / "xdg")
        self.cli("start", "quick", "--", "/bin/echo", "ok", env=env)
        self.assertTrue((self.base / "xdg" / "bgx" / "instances" / "quick" / "meta.json").exists())
        env.pop("XDG_STATE_HOME")
        self.cli("start", "quick", "--", "/bin/echo", "ok", env=env)
        self.assertTrue((self.base / "home" / ".local/state/bgx/instances/quick/meta.json").exists())

    def test_inherited_file_descriptors_are_closed(self):
        import fcntl
        with open(self.base / "sentinel", "w") as file:
            fd = fcntl.fcntl(file.fileno(), fcntl.F_DUPFD, 100)
            try:
                code = (
                    "import os,time\n"
                    "try:\n os.fstat(100); print('leaked',flush=True)\n"
                    "except OSError:\n print('ready closed',flush=True)\n"
                    "time.sleep(120)\n"
                )
                self.cli("start", "web", "--", sys.executable, "-c", code, pass_fds=(fd,))
                self.track()
                self.assertIn(b"ready closed", self.wait_log(b"ready"))
            finally:
                os.close(fd)


if __name__ == "__main__":
    unittest.main()
