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

    def assert_failed(self):
        self.assertEqual(self.record()["launch"], "failed")
        self.assertTrue(self.record()["exited"])
        self.assertTrue(self.record()["error"])
        self.assertEqual(sorted(p.name for p in (self.base / "state/instances/web").iterdir()),
                         ["meta.json", "output.log"])

    def assert_unknown(self):
        # A pending launch may have started a process; it must be visible
        # and must block another launch until it is force-removed.
        self.assertEqual(self.record()["launch"], "pending")
        self.assertFalse(self.record()["exited"])
        plain = self.env.copy()
        plain.pop("BGX_TEST_POINT", None)
        plain.pop("BGX_TEST_FAULT", None)
        self.assertIn(b"unknown", self.cli("ls", env=plain, timeout=2).stdout)
        self.cli("start", "web", "--", TRUE, env=plain, expect=1, timeout=2)
        self.cli("stop", "web", env=plain, expect=1, timeout=2)
        self.cli("rm", "web", env=plain, expect=1, timeout=2)

    def test_killed_before_ready_is_failure(self):
        pid = self.launch("before_ready")
        os.kill(pid, signal.SIGKILL)
        self.result()
        self.assert_failed()

    def test_hung_before_ready_times_out_and_reaps_child(self):
        self.launch("before_ready")
        self.assertIn(b"timed out", self.result())
        self.assert_failed()

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
        self.assert_failed()

    def test_cleanup_timeout_keeps_unknown_instead_of_hanging(self):
        self.launch("before_ready", fault="cleanup_timeout")
        self.assertIn(b"cleanup", self.result())
        self.assert_unknown()

    def test_commit_failure_stops_target_and_retains_diagnostics(self):
        self.spawn(fault="save_identity")
        error = self.result()
        self.assertIn(b"metadata write failure", error)
        self.assert_failed()

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
        self.assertEqual(out, b"web\n")
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

    def test_interrupted_preparation_retains_pending_without_identity(self):
        pid = self.launch("launch_prepared")
        self.assertNotIn("process_identity", self.record())
        os.kill(pid, signal.SIGKILL)
        self.child.communicate(timeout=3)
        self.assert_unknown()
        self.cli("prune", "--dry-run", expect=1)
        self.assertTrue((self.base / "state/instances/web/meta.json").exists())

    def test_identity_write_failure_never_releases_the_command(self):
        marker = self.base / "executed"
        self.spawn(["/usr/bin/touch", str(marker)], fault="save_release")
        self.assertIn(b"release identity write failure", self.result())
        self.assert_failed()
        self.assertFalse(marker.exists())

    def test_commit_failure_with_unconfirmed_cleanup_stays_pending(self):
        for fault in ["save_identity,cleanup_timeout", "save_identity,cleanup_timeout,save_diagnostics"]:
            with self.subTest(fault=fault):
                self.spawn(fault=fault)
                self.assertIn(b"cleanup", self.result())
                self.assert_unknown()
                self.assertIn("process_identity", self.record())
                self.cli("rm", "web", "--force")
                self.child_pidfile.unlink(missing_ok=True)

    def test_sync_failure_after_publication_keeps_confirmed_process_manageable(self):
        for fault in ["commit_sync_dir", "commit_sync_registry"]:
            with self.subTest(fault=fault):
                self.spawn(fault=fault)
                error = self.result()
                self.assertIn(b"web:", error)
                self.assertIn(b"command started and is recorded", error)
                record = self.record()
                self.pids.add(record["process_identity"]["pid"])
                self.assertEqual(record["launch"], "started")
                os.kill(record["process_identity"]["pid"], 0)
                self.assertIn(b"running", self.cli("list").stdout)
                self.cli("start", "web", "--", TRUE, expect=1)
                self.cli("stop", "web")
                self.cli("rm", "web")
                self.child_pidfile.unlink(missing_ok=True)

    def test_parent_dies_after_publication_leaves_one_started_record(self):
        pid = self.launch("meta_renamed")
        os.kill(pid, signal.SIGKILL)
        self.child.communicate(timeout=3)
        record = self.record()
        self.pids.add(record["process_identity"]["pid"])
        self.assertEqual(record["launch"], "started")
        self.assertIn(b"running", self.cli("list").stdout)
        self.cli("stop", "web")
        self.assertTrue(self.record()["exited"])

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
