"""The Milvus launcher publishes its ports on loopback only (sd:3551).

The vendored upstream standalone_embed.sh publishes 19530, 9091 and 2379 on
every interface, against the README. milvus.sh rewrites the bare `-p` lines
before each run, so a freshly refreshed copy is patched too. The test runs
milvus.sh in a scratch copy with an unpatched fixture and a stub docker.
Stdlib only, so it runs in the `tests/ci-native.sh` preflight.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FOLDER = REPO_ROOT / "local-milvus"

BARE_PUBLISH = re.compile(r"-p\s+\d+:\d+")

UPSTREAM = """#!/bin/sh
echo "$@" >/dev/null
docker run -d --rm \\
    -p 19530:19530 \\
    -p 9091:9091 \\
    -p 2379:2379 \\
    milvusdb/milvus
"""


class MilvusLoopback(unittest.TestCase):
    def test_the_tracked_launcher_has_no_bare_publish(self):
        text = (FOLDER / "standalone_embed.sh").read_text()
        self.assertEqual(BARE_PUBLISH.findall(text), [])
        self.assertEqual(len(re.findall(r"-p 127\.0\.0\.1:\d+:\d+", text)), 3)

    def test_milvus_sh_patches_an_unpatched_launcher_and_a_rerun_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            shutil.copy(FOLDER / "milvus.sh", tmp / "milvus.sh")
            launcher = tmp / "standalone_embed.sh"
            launcher.write_text(UPSTREAM)
            launcher.chmod(0o755)
            bindir = tmp / "bin"
            bindir.mkdir()
            stub = bindir / "docker"
            stub.write_text("#!/bin/sh\nexit 0\n")
            stub.chmod(0o755)
            env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")

            def run():
                return subprocess.run(
                    ["sh", str(tmp / "milvus.sh"), "stop"],
                    env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30,
                )

            first = run()
            self.assertEqual(first.returncode, 0, first.stderr)
            once = launcher.read_text()
            self.assertEqual(BARE_PUBLISH.findall(once), [])
            self.assertIn("-p 127.0.0.1:2379:2379", once)
            self.assertTrue(os.access(launcher, os.X_OK))
            self.assertEqual(run().returncode, 0)
            self.assertEqual(launcher.read_text(), once)


if __name__ == "__main__":
    unittest.main()
