"""The tooling stage keeps env.sh's per-account claude token block (sd:9905).

Headless claude reads CUSTOM_CLAUDE_CODE_OAUTH_TOKEN from env.sh. The old
one-line export read one Keychain item for every account; the block reads the
item of the account cswap made active, the plain item its fallback. The stage
turns the old line into the block between marker comments, once, and changes
no other line. A hub-style block written by hand is left alone. A line with a
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
        (self.home / ".claude.json").write_text('{"oauthAccount": {"emailAddress": "a@example.test"}}')
        security = self.stubs / "security-fake"
        base.write_exec(security / "security",
                        '#!/bin/sh\n[ "$3" = claude-code-oauth-token-a@example.test ] && echo tok-a && exit 0\n'
                        '[ "$3" = claude-code-oauth-token ] && echo tok-plain && exit 0\nexit 44\n')
        run = subprocess.run(["/bin/sh", "-c", f'. "{self.env_sh}"; echo "$CUSTOM_CLAUDE_CODE_OAUTH_TOKEN"'],
                             env={"HOME": str(self.home), "PATH": f"{security}:/usr/bin:/bin"},
                             capture_output=True, text=True, check=False)
        self.assertEqual(run.stdout, "tok-a\n", run.stderr)
        (self.home / ".claude.json").write_text('{"oauthAccount": {"emailAddress": "b@example.test"}}')
        run = subprocess.run(["/bin/sh", "-c", f'. "{self.env_sh}"; echo "$CUSTOM_CLAUDE_CODE_OAUTH_TOKEN"'],
                             env={"HOME": str(self.home), "PATH": f"{security}:/usr/bin:/bin"},
                             capture_output=True, text=True, check=False)
        self.assertEqual(run.stdout, "tok-plain\n", run.stderr)

    def test_a_hub_style_block_is_ok_and_untouched(self):
        hub = ('_cc_acct="$(/usr/bin/plutil -extract oauthAccount.emailAddress raw -o - '
               '"$HOME/.claude.json" 2>/dev/null)"\n'
               'export CUSTOM_CLAUDE_CODE_OAUTH_TOKEN="$( { [ -n "$_cc_acct" ] && security '
               'find-generic-password -s "claude-code-oauth-token-$_cc_acct" -w 2>/dev/null; } '
               '|| security find-generic-password -s claude-code-oauth-token -w 2>/dev/null)"\n'
               'unset _cc_acct\n')
        self.write(BEFORE + hub + AFTER)
        out = self.run_tooling("--apply")
        self.assertIn("ok      ~/.config/shell/env.sh reads the claude token per account", out)
        self.assertEqual(self.env_sh.read_text(), BEFORE + hub + AFTER)

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
