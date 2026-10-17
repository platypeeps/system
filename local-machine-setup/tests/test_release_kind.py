"""The `.release` kind, `!name` lines and the node `link` field (sd:4853).

Homebrew builds every formula from source on an Intel Mac, and a few CLIs
then pull in rust and llvm. So a profile can name tools to take from their
upstream release archive instead:

* `<profile>.release` lines are `<bins> <owner/repo> <asset> [<tag>]`. The
  release stage puts each bin in ~/.local/bin and records the tag it
  installed; `{version}` and `{tag}` in the asset name take the tag. A tag
  on the line pins the entry; the upgrade sweep moves only the others.
* A `!name` line in a profile file drops the entry `name` (or `<tap>/name`)
  that common lists; capture keeps those lines.
* A `.node` line may carry `link`: ~/.local/bin/node, npm and npx then point
  at fnm's newest node of that major.

Each case runs machine-setup.sh from a copy of this folder. curl is a stub on
PATH that serves archives from the case's folder and logs every URL, so no
case opens a network connection; tar is the real one, unzip a stub.
"""

import hashlib
import io
import os
import pathlib
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest
import zipfile

from tests import fixture_config

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent
LIB = FOLDER.parent / "lib"

BASE = "https://releases.example.test"
REPO = "example-owner/example"
PINNED_REPO = "example-owner/pinned"
MOVED_REPO = "example-owner/moved"

# Serves $ASSETS/<owner>/<repo>/<tag>/<asset> for a download URL, the tag in
# $ASSETS/<owner>/<repo>/LATEST for the latest-release redirect, and answers
# 404 for a checksum file the folder lacks. Logs each URL to $CURL_LOG.
# $ASSETS/<owner>/<repo>/REDIRECT, when there, is the redirect GitHub answers
# instead: empty for a 200 with none (sd:4916).
CURL_STUB = r"""#!/bin/sh
out=""; fmt=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -w) fmt="$2"; shift 2 ;;
    --max-time|-m) shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
echo "$url" >> "$CURL_LOG"
rel=${url#"$RELEASE_BASE"/}
case "$url" in
  */releases/latest)
    r="$ASSETS/${rel%/releases/latest}/REDIRECT"
    if [ -f "$r" ]; then
      [ "$fmt" = '%{redirect_url}' ] && cat "$r"
      exit 0
    fi
    f="$ASSETS/${rel%/releases/latest}/LATEST"
    [ -f "$f" ] || exit 22
    [ "$fmt" = '%{redirect_url}' ] && printf '%s' "${url%/latest}/tag/$(cat "$f")"
    exit 0 ;;
  */releases/download/*)
    f="$ASSETS/$(printf '%s' "$rel" | sed 's|/releases/download/|/|')"
    case "$url" in
      *.sha256)
        if [ -f "$f" ]; then cp "$f" "$out"; code=200; else code=404; fi
        [ "$fmt" = '%{http_code}' ] && printf '%s' "$code"
        exit 0 ;;
    esac
    [ -f "$f" ] || exit 22
    cp "$f" "$out"
    exit 0 ;;
esac
exit 22
"""

# `unzip -q <zip> -d <dir>` through Python's zipfile, keeping the mode bits:
# the Linux legs need not have unzip.
UNZIP_STUB = r"""#!/bin/sh
echo "unzip $*" >> "$CURL_LOG"
exec python3 -I -c '
import os, sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as z:
    for info in z.infolist():
        path = z.extract(info, sys.argv[2])
        mode = info.external_attr >> 16
        if mode:
            os.chmod(path, mode & 0o777)
' "$2" "$4"
"""

# `fnm ls` lists $FNM_HAVE (full versions); `fnm exec --using=<v> -- sh -c
# 'command -v node'` names that version's node under $FNM_ROOT, and any other
# exec fails, so node_link's exact call is the one tested (sd:4916).
FNM_STUB = r"""#!/bin/sh
echo "fnm $*" >> "$CURL_LOG"
case "$1" in
  ls) for v in $FNM_HAVE; do echo "* v$v"; done ;;
  exec)
    v=${2#--using=v}
    case " $FNM_HAVE " in *" $v "*) ;; *) exit 1 ;; esac
    [ $# -eq 6 ] && [ "$3 $4 $5 $6" = "-- sh -c command -v node" ] || exit 1
    echo "$FNM_ROOT/node-versions/v$v/installation/bin/node" ;;
esac
exit 0
"""

TOOL = "#!/bin/sh\necho example {}\n"


def write_exec(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def archive_members(members):
    """(name, bytes, mode) for each member; a str body is encoded."""
    for name, body, mode in members:
        yield name, body.encode() if isinstance(body, str) else body, mode


def make_tar(path, members):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as tar:
        for name, data, mode in archive_members(members):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            tar.addfile(info, io.BytesIO(data))
    return path


def make_zip(path, members):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, data, mode in archive_members(members):
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFREG | mode) << 16
            z.writestr(info, data)
    return path


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)
        self.repo = self.base / "repo"
        self.folder = self.repo / "local-machine-setup"
        shutil.copytree(FOLDER, self.folder, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copytree(LIB, self.repo / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.config = fixture_config.copy_capture_config(self.base)
        self.profiles = self.config / "machine-setup/profiles"
        self.home = self.base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")
        self.bin = self.home / ".local/bin"
        self.records = self.state / "release"
        self.assets = self.base / "assets"
        self.assets.mkdir()
        self.curl_log = self.base / "calls.log"
        self.stubs = self.base / "stubs"
        write_exec(self.stubs / "curl", CURL_STUB)
        write_exec(self.stubs / "unzip", UNZIP_STUB)
        write_exec(self.stubs / "brew", "#!/bin/sh\nexit 0\n")
        fixture_config.seal(self, self.stubs)
        self.fnm_have = ""

    def run_verb(self, *args, **extra):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(self.config),
            "SYSTEM_TOOLS_LABEL_PREFIX": fixture_config.LABEL_PREFIX,
            "MACHINE_SETUP_RELEASE_BASE": BASE,
            "RELEASE_BASE": BASE,
            "ASSETS": str(self.assets),
            "CURL_LOG": str(self.curl_log),
            "FNM_HAVE": self.fnm_have,
            "FNM_ROOT": str(self.base / "fnm"),
            **extra,
        }
        return subprocess.run([str(self.folder / "machine-setup.sh"), *args], env=env,
                              capture_output=True, text=True, cwd=self.tmp.name,
                              stdin=subprocess.DEVNULL, timeout=120)

    def out(self, result):
        return result.stdout + result.stderr

    def urls(self):
        return self.curl_log.read_text().splitlines() if self.curl_log.exists() else []

    def release(self, tag, asset, members, repo=REPO, archive=make_tar, latest=True):
        """Publish ASSET at TAG of REPO; LATEST names TAG when latest is set."""
        path = archive(self.assets / repo / tag / asset, members)
        if latest:
            (self.assets / repo / "LATEST").write_text(tag)
        return path

    def tools(self, tag, *names, root="example"):
        return [(f"{root}/{n}", TOOL.format(tag), 0o755) for n in names]

    def record(self, name="example"):
        """The tag recorded for NAME, or None."""
        line = self.record_line(name)
        return line.split()[0] if line else None

    def record_line(self, name="example"):
        path = self.records / f"{name}.tag"
        return path.read_text().strip() if path.exists() else None

    def installed(self, name):
        path = self.bin / name
        return path.read_text() if path.exists() else None


class ReleaseStage(Case):
    def setUp(self):
        super().setUp()
        (self.profiles / "personal.release").write_text(
            f"# tools from upstream archives\nexample,example-helper {REPO} example-{{version}}-x86_64.tar.gz\n")

    def test_a_dry_run_names_the_missing_entry_and_installs_nothing(self):
        self.release("v1.2.3", "example-1.2.3-x86_64.tar.gz", self.tools("1.2.3", "example", "example-helper"))

        result = self.run_verb("update", "release")

        self.assertEqual(result.returncode, 0, self.out(result))
        self.assertIn("  MISSING release example", result.stdout)
        self.assertIn("  [dry-run] release_install example example,example-helper", result.stdout)
        self.assertEqual(self.urls(), [], self.out(result))
        self.assertFalse(self.bin.exists())
        self.assertIsNone(self.record())

    def test_apply_installs_every_bin_and_records_the_tag(self):
        self.release("v1.2.3", "example-1.2.3-x86_64.tar.gz", self.tools("1.2.3", "example", "example-helper"))

        result = self.run_verb("update", "release", "--apply")

        self.assertEqual(result.returncode, 0, self.out(result))
        self.assertEqual(self.installed("example"), TOOL.format("1.2.3"))
        self.assertEqual(self.installed("example-helper"), TOOL.format("1.2.3"))
        self.assertTrue(os.access(self.bin / "example", os.X_OK))
        self.assertEqual(self.record(), "v1.2.3")
        # {version} is the tag without its leading v.
        self.assertIn(f"{BASE}/{REPO}/releases/download/v1.2.3/example-1.2.3-x86_64.tar.gz", self.urls())
        self.assertEqual(sorted(p.name for p in self.bin.iterdir()), ["example", "example-helper"])

        again = self.run_verb("update", "release", "--apply")

        self.assertIn("  ok      release example v1.2.3", again.stdout, self.out(again))
        self.assertEqual(len([u for u in self.urls() if "/download/" in u]), 2, self.urls())

    def test_a_pinned_tag_is_installed_without_asking_for_the_latest(self):
        (self.profiles / "personal.release").write_text(f"example {REPO} example-{{tag}}.tar.gz v1.0.0\n")
        self.release("v1.0.0", "example-v1.0.0.tar.gz", self.tools("1.0.0", "example"), latest=False)

        result = self.run_verb("update", "release", "--apply")

        self.assertEqual(result.returncode, 0, self.out(result))
        self.assertEqual(self.record(), "v1.0.0")
        self.assertFalse([u for u in self.urls() if u.endswith("/releases/latest")], self.urls())

    def test_a_record_other_than_the_pin_is_reinstalled_at_the_pin(self):
        (self.profiles / "personal.release").write_text(f"example {REPO} example-{{tag}}.tar.gz v1.0.0\n")
        self.release("v1.0.0", "example-v1.0.0.tar.gz", self.tools("1.0.0", "example"), latest=False)
        write_exec(self.bin / "example", TOOL.format("0.9.0"))
        self.records.mkdir(parents=True)
        (self.records / "example.tag").write_text("v0.9.0\n")

        dry = self.run_verb("update", "release")
        self.assertIn("  DIFFERS release example v0.9.0, pinned v1.0.0", dry.stdout, self.out(dry))

        result = self.run_verb("update", "release", "--apply")

        self.assertEqual(result.returncode, 0, self.out(result))
        self.assertEqual(self.installed("example"), TOOL.format("1.0.0"))
        self.assertEqual(self.record(), "v1.0.0")

    def test_a_checksum_that_matches_installs(self):
        path = self.release("v1.2.3", "example-1.2.3-x86_64.tar.gz",
                            self.tools("1.2.3", "example", "example-helper"))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        path.with_name(path.name + ".sha256").write_text(f"{digest.upper()}  {path.name}\n")

        result = self.run_verb("update", "release", "--apply")

        self.assertEqual(result.returncode, 0, self.out(result))
        self.assertEqual(self.record(), "v1.2.3")
        self.assertNotIn("no checksum", result.stdout)

    def test_a_checksum_mismatch_installs_nothing(self):
        path = self.release("v1.2.3", "example-1.2.3-x86_64.tar.gz",
                            self.tools("1.2.3", "example", "example-helper"))
        path.with_name(path.name + ".sha256").write_text("0" * 64 + f"  {path.name}\n")

        result = self.run_verb("update", "release", "--apply")

        self.assertEqual(result.returncode, 1, self.out(result))
        self.assertIn("checksum mismatch", result.stdout)
        self.assertIn("FAILED  release_install example", result.stdout)
        self.assertFalse((self.bin / "example").exists())
        self.assertIsNone(self.record())

    def test_no_published_checksum_says_so_and_installs(self):
        self.release("v1.2.3", "example-1.2.3-x86_64.tar.gz", self.tools("1.2.3", "example", "example-helper"))

        result = self.run_verb("update", "release", "--apply")

        self.assertEqual(result.returncode, 0, self.out(result))
        self.assertIn("no checksum published for example-1.2.3-x86_64.tar.gz", result.stdout)
        self.assertEqual(self.record(), "v1.2.3")

    def test_two_files_of_one_bin_name_install_nothing(self):
        self.release("v1.2.3", "example-1.2.3-x86_64.tar.gz",
                     self.tools("1.2.3", "example", "example-helper")
                     + self.tools("1.2.3", "example", root="example/old"))

        result = self.run_verb("update", "release", "--apply")

        self.assertEqual(result.returncode, 1, self.out(result))
        self.assertIn("holds 2 executable file(s) named example", result.stdout)
        self.assertFalse(self.bin.exists() and any(self.bin.iterdir()))
        self.assertIsNone(self.record())

    def test_a_bin_the_archive_lacks_installs_nothing_and_the_next_run_retries(self):
        self.release("v1.2.3", "example-1.2.3-x86_64.tar.gz", self.tools("1.2.3", "example"))

        result = self.run_verb("update", "release", "--apply")

        self.assertEqual(result.returncode, 1, self.out(result))
        self.assertIn("holds 0 executable file(s) named example-helper", result.stdout)
        self.assertFalse((self.bin / "example").exists())
        self.assertIsNone(self.record())

        self.release("v1.2.3", "example-1.2.3-x86_64.tar.gz", self.tools("1.2.3", "example", "example-helper"))
        retry = self.run_verb("update", "release", "--apply")

        self.assertIn("  MISSING release example", retry.stdout, self.out(retry))
        self.assertEqual(retry.returncode, 0, self.out(retry))
        self.assertEqual(self.record(), "v1.2.3")

    def test_a_file_without_the_executable_bit_does_not_count(self):
        self.release("v1.2.3", "example-1.2.3-x86_64.tar.gz",
                     [("example/example", TOOL.format("1.2.3"), 0o644),
                      ("example/example-helper", TOOL.format("1.2.3"), 0o755)])

        result = self.run_verb("update", "release", "--apply")

        self.assertEqual(result.returncode, 1, self.out(result))
        self.assertIn("holds 0 executable file(s) named example", result.stdout)
        self.assertIsNone(self.record())

    def test_a_zip_asset_is_unpacked(self):
        (self.profiles / "personal.release").write_text(f"example {REPO} example-macos.zip\n")
        self.release("v2.0.0", "example-macos.zip", self.tools("2.0.0", "example"), archive=make_zip)

        result = self.run_verb("update", "release", "--apply")

        self.assertEqual(result.returncode, 0, self.out(result))
        self.assertEqual(self.installed("example"), TOOL.format("2.0.0"))
        self.assertEqual(self.record(), "v2.0.0")

    def test_an_unreadable_latest_tag_installs_nothing(self):
        self.release("v1.2.3", "example-1.2.3-x86_64.tar.gz",
                     self.tools("1.2.3", "example", "example-helper"), latest=False)

        result = self.run_verb("update", "release", "--apply")

        self.assertEqual(result.returncode, 1, self.out(result))
        self.assertIn("no latest release tag", result.stdout)
        self.assertFalse([u for u in self.urls() if "/download/" in u], self.urls())
        self.assertIsNone(self.record())

    def test_a_line_of_another_shape_is_a_failure_and_the_rest_still_install(self):
        (self.profiles / "personal.release").write_text(
            f"broken {REPO}\nexample,example-helper {REPO} example-{{version}}-x86_64.tar.gz\n")
        self.release("v1.2.3", "example-1.2.3-x86_64.tar.gz", self.tools("1.2.3", "example", "example-helper"))

        result = self.run_verb("update", "release", "--apply")

        self.assertEqual(result.returncode, 1, self.out(result))
        self.assertIn(f"FAILED  release: cannot read the line 'broken {REPO}'", result.stdout)
        self.assertEqual(self.record(), "v1.2.3")

    def test_a_copy_an_interrupted_run_left_is_cleared(self):
        """A run stopped between the copy and the move leaves a dot file
        beside the bin; the next install removes it."""
        self.release("v1.2.3", "example-1.2.3-x86_64.tar.gz", self.tools("1.2.3", "example", "example-helper"))
        write_exec(self.bin / ".example.release.99999", "partial")

        result = self.run_verb("update", "release", "--apply")

        self.assertEqual(result.returncode, 0, self.out(result))
        self.assertEqual(sorted(p.name for p in self.bin.iterdir()), ["example", "example-helper"])

    def test_a_lost_bin_is_missing_and_reinstalled(self):
        self.release("v1.2.3", "example-1.2.3-x86_64.tar.gz", self.tools("1.2.3", "example", "example-helper"))
        self.run_verb("update", "release", "--apply")
        (self.bin / "example-helper").unlink()

        result = self.run_verb("update", "release", "--apply")

        self.assertIn("  MISSING release example (no example-helper in", result.stdout, self.out(result))
        self.assertEqual(self.installed("example-helper"), TOOL.format("1.2.3"))

    def test_a_latest_answer_with_no_redirect_installs_nothing(self):
        """GitHub answers 200 with no redirect (sd:4916)."""
        self.release("v1.2.3", "example-1.2.3-x86_64.tar.gz", self.tools("1.2.3", "example", "example-helper"))
        (self.assets / REPO / "REDIRECT").write_text("")

        result = self.run_verb("update", "release", "--apply")

        self.assertEqual(result.returncode, 1, self.out(result))
        self.assertIn("FAILED  release_install example", result.stdout)
        self.assertFalse([u for u in self.urls() if "/download/" in u], self.urls())
        self.assertFalse(self.bin.exists())
        self.assertIsNone(self.record())

    def test_a_redirect_to_releases_with_no_tag_installs_nothing(self):
        """A repo with no release redirects to its /releases page (sd:4916)."""
        self.release("v1.2.3", "example-1.2.3-x86_64.tar.gz", self.tools("1.2.3", "example", "example-helper"))
        (self.assets / REPO / "REDIRECT").write_text(f"{BASE}/{REPO}/releases")

        result = self.run_verb("update", "release", "--apply")

        self.assertEqual(result.returncode, 1, self.out(result))
        self.assertIn("FAILED  release_install example", result.stdout)
        self.assertFalse([u for u in self.urls() if "/download/" in u], self.urls())
        self.assertFalse(self.bin.exists())
        self.assertIsNone(self.record())

    def test_the_record_holds_the_repo_and_asset_beside_the_tag(self):
        self.release("v1.2.3", "example-1.2.3-x86_64.tar.gz", self.tools("1.2.3", "example", "example-helper"))

        self.run_verb("update", "release", "--apply")

        self.assertEqual(self.record_line(), f"v1.2.3 {REPO} example-{{version}}-x86_64.tar.gz")

    def test_a_changed_repo_or_asset_differs_and_reinstalls(self):
        """The tag alone stays the same when the line moves to another repo
        or asset; the entry still reinstalls (sd:4916)."""
        other = "example-owner/fork"
        self.release("v1.2.3", "example-1.2.3-x86_64.tar.gz", self.tools("1.2.3", "example", "example-helper"))
        self.release("v1.2.3", "example-1.2.3-arm64.tar.gz", self.tools("1.2.3-arm", "example", "example-helper"))
        self.release("v1.2.3", "fork-1.2.3.tar.gz", self.tools("1.2.3-fork", "example", "example-helper"),
                     repo=other)
        self.assertEqual(self.run_verb("update", "release", "--apply").returncode, 0)

        for line, tool in ((f"{REPO} example-{{version}}-arm64.tar.gz", "1.2.3-arm"),
                           (f"{other} fork-{{version}}.tar.gz", "1.2.3-fork")):
            (self.profiles / "personal.release").write_text(f"example,example-helper {line}\n")
            dry = self.run_verb("update", "release")
            self.assertIn("  DIFFERS release example ", dry.stdout, self.out(dry))

            result = self.run_verb("update", "release", "--apply")

            self.assertEqual(result.returncode, 0, self.out(result))
            self.assertEqual(self.installed("example"), TOOL.format(tool))
            self.assertEqual(self.record_line(), f"v1.2.3 {line}")
            again = self.run_verb("update", "release")
            self.assertIn("  ok      release example v1.2.3\n", again.stdout, self.out(again))

    def test_a_tag_only_record_an_older_version_wrote_reinstalls_once(self):
        """A record from before sd:4916 names no repo or asset: DIFFERS once,
        then a full record."""
        self.release("v1.2.3", "example-1.2.3-x86_64.tar.gz", self.tools("1.2.3", "example", "example-helper"))
        for name in ("example", "example-helper"):
            write_exec(self.bin / name, TOOL.format("1.2.3"))
        self.records.mkdir(parents=True)
        (self.records / "example.tag").write_text("v1.2.3\n")

        result = self.run_verb("update", "release", "--apply")

        self.assertEqual(result.returncode, 0, self.out(result))
        self.assertIn("  DIFFERS release example ", result.stdout)
        self.assertEqual(self.record_line(), f"v1.2.3 {REPO} example-{{version}}-x86_64.tar.gz")
        again = self.run_verb("update", "release")
        self.assertIn("  ok      release example v1.2.3\n", again.stdout, self.out(again))

    def test_two_lines_of_one_name_fail_and_install_the_first_alone(self):
        """sd:4942: common's line and the profile's own, with no `!name`,
        read each other's record, so each run would install both."""
        (self.profiles / "common.release").write_text(f"example {REPO} example-{{version}}-aarch64.tar.gz\n")
        (self.profiles / "personal.release").write_text(f"example {REPO} example-{{version}}-x86_64.tar.gz\n")
        self.release("v1.2.3", "example-1.2.3-aarch64.tar.gz", self.tools("arm", "example"))
        self.release("v1.2.3", "example-1.2.3-x86_64.tar.gz", self.tools("1.2.3", "example"))

        result = self.run_verb("update", "release", "--apply")

        self.assertEqual(result.returncode, 1, self.out(result))
        self.assertIn(f"  FAILED  release: two lines name example ('example {REPO} example-{{version}}-aarch64.tar.gz'"
                      f" and 'example {REPO} example-{{version}}-x86_64.tar.gz'); add !example", result.stdout)
        self.assertEqual(len([u for u in self.urls() if "/download/" in u]), 2, self.urls())  # archive, .sha256

        again = self.run_verb("update", "release", "--apply")

        self.assertIn("  ok      release example v1.2.3", again.stdout, self.out(again))
        self.assertNotIn("DIFFERS", again.stdout)
        self.assertEqual(len([u for u in self.urls() if "/download/" in u]), 2, self.urls())

    def test_a_profile_without_release_entries_installs_nothing(self):
        (self.profiles / "personal.release").unlink()

        result = self.run_verb("update", "release", "--apply")

        self.assertEqual(result.returncode, 0, self.out(result))
        self.assertIn("== release\n  no release tools in this profile\n", result.stdout)
        self.assertEqual(self.urls(), [])


class ReleaseUpgrade(Case):
    def setUp(self):
        super().setUp()
        (self.profiles / "personal.release").write_text(
            f"example {REPO} example-{{version}}.tar.gz\n"
            f"pinned {PINNED_REPO} pinned-{{version}}.tar.gz v1.0.0\n")
        self.release("v1.2.3", "example-1.2.3.tar.gz", self.tools("1.2.3", "example"))
        self.release("v1.0.0", "pinned-1.0.0.tar.gz", self.tools("1.0.0", "pinned"), repo=PINNED_REPO)
        (self.assets / PINNED_REPO / "LATEST").write_text("v9.0.0")
        result = self.run_verb("update", "release", "--apply")
        self.assertEqual(result.returncode, 0, self.out(result))
        self.curl_log.unlink()
        # The upgrade-report's neighbours: an inventory and a grants probe.
        write_exec(self.repo / "local-project-dashboard/dashboard.sh", "#!/bin/sh\necho ok\n")
        write_exec(self.repo / "local-ai-apps/ai-apps.sh", "#!/bin/sh\necho none\n")

    def test_the_sweep_moves_an_unpinned_entry_to_a_new_tag_and_leaves_the_pin(self):
        self.release("v1.3.0", "example-1.3.0.tar.gz", self.tools("1.3.0", "example"))

        result = self.run_verb("upgrade", "--apply")

        self.assertEqual(result.returncode, 0, self.out(result))
        self.assertIn("  upgraded release example v1.2.3 -> v1.3.0", result.stdout)
        self.assertEqual(self.installed("example"), TOOL.format("1.3.0"))
        self.assertEqual(self.record(), "v1.3.0")
        self.assertEqual(self.record("pinned"), "v1.0.0")
        self.assertFalse([u for u in self.urls() if PINNED_REPO in u], self.urls())

    def test_the_sweep_fails_a_second_line_of_one_name(self):
        (self.profiles / "common.release").write_text(f"example {MOVED_REPO} example-{{version}}.tar.gz\n")
        self.release("v1.2.3", "example-1.2.3.tar.gz", self.tools("moved", "example"), repo=MOVED_REPO)

        result = self.run_verb("upgrade", "--apply")

        self.assertEqual(result.returncode, 1, self.out(result))
        self.assertIn("failed steps: release example", result.stdout)
        self.assertEqual(self.installed("example"), TOOL.format("1.2.3"))
        self.assertEqual(self.record_line(), f"v1.2.3 {REPO} example-{{version}}.tar.gz")

    def test_the_sweep_leaves_an_entry_already_at_the_latest_tag(self):
        result = self.run_verb("upgrade", "--apply")

        self.assertEqual(result.returncode, 0, self.out(result))
        self.assertNotIn("upgraded release", result.stdout)
        self.assertFalse([u for u in self.urls() if "/download/" in u], self.urls())

    def test_a_dry_run_sweep_reads_no_tag_and_names_what_it_would_check(self):
        """The brew half reads nothing in a dry run; nor does this one, so an
        offline dry run fails no step."""
        (self.assets / REPO / "LATEST").unlink()

        result = self.run_verb("upgrade")

        self.assertEqual(result.returncode, 0, self.out(result))
        self.assertIn("  [dry-run] release: check the latest tag of example\n", result.stdout)
        self.assertNotIn("FAILED", result.stdout)
        self.assertEqual(self.urls(), [])
        self.assertEqual(self.record(), "v1.2.3")
        self.assertEqual(self.installed("example"), TOOL.format("1.2.3"))

    def test_a_failed_move_is_a_failed_step_and_keeps_the_old_record(self):
        (self.assets / REPO / "LATEST").write_text("v1.3.0")  # no archive at v1.3.0

        result = self.run_verb("upgrade", "--apply")

        self.assertEqual(result.returncode, 1, self.out(result))
        self.assertIn("failed steps: release example", result.stdout)
        self.assertEqual(self.record(), "v1.2.3")
        self.assertEqual(self.installed("example"), TOOL.format("1.2.3"))

    def test_the_report_runs_the_sweep_for_a_release_alone_and_names_it(self):
        self.release("v1.3.0", "example-1.3.0.tar.gz", self.tools("1.3.0", "example"))

        result = self.run_verb("upgrade-report")

        self.assertNotIn("nothing outdated", result.stdout, self.out(result))
        self.assertIn("\nupgraded (release):\n  example\n", result.stdout)
        self.assertEqual(self.record(), "v1.3.0")

    def test_a_quiet_week_with_every_release_current_stays_quiet(self):
        result = self.run_verb("upgrade-report")

        self.assertEqual(result.returncode, 0, self.out(result))
        self.assertIn("nothing outdated", result.stdout)

    def test_a_latest_tag_the_report_cannot_read_is_unknown_not_current(self):
        (self.assets / REPO / "LATEST").unlink()

        result = self.run_verb("upgrade-report")

        self.assertNotIn("nothing outdated", result.stdout, self.out(result))
        self.assertIn("outdated (release): unknown", result.stdout)


# mv that fails when its last argument ends in /$MV_FAIL; any other mv is
# the real one.
MV_STUB = r"""#!/bin/sh
for a in "$@"; do last="$a"; done
if [ -n "$MV_FAIL" ]; then
  case "$last" in */"$MV_FAIL") exit 1 ;; esac
fi
exec /bin/mv "$@"
"""


class PartialInstall(Case):
    """A run that moved some bins and not the rest leaves no tag record, so
    no later run calls the entry ok (lane round 1, sd:4853)."""

    def setUp(self):
        super().setUp()
        write_exec(self.stubs / "mv", MV_STUB)
        self.release("v1.2.3", "example-1.2.3.tar.gz", self.tools("1.2.3", "example", "example-helper"))
        self.release("v1.3.0", "example-1.3.0.tar.gz", self.tools("1.3.0", "example", "example-helper"),
                     latest=False)

    def install(self, line, **extra):
        (self.profiles / "personal.release").write_text(line)
        return self.run_verb("update", "release", "--apply", **extra)

    def test_an_upgrade_that_moved_one_bin_of_two_is_missing_and_reinstalled(self):
        unpinned = f"example,example-helper {REPO} example-{{version}}.tar.gz\n"
        self.assertEqual(self.install(unpinned).returncode, 0)
        (self.assets / REPO / "LATEST").write_text("v1.3.0")

        swept = self.run_verb("upgrade", "--apply", MV_FAIL="example-helper")

        self.assertIn("failed steps: release example", swept.stdout, self.out(swept))
        self.assertEqual(self.installed("example"), TOOL.format("1.3.0"))
        self.assertEqual(self.installed("example-helper"), TOOL.format("1.2.3"))
        self.assertIsNone(self.record())
        dry = self.run_verb("update", "release")
        self.assertIn("  MISSING release example (no tag recorded)", dry.stdout, self.out(dry))

        again = self.run_verb("update", "release", "--apply")

        self.assertEqual(again.returncode, 0, self.out(again))
        self.assertEqual(self.installed("example-helper"), TOOL.format("1.3.0"))
        self.assertEqual(self.record(), "v1.3.0")

    def test_a_pinned_install_that_moved_one_bin_of_two_is_repaired_at_the_pin(self):
        self.assertEqual(self.install(f"example,example-helper {REPO} example-{{version}}.tar.gz v1.2.3\n")
                         .returncode, 0)
        pinned = f"example,example-helper {REPO} example-{{version}}.tar.gz v1.3.0\n"

        partial = self.install(pinned, MV_FAIL="example-helper")

        self.assertEqual(partial.returncode, 1, self.out(partial))
        self.assertEqual(self.installed("example"), TOOL.format("1.3.0"))
        self.assertIsNone(self.record())

        again = self.install(pinned)

        self.assertIn("  MISSING release example (no tag recorded)", again.stdout, self.out(again))
        self.assertEqual(self.installed("example-helper"), TOOL.format("1.3.0"))
        self.assertEqual(self.record(), "v1.3.0")

    def test_a_record_that_cannot_be_written_leaves_none(self):
        unpinned = f"example,example-helper {REPO} example-{{version}}.tar.gz\n"
        self.assertEqual(self.install(unpinned).returncode, 0)
        (self.assets / REPO / "LATEST").write_text("v1.3.0")

        swept = self.run_verb("upgrade", "--apply", MV_FAIL="example.tag")

        self.assertIn("failed steps: release example", swept.stdout, self.out(swept))
        self.assertEqual(self.installed("example-helper"), TOOL.format("1.3.0"))
        self.assertIsNone(self.record())


# `brew list --formula` and `brew leaves` both name $BREW_HAVE; every install
# is logged.
BREW_STUB = r"""#!/bin/sh
case "$*" in
  "list --formula"|leaves) for f in $BREW_HAVE; do echo "$f"; done ;;
  install*) echo "brew $*" >> "$CURL_LOG" ;;
  --prefix) echo /usr/local ;;
esac
exit 0
"""


class Negation(Case):
    def setUp(self):
        super().setUp()
        write_exec(self.stubs / "brew", BREW_STUB)
        write_exec(self.stubs / "mas", "#!/bin/sh\nexit 0\n")
        for name in ("git", "defaults", "sudo", "launchctl"):
            write_exec(self.stubs / name, "#!/bin/sh\nexit 1\n")
        (self.profiles / "common.brew").write_text("git\nexample-dropped\nexample-tap/tap/example-qualified\n")
        (self.profiles / "personal.brew").write_text("# this machine\n!example-dropped\n!example-qualified\nexample-extra\n")

    def brew(self, have, *args):
        return self.run_verb(*args, BREW_HAVE=have)

    def test_the_brew_stage_installs_neither_a_dropped_entry_nor_its_tap_spelling(self):
        result = self.brew("git", "update", "brew", "--apply")

        installs = [c for c in self.urls() if c.startswith("brew install")]
        self.assertEqual(installs, ["brew install example-extra"], self.out(result))
        self.assertNotIn("!", result.stdout)

    def test_a_bang_line_drops_common_s_entry_and_not_the_profile_s_own(self):
        """`!name` beside the profile's own spelling swaps common's entry."""
        (self.profiles / "personal.brew").write_text("!example-dropped\nother-tap/tap/example-dropped\n")

        result = self.brew("git example-qualified", "update", "brew", "--apply")

        installs = [c for c in self.urls() if c.startswith("brew install")]
        self.assertEqual(installs, ["brew install other-tap/tap/example-dropped"], self.out(result))

    def test_status_reports_a_dropped_formula_still_installed_as_extra(self):
        result = self.brew("git example-dropped example-extra", "status")

        self.assertIn("  extra   : example-dropped", result.stdout, self.out(result))
        self.assertNotIn("missing : example-qualified", result.stdout)

    def test_capture_keeps_the_bang_lines_and_writes_no_dropped_entry_back(self):
        result = self.brew("git example-dropped example-extra example-new", "capture", "--apply")

        lines = [l for l in (self.profiles / "personal.brew").read_text().splitlines()
                 if l.strip() and not l.startswith("#")]
        self.assertEqual(lines, ["!example-dropped", "!example-qualified", "example-extra", "example-new"],
                         self.out(result))

    def test_capture_keeps_the_profile_s_own_spelling_of_a_dropped_entry(self):
        (self.profiles / "personal.brew").write_text(
            "!example-dropped\nexample-extra\nother-tap/tap/example-dropped\n")

        result = self.brew("git example-dropped example-extra other-tap/tap/example-dropped",
                           "capture", "--apply")

        lines = [l for l in (self.profiles / "personal.brew").read_text().splitlines()
                 if l.strip() and not l.startswith("#")]
        self.assertEqual(lines, ["!example-dropped", "example-extra", "other-tap/tap/example-dropped"],
                         self.out(result))

    def test_an_additive_capture_does_not_hold_the_bang_lines_as_removals(self):
        result = self.brew("git example-extra", "capture", "--apply", "--additive")

        self.assertNotIn("HELD    personal.brew", result.stdout, self.out(result))
        self.assertIn("!example-dropped", (self.profiles / "personal.brew").read_text())

    def test_bang_lines_do_not_let_capture_write_a_roster_empty(self):
        """sd:3106's guard: a capture that finds no entry where the profile
        lists some writes nothing, `!` lines or not."""
        before = (self.profiles / "personal.brew").read_text()

        result = self.brew("git", "capture", "--apply")

        self.assertIn("REFUSED personal.brew", result.stdout, self.out(result))
        self.assertEqual((self.profiles / "personal.brew").read_text(), before)

    def test_a_bang_line_drops_a_release_entry_by_its_first_bin(self):
        (self.profiles / "common.release").write_text(f"example,example-helper {REPO} example.tar.gz\n")
        (self.profiles / "personal.release").write_text("!example\n")

        result = self.run_verb("update", "release")

        self.assertIn("== release\n  no release tools in this profile\n", result.stdout, self.out(result))

    def test_compare_lists_no_bang_line_as_an_entry(self):
        """A `!` line drops an entry; it is none (sd:4916)."""
        result = self.run_verb("compare", "personal", "work")

        self.assertEqual(result.returncode, 0, self.out(result))
        self.assertNotIn("!", result.stdout)
        self.assertIn("== brew (match: 0, only personal: 1, only work: 1)", result.stdout)

    def test_report_counts_no_bang_line_as_an_entry(self):
        result = self.run_verb("report", "personal")

        self.assertEqual(result.returncode, 0, self.out(result))
        self.assertNotIn("!", result.stdout)
        self.assertIn("    brew (1)\n      example-extra\n", result.stdout)


class NodeLink(Case):
    def setUp(self):
        super().setUp()
        write_exec(self.stubs / "fnm", FNM_STUB)
        for v in ("26.0.0", "26.1.0", "24.3.0"):
            bindir = self.base / "fnm/node-versions" / f"v{v}" / "installation/bin"
            for name in ("node", "npm", "npx"):
                write_exec(bindir / name, f"#!/bin/sh\necho {v}\n")
        self.fnm_have = "24.3.0 26.0.0 26.1.0"
        (self.profiles / "personal.node").write_text("24\n26 link\n")

    def target(self, version, name):
        return str(self.base / "fnm/node-versions" / f"v{version}" / "installation/bin" / name)

    def links(self):
        return {n: os.readlink(self.bin / n) if (self.bin / n).is_symlink() else None
                for n in ("node", "npm", "npx")}

    def test_link_points_node_npm_and_npx_at_the_newest_installed_major(self):
        dry = self.run_verb("update", "tooling")
        self.assertIn("  MISSING node link 26: ", dry.stdout, self.out(dry))
        self.assertFalse(self.bin.exists())

        result = self.run_verb("update", "tooling", "--apply")

        self.assertEqual(result.returncode, 0, self.out(result))
        self.assertEqual(self.links(), {n: self.target("26.1.0", n) for n in ("node", "npm", "npx")})
        self.assertNotIn("fnm install 26 link", self.urls())

        again = self.run_verb("update", "tooling")
        self.assertIn("  ok      node link 26 -> v26.1.0", again.stdout, self.out(again))

    def test_a_link_to_an_older_node_is_repaired(self):
        self.bin.mkdir(parents=True)
        for n in ("node", "npm", "npx"):
            (self.bin / n).symlink_to(self.target("26.0.0", n))

        dry = self.run_verb("update", "tooling")
        self.assertIn("  DIFFERS node link 26: ", dry.stdout, self.out(dry))

        result = self.run_verb("update", "tooling", "--apply")

        self.assertEqual(result.returncode, 0, self.out(result))
        self.assertEqual(self.links(), {n: self.target("26.1.0", n) for n in ("node", "npm", "npx")})

    def test_a_run_stopped_between_links_is_finished(self):
        """node moved, npm still old, npx not made yet."""
        self.bin.mkdir(parents=True)
        (self.bin / "node").symlink_to(self.target("26.1.0", "node"))
        (self.bin / "npm").symlink_to(self.target("26.0.0", "npm"))

        result = self.run_verb("update", "tooling", "--apply")

        self.assertIn("  DIFFERS node link 26: ", result.stdout, self.out(result))
        self.assertIn("  MISSING node link 26: ", result.stdout)
        self.assertEqual(self.links(), {n: self.target("26.1.0", n) for n in ("node", "npm", "npx")})

    def test_a_temporary_link_an_interrupted_run_left_is_cleared(self):
        self.bin.mkdir(parents=True)
        (self.bin / "node.link.99999").symlink_to(self.target("26.0.0", "node"))

        result = self.run_verb("update", "tooling", "--apply")

        self.assertEqual(result.returncode, 0, self.out(result))
        self.assertEqual(sorted(p.name for p in self.bin.iterdir()), ["node", "npm", "npx"])

    def test_a_version_folder_without_node_links_nothing(self):
        """fnm names a node that is not there: no link moves (sd:4942)."""
        (self.base / "fnm/node-versions/v26.1.0/installation/bin/node").unlink()

        result = self.run_verb("update", "tooling", "--apply")

        self.assertEqual(result.returncode, 1, self.out(result))
        self.assertIn("node link 26: fnm names no node for v26.1.0; no link changed", result.stdout)
        self.assertIn("fnm exec --using=v26.1.0 -- sh -c command -v node", self.urls())
        self.assertFalse(self.bin.exists())

    def test_a_file_that_is_not_a_link_is_left_alone(self):
        write_exec(self.bin / "node", "#!/bin/sh\necho mine\n")

        result = self.run_verb("update", "tooling", "--apply")

        self.assertIn("is not a symlink; left alone", result.stdout, self.out(result))
        self.assertEqual(self.installed("node"), "#!/bin/sh\necho mine\n")

    def test_two_link_lines_change_no_link(self):
        (self.profiles / "personal.node").write_text("24 link\n26 link\n")

        result = self.run_verb("update", "tooling", "--apply")

        self.assertEqual(result.returncode, 1, self.out(result))
        self.assertIn("2 .node lines carry link", result.stdout)
        self.assertFalse(self.bin.exists())

    def test_a_major_fnm_lacks_is_missing_and_links_nothing(self):
        self.fnm_have = "24.3.0"

        result = self.run_verb("update", "tooling")

        self.assertIn("  MISSING node link 26: fnm has no v26", result.stdout, self.out(result))
        self.assertFalse(self.bin.exists())

    def test_a_major_in_common_and_linked_in_the_profile_is_one_major(self):
        (self.profiles / "common.node").write_text("26\n")
        self.fnm_have = "24.3.0"

        result = self.run_verb("update", "tooling", "--apply")

        self.assertEqual(result.stdout.count("  MISSING node 26 (fnm)\n"), 1, self.out(result))
        self.assertEqual([c for c in self.urls() if c.startswith("fnm install")],
                         ["fnm install 26"])

    def test_the_link_field_never_reaches_fnm_or_doctor(self):
        self.fnm_have = "24.3.0"
        # doctor reads iTerm2's defaults and launchd: no answer from either.
        for name in ("defaults", "launchctl", "sudo"):
            write_exec(self.stubs / name, "#!/bin/sh\nexit 1\n")

        self.run_verb("update", "tooling", "--apply")
        doctor = self.run_verb("doctor")

        self.assertIn("fnm install 26", self.urls())
        self.assertNotIn("fnm install 26 link", self.urls())
        self.assertIn("  WARN    fnm node 26 missing — run: fnm install 26\n", doctor.stdout)


if __name__ == "__main__":
    unittest.main()
