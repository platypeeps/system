"""Executable stand-ins that macOS does not scan inside a test's deadline.

macOS scans a newly written executable the first time it runs, one file at a
time (`XprotectService`), and remembers the verdict for that file. Measured
here: 0.3 seconds for one fresh script, over 2 seconds each for eight started
together, 10 milliseconds for a second run. `make check` runs every suite at
once and each writes its own stand-ins, so a stand-in a deadline test wrote
waited in that queue for most of the budget the test was measuring, and the
dashboard leg failed on a different deadline test almost every run.

A stand-in written here is a symbolic link to one runner that this process has
already run, so the scan is behind it. The runner reads the stand-in's text
from `<path>.body` with the shell, and reading is not scanned. The stand-in
keeps its own path as `$0`, its arguments, and its pid.
"""

from __future__ import annotations

import atexit
import shutil
import subprocess
import tempfile
from pathlib import Path

_runner: Path | None = None


def _scanned_runner() -> Path:
    global _runner
    if _runner is None:
        home = Path(tempfile.mkdtemp(prefix="stub-runner-"))
        atexit.register(shutil.rmtree, home, ignore_errors=True)
        runner = home / "runner"
        runner.write_text('#!/bin/sh\n. "$0.body"\n')
        # Read-only: a test that rewrites a stand-in in place writes through
        # the link, and that has to fail there, not change every stand-in.
        runner.chmod(0o555)
        Path(f"{runner}.body").write_text(":\n")
        # The one scan, here and not inside a budget.
        subprocess.run([str(runner)], check=True)
        _runner = runner
    return _runner


def executable(path: Path | str, text: str) -> Path:
    """`path` runs `text`, a script that starts with its `#!` line.

    A `#!/bin/sh` script is read by the runner's own shell. Any other
    interpreter is started on `<path>.source`, so that script sees one more
    leading argument than a file run directly would: its own source path.
    """
    path = Path(path)
    shebang, _, rest = text.partition("\n")
    if not shebang.startswith("#!"):
        raise ValueError(f"{path}: a stand-in starts with its #! line, got {shebang!r}")
    if shebang == "#!/bin/sh":
        body = rest
    else:
        Path(f"{path}.source").write_text(text)
        body = f'exec {shebang[2:].strip()} "$0.source" "$@"\n'
    Path(f"{path}.body").write_text(body)
    path.unlink(missing_ok=True)
    path.symlink_to(_scanned_runner())
    return path

