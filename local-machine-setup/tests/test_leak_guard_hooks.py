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

import pathlib
import shutil
import subprocess

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
        subprocess.run(["git", "init", "-q", str(path)], check=True, stdin=subprocess.DEVNULL)
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

    def env(self):
        return {"HOME": str(self.home), "MACHINE_SETUP_STATE": str(self.state),
                "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
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
        # The foreign hook stays drift until the operator chains the guard.
        self.assertIn("DIFFERS ~/repos/hooked", after)

    def test_a_foreign_hook_that_chains_the_guard_is_ok(self):
        self.write_hook(self.foreign, FOREIGN + f"sh '{self.script}' hook \"$@\" || exit 1\n")

        out = self.run_repos("--apply")

        self.assertIn("ok      ~/repos/hooked leak guard, chained in its own pre-push hook", out)
        self.assertNotIn("DIFFERS ~/repos/hooked", out)

    def test_a_guard_from_another_checkout_or_not_executable_is_stale_and_rewritten(self):
        self.write_hook(self.private, f"#!/bin/sh\n{MARKER}\nexec sh '/gone/leak-guard.sh' hook \"$@\"\n")
        subprocess.run(["sh", str(self.script), "install", str(self.public)], check=True,
                       capture_output=True, stdin=subprocess.DEVNULL)
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

    def test_a_failed_repo_list_is_a_skip(self):
        base.write_exec(self.pack / "bin/sd-db", "#!/bin/sh\nexit 1\n")

        out = self.run_repos("--apply")

        self.assertIn("SKIP    leak guard hooks: sd-db repo list --managed failed", out)
        self.assertFalse(self.hook(self.public).exists())
