"""Best-effort cleanup of test processes, without hiding permission failures."""
import os
import signal
import time


def kill_if_present(pid, *, group=False):
    send = os.killpg if group else os.kill
    try:
        send(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        # Darwin can return EPERM while a fast-exiting process group is being
        # dismantled. Only accept a subsequent ESRCH as proof it is gone; do not
        # retry SIGKILL or silently accept an inaccessible, still-existing target.
        deadline = time.monotonic() + 0.2
        while True:
            try:
                send(pid, 0)
            except ProcessLookupError:
                return
            except PermissionError:
                pass
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)
