import json
from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from process_cleanup import kill_if_present

BINARY = Path(os.environ.get("BGX_BIN", "build/bgx")).resolve()
TRUE = shutil.which("true")

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
            kill_if_present(pgid, group=True)
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
        bitmap = [0] * ((len(name) + 3) // 4)
        for i, char in enumerate(name):
            if "A" <= char <= "Z":
                bitmap[i // 4] |= 1 << (i % 4)
        key = name.lower()
        if any(bitmap):
            key += "." + "".join(format(value, "x") for value in bitmap)
        return self.state / "instances" / key

    def record(self, name="web"):
        return json.loads((self.directory(name) / "meta.json").read_text())

    def log_path(self, name="web"):
        return self.directory(name) / "output.log"

    def stored_record(self, name):
        record = self.track(name)
        return self.directory(name), record

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
        self.assertEqual(result.stdout, (name + "\n").encode())
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

    def test_automatic_names_are_independent_and_work_across_terminals(self):
        children = [self.spawn(["start", "--", "/bin/sleep", "120"]) for _ in range(6)]
        names = []
        for child in children:
            out, err = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, err)
            self.assertEqual(err, b"")
            name = out.decode().strip()
            self.assertRegex(name, r"^sleep-[a-z0-9]{4}$")
            self.track(name)
            names.append(name)
        self.assertEqual(len(set(names)), len(names))
        for name in names:
            self.cli("stop", name, cwd=self.base)
            self.log_path(name).write_text(name)
        extra = self.cli("s", "--", "/bin/sleep", "120").stdout.decode().strip()
        self.track(extra)
        self.assertNotIn(extra, names)
        for name in names:
            self.assertEqual(self.cli("log", name).stdout.decode(), name)
        self.cli("stop", extra)
        self.cli("prune")
        self.assertEqual(list((self.state / "instances").iterdir()), [])

    def test_automatic_name_normalization(self):
        examples = [("My.Worker", "my-worker"), ("__Some_CMD", "some_cmd"),
                    ("!中文!", "task"), ("A" * 30, "a" * 24)]
        for filename, prefix in examples:
            program = self.cwd / filename
            program.write_text("#!/bin/sh\nprintf 'ok\\n'\n")
            program.chmod(0o700)
            result = self.cli("start", "--", str(program))
            name = result.stdout.decode().strip()
            self.assertRegex(name, "^" + prefix + r"-[a-z0-9]{4}$")
            self.track(name)
            self.wait_exited(name)
            self.assertEqual(self.cli("log", name).stdout, b"ok\n")

    def test_automatic_launch_failure_reports_its_name(self):
        result = self.cli("start", "--", str(self.base / "missing"), expect=1)
        self.assertEqual(result.stdout, b"")
        name = result.stderr.decode().split(":", 2)[1].strip()
        self.assertRegex(name, r"^missing-[a-z0-9]{4}$")
        self.assertEqual(self.record(name)["launch"], "failed")
        self.assertIn(b"Error:", self.cli("info", name).stdout)
        self.cli("remove", name)

    @unittest.skipUnless(os.environ.get("BGX_FAULT_BIN"), "requires fault-test binary")
    def test_automatic_name_collision_never_overwrites_existing_data(self):
        name = "true-0000"
        self.cli("start", name, "--", "/bin/echo", "keep")
        self.track(name)
        self.wait_exited(name)
        env = dict(self.env, BGX_TEST_FAULT="name_collision")
        for damaged in [False, True]:
            if damaged:
                (self.directory(name) / "meta.json").write_text("{broken")
            before = {p.name: p.read_bytes() for p in self.directory(name).iterdir()}
            result = subprocess.run([os.environ["BGX_FAULT_BIN"], "start", "--", TRUE],
                                    env=env, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            generated = result.stdout.decode().strip()
            self.assertNotEqual(generated, name)
            self.track(generated)
            self.assertEqual({p.name: p.read_bytes() for p in self.directory(name).iterdir()}, before)

    def test_name_reuse_discards_old_definition_and_log_even_on_failure(self):
        self.cli("start", "web", "--", "/bin/echo", "old")
        self.track()
        self.wait_exited()
        with self.log_path().open("rb") as previous:
            self.cli("start", "web", "--", "/bin/echo", "new")
            self.track()
            self.wait_exited()
            self.assertEqual(self.cli("log", "web").stdout, b"new\n")
            self.assertEqual(previous.read(), b"old\n")
            self.assertNotEqual(os.fstat(previous.fileno()).st_ino, self.log_path().stat().st_ino)
        self.cli("start", "web", "--", str(self.base / "missing"), expect=1)
        self.assertEqual(self.record()["launch"], "failed")
        self.assertEqual(self.cli("log", "web").stdout, b"")
        self.assertEqual(sorted(p.name for p in self.directory().iterdir()), ["meta.json", "output.log"])
        self.cli("start", "web", "--", "/bin/echo", "retry")
        self.track()
        self.wait_exited()
        self.assertEqual(self.cli("log", "web").stdout, b"retry\n")

    def test_reuse_uses_current_directory_and_environment(self):
        code = "import os; print(os.getcwd(), os.environ['BGX_TEST_VALUE'], flush=True)"
        self.cli("start", "web", "--", TRUE)
        self.track()
        self.wait_exited()
        self.cli("start", "web", "--", sys.executable, "-c", code,
                 cwd=self.base, env=dict(self.env, BGX_TEST_VALUE="changed"))
        self.track()
        self.wait_exited()
        self.assertEqual(self.record()["cwd"], str(self.base))
        self.assertEqual(self.cli("log", "web").stdout.decode(), str(self.base) + " changed\n")

    def test_old_and_incomplete_records_are_never_reinterpreted(self):
        self.make_log(b"keep\n")
        original = self.record()
        for changes in [{"schema_version": 3}, {"name": "web"}, {"launch": "prepared"},
                        {"launch": "pending", "exited": True},
                        {"launch": "failed", "exited": False}, {"process_identity": None}]:
            record = dict(original, **changes)
            self.save(record)
            before = (self.directory() / "meta.json").read_bytes()
            self.cli("start", "web", "--", "/bin/true", expect=1)
            self.cli("stop", "web", expect=1)
            self.assertEqual((self.directory() / "meta.json").read_bytes(), before)
            self.assertEqual(self.log_path().read_bytes(), b"keep\n")
        (self.directory() / "meta.json").unlink()
        self.cli("start", "web", "--", "/bin/true", expect=1)
        self.assertIn(b"unknown", self.cli("list", expect=1).stdout)
        self.cli("rm", "web", "--force")

    def test_rename_keeps_running_process_metadata_and_open_log(self):
        original = self.start()
        before = (self.directory() / "meta.json").read_bytes()
        self.assertNotIn("name", original)
        self.assertEqual(original["schema_version"], 4)
        follower = self.follow()
        self.expect_output(follower, b"ready\n")
        with self.log_path().open("ab", buffering=0) as writer:
            inode = os.fstat(writer.fileno()).st_ino
            self.cli("rename", "web", "Web")
            self.assertFalse(self.directory().exists())
            self.assertEqual((self.directory("Web") / "meta.json").read_bytes(), before)
            self.assertEqual(self.log_path("Web").stat().st_ino, inode)
            self.assertIn(b"Name:          Web", self.cli("info", "Web").stdout)
            self.assertIn(b"running", self.cli("list").stdout)
            writer.write(b"after rename\n")
            self.expect_output(follower, b"after rename\n")
        self.cli("stop", "web", expect=1)
        os.kill(original["process_identity"]["pid"], 0)
        self.cli("rename", "Web", "Web")  # An existing, readable same-name move is a no-op.
        self.cli("stop", "Web")
        self.cli("rename", "Web", "finished")
        self.assertTrue(self.record("finished")["exited"])
        self.assertTrue((self.state / "locks/web.lock").exists())
        self.assertTrue((self.state / "locks/web.1.lock").exists())
        self.stop_follow(follower)
        self.cli("rm", "finished")

    def test_rename_automatic_and_failed_runs(self):
        name = self.cli("start", "--", "/bin/sleep", "120").stdout.decode().strip()
        self.track(name)
        self.cli("rename", name, "worker")
        self.cli("stop", "worker")
        self.cli("start", "broken", "--", str(self.base / "missing"), expect=1)
        before = self.record("broken")
        self.cli("rename", "broken", "failure")
        self.assertEqual(self.record("failure"), before)
        self.cli("prune")

    def test_rename_never_overwrites_any_destination(self):
        self.make_log(b"source\n")
        self.start("target")
        for kind in ["running", "exited", "broken", "empty", "symlink"]:
            with self.subTest(kind=kind):
                if kind == "exited":
                    self.cli("stop", "target")
                elif kind == "broken":
                    (self.directory("target") / "meta.json").write_text("{broken")
                elif kind == "empty":
                    self.cli("rm", "target", "--force")
                    self.directory("target").mkdir()
                elif kind == "symlink":
                    self.directory("target").rmdir()
                    self.directory("target").symlink_to(self.cwd)
                self.cli("rename", "web", "target", expect=1)
                self.assertEqual(self.cli("log", "web").stdout, b"source\n")
                self.assertTrue(self.directory("target").exists())

    def test_rename_refuses_missing_invalid_stale_and_pending_sources(self):
        self.cli("rename", "missing", "new", expect=1)
        self.assertFalse(self.state.exists())
        original = self.start()
        for kind in ["stale", "pending", "invalid"]:
            changed = deepcopy(original)
            if kind == "stale":
                changed["process_identity"]["start_marker"] += "-wrong"
            elif kind == "pending":
                changed["launch"] = "pending"
            self.save(changed)
            if kind == "invalid":
                (self.directory() / "meta.json").write_text("{broken")
            before = (self.directory() / "meta.json").read_bytes()
            self.cli("rename", "web", "new", expect=1)
            self.assertEqual((self.directory() / "meta.json").read_bytes(), before)
            self.assertFalse(self.directory("new").exists())
            os.kill(original["process_identity"]["pid"], 0)
        self.save(original)
        self.cli("stop", "web")

    def test_batch_stop_and_remove_continue_after_named_errors(self):
        self.start("one")
        stale = self.start("stale")
        changed = deepcopy(stale)
        changed["process_identity"]["start_marker"] += "-wrong"
        self.save(changed, "stale")
        self.start("two")
        result = self.cli("stop", "one", "missing", "stale", "two", expect=1)
        self.assertIn(b"bgx: missing:", result.stderr)
        self.assertIn(b"bgx: stale:", result.stderr)
        self.assertTrue(self.record("one")["exited"])
        self.assertTrue(self.record("two")["exited"])
        os.kill(stale["process_identity"]["pid"], 0)
        result = self.cli("rm", "one", "missing", "stale", "two", expect=1)
        self.assertIn(b"bgx: missing:", result.stderr)
        self.assertFalse(self.directory("one").exists())
        self.assertFalse(self.directory("two").exists())
        self.assertTrue(self.directory("stale").exists())
        self.save(stale, "stale")
        self.cli("stop", "stale")

    def test_batch_force_applies_to_all_names_without_signals(self):
        for position in range(3):
            one, two = self.start("one"), self.start("two")
            args = ["one", "two"]
            args.insert(position, "--force")
            self.cli("rm", *args)
            for name, record in [("one", one), ("two", two)]:
                self.assertFalse(self.directory(name).exists())
                os.kill(record["process_identity"]["pid"], 0)

    def test_batch_syntax_errors_do_not_partially_mutate(self):
        self.start("one")
        for args in [("stop", "one", "bad.name"), ("stop", "one", "--force"),
                     ("rm", "one", "--force", "bad/name"),
                     ("rm", "--force", "one", "--force")]:
            self.cli(*args, expect=2)
            self.assertIn(b"running", self.cli("info", "one").stdout)
        for args in [("rename",), ("rename", "one"), ("rename", "one", "new", "extra"),
                     ("rename", "one", "bad.name")]:
            self.cli(*args, expect=2)
        self.cli("stop", "one", "one")
        self.cli("rm", "one")

    @unittest.skipUnless(os.environ.get("BGX_FAULT_BIN"), "requires fault-test binary")
    def test_rename_crash_keeps_one_complete_directory(self):
        for point, visible in [("before_rename", "web"), ("after_rename", "new")]:
            self.start()
            before = (self.directory() / "meta.json").read_bytes()
            notice = self.base / point
            env = dict(self.env, BGX_TEST_POINT=point, BGX_TEST_NOTIFY=str(notice))
            child = subprocess.Popen([os.environ["BGX_FAULT_BIN"], "rename", "web", "new"],
                                     env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.followers.append(child)
            self.wait_test_point(child, notice)
            child.kill()
            child.communicate(timeout=5)
            self.assertEqual(sorted(p.name for p in (self.state / "instances").iterdir()), [visible])
            self.assertEqual((self.directory(visible) / "meta.json").read_bytes(), before)
            self.cli("stop", visible)
            self.cli("rm", visible)

    @unittest.skipUnless(os.environ.get("BGX_FAULT_BIN"), "requires fault-test binary")
    def test_rename_sync_error_reports_new_name(self):
        self.start()
        result = subprocess.run([os.environ["BGX_FAULT_BIN"], "rename", "web", "new"],
                                env=dict(self.env, BGX_TEST_FAULT="rename_sync"), capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"use the new name", result.stderr)
        self.assertFalse(self.directory().exists())
        self.cli("stop", "new")

    @unittest.skipUnless(os.environ.get("BGX_FAULT_BIN"), "requires fault-test binary")
    def test_rename_holds_both_names_and_waiting_stop_reloads_old_name(self):
        import fcntl
        self.start("a")
        notice = self.base / "rename-notice"
        env = dict(self.env, BGX_TEST_POINT="before_rename", BGX_TEST_NOTIFY=str(notice))
        move = subprocess.Popen([os.environ["BGX_FAULT_BIN"], "rename", "a", "b"], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.followers.append(move)
        self.wait_test_point(move, notice)
        for name in ["a", "b"]:
            with (self.state / "locks" / (name + ".lock")).open("rb") as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stop = self.spawn(["stop", "a"])
        opposite = self.spawn(["rename", "b", "a"])
        with (self.state / "locks/a.lock").open("rb") as lock:
            self.wait_for_name_lock(stop, lock)
            self.wait_for_name_lock(opposite, lock)
        os.kill(move.pid, signal.SIGCONT)
        out, err = move.communicate(timeout=5)
        self.assertEqual(move.returncode, 0, (out, err))
        # Whichever waiter wins, both finish without deadlock or signalling a
        # different run. The opposite rename returns the original run to a.
        out, err = opposite.communicate(timeout=5)
        self.assertEqual(opposite.returncode, 0, (out, err))
        stop.communicate(timeout=5)
        self.assertIn(stop.returncode, [0, 1])
        self.cli("stop", "a")
        self.assertFalse(self.directory("b").exists())

    @unittest.skipUnless(os.environ.get("BGX_FAULT_BIN"), "requires fault-test binary")
    def test_log_reader_pinned_before_rename_keeps_original_run(self):
        self.make_log(b"original\n")
        notice = self.base / "read-notice"
        reader = subprocess.Popen([os.environ["BGX_FAULT_BIN"], "log", "web"],
                                  env=dict(self.env, BGX_TEST_POINT="log_record_loaded",
                                           BGX_TEST_NOTIFY=str(notice)),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.followers.append(reader)
        self.wait_test_point(reader, notice)
        self.cli("rename", "web", "other")
        self.cli("start", "web", "--", "/bin/echo", "new")
        self.track()
        self.wait_exited()
        os.kill(reader.pid, signal.SIGCONT)
        out, err = reader.communicate(timeout=5)
        self.assertEqual(reader.returncode, 0, err)
        self.assertEqual(out, b"original\n")
        self.assertEqual(self.cli("log", "web").stdout, b"new\n")

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
            ("restart",), ("restart", "web"), ("restart", "web", "extra"), ("restart", "web", "--", "true"),
            ("replace", "web"), ("replace", "web", "--"), ("replace", "web", "--", ""),
            ("replace", "../escape", "--", "true"), ("replace", "web", "true"),
            ("forget", "web"),
            ("prune", "web"), ("prune", "--dry-run", "web"),
            ("prune", "--dry-run", "--dry-run"), ("prune", "--force"),
            ("completion", "bash"), ("logpath", "web"), ("ps",), ("logs", "web"),
            ("-v",), ("up", "web", "--", "true"), ("down", "web"),
        ]:
            with self.subTest(args=args):
                self.cli(*args, expect=2)

    def test_empty_list_does_not_create_state(self):
        self.assertIn(b"NAME", self.cli("ls").stdout)
        self.assertFalse(self.state.exists())

    def test_argument_errors_explain_cause_and_show_local_usage(self):
        cases = [
            (("log", "web", "--tail"), "--tail requires a value", "log"),
            (("log", "web", "--tail", "-1"), "--tail requires a nonnegative integer", "log"),
            (("log", "web", "--foo"), "unknown option: --foo", "log"),
            (("log", "web", "-f", "--follow"), "duplicate option: --follow", "log"),
            (("log", "web", "--tail", "1", "--tail", "2"), "duplicate option: --tail", "log"),
            (("rm", "web", "--force", "--force"), "duplicate option: --force", "remove"),
            (("prune", "--dry-run", "--dry-run"), "duplicate option: --dry-run", "prune"),
            (("stop",), "missing task name", "stop"),
            (("info", "web", "extra"), "unexpected argument: extra", "info"),
            (("ls", "extra"), "unexpected argument: extra", "list"),
            (("version", "extra"), "unexpected argument: extra", "version"),
            (("start", "--"), "missing program after '--'", "start"),
            (("start", "web", "true"), "expected '--' before program", "start"),
            (("start", "web", "--"), "missing program after '--'", "start"),
            (("help", "missing"), "unknown command: missing", "help"),
        ]
        for args, message, command in cases:
            with self.subTest(args=args):
                result = self.cli(*args, expect=2)
                self.assertEqual(result.stdout, b"")
                self.assertIn(f"bgx: {message}\n".encode(), result.stderr)
                usages = [line.strip() for line in result.stderr.splitlines() if line.startswith(b"  bgx ")]
                self.assertEqual(len(usages), 1)
                self.assertEqual(usages[0].split()[1], command.encode())
        self.assertIn(b"unknown command: missing", self.cli("missing", expect=2).stderr)
        self.assertFalse(self.state.exists())

    def test_help_anywhere_before_separator_has_no_task_side_effects(self):
        cases = [("log", ["web", "--tail", "10", "-f"]), ("log", ["web", "--tail"]),
                 ("start", ["web"]), ("s", ["web"]),
                 ("stop", ["web"]), ("info", ["web"]),
                 ("remove", ["web", "--force"]), ("prune", ["--dry-run"]),
                 ("list", []), ("version", [])]
        for command, arguments in cases:
            expected = self.cli("help", command).stdout
            for option in ["-h", "--help"]:
                for position in range(len(arguments) + 1):
                    args = [command, *arguments[:position], option, *arguments[position:]]
                    with self.subTest(args=args):
                        self.assertEqual(self.cli(*args).stdout, expected)
        self.assertFalse(self.state.exists())
        self.start()
        before = (self.directory() / "meta.json").read_bytes()
        for command in ["stop", "remove", "start"]:
            self.cli(command, "web", "--help")
        self.cli("prune", "--help")
        self.assertEqual((self.directory() / "meta.json").read_bytes(), before)
        self.assertIn(b"running", self.cli("list").stdout)

    def test_program_help_flags_after_separator_are_forwarded(self):
        forwarded = ["--help", "-h", "--tail", "--force", "", "--"]
        code = "import json,sys; print(json.dumps(sys.argv[1:]),flush=True)"
        for command in ["start", "s"]:
            self.cli(command, "web", "--", sys.executable, "-c", code, *forwarded)
            self.track()
            self.wait_exited()
            self.assertEqual(json.loads(self.cli("log", "web").stdout), forwarded)
            self.assertEqual(self.record()["argv"][-len(forwarded):], forwarded)

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
                self.assertEqual(Path(fields["Log"]).name, "output.log")
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
        for phase in ["stale", "unknown"]:
            changed = deepcopy(original)
            if phase == "stale":
                changed["process_identity"]["start_marker"] += "-wrong"
            else:
                changed["launch"] = "pending"
                changed["error"] = "launch interrupted"
            self.save(changed)
            before = (self.directory() / "meta.json").read_bytes()
            fields = {key: value.strip() for key, value in
                      (line.split(":", 1) for line in self.cli("info", "web").stdout.decode().splitlines())}
            self.assertEqual(fields["Status"], phase)
            self.assertIn("Reason", fields)
            self.assertEqual(fields["Recorded PID"], str(original["process_identity"]["pid"]))
            self.cli("stop", "web", expect=1)
            self.assertEqual((self.directory() / "meta.json").read_bytes(), before)
            os.kill(original["process_identity"]["pid"], 0)
        self.save(original)
        self.cli("stop", "web")

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
        for operation in ["stop", "rm", "start"]:
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
                if operation == "start":
                    self.start()
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
        self.assertIn(b"bgx start [NAME]", general)
        for args in [(), ("-h",), ("--help",)]:
            self.assertEqual(self.cli(*args).stdout, general)
        commands = {"start": ["s"], "stop": [], "rename": [],
                    "list": ["ls"], "info": [], "remove": ["rm"],
                    "prune": [], "log": [], "help": ["-h", "--help"], "version": ["-V", "--version"]}
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
            self.assertEqual(self.cli(alias, "log").stdout, self.cli("help", "log").stdout)
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
            self.assertEqual(self.cli("ls").stdout, listed)
            self.assertEqual(self.cli("log", "web", "--tail", "1").stdout, b"ready\n")
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

    def test_stopped_name_can_be_reused_and_removed(self):
        previous = None
        for _ in range(3):
            result = self.cli("s", "web", "--", "/bin/sleep", "120")
            self.assertEqual(result.stdout, b"web\n")
            current = self.track()["process_identity"]["pid"]
            self.assertNotEqual(current, previous)
            row = self.cli("list").stdout.decode().splitlines()[1].split()
            self.assertEqual(row[:3], ["web", "running", str(current)])
            self.cli("stop", "web")
            self.assertTrue(self.record()["exited"])
            previous = current
        self.cli("rm", "web")
        self.assertFalse(self.directory().exists())

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
            (path / "output.log").write_bytes((name + "\n").encode())
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
        self.cli("start", "Web", "--", "/bin/sleep", "120")
        _, restarted = self.stored_record("Web")
        self.assertNotEqual(restarted["process_identity"]["pid"], stored["Web"][1]["process_identity"]["pid"])
        self.assertEqual(self.cli("log", "Web").stdout, b"")
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

    def test_exec_failures_retain_only_the_current_run(self):
        script = self.base / "not-executable"
        script.write_text("#!/bin/sh\nexit 0\n")
        for command in [str(self.base / "missing"), str(script)]:
            result = self.cli("start", "broken", "--", command, expect=1)
            self.assertEqual(result.stdout, b"")
            self.assertIn(b"bgx: broken:", result.stderr)
            record = self.record("broken")
            self.assertEqual(record["launch"], "failed")
            self.assertTrue(record["exited"])
            self.assertEqual(record["argv"], [command])
            self.assertIn("execute", record["error"])
            self.assertEqual(self.cli("log", "broken").stdout, b"")
        self.cli("rm", "broken")

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
        self.assertEqual(self.record()["launch"], "failed")

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
            self.cli("start", "web", "--", "/bin/echo", "new")
            os.kill(child.pid, signal.SIGCONT)
            out, err = child.communicate(timeout=3)
            self.assertEqual(child.returncode, 1, err)
            self.assertEqual(out, b"")
            # The old directory is gone; the reader must not print the new run.
            self.assertIn(b"output.log", err)
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

    def test_log_follow_accepts_options_before_or_after_name(self):
        self.make_log(b"kept\n")
        for command in [["log", "-f", "web"],
                        ["log", "--tail", "1", "web", "-f"],
                        ["log", "web", "-f", "--tail", "1"],
                        ["log", "--follow", "web", "--tail", "1"]]:
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
        self.cli("start", "web", "--", "/bin/echo", "beta")
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
        self.assertIn(b"output.log", result.stderr)
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
                self.assertIn(b"output.log", result.stderr)
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

    def test_identity_mismatch_and_pending_launch_refuse_mutation(self):
        original = self.start()
        for changes in ["identity", "pending"]:
            record = deepcopy(original)
            if changes == "identity":
                record["process_identity"]["start_marker"] += "-wrong"
            else:
                record["launch"] = "pending"
            self.save(record)
            before = (self.directory() / "meta.json").read_bytes()
            for args in [("stop", "web"), ("rm", "web"), ("start", "web", "--", "/bin/true")]:
                self.cli(*args, expect=1)
            self.assertEqual((self.directory() / "meta.json").read_bytes(), before)
            os.kill(original["process_identity"]["pid"], 0)
        self.cli("rm", "web", "--force")
        os.kill(original["process_identity"]["pid"], 0)

    def test_previous_boot_is_exited_and_never_signals_reused_pid(self):
        # Simulate an old record whose PID/PGID now refer to a live, unrelated
        # process. The boot mismatch must bypass all signaling of that process.
        for operation in ["stop", "rm", "start"]:
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
                    self.start()
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
        pending = self.record("ddd")
        pending.update(launch="pending", exited=False)
        self.save(pending, "ddd")
        self.start("eee")
        self.cli("stop", "eee")
        records = {name: (self.directory(name) / "meta.json").read_bytes()
                   for name in ["bbb", "ccc", "ddd"]}
        result = self.cli("prune", "--dry-run", expect=1)
        self.assertEqual(result.stdout, b"Would remove aaa\nWould remove eee\nWould remove 2 task(s).\n")
        self.assertIn(b"bgx: bbb: invalid JSON metadata", result.stderr)
        self.assertIn(b"bgx: ccc: process start marker does not match recorded identity", result.stderr)
        self.assertIn(b"bgx: ddd: launch outcome has not been confirmed", result.stderr)
        for name, contents in records.items():
            self.assertEqual((self.directory(name) / "meta.json").read_bytes(), contents, name)
        result = self.cli("prune", expect=1)
        self.assertEqual(result.stdout, b"Removed aaa\nRemoved eee\nRemoved 2 task(s).\n")
        self.assertIn(b"bgx: some tasks could not be pruned", result.stderr)
        self.assertFalse(self.directory("aaa").exists())
        self.assertFalse(self.directory("eee").exists())
        for name, contents in records.items():
            self.assertEqual((self.directory(name) / "meta.json").read_bytes(), contents, name)
        os.kill(stale["process_identity"]["pid"], 0)

    def test_prune_removes_failed_runs(self):
        self.start()
        self.cli("stop", "web")
        self.cli("start", "web", "--", str(self.base / "missing"), expect=1)
        self.assertEqual(self.record()["launch"], "failed")
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
            self.start()
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
            base.replace('"schema_version": 4', '"schema_version": 3'),
            base.replace('"schema_version": 4', '"schema_version": 4, "schema_version": 4'),
            base[:-1] + ', "name": "web"}',
            base.replace('"launch": "started"', '"launch": "started\\u0000suffix"'),
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
        for file in [path, self.log_path(), self.state / "locks" / "web.lock"]:
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)

    def test_symlinks_are_not_followed(self):
        self.make_log()
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
