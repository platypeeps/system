"""Kill the process group a test started, whether or not it is still there.

The suites start `health-check.sh` with `start_new_session=True`, so the
process group id is the script's pid, and clean up with one `killpg`. That
call races the script: it may exit between the last poll and the kill.

When the group is gone, `killpg` answers `ProcessLookupError` (ESRCH). On
macOS it answers `PermissionError` (EPERM) instead while the exited leader is
an unreaped zombie and no live member remains (sd:2261). Both mean there is
nothing left to kill. Any other error is a real failure and is raised.
"""

import os
import signal


def kill_group(pgid, sig=signal.SIGKILL):
    """Send `sig` to process group `pgid`; a group already gone is no error."""
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass
