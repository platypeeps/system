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
case "$1" in
  start)
    docker run -d --rm \\
        -p 19530:19530 \\
        -p 9091:9091 \\
        -p 2379:2379 \\
        milvusdb/milvus ;;
  stop) docker stop milvus-standalone ;;
  upgrade)
    curl -sfL https://example.test/standalone_embed.sh -o standalone_embed_latest.sh &&
    sh standalone_embed_latest.sh start ;;
esac
"""

# Stands in for the download: writes the unpatched upstream text to the -o file.
CURL_STUB = """#!/bin/sh
while [ $# -gt 0 ]; do
  [ "$1" = -o ] && out="$2"
  shift
done
cat "$UPSTREAM_COPY" > "$out"
"""


class MilvusLoopback(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = tmp = Path(self._tmp.name)
        shutil.copy(FOLDER / "milvus.sh", tmp / "milvus.sh")
        self.launcher = tmp / "standalone_embed.sh"
        self.launcher.write_text(UPSTREAM)
        self.launcher.chmod(0o755)
        (tmp / "upstream.sh").write_text(UPSTREAM)
        bindir = tmp / "bin"
        bindir.mkdir()
        for name, body in (
            ("docker", '#!/bin/sh\necho "$@" >> "$DOCKER_LOG"\nexit 0\n'),
            ("curl", CURL_STUB),
        ):
            (bindir / name).write_text(body)
            (bindir / name).chmod(0o755)
        self.log = tmp / "docker.log"
        self.env = dict(
            os.environ,
            PATH=f"{bindir}:{os.environ['PATH']}",
            DOCKER_LOG=str(self.log),
            UPSTREAM_COPY=str(tmp / "upstream.sh"),
        )

    def run_milvus(self, verb):
        return subprocess.run(
            ["sh", str(self.tmp / "milvus.sh"), verb],
            env=self.env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30,
        )

    def published(self):
        return self.log.read_text() if self.log.exists() else ""

    def test_the_tracked_launcher_has_no_bare_publish(self):
        text = (FOLDER / "standalone_embed.sh").read_text()
        self.assertEqual(BARE_PUBLISH.findall(text), [])
        self.assertEqual(len(re.findall(r"-p 127\.0\.0\.1:\d+:\d+", text)), 3)

    def test_milvus_sh_patches_an_unpatched_launcher_and_a_rerun_is_a_no_op(self):
        first = self.run_milvus("stop")
        self.assertEqual(first.returncode, 0, first.stderr)
        once = self.launcher.read_text()
        self.assertEqual(BARE_PUBLISH.findall(once), [])
        self.assertIn("-p 127.0.0.1:2379:2379", once)
        self.assertTrue(os.access(self.launcher, os.X_OK))
        self.assertEqual(self.run_milvus("stop").returncode, 0)
        self.assertEqual(self.launcher.read_text(), once)

    def test_update_starts_a_launcher_that_binds_loopback_only(self):
        result = self.run_milvus("update")
        self.assertEqual(result.returncode, 0, result.stderr)
        runs = [line for line in self.published().splitlines() if line.startswith("run ")]
        self.assertEqual(len(runs), 1, self.published())
        self.assertEqual(BARE_PUBLISH.findall(runs[0]), [], runs[0])
        self.assertIn("-p 127.0.0.1:19530:19530", runs[0])
        self.assertIn("-p 127.0.0.1:2379:2379", runs[0])

    def test_install_patches_the_downloaded_launcher(self):
        self.launcher.write_text("stale\n")
        result = self.run_milvus("install")
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.launcher.read_text()
        self.assertEqual(BARE_PUBLISH.findall(text), [])
        self.assertIn("-p 127.0.0.1:9091:9091", text)

    def test_a_failed_download_keeps_the_current_launcher(self):
        curl = self.tmp / "bin" / "curl"
        curl.write_text("#!/bin/sh\nexit 22\n")
        self.assertEqual(self.run_milvus("stop").returncode, 0)
        before = self.launcher.read_text()
        self.log.unlink(missing_ok=True)
        for verb in ("install", "update"):
            self.assertNotEqual(self.run_milvus(verb).returncode, 0, verb)
            self.assertEqual(self.launcher.read_text(), before, verb)
        self.assertEqual(self.published(), "")


if __name__ == "__main__":
    unittest.main()
