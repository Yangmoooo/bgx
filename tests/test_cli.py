import json
from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path
import select
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
        # getcwd() reports the physical path (e.g. /private/var on macOS, where
        # /var is a symlink), so keep fixture paths physical for comparisons.
        self.base = Path(self.temp.name).resolve()
        self.state = self.base / "state"
        self.cwd = self.base / "project with spaces"
        self.cwd.mkdir()
        self.env = os.environ.copy()
        self.env["BGX_STATE_HOME"] = str(self.state)
        self.env["BGX_TEST_VALUE"] = "inherited value"
        self.groups = set()
        self.followers = []

    def tearDown(self):
        for child in self.followers:
            if child.poll() is None:
                child.kill()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
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

    def attempt(self, name="web"):
        return json.loads((self.directory(name) / "attempt.json").read_text())

    def write_attempt(self, value, name="web"):
        (self.directory(name) / "attempt.json").write_text(json.dumps(value))

    def attempt_value(self, name="web", argv=("/bin/sleep", "120"), cwd=None, log=None,
                      state="uncertain", identity=None, error=None):
        value = {
            "schema_version": 2,
            "name": name,
            "state": state,
            "argv": list(argv),
            "cwd": str(cwd or self.cwd),
            "started_at": "2026-01-02T03:04:05Z",
            "log": log or "log-99",
        }
        if identity:
            value["process_identity"] = identity
        if error:
            value["error"] = error
        return value

    def log_path(self, name="web"):
        if (self.directory(name) / "meta.json").exists():
            return self.directory(name) / self.record(name)["log"]
        return self.directory(name) / self.attempt(name)["log"]

    def stored_record(self, name):
        for path in (self.state / "instances").iterdir():
            record = json.loads((path / "meta.json").read_text())
            if record["name"] == name:
                self.groups.add(record["process_identity"]["pid"])
                return path, record
        self.fail(f"no record for {name!r}")

    def save(self, value, name="web"):
        (self.directory(name) / "meta.json").write_text(json.dumps(value))

    def track(self, name="web"):
        record = self.record(name)
        self.groups.add(record["process_identity"]["process_group"])
        return record

    def start(self, name="web", code=None, command="start"):
        if code is None:
            code = "import time; print('ready', flush=True); time.sleep(120)"
        result = self.cli(command, name, "--", sys.executable, "-c", code)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")
        record = self.track(name)
        self.wait_log(b"ready", name)
        return record

    def make_log(self, contents=b""):
        self.start()
        self.cli("stop", "web")
        path = self.log_path()
        path.write_bytes(contents)
        return path

    def wait_log(self, needle, name="web"):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            path = self.log_path(name)
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

    def spawn(self, command):
        child = subprocess.Popen(
            [str(BINARY), *command], env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
        )
        self.followers.append(child)
        return child

    def follow(self, *options, name="web"):
        return self.spawn(["log", name, *options, "--follow"])

    def expect_output(self, child, expected, timeout=5):
        data = b""
        deadline = time.monotonic() + timeout
        while len(data) < len(expected):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.fail(f"timed out with {data!r} waiting for {expected[len(data):]!r}")
            if not select.select([child.stdout], [], [], remaining)[0]:
                self.fail(f"timed out with {data!r} waiting for {expected[len(data):]!r}")
            chunk = os.read(child.stdout.fileno(), len(expected) - len(data))
            if not chunk:
                self.fail(f"output ended after {data!r} of {expected!r}")
            data += chunk
        self.assertEqual(data, expected)
        return data

    def expect_no_output(self, child, window=0.5):
        if not select.select([child.stdout], [], [], window)[0]:
            return
        extra = os.read(child.stdout.fileno(), 4096)
        if not extra:
            self.fail("follow ended before the viewer was interrupted")
        self.fail(f"unexpected follow output: {extra!r}")

    def stop_follow(self, child):
        child.send_signal(signal.SIGINT)
        out, err = child.communicate(timeout=5)
        self.assertEqual(child.returncode, -signal.SIGINT, (out, err))
        self.assertEqual(out, b"")
        self.assertEqual(err, b"")
        self.followers.remove(child)

    def wait_test_point(self, child, notice, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if notice.exists():
                status = subprocess.run(["ps", "-o", "stat=", "-p", str(child.pid)],
                                        capture_output=True, timeout=1).stdout.strip()
                if status.startswith(b"T"):
                    return
            if child.poll() is not None:
                self.fail("child exited before reaching the test point")
            time.sleep(0.005)
        self.fail("child did not reach the test point")

    def wait_for_name_lock(self, child, lock, timeout=5):
        inode = os.fstat(lock.fileno()).st_ino
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if child.poll() is not None:
                self.fail("child finished before its name lock was released")
            if sys.platform == "linux":
                # A blocked flock entry includes an arrow and the waiting PID.
                for line in Path("/proc/locks").read_text().splitlines():
                    fields = line.split()
                    if (len(fields) >= 8 and fields[1:4] == ["->", "FLOCK", "ADVISORY"]
                            and fields[5] == str(child.pid)
                            and fields[6].endswith(f":{inode}")):
                        return
            else:
                # After the checkpoint this single-threaded path has no sleeps;
                # wait for a sleeping child rather than merely a runnable one.
                status = subprocess.run(["ps", "-o", "stat=", "-p", str(child.pid)],
                                        capture_output=True, timeout=1).stdout.strip()
                if status.startswith(b"S"):
                    return
            time.sleep(0.005)
        self.fail("child did not block on its name lock")

    def interrupted_first_launch(self, name="web"):
        """Kill the launcher after durable pre-exec evidence, before release."""
        notice = self.base / "prep-notice"
        notice.unlink(missing_ok=True)
        env = dict(self.env, BGX_TEST_POINT="attempt_prepared", BGX_TEST_NOTIFY=str(notice))
        child = subprocess.Popen([os.environ["BGX_FAULT_BIN"], "start", name, "--", "/bin/sleep", "120"],
                                 env=env, cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.followers.append(child)
        self.wait_test_point(child, notice)
        child.kill()
        child.communicate(timeout=5)
        self.followers.remove(child)
        self.assertEqual(self.attempt(name)["state"], "prepared")
        self.assertFalse((self.directory(name) / "meta.json").exists())

    def test_invalid_arguments(self):
        for args in [
            ("web", "--", "true"), ("run", "web", "--", "true"),
            ("start",), ("start", "web"), ("start", "web", "true"),
            ("start", "web", "--"), ("start", "web", "--", ""),
            ("start", "../escape", "--", "true"),
            ("start", "Écho", "--", "true"),
            ("start", "a" * 129, "--", "true"),
            ("ls", "extra"), ("rm", "--force"),
            ("log", "web", "-f", "--tail"), ("log", "web", "--follow", "--follow"),
            ("log", "web", "--follow=1"), ("log", "-f"), ("log", "--follow", "--tail", "1"),
            ("log", "web", "--tail", "1", "-f", "extra"),
            ("info",), ("info", "web", "extra"), ("info", "../escape"), ("info", "web", "--json"),
            ("restart",), ("restart", "web", "extra"), ("restart", "web", "--", "true"),
            ("replace", "web"), ("replace", "web", "--"), ("replace", "web", "--", ""),
            ("replace", "../escape", "--", "true"), ("replace", "web", "true"),
            ("forget", "web"),
            ("prune", "web"), ("prune", "--dry-run", "web"),
            ("prune", "--dry-run", "--dry-run"), ("prune", "--force"),
            ("completion", "bash"), ("logpath", "web"),
            ("-v",), ("up", "web", "--", "true"), ("down", "web"),
        ]:
            with self.subTest(args=args):
                self.cli(*args, expect=2)

    def test_empty_list_does_not_create_state(self):
        self.assertIn(b"NAME", self.cli("ls").stdout)
        self.assertFalse(self.state.exists())

    def test_list_continues_after_invalid_records(self):
        self.start("aaa")
        self.start("bbb")
        self.start("ccc")
        meta = self.directory("bbb") / "meta.json"
        for contents in ["{broken", '{"schema_version": 999}']:
            with self.subTest(contents=contents):
                meta.write_text(contents)
                result = self.cli("list", expect=1)
                rows = [line.split() for line in result.stdout.decode().splitlines()[1:]]
                self.assertEqual([row[0] for row in rows], ["aaa", "bbb", "ccc"])
                self.assertEqual(rows[0][1], "running")
                self.assertEqual(rows[1], ["bbb", "unknown", "-", "-", "-"])
                self.assertEqual(rows[2][1], "running")
                self.assertIn(b"bgx: bbb:", result.stderr)
                self.assertIn(b"invalid", result.stderr)
                self.assertEqual(meta.read_text(), contents)

    def test_info_shows_recorded_fields_and_local_time(self):
        self.cli("start", "Web", "--", "/bin/sleep", "120")
        directory, record = self.stored_record("Web")
        record["started_at"] = "2026-01-02T03:04:05Z"
        meta = directory / "meta.json"
        meta.write_text(json.dumps(record))
        before = meta.read_bytes()
        for zone, expected in [("UTC0", "2026-01-02 03:04:05 +0000"),
                               ("CST-8", "2026-01-02 11:04:05 +0800"),
                               ("EST5", "2026-01-01 22:04:05 -0500")]:
            with self.subTest(zone=zone):
                result = self.cli("info", "Web", env=dict(self.env, TZ=zone))
                self.assertEqual(result.stderr, b"")
                fields = dict(line.split(":", 1) for line in result.stdout.decode().splitlines())
                fields = {key: value.strip() for key, value in fields.items()}
                self.assertEqual(fields["Name"], "Web")
                self.assertEqual(fields["Status"], "running")
                self.assertEqual(fields["Recorded PID"], str(record["process_identity"]["pid"]))
                self.assertEqual(fields["Recorded PGID"], str(record["process_identity"]["process_group"]))
                self.assertEqual(fields["Started"], expected)
                self.assertRegex(fields["Uptime"], r"^\d{2,}:\d{2}:\d{2}$")
                self.assertEqual(fields["Directory"], json.dumps(str(self.cwd)))
                self.assertEqual(fields["Command"], "/bin/sleep 120")
                self.assertEqual(Path(fields["Log"]).parent, directory)
                self.assertTrue(Path(fields["Log"]).name.startswith("log-"))
                self.assertNotIn("Reason", fields)
                self.assertEqual(meta.read_bytes(), before)
        self.cli("stop", "Web")
        result = self.cli("info", "Web")
        fields = {key: value.strip() for key, value in
                  (line.split(":", 1) for line in result.stdout.decode().splitlines())}
        self.assertEqual(fields["Status"], "exited")
        self.assertEqual(fields["Uptime"], "-")
        self.assertEqual(fields["Recorded PID"], str(record["process_identity"]["pid"]))

    def test_info_explains_stale_and_unknown_without_mutating_records(self):
        original = self.start()
        stale = deepcopy(original)
        stale["process_identity"]["start_marker"] += "-wrong"
        self.save(stale)
        before = (self.directory() / "meta.json").read_bytes()
        result = self.cli("info", "web")
        fields = {key: value.strip() for key, value in
                  (line.split(":", 1) for line in result.stdout.decode().splitlines())}
        self.assertEqual(result.stderr, b"")
        self.assertEqual(fields["Status"], "stale")
        self.assertIn("start marker", fields["Reason"])
        self.assertEqual(fields["Uptime"], "-")
        self.cli("stop", "web", expect=1)
        self.assertEqual((self.directory() / "meta.json").read_bytes(), before)
        os.kill(original["process_identity"]["pid"], 0)
        self.save(original)
        self.cli("stop", "web")
        # A pending attempt dominates the confirmed, already-ended execution and
        # is described with its own command, directory, and identity.
        self.write_attempt(self.attempt_value(argv=("/bin/other", "x"), error="launch interrupted"))
        before_meta = (self.directory() / "meta.json").read_bytes()
        before_attempt = (self.directory() / "attempt.json").read_bytes()
        result = self.cli("info", "web")
        fields = {key: value.strip() for key, value in
                  (line.split(":", 1) for line in result.stdout.decode().splitlines())}
        self.assertEqual(result.stderr, b"")
        self.assertEqual(fields["Status"], "unknown")
        self.assertIn("launch", fields["Reason"])
        self.assertEqual(fields["Recorded PID"], str(original["process_identity"]["pid"]))
        self.assertEqual(fields["Pending command"], "/bin/other x")
        self.assertEqual(fields["Pending PID"], "-")
        self.assertEqual(fields["Attempt error"], chr(34) + "launch interrupted" + chr(34))
        self.cli("stop", "web", expect=1)
        self.assertEqual((self.directory() / "meta.json").read_bytes(), before_meta)
        self.assertEqual((self.directory() / "attempt.json").read_bytes(), before_attempt)
        (self.directory() / "attempt.json").unlink()

    def test_info_missing_and_invalid_records_report_named_errors(self):
        result = self.cli("info", "missing", expect=1)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"instance not found: missing", result.stderr)
        self.assertFalse(self.state.exists())
        self.make_log()
        meta = self.directory() / "meta.json"
        for contents in ["{broken", '{"schema_version": 999}']:
            meta.write_text(contents)
            result = self.cli("info", "web", expect=1)
            self.assertEqual(result.stdout, b"")
            self.assertIn(b"web", result.stderr)
            self.assertIn(b"invalid", result.stderr)
            self.assertEqual(meta.read_text(), contents)
        result = self.cli("info", "missing", expect=1)
        self.assertEqual(result.stdout, b"")
        self.assertFalse((self.state / "locks" / "missing.lock").exists())

    def test_info_is_readonly_and_does_not_need_name_locks(self):
        import fcntl
        self.make_log()
        meta = self.directory() / "meta.json"
        before = meta.read_bytes()
        with (self.state / "locks" / "web.lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertIn(b"exited", self.cli("info", "web").stdout)
        (self.state / "locks" / "web.lock").unlink()
        (self.state / "locks").rmdir()
        (self.log_path()).unlink()  # Info does not require a log to exist.
        directories = [self.state, self.state / "instances", self.directory()]
        try:
            for directory in directories:
                directory.chmod(0o500)
            meta.chmod(0o400)
            self.assertIn(b"exited", self.cli("info", "web").stdout)
            self.assertFalse((self.state / "locks").exists())
            self.assertEqual(meta.read_bytes(), before)
            self.assertEqual(meta.stat().st_mode & 0o777, 0o400)
            for directory in directories:
                self.assertEqual(directory.stat().st_mode & 0o777, 0o500)
        finally:
            for directory in directories:
                directory.chmod(0o700)
            meta.chmod(0o600)

    def test_confirmed_completion_never_rechecks_historical_identity(self):
        unrelated = self.start("other")
        for operation in ["stop", "rm", "restart"]:
            with self.subTest(operation=operation):
                self.start()
                self.cli("stop", "web")
                ended = self.record()
                ended["process_identity"] = deepcopy(unrelated["process_identity"])
                ended["process_identity"]["start_marker"] += "-historical"
                self.save(ended)
                before = (self.directory() / "meta.json").read_bytes()
                self.assertIn(b"Status:        exited", self.cli("info", "web").stdout)
                self.assertEqual((self.directory() / "meta.json").read_bytes(), before)
                if operation == "restart":
                    self.cli("restart", "web")
                    self.wait_log(b"ready")
                    self.assertFalse(self.record()["exited"])
                    self.cli("stop", "web")
                else:
                    self.cli(operation, "web")
                    if operation == "stop":
                        self.assertEqual((self.directory() / "meta.json").read_bytes(), before)
                os.kill(unrelated["process_identity"]["pid"], 0)
                if self.directory().exists():
                    self.cli("rm", "web", "--force")
        self.cli("stop", "other")

    def test_legacy_records_remain_readable(self):
        original = self.start()
        legacy = deepcopy(original)
        legacy.pop("exited")
        legacy.pop("starting", None)
        self.save(legacy)
        self.assertIn(b"Status:        running", self.cli("info", "web").stdout)
        self.cli("stop", "web")
        self.assertEqual(self.record()["process_identity"], legacy["process_identity"])
        legacy.pop("process_identity")
        self.save(legacy)
        before = (self.directory() / "meta.json").read_bytes()
        result = self.cli("info", "web")
        self.assertIn(b"Status:        exited", result.stdout)
        self.assertIn(b"Recorded PID:  -", result.stdout)
        self.assertIn(b"Recorded PGID: -", result.stdout)
        self.assertEqual((self.directory() / "meta.json").read_bytes(), before)
        self.cli("stop", "web")
        self.cli("rm", "web")

    def test_conflicting_or_invalid_completion_markers_are_rejected(self):
        original = self.start()
        for changes in [{"starting": True, "exited": True}, {"exited": "yes"}, {"exited": 1}]:
            with self.subTest(changes=changes):
                record = deepcopy(original)
                record.update(changes)
                self.save(record)
                before = (self.directory() / "meta.json").read_bytes()
                self.cli("info", "web", expect=1)
                self.cli("stop", "web", expect=1)
                self.assertEqual((self.directory() / "meta.json").read_bytes(), before)
                os.kill(original["process_identity"]["pid"], 0)
        self.save(original)
        self.cli("stop", "web")

    def test_help_and_aliases(self):
        general = self.cli("help").stdout
        self.assertIn(b"bgx start NAME", general)
        for args in [(), ("-h",), ("--help",)]:
            self.assertEqual(self.cli(*args).stdout, general)
        commands = {"start": ["s"], "restart": [], "replace": [], "stop": [],
                    "list": ["ls", "ps"], "info": [], "remove": ["rm"],
                    "prune": [], "log": ["logs"], "help": ["-h", "--help"], "version": ["-V", "--version"]}
        for command, aliases in commands.items():
            with self.subTest(command=command):
                specific = self.cli("help", command).stdout
                self.assertNotEqual(specific, general)
                self.assertIn(specific.splitlines()[0], general)
                self.assertIn(f"bgx {command}".encode(), specific)
                for name in [command, *aliases]:
                    for args in [("help", name), (name, "-h"), (name, "--help")]:
                        self.assertEqual(self.cli(*args).stdout, specific, args)
        for alias in commands["help"]:
            self.assertEqual(self.cli(alias, "logs").stdout, self.cli("help", "log").stdout)
        version = self.cli("version").stdout
        self.assertTrue(version.startswith(b"bgx "))
        for alias in commands["version"]:
            self.assertEqual(self.cli(alias).stdout, version)
        self.assertNotIn(b"bgx start", self.cli("help", "log").stdout)
        self.assertIn(b"--tail N", self.cli("help", "log").stdout)
        self.assertIn(b"--follow", self.cli("help", "log").stdout)
        self.assertIn(b"--dry-run", self.cli("help", "prune").stdout)
        self.cli("help", "missing", expect=2)
        self.assertFalse(self.state.exists())

    def test_command_aliases_manage_same_task(self):
        for removal in ["remove", "rm"]:
            self.start(command="s")
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

    def test_stop_preserves_launch_metadata_and_records_completion(self):
        original = self.start()
        log_path = self.log_path()
        log = log_path.read_bytes()
        self.cli("stop", "web")
        stopped = self.record()
        self.assertEqual(stopped.get("process_identity"), original["process_identity"])
        self.assertTrue(stopped["exited"])
        for key, value in original.items():
            if key != "exited":
                self.assertEqual(stopped[key], value, key)
        self.assertEqual(log_path.read_bytes(), log)
        saved = (self.directory() / "meta.json").read_bytes()
        self.cli("stop", "web")
        self.assertEqual((self.directory() / "meta.json").read_bytes(), saved)
        self.assertIn(b"exited", self.cli("list").stdout)

    def test_stopped_name_can_be_restarted_and_removed(self):
        previous = None
        for _ in range(3):
            if previous is None:
                self.cli("s", "web", "--", "/bin/sleep", "120")
            else:
                self.cli("restart", "web")
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

    @unittest.skipUnless(sys.platform == "linux" and os.geteuid() != 0,
                         "requires a non-root Linux process")
    def test_stop_with_unreaped_root_does_not_become_stale(self):
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        previous = ctypes.c_int()
        self.assertEqual(libc.prctl(37, ctypes.byref(previous), 0, 0, 0), 0)  # GET_CHILD_SUBREAPER
        self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)  # SET_CHILD_SUBREAPER
        pid = None
        try:
            pid = self.start()["process_identity"]["pid"]
            # Adopt the target when bgx exits. Keeping it unreaped makes the
            # /proc ownership change during exit deterministic, without PID reuse.
            self.cli("stop", "web")
            self.assertIn(b"exited", self.cli("list").stdout)
            self.cli("rm", "web")
            self.assertFalse(self.directory().exists())
        finally:
            if pid is not None:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                self.groups.discard(pid)
            self.assertEqual(libc.prctl(36, previous.value, 0, 0, 0), 0)

    @unittest.skipUnless(sys.platform == "linux" and os.geteuid() != 0,
                         "requires a non-root Linux process")
    def test_nondumpable_task_uses_effective_uid_not_proc_owner(self):
        record = self.start(code=(
            "import ctypes,time;"
            "libc=ctypes.CDLL(None);"
            "assert libc.prctl(4,0,0,0,0)==0;"  # PR_SET_DUMPABLE
            "print('ready',flush=True);time.sleep(120)"
        ))
        self.assertEqual(os.stat(f"/proc/{record['process_identity']['pid']}/stat").st_uid, 0)
        self.assertIn(b"running", self.cli("list").stdout)
        self.cli("stop", "web")
        self.cli("rm", "web")

    def test_names_can_be_subcommands(self):
        for name in ["start", "s", "restart", "replace", "stop", "list", "ls", "ps", "info",
                     "remove", "rm", "prune", "log", "logs", "help", "version"]:
            self.start(name)
            self.cli("stop", name)
            self.cli("rm", name)

    def test_case_sensitive_names_remain_independent(self):
        names = ["web", "Web", "WEB", "wEb"]
        stored = {}
        for name in names:
            self.cli("s", name, "--", "/bin/sleep", "120")
            path, record = self.stored_record(name)
            stored[name] = (path, record)
            (path / record["log"]).write_bytes((name + "\n").encode())
        # Storage keys must remain distinct even when the filesystem folds case.
        self.assertEqual(len({p.name.casefold() for p, _ in stored.values()}), len(names))
        locks = list((self.state / "locks").iterdir())
        self.assertEqual(len({p.stat().st_ino for p in locks}), len(names))
        rows = [line.split() for line in self.cli("list").stdout.decode().splitlines()[1:]]
        self.assertEqual([row[0] for row in rows], sorted(names))
        self.assertTrue(all(row[1] == "running" for row in rows))
        for name in names:
            self.assertEqual(self.cli("log", name).stdout, (name + "\n").encode())
        self.cli("s", "Web", "--", "/bin/sleep", "120", expect=1)
        self.cli("stop", "Web")
        self.cli("restart", "Web")
        _, restarted = self.stored_record("Web")
        self.assertNotEqual(restarted["process_identity"]["pid"], stored["Web"][1]["process_identity"]["pid"])
        self.assertEqual(self.cli("log", "Web").stdout, b"")
        self.assertNotEqual(restarted["log"], stored["Web"][1]["log"])
        self.cli("rm", "WEB", "--force")
        self.assertFalse(stored["WEB"][0].exists())
        os.kill(stored["WEB"][1]["process_identity"]["pid"], 0)
        for name in ["web", "wEb"]:
            self.assertEqual(self.cli("log", name).stdout, (name + "\n").encode())
            os.kill(stored[name][1]["process_identity"]["pid"], 0)
        for name in ["web", "Web", "wEb"]:
            self.cli("stop", name)
            self.cli("rm", name)
        self.assertEqual(len(self.cli("list").stdout.decode().splitlines()), 1)

    def test_case_sensitive_names_preserve_length_limit(self):
        names = ["A" * 128, "a" * 128, "A" + "0_-" * 42 + "Z", "a" + "0_-" * 42 + "z"]
        for name in names:
            self.cli("s", name, "--", "/bin/sleep", "120")
            path, _ = self.stored_record(name)
            self.assertLessEqual(len(path.name), 255)
        listed = self.cli("list").stdout.decode().splitlines()[1:]
        self.assertEqual({line.split()[0] for line in listed}, set(names))
        for name in names:
            self.cli("stop", name)
            self.cli("rm", name)
        self.cli("s", "A" * 129, "--", "/bin/sleep", "120", expect=2)

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
        self.assertEqual(self.record()["process_identity"], record["process_identity"])
        self.assertTrue(self.record()["exited"])
        self.cli("stop", "web")  # idempotent
        self.cli("rm", "web")
        self.assertFalse(self.directory().exists())
        self.assertTrue((self.state / "locks" / "web.lock").exists())

    def test_active_duplicate_preserves_record_and_log(self):
        self.start()
        meta = (self.directory() / "meta.json").read_bytes()
        output = self.log_path().read_bytes()
        self.cli("start", "web", "--", "/bin/echo", "other", expect=1)
        self.assertEqual(meta, (self.directory() / "meta.json").read_bytes())
        self.assertEqual(output, self.log_path().read_bytes())
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
        self.cli("rm", "web")
        self.cli("start", "web", "--", "custom-command", env=env, expect=1)
        self.assertFalse(marker.exists())
        self.assertFalse(self.directory().exists())

    def test_restart_and_replace_get_fresh_logs(self):
        self.cli("start", "web", "--", "/bin/echo", "old")
        self.track()
        self.wait_exited()
        self.assertEqual(self.cli("log", "web").stdout, b"old\n")
        first_log = self.log_path()
        first_inode = first_log.stat().st_ino
        self.cli("restart", "web")
        self.track()
        self.wait_exited()
        self.assertEqual(self.cli("log", "web").stdout, b"old\n")
        second_log = self.log_path()
        self.assertNotEqual(first_log.name, second_log.name)
        self.assertNotEqual(first_log.stat().st_ino if first_log.exists() else first_inode,
                            second_log.stat().st_ino)
        self.cli("replace", "web", "--", "/bin/echo", "new")
        self.track()
        self.wait_exited()
        self.assertEqual(self.cli("log", "web").stdout, b"new\n")
        self.assertEqual(sorted(p.name for p in self.directory().iterdir()),
                         sorted(["meta.json", self.record()["log"]]))
        # A failed replacement keeps the confirmed definition and its log.
        self.cli("replace", "web", "--", str(self.base / "missing"), expect=1)
        self.assertEqual(self.cli("log", "web").stdout, b"new\n")
        self.cli("restart", "web")
        self.track()
        self.wait_exited()
        self.assertEqual(self.cli("log", "web").stdout, b"new\n")
        self.cli("rm", "web")

    def test_log_missing_instance_does_not_create_state_or_locks(self):
        result = self.cli("log", "missing", expect=1)
        self.assertIn(b"instance not found: missing", result.stderr)
        self.assertFalse(self.state.exists())
        (self.state / "instances").mkdir(parents=True)
        result = self.cli("log", "missing", expect=1)
        self.assertIn(b"instance not found: missing", result.stderr)
        self.assertFalse((self.state / "locks").exists())
        self.assertEqual(list((self.state / "instances").iterdir()), [])

    def test_log_reads_readonly_state_without_locks(self):
        path = self.make_log(b"saved output\n")
        (self.state / "locks" / "web.lock").unlink()
        (self.state / "locks").rmdir()
        directories = [self.state, self.state / "instances", self.directory()]
        files = [self.directory() / "meta.json", path]
        try:
            for directory in directories:
                directory.chmod(0o500)
            for file in files:
                file.chmod(0o400)
            self.assertEqual(self.cli("log", "web").stdout, b"saved output\n")
            self.assertFalse((self.state / "locks").exists())
            for directory in directories:
                self.assertEqual(directory.stat().st_mode & 0o777, 0o500)
            for file in files:
                self.assertEqual(file.stat().st_mode & 0o777, 0o400)
        finally:
            for directory in directories:
                directory.chmod(0o700)
            for file in files:
                file.chmod(0o600)

    def test_log_does_not_wait_for_name_lock(self):
        import fcntl
        self.make_log(b"saved output\n")
        with (self.state / "locks" / "web.lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(self.cli("log", "web").stdout, b"saved output\n")

    @unittest.skipUnless(os.environ.get("BGX_FAULT_BIN"), "requires fault-test binary")
    def test_log_does_not_switch_to_replacement_after_loading_record(self):
        self.make_log(b"old output\n")
        notice = self.base / "log-notice"
        env = dict(self.env, BGX_TEST_POINT="log_record_loaded", BGX_TEST_NOTIFY=str(notice))
        child = subprocess.Popen([os.environ["BGX_FAULT_BIN"], "log", "web"], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if notice.exists():
                    status = subprocess.run(["ps", "-o", "stat=", "-p", str(child.pid)],
                                            capture_output=True, timeout=1).stdout.strip()
                    if status.startswith(b"T"):
                        break
                if child.poll() is not None:
                    self.fail("log exited before reaching the read checkpoint")
                time.sleep(0.005)
            else:
                self.fail("log did not reach the read checkpoint")
            self.cli("replace", "web", "--", "/bin/echo", "new")
            os.kill(child.pid, signal.SIGCONT)
            out, err = child.communicate(timeout=3)
            self.assertEqual(child.returncode, 1, err)
            self.assertEqual(out, b"")
            # The retired log serial is gone; the reader must not print the new execution.
            self.assertIn(b"log-", err)
            self.assertNotIn(b"AddressSanitizer", err)
            self.assertNotIn(b"runtime error:", err)
            self.wait_log(b"new")
            self.assertEqual(self.cli("log", "web").stdout, b"new\n")
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=3)

    def test_log_rejects_invalid_record(self):
        self.make_log(b"saved output\n")
        path = self.directory() / "meta.json"
        path.write_text("{broken")
        result = self.cli("log", "web", expect=1)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"invalid JSON metadata", result.stderr)
        self.assertEqual(path.read_text(), "{broken")

    def test_log_is_full_raw_snapshot_and_missing_log_errors(self):
        output = b"line\n" * 500 + b"\x00raw\xffwithout-final-newline"
        path = self.make_log(output)
        self.assertEqual(self.cli("log", "web").stdout, output)
        path.unlink()
        self.cli("log", "web", expect=1)
        self.cli("log", "missing", expect=1)

    def test_log_tail_preserves_lines_and_raw_bytes(self):
        path = self.make_log()
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

    def test_log_snapshots_survive_removal_and_ignore_appends(self):
        cases = [([], b"", b"a" * (2 * 1024 * 1024)),
                 (["--tail", "1"], b"skip\n", b"x" * (2 * 1024 * 1024) + b"\n")]
        for options, prefix, output in cases:
            with self.subTest(options=options):
                path = self.make_log(prefix + output)
                child = subprocess.Popen([str(BINARY), "log", "web", *options], env=self.env,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
                try:
                    first = child.stdout.read(1)
                    self.assertEqual(first, output[:1])
                    with path.open("ab") as stream:
                        stream.write(b"new data\n")
                    self.cli("rm", "web")  # Must not wait for the slow stdout consumer.
                    rest, errors = child.communicate(timeout=5)
                    self.assertEqual(child.returncode, 0, errors)
                    self.assertEqual(first + rest, output)
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait()

    def test_log_follow_prints_history_then_appends_raw_bytes(self):
        marker = self.base / "append-now"
        code = (
            "import os,sys,time\n"
            "print('one',flush=True)\n"
            "sys.stdout.write('partial')\n"
            "sys.stdout.flush()\n"
            "while not os.path.exists(sys.argv[1]): time.sleep(0.01)\n"
            "sys.stdout.write('-two'+chr(10))\n"
            "sys.stdout.flush()\n"
            "time.sleep(120)\n"
        )
        self.cli("start", "web", "--", sys.executable, "-c", code, str(marker))
        record = self.track()
        child = self.follow()
        self.expect_output(child, b"one\npartial")
        self.expect_no_output(child)
        marker.write_text("go")
        self.expect_output(child, b"-two\n")
        self.expect_no_output(child)
        self.stop_follow(child)
        os.kill(record["process_identity"]["pid"], 0)
        self.assertIn(b"Status:        running", self.cli("info", "web").stdout)
        self.cli("stop", "web")

    def test_log_follow_tail_selection_and_zero_starts_at_end(self):
        path = self.make_log(b"a\nb\nc\n")
        child = self.follow("--tail", "1")
        self.expect_output(child, b"c\n")
        self.expect_no_output(child)
        with path.open("ab") as stream:
            stream.write(b"d\n")
        self.expect_output(child, b"d\n")
        self.stop_follow(child)
        child = self.follow("--tail", "0")
        self.expect_no_output(child)
        with path.open("ab") as stream:
            stream.write(b"new\n")
        self.expect_output(child, b"new\n")
        self.stop_follow(child)

    def test_log_follow_accepts_options_before_or_after_name_and_logs_alias(self):
        self.make_log(b"kept\n")
        for command in [["log", "-f", "web"],
                        ["log", "--tail", "1", "web", "-f"],
                        ["log", "web", "-f", "--tail", "1"],
                        ["logs", "--follow", "web", "--tail", "1"]]:
            with self.subTest(command=command):
                child = self.spawn(command)
                self.expect_output(child, b"kept\n")
                self.expect_no_output(child)
                self.stop_follow(child)

    def test_log_follow_survives_unlink_with_live_writer(self):
        marker = self.base / "after-unlink"
        code = (
            "import os,sys,time\n"
            "print('ready',flush=True)\n"
            "while not os.path.exists(sys.argv[1]): time.sleep(0.01)\n"
            "print('after-unlink',flush=True)\n"
            "time.sleep(120)\n"
        )
        self.cli("start", "web", "--", sys.executable, "-c", code, str(marker))
        self.track()
        child = self.follow()
        self.expect_output(child, b"ready\n")
        (self.log_path()).unlink()
        marker.write_text("go")
        self.expect_output(child, b"after-unlink\n")
        self.stop_follow(child)
        self.cli("stop", "web")
        self.cli("rm", "web")

    def test_log_follow_keeps_original_file_when_task_is_replaced(self):
        marker = self.base / "exit-now"
        code = (
            "import os,sys,time\n"
            "print('alpha',flush=True)\n"
            "while not os.path.exists(sys.argv[1]): time.sleep(0.01)\n"
            "print('alpha2',flush=True)\n"
        )
        self.cli("start", "web", "--", sys.executable, "-c", code, str(marker))
        self.track()
        child = self.follow()
        self.expect_output(child, b"alpha\n")
        marker.write_text("go")
        self.expect_output(child, b"alpha2\n")
        self.wait_exited()
        self.cli("replace", "web", "--", "/bin/echo", "beta")
        self.track()
        self.wait_log(b"beta")
        self.assertIsNone(child.poll())
        self.expect_no_output(child)
        self.stop_follow(child)
        self.wait_exited()
        self.cli("rm", "web")

    def test_log_follow_recovers_from_observed_truncation(self):
        path = self.make_log(b"first line\nsecond line\n")
        child = self.follow()
        self.expect_output(child, b"first line\nsecond line\n")
        self.expect_no_output(child)
        path.write_bytes(b"replacement\n")
        self.expect_output(child, b"replacement\n")
        self.expect_no_output(child)
        self.stop_follow(child)

    def test_log_follow_stays_after_task_exit_and_interrupt_ends_only_viewer(self):
        path = self.make_log(b"done\n")
        child = self.follow()
        self.expect_output(child, b"done\n")
        self.expect_no_output(child)
        self.assertIsNone(child.poll())
        self.assertIn(b"exited", self.cli("info", "web").stdout)
        with path.open("ab") as stream:
            stream.write(b"after exit\n")
        self.expect_output(child, b"after exit\n")
        self.expect_no_output(child)
        self.stop_follow(child)

    def test_log_follow_startup_errors_are_immediate_and_readonly(self):
        result = self.cli("log", "missing", "--follow", expect=1)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"instance not found: missing", result.stderr)
        self.assertFalse(self.state.exists())
        path = self.make_log(b"data\n")
        meta = self.directory() / "meta.json"
        record = self.record()
        locks = sorted(entry.name for entry in (self.state / "locks").iterdir())
        path.unlink()
        result = self.cli("log", "-f", "web", expect=1)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"log-", result.stderr)
        path.write_bytes(b"data\n")
        meta.write_text("{broken")
        result = self.cli("log", "web", "--follow", expect=1)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"invalid JSON metadata", result.stderr)
        self.assertEqual(meta.read_text(), "{broken")
        self.save(record)
        if os.geteuid() != 0:
            path.chmod(0)
            try:
                result = self.cli("log", "web", "-f", expect=1)
                self.assertEqual(result.stdout, b"")
                self.assertIn(b"log-", result.stderr)
            finally:
                path.chmod(0o600)
        self.assertEqual(sorted(entry.name for entry in (self.state / "locks").iterdir()), locks)

    def test_log_follow_is_readonly_and_ignores_name_lock(self):
        import fcntl
        path = self.make_log(b"saved output\n")
        meta = self.directory() / "meta.json"
        with (self.state / "locks" / "web.lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            child = self.follow()
            self.expect_output(child, b"saved output\n")
            self.stop_follow(child)
        (self.state / "locks" / "web.lock").unlink()
        (self.state / "locks").rmdir()
        before = meta.read_bytes()
        directories = [self.state, self.state / "instances", self.directory()]
        files = [meta, path]
        try:
            for directory in directories:
                directory.chmod(0o500)
            for file in files:
                file.chmod(0o400)
            child = self.follow()
            self.expect_output(child, b"saved output\n")
            self.expect_no_output(child)
            self.stop_follow(child)
            self.assertFalse((self.state / "locks").exists())
            self.assertEqual(meta.read_bytes(), before)
            for directory in directories:
                self.assertEqual(directory.stat().st_mode & 0o777, 0o500)
            for file in files:
                self.assertEqual(file.stat().st_mode & 0o777, 0o400)
        finally:
            for directory in directories:
                directory.chmod(0o700)
            for file in files:
                file.chmod(0o600)

    def test_log_follow_pipe_closure_during_initial_history_exits_zero(self):
        self.make_log(b"x" * (4 * 1024 * 1024))
        child = self.follow()
        child.stdout.close()
        self.assertEqual(child.wait(timeout=10), 0)
        self.assertEqual(child.stderr.read(), b"")
        child.stderr.close()
        self.followers.remove(child)

    def test_log_follow_pipe_closure_on_later_write_exits_zero(self):
        path = self.make_log(b"start\n")
        child = self.follow()
        self.expect_output(child, b"start\n")
        self.expect_no_output(child)
        child.stdout.close()
        with path.open("ab") as stream:
            stream.write(b"later\n")
        self.assertEqual(child.wait(timeout=10), 0)
        self.assertEqual(child.stderr.read(), b"")
        child.stderr.close()
        self.followers.remove(child)

    def test_log_without_follow_still_reports_pipe_closure(self):
        self.make_log(b"payload\n" * 1000000)
        child = self.spawn(["log", "web"])
        child.stdout.close()
        self.assertEqual(child.wait(timeout=10), 1)
        self.assertIn(b"Broken pipe", child.stderr.read())
        child.stderr.close()
        self.followers.remove(child)

    def test_identity_mismatch_and_pending_attempt_refuse_signals(self):
        original = self.start()
        wrong_birth = deepcopy(original)
        wrong_birth["process_identity"]["start_marker"] += "-wrong"
        self.save(wrong_birth)
        before = (self.directory() / "meta.json").read_bytes()
        for args in [("stop", "web"), ("rm", "web"), ("start", "web", "--", "/bin/echo", "new"),
                     ("restart", "web"), ("replace", "web", "--", "/bin/echo", "new")]:
            self.cli(*args, expect=1)
        self.assertEqual((self.directory() / "meta.json").read_bytes(), before)
        os.kill(original["process_identity"]["pid"], 0)
        self.save(original)
        self.cli("stop", "web")
        # A pending replacement attempt dominates the ended execution.
        self.write_attempt(self.attempt_value())
        before_meta = (self.directory() / "meta.json").read_bytes()
        for args in [("stop", "web"), ("rm", "web"), ("start", "web", "--", "/bin/true"),
                     ("restart", "web"), ("replace", "web", "--", "/bin/true")]:
            with self.subTest(args=args):
                self.cli(*args, expect=1)
        self.assertEqual((self.directory() / "meta.json").read_bytes(), before_meta)
        self.assertTrue((self.directory() / "attempt.json").exists())
        self.assertIn(b"unknown", self.cli("list").stdout)
        self.cli("rm", "web", "--force")
        self.assertFalse(self.directory().exists())

    def test_previous_boot_is_exited_and_never_signals_reused_pid(self):
        # Simulate an old record whose PID/PGID now refer to a live, unrelated
        # process. The boot mismatch must bypass all signaling of that process.
        for operation in ["stop", "rm", "restart"]:
            with self.subTest(operation=operation):
                original = self.start()
                old = deepcopy(original)
                old["process_identity"]["boot_id"] = "previous-boot"
                self.save(old)
                row = self.cli("ls").stdout.decode().splitlines()[1].split()
                self.assertEqual(row[:4], ["web", "exited", "-", "-"])
                if operation == "stop":
                    self.cli("stop", "web")
                    self.assertEqual(self.record()["process_identity"], old["process_identity"])
                    self.assertTrue(self.record()["exited"])
                elif operation == "rm":
                    self.cli("rm", "web")
                    self.assertFalse(self.directory().exists())
                else:
                    self.cli("restart", "web")
                    self.track()
                    self.wait_log(b"ready")
                    self.assertFalse(self.record()["exited"])
                os.kill(original["process_identity"]["pid"], 0)
                self.cli("rm", "web", "--force")

    def test_uptime_is_hours_minutes_seconds_without_day_wrap(self):
        original = self.start()
        for elapsed in [0, 59, 60, 3599, 3600, 86400, 100 * 3600 + 125, -3600]:
            with self.subTest(elapsed=elapsed):
                started = int(time.time()) - elapsed
                record = deepcopy(original)
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

    def test_prune_removes_only_exited_tasks(self):
        self.start("live")
        self.cli("start", "done", "--", "/bin/echo", "done")
        self.track("done")
        self.wait_exited("done")
        self.start("stopped")
        self.cli("stop", "stopped")
        live = (self.directory("live") / "meta.json").read_bytes()
        result = self.cli("prune")
        self.assertEqual(result.stdout, b"Removed done\nRemoved stopped\nRemoved 2 task(s).\n")
        self.assertEqual(result.stderr, b"")
        self.assertFalse(self.directory("done").exists())
        self.assertFalse(self.directory("stopped").exists())
        self.assertEqual((self.directory("live") / "meta.json").read_bytes(), live)
        self.assertIn(b"running", self.cli("ls").stdout)
        # Lock files coordinate naming; pruning retains them.
        self.assertTrue((self.state / "locks" / "done.lock").exists())
        self.assertTrue((self.state / "locks" / "stopped.lock").exists())
        self.cli("stop", "live")
        result = self.cli("prune")
        self.assertEqual(result.stdout, b"Removed live\nRemoved 1 task(s).\n")
        self.assertFalse(self.directory("live").exists())

    def test_prune_dry_run_previews_without_mutation_or_locks(self):
        import fcntl
        self.start("live")
        self.start("stopped")
        self.cli("stop", "stopped")

        def snapshot():
            entries = {}
            for path in self.state.rglob("*"):
                entry = [path.stat().st_mode & 0o777, None]
                if path.is_file():
                    entry[1] = path.read_bytes()
                entries[str(path.relative_to(self.state))] = entry
            return entries

        before = snapshot()
        with (self.state / "locks" / "stopped.lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self.cli("prune", "--dry-run")
        self.assertEqual(result.stdout, b"Would remove stopped\nWould remove 1 task(s).\n")
        self.assertEqual(result.stderr, b"")
        self.assertEqual(snapshot(), before)
        # A preview neither needs nor creates the lock directory.
        (self.state / "locks" / "live.lock").unlink()
        (self.state / "locks" / "stopped.lock").unlink()
        (self.state / "locks").rmdir()
        before = snapshot()
        result = self.cli("prune", "--dry-run")
        self.assertEqual(result.stdout, b"Would remove stopped\nWould remove 1 task(s).\n")
        self.assertEqual(result.stderr, b"")
        self.assertFalse((self.state / "locks").exists())
        self.assertEqual(snapshot(), before)

    def test_prune_absent_and_empty_state_succeed_without_creation(self):
        absent = self.base / "absent"
        env = dict(self.env, BGX_STATE_HOME=str(absent))
        for args, expected in [(('prune',), b"Removed 0 task(s).\n"),
                               (('prune', '--dry-run'), b"Would remove 0 task(s).\n")]:
            with self.subTest(args=args):
                result = self.cli(*args, env=env)
                self.assertEqual(result.stdout, expected)
                self.assertEqual(result.stderr, b"")
                self.assertFalse(absent.exists())
        (absent / "instances").mkdir(parents=True)
        result = self.cli("prune", env=env)
        self.assertEqual(result.stdout, b"Removed 0 task(s).\n")
        self.assertEqual(result.stderr, b"")
        self.assertFalse((absent / "locks").exists())
        self.assertEqual(list((absent / "instances").iterdir()), [])

    def test_prune_creates_missing_lock_directory_for_locking(self):
        self.start("web")
        self.cli("stop", "web")
        (self.state / "locks" / "web.lock").unlink()
        (self.state / "locks").rmdir()
        result = self.cli("prune")
        self.assertEqual(result.stdout, b"Removed web\nRemoved 1 task(s).\n")
        self.assertEqual(result.stderr, b"")
        self.assertFalse(self.directory().exists())
        self.assertEqual((self.state / "locks").stat().st_mode & 0o777, 0o700)

    def test_prune_reports_damaged_and_uncertain_records_and_continues(self):
        self.cli("start", "aaa", "--", "/bin/echo", "ok")
        self.track("aaa")
        self.wait_exited("aaa")
        self.cli("start", "bbb", "--", "/bin/echo", "ok")
        self.track("bbb")
        self.wait_exited("bbb")
        (self.directory("bbb") / "meta.json").write_text("{broken")
        stale = self.start("ccc")
        wrong = deepcopy(stale)
        wrong["process_identity"]["start_marker"] += "-wrong"
        self.save(wrong, "ccc")
        self.start("ddd")
        self.cli("stop", "ddd")
        self.write_attempt(self.attempt_value(name="ddd"), "ddd")
        self.start("eee")
        self.cli("stop", "eee")
        records = {name: (self.directory(name) / "meta.json").read_bytes()
                   for name in ["bbb", "ccc", "ddd"]}
        attempts = {name: (self.directory(name) / "attempt.json").read_bytes() for name in ["ddd"]}
        result = self.cli("prune", "--dry-run", expect=1)
        self.assertEqual(result.stdout, b"Would remove aaa\nWould remove eee\nWould remove 2 task(s).\n")
        self.assertIn(b"bgx: bbb: invalid JSON metadata", result.stderr)
        self.assertIn(b"bgx: ccc: process start marker does not match recorded identity", result.stderr)
        self.assertIn(b"bgx: ddd: launch outcome has not been confirmed", result.stderr)
        for name, contents in records.items():
            self.assertEqual((self.directory(name) / "meta.json").read_bytes(), contents, name)
        for name, contents in attempts.items():
            self.assertEqual((self.directory(name) / "attempt.json").read_bytes(), contents, name)
        result = self.cli("prune", expect=1)
        self.assertEqual(result.stdout, b"Removed aaa\nRemoved eee\nRemoved 2 task(s).\n")
        self.assertIn(b"bgx: some tasks could not be pruned", result.stderr)
        self.assertFalse(self.directory("aaa").exists())
        self.assertFalse(self.directory("eee").exists())
        for name, contents in records.items():
            self.assertEqual((self.directory(name) / "meta.json").read_bytes(), contents, name)
        os.kill(stale["process_identity"]["pid"], 0)

    def test_prune_removes_failed_attempt_data_with_the_task(self):
        self.start()
        self.cli("stop", "web")
        self.cli("replace", "web", "--", str(self.base / "missing"), expect=1)
        self.assertTrue((self.directory() / "attempt.json").exists())
        result = self.cli("prune")
        self.assertEqual(result.stdout, b"Removed web\nRemoved 1 task(s).\n")
        self.assertFalse(self.directory().exists())

    @unittest.skipIf(os.geteuid() == 0, "requires a non-root user")
    def test_prune_continues_after_deletion_failure(self):
        for name in ["aaa", "bbb"]:
            self.start(name)
            self.cli("stop", name)
        blocked = self.directory("aaa")
        blocked.chmod(0o500)
        try:
            result = self.cli("prune", expect=1)
            self.assertEqual(result.stdout, b"Removed bbb\nRemoved 1 task(s).\n")
            self.assertIn(b"bgx: aaa:", result.stderr)
            self.assertIn(b"bgx: some tasks could not be pruned", result.stderr)
            self.assertTrue((blocked / "meta.json").exists())
            self.assertFalse(self.directory("bbb").exists())
        finally:
            blocked.chmod(0o700)
        result = self.cli("prune")
        self.assertEqual(result.stdout, b"Removed aaa\nRemoved 1 task(s).\n")
        self.assertFalse(blocked.exists())

    def test_prune_continues_after_lock_error(self):
        for name in ["aaa", "bbb"]:
            self.start(name)
            self.cli("stop", name)
        lock = self.state / "locks" / "aaa.lock"
        lock.unlink()
        lock.symlink_to(self.base / "elsewhere")
        result = self.cli("prune", expect=1)
        self.assertEqual(result.stdout, b"Removed bbb\nRemoved 1 task(s).\n")
        self.assertIn(b"bgx: aaa:", result.stderr)
        self.assertIn(b"bgx: some tasks could not be pruned", result.stderr)
        self.assertTrue(self.directory("aaa").exists())
        self.assertFalse(self.directory("bbb").exists())
        self.assertTrue(lock.is_symlink())

    @unittest.skipIf(os.geteuid() == 0, "requires a non-root user")
    def test_prune_reports_unreadable_record_and_continues(self):
        for name in ["aaa", "bbb"]:
            self.start(name)
            self.cli("stop", name)
        meta = self.directory("aaa") / "meta.json"
        meta.chmod(0)
        try:
            result = self.cli("prune", expect=1)
            self.assertEqual(result.stdout, b"Removed bbb\nRemoved 1 task(s).\n")
            self.assertIn(b"bgx: aaa:", result.stderr)
            self.assertIn(b"bgx: some tasks could not be pruned", result.stderr)
            self.assertTrue(self.directory("aaa").exists())
            self.assertFalse(self.directory("bbb").exists())
        finally:
            meta.chmod(0o600)

    def test_prune_fails_safely_when_names_cannot_be_enumerated(self):
        self.start("web")
        self.cli("stop", "web")
        (self.state / "instances" / "bad~name").mkdir()
        before = (self.directory() / "meta.json").read_bytes()
        result = self.cli("prune", expect=1)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"name must use ASCII", result.stderr)
        self.assertEqual((self.directory() / "meta.json").read_bytes(), before)

    def test_prune_uses_configured_state_root(self):
        env = self.env.copy()
        env.pop("BGX_STATE_HOME")
        env["XDG_STATE_HOME"] = str(self.base / "xdg")
        self.cli("start", "quick", "--", "/bin/echo", "ok", env=env)
        self.cli("stop", "quick", env=env)
        result = self.cli("prune", env=env)
        self.assertEqual(result.stdout, b"Removed quick\nRemoved 1 task(s).\n")
        self.assertEqual(result.stderr, b"")
        self.assertFalse((self.base / "xdg" / "bgx" / "instances" / "quick").exists())
        self.assertFalse(self.state.exists())

    @unittest.skipUnless(os.path.exists("/dev/full"), "requires /dev/full")
    def test_prune_output_errors_do_not_claim_success(self):
        self.start()
        self.cli("stop", "web")
        with open("/dev/full", "wb") as sink:
            result = subprocess.run([str(BINARY), "prune"], stdout=sink, stderr=subprocess.PIPE,
                                    cwd=self.cwd, env=self.env, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"write output", result.stderr)
        self.assertNotIn(b"AddressSanitizer", result.stderr)
        self.assertNotIn(b"runtime error:", result.stderr)

    @unittest.skipUnless(os.environ.get("BGX_FAULT_BIN"), "requires fault-test binary")
    def test_prune_waits_for_name_lock_and_rechecks_before_removing(self):
        import fcntl
        import shutil
        self.make_log(b"exited\n")
        notice = self.base / "prune-notice"
        env = dict(self.env, BGX_TEST_POINT="prune_candidate", BGX_TEST_NOTIFY=str(notice))
        lock = (self.state / "locks" / "web.lock").open("rb")
        fcntl.flock(lock, fcntl.LOCK_EX)
        child = subprocess.Popen([os.environ["BGX_FAULT_BIN"], "prune"], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.followers.append(child)
        try:
            self.wait_test_point(child, notice)
            # The record disappears while prune is stopped before it locks the name.
            shutil.rmtree(self.directory())
            os.kill(child.pid, signal.SIGCONT)
            self.wait_for_name_lock(child, lock)
            self.assertIsNone(child.poll())
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        out, err = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, (out, err))
        self.assertEqual(out, b"Removed 0 task(s).\n")
        self.assertEqual(err, b"")
        self.assertFalse(self.directory().exists())
        self.assertTrue((self.state / "locks" / "web.lock").exists())

    @unittest.skipUnless(os.environ.get("BGX_FAULT_BIN"), "requires fault-test binary")
    def test_prune_skips_a_name_reused_as_running_before_locking(self):
        self.start()
        self.cli("stop", "web")
        notice = self.base / "prune-notice"
        env = dict(self.env, BGX_TEST_POINT="prune_candidate", BGX_TEST_NOTIFY=str(notice))
        child = subprocess.Popen([os.environ["BGX_FAULT_BIN"], "prune"], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.followers.append(child)
        try:
            self.wait_test_point(child, notice)
            # Reuse the name while prune waits between assessment and locking.
            self.cli("restart", "web")
            reused = self.track()
            before = (self.directory() / "meta.json").read_bytes()
            os.kill(child.pid, signal.SIGCONT)
            out, err = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, (out, err))
            self.assertEqual(out, b"Removed 0 task(s).\n")
            self.assertEqual(err, b"")
            self.assertEqual((self.directory() / "meta.json").read_bytes(), before)
            self.assertEqual(self.record()["process_identity"], reused["process_identity"])
            os.kill(reused["process_identity"]["pid"], 0)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
        self.cli("stop", "web")
        self.cli("rm", "web")

    def test_invalid_metadata_is_never_reset(self):
        original = self.start()
        path = self.directory() / "meta.json"
        base = json.dumps(original)
        variants = [
            base + "{}",
            base.replace('"schema_version": 2', '"schema_version": 1'),
            base.replace('"schema_version": 2', '"schema_version": 2, "schema_version": 2'),
            base.replace('"name": "web"', '"name": "other"'),
            base.replace('"name": "web"', '"name": "web\\u0000suffix"'),
            base[:-1] + ', "surprise": true}',
        ]
        for text in variants:
            with self.subTest(text=text):
                path.write_text(text)
                self.cli("stop", "web", expect=1)
                self.cli("start", "web", "--", "/bin/echo", "new", expect=1)
                self.cli("restart", "web", expect=1)
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
        for file in [path, self.log_path(), self.state / "locks" / "web.lock"]:
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)

    def test_symlinks_are_not_followed(self):
        self.make_log()
        outside = self.base / "evidence"
        outside.write_text("keep")
        meta = self.directory() / "meta.json"
        meta.unlink()
        meta.symlink_to(outside)
        self.cli("replace", "web", "--", "/bin/echo", "new", expect=1)
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

    def test_start_rejects_existing_names_including_exited(self):
        self.start()
        for stage in ["running", "exited"]:
            with self.subTest(stage=stage):
                if stage == "exited":
                    self.cli("stop", "web")
                meta = (self.directory() / "meta.json").read_bytes()
                log = self.log_path().read_bytes()
                result = self.cli("start", "web", "--", "/bin/echo", "other", expect=1)
                self.assertEqual(result.stdout, b"")
                self.assertIn(b"already exists", result.stderr)
                self.assertEqual(meta, (self.directory() / "meta.json").read_bytes())
                self.assertEqual(log, self.log_path().read_bytes())
                self.assertFalse((self.directory() / "attempt.json").exists())
        self.cli("rm", "web")

    def test_restart_and_replace_require_existing_names(self):
        for args in [("restart", "missing"), ("replace", "missing", "--", "/bin/true")]:
            with self.subTest(args=args):
                result = self.cli(*args, expect=1)
                self.assertEqual(result.stdout, b"")
                self.assertIn(b"instance not found: missing", result.stderr)
                self.assertFalse(self.directory("missing").exists())

    def test_restart_uses_recorded_definition_and_current_environment(self):
        code = (
            "import json,os,sys,time;"
            "print(json.dumps([sys.argv[1:],os.getcwd(),os.getenv('BGX_TEST_VALUE')]),flush=True);"
            "time.sleep(120)"
        )
        self.cli("start", "web", "--", sys.executable, "-c", code, "fixed arg")
        first = self.track()
        self.wait_log(b"fixed arg")
        self.cli("stop", "web")
        env = dict(self.env, BGX_TEST_VALUE="second value")
        result = self.cli("restart", "web", cwd=self.base, env=env)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")
        second = self.track()
        output = self.wait_log(b"second value")
        argv, cwd, value = json.loads(output.splitlines()[0])
        self.assertEqual(argv, ["fixed arg"])
        self.assertEqual(Path(cwd).resolve(), self.cwd.resolve())
        self.assertEqual(value, "second value")
        self.assertEqual(second["name"], "web")
        self.assertEqual(second["argv"], first["argv"])
        self.assertEqual(second["cwd"], first["cwd"])
        self.assertNotEqual(second["process_identity"]["pid"], first["process_identity"]["pid"])
        self.assertNotEqual(second["log"], first["log"])
        self.assertFalse(second["exited"])
        self.assertEqual(sorted(p.name for p in self.directory().iterdir()),
                         sorted(["meta.json", second["log"]]))
        self.cli("stop", "web")
        self.cli("rm", "web")

    def test_restart_stops_running_execution_before_relaunching(self):
        self.start()
        previous = self.track()["process_identity"]["pid"]
        result = self.cli("restart", "web")
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")
        current = self.track()
        self.assertNotEqual(current["process_identity"]["pid"], previous)
        self.wait_log(b"ready")
        with self.assertRaises(ProcessLookupError):
            os.kill(previous, 0)
        self.cli("stop", "web")
        self.cli("rm", "web")

    def test_replace_adopts_definition_only_on_success(self):
        self.cli("start", "web", "--", "/bin/echo", "old")
        self.track()
        self.wait_exited()
        confirmed = self.record()
        missing = str(self.base / "missing")
        result = self.cli("replace", "web", "--", missing, expect=1)
        self.assertIn(b"missing", result.stderr)
        self.assertEqual(self.record()["argv"], confirmed["argv"])
        self.assertEqual(self.record()["cwd"], confirmed["cwd"])
        self.assertEqual(self.record()["log"], confirmed["log"])
        failed = self.attempt()
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["argv"], [missing])
        self.assertIn("No such file", failed["error"])
        # An acquired candidate identity is retained as historical metadata.
        self.assertIn("process_identity", failed)
        self.assertGreater(failed["process_identity"]["pid"], 0)
        # The naturally exited confirmed execution is durably marked ended
        # before the candidate may fail, with its identity preserved.
        self.assertTrue(self.record()["exited"])
        self.assertEqual(self.record()["process_identity"], confirmed["process_identity"])
        self.assertEqual(sorted(p.name for p in self.directory().iterdir()),
                         sorted(["meta.json", "attempt.json", confirmed["log"]]))
        # A failed replacement never becomes the definition used by restart.
        self.cli("restart", "web")
        self.track()
        self.wait_exited()
        self.assertEqual(self.cli("log", "web").stdout, b"old\n")
        self.assertEqual(self.record()["argv"], confirmed["argv"])
        # A successful replacement adopts the supplied command and directory.
        result = self.cli("replace", "web", "--", "/bin/echo", "new", cwd=self.base)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")
        adopted = self.track()
        self.wait_log(b"new")
        self.assertEqual(adopted["argv"], ["/bin/echo", "new"])
        self.assertEqual(Path(adopted["cwd"]).resolve(), self.base.resolve())
        self.assertFalse(adopted["exited"])
        self.assertEqual(sorted(p.name for p in self.directory().iterdir()),
                         sorted(["meta.json", adopted["log"]]))
        self.cli("rm", "web")

    def test_retry_after_failed_replace_supersedes_the_attempt(self):
        self.cli("start", "web", "--", "/bin/echo", "one")
        self.track()
        self.wait_exited()
        self.cli("replace", "web", "--", str(self.base / "missing"), expect=1)
        failed = self.attempt()
        # A syntax failure before a new attempt begins keeps the diagnostics.
        self.cli("replace", "web", "--", expect=2)
        self.assertEqual(self.attempt(), failed)
        self.cli("replace", "web", "--", "/bin/echo", "two")
        self.track()
        self.wait_log(b"two")
        self.assertFalse((self.directory() / "attempt.json").exists())
        self.assertEqual(self.record()["argv"], ["/bin/echo", "two"])
        self.assertEqual(sorted(p.name for p in self.directory().iterdir()),
                         sorted(["meta.json", self.record()["log"]]))
        self.cli("rm", "web")

    def test_preflight_failure_after_failed_replace_keeps_attempt(self):
        directory = self.base / "recorded"
        directory.mkdir()
        self.cli("start", "web", "--", "/bin/echo", "old", cwd=directory)
        self.track()
        self.wait_exited()
        self.cli("replace", "web", "--", str(self.base / "missing"), expect=1)
        failed = self.attempt()
        self.assertEqual(failed["state"], "failed")
        # A preflight failure before a new attempt begins keeps those diagnostics.
        directory.rmdir()
        result = self.cli("restart", "web", expect=1)
        self.assertIn(b"recorded directory", result.stderr)
        self.assertEqual(self.attempt(), failed)
        self.assertEqual(self.record()["argv"], ["/bin/echo", "old"])
        self.cli("rm", "web", "--force")

    def test_restart_checks_recorded_directory_before_stopping(self):
        directory = self.base / "recorded"
        directory.mkdir()
        self.cli("start", "web", "--", "/bin/sleep", "120", cwd=directory)
        record = self.track()
        before = (self.directory() / "meta.json").read_bytes()
        directory.rmdir()
        result = self.cli("restart", "web", expect=1)
        self.assertIn(b"recorded directory", result.stderr)
        self.assertEqual((self.directory() / "meta.json").read_bytes(), before)
        self.assertFalse((self.directory() / "attempt.json").exists())
        os.kill(record["process_identity"]["pid"], 0)
        self.assertIn(b"running", self.cli("ls").stdout)
        self.cli("stop", "web")
        self.cli("rm", "web")

    def test_pending_attempt_dominates_and_is_never_treated_as_ended(self):
        self.make_log(b"confirmed\n")
        self.write_attempt(self.attempt_value(argv=("/bin/echo", "candidate"), log="log-88"))
        (self.directory() / "log-88").write_bytes(b"candidate output\n")
        self.assertIn(b"unknown", self.cli("list").stdout)
        self.cli("stop", "web", expect=1)
        self.cli("rm", "web", expect=1)
        result = self.cli("prune", "--dry-run", expect=1)
        self.assertEqual(result.stdout, b"Would remove 0 task(s).\n")
        self.assertIn(b"bgx: web: launch outcome has not been confirmed", result.stderr)
        self.cli("prune", "--dry-run", expect=1)
        # The confirmed log stays the normal log; the attempt log is separate.
        self.assertEqual(self.cli("log", "web").stdout, b"confirmed\n")
        fields = {key: value.strip() for key, value in
                  (line.split(":", 1) for line in self.cli("info", "web").stdout.decode().splitlines())}
        self.assertEqual(fields["Log"], str(self.log_path()))
        self.assertEqual(fields["Attempt log"], str(self.directory() / "log-88"))
        self.assertEqual(fields["Pending command"], "/bin/echo candidate")
        self.cli("rm", "web", "--force")
        self.assertFalse(self.directory().exists())

    def test_failed_attempt_diagnostics_are_reported_and_removed_with_task(self):
        self.start()
        self.cli("stop", "web")
        self.write_attempt(self.attempt_value(argv=("/bin/bogus",), state="failed", log="log-77",
                                              error="execute /bin/bogus: No such file or directory"))
        (self.directory() / "log-77").write_bytes(b"partial output\n")
        self.assertIn(b"exited", self.cli("list").stdout)
        fields = {key: value.strip() for key, value in
                  (line.split(":", 1) for line in self.cli("info", "web").stdout.decode().splitlines())}
        self.assertEqual(fields["Failed command"], "/bin/bogus")
        self.assertEqual(fields["Attempt error"], '"execute /bin/bogus: No such file or directory"')
        self.assertEqual(fields["Attempt log"], str(self.directory() / "log-77"))
        self.cli("stop", "web")
        self.cli("rm", "web")
        self.assertFalse(self.directory().exists())

    def test_log_reads_attempt_log_without_confirmed_execution(self):
        directory = self.directory()
        directory.mkdir(parents=True)
        self.write_attempt(self.attempt_value(argv=("/bin/echo", "only"), log="log-1"))
        (directory / "log-1").write_bytes(b"attempt only\n")
        self.assertEqual(self.cli("log", "web").stdout, b"attempt only\n")
        fields = {key: value.strip() for key, value in
                  (line.split(":", 1) for line in self.cli("info", "web").stdout.decode().splitlines())}
        self.assertEqual(fields["Status"], "unknown")
        self.assertEqual(fields["Log"], str(directory / "log-1"))
        self.assertNotIn("Attempt log", fields)
        self.cli("start", "web", "--", "/bin/true", expect=1)
        self.cli("rm", "web", "--force")

    @unittest.skipUnless(os.environ.get("BGX_FAULT_BIN"), "requires fault-test binary")
    def test_interrupted_preparation_is_recovered_by_mutation(self):
        self.interrupted_first_launch()
        # Reads never recover; the interrupted name stays visibly unknown.
        self.assertIn(b"unknown", self.cli("ls").stdout)
        self.cli("stop", "web", expect=1)
        # A mutating launch recovers the provably pre-exec attempt first.
        self.cli("start", "web", "--", "/bin/echo", "recovered")
        self.track()
        self.wait_exited()
        self.assertEqual(self.cli("log", "web").stdout, b"recovered\n")
        self.cli("rm", "web")

    def test_restart_and_replace_refuse_recovered_first_launch(self):
        # An interrupted initial start has no confirmed definition; restart and
        # replace must refuse by name instead of acting as an implicit start.
        self.interrupted_first_launch()
        result = self.cli("restart", "web", expect=1)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"no confirmed execution", result.stderr)
        self.assertFalse(self.directory().exists())
        self.interrupted_first_launch()
        marker = self.base / "should-not-run"
        result = self.cli("replace", "web", "--", "/usr/bin/touch", str(marker), expect=1)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"no confirmed execution", result.stderr)
        self.assertFalse(marker.exists())
        self.assertFalse(self.directory().exists())

    def test_interrupted_commit_residue_reads_as_committed(self):
        self.start()
        confirmed = self.record()
        # Simulate a crash between publishing meta.json and removing attempt.json.
        residue = self.attempt_value(argv=confirmed["argv"], cwd=confirmed["cwd"], state="committing",
                                     log=confirmed["log"], identity=confirmed["process_identity"])
        self.write_attempt(residue)
        self.assertIn(b"running", self.cli("list").stdout)
        fields = {key: value.strip() for key, value in
                  (line.split(":", 1) for line in self.cli("info", "web").stdout.decode().splitlines())}
        self.assertEqual(fields["Status"], "running")
        self.assertNotIn("Pending command", fields)
        # An uncertain attempt with the same log and identity is a real
        # candidate, not residue, and must stay visible as unknown.
        self.write_attempt(dict(residue, state="uncertain"))
        self.assertIn(b"unknown", self.cli("list").stdout)
        self.cli("stop", "web", expect=1)
        self.write_attempt(residue)
        # A mutation discards the stale attempt record and proceeds normally.
        self.cli("restart", "web")
        self.track()
        self.wait_log(b"ready")
        self.assertFalse((self.directory() / "attempt.json").exists())
        self.assertNotEqual(self.record()["log"], confirmed["log"])
        self.cli("stop", "web")
        self.cli("rm", "web")

    @unittest.skipUnless(os.environ.get("BGX_FAULT_BIN"), "requires fault-test binary")
    def test_interrupted_restart_preparation_preserves_confirmed_task(self):
        self.make_log(b"ready\n")
        confirmed = self.record()
        notice = self.base / "prep-notice"
        env = dict(self.env, BGX_TEST_POINT="attempt_prepared", BGX_TEST_NOTIFY=str(notice))
        child = subprocess.Popen([os.environ["BGX_FAULT_BIN"], "restart", "web"], env=env, cwd=self.cwd,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.followers.append(child)
        self.wait_test_point(child, notice)
        child.kill()
        child.communicate(timeout=5)
        self.assertEqual(self.attempt()["state"], "prepared")
        self.assertEqual(self.record(), confirmed)
        self.cli("restart", "web")
        self.track()
        self.wait_log(b"ready")
        self.assertFalse((self.directory() / "attempt.json").exists())
        self.assertNotEqual(self.record()["log"], confirmed["log"])
        self.cli("stop", "web")
        self.cli("rm", "web")

    @unittest.skipUnless(os.environ.get("BGX_FAULT_BIN"), "requires fault-test binary")
    def test_prune_preserves_a_prepared_first_launch_as_unknown(self):
        notice = self.base / "prep-notice"
        env = dict(self.env, BGX_TEST_POINT="attempt_prepared", BGX_TEST_NOTIFY=str(notice))
        child = subprocess.Popen([os.environ["BGX_FAULT_BIN"], "start", "web", "--", "/bin/sleep", "120"],
                                 env=env, cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.followers.append(child)
        self.wait_test_point(child, notice)
        child.kill()
        child.communicate(timeout=5)
        self.assertEqual(self.attempt()["state"], "prepared")
        before = {path.name: path.read_bytes() for path in self.directory().iterdir()}
        self.assertIn(b"Status:        unknown", self.cli("info", "web").stdout)
        for options, expected in [(["--dry-run"], b"Would remove 0 task(s).\n"),
                                  ([], b"Removed 0 task(s).\n")]:
            result = self.cli("prune", *options, expect=1)
            self.assertEqual(result.stdout, expected)
            self.assertIn(b"bgx: web:", result.stderr)
            self.assertEqual({path.name: path.read_bytes() for path in self.directory().iterdir()}, before)

    def test_prune_removes_exited_task_with_prepared_attempt(self):
        self.make_log(b"confirmed\n")
        self.write_attempt(self.attempt_value(state="prepared"))
        (self.directory() / "log-99").write_bytes(b"")
        self.assertIn(b"Status:        exited", self.cli("info", "web").stdout)
        self.assertEqual(self.cli("prune", "--dry-run").stdout,
                         b"Would remove web\nWould remove 1 task(s).\n")
        self.assertEqual(self.cli("prune").stdout, b"Removed web\nRemoved 1 task(s).\n")
        self.assertFalse(self.directory().exists())

    @unittest.skipUnless(os.environ.get("BGX_FAULT_BIN"), "requires fault-test binary")
    def test_prune_rechecks_unknown_without_recovering_it(self):
        self.make_log(b"confirmed\n")
        notice = self.base / "prune-notice"
        env = dict(self.env, BGX_TEST_POINT="prune_candidate", BGX_TEST_NOTIFY=str(notice))
        child = subprocess.Popen([os.environ["BGX_FAULT_BIN"], "prune"], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.followers.append(child)
        try:
            self.wait_test_point(child, notice)
            # The candidate becomes an unconfirmed first launch before locking.
            (self.directory() / "meta.json").unlink()
            self.write_attempt(self.attempt_value(state="prepared"))
            (self.directory() / "log-99").write_bytes(b"")
            before = {path.name: path.read_bytes() for path in self.directory().iterdir()}
            os.kill(child.pid, signal.SIGCONT)
            out, err = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 1, (out, err))
            self.assertEqual(out, b"Removed 0 task(s).\n")
            self.assertIn(b"bgx: web:", err)
            self.assertNotIn(b"AddressSanitizer", err)
            self.assertNotIn(b"runtime error:", err)
            self.assertEqual({path.name: path.read_bytes() for path in self.directory().iterdir()}, before)
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=3)

    @unittest.skipUnless(os.environ.get("BGX_FAULT_BIN"), "requires fault-test binary")
    def test_mutation_waits_for_name_lock_and_releases_it(self):
        import fcntl
        self.make_log(b"exited\n")
        notice = self.base / "lock-notice"
        env = dict(self.env, BGX_TEST_POINT="before_name_lock", BGX_TEST_NOTIFY=str(notice))
        lock = (self.state / "locks" / "web.lock").open("rb")
        fcntl.flock(lock, fcntl.LOCK_EX)
        child = subprocess.Popen([os.environ["BGX_FAULT_BIN"], "restart", "web"], env=env, cwd=self.cwd,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.followers.append(child)
        try:
            self.wait_test_point(child, notice)
            # The child is stopped before locking, then must block on the held lock.
            os.kill(child.pid, signal.SIGCONT)
            self.wait_for_name_lock(child, lock)
            self.assertIsNone(child.poll())
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        out, err = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, (out, err))
        self.assertEqual(out, b"")
        self.assertEqual(err, b"")
        self.track()
        self.wait_log(b"ready")
        # The name lock is released for later mutations.
        self.cli("stop", "web")
        self.cli("rm", "web")

    @unittest.skipUnless(os.environ.get("BGX_FAULT_BIN"), "requires fault-test binary")
    def test_stop_failure_prevents_relaunch(self):
        self.start()
        record = self.track()
        before = (self.directory() / "meta.json").read_bytes()
        env = dict(self.env, BGX_TEST_FAULT="stop_failure")
        result = subprocess.run([os.environ["BGX_FAULT_BIN"], "restart", "web"], env=env, cwd=self.cwd,
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 1, (result.stdout, result.stderr))
        self.assertIn(b"injected stop failure", result.stderr)
        self.assertEqual((self.directory() / "meta.json").read_bytes(), before)
        self.assertFalse((self.directory() / "attempt.json").exists())
        os.kill(record["process_identity"]["pid"], 0)
        self.assertIn(b"running", self.cli("ls").stdout)
        self.cli("stop", "web")

    def test_replace_stops_running_execution_before_adopting(self):
        self.start()
        previous = self.track()["process_identity"]["pid"]
        self.cli("replace", "web", "--", "/bin/echo", "replaced")
        current = self.track()
        self.assertNotEqual(current["process_identity"]["pid"], previous)
        self.wait_log(b"replaced")
        with self.assertRaises(ProcessLookupError):
            os.kill(previous, 0)
        self.cli("rm", "web")

    def test_restart_uses_current_path(self):
        first = self.base / "path-one"
        second = self.base / "path-two"
        first.mkdir()
        second.mkdir()
        (first / "chosen-command").write_text("#!/bin/sh\nprintf 'first\\n'\n")
        (first / "chosen-command").chmod(0o700)
        (second / "chosen-command").write_text("#!/bin/sh\nprintf 'second\\n'\n")
        (second / "chosen-command").chmod(0o700)
        self.cli("start", "web", "--", "chosen-command", env=dict(self.env, PATH=str(first)))
        self.track()
        self.wait_exited()
        self.assertEqual(self.cli("log", "web").stdout, b"first\n")
        self.cli("restart", "web", env=dict(self.env, PATH=str(second)))
        self.track()
        self.wait_exited()
        self.assertEqual(self.cli("log", "web").stdout, b"second\n")
        self.cli("rm", "web")

    def test_recorded_env_assignment_survives_restart(self):
        code = "import os,time; print(os.environ.get('BGX_TEST_VALUE'), flush=True); time.sleep(0.05)"
        self.cli("start", "web", "--", "env", "BGX_TEST_VALUE=fixed", sys.executable, "-c", code)
        self.track()
        self.wait_exited()
        self.assertEqual(self.cli("log", "web").stdout, b"fixed\n")
        self.cli("restart", "web", env=dict(self.env, BGX_TEST_VALUE="changed"))
        self.track()
        self.wait_exited()
        self.assertEqual(self.cli("log", "web").stdout, b"fixed\n")
        self.cli("rm", "web")

    @unittest.skipUnless(os.environ.get("BGX_FAULT_BIN"), "requires fault-test binary")
    def test_read_snapshot_is_stable_across_commit(self):
        # A failed attempt B exists; commit replaces meta A with C and removes
        # attempt.json. A reader paused between its record samples must never
        # combine ended A with the missing attempt.
        self.cli("start", "web", "--", "/bin/echo", "old")
        self.track()
        self.wait_exited()
        self.cli("stop", "web")
        confirmed = self.record()
        self.assertTrue(confirmed["exited"])
        self.write_attempt(self.attempt_value(argv=("/bin/echo", "missing"), state="failed", log="log-98",
                                              error="execute /bin/echo missing: No such file or directory"))
        (self.directory() / "log-98").write_bytes(b"stale\n")
        notice = self.base / "snapshot-notice"
        notice.unlink(missing_ok=True)
        reader_env = dict(self.env, BGX_TEST_POINT="task_snapshot", BGX_TEST_NOTIFY=str(notice))
        reader = subprocess.Popen([os.environ["BGX_FAULT_BIN"], "info", "web"], env=reader_env, cwd=self.cwd,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.followers.append(reader)
        try:
            self.wait_test_point(reader, notice)
            self.assertEqual(self.attempt()["state"], "failed")
            self.cli("replace", "web", "--", "/bin/sleep", "120")
            self.track()
            os.kill(reader.pid, signal.SIGCONT)
            out, err = reader.communicate(timeout=5)
            self.assertEqual(reader.returncode, 0, (out, err))
            self.assertEqual(err, b"")
            self.assertNotIn(b"exited", out)
            fields = {key: value.strip() for key, value in
                      (line.split(":", 1) for line in out.decode().splitlines())}
            self.assertEqual(fields["Status"], "running")
            self.assertEqual(fields["Command"], "/bin/sleep 120")
        finally:
            if reader.poll() is None:
                reader.kill()
                reader.communicate(timeout=5)
            self.followers.remove(reader)
        self.cli("stop", "web")
        self.cli("rm", "web")

    @unittest.skipUnless(os.environ.get("BGX_FAULT_BIN"), "requires fault-test binary")
    def test_read_snapshot_pinned_directory_removed_reports_missing(self):
        # The reader is pinned to the prepared instance directory. Recovery for
        # a later start unlinks that directory's contents and recreates it, so
        # the reader must report the existing absent-task result for its
        # coherent (emptied) snapshot, never a synthetic unnamed row.
        self.interrupted_first_launch()
        notice = self.base / "snapshot-notice"
        notice.unlink(missing_ok=True)
        reader_env = dict(self.env, BGX_TEST_POINT="task_snapshot", BGX_TEST_NOTIFY=str(notice))
        reader = subprocess.Popen([os.environ["BGX_FAULT_BIN"], "info", "web"], env=reader_env, cwd=self.cwd,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.followers.append(reader)
        try:
            self.wait_test_point(reader, notice)
            self.cli("start", "web", "--", "/bin/sleep", "120")
            self.track()
            os.kill(reader.pid, signal.SIGCONT)
            out, err = reader.communicate(timeout=5)
            self.assertEqual(reader.returncode, 1, (out, err))
            self.assertEqual(out, b"")
            self.assertIn(b"instance not found: web", err)
            self.assertNotIn(b"Name:", out)
        finally:
            if reader.poll() is None:
                reader.kill()
                reader.communicate(timeout=5)
            self.followers.remove(reader)
        self.cli("stop", "web")
        self.cli("rm", "web")

    @unittest.skipUnless(os.environ.get("BGX_FAULT_BIN"), "requires fault-test binary")
    def test_read_snapshot_first_commit_in_same_directory(self):
        # The first commit publishes meta.json inside the pinned directory and
        # removes the attempt record; the reader must resample and see the new
        # confirmed record rather than not-found or a mixed snapshot.
        self.interrupted_first_launch()
        prepared = self.attempt()
        notice = self.base / "snapshot-notice"
        notice.unlink(missing_ok=True)
        reader_env = dict(self.env, BGX_TEST_POINT="task_snapshot", BGX_TEST_NOTIFY=str(notice))
        reader = subprocess.Popen([os.environ["BGX_FAULT_BIN"], "info", "web"], env=reader_env, cwd=self.cwd,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.followers.append(reader)
        try:
            self.wait_test_point(reader, notice)
            record = {
                "schema_version": 2, "name": "web", "argv": ["/bin/echo", "published"],
                "cwd": str(self.cwd), "started_at": "2026-01-02T03:04:05Z", "exited": True,
                "log": prepared["log"],
            }
            (self.directory() / "meta.json").write_text(json.dumps(record))
            (self.directory() / "attempt.json").unlink()
            os.kill(reader.pid, signal.SIGCONT)
            out, err = reader.communicate(timeout=5)
            self.assertEqual(reader.returncode, 0, (out, err))
            self.assertEqual(err, b"")
            fields = {key: value.strip() for key, value in
                      (line.split(":", 1) for line in out.decode().splitlines())}
            self.assertEqual(fields["Name"], "web")
            self.assertEqual(fields["Status"], "exited")
            self.assertEqual(fields["Command"], "/bin/echo published")
        finally:
            if reader.poll() is None:
                reader.kill()
                reader.communicate(timeout=5)
            self.followers.remove(reader)
        self.cli("rm", "web")

    def test_info_missing_for_empty_instance_directory(self):
        self.directory().mkdir(parents=True)
        result = self.cli("info", "web", expect=1)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"instance not found: web", result.stderr)
        self.cli("log", "web", expect=1)
        listed = self.cli("list").stdout.decode().splitlines()
        self.assertEqual(len(listed), 1)
        result = self.cli("prune")
        self.assertEqual(result.stdout, b"Removed 0 task(s).\n")
        self.assertEqual(result.stderr, b"")
        self.assertTrue(self.directory().exists())

    def test_unresolved_adoption_reads_use_recovery_previous(self):
        # Hand-built unresolved adoption: meta.json names B, attempt B is
        # committing, recovery.json holds A. Reads must use A as the confirmed
        # execution and never treat the matching meta+attempt as adopted.
        self.make_log(b"confirmed\n")
        previous = self.record()
        previous["exited"] = True
        self.save(previous)
        candidate = self.attempt_value(argv=("/bin/echo", "candidate"), state="committing",
                                       log="log-88", identity={
                                           "kind": "unix", "pid": 4242, "process_group": 4242,
                                           "start_marker": "1", "boot_id": "boot"})
        (self.directory() / "log-88").write_bytes(b"candidate\n")
        self.write_attempt(candidate)
        (self.directory() / "recovery.json").write_text(json.dumps({
            "schema_version": 2, "name": "web", "previous": previous,
        }))
        fields = {key: value.strip() for key, value in
                  (line.split(":", 1) for line in self.cli("info", "web").stdout.decode().splitlines())}
        self.assertEqual(fields["Status"], "unknown")
        self.assertEqual(fields["Reason"], "commit outcome has not been confirmed")
        self.assertNotIn("candidate", fields["Command"])
        self.assertEqual(fields["Recorded PID"], str(previous["process_identity"]["pid"]))
        self.assertEqual(fields["Log"], str(self.directory() / previous["log"]))
        self.assertEqual(fields["Pending command"], "/bin/echo candidate")
        self.assertEqual(self.cli("log", "web").stdout, b"confirmed\n")
        self.assertIn(b"unknown", self.cli("list").stdout)
        for args in [("stop", "web"), ("rm", "web"), ("restart", "web"),
                     ("replace", "web", "--", "/bin/true")]:
            with self.subTest(args=args):
                self.cli(*args, expect=1)
        result = self.cli("prune", "--dry-run", expect=1)
        self.assertIn(b"commit outcome has not been confirmed", result.stderr)
        self.cli("rm", "web", "--force")
        self.assertFalse(self.directory().exists())

    @unittest.skipUnless(os.environ.get("BGX_FAULT_BIN"), "requires fault-test binary")
    def test_failed_b_then_failed_c_retains_a_and_c_only(self):
        self.cli("start", "web", "--", "/bin/echo", "one")
        self.track()
        self.wait_exited()
        confirmed = self.record()
        fault_bin = os.environ["BGX_FAULT_BIN"]

        def failed_replace(program):
            # Wait for real candidate output at the pre-publication checkpoint,
            # then resume into the injected commit failure; the launcher is
            # stopped while the output is written, so nothing races the abort.
            notice = self.base / ("fail-" + program[-1])
            notice.unlink(missing_ok=True)
            env = dict(self.env, BGX_TEST_FAULT="save_identity", BGX_TEST_POINT="before_publication",
                       BGX_TEST_NOTIFY=str(notice))
            child = subprocess.Popen([fault_bin, "replace", "web", "--", *program], env=env, cwd=self.cwd,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.followers.append(child)
            try:
                self.wait_test_point(child, notice)
                attempted = self.attempt()
                self.assertEqual(attempted["state"], "committing")
                log = self.directory() / attempted["log"]
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and program[-1].encode() not in log.read_bytes():
                    time.sleep(0.005)
                self.assertIn(program[-1].encode(), log.read_bytes())
                os.kill(child.pid, signal.SIGCONT)
                out, err = child.communicate(timeout=5)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=5)
                self.followers.remove(child)
            self.assertNotIn(b"AddressSanitizer", err)
            self.assertEqual(child.returncode, 1, (out, err))
            self.assertIn(b"metadata write failure", err)
            self.assertIn(b"attempt log:", err)

        failed_replace(["/bin/echo", "two"])
        second = self.attempt()
        self.assertEqual(second["argv"], ["/bin/echo", "two"])
        second_log = self.directory() / second["log"]
        self.assertIn(b"two", second_log.read_bytes())
        failed_replace(["/bin/echo", "three"])
        third = self.attempt()
        self.assertEqual(third["argv"], ["/bin/echo", "three"])
        third_log = self.directory() / third["log"]
        self.assertIn(b"three", third_log.read_bytes())
        self.assertNotEqual(second["log"], third["log"])
        self.assertFalse(second_log.exists())
        current = self.record()
        self.assertEqual(current["argv"], confirmed["argv"])
        self.assertEqual(current["cwd"], confirmed["cwd"])
        self.assertEqual(current["log"], confirmed["log"])
        self.assertEqual(current["process_identity"], confirmed["process_identity"])
        self.assertTrue(current["exited"])
        self.assertEqual(sorted(p.name for p in self.directory().iterdir()),
                         sorted(["meta.json", "attempt.json", confirmed["log"], third["log"]]))
        fields = {key: value.strip() for key, value in
                  (line.split(":", 1) for line in self.cli("info", "web").stdout.decode().splitlines())}
        self.assertEqual(fields["Command"], "/bin/echo one")
        self.assertEqual(fields["Failed command"], "/bin/echo three")
        self.assertEqual(fields["Attempt log"], str(third_log))
        self.cli("rm", "web")


if __name__ == "__main__":
    unittest.main()
