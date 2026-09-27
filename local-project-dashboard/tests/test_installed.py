"""The package installs, and what installs is a copy.

The sibling library's `tests/test_installed.py` states the case and this file
follows it: the assertion that matters is not `import sd_dashboard`, which
succeeds from the checkout because the checkout is on `sys.path`. It is that a
**built and installed** copy imports, because that is the path consumers use.

One thing differs here. `sd_dashboard` declares a dependency on `sd-db`, so
the environment below installs the sibling first, from its folder, offline.
That is the real shape: two wheels in one environment, nothing fetched.

The test is slow -- it builds two wheels and creates a virtual environment --
and it is the only test here that is. It is not skipped when the network is
absent: `--no-index` and pure-Python packages mean nothing is fetched.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
import venv
import zipfile
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
SIBLING = PACKAGE_ROOT.parent / "local-sd-db"


def clean_environment():
    """The caller's `PYTHONPATH` names the checkout, which is the thing this
    file exists to import *around*. Left in place it puts the checkout ahead
    of the installed copy and every assertion below passes for the wrong
    reason."""
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    return environment


#: Where a probe runs. `python -c` puts the working directory on `sys.path`,
#: so a probe run from inside the checkout imports the checkout no matter what
#: `PYTHONPATH` says. The directory is neutral so the result does not depend
#: on where the operator was standing.
NEUTRAL = Path(tempfile.gettempdir())

PROBE = """\
import json, sd_dashboard
from sd_dashboard import server
print(json.dumps({
    "version": sd_dashboard.__version__,
    "file": sd_dashboard.__file__,
    "server_build": callable(server.build),
}))
"""


class TheInstalledCopy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        builder = venv.EnvBuilder(with_pip=True)
        builder.create(root / "venv")
        cls.python = root / "venv" / "bin" / "python"
        for folder in (SIBLING, PACKAGE_ROOT):
            completed = subprocess.run(
                [str(cls.python), "-m", "pip", "install", "--no-index", "--quiet", str(folder)],
                capture_output=True, text=True, env=clean_environment(),
            )
            if completed.returncode != 0:
                cls.tmp.cleanup()
                raise AssertionError(
                    f"pip install {folder.name} failed:\n{completed.stdout}\n{completed.stderr}"
                )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def probe(self):
        completed = subprocess.run(
            [str(self.python), "-c", PROBE], capture_output=True, text=True,
            env=clean_environment(), cwd=NEUTRAL,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_the_package_imports_from_the_installed_copy(self):
        self.assertEqual(self.probe()["version"], "0.1.0")

    def test_what_imported_is_not_the_checkout(self):
        installed = Path(self.probe()["file"]).resolve()
        self.assertNotIn(PACKAGE_ROOT, installed.parents)
        self.assertIn("site-packages", installed.parts)

    def test_the_server_ships_with_it(self):
        """`sd_dashboard.server.build` is what a consumer's test starts."""
        self.assertTrue(self.probe()["server_build"])

    def test_the_static_assets_ship_with_it(self):
        """Not only modules: the stylesheet and script are package data, and a
        wheel that dropped them would serve a dashboard with no styling."""
        installed = Path(self.probe()["file"]).resolve().parent
        for name in ("dashboard.css", "dashboard.js"):
            self.assertTrue((installed / "static" / name).is_file(), name)

    def test_the_declared_dependency_is_in_the_built_metadata(self):
        """A wheel that forgets `Requires-Dist` installs into an environment
        with no `sd_db` and fails at import, not at install."""
        script = (
            "import json\n"
            "from importlib import metadata\n"
            "print(json.dumps(metadata.requires('sd-dashboard')))\n"
        )
        completed = subprocess.run(
            [str(self.python), "-c", script], capture_output=True, text=True,
            env=clean_environment(), cwd=NEUTRAL,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("sd-db", json.loads(completed.stdout))

    def test_the_declared_metadata_and_the_built_metadata_agree(self):
        """Two files carry the version; a wheel built from a stale one lies."""
        declared = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        spec = importlib.util.spec_from_file_location(
            "sd_dashboard_build", PACKAGE_ROOT / "_build.py"
        )
        backend = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(backend)
        self.assertEqual(declared["project"]["version"], backend.VERSION)
        self.assertEqual(declared["project"]["name"], backend.DISTRIBUTION)
        self.assertEqual(declared["project"]["requires-python"], backend.REQUIRES_PYTHON)
        self.assertEqual(declared["project"]["dependencies"], list(backend.REQUIRES))
        self.assertEqual(backend.VERSION, self.probe()["version"])

    def test_the_build_is_reproducible(self):
        """The consumer's snapshot procedure regenerates and compares bytes, so
        equal input must produce an equal wheel.

        Two builds a second apart used to differ: `writestr` given a bare name
        stamps the clock into every zip entry. Comparing two builds catches that
        only when the second one lands in a later second, so the stamps
        themselves are asserted rather than left to the machine's load.
        """
        hashes = set()
        for index in ("a", "b"):
            with tempfile.TemporaryDirectory() as out:
                completed = subprocess.run(
                    [str(self.python), "-m", "pip", "wheel", "--no-index", "--no-deps",
                     "--quiet", "-w", out, str(PACKAGE_ROOT)],
                    capture_output=True, text=True, env=clean_environment(),
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                built = list(Path(out).glob("*.whl"))
                self.assertEqual(len(built), 1, f"{index}: {built}")
                hashes.add(built[0].read_bytes())
                with zipfile.ZipFile(built[0]) as wheel:
                    stamps = {entry.date_time for entry in wheel.infolist()}
                self.assertEqual(stamps, {(1980, 1, 1, 0, 0, 0)},
                                 f"{index}: a wheel entry carries the build time")
        self.assertEqual(len(hashes), 1, "two builds of the same source differ")
    def _backend_on_a_copy(self, module):
        """The backend builds from wherever its own file sits, so a copy of the
        package is a build root this test may mangle the timestamps of without
        touching the checkout."""
        copy = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, copy, True)
        root = copy / PACKAGE_ROOT.name
        shutil.copytree(PACKAGE_ROOT, root, ignore=shutil.ignore_patterns("__pycache__"))
        spec = importlib.util.spec_from_file_location(module, root / "_build.py")
        backend = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(backend)
        return backend, root

    def test_the_source_distribution_is_reproducible_too(self):
        """`build_sdist` used to stamp the build time into the gzip header and
        copy each file's mtime and owner into its tar header, so two source
        distributions of one revision differed for reasons that were not the
        source. The wheel beside it promises the opposite.

        Two builds a second apart would have matched even before the fix, so
        the timestamps are moved between them instead of the clock being
        trusted: that is the input the old code copied and the new code
        ignores.
        """
        backend, root = self._backend_on_a_copy("sd_dashboard_build_sdist")
        built = []
        for stamp in (1000000000, 1600000000):
            for path in sorted(root.rglob("*")):
                os.utime(path, (stamp, stamp))
            with tempfile.TemporaryDirectory() as out:
                name = backend.build_sdist(out)
                built.append((Path(out) / name).read_bytes())
        self.assertEqual(built[0], built[1], "two sdists of the same source differ")
        # Bytes 4..8 of a gzip stream are its MTIME field, and zero means "no
        # timestamp". `tarfile.open(..., "w:gz")` wrote the build time there,
        # which no amount of tar-header care would have hidden.
        self.assertEqual(built[0][4:8], b"\x00\x00\x00\x00", "the gzip header carries a build time")

    def test_an_editable_install_is_refused(self):
        """Not discouraged -- impossible. The backend has no PEP 660 hook."""
        completed = subprocess.run(
            [str(self.python), "-m", "pip", "install", "--no-index", "-e", str(PACKAGE_ROOT)],
            capture_output=True, text=True, env=clean_environment(),
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("build_editable", completed.stdout + completed.stderr)


if __name__ == "__main__":
    sys.exit(unittest.main())
