"""The shared config location: lib/config.sh and lib/system_tools_config.py agree."""

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

LIB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LIB))

import system_tools_config as stc  # noqa: E402


def shell(script, env):
    return subprocess.run(["/bin/sh", "-c", f'. "{LIB}/config.sh"; {script}'],
                          env=env, capture_output=True, text=True, check=False)


class Location(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def env(self, **extra):
        return {"PATH": os.environ["PATH"], "HOME": str(self.home), **extra}

    def test_default_is_under_home_config(self):
        env = self.env()
        want = self.home / ".config/system/notify"
        self.assertEqual(stc.config_dir("notify", env), want)
        self.assertEqual(shell("st_config_dir notify", env).stdout.strip(), str(want))

    def test_xdg_config_home_moves_it(self):
        env = self.env(XDG_CONFIG_HOME=str(self.home / "xdg"))
        want = self.home / "xdg/system/repo-sync"
        self.assertEqual(stc.config_dir("repo-sync", env), want)
        self.assertEqual(shell("st_config_dir repo-sync", env).stdout.strip(), str(want))

    def test_system_tools_config_wins(self):
        env = self.env(XDG_CONFIG_HOME=str(self.home / "xdg"), SYSTEM_TOOLS_CONFIG=str(self.home / "c"))
        self.assertEqual(stc.config_dir("weekly-digest", env), self.home / "c/weekly-digest")
        self.assertEqual(shell("st_config_dir weekly-digest", env).stdout.strip(), str(self.home / "c/weekly-digest"))

    def test_config_file_sits_in_the_tool_dir_except_privacy_patterns(self):
        env = self.env(SYSTEM_TOOLS_CONFIG=str(self.home / "c"))
        self.assertEqual(shell("st_config_file notify notify.conf", env).stdout.strip(),
                         str(self.home / "c/notify/notify.conf"))
        self.assertEqual(shell("st_config_file leak-guard privacy-patterns", env).stdout.strip(),
                         str(self.home / "c/privacy-patterns"))


class LabelPrefix(unittest.TestCase):
    """The launchd label prefix default lives in one place per language (sd:3574)."""

    def test_default_and_override_agree_in_shell_and_python(self):
        for extra, want in (({}, "local.system-tools"), ({"SYSTEM_TOOLS_LABEL_PREFIX": "example.test"}, "example.test")):
            env = {"PATH": os.environ["PATH"], "HOME": "/nonexistent", **extra}
            self.assertEqual(shell("st_label_prefix", env).stdout.strip(), want)
            self.assertEqual(stc.label_prefix(env), want)

    def test_no_tracked_script_repeats_the_default(self):
        done = subprocess.run(
            ["git", "grep", "-nE", r"SYSTEM_TOOLS_LABEL_PREFIX(:-|\",[[:space:]]*)\"?local\.system-tools", "--",
             "*.sh", "*.py", ":(exclude)tests", ":(exclude)*/tests/*"],
            cwd=LIB.parent, capture_output=True, text=True)
        hits = sorted({line.split(":")[0] for line in done.stdout.splitlines()})
        # The dashboard package installs into its own venv, with no lib/ beside
        # it, so runtime.py keeps the one literal (see the comment there).
        self.assertEqual(hits, ["lib/config.sh", "local-project-dashboard/sd_dashboard/runtime.py"])


class EnvFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conf = Path(self.tmp.name) / "c"
        (self.conf / "notify").mkdir(parents=True)
        (self.conf / "notify/.env").write_text(
            "# comment\n\nexport A=1\nB='two words'\nC=\"3\"\nnot a line\n")
        self.envv = {"PATH": os.environ["PATH"], "HOME": self.tmp.name, "SYSTEM_TOOLS_CONFIG": str(self.conf)}

    def test_python_reads_it(self):
        self.assertEqual(stc.read_env("notify", self.envv), {"A": "1", "B": "two words", "C": "3"})

    def test_python_expands_home_as_sourcing_does(self):
        # sd:1868: agent-meter read `$HOME/...` verbatim and missed its ledger.
        (self.conf / "home").mkdir(parents=True)
        (self.conf / "home/.env").write_text(
            "D=$HOME/d\nB=${HOME}/b\nT=~/t\nQ=\"$HOME/q\"\nS='$HOME/s'\nM=a$HOMEX\n")
        home = self.envv["HOME"]
        done = shell('st_source_env home; sh -c \'printf "%s|%s|%s|%s|%s" "$D" "$B" "$T" "$Q" "$S"\'', self.envv)
        self.assertEqual(done.stdout, f"{home}/d|{home}/b|{home}/t|{home}/q|$HOME/s")
        expected = {"D": f"{home}/d", "B": f"{home}/b", "T": f"{home}/t", "Q": f"{home}/q",
                    "S": "$HOME/s", "M": "a$HOMEX"}
        self.assertEqual(stc.read_env("home", self.envv), expected)
        sys.path.insert(0, str(SD_DB))
        self.addCleanup(sys.path.remove, str(SD_DB))
        from sd_db import config as twin
        self.assertEqual(twin.read_env("home", self.envv), expected)

    def test_python_absent_is_empty(self):
        self.assertEqual(stc.read_env("nothing", self.envv), {})

    def test_shell_sources_and_exports_it(self):
        done = shell('st_source_env notify; sh -c \'printf "%s|%s|%s" "$A" "$B" "$C"\'', self.envv)
        self.assertEqual(done.stdout, "1|two words|3")

    def test_an_exported_value_wins_over_the_file(self):
        # sd:3553. One rule for every caller: `VAR=x tool start` overrides the
        # file. The file fills only the names that are unset; an exported empty
        # value wins too, as it switches a setting off (local-kev).
        env = dict(self.envv, A="from-shell", C="")
        done = shell('st_source_env notify; sh -c \'printf "%s|%s|%s" "$A" "$B" "$C"\'', env)
        self.assertEqual(done.stdout, "from-shell|two words|")

    def test_an_exported_value_wins_wherever_the_file_assigns_it(self):
        # sd:3553 review: jev and notify kept their own snapshots, which also
        # covered a second assignment on one line and a `${X:=...}` default.
        # The loader alone must too, now that they lean on it.
        (self.conf / "notify/.env").write_text('export A=1 B=2\n: "${C:=file}"\n')
        env = dict(self.envv, B="from-shell", C="")
        done = shell('st_source_env notify; sh -c \'printf "%s|%s|%s" "$A" "$B" "$C"\'', env)
        self.assertEqual(done.stdout, "1|from-shell|")

    def test_a_script_default_that_is_not_exported_loses_to_the_file(self):
        # sd:3553 review. Only the environment is the operator's override. A
        # default the script assigned itself (mezmo-pipeline/lib.sh sets
        # MEZMO_API_BASE before it loads the file) must not beat the file.
        done = shell('A=default; B=also; st_source_env notify; sh -c \'printf "%s|%s" "$A" "$B"\'', self.envv)
        self.assertEqual(done.stdout, "1|two words")

    def test_a_script_default_that_keeps_the_exported_value_still_loses_to_it(self):
        done = shell('A="${A:-default}"; st_source_env notify; printf "%s" "$A"', dict(self.envv, A="from-shell"))
        self.assertEqual(done.stdout, "from-shell")

    def test_an_exported_value_is_restored_exported(self):
        done = shell('st_source_env notify; sh -c \'printf "%s" "$A"\'', dict(self.envv, A="from-shell"))
        self.assertEqual(done.stdout, "from-shell")

    def test_an_exported_value_with_odd_characters_survives(self):
        odd = "a b'c\"d$E`f\\g\nh"
        done = shell('st_source_env notify; printf %s "$B"', dict(self.envv, B=odd))
        self.assertEqual(done.stdout, odd)

    def test_sourcing_leaves_no_helper_variables_behind(self):
        done = shell('st_source_env notify; set | grep -c "^_st_" || true', dict(self.envv, A="x"))
        self.assertEqual(done.stdout.strip(), "0")

    def test_a_named_file_in_the_tool_folder_keeps_the_rule(self):
        # sd:3964: machine-setup reads <config>/machine-setup/machine-setup.env.
        (self.conf / "notify/other.env").write_text("A=other\nB=other\n")
        done = shell('st_source_env notify other.env; sh -c \'printf "%s|%s" "$A" "$B"\'', dict(self.envv, B=""))
        self.assertEqual(done.stdout, "other|")

    def test_no_tracked_script_loads_its_env_file_its_own_way(self):
        # sd:3553 class pass. st_source_env is the one loader; a hand-rolled
        # snapshot (ENV_X="${X:-}") let an exported empty value lose, and a
        # bare `. "$..._ENV_FILE"` let the file win.
        def files(pattern):
            done = subprocess.run(
                ["git", "grep", "-nE", pattern, "--", "*.sh", ":(exclude)tests", ":(exclude)*/tests/*"],
                cwd=LIB.parent, capture_output=True, text=True)
            # A subshell that reads one value changes nothing in its caller.
            hits = [line for line in done.stdout.splitlines()
                    if not line.split(":", 2)[2].lstrip().startswith("(")]
            return {line.split(":")[0] for line in hits}, done.stdout

        snapshots, out = files(r'^[^#]*ENV_[A-Z_]+="\$\{[A-Z_]+:-\}"')
        self.assertEqual(set(), snapshots, out)
        sourced, out = files(r'^[^#]*(^|[;&|[:space:]])\. "?\$[^ ]*(ENV_FILE|\.env)\b')
        allowed = {
            # Third-party tools' own files, outside the config root (CLAUDE.md deviations).
            "local-gito/gito.sh", "local-prism/prism.sh",
        }
        self.assertLessEqual(sourced, allowed, out)

    def test_shell_absent_is_fine(self):
        self.assertEqual(shell("st_source_env nothing; echo ok", self.envv).stdout.strip(), "ok")

    def test_missing_names_the_path_and_the_example(self):
        done = shell("st_missing NTFY_TOPIC notify .env", self.envv)
        self.assertIn("NTFY_TOPIC is not set", done.stderr)
        self.assertIn(f"copy local-notify/.env.example to {self.conf}/notify/.env", done.stderr)
        msg = stc.missing("NTFY_TOPIC", "notify", ".env", environ=self.envv)
        self.assertIn(f"copy local-notify/.env.example to {self.conf}/notify/.env", msg)

    def test_mezmo_folder_keeps_its_name(self):
        done = shell("st_missing MEZMO_KEY mezmo-pipeline .env mezmo-pipeline", self.envv)
        self.assertIn(f"copy mezmo-pipeline/.env.example to {self.conf}/mezmo-pipeline/.env", done.stderr)


#: The `sd_db` twin lives in the command pack's `lib/` (`SD_PACK_ROOT`).
SD_DB = Path(os.environ.get("SD_PACK_ROOT") or Path.home() / "repos/platypeeps/sd-ai-command-pack") / "lib"


class CronJobDirs(unittest.TestCase):
    """cron_job_dirs here, its sd_db twin and cron-jobs.sh resolve the same folders."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conf = Path(self.tmp.name) / "c"
        self.jobs = self.conf / "cron-jobs/jobs"
        self.jobs.mkdir(parents=True)
        sys.path.insert(0, str(SD_DB))
        self.addCleanup(sys.path.remove, str(SD_DB))
        from sd_db import config as twin
        self.twin = twin

    def cases(self):
        base = {"PATH": os.environ["PATH"], "HOME": self.tmp.name, "SYSTEM_TOOLS_CONFIG": str(self.conf)}
        return [{**base, "CRON_JOBS_HOST": "Mini"},
                {**base, "CRON_JOBS_HOST": "mini", "CRON_JOBS_EXTRA_DIRS": "/a::/b"},
                base]

    def test_host_folder_sits_between_the_extra_dirs_and_the_shared_one(self):
        env = self.cases()[1]
        self.assertEqual(stc.cron_job_dirs(env), [Path("/a"), Path("/b"), self.jobs / "mini", self.jobs])

    def test_the_twin_agrees(self):
        for env in self.cases():
            self.assertEqual(stc.cron_job_dirs(env), self.twin.cron_job_dirs(env), env)

    def test_the_default_host_matches_hostname_s(self):
        short = subprocess.run(["hostname", "-s"], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(stc.cron_job_dirs(self.cases()[2])[0], self.jobs / short.lower())

    def test_env_file_supplies_the_host_and_an_exported_value_wins(self):
        (self.conf / "cron-jobs/.env").write_text('CRON_JOBS_HOST="mini"\n')
        env = self.cases()[2]
        self.assertEqual(stc.cron_job_dirs(env)[0], self.jobs / "mini")
        self.assertEqual(self.twin.cron_job_dirs(env)[0], self.jobs / "mini")
        self.assertEqual(stc.cron_job_dirs({**env, "CRON_JOBS_HOST": "studio"})[0], self.jobs / "studio")

    def test_an_exported_empty_value_beats_the_file(self):
        # sd:3964: cron-jobs.sh keeps an exported empty value (st_source_env),
        # so the file's host and extra folder must not come back here. The
        # pack twin still lets the file win until its own fix lands.
        (self.conf / "cron-jobs/.env").write_text('CRON_JOBS_HOST="mini"\nCRON_JOBS_EXTRA_DIRS=/a\n')
        short = subprocess.run(["hostname", "-s"], capture_output=True, text=True, check=True).stdout.strip()
        env = {**self.cases()[2], "CRON_JOBS_HOST": "", "CRON_JOBS_EXTRA_DIRS": ""}
        self.assertEqual(stc.cron_job_dirs(env), [self.jobs / short.lower(), self.jobs])


#: `<environ>.get(X) or <file values>.get(X)`, the same name on both sides: an
#: exported empty value is falsy, so the file wins where the shell keeps it.
HAND_ROLLED = re.compile(r'\.get\(\s*("[A-Za-z_]\w*"|[A-Za-z_]\w*)(?=[\s,)])[^()]*\)\s*or\s*'
                         r'(?!(?:os\.)?environ\b)[\w.]+(?:\([^()]*\))?\.get\(\s*\1(?=[\s,)])')


class PythonReaders(unittest.TestCase):
    """The Python readers keep st_source_env's rule (sd:3964)."""

    def test_an_exported_value_wins_an_empty_one_too(self):
        saved = {"A": "file", "B": "file"}
        env = {"A": "", "C": "shell"}
        self.assertEqual([stc.setting(name, saved, env) for name in "ABCD"], ["", "file", "shell", None])

    def test_no_tracked_python_reader_lets_the_file_win(self):
        files = subprocess.run(["git", "ls-files", "-z", "*.py", ":(exclude)tests", ":(exclude)*/tests/*"],
                               cwd=LIB.parent, capture_output=True, text=True, check=True).stdout.split("\0")
        hits = []
        for name in filter(None, files):
            text = (LIB.parent / name).read_text(encoding="utf-8", errors="replace")
            hits += [f"{name}:{text.count(chr(10), 0, m.start()) + 1}: {m.group(0)}" for m in HAND_ROLLED.finditer(text)]
        self.assertEqual(hits, [], "read the value with system_tools_config.setting")

    def test_the_guard_sees_each_shape_it_names(self):
        for line in ('env.get("X") or saved.get("X")', "environ.get(name) or saved.get(name) or ''",
                     'os.environ.get("X")\n    or system_tools_config.read_env(TOOL).get("X")'):
            self.assertRegex(line, HAND_ROLLED)
        for line in ('env.get("X") or env.get("Y")', 'environ.get("X") or os.environ.get("X")'):
            self.assertNotRegex(line, HAND_ROLLED)


if __name__ == "__main__":
    unittest.main()
