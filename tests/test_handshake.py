"""Deterministic fault injection; hooks exist only in the test executable."""
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

TRUE = shutil.which("true")


@unittest.skipUnless(os.environ.get("BGX_FAULT_BIN"), "requires fault-test binary")
class HandshakeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bgx-handshake-")
        self.base = Path(self.temp.name)
        self.env = os.environ.copy()
        self.env["BGX_STATE_HOME"] = str(self.base / "state")
        self.binary = os.environ["BGX_FAULT_BIN"]
        self.child = None
        self.pids = set()
        self.target_pidfile = self.base / "target-pid"
        self.child_pidfile = self.base / "child-pid"
        self.env["BGX_TEST_CHILD_NOTIFY"] = str(self.child_pidfile)

    def tearDown(self):
        for path in [self.target_pidfile, self.child_pidfile]:
            if path.exists():
                self.pids.add(int(path.read_text()))
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.child:
            if self.child.poll() is None:
                self.child.kill()
            self.child.communicate(timeout=5)
        self.temp.cleanup()

    def wait_stopped(self, pid, timeout=3):
        """Confirm the handler's SIGSTOP is in effect before resuming it."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                                    capture_output=True, timeout=1).stdout.strip()
            if status.startswith(b"T"):
                return
            time.sleep(0.005)
        self.fail("process did not stop at the test point")

    def cli(self, *args, expect=0, env=None, binary=None, timeout=3):
        result = subprocess.run([binary or self.binary, *args], env=env or self.env,
                                capture_output=True, timeout=timeout)
        self.assertEqual(result.returncode, expect, (args, result.stderr))
        self.assertNotIn(b"AddressSanitizer", result.stderr)
        self.assertNotIn(b"runtime error:", result.stderr)
        return result

    def spawn(self, program=None, fault=None):
        if fault:
            self.env["BGX_TEST_FAULT"] = fault
        self.child = subprocess.Popen(
            [self.binary, "start", "web", "--", *(program or ["/bin/sleep", "120"])],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def launch(self, point, program=None, fault=None):
        self.env["BGX_TEST_POINT"] = point
        self.env["BGX_TEST_NOTIFY"] = str(self.base / "notice")
        self.spawn(program, fault)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                data = (self.base / "notice").read_text().strip()
                if data:
                    pid = int(data)
                    self.pids.add(pid)
                    # Notification is written before raise(SIGSTOP). Sending
                    # SIGCONT before the stop takes effect loses the wakeup.
                    status = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                                            capture_output=True, timeout=1).stdout.strip()
                    if status.startswith(b"T"):
                        return pid
            except FileNotFoundError:
                pass
            time.sleep(0.005)
        self.fail("fault process did not reach the stopped state")

    def result(self):
        out, err = self.child.communicate(timeout=3)
        self.assertEqual(out, b"")
        self.assertNotEqual(self.child.returncode, 0, err)
        self.assertNotIn(b"AddressSanitizer", err)
        self.assertNotIn(b"runtime error:", err)
        return err

    def record(self):
        return json.loads((self.base / "state/instances/web/meta.json").read_text())

    def attempt(self):
        return json.loads((self.base / "state/instances/web/attempt.json").read_text())

    def assert_no_record(self):
        self.assertFalse((self.base / "state/instances/web").exists())

    def assert_unknown(self):
        # An uncertain attempt may have started a process; it must be visible
        # and must block another launch until it is force-removed.
        self.assertEqual(self.attempt()["state"], "uncertain")
        self.assertFalse((self.base / "state/instances/web/meta.json").exists())
        plain = self.env.copy()
        plain.pop("BGX_TEST_POINT", None)
        plain.pop("BGX_TEST_FAULT", None)
        self.assertIn(b"unknown", self.cli("ls", env=plain, timeout=2).stdout)
        self.cli("start", "web", "--", TRUE, env=plain, expect=1, timeout=2)
        self.cli("restart", "web", env=plain, expect=1, timeout=2)

    def test_killed_before_ready_is_failure(self):
        pid = self.launch("before_ready")
        os.kill(pid, signal.SIGKILL)
        self.result()
        self.assert_no_record()

    def test_hung_before_ready_times_out_and_reaps_child(self):
        self.launch("before_ready")
        self.assertIn(b"timed out", self.result())
        self.assert_no_record()

    def test_hung_before_exec_times_out_but_preserves_uncertain_record(self):
        self.launch("before_exec")
        self.assertIn(b"timed out", self.result())
        self.assert_unknown()

    def test_killed_before_exec_does_not_report_success(self):
        pid = self.launch("before_exec")
        os.kill(pid, signal.SIGKILL)
        self.result()
        self.assert_unknown()

    def test_parent_dies_before_gate_child_cannot_execute(self):
        marker = self.base / "executed"
        pid = self.launch("before_gate", ["/usr/bin/touch", str(marker)])
        self.assertEqual(pid, self.child.pid)
        os.kill(pid, signal.SIGKILL)
        self.child.communicate(timeout=3)
        self.assert_unknown()
        # The gated child sees EOF when its parent dies; no shell/executable runs.
        self.assertFalse(marker.exists())

    def test_parent_dies_after_exec_leaves_unknown_record(self):
        code = "import os,pathlib,time; pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(120)" % str(self.target_pidfile)
        pid = self.launch("before_commit", [sys.executable, "-c", code])
        deadline = time.monotonic() + 2
        while not self.target_pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(self.target_pidfile.exists())
        os.kill(pid, signal.SIGKILL)
        self.child.communicate(timeout=3)
        self.assert_unknown()

    def test_killed_after_exec_notice_is_uncertain(self):
        pid = self.launch("after_exec_notice")
        os.kill(pid, signal.SIGKILL)
        self.result()
        self.assert_unknown()

    def test_parent_interrupt_before_release_cleans_child(self):
        pid = self.launch("before_gate", fault="delay_before_stop")
        os.kill(pid, signal.SIGTERM)
        os.kill(pid, signal.SIGCONT)
        self.assertIn(b"interrupted", self.result())
        self.assert_no_record()

    def test_cleanup_timeout_keeps_unknown_instead_of_hanging(self):
        self.launch("before_ready", fault="cleanup_timeout")
        self.assertIn(b"cleanup", self.result())
        self.assert_unknown()

    def test_commit_failure_stops_target_and_removes_record(self):
        self.spawn(fault="save_identity")
        error = self.result()
        self.assertIn(b"metadata write failure", error)
        self.assert_no_record()

    def test_partial_or_invalid_exec_messages_preserve_record(self):
        for fault in ["partial_exec_error", "invalid_exec_message"]:
            with self.subTest(fault=fault):
                self.spawn([TRUE], fault)
                self.result()
                self.assert_unknown()
                self.cli("rm", "web", "--force", timeout=2)
                self.child_pidfile.unlink()

    def test_normally_exited_zombie_after_eof_confirms_exec(self):
        # Darwin hides an unreaped zombie from proc_pidinfo, so the exec flag
        # cannot be checked there once the target exits. The launch path must
        # still accept a fast normal exit after the EXECUTING notice.
        parent = self.launch("after_eof", [TRUE])
        target = int(self.child_pidfile.read_text())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = subprocess.run(["ps", "-o", "stat=", "-p", str(target)],
                                    capture_output=True, timeout=1).stdout.strip()
            if status.startswith(b"Z"):
                break
            time.sleep(0.005)
        else:
            self.fail("target did not become an unreaped zombie")
        os.kill(parent, signal.SIGCONT)
        out, err = self.child.communicate(timeout=3)
        self.assertEqual(out, b"")
        self.assertEqual(self.child.returncode, 0, err)
        self.assertFalse(self.record()["exited"])
        self.assertFalse((self.base / "state/instances/web/attempt.json").exists())

    def test_signaled_target_during_eof_observation_is_uncertain(self):
        parent = self.launch("after_eof")
        target = int(self.child_pidfile.read_text())
        os.kill(target, signal.SIGKILL)
        # Wait for a dead/zombie target while the parent remains paused. This
        # tests EOF interpretation without racing the kernel's exit bookkeeping.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = subprocess.run(["ps", "-o", "stat=", "-p", str(target)],
                                    capture_output=True, timeout=1).stdout
            if not status.strip() or b"Z" in status:
                break
            time.sleep(0.005)
        os.kill(parent, signal.SIGCONT)
        self.result()
        self.assert_unknown()

    def test_normal_immediate_exit_including_127_is_a_successful_launch(self):
        for status in [0, 1, 127]:
            self.child_pidfile.unlink(missing_ok=True)
            self.cli("start", "web", "--", "/bin/sh", "-c", "exit %d" % status)
            self.assertFalse(self.record()["exited"])
            self.cli("rm", "web", "--force")
            time.sleep(0.02)

    def test_production_binary_ignores_fault_configuration(self):
        binary = os.environ.get("BGX_BIN")
        if not binary:
            self.skipTest("requires production binary")
        self.env["BGX_TEST_POINT"] = "before_ready"
        self.env["BGX_TEST_NOTIFY"] = str(self.base / "notice")
        self.env["BGX_TEST_FAULT"] = "save_identity"
        self.cli("start", "web", "--", TRUE, binary=binary)
        self.assertFalse((self.base / "notice").exists())

    @unittest.skipUnless(sys.platform == "darwin", "macOS libproc error convention")
    def test_process_list_failure_does_not_mean_exited(self):
        self.cli("start", "web", "--", "/bin/sleep", "120")
        original = self.record()
        pid = original["process_identity"]["pid"]
        self.pids.add(pid)
        self.env["BGX_TEST_FAULT"] = "listpids_error"
        self.assertIn(b"unknown", self.cli("list").stdout)
        for args in [["stop", "web"], ["remove", "web"], ["start", "web", "--", TRUE]]:
            self.cli(*args, expect=1)
            self.assertEqual(self.record(), original)
            os.kill(pid, 0)
        self.env.pop("BGX_TEST_FAULT")
        self.cli("stop", "web")

    def test_killed_after_replace_exec_leaves_pending_over_confirmed(self):
        marker = self.base / "target-pid"
        code = "import os,pathlib,time; pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(120)" % str(marker)
        self.cli("start", "web", "--", "/bin/sleep", "120")
        self.pids.add(self.record()["process_identity"]["pid"])
        self.cli("stop", "web")
        confirmed = self.record()
        self.child_pidfile.unlink(missing_ok=True)
        env = dict(self.env, BGX_TEST_POINT="before_commit", BGX_TEST_NOTIFY=str(self.base / "notice"))
        child = subprocess.Popen([self.binary, "replace", "web", "--", sys.executable, "-c", code],
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not (self.base / "notice").exists():
                if child.poll() is not None:
                    self.fail("replace exited before the commit checkpoint")
                time.sleep(0.005)
            self.assertTrue((self.base / "notice").exists())
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not marker.exists():
                time.sleep(0.005)
            self.assertTrue(marker.exists())
            os.kill(child.pid, signal.SIGKILL)
            child.communicate(timeout=3)
            # The confirmed record is untouched and the pending attempt dominates it.
            self.assertEqual(self.record(), confirmed)
            self.assertEqual(self.attempt()["state"], "uncertain")
            self.assertIn(b"unknown", self.cli("ls").stdout)
            for args in [("stop", "web"), ("rm", "web"), ("restart", "web"),
                         ("replace", "web", "--", TRUE)]:
                with self.subTest(args=args):
                    self.cli(*args, expect=1)
            self.assertEqual(self.record(), confirmed)
            self.cli("rm", "web", "--force")
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=3)

    def test_commit_failure_after_replace_keeps_confirmed_definition(self):
        self.cli("start", "web", "--", "/bin/sleep", "120")
        self.pids.add(self.record()["process_identity"]["pid"])
        self.cli("stop", "web")
        confirmed = self.record()
        self.child_pidfile.unlink(missing_ok=True)
        env = dict(self.env, BGX_TEST_FAULT="save_identity", BGX_TEST_POINT="before_commit",
                   BGX_TEST_NOTIFY=str(self.base / "notice"))
        child = subprocess.Popen([self.binary, "replace", "web", "--", "/bin/echo", "replaced"],
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not (self.base / "notice").exists():
                if child.poll() is not None:
                    self.fail("replace exited before the commit checkpoint")
                time.sleep(0.005)
            self.assertTrue((self.base / "notice").exists())
            attempted = json.loads((self.base / "state/instances/web/attempt.json").read_text())
            log = self.base / "state/instances/web" / attempted["log"]
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and b"replaced" not in log.read_bytes():
                time.sleep(0.005)
            self.wait_stopped(child.pid)
            os.kill(child.pid, signal.SIGCONT)
            out, err = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 1, (out, err))
            self.assertIn(b"metadata write failure", err)
            # Useful candidate output is reported by the failing invocation.
            self.assertIn(b"attempt log:", err)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=3)
        # The candidate was aborted; the attempt is retained as diagnostics only.
        self.assertEqual(self.record(), confirmed)
        self.assertFalse((self.base / "state/instances/web/recovery.json").exists())
        attempt = self.attempt()
        self.assertEqual(attempt["state"], "failed")
        self.assertIn("metadata write failure", attempt["error"])
        self.assertIn("log", attempt)
        self.assertIn("process_identity", attempt)
        attempt_log = (self.base / "state/instances/web" / attempt["log"]).read_text()
        self.assertIn("replaced", attempt_log)
        info = self.cli("info", "web").stdout.decode()
        self.assertIn("Attempt log:", info)
        self.assertIn("Failed PID:", info)
        # Restart retries the confirmed definition, not the failed candidate.
        self.child_pidfile.unlink(missing_ok=True)
        self.cli("restart", "web")
        self.pids.add(self.record()["process_identity"]["pid"])
        self.assertEqual(self.record()["argv"], confirmed["argv"])
        self.assertFalse((self.base / "state/instances/web/attempt.json").exists())
        self.cli("stop", "web")
        self.cli("rm", "web")

    def stopped_fixture(self):
        """A durably completed confirmed execution named web."""
        if (self.base / "state/instances/web").exists():
            self.cli("rm", "web", "--force")
        self.child_pidfile.unlink(missing_ok=True)
        self.cli("start", "web", "--", "/bin/sleep", "120")
        self.pids.add(self.record()["process_identity"]["pid"])
        self.cli("stop", "web")
        record = self.record()
        self.assertTrue(record["exited"])
        return record

    def confirmed_with_output(self):
        """A durably completed confirmed execution whose log holds output."""
        if (self.base / "state/instances/web").exists():
            self.cli("rm", "web", "--force")
        self.child_pidfile.unlink(missing_ok=True)
        self.cli("start", "web", "--", "/bin/sh", "-c", "echo confirmed; exec sleep 120")
        self.pids.add(self.record()["process_identity"]["pid"])
        log = self.base / "state/instances/web" / self.record()["log"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and b"confirmed\n" not in log.read_bytes():
            time.sleep(0.005)
        self.assertEqual(log.read_bytes(), b"confirmed\n")
        self.cli("stop", "web")
        record = self.record()
        self.assertTrue(record["exited"])
        self.assertEqual(self.cli("log", "web").stdout, b"confirmed\n")
        return record

    def assert_unresolved_adoption(self, previous):
        """A stays durable and observable while the adoption is unresolved."""
        plain = dict(self.env)
        plain.pop("BGX_TEST_POINT", None)
        plain.pop("BGX_TEST_FAULT", None)
        self.assertTrue((self.base / "state/instances/web/recovery.json").exists())
        info = self.cli("info", "web", env=plain).stdout.decode()
        fields = {key: value.strip() for key, value in
                  (line.split(":", 1) for line in info.splitlines())}
        self.assertEqual(fields["Status"], "unknown")
        self.assertEqual(fields["Reason"], "commit outcome has not been confirmed")
        self.assertEqual(fields["Recorded PID"], str(previous["process_identity"]["pid"]))
        self.assertIn("echo confirmed", fields["Command"])
        self.assertEqual(fields["Log"], str(self.base / "state/instances/web" / previous["log"]))
        self.assertIn("Pending command", fields)
        # The confirmed log stays the normal log.
        self.assertEqual(self.cli("log", "web", env=plain).stdout, b"confirmed\n")
        self.assertIn(b"unknown", self.cli("list", env=plain).stdout)
        result = self.cli("prune", "--dry-run", expect=1, env=plain)
        self.assertIn(b"commit outcome has not been confirmed", result.stderr)
        self.assertEqual(result.stdout, b"Would remove 0 task(s).\n")
        for args in [("start", "web", "--", TRUE), ("stop", "web"), ("rm", "web"), ("restart", "web"),
                     ("replace", "web", "--", TRUE)]:
            with self.subTest(args=args):
                self.cli(*args, expect=1, env=plain)

    def test_post_publication_sync_failure_restores_confirmed_definition(self):
        for fault in ["commit_sync_dir", "commit_sync_registry"]:
            with self.subTest(fault=fault):
                confirmed = self.stopped_fixture()
                self.child_pidfile.unlink(missing_ok=True)
                notice = self.base / "notice"
                notice.unlink(missing_ok=True)
                env = dict(self.env, BGX_TEST_FAULT=fault, BGX_TEST_POINT="before_publication",
                           BGX_TEST_NOTIFY=str(notice))
                child = subprocess.Popen([self.binary, "replace", "web", "--", "/bin/echo", "replaced"],
                                         env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline and not notice.exists():
                        if child.poll() is not None:
                            self.fail("replace exited before the publication checkpoint")
                        time.sleep(0.005)
                    self.assertTrue(notice.exists())
                    attempted = self.attempt()
                    self.assertEqual(attempted["state"], "committing")
                    log = self.base / "state/instances/web" / attempted["log"]
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline and b"replaced" not in log.read_bytes():
                        time.sleep(0.005)
                    self.wait_stopped(child.pid)
                    os.kill(child.pid, signal.SIGCONT)
                    out, err = child.communicate(timeout=5)
                    self.assertEqual(child.returncode, 1, (out, err))
                    self.assertIn(b"injected", err)
                    self.assertIn(b"attempt log:", err)
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.communicate(timeout=3)
                # The failed adoption restored A's definition and completion.
                self.assertEqual(self.record(), confirmed)
                attempt = self.attempt()
                self.assertEqual(attempt["state"], "failed")
                self.assertIn("injected", attempt["error"])
                self.assertIn("log", attempt)
                self.assertIn("replaced",
                              (self.base / "state/instances/web" / attempt["log"]).read_text())
                info = self.cli("info", "web").stdout.decode()
                self.assertIn("Failed command:", info)
                self.assertIn("Failed PID:", info)
                self.assertIn("Attempt log:", info)
                fields = {key: value.strip() for key, value in
                          (line.split(":", 1) for line in info.splitlines())}
                self.assertEqual(fields["Command"], "/bin/sleep 120")
                # restart retries A, not the failed candidate.
                self.child_pidfile.unlink(missing_ok=True)
                self.cli("restart", "web")
                self.pids.add(self.record()["process_identity"]["pid"])
                self.assertEqual(self.record()["argv"], confirmed["argv"])
                self.cli("stop", "web")
                self.cli("rm", "web", "--force")

    def test_post_publication_sync_failure_with_unclean_candidate_stays_unknown(self):
        for fault in ["commit_sync_dir,cleanup_timeout",
                      "commit_sync_dir,cleanup_timeout,uncertain_failure"]:
            with self.subTest(fault=fault):
                previous = self.confirmed_with_output()
                self.child_pidfile.unlink(missing_ok=True)
                env = dict(self.env, BGX_TEST_FAULT=fault)
                result = subprocess.run([self.binary, "replace", "web", "--", "/bin/echo", "replaced"],
                                        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
                self.assertEqual(result.returncode, 1, (result.stdout, result.stderr))
                self.assertIn(b"injected", result.stderr)
                self.assertIn(b"cleanup", result.stderr)
                # meta.json already names the candidate, but the recovery
                # artifact keeps A as the durable confirmed record and matching
                # meta+attempt must not hide the unresolved adoption.
                self.assertIn(self.attempt()["state"], ["uncertain", "committing"])
                self.assert_unresolved_adoption(previous)
                self.cli("rm", "web", "--force")
                self.assertFalse((self.base / "state/instances/web").exists())

    def kill_at_point(self, args, point):
        notice = self.base / "notice"
        notice.unlink(missing_ok=True)
        env = dict(self.env, BGX_TEST_POINT=point, BGX_TEST_NOTIFY=str(notice))
        child = subprocess.Popen([self.binary, *args], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not notice.exists():
                if child.poll() is not None:
                    self.fail("command exited before the test point")
                time.sleep(0.005)
            self.assertTrue(notice.exists())
            self.wait_stopped(child.pid)
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=3)

    def test_crash_after_publication_keeps_a_durable_and_unknown(self):
        previous = self.confirmed_with_output()
        self.child_pidfile.unlink(missing_ok=True)
        self.kill_at_point(["replace", "web", "--", "/bin/sleep", "120"], "meta_renamed")
        attempt = self.attempt()
        self.assertEqual(attempt["state"], "committing")
        self.pids.add(attempt["process_identity"]["pid"])
        # The recovery artifact keeps A the confirmed record; the published B
        # is still only a pending candidate.
        self.assert_unresolved_adoption(previous)
        fields = {key: value.strip() for key, value in
                  (line.split(":", 1) for line in self.cli("info", "web").stdout.decode().splitlines())}
        self.assertEqual(fields["Pending command"], "/bin/sleep 120")
        self.cli("rm", "web", "--force")

    def test_crash_after_finalized_commit_reads_committed(self):
        self.stopped_fixture()
        self.child_pidfile.unlink(missing_ok=True)
        self.kill_at_point(["replace", "web", "--", "/bin/sleep", "120"], "commit_finalized")
        published = self.record()
        self.assertEqual(published["argv"], ["/bin/sleep", "120"])
        self.pids.add(published["process_identity"]["pid"])
        self.assertEqual(self.attempt()["state"], "committing")
        self.assertFalse((self.base / "state/instances/web/recovery.json").exists())
        # The success point was reached: B reads as the confirmed execution and
        # a later mutation only discards the leftover attempt record.
        self.assertIn(b"running", self.cli("ls").stdout)
        info = self.cli("info", "web").stdout.decode()
        fields = {key: value.strip() for key, value in
                  (line.split(":", 1) for line in info.splitlines())}
        self.assertEqual(fields["Command"], "/bin/sleep 120")
        self.assertNotIn("Pending command", fields)
        self.child_pidfile.unlink(missing_ok=True)
        self.cli("restart", "web")
        self.pids.add(self.record()["process_identity"]["pid"])
        self.assertFalse((self.base / "state/instances/web/attempt.json").exists())
        self.cli("stop", "web")
        self.cli("rm", "web")

    def test_interrupted_before_restore_completes_recovery(self):
        previous = self.confirmed_with_output()
        self.child_pidfile.unlink(missing_ok=True)
        notice = self.base / "notice"
        notice.unlink(missing_ok=True)
        env = dict(self.env, BGX_TEST_FAULT="commit_sync_dir", BGX_TEST_POINT="adoption_failed",
                   BGX_TEST_NOTIFY=str(notice))
        child = subprocess.Popen([self.binary, "replace", "web", "--", "/bin/sleep", "120"],
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not notice.exists():
                if child.poll() is not None:
                    self.fail("replace exited before the restore checkpoint")
                time.sleep(0.005)
            self.assertTrue(notice.exists())
            self.wait_stopped(child.pid)
            os.kill(child.pid, signal.SIGKILL)
            child.communicate(timeout=3)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=3)
        # B is definitively failed and A's recovery record remains: the old
        # confirmed execution is visible, with the failure as diagnostics.
        self.assertEqual(self.attempt()["state"], "failed")
        fields = {key: value.strip() for key, value in
                  (line.split(":", 1) for line in self.cli("info", "web").stdout.decode().splitlines())}
        self.assertEqual(fields["Status"], "exited")
        self.assertEqual(fields["Recorded PID"], str(previous["process_identity"]["pid"]))
        self.assertIn("echo confirmed", fields["Command"])
        self.assertEqual(fields["Log"], str(self.base / "state/instances/web" / previous["log"]))
        self.assertIn("Failed command", fields)
        self.assertEqual(self.cli("log", "web").stdout, b"confirmed\n")
        # A later mutation finishes the restoration and proceeds.
        self.child_pidfile.unlink(missing_ok=True)
        self.cli("restart", "web")
        self.pids.add(self.record()["process_identity"]["pid"])
        self.assertFalse((self.base / "state/instances/web/recovery.json").exists())
        self.assertEqual(self.record()["argv"], previous["argv"])
        self.cli("stop", "web")
        self.cli("rm", "web")

    def test_commit_marker_failure_reports_marker_cause(self):
        previous = self.stopped_fixture()
        self.child_pidfile.unlink(missing_ok=True)
        env = dict(self.env, BGX_TEST_FAULT="commit_marker_failure")
        result = subprocess.run([self.binary, "replace", "web", "--", "/bin/echo", "replaced"],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        self.assertEqual(result.returncode, 1, (result.stdout, result.stderr))
        self.assertIn(b"injected commit evidence write failure", result.stderr)
        self.assertNotIn(b"recovery.json", result.stderr)
        # A is untouched and B's diagnostics carry the marker's cause.
        self.assertEqual(self.record(), previous)
        self.assertFalse((self.base / "state/instances/web/recovery.json").exists())
        attempt = self.attempt()
        self.assertEqual(attempt["state"], "failed")
        self.assertIn("injected commit evidence write failure", attempt["error"])
        self.assertNotIn("No such file", attempt["error"])
        self.child_pidfile.unlink(missing_ok=True)
        self.cli("restart", "web")
        self.pids.add(self.record()["process_identity"]["pid"])
        self.cli("stop", "web")
        self.cli("rm", "web")

    def test_recovery_write_failure_keeps_confirmed_definition(self):
        previous = self.stopped_fixture()
        self.child_pidfile.unlink(missing_ok=True)
        env = dict(self.env, BGX_TEST_FAULT="recovery_failure")
        result = subprocess.run([self.binary, "replace", "web", "--", "/bin/echo", "replaced"],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        self.assertEqual(result.returncode, 1, (result.stdout, result.stderr))
        self.assertIn(b"injected recovery evidence write failure", result.stderr)
        self.assertEqual(self.record(), previous)
        self.assertFalse((self.base / "state/instances/web/recovery.json").exists())
        attempt = self.attempt()
        self.assertEqual(attempt["state"], "failed")
        self.assertIn("injected recovery evidence write failure", attempt["error"])

    def test_success_point_removal_failure_restores_confirmed_definition(self):
        previous = self.confirmed_with_output()
        self.child_pidfile.unlink(missing_ok=True)
        env = dict(self.env, BGX_TEST_FAULT="remove_recovery_failure")
        result = subprocess.run([self.binary, "replace", "web", "--", "/bin/echo", "replaced"],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        self.assertEqual(result.returncode, 1, (result.stdout, result.stderr))
        self.assertIn(b"injected recovery removal failure", result.stderr)
        # Publication happened, but the success point failed: A is restored as
        # the confirmed record and B is a definitively failed attempt.
        fields = {key: value.strip() for key, value in
                  (line.split(":", 1) for line in self.cli("info", "web").stdout.decode().splitlines())}
        self.assertEqual(fields["Status"], "exited")
        self.assertEqual(fields["Recorded PID"], str(previous["process_identity"]["pid"]))
        self.assertIn("echo confirmed", fields["Command"])
        self.assertIn("Failed command", fields)
        self.assertEqual(self.cli("log", "web").stdout, b"confirmed\n")
        self.assertEqual(self.attempt()["state"], "failed")
        self.child_pidfile.unlink(missing_ok=True)
        self.cli("restart", "web")
        self.pids.add(self.record()["process_identity"]["pid"])
        self.assertEqual(self.record()["argv"], previous["argv"])
        self.assertFalse((self.base / "state/instances/web/recovery.json").exists())
        self.cli("stop", "web")
        self.cli("rm", "web")

    def test_restore_write_failure_keeps_a_visible_and_unknown(self):
        previous = self.confirmed_with_output()
        self.child_pidfile.unlink(missing_ok=True)
        notice = self.base / "notice"
        env = dict(self.env, BGX_TEST_FAULT="commit_sync_dir,restore_failure",
                   BGX_TEST_POINT="before_publication", BGX_TEST_NOTIFY=str(notice))
        child = subprocess.Popen([self.binary, "replace", "web", "--", "/bin/echo", "candidate output"],
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not notice.exists():
                if child.poll() is not None:
                    self.fail("replace exited before the publication checkpoint")
                time.sleep(0.005)
            self.assertTrue(notice.exists())
            self.wait_stopped(child.pid)
            attempt_log = self.base / "state/instances/web" / self.attempt()["log"]
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and b"candidate output\n" not in attempt_log.read_bytes():
                time.sleep(0.005)
            self.assertEqual(attempt_log.read_bytes(), b"candidate output\n")
            os.kill(child.pid, signal.SIGCONT)
            out, err = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 1, (out, err))
            self.assertEqual(out, b"")
            self.assertIn(b"injected instance directory sync failure", err)
            self.assertIn(b"injected confirmed-record restore failure", err)
            self.assertIn(b"could not restore confirmed record", err)
            self.assertIn(f"attempt log: {attempt_log}".encode(), err)
            self.assertNotIn(b"AddressSanitizer", err)
            self.assertNotIn(b"runtime error:", err)
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=3)
        # The restore never took effect: the recovery artifact still holds A,
        # so reads stay unknown on A's durable definition and log.
        self.assertTrue((self.base / "state/instances/web/recovery.json").exists())
        self.assertIn(self.attempt()["state"], ["failed", "uncertain", "committing"])
        fields = {key: value.strip() for key, value in
                  (line.split(":", 1) for line in self.cli("info", "web").stdout.decode().splitlines())}
        self.assertEqual(fields["Recorded PID"], str(previous["process_identity"]["pid"]))
        self.assertIn("echo confirmed", fields["Command"])
        self.assertEqual(fields["Log"], str(self.base / "state/instances/web" / previous["log"]))
        self.assertEqual(self.cli("log", "web").stdout, b"confirmed\n")
        if self.attempt()["state"] != "failed":
            self.assertEqual(fields["Status"], "unknown")
        # A later mutation resolves it and proceeds.
        self.child_pidfile.unlink(missing_ok=True)
        self.cli("restart", "web")
        self.pids.add(self.record()["process_identity"]["pid"])
        self.assertFalse((self.base / "state/instances/web/recovery.json").exists())
        self.assertEqual(self.record()["argv"], previous["argv"])
        self.cli("stop", "web")
        self.cli("rm", "web")

    def test_crash_before_publication_keeps_confirmed_and_unknown(self):
        confirmed = self.stopped_fixture()
        self.child_pidfile.unlink(missing_ok=True)
        self.kill_at_point(["replace", "web", "--", "/bin/sleep", "120"], "before_publication")
        # The publication never happened: A is untouched, and the released
        # candidate keeps the task visibly unknown.
        self.assertEqual(self.record(), confirmed)
        attempt = self.attempt()
        self.assertEqual(attempt["state"], "committing")
        self.assertTrue((self.base / "state/instances/web/recovery.json").exists())
        self.pids.add(attempt["process_identity"]["pid"])
        self.assertIn(b"unknown", self.cli("ls").stdout)
        info = self.cli("info", "web").stdout.decode()
        self.assertIn("Status:        unknown", info)
        self.assertIn("Pending command:", info)
        result = self.cli("prune", "--dry-run", expect=1)
        self.assertIn(b"commit outcome has not been confirmed", result.stderr)
        self.assertEqual(result.stdout, b"Would remove 0 task(s).\n")
        for args in [("stop", "web"), ("rm", "web"), ("restart", "web"),
                     ("replace", "web", "--", TRUE)]:
            with self.subTest(args=args):
                self.cli(*args, expect=1)
        self.cli("rm", "web", "--force")
