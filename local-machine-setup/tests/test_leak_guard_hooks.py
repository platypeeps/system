"""The repos stage installs the pre-push leak guard in every public managed repo (sd:3384).

A public repository publishes whatever a push carries, so each public managed
checkout on this machine gets local-leak-guard's hook, written by
`leak-guard.sh install`. A private repository is left without it. A pre-push
hook another tool wrote is reported, never replaced. Whether a repository is
public comes from an unauthenticated request for its git refs on GitHub; a
request that gets no answer skips the repository and never fails the run.

`sd-db repo list --managed` is a stub, the pack's `bin/sd-db`; `curl` is a stub
that answers per repository; the repositories are fixtures.
"""

import os
import pathlib
import shutil
import subprocess
from unittest import mock

from tests import fixture_config
from tests import test_claude_settings as base

GUARD = base.FOLDER.parent / "local-leak-guard"
MARKER = "# installed by local-leak-guard"
FOREIGN = "#!/bin/sh\necho another tool's pre-push\n"

# Answers the visibility probe from $CURL_ANSWERS, one "<owner/repo> <code>"
# line each, and logs every call. CURL_OFFLINE answers 000 and exits 7, as
# curl does when it cannot connect.
CURL = r"""#!/bin/sh
printf '%s\n' "$*" >> "$CURL_LOG"
if [ -n "${CURL_OFFLINE:-}" ]; then printf 000; exit 7; fi
for arg; do url=$arg; done
while read -r slug code; do
  case $url in "https://github.com/$slug.git/info/refs?service=git-upload-pack") printf '%s' "$code"; exit 0 ;; esac
done < "$CURL_ANSWERS"
printf 404
"""


class LeakGuardHooks(base.SyntheticHome):
    def setUp(self):
        super().setUp()
        shutil.copytree(GUARD, self.repo / "local-leak-guard",
                        ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.script = self.repo / "local-leak-guard" / "leak-guard.sh"
        self.pack = self.home / "pack"
        self.rows = []
        self.answers = self.home / "curl-answers"
        self.answers.write_text("")
        self.curl_log = self.home / "curl.log"
        base.write_exec(self.stubs / "curl", CURL)
        self.offline = ""
        self.public = self.checkout("open", "git@github.com:example/open.git", 200)
        self.private = self.checkout("closed", "https://github.com/example/closed", 401)
        self.foreign = self.checkout("hooked", "https://github.com/example/hooked.git", 200)
        self.write_hook(self.foreign, FOREIGN)

    def checkout(self, name, url, code):
        path = self.home / "repos" / name
        path.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(path)], check=True, env=self.git_env(),
                       capture_output=True, stdin=subprocess.DEVNULL)
        self.rows.append(f"sd-db: ~/repos/{name}  {url}  yes  local  hub  auto")
        base.write_exec(self.pack / "bin/sd-db",
                        "#!/bin/sh\n[ \"$*\" = 'repo list --managed' ] || exit 9\ncat <<'EOF'\n"
                        + "\n".join(self.rows) + "\nEOF\n")
        if "github.com" in url:
            slug = url.split("github.com")[1][1:].removesuffix(".git")
            with self.answers.open("a") as answers:
                answers.write(f"{slug} {code}\n")
        return path

    def hook(self, repo):
        return repo / ".git/hooks/pre-push"

    def write_hook(self, repo, text):
        base.write_exec(self.hook(repo), text)

    def git_env(self):
        """Every subprocess's base: the synthetic HOME and no git config.

        Built from nothing, so no inherited GIT_* variable reaches git, and an
        operator's global core.hooksPath cannot send a fixture's hook into the
        operator's real hooks folder.
        """
        return {"HOME": str(self.home), "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
                "LANG": "en_US.UTF-8", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

    def install(self, repo):
        subprocess.run(["sh", str(self.script), "install", str(repo)], check=True, env=self.git_env(),
                       capture_output=True, stdin=subprocess.DEVNULL)

    def env(self):
        return {**self.git_env(), "MACHINE_SETUP_STATE": str(self.state),
                **fixture_config.env(), "SYSTEM_TOOLS_LABEL_PREFIX": fixture_config.LABEL_PREFIX,
                "SD_PACK_ROOT": str(self.pack), "CURL_LOG": str(self.curl_log),
                "CURL_ANSWERS": str(self.answers), "CURL_OFFLINE": self.offline}

    def run_verb(self, *args):
        result = subprocess.run([str(self.folder / "machine-setup.sh"), *args],
                                env=self.env(), capture_output=True, text=True, cwd=self.cwd,
                                stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def run_repos(self, *flags):
        return self.run_verb("update", "repos", *flags)

    def probes(self):
        return self.curl_log.read_text().splitlines() if self.curl_log.exists() else []

    def test_a_dry_run_reports_the_public_repos_and_writes_nothing(self):
        out = self.run_repos()

        self.assertIn("MISSING ~/repos/open leak guard pre-push hook", out)
        self.assertIn("DIFFERS ~/repos/hooked has a pre-push hook local-leak-guard did not write", out)
        self.assertNotIn("~/repos/closed", out)
        self.assertFalse(self.hook(self.public).exists())
        self.assertFalse(self.hook(self.private).exists())
        self.assertEqual(self.hook(self.foreign).read_text(), FOREIGN)

    def test_apply_installs_only_in_the_public_repo_and_a_second_run_is_clean(self):
        self.run_repos("--apply")

        text = self.hook(self.public).read_text()
        self.assertIn(MARKER, text)
        self.assertIn(f"'{self.script}' hook", text)
        self.assertFalse(self.hook(self.private).exists())
        self.assertEqual(self.hook(self.foreign).read_text(), FOREIGN)

        probed = len(self.probes())
        again = self.run_repos("--apply")

        self.assertIn("ok      ~/repos/open leak guard", again)
        self.assertNotIn("MISSING", again)
        self.assertEqual(self.hook(self.public).read_text(), text)
        # An installed guard needs no probe: only closed and hooked ask again.
        self.assertEqual(len(self.probes()) - probed, 2, self.probes())

    def test_status_counts_the_missing_guard_and_not_once_it_is_installed(self):
        # status runs every stage; these answer "cannot read" the same way
        # before and after, so only the repos stage moves the count.
        for name in ("sudo", "launchctl", "defaults", "mas"):
            base.write_exec(self.stubs / name, "#!/bin/sh\nexit 1\n")
        before = self.run_verb("status")
        self.run_repos("--apply")
        after = self.run_verb("status")

        drift_before = int(before.split("drift   : ")[1].split()[0])
        drift_after = int(after.split("drift   : ")[1].split()[0])
        self.assertIn("MISSING ~/repos/open leak guard pre-push hook", before)
        self.assertNotIn("~/repos/open leak guard pre-push hook", after)
        self.assertEqual(drift_before - drift_after, 1, before + after)
        # A foreign hook stays drift: the operator reviews it.
        self.assertIn("DIFFERS ~/repos/hooked", after)

    def test_a_foreign_hook_naming_the_guard_is_still_differs(self):
        # Review round 1: text that names the guard does not prove it runs.
        for text in (FOREIGN + f"sh '{self.script}' hook \"$@\" || exit 1\n",
                     FOREIGN + f"sh '{self.script.parent}/not-leak-guard.sh' hook \"$@\"\n"):
            with self.subTest(text=text):
                self.write_hook(self.foreign, text)

                out = self.run_repos("--apply")

                self.assertIn("DIFFERS ~/repos/hooked has a pre-push hook local-leak-guard did not write", out)
                self.assertNotIn("ok      ~/repos/hooked", out)
                self.assertEqual(self.hook(self.foreign).read_text(), text)

    def test_a_non_executable_copy_of_the_guard_is_not_ok(self):
        self.hook(self.foreign).unlink()
        self.install(self.foreign)
        text = self.hook(self.foreign).read_text().replace(MARKER + "\n", "")
        self.hook(self.foreign).write_text(text)
        self.hook(self.foreign).chmod(0o644)

        out = self.run_repos("--apply")

        self.assertIn("DIFFERS ~/repos/hooked", out)
        self.assertNotIn("ok      ~/repos/hooked", out)

    def test_a_commented_out_guard_of_ours_is_stale_and_rewritten(self):
        self.install(self.public)
        installed = self.hook(self.public).read_text()
        self.hook(self.public).write_text(installed.replace("exec sh", "# exec sh"))

        out = self.run_repos("--apply")

        self.assertIn("STALE   ~/repos/open leak guard pre-push hook", out)
        self.assertNotIn("ok      ~/repos/open", out)
        self.assertEqual(self.hook(self.public).read_text(), installed)

    def test_the_operators_global_git_config_is_never_read(self):
        # Review round 1: a fixture subprocess that inherited the operator's
        # global core.hooksPath wrote its hook into the operator's folder.
        sentinel = self.home.parent / "operator-hooks"
        sentinel.mkdir()
        operator = self.home.parent / "operator.gitconfig"
        operator.write_text(f"[core]\n\thooksPath = {sentinel}\n")
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(operator),
                                          "GIT_DIR": str(self.public / ".git")}):
            late = self.checkout("late", "https://github.com/example/late", 200)
            self.install(self.public)
            self.run_repos("--apply")

        self.assertEqual(list(sentinel.iterdir()), [])
        self.assertTrue(self.hook(self.public).exists())
        self.assertTrue(self.hook(late).exists())

    def test_a_guard_from_another_checkout_or_not_executable_is_stale_and_rewritten(self):
        self.write_hook(self.private, f"#!/bin/sh\n{MARKER}\nexec sh '/gone/leak-guard.sh' hook \"$@\"\n")
        self.install(self.public)
        self.hook(self.public).chmod(0o644)
        probed = len(self.probes())

        out = self.run_repos("--apply")

        self.assertIn("STALE   ~/repos/closed leak guard pre-push hook", out)
        self.assertIn("STALE   ~/repos/open leak guard pre-push hook", out)
        for repo in (self.private, self.public):
            self.assertIn(f"'{self.script}' hook", self.hook(repo).read_text())
            self.assertTrue(self.hook(repo).stat().st_mode & 0o100)
        # A stale guard of ours is rewritten without asking GitHub.
        self.assertEqual([p for p in self.probes()[probed:] if "example/open" in p or "example/closed" in p], [])

    def test_an_unanswered_probe_skips_without_writing_and_stops_probing(self):
        self.offline = "1"

        out = self.run_repos("--apply")

        self.assertIn("SKIP    ~/repos/open leak guard: GitHub did not answer", out)
        self.assertNotIn("MISSING", out)
        self.assertFalse(self.hook(self.public).exists())
        self.assertEqual(self.hook(self.foreign).read_text(), FOREIGN)
        self.assertEqual(len(self.probes()), 1, self.probes())

    def test_a_rate_limited_probe_is_unknown_not_private(self):
        self.answers.write_text("example/open 403\nexample/closed 401\nexample/hooked 200\n")

        out = self.run_repos("--apply")

        self.assertIn("SKIP    ~/repos/open leak guard: GitHub answered 403", out)
        self.assertFalse(self.hook(self.public).exists())

    def test_the_probe_sends_no_credentials(self):
        self.run_repos()

        self.assertEqual(len(self.probes()), 3, self.probes())
        for probe in self.probes():
            args = probe.split()
            self.assertEqual(args[0], "-q", probe)
            for flag in ("-u", "--user", "-n", "--netrc", "-H", "--header", "-b", "--cookie"):
                self.assertNotIn(flag, args, probe)

    def test_a_remote_not_on_github_is_a_skip(self):
        elsewhere = self.checkout("elsewhere", "https://example.test/x.git", 0)
        none = self.checkout("none", "-", 0)

        out = self.run_repos("--apply")

        self.assertIn("SKIP    ~/repos/elsewhere leak guard: its remote is not on GitHub", out)
        self.assertIn("SKIP    ~/repos/none leak guard: its remote is not on GitHub", out)
        self.assertFalse(self.hook(elsewhere).exists())
        self.assertFalse(self.hook(none).exists())
        self.assertEqual([p for p in self.probes() if "example.test" in p], [])

    def test_a_failed_install_is_reported_and_the_stage_goes_on(self):
        hooks = self.public / ".git/hooks"
        hooks.chmod(0o555)
        self.addCleanup(hooks.chmod, 0o755)

        out = self.run_repos("--apply")

        self.assertIn("MISSING ~/repos/open leak guard pre-push hook", out)
        self.assertIn("FAILED  ~/repos/open leak guard: leak-guard.sh install did not finish", out)
        self.assertIn("DIFFERS ~/repos/hooked", out)

    def test_a_checkout_git_cannot_read_is_a_skip(self):
        broken = self.checkout("broken", "https://github.com/example/broken", 200)
        shutil.rmtree(broken / ".git")
        (broken / ".git").write_text("gitdir: /nonexistent/example\n")

        out = self.run_repos("--apply")

        self.assertIn("SKIP    ~/repos/broken leak guard: git cannot name its hooks folder", out)
        self.assertIn("MISSING ~/repos/open leak guard pre-push hook", out)

    def linked_worktree(self):
        """Commit the fixture checkout and run machine-setup from a linked worktree of it."""
        for args in (["init", "-q"], ["add", "-A"],
                     ["-c", "user.name=Test", "-c", "user.email=test@example.test",
                      "commit", "-q", "-m", "fixture"],
                     ["worktree", "add", "-q", "--detach", str(self.repo.parent / "linked")]):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, env=self.git_env(),
                           capture_output=True, stdin=subprocess.DEVNULL)
        self.folder = self.repo.parent / "linked" / "local-machine-setup"

    def test_a_linked_worktree_keeps_the_main_checkouts_guard(self):
        # sd:3390: a hook that names a linked worktree fails every push once
        # the worktree is removed, so status and apply name the main checkout.
        self.linked_worktree()
        # Installed from the main checkout, and by an older run from the worktree.
        self.install(self.public)
        installed = self.hook(self.public).read_text()
        worktree_guard = self.repo.parent / "linked" / "local-leak-guard" / "leak-guard.sh"
        self.write_hook(self.private, f"#!/bin/sh\n{MARKER}\nexec sh '{worktree_guard}' hook \"$@\"\n")

        out = self.run_repos("--apply")

        self.assertIn("ok      ~/repos/open leak guard", out)
        self.assertEqual(self.hook(self.public).read_text(), installed)
        self.assertIn("STALE   ~/repos/closed leak guard pre-push hook", out)
        self.assertIn(f"exec sh '{self.repo.resolve()}/local-leak-guard/leak-guard.sh' hook",
                      self.hook(self.private).read_text())
        self.assertIn("ok      ~/repos/closed leak guard", self.run_repos("--apply"))

    def test_a_main_checkout_without_the_guard_is_a_skip(self):
        self.linked_worktree()
        self.script.unlink()

        out = self.run_repos("--apply")

        self.assertIn(f"SKIP    leak guard hooks: {self.repo.resolve()}/local-leak-guard/leak-guard.sh is missing", out)
        self.assertFalse(self.hook(self.public).exists())

    def test_a_checkout_git_cannot_name_is_a_skip_not_its_own_path(self):
        (self.repo / ".git").write_text("gitdir: /nonexistent/example\n")

        out = self.run_repos("--apply")

        self.assertIn("SKIP    leak guard hooks: git cannot name the main checkout", out)
        self.assertFalse(self.hook(self.public).exists())

    def test_a_failed_repo_list_is_a_skip(self):
        base.write_exec(self.pack / "bin/sd-db", "#!/bin/sh\nexit 1\n")

        out = self.run_repos("--apply")

        self.assertIn("SKIP    leak guard hooks: sd-db repo list --managed failed", out)
        self.assertFalse(self.hook(self.public).exists())
