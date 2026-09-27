"""The package installs, and what installs is a copy.

Hand-off 7: `system` has no Python test suite, so this pull request asserts
the import from the library's own package tests. The assertion that matters
is not `import sd_db` -- that succeeds from the checkout because the checkout
is on `sys.path`. It is that a **built and installed** copy imports, because
that is the path both repositories use: `pip install`, never `-e`, at a tag.
An editable install would put the checkout back on `sys.path` and prove
nothing.

The test is slow -- it builds a wheel and creates a virtual environment --
and it is the only test here that is. It is not skipped when the network is
absent: `--no-index` and a `pyproject.toml` with no dependencies mean nothing
is fetched.
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
#: `PYTHONPATH` says -- `sd-db.sh test` invoked from this folder failed here
#: on 2026-09-06 while the same command from elsewhere passed. The directory
#: is neutral so the result does not depend on where the operator was standing.
NEUTRAL = Path(tempfile.gettempdir())

PROBE = """\
import json, sd_db, sd_db.testing
print(json.dumps({
    "version": sd_db.__version__,
    "file": sd_db.__file__,
    "exports": sorted(sd_db.testing.__all__),
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
        completed = subprocess.run(
            [str(cls.python), "-m", "pip", "install", "--no-index", "--quiet", str(PACKAGE_ROOT)],
            capture_output=True, text=True, env=clean_environment(),
        )
        if completed.returncode != 0:
            cls.tmp.cleanup()
            raise AssertionError(f"pip install failed:\n{completed.stdout}\n{completed.stderr}")

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
        result = self.probe()
        declared = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(result["version"], declared["project"]["version"])

    def test_what_imported_is_not_the_checkout(self):
        installed = Path(self.probe()["file"]).resolve()
        self.assertNotIn(PACKAGE_ROOT, installed.parents)
        self.assertIn("site-packages", installed.parts)

    def test_the_testing_package_ships_with_it(self):
        exports = self.probe()["exports"]
        for name in ("FixtureHome", "FixtureRemote", "GitHubDouble", "ProviderDouble", "Stubs"):
            self.assertIn(name, exports)

    def test_the_installed_harness_runs(self):
        """Not merely importable: the fixture remote works from the copy."""
        script = (
            "import tempfile\n"
            "from sd_db.testing import FixtureRemote, RemoteRefusal\n"
            "with tempfile.TemporaryDirectory() as tmp:\n"
            "    remote = FixtureRemote(tmp)\n"
            "    stale = remote.commit_on('feature', 'work')\n"
            "    pull = remote.open_pull_request('feature')\n"
            "    remote.commit_on('feature', 'again')\n"
            "    try:\n"
            "        remote.merge(pull.number, sha=stale)\n"
            "    except RemoteRefusal as refusal:\n"
            "        print(refusal.status)\n"
        )
        completed = subprocess.run(
            [str(self.python), "-c", script], capture_output=True, text=True,
            env=clean_environment(), cwd=NEUTRAL,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "405")

    def test_a_newer_database_tells_the_installed_copy_to_reinstall(self):
        """sd:1664 names a pull for the checkout; an installed copy has none."""
        completed = subprocess.run(
            [str(self.python), "-c", "from sd_db.errors import SchemaTooNew; print(SchemaTooNew(16, 15))"],
            capture_output=True, text=True, env=clean_environment(), cwd=NEUTRAL,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("install the matching version of sd-db", completed.stdout)
        self.assertNotIn("pull", completed.stdout)

    def test_the_declared_metadata_and_the_built_metadata_agree(self):
        """Two files carry the version; a wheel built from a stale one lies."""
        declared = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        spec = importlib.util.spec_from_file_location("sd_db_build", PACKAGE_ROOT / "_build.py")
        backend = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(backend)
        self.assertEqual(declared["project"]["version"], backend.VERSION)
        self.assertEqual(declared["project"]["name"], backend.DISTRIBUTION)
        self.assertEqual(declared["project"]["requires-python"], backend.REQUIRES_PYTHON)
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
        backend, root = self._backend_on_a_copy("sd_db_build_sdist")
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

    def test_python_typing_is_declared(self):
        """`py.typed` ships, so a caller's type checker sees the annotations."""
        installed = Path(self.probe()["file"]).resolve().parent
        self.assertTrue((installed / "py.typed").is_file())


if __name__ == "__main__":
    sys.exit(unittest.main())
