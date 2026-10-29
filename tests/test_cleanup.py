"""Exercise fixture cleanup without signalling unrelated system processes."""
import errno
import signal
import unittest
from unittest.mock import Mock, patch

import test_cli
from process_cleanup import kill_if_present


class CleanupTest(unittest.TestCase):
    def fixture(self):
        fixture = test_cli.BgxTest()
        fixture.followers = []
        fixture.groups = {12345}
        fixture.temp = Mock()
        return fixture

    def test_disappearing_group_permission_error_is_not_a_test_failure(self):
        fixture = self.fixture()
        with patch.object(test_cli.os, "killpg", side_effect=[
            PermissionError(errno.EPERM, "Operation not permitted"),
            ProcessLookupError(errno.ESRCH, "No such process"),
        ]) as kill:
            fixture.tearDown()
        self.assertEqual(kill.call_args_list[0].args, (12345, signal.SIGKILL))
        self.assertEqual(kill.call_args_list[1].args, (12345, 0))
        fixture.temp.cleanup.assert_called_once()

    def test_persistent_permission_errors_are_not_silenced(self):
        fixture = self.fixture()
        with patch.object(test_cli.os, "killpg", side_effect=PermissionError(
            errno.EPERM, "Operation not permitted"
        )):
            with self.assertRaises(PermissionError):
                fixture.tearDown()

    def test_disappearing_process_uses_the_same_cleanup_rule(self):
        with patch.object(test_cli.os, "kill", side_effect=[
            PermissionError(errno.EPERM, "Operation not permitted"),
            ProcessLookupError(errno.ESRCH, "No such process"),
        ]) as kill:
            kill_if_present(12345)
        self.assertEqual([call.args for call in kill.call_args_list],
                         [(12345, signal.SIGKILL), (12345, 0)])

    def test_a_still_existing_group_is_not_treated_as_gone(self):
        fixture = self.fixture()

        def signal_group(pgid, sig):
            if sig == signal.SIGKILL:
                raise PermissionError(errno.EPERM, "Operation not permitted")

        with patch.object(test_cli.os, "killpg", side_effect=signal_group) as kill:
            with self.assertRaises(PermissionError):
                fixture.tearDown()
        self.assertEqual(sum(call.args[1] == signal.SIGKILL for call in kill.call_args_list), 1)
