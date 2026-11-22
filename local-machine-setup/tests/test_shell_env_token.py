"""The tooling stage keeps env.sh's per-account claude token block (sd:9905).

Headless claude reads CUSTOM_CLAUDE_CODE_OAUTH_TOKEN from env.sh. The old
one-line export read one Keychain item for every account; the block reads the
item of the account cswap made active, the plain item its fallback. The stage
turns the old line into the block between marker comments, once, and changes
no other line. A block written by hand is left alone; one that reads by
email alone is named, since two accounts with one email load one item. A line with a
value of its own is named by number only and never touched.
"""

from tests import test_claude_settings as base

OLD = ('export CUSTOM_CLAUDE_CODE_OAUTH_TOKEN="$(security find-generic-password '
       '-s claude-code-oauth-token -w 2>/dev/null)"\n')
BEFORE = "# keys\nexport OTHER_KEY=change-me\n"
AFTER = "export LAST_KEY=change-me\n"
START = "# >>> machine-setup: claude token per cswap account (sd:9905)"
END = "# <<< machine-setup: claude token per cswap account"


class TokenBlock(base.SyntheticHome):
    def setUp(self):
        super().setUp()
        self.env_sh = self.home / ".config/shell/env.sh"
        self.env_sh.parent.mkdir(parents=True)

    def write(self, text):
        self.env_sh.write_text(text)
        self.env_sh.chmod(0o600)

    def test_a_dry_run_names_the_old_line_and_writes_nothing(self):
        self.write(BEFORE + OLD + AFTER)
        out = self.run_tooling()
        self.assertIn("DIFFERS ~/.config/shell/env.sh line 3", out)
        self.assertEqual(self.env_sh.read_text(), BEFORE + OLD + AFTER)

    def test_apply_turns_the_old_line_into_the_block_once(self):
        self.write(BEFORE + OLD + AFTER)
        self.run_tooling("--apply")
        text = self.env_sh.read_text()
        self.assertTrue(text.startswith(BEFORE + START + "\n"), text)
        self.assertTrue(text.endswith(END + "\n" + AFTER), text)
        self.assertNotIn(OLD, text)
        self.assertIn('"claude-code-oauth-token-$_cc_acct"', text)
        self.assertIn("-s claude-code-oauth-token -w", text)
        self.assertEqual(oct(self.env_sh.stat().st_mode & 0o777), "0o600")
        out = self.run_tooling("--apply")
        self.assertIn("ok      ~/.config/shell/env.sh reads the claude token per account", out)
        self.assertEqual(self.env_sh.read_text(), text)

    def test_the_block_sources_and_exports_the_active_accounts_key(self):
        import subprocess
        self.write(BEFORE + OLD + AFTER)
        self.run_tooling("--apply")
        (self.home / ".claude.json").write_text(
            '{"oauthAccount": {"emailAddress": "a@example.test", "organizationUuid": "org-a"}}')
        security = self.stubs / "security-fake"
        base.write_exec(security / "security",
                        '#!/bin/sh\n[ "$3 $5" = "claude-code-oauth-token-a@example.test org-a" ] && echo tok-a && exit 0\n'
                        '[ "$3" = claude-code-oauth-token ] && echo tok-plain && exit 0\nexit 44\n')
        run = subprocess.run(["/bin/sh", "-c", f'. "{self.env_sh}"; echo "$CUSTOM_CLAUDE_CODE_OAUTH_TOKEN"'],
                             env={"HOME": str(self.home), "PATH": f"{security}:/usr/bin:/bin"},
                             capture_output=True, text=True, check=False)
        self.assertEqual(run.stdout, "tok-a\n", run.stderr)
        (self.home / ".claude.json").write_text(
            '{"oauthAccount": {"emailAddress": "b@example.test", "organizationUuid": "org-b"}}')
        run = subprocess.run(["/bin/sh", "-c", f'. "{self.env_sh}"; echo "$CUSTOM_CLAUDE_CODE_OAUTH_TOKEN"'],
                             env={"HOME": str(self.home), "PATH": f"{security}:/usr/bin:/bin"},
                             capture_output=True, text=True, check=False)
        self.assertEqual(run.stdout, "tok-plain\n", run.stderr)

    def test_the_block_falls_back_under_set_e_with_no_account(self):
        import subprocess
        self.write(BEFORE + OLD + AFTER)
        self.run_tooling("--apply")
        security = self.stubs / "security-fake"
        base.write_exec(security / "security",
                        '#!/bin/sh\n[ "$3" = claude-code-oauth-token ] && echo tok-plain && exit 0\nexit 44\n')
        for claude_json in (None, '{"other": 1}'):
            if claude_json is not None:
                (self.home / ".claude.json").write_text(claude_json)
            for shell in ("/bin/sh", "/bin/bash"):
                run = subprocess.run([shell, "-e", "-c",
                                      f'. "{self.env_sh}"; echo "$CUSTOM_CLAUDE_CODE_OAUTH_TOKEN"'],
                                     env={"HOME": str(self.home), "PATH": f"{security}:/usr/bin:/bin"},
                                     capture_output=True, text=True, check=False)
                self.assertEqual((run.returncode, run.stdout), (0, "tok-plain\n"),
                                 f"{shell} {claude_json}: {run.stderr}")

    def test_a_hand_written_email_only_block_is_named_and_untouched(self):
        hub = ('_cc_acct="$(/usr/bin/plutil -extract oauthAccount.emailAddress raw -o - '
               '"$HOME/.claude.json" 2>/dev/null)"\n'
               'export CUSTOM_CLAUDE_CODE_OAUTH_TOKEN="$( { [ -n "$_cc_acct" ] && security '
               'find-generic-password -s "claude-code-oauth-token-$_cc_acct" -w 2>/dev/null; } '
               '|| security find-generic-password -s claude-code-oauth-token -w 2>/dev/null)"\n'
               'unset _cc_acct\n')
        self.write(BEFORE + hub + AFTER)
        out = self.run_tooling("--apply")
        # By email alone, two accounts with one email load one item.
        self.assertIn("WARN    ~/.config/shell/env.sh reads the claude token by email alone", out)
        self.assertNotIn("reads the claude token per account", out)
        self.assertEqual(self.env_sh.read_text(), BEFORE + hub + AFTER)
        org = hub.replace('-s "claude-code-oauth-token-$_cc_acct" -w',
                          '-s "claude-code-oauth-token-$_cc_acct" -a "$_cc_org" -w')
        self.write(BEFORE + org + AFTER)
        out = self.run_tooling("--apply")
        self.assertIn("ok      ~/.config/shell/env.sh reads the claude token per account", out)
        self.assertEqual(self.env_sh.read_text(), BEFORE + org + AFTER)

    def test_a_line_with_its_own_value_is_named_by_number_and_kept(self):
        own = "export CUSTOM_CLAUDE_CODE_OAUTH_TOKEN=tok-literal-value\n"
        self.write(BEFORE + own + AFTER)
        out = self.run_tooling("--apply")
        self.assertIn("WARN    ~/.config/shell/env.sh line 3", out)
        self.assertNotIn("tok-literal-value", out)
        self.assertEqual(self.env_sh.read_text(), BEFORE + own + AFTER)

    def test_a_failed_splice_keeps_env_sh_whole(self):
        # A head that fails on env.sh alone: tail still succeeds after it.
        base.write_exec(self.stubs / "head",
                        '#!/bin/sh\ncase "$*" in *.config/shell/env.sh) exit 1 ;; esac\n'
                        'exec /usr/bin/head "$@"\n')
        self.write(BEFORE + OLD + AFTER)
        with self.assertRaises(AssertionError) as failed:   # the run exits 1
            self.run_tooling("--apply")
        self.assertIn("FAILED  claude_token_block_write", str(failed.exception))
        self.assertEqual(self.env_sh.read_text(), BEFORE + OLD + AFTER)
        self.assertEqual(sorted(p.name for p in self.env_sh.parent.iterdir()), ["env.sh"])

    def test_the_old_line_first_in_env_sh_is_migrated(self):
        self.write(OLD + AFTER)
        self.run_tooling("--apply")
        text = self.env_sh.read_text()
        self.assertTrue(text.startswith(START + "\n"), text)
        self.assertTrue(text.endswith(END + "\n" + AFTER), text)
        self.assertNotIn(OLD, text)

    def test_a_symlinked_env_sh_is_migrated_in_its_target(self):
        target = self.home / "dotfiles/env.sh"
        target.parent.mkdir()
        target.write_text(BEFORE + OLD + AFTER)
        target.chmod(0o600)
        self.env_sh.symlink_to("../../dotfiles/env.sh")
        self.run_tooling("--apply")
        self.assertTrue(self.env_sh.is_symlink())
        text = target.read_text()
        self.assertIn(START, text)
        self.assertNotIn(OLD, text)
        self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ["env.sh"])

    def test_the_block_loads_the_item_of_the_email_and_organization(self):
        # Two accounts with one email in two organizations hold two items,
        # told apart by the account field; each loads its own.
        import subprocess
        self.write(BEFORE + OLD + AFTER)
        self.run_tooling("--apply")
        security = self.stubs / "security-fake"
        base.write_exec(security / "security",
                        '#!/bin/sh\n'
                        'case "$*" in\n'
                        '  "find-generic-password -s claude-code-oauth-token-a@example.test -a org-1 -w") echo tok-1 ;;\n'
                        '  "find-generic-password -s claude-code-oauth-token-a@example.test -a org-2 -w") echo tok-2 ;;\n'
                        '  "find-generic-password -s claude-code-oauth-token-a@example.test -w") echo tok-first ;;\n'
                        '  "find-generic-password -s claude-code-oauth-token -w") echo tok-plain ;;\n'
                        '  *) exit 44 ;;\n'
                        'esac\n')
        for org, want in (("org-1", "tok-1"), ("org-2", "tok-2"), ("org-3", "tok-plain")):
            (self.home / ".claude.json").write_text(
                '{"oauthAccount": {"emailAddress": "a@example.test", "organizationUuid": "%s"}}' % org)
            run = subprocess.run(["/bin/sh", "-e", "-c", f'. "{self.env_sh}"; echo "$CUSTOM_CLAUDE_CODE_OAUTH_TOKEN"'],
                                 env={"HOME": str(self.home), "PATH": f"{security}:/usr/bin:/bin"},
                                 capture_output=True, text=True, check=False)
            self.assertEqual((run.returncode, run.stdout), (0, want + "\n"), f"{org}: {run.stderr}")

    def test_a_traced_shell_sourcing_the_block_prints_no_key(self):
        # The Bash profile and zsh source env.sh directly, past
        # st_shell_env: the block itself turns xtrace off and back on.
        import shutil
        import subprocess
        import sys
        # zsh is the macOS login shell; a Linux runner may lack it.
        zsh = shutil.which("zsh")
        if sys.platform == "darwin":
            self.assertIsNotNone(zsh, "macOS ships zsh")
        shells = ["/bin/bash", "/bin/sh"] + ([zsh] if zsh else [])
        self.write(BEFORE + OLD + AFTER)
        self.run_tooling("--apply")
        security = self.stubs / "security-fake"
        base.write_exec(security / "security",
                        '#!/bin/sh\n[ "$5" = org-a ] && echo synthetic-key && exit 0\n'
                        '[ "$3" = claude-code-oauth-token ] && echo synthetic-plain && exit 0\nexit 44\n')
        accounts = (
            ('{"oauthAccount": {"emailAddress": "a@example.test", "organizationUuid": "org-a"}}',
             "synthetic-key"),
            ('{"other": 1}', "synthetic-plain"))
        for claude_json, want in accounts:
            (self.home / ".claude.json").write_text(claude_json)
            for shell in shells:
                run = subprocess.run(
                    [shell, "-x", "-c",
                     f'. "{self.env_sh}"; t=$-; set +x; '
                     'case $t in *x*) t=on;; *) t=off;; esac; '
                     'echo "$t $CUSTOM_CLAUDE_CODE_OAUTH_TOKEN"'],
                    env={"HOME": str(self.home), "PATH": f"{security}:/usr/bin:/bin"},
                    capture_output=True, text=True, check=False)
                self.assertEqual(run.stdout, f"on {want}\n", f"{shell}: {run.stderr}")
                self.assertNotIn("synthetic", run.stderr, f"{shell} traced the key")

    def test_the_block_reads_email_and_organization_from_one_snapshot(self):
        # cswap may swap ~/.claude.json between two reads; a mixed pair
        # names no item and falls back to the plain key. A FIFO serves
        # account A to the first open and account B to any second one.
        import os
        import subprocess
        import threading
        self.write(BEFORE + OLD + AFTER)
        self.run_tooling("--apply")
        security = self.stubs / "security-fake"
        base.write_exec(security / "security",
                        '#!/bin/sh\n'
                        'case "$*" in\n'
                        '  "find-generic-password -s claude-code-oauth-token-a@example.test -a org-a -w") echo tok-a ;;\n'
                        '  "find-generic-password -s claude-code-oauth-token-b@example.test -a org-b -w") echo tok-b ;;\n'
                        '  "find-generic-password -s claude-code-oauth-token -w") echo tok-plain ;;\n'
                        '  *) exit 44 ;;\n'
                        'esac\n')
        # A slow cat holds the FIFO open past A's close, as a loaded
        # machine does; the writer must still give it A alone.
        base.write_exec(security / "cat",
                        '#!/bin/sh\n[ $# -eq 1 ] || exec /bin/cat "$@"\n'
                        'exec 3<"$1"; sleep 0.3; exec /bin/cat <&3\n')
        fifo = self.home / ".claude.json"
        os.mkfifo(fifo)
        done = threading.Event()

        def serve():
            # A second open must be a new reader: B waits until the first
            # reader is gone (ENXIO), or B lands in A's read and the pair
            # is no JSON (sd:9652 gate flake under load).
            reader_gone = True
            for who in ("a", "b"):
                while not done.is_set():
                    try:
                        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                    except OSError:
                        reader_gone = True
                        done.wait(0.01)
                        continue
                    if reader_gone:
                        break
                    os.close(fd)
                    done.wait(0.01)
                else:
                    return
                reader_gone = False
                os.set_blocking(fd, True)
                os.write(fd, ('{"oauthAccount": {"emailAddress": "%s@example.test", '
                              '"organizationUuid": "org-%s"}}' % (who, who)).encode())
                os.close(fd)

        writer = threading.Thread(target=serve, daemon=True)
        writer.start()
        try:
            run = subprocess.run(["/bin/sh", "-e", "-c", f'. "{self.env_sh}"; echo "$CUSTOM_CLAUDE_CODE_OAUTH_TOKEN"'],
                                 env={"HOME": str(self.home), "PATH": f"{security}:/usr/bin:/bin"},
                                 capture_output=True, text=True, check=False, timeout=30)
        finally:
            done.set()
            writer.join(5)
        self.assertEqual((run.returncode, run.stdout), (0, "tok-a\n"), run.stderr)
