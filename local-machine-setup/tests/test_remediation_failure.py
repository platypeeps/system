"""One failing remediation is reported and the run goes on (sd:3543).

do_stages runs each stage bare under `set -e`, and the stage loops are
pipelines. A bare `run` whose command failed ended the loop, the stage and the
run: one dead formula stopped `setup --apply` before every later formula,
cask and stage. `run` now prints a FAILED line, the run goes on, and the run
ends with a summary of what failed and exit 1, so a nightly caller still sees
it. Stubs on PATH stand in for brew and launchctl; nothing real runs.
"""

import hashlib
import os
import pathlib
import pty
import select
import shutil
import stat
import time
import subprocess
import tempfile
import unittest

from tests import fixture_config

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent

# Installs nothing; `install bad-formula` fails, every call is logged.
BREW_STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$BREW_LOG"
case "$*" in
  "install bad-formula") echo "Error: bad-formula: download failed" >&2; exit 1 ;;
esac
exit 0
"""

# Nothing is loaded; bootout fails as it does for a label launchd does not
# hold, and bootstrap fails for the agent named in $BOOTSTRAP_FAILS.
LAUNCHCTL_STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$LAUNCHCTL_LOG"
case "$1" in
  print|bootout) exit 1 ;;
  bootstrap) case "$3" in *"/$BOOTSTRAP_FAILS.plist") exit 5 ;; esac ;;
esac
exit 0
"""


# A stand-in for a real tool that fails when its last argument is $FAIL_TARGET
# (cp, mkdir) or names it, and runs the real tool otherwise.
FAILING_STUB = r"""#!/bin/sh
for last; do :; done
case "$last" in "$FAIL_TARGET") echo "{name}: $last: refused" >&2; exit 1 ;; esac
exec {real} "$@"
"""


def failing(name):
    return FAILING_STUB.replace("{name}", name).replace("{real}", shutil.which(name))


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def write_exec(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class RemediationFailure(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)
        self.config = fixture_config.copy_config(self.base)
        self.profiles = self.config / "machine-setup/profiles"
        self.home = self.base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")
        self.stubs = self.base / "stubs"
        self.stubs.mkdir()
        self.brew_log = self.base / "brew.log"
        self.launchctl_log = self.base / "launchctl.log"
        write_exec(self.stubs / "brew", BREW_STUB)
        write_exec(self.stubs / "launchctl", LAUNCHCTL_STUB)
        fixture_config.seal(self, self.stubs)

    def run_verb(self, *args, **extra):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(self.config),
            "BREW_LOG": str(self.brew_log),
            "LAUNCHCTL_LOG": str(self.launchctl_log),
            **extra,
        }
        return subprocess.run([str(FOLDER / "machine-setup.sh"), *args], env=env,
                              capture_output=True, text=True, cwd=self.tmp.name,
                              stdin=subprocess.DEVNULL, timeout=120)

    def log(self, path):
        return path.read_text().splitlines() if path.exists() else []

    def test_a_failed_formula_does_not_stop_the_formulae_and_casks_after_it(self):
        (self.profiles / "personal.brew").write_text("bad-formula\ngood-formula\n")
        (self.profiles / "personal.cask").write_text("good-cask\n")
        result = self.run_verb("update", "brew", "--apply")
        out = result.stdout
        self.assertIn("install good-formula", self.log(self.brew_log), out + result.stderr)
        self.assertIn("install --cask --adopt good-cask", self.log(self.brew_log))
        self.assertIn("  FAILED  brew install bad-formula (exit 1)\n", out)
        # The summary prints after the stage loop, so the loop went on.
        self.assertIn("failed — 1 remediation(s) did not finish; fix them, then re-run:\n"
                      "    brew install bad-formula\n", out)
        self.assertEqual(result.returncode, 1, out + result.stderr)

    def test_a_run_with_no_failure_prints_no_summary_and_exits_0(self):
        (self.profiles / "personal.brew").write_text("good-formula\n")
        result = self.run_verb("update", "brew", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("FAILED", result.stdout)
        self.assertNotIn("did not finish", result.stdout)

    def test_a_refused_bootstrap_is_reported_and_the_next_agent_still_loads(self):
        labels = [line.strip() for line in (self.profiles / "personal.agent").read_text().splitlines()
                  if line.strip() and not line.startswith("#")]
        self.assertGreaterEqual(len(labels), 2)
        (self.home / "Library/LaunchAgents").mkdir(parents=True)
        result = self.run_verb("update", "agents", "--apply", BOOTSTRAP_FAILS=labels[0])
        out = result.stdout
        bootstraps = [line for line in self.log(self.launchctl_log) if line.startswith("bootstrap ")]
        self.assertTrue(any(line.endswith(f"/{labels[1]}.plist") for line in bootstraps), out + result.stderr)
        self.assertIn("  FAILED  launchctl bootstrap gui/", out)
        self.assertIn(f"/{labels[0]}.plist (exit 5)\n", out)
        self.assertEqual(result.returncode, 1, out + result.stderr)

    def test_a_bootout_of_a_label_launchd_does_not_hold_is_not_a_failure(self):
        # STALE: the installed plist is the recorded one, the repo moved. The
        # stage boots the old job out before it bootstraps the new file, and
        # bootout fails when launchd holds nothing under that label.
        label, _ = self.stale_agent()
        result = self.run_verb("update", "agents", "--apply")
        self.assertIn(f"  STALE   {label}", result.stdout, result.stdout + result.stderr)
        self.assertIn("bootout gui/", "\n".join(self.log(self.launchctl_log)))
        self.assertNotIn("FAILED", result.stdout)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def stale_agent(self):
        """An installed plist this run sees as STALE: recorded, and the repo moved."""
        label = "local.system-tools.sd-serve"
        (self.profiles / "personal.agent").write_text(f"{label}\n")
        plist = self.home / "Library/LaunchAgents" / f"{label}.plist"
        plist.parent.mkdir(parents=True)
        plist.write_text("<plist>older</plist>\n")
        installed = self.state / "installed"
        installed.mkdir()
        digest = hashlib.sha256(b"<plist>older</plist>\n").hexdigest()
        (installed / f"agents_{label}.sha").write_text(digest + "\n")
        return label, plist

    def test_a_failed_backup_skips_the_overwrite_it_guards(self):
        # Under set -e a failed backup ended the run before the overwrite.
        # run now goes on, so the backup's failure must still stop the copy.
        label, plist = self.stale_agent()
        write_exec(self.stubs / "cp", '#!/bin/sh\ncase "$*" in *.bak-*) exit 1 ;; esac\nexec /bin/cp "$@"\n')
        result = self.run_verb("update", "agents", "--apply")
        out = result.stdout
        self.assertIn(f"  FAILED  backup_file {plist} (exit 1)\n", out, out + result.stderr)
        self.assertEqual(plist.read_text(), "<plist>older</plist>\n")
        self.assertFalse([line for line in self.log(self.launchctl_log) if line.startswith("bootstrap ")])
        self.assertEqual(result.returncode, 1)

    def test_a_failed_env_append_is_not_recorded_so_the_next_run_retries(self):
        template = self.config / "machine-setup/envs/personal/local-notify.env"
        template.parent.mkdir(parents=True)
        template.write_text("A=1\nB=2\n")
        live = self.config / "notify/.env"
        live.parent.mkdir(parents=True)
        live.write_text("A=1\n")
        record = self.state / "installed/envs_local-notify.sha"
        record.parent.mkdir(parents=True)
        older = hashlib.sha256(b"A=1\n").hexdigest()
        record.write_text(older + "\n")
        live.chmod(0o400)
        first = self.run_verb("update", "envs", "--apply")
        self.assertIn(f"  FAILED  append B to {live} (exit ", first.stdout, first.stdout + first.stderr)
        self.assertEqual(first.returncode, 1)
        self.assertEqual(record.read_text().strip(), older)
        live.chmod(0o600)
        # The first run's backup kept the 0400 mode and, inside one second,
        # its name: the retry would fail to overwrite it.
        for backup in live.parent.glob(".env.bak-*"):
            backup.unlink()
        second = self.run_verb("update", "envs", "--apply")
        self.assertIn("appended from the template", second.stdout, second.stdout + second.stderr)
        self.assertEqual(live.read_text(), "A=1\nB=2\n")
        self.assertEqual(record.read_text().strip(), hashlib.sha256(b"A=1\nB=2\n").hexdigest())
        self.assertEqual(second.returncode, 0)

    # ------------------------------------------------- dependent steps ----
    # A step that later steps depend on stops them when it fails: the next
    # independent item still runs. Each case fails one step and checks that
    # nothing after it in the same sequence ran or was recorded.

    def test_a_failed_agent_copy_records_nothing_and_loads_nothing(self):
        # No LaunchAgents folder, so the copy of a MISSING agent fails.
        result = self.run_verb("update", "agents", "--apply")
        out = result.stdout
        self.assertIn("  FAILED  cp ", out, out + result.stderr)
        self.assertEqual([line for line in self.log(self.launchctl_log) if line.startswith("bootstrap ")], [], out)
        self.assertFalse((self.state / "installed").exists())
        self.assertEqual(result.returncode, 1)

    # A cp that fails part-way: a full disk leaves a part file behind.
    PART_COPY_STUB = r"""#!/bin/sh
for last; do :; done
case "$last" in "$FAIL_TARGET"|"$FAIL_TARGET.new") printf '<plist>trunc' > "$last"; exit 1 ;; esac
exec /bin/cp "$@"
"""

    def agent_branch(self, branch):
        """An installed agent that reaches one of the three overwrite branches."""
        label, plist = self.stale_agent()
        record = self.state / f"installed/agents_{label}.sha"
        if branch == "edited":
            record.write_text(sha("<plist>as installed</plist>\n") + "\n")
        elif branch == "unrecorded":
            record.unlink()
        return label, plist, record, ("--force",) if branch != "stale" else ()

    def assert_part_copy_leaves_the_job(self, branch, word, says):
        # The job stays loaded on its old plist: no bootout, no bootstrap,
        # and no part file in place of the plist.
        label, plist, record, extra = self.agent_branch(branch)
        before = record.read_text() if record.exists() else None
        write_exec(self.stubs / "cp", self.PART_COPY_STUB)
        result = self.run_verb("update", "agents", "--apply", *extra, FAIL_TARGET=str(plist))
        out = result.stdout
        self.assertIn(f"  {word} {label} — {says}", out, out + result.stderr)
        self.assertIn("  FAILED  cp ", out)
        self.assertEqual(plist.read_text(), "<plist>older</plist>\n", out)
        self.assertEqual(list(plist.parent.glob("*.new")), [])
        self.assertEqual(record.read_text() if record.exists() else None, before)
        self.assertEqual([line for line in self.log(self.launchctl_log)
                          if line.startswith(("bootout ", "bootstrap "))], [], out)
        self.assertEqual(result.returncode, 1)

    def test_a_part_copy_over_a_stale_agent_leaves_the_job_on_its_old_plist(self):
        self.assert_part_copy_leaves_the_job("stale", "STALE  ", "untouched here")

    def test_a_part_copy_over_an_edited_agent_leaves_the_job_on_its_old_plist(self):
        self.assert_part_copy_leaves_the_job("edited", "DIFFERS", "edited here; --force")

    def test_a_part_copy_over_an_unrecorded_agent_leaves_the_job_on_its_old_plist(self):
        self.assert_part_copy_leaves_the_job("unrecorded", "DIFFERS", "nothing recorded; --force")

    def test_a_failed_rename_loads_the_old_plist_back(self):
        # The job is out by then. A rename leaves no part file, so the plist
        # in place is the old one, whole, and it goes back in.
        label, plist, record, _ = self.agent_branch("stale")
        before = record.read_text()
        write_exec(self.stubs / "mv", failing("mv"))
        result = self.run_verb("update", "agents", "--apply", FAIL_TARGET=str(plist))
        out = result.stdout
        self.assertIn(f"  FAILED  mv {plist}.new {plist} (exit 1)\n", out, out + result.stderr)
        self.assertEqual(plist.read_text(), "<plist>older</plist>\n")
        self.assertEqual(list(plist.parent.glob("*.new")), [])
        self.assertEqual(record.read_text(), before)
        self.assertEqual([line for line in self.log(self.launchctl_log)
                          if line.startswith(("bootout ", "bootstrap "))],
                         [f"bootout gui/{os.getuid()}/{label}", f"bootstrap gui/{os.getuid()} {plist}"], out)
        self.assertEqual(result.returncode, 1)

    def test_a_part_copy_of_a_missing_agent_leaves_no_plist(self):
        # A part file would read as a local edit on the next run, which then
        # refuses to overwrite it without --force.
        label = "local.system-tools.sd-serve"
        (self.profiles / "personal.agent").write_text(f"{label}\n")
        plist = self.home / "Library/LaunchAgents" / f"{label}.plist"
        plist.parent.mkdir(parents=True)
        write_exec(self.stubs / "cp", self.PART_COPY_STUB)
        result = self.run_verb("update", "agents", "--apply", FAIL_TARGET=str(plist))
        out = result.stdout
        self.assertIn("  FAILED  cp ", out, out + result.stderr)
        self.assertEqual(list(plist.parent.iterdir()), [])
        self.assertEqual([line for line in self.log(self.launchctl_log) if line.startswith("bootstrap ")], [], out)
        self.assertEqual(result.returncode, 1)

    def test_a_stale_agent_whose_old_job_will_not_boot_out_is_not_bootstrapped_over(self):
        # The copy waits for the bootout: a new plist under the old loaded job
        # would read "ok" on the next run while launchd runs the old one.
        label, plist = self.stale_agent()
        record = self.state / f"installed/agents_{label}.sha"
        before = record.read_text()
        write_exec(self.stubs / "launchctl", '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$LAUNCHCTL_LOG"\n'
                   'case "$1" in bootout) exit 5 ;; esac\nexit 0\n')
        result = self.run_verb("update", "agents", "--apply")
        out = result.stdout
        self.assertEqual(plist.read_text(), "<plist>older</plist>\n", out)
        self.assertEqual(record.read_text(), before)
        self.assertIn(f"  FAILED  launchctl bootout gui/", out, out + result.stderr)
        self.assertIn(f"/{label} (exit 5)\n", out)
        self.assertEqual([line for line in self.log(self.launchctl_log) if line.startswith("bootstrap ")], [], out)
        self.assertEqual(result.returncode, 1)

    def dotfile(self, name, text):
        src = self.config / "machine-setup/dotfiles/common" / name
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(text)
        return self.home / name

    def test_a_failed_forced_dotfile_copy_is_not_recorded(self):
        # Recording the local edit as installed would let the next plain
        # update read it STALE and overwrite it without --force.
        dst = self.dotfile(".gitconfig", "[user]\n\tname = Example\n")
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("[user]\n\tname = Edited here\n")
        write_exec(self.stubs / "cp", failing("cp"))
        result = self.run_verb("update", "dotfiles", "--apply", "--force", FAIL_TARGET=str(dst))
        out = result.stdout
        self.assertIn(f"  FAILED  cp ", out, out + result.stderr)
        self.assertEqual(dst.read_text(), "[user]\n\tname = Edited here\n")
        self.assertFalse((self.state / "installed/dotfiles_.gitconfig.sha").exists(), out)
        self.assertEqual(result.returncode, 1)

    def test_a_failed_dotfile_folder_skips_the_copy(self):
        dst = self.dotfile(".config/gh/config.yml", "git_protocol: ssh\n")
        write_exec(self.stubs / "mkdir", failing("mkdir"))
        result = self.run_verb("update", "dotfiles", "--apply", FAIL_TARGET=str(dst.parent))
        out = result.stdout
        self.assertIn(f"  FAILED  mkdir -p {dst.parent} (exit 1)\n", out, out + result.stderr)
        self.assertNotIn("FAILED  cp", out)
        self.assertFalse(dst.exists())
        self.assertEqual(result.returncode, 1)

    def test_a_dotfile_merge_whose_awk_fails_leaves_the_dotfile(self):
        # .aws/config keeps this machine's login_session lines through
        # keep_sessions_cp: awk writes a merge and mv puts it in place. A
        # failed awk must not move a half-written merge over the file.
        dst = self.home / ".aws/config"
        dst.parent.mkdir(parents=True)
        dst.write_text("[profile old]\nregion = us-east-1\n")
        installed = self.state / "installed"
        installed.mkdir()
        (installed / "dotfiles_.aws_config.sha").write_text(sha(dst.read_text()) + "\n")
        write_exec(self.stubs / "awk", '#!/bin/sh\ncase "$*" in *"-v re="*) exit 2 ;; esac\n'
                   f'exec {shutil.which("awk")} "$@"\n')
        result = self.run_verb("update", "dotfiles", "--apply")
        out = result.stdout
        self.assertIn("  STALE   .aws/config", out, out + result.stderr)
        self.assertIn("  FAILED  keep_sessions_cp .aws/config ", out)
        self.assertEqual(dst.read_text(), "[profile old]\nregion = us-east-1\n")
        self.assertEqual(list(dst.parent.glob("config.merge.*")), [])
        self.assertEqual(result.returncode, 1)

    def test_a_failed_env_seed_records_no_template(self):
        template = self.config / "machine-setup/envs/personal/local-notify.env"
        template.parent.mkdir(parents=True)
        template.write_text("A=1\n")
        live = self.config / "notify/.env"
        live.parent.mkdir(parents=True)
        live.parent.chmod(0o500)
        self.addCleanup(live.parent.chmod, 0o700)
        result = self.run_verb("update", "envs", "--apply")
        out = result.stdout
        self.assertIn(f"  MISSING {live}", out, out + result.stderr)
        self.assertIn("  FAILED  ", out)
        self.assertFalse(live.exists())
        self.assertFalse((self.state / "installed/envs_local-notify.sha").exists(), out)
        self.assertEqual(result.returncode, 1)

    def test_a_failed_template_read_is_not_an_append(self):
        # sed piped into head reported head's status: a failed read of the
        # template appended nothing and still recorded the template.
        template = self.config / "machine-setup/envs/personal/local-notify.env"
        template.parent.mkdir(parents=True)
        template.write_text("A=1\nB=2\n")
        live = self.config / "notify/.env"
        live.parent.mkdir(parents=True)
        live.write_text("A=1\n")
        record = self.state / "installed/envs_local-notify.sha"
        record.parent.mkdir(parents=True)
        older = sha("A=1\n")
        record.write_text(older + "\n")
        write_exec(self.stubs / "sed", failing("sed"))
        result = self.run_verb("update", "envs", "--apply", FAIL_TARGET=str(template))
        out = result.stdout
        self.assertIn(f"  FAILED  append B to {live} (exit 1)\n", out, out + result.stderr)
        self.assertEqual(record.read_text().strip(), older)
        self.assertEqual(result.returncode, 1)

    def test_a_record_that_cannot_be_written_is_reported_and_the_run_goes_on(self):
        # A file where the record folder belongs: mkdir fails. Under set -e
        # that ended the run with no FAILED line.
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "installed").write_text("")
        first = self.dotfile(".gitconfig", "[user]\n\tname = Example\n")
        second = self.dotfile(".config/gh/config.yml", "git_protocol: ssh\n")
        result = self.run_verb("update", "dotfiles", "--apply")
        out = result.stdout
        self.assertIn("  FAILED  record_hash dotfiles/.gitconfig (exit 1)\n", out, out + result.stderr)
        self.assertIn("  FAILED  record_hash dotfiles/.config/gh/config.yml (exit 1)\n", out)
        self.assertTrue(first.exists() and second.exists(), out)
        self.assertEqual(result.returncode, 1)

    def claude(self, fail):
        """A `claude` stub that logs each call and fails `plugin $fail`."""
        self.claude_log = self.base / "claude.log"
        write_exec(self.stubs / "claude", '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$CLAUDE_LOG"\n'
                   f'case "$*" in "plugin {fail}"*) exit 1 ;; esac\nexit 0\n')

    def test_a_failed_marketplace_add_skips_the_plugin_install(self):
        self.claude("marketplace add")
        result = self.run_verb("update", "tooling", "--apply", CLAUDE_LOG=str(self.base / "claude.log"))
        out = result.stdout
        self.assertIn("  FAILED  claude plugin marketplace add jarrodwatts/claude-hud (exit 1)\n", out,
                      out + result.stderr)
        self.assertNotIn("plugin install claude-hud@claude-hud", self.log(self.claude_log))
        self.assertEqual(result.returncode, 1)

    def test_a_failed_hud_config_folder_skips_the_seeded_config(self):
        self.claude("none")
        folder = self.home / ".claude/plugins/claude-hud"
        write_exec(self.stubs / "mkdir", failing("mkdir"))
        result = self.run_verb("update", "tooling", "--apply", CLAUDE_LOG=str(self.base / "claude.log"),
                               FAIL_TARGET=str(folder))
        out = result.stdout
        self.assertIn(f"  FAILED  mkdir -p {folder} (exit 1)\n", out, out + result.stderr)
        self.assertNotIn("FAILED  cp", out)
        self.assertEqual(result.returncode, 1)

    def test_a_failed_settings_backup_skips_the_baseline_merge(self):
        settings = self.home / ".claude/settings.json"
        settings.parent.mkdir(parents=True)
        # statusLine already set, so the statusline step leaves the file alone.
        current = '{"statusLine": {"command": "sh local-statusline/statusline.sh render"}}\n'
        settings.write_text(current)
        self.claude("none")
        # The plugin and its config are present, so only the baseline is due.
        (self.home / ".claude/plugins/cache/x/claude-hud/1.0").mkdir(parents=True)
        (self.home / ".claude/plugins/claude-hud").mkdir(parents=True)
        (self.home / ".claude/plugins/claude-hud/config.json").write_text("{}\n")
        write_exec(self.stubs / "cp", '#!/bin/sh\ncase "$*" in *.bak-*) exit 1 ;; esac\n'
                   f'exec {shutil.which("cp")} "$@"\n')
        result = self.run_verb("update", "tooling", "--apply", CLAUDE_LOG=str(self.base / "claude.log"))
        out = result.stdout
        self.assertIn(f"  FAILED  backup_file {settings} (exit 1)\n", out, out + result.stderr)
        self.assertEqual(settings.read_text(), current)
        self.assertEqual(result.returncode, 1)

    # ------------------------------------------------------ decommission ----

    def decommission(self, still_loaded):
        """`decommission --apply` on a terminal, typing the confirmation."""
        label = "local.system-tools.sd-serve"
        (self.profiles / "personal.agent").write_text(f"{label}\n")
        plist = self.home / "Library/LaunchAgents" / f"{label}.plist"
        plist.parent.mkdir(parents=True)
        plist.write_text("<plist/>\n")
        # bootout fails; print answers whether launchd still holds the job.
        write_exec(self.stubs / "launchctl", '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$LAUNCHCTL_LOG"\n'
                   'case "$1" in bootout) exit 5 ;; print) exit "$PRINT_RC" ;; esac\nexit 0\n')
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(self.config),
            "LAUNCHCTL_LOG": str(self.launchctl_log),
            "PRINT_RC": "0" if still_loaded else "113",
        }
        # The folder alone, so cron-jobs.sh is missing and its step fails:
        # nothing real is uninstalled.
        folder = self.base / "repo/local-machine-setup"
        shutil.copytree(FOLDER, folder, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copytree(FOLDER.parent / "lib", self.base / "repo/lib",
                        ignore=shutil.ignore_patterns("tests", "__pycache__"))
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(self.tmp.name)
            os.execve(str(folder / "machine-setup.sh"), [str(folder / "machine-setup.sh"),
                                                          "decommission", "--apply"], env)
        out = b""
        sent = False
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 1)
            if not ready:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            if not sent and b'type "decommission"' in out:
                os.write(fd, b"decommission\n")
                sent = True
        _, status = os.waitpid(pid, 0)
        return plist, out.decode(errors="replace").replace("\r\n", "\n"), os.waitstatus_to_exitcode(status)

    def test_decommission_keeps_the_plist_of_an_agent_still_loaded(self):
        plist, out, rc = self.decommission(still_loaded=True)
        self.assertTrue(plist.exists(), out)
        self.assertIn("  FAILED  launchctl bootout gui/", out)
        self.assertIn(f"  KEPT    {plist}", out)
        self.assertEqual(rc, 1, out)

    def test_decommission_removes_the_plist_once_launchd_holds_no_job(self):
        plist, out, rc = self.decommission(still_loaded=False)
        self.assertFalse(plist.exists(), out)
        self.assertNotIn("FAILED  launchctl bootout", out)


if __name__ == "__main__":
    unittest.main()
