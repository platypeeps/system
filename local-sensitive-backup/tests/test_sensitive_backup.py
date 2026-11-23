"""sensitive-backup.sh stores the named files once per month (sd:10098).

A fake op on PATH keeps the vault in a JSON file, so every case runs
offline against a temporary HOME and config root.
"""

import json
import os
import pathlib
import socket
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import unittest

FOLDER = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = FOLDER / "sensitive-backup.sh"
HOST = socket.gethostname().split(".")[0].lower()
MONTH = time.strftime("%Y-%m")

FAKE_OP = textwrap.dedent("""\
    import json, os, shutil, sys, uuid
    state = os.environ["FAKE_OP_STATE"]
    vault = json.load(open(state))
    args = sys.argv[1:]
    with open(state + ".log", "a") as log:
        log.write(" ".join(args[:2]) + "\\n")
    fail = os.environ.get("FAKE_OP_FAIL", "")
    if fail and " ".join(args[:2]) == fail:
        sys.exit("op: failed on purpose")
    if args[:2] == ["document", "create"]:
        ident = uuid.uuid4().hex
        copy = state + "." + ident
        shutil.copy(args[2], copy)
        title = args[args.index("--title") + 1]
        vault.append({"id": ident, "title": title, "category": "DOCUMENT",
                      "created_at": "2026-01-01T00:00:00Z", "file": copy,
                      "vault": args[args.index("--vault") + 1]})
        print(json.dumps({"uuid": ident}))
    elif args[:2] == ["item", "list"]:
        print(json.dumps(vault))
    elif args[:2] == ["document", "delete"]:
        vault = [i for i in vault if i["id"] != args[2]]
    json.dump(vault, open(state, "w"))
    """)


class SensitiveBackupTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "home"
        self.config = self.root / "config"
        (self.config / "sensitive-backup").mkdir(parents=True)
        (self.home / ".ssh").mkdir(parents=True)
        (self.home / ".ssh/id_example").write_text("key\n")
        (self.home / ".ssh/known_hosts").write_text("hosts\n")
        (self.home / ".ssh/config.bak-1").write_text("old\n")
        (self.home / ".aws").mkdir()
        (self.home / ".aws/credentials").write_text("secret-value\n")
        (self.home / ".aws/cache").write_text("skip\n")
        (self.home / "naïve name.env").write_text("ü\n")
        self.targets(".ssh", "!known_hosts*", "!*.bak*", ".aws/credentials",
                     "naïve*")
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "fake_op.py").write_text(FAKE_OP)
        self.state = self.root / "vault.json"
        self.vault([])
        self.env = {**os.environ, "HOME": str(self.home),
                    "SYSTEM_TOOLS_CONFIG": str(self.config),
                    "OP_SERVICE_ACCOUNT_TOKEN": "token-for-test",
                    "FAKE_OP_STATE": str(self.state), "TMPDIR": str(self.root),
                    "PYTHON": sys.executable}
        op = bin_dir / "op"
        op.write_text(f"#!/bin/sh\nexec '{sys.executable}' '{bin_dir / 'fake_op.py'}' \"$@\"\n")
        op.chmod(0o755)
        self.env["SENSITIVE_BACKUP_OP"] = str(op)

    def targets(self, *lines):
        (self.config / "sensitive-backup/targets").write_text("\n".join(lines) + "\n")

    def vault(self, items=None):
        if items is not None:
            self.state.write_text(json.dumps(items))
        return json.loads(self.state.read_text())

    def calls(self):
        log = pathlib.Path(str(self.state) + ".log")
        return log.read_text().splitlines() if log.exists() else []

    def run_script(self, *args, **env):
        return subprocess.run(["sh", str(SCRIPT), *args], capture_output=True, text=True,
                              env={**self.env, **env}, timeout=120)

    def test_run_stores_the_named_files_and_nothing_else(self):
        done = self.run_script("run")
        self.assertEqual(done.returncode, 0, done.stderr)
        [item] = self.vault()
        self.assertEqual(item["title"], f"{HOST} sensitive files {MONTH}")
        self.assertEqual(item["vault"], "Backups")
        with tarfile.open(item["file"]) as tar:
            names = sorted(tar.getnames())
        self.assertEqual(names, [".aws/credentials", ".ssh/id_example", "naïve name.env"])
        self.assertIn(f"stored {item['id']}: 3 files,", done.stdout)
        self.assertNotIn("secret-value", done.stdout + done.stderr)
        self.assertNotIn("token-for-test", done.stdout + done.stderr)
        self.assertEqual([p for p in self.root.iterdir() if p.name.startswith("sensitive-backup.")], [])

    def test_a_second_run_in_a_month_replaces_the_item(self):
        self.assertEqual(self.run_script("run").returncode, 0)
        first = self.vault()[0]["id"]
        done = self.run_script("run")
        self.assertEqual(done.returncode, 0, done.stderr)
        [item] = self.vault()
        self.assertNotEqual(item["id"], first)
        self.assertIn("deleted 1 items", done.stdout)

    def test_only_the_newest_twelve_months_of_this_host_stay(self):
        old = [{"id": f"m{m:02d}", "title": f"{HOST} sensitive files 2000-{m:02d}",
                "category": "DOCUMENT", "created_at": ""} for m in range(1, 13)]
        others = [{"id": "other-host", "title": f"x{HOST} sensitive files 2000-01",
                   "category": "DOCUMENT"},
                  {"id": "suffix", "title": f"{HOST} sensitive files 2000-01 copy",
                   "category": "DOCUMENT"}]
        self.vault(old + others)
        done = self.run_script("run")
        self.assertEqual(done.returncode, 0, done.stderr)
        ids = {i["id"] for i in self.vault()}
        self.assertNotIn("m01", ids)
        self.assertTrue({"m02", "m12", "other-host", "suffix"} <= ids)
        self.assertIn("kept 12 months, deleted 1 items", done.stdout)

    def test_no_token_refuses_before_op_runs(self):
        done = self.run_script("run", OP_SERVICE_ACCOUNT_TOKEN="")
        self.assertEqual(done.returncode, 1)
        self.assertIn("OP_BACKUP_SERVICE_ACCOUNT_TOKEN", done.stderr)
        self.assertEqual(self.calls(), [])

    def test_a_failed_create_removes_the_tarball_and_fails(self):
        done = self.run_script("run", FAKE_OP_FAIL="document create")
        self.assertEqual(done.returncode, 1)
        self.assertIn("op document create exited", done.stderr)
        self.assertEqual(self.vault(), [])
        self.assertEqual([p for p in self.root.iterdir() if p.name.startswith("sensitive-backup.")], [])

    def test_a_failed_prune_fails_after_the_backup_is_stored(self):
        done = self.run_script("run", FAKE_OP_FAIL="item list")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(len(self.vault()), 1)
        self.assertIn("SD_REPORT_ATTENTION: backup stored, prune failed", done.stdout)

    def test_unsafe_target_lines_are_refused(self):
        for line in ("/etc/hosts", "~/.ssh", ".ssh/../../x", "!a/b"):
            with self.subTest(line=line):
                self.targets(line)
                done = self.run_script("plan")
                self.assertEqual(done.returncode, 1)
                self.assertIn("targets:1:", done.stderr)

    def test_a_target_that_vanished_fails_and_keeps_the_month(self):
        self.assertEqual(self.run_script("run").returncode, 0)
        [first] = self.vault()
        (self.home / ".aws/credentials").unlink()
        done = self.run_script("run")
        self.assertEqual(done.returncode, 1)
        self.assertIn("no file matches .aws/credentials", done.stderr)
        self.assertEqual(self.vault(), [first])
        self.assertNotIn("document delete", self.calls())

    def test_an_unreadable_folder_fails_the_run(self):
        locked = self.home / ".ssh/locked"
        locked.mkdir()
        (locked / "key").write_text("k\n")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        done = self.run_script("run")
        self.assertEqual(done.returncode, 1)
        self.assertIn("cannot read", done.stderr)
        self.assertEqual(self.vault(), [])

    def test_a_symlinked_target_folder_is_refused(self):
        shared = self.root / "shared"
        shared.mkdir()
        (shared / "other").write_text("x\n")
        (self.home / ".kube").symlink_to(shared)
        for line in (".kube", ".kube/*"):
            with self.subTest(line=line):
                self.targets(line)
                done = self.run_script("plan")
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn("is a symlink", done.stderr)

    def test_retention_never_deletes_the_new_document(self):
        future = [{"id": f"f{m:02d}", "title": f"{HOST} sensitive files 2999-{m:02d}",
                   "category": "DOCUMENT"} for m in range(1, 13)]
        self.vault(future)
        done = self.run_script("run")
        self.assertEqual(done.returncode, 0, done.stderr)
        titles = {i["title"] for i in self.vault()}
        self.assertIn(f"{HOST} sensitive files {MONTH}", titles)
        self.assertEqual(len(titles), 12)
        self.assertNotIn(f"{HOST} sensitive files 2999-01", titles)

    def test_a_wildcard_before_the_last_part_is_refused(self):
        for line in (".ssh/**/*", "*/credentials", ".a*/credentials"):
            with self.subTest(line=line):
                self.targets(line)
                done = self.run_script("plan")
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn("targets:1:", done.stderr)

    def test_an_unreadable_folder_a_glob_lists_fails(self):
        locked = self.home / ".locked"
        locked.mkdir()
        (locked / "a.env").write_text("x\n")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        self.targets(".ssh", ".locked/*.env")
        done = self.run_script("plan")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("cannot read", done.stderr)

    def test_a_target_that_adds_no_file_fails(self):
        (self.home / ".empty").mkdir()
        (self.home / ".only-excluded").mkdir()
        (self.home / ".only-excluded/known_hosts").write_text("h\n")
        for line in (".empty", ".only-excluded"):
            with self.subTest(line=line):
                self.targets(".ssh", line, "!known_hosts*")
                done = self.run_script("plan")
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn(f"{line} adds no file", done.stderr)

    def test_a_matched_broken_link_fails(self):
        (self.home / ".aws/config").symlink_to(self.home / "gone")
        self.targets(".aws/c*")
        done = self.run_script("plan")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn(".aws/config is not a file", done.stderr)

    def test_a_file_gone_before_packing_fails(self):
        sys.path.insert(0, str(FOLDER))
        import sensitive_backup
        with self.assertRaises(sensitive_backup.Failure):
            sensitive_backup.pack({"gone": self.home / "gone"}, self.root)

    def test_each_folder_a_wildcard_matches_must_add_a_file(self):
        (self.home / ".tool-a").mkdir()
        (self.home / ".tool-b").mkdir()
        (self.home / ".tool-b/key").write_text("k\n")
        self.targets(".tool-*")
        done = self.run_script("plan")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn(".tool-a adds no file", done.stderr)

    def test_a_trailing_slash_requires_a_folder(self):
        (self.home / ".tool").write_text("now a file\n")
        (self.home / ".dirs").mkdir()
        (self.home / ".dirs/plain").write_text("f\n")
        for line in (".tool/", ".dirs/p*/"):
            with self.subTest(line=line):
                self.targets(line)
                done = self.run_script("plan")
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn("is not a folder", done.stderr)

    def test_targets_that_match_nothing_store_nothing(self):
        self.targets(".absent")
        done = self.run_script("run")
        self.assertEqual(done.returncode, 1)
        self.assertIn("no file matches .absent", done.stderr)
        self.assertEqual(self.calls(), [])

    def test_a_missing_targets_file_names_the_example(self):
        (self.config / "sensitive-backup/targets").unlink()
        done = self.run_script("plan")
        self.assertEqual(done.returncode, 1)
        self.assertIn("targets.example", done.stderr)

    def test_a_symlinked_folder_inside_a_target_is_not_entered(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "big").write_text("x\n")
        (self.home / ".ssh/link").symlink_to(outside)
        (self.home / ".ssh/filelink").symlink_to(self.home / ".aws/cache")
        done = self.run_script("plan")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn(".ssh/link/big", done.stdout)
        self.assertIn(".ssh/filelink", done.stdout)

    def test_plan_needs_no_token(self):
        done = self.run_script("plan", OP_SERVICE_ACCOUNT_TOKEN="")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("plan: 3 files", done.stdout)

    def test_usage(self):
        self.assertEqual(self.run_script().returncode, 1)
        self.assertEqual(self.run_script("help").returncode, 0)


if __name__ == "__main__":
    unittest.main()
