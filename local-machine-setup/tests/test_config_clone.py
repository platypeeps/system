"""The repos stage joins the config root to its private clone (sd:9091).

The config root is a clone of the operator's private config repository.
MACHINE_SETUP_CONFIG_REMOTE names it. `update repos --apply` joins a root
that is no clone yet and keeps every file on disk. Every run reports
uncommitted or unpushed config as DIFFERS, so an edit lands in git.
"""

import os
import subprocess

from tests import fixture_config
from tests import test_claude_settings as base

GIT_ENV = {"GIT_AUTHOR_NAME": "operator", "GIT_AUTHOR_EMAIL": "operator@example.test",
           "GIT_COMMITTER_NAME": "operator", "GIT_COMMITTER_EMAIL": "operator@example.test",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


class ConfigClone(base.SyntheticHome):
    def setUp(self):
        super().setUp()
        self.remote = self.home.parent / "system-config.git"
        seed = self.home.parent / "seed"
        self.git(None, "init", "-q", "--bare", "-b", "main", str(self.remote))
        self.git(None, "clone", "-q", str(self.remote), str(seed))
        (seed / "remote-only.txt").write_text("from the remote\n")
        (seed / "shared.env").write_text("A=remote\n")
        self.git(seed, "add", "-A")
        self.git(seed, "commit", "-q", "-m", "seed")
        self.git(seed, "push", "-q", "origin", "main")
        (self.config_root / "shared.env").write_text("A=local\n")

    def git(self, cwd, *args):
        cmd = ["git"] + (["-C", str(cwd)] if cwd else []) + list(args)
        subprocess.run(cmd, check=True, env={**os.environ, **GIT_ENV}, capture_output=True)

    def run_repos(self, *flags, remote=True):
        env = {"HOME": str(self.home), "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
               "LANG": "en_US.UTF-8", "MACHINE_SETUP_STATE": str(self.state),
               **fixture_config.env(self.config_root), **GIT_ENV,
               "SYSTEM_TOOLS_LABEL_PREFIX": fixture_config.LABEL_PREFIX,
               "SD_PACK_ROOT": str(self.home / "no-pack"),
               "MACHINE_SETUP_CONFIG_REMOTE": str(self.remote) if remote else ""}
        result = subprocess.run([str(self.folder / "machine-setup.sh"), "update", "repos", *flags],
                                env=env, capture_output=True, text=True, cwd=self.cwd,
                                stdin=subprocess.DEVNULL, timeout=120)
        return result.stdout + result.stderr

    def test_a_dry_run_names_the_missing_clone_and_writes_nothing(self):
        out = self.run_repos()

        self.assertIn(f"MISSING config root {self.config_root} is no clone", out)
        self.assertFalse((self.config_root / ".git").exists())

    def test_apply_joins_and_keeps_the_files_on_disk(self):
        out = self.run_repos("--apply")

        self.assertIn(f"JOINED  config root {self.config_root}", out)
        self.assertEqual((self.config_root / "remote-only.txt").read_text(), "from the remote\n")
        self.assertEqual((self.config_root / "shared.env").read_text(), "A=local\n")
        self.assertIn(f"DIFFERS config root {self.config_root} has", out)
        self.assertIn("uncommitted change(s)", out)

    def test_a_committed_clone_is_ok_and_an_unpushed_commit_differs(self):
        self.run_repos("--apply")
        self.git(self.config_root, "add", "-A")
        self.git(self.config_root, "commit", "-q", "-m", "host files")

        out = self.run_repos(remote=False)
        self.assertIn("DIFFERS", out)
        self.assertIn("1 unpushed commit(s)", out)

        self.git(self.config_root, "push", "-q", "origin", "main")
        out = self.run_repos(remote=False)
        self.assertIn(f"ok      config root {self.config_root} committed", out)
        self.assertNotIn("config root", out.replace(f"ok      config root {self.config_root} committed", ""))

    def test_no_remote_and_no_clone_says_nothing(self):
        out = self.run_repos(remote=False)

        self.assertNotIn("config root", out)

    def push_from_elsewhere(self, name):
        other = self.home.parent / name
        self.git(None, "clone", "-q", str(self.remote), str(other))
        (other / "new.txt").write_text(f"{name}\n")
        self.git(other, "add", "-A")
        self.git(other, "commit", "-q", "-m", name)
        self.git(other, "push", "-q", "origin", "main")
        return other

    def committed_root(self):
        self.run_repos("--apply")
        self.git(self.config_root, "add", "-A")
        self.git(self.config_root, "commit", "-q", "-m", "host files")
        self.git(self.config_root, "push", "-q", "origin", "main")

    def head(self, repo):
        return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout.strip()

    def test_apply_fast_forwards_a_clean_root_behind_main(self):
        self.committed_root()
        other = self.push_from_elsewhere("other")

        out = self.run_repos("--apply")
        self.assertIn(f"PULLED  config root {self.config_root} to {self.head(other)[:7]}", out)
        self.assertEqual(self.head(self.config_root), self.head(other))
        self.assertEqual((self.config_root / "new.txt").read_text(), "other\n")

    def test_a_dry_run_does_not_pull(self):
        self.committed_root()
        before = self.head(self.config_root)
        self.push_from_elsewhere("other")

        out = self.run_repos()
        self.assertNotIn("PULLED", out)
        self.assertEqual(self.head(self.config_root), before)

    def test_dirty_diverged_or_off_main_roots_are_left_as_they_are(self):
        self.committed_root()
        self.push_from_elsewhere("other")

        (self.config_root / "shared.env").write_text("A=edited\n")
        before = self.head(self.config_root)
        out = self.run_repos("--apply")
        self.assertNotIn("PULLED", out)
        self.assertEqual(self.head(self.config_root), before)
        self.assertEqual((self.config_root / "shared.env").read_text(), "A=edited\n")

        self.git(self.config_root, "commit", "-q", "-am", "local edit")
        before = self.head(self.config_root)
        out = self.run_repos("--apply")
        self.assertNotIn("PULLED", out)
        self.assertIn("1 unpushed commit(s)", out)
        self.assertEqual(self.head(self.config_root), before)

        self.git(self.config_root, "reset", "-q", "--hard", "HEAD~1")
        self.git(self.config_root, "switch", "-q", "-c", "join/host")
        before = self.head(self.config_root)
        out = self.run_repos("--apply")
        self.assertIn("is on join/host, not main", out)
        self.assertEqual(self.head(self.config_root), before)

    def test_an_untracked_copy_of_an_origin_file_is_named_and_kept(self):
        self.committed_root()
        self.push_from_elsewhere("other")
        self.git(self.config_root, "fetch", "-q", "origin")
        copy = self.config_root / "new.txt"
        copy.write_text("other\n")
        (self.config_root / "notes.txt").write_text("mine\n")
        before = self.head(self.config_root)

        out = self.run_repos("--apply")
        self.assertIn(f"STALE   config root {copy} is identical to origin/main's;"
                      " remove it by hand, then --apply", out)
        self.assertIn(f"DIFFERS config root {self.config_root} has 1 uncommitted", out)
        self.assertNotIn("notes.txt", out)
        self.assertNotIn("PULLED", out)
        self.assertEqual(copy.read_text(), "other\n")
        self.assertEqual(self.head(self.config_root), before)

        (self.config_root / "notes.txt").unlink()
        out = self.run_repos()
        self.assertIn(f"STALE   config root {copy}", out)
        self.assertNotIn("DIFFERS", out)
        self.assertNotIn("ok      config root", out)

    def test_an_untracked_file_unlike_origin_main_is_no_copy(self):
        self.committed_root()
        self.push_from_elsewhere("other")
        self.git(self.config_root, "fetch", "-q", "origin")
        cases = {"bytes": ("other\r\n", 0o644), "mode": ("other\n", 0o755)}
        for name, (text, mode) in cases.items():
            with self.subTest(name):
                copy = self.config_root / "new.txt"
                copy.write_text(text, newline="")
                copy.chmod(mode)
                out = self.run_repos()
                self.assertNotIn("STALE   config root", out)
                self.assertIn("1 uncommitted change(s)", out)
                copy.unlink()
        link = self.config_root / "new.txt"
        target = self.config_root.parent / "target.txt"
        target.write_text("other\n")
        link.symlink_to(target)
        out = self.run_repos()
        self.assertNotIn("STALE   config root", out)
        self.assertIn("1 uncommitted change(s)", out)

    def test_a_copy_in_a_new_folder_with_a_non_ascii_name_is_named(self):
        self.committed_root()
        other = self.home.parent / "nested"
        self.git(None, "clone", "-q", str(self.remote), str(other))
        (other / "dir").mkdir()
        (other / "dir" / "café.env").write_text("B=1\n")
        self.git(other, "add", "-A")
        self.git(other, "commit", "-q", "-m", "nested")
        self.git(other, "push", "-q", "origin", "main")
        self.git(self.config_root, "fetch", "-q", "origin")
        (self.config_root / "dir").mkdir()
        (self.config_root / "dir" / "café.env").write_text("B=1\n")

        out = self.run_repos()
        self.assertIn(f"STALE   config root {self.config_root}/dir/café.env", out)
        self.assertNotIn("DIFFERS", out)
