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

    def test_state_dir_is_the_xdg_state_folder_not_the_config_folder(self):
        # sd:3990. Run state sat in <config>/<tool>/, where values belong.
        self.assertEqual(shell("st_state_dir mirror-sync", self.env()).stdout.strip(),
                         str(self.home / ".local/state/system/mirror-sync"))
        env = self.env(XDG_STATE_HOME=str(self.home / "xs"), SYSTEM_TOOLS_CONFIG=str(self.home / "c"))
        self.assertEqual(shell("st_state_dir mirror-sync", env).stdout.strip(), str(self.home / "xs/system/mirror-sync"))

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
        # The dashboard, which kept the one literal, is the pack's since pack sd:5987.
        self.assertEqual(hits, [])


class SharedValues(unittest.TestCase):
    """SYSTEM_TOOLS_LABEL_PREFIX and OBSIDIAN_VAULT live once, in <config>/shared.env (sd:3990)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.conf = self.home / "c"
        (self.conf / "notify").mkdir(parents=True)
        self.shared = self.conf / "shared.env"
        self.shared.write_text('SYSTEM_TOOLS_LABEL_PREFIX="org.example"\nOBSIDIAN_VAULT="$HOME/My Vault"\n')
        self.envv = {"PATH": os.environ["PATH"], "HOME": str(self.home), "SYSTEM_TOOLS_CONFIG": str(self.conf)}

    def test_both_languages_read_the_prefix_and_the_vault_from_the_file(self):
        self.assertEqual(shell("st_label_prefix", self.envv).stdout.strip(), "org.example")
        self.assertEqual(stc.label_prefix(self.envv), "org.example")
        want = f"{self.home}/My Vault"
        self.assertEqual(shell("st_shared OBSIDIAN_VAULT", self.envv).stdout.strip(), want)
        self.assertEqual(shell("st_obsidian_vault", self.envv).stdout.strip(), want)
        self.assertEqual(stc.shared("OBSIDIAN_VAULT", self.envv), want)
        self.assertEqual(stc.obsidian_vault(self.envv), Path(want))

    def test_an_exported_value_wins_over_the_file_an_empty_one_too(self):
        env = dict(self.envv, SYSTEM_TOOLS_LABEL_PREFIX="other.example", OBSIDIAN_VAULT="")
        self.assertEqual(shell("st_label_prefix", env).stdout.strip(), "other.example")
        self.assertEqual(stc.label_prefix(env), "other.example")
        self.assertEqual(shell("st_shared OBSIDIAN_VAULT", env).stdout, "\n")
        self.assertEqual(stc.shared("OBSIDIAN_VAULT", env), "")

    def test_a_tool_env_file_still_overrides_the_shared_one(self):
        # Older state: a tool .env that still sets the prefix keeps winning.
        (self.conf / "notify/.env").write_text("SYSTEM_TOOLS_LABEL_PREFIX=old.example\n")
        done = shell("st_source_env notify; st_label_prefix", self.envv)
        self.assertEqual(done.stdout.strip(), "old.example")

    def test_without_the_file_the_defaults_hold(self):
        self.shared.unlink()
        self.assertEqual(shell("st_label_prefix", self.envv).stdout.strip(), "local.system-tools")
        self.assertEqual(stc.label_prefix(self.envv), "local.system-tools")
        self.assertEqual(shell("st_obsidian_vault", self.envv).stdout.strip(),
                         f"{self.home}/Documents/Obsidian Vault")
        self.assertEqual(stc.obsidian_vault(self.envv), self.home / "Documents/Obsidian Vault")

    def test_a_file_that_does_not_source_is_named_not_taken_for_empty(self):
        self.shared.write_text('OBSIDIAN_VAULT="unterminated\n')
        done = shell("st_shared OBSIDIAN_VAULT", self.envv)
        self.assertEqual(done.stdout.strip(), "")
        self.assertIn(str(self.shared), done.stderr)

    def test_config_file_maps_shared_env_to_the_root(self):
        self.assertEqual(shell("st_config_file config-check shared.env", self.envv).stdout.strip(),
                         str(self.shared))

    def test_the_file_does_not_leak_other_names_into_the_caller(self):
        self.shared.write_text("OTHER=1\nOBSIDIAN_VAULT=/v\n")
        done = shell('st_shared OBSIDIAN_VAULT >/dev/null; printf "%s" "${OTHER-unset}"', self.envv)
        self.assertEqual(done.stdout, "unset")


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

    def test_python_keeps_an_unquoted_path_with_spaces(self):
        # sd:10423 review: `.` refuses such a line, but a Python-only
        # reader (agent-meter's ledger) read it whole before; a word
        # break must not cut it. Only a blank before `#` ends it.
        (self.conf / "meter").mkdir(parents=True)
        (self.conf / "meter/.env").write_text(
            "AGENT_METER_FILE=$HOME/my ledger/meter.jsonl\n"
            "N=two  words\nT=a b\t\nL= /lead\n")
        home = self.envv["HOME"]
        self.assertEqual(stc.read_env("meter", self.envv),
                         {"AGENT_METER_FILE": f"{home}/my ledger/meter.jsonl",
                          "N": "two  words", "T": "a b", "L": "/lead"})

    def test_a_refusal_names_only_an_assigned_variable(self):
        # Review class: the refusal named text the parser had not
        # established as an assignment's variable. A word inside a
        # quoted argument, a continuation line of an open quote and a
        # name sh would not assign never reach the message.
        (self.conf / "bad").mkdir(parents=True)
        env = self.conf / "bad/.env"
        for text, read in ((": 'abc& privateToken=hidden'", {}),
                           ("X=1; : 'abc& privateToken=hidden'", {"X": "1"}),
                           ("X=1 && : 'abc& privateToken=hidden'", {"X": "1"}),
                           (": \"${U:=http://example.test/?a=1&b=2}\"", {}),
                           ("echo hi; X=1", {"X": "1"}),
                           ("echo a | cat\nX=1", {"X": "1"})):
            with self.subTest(text=text):
                env.write_text(text + "\n")
                self.assertEqual(stc.read_env("bad", self.envv), read)
        for text, line, name in (
                (": abc& privateToken=hidden", 1, ""),
                ("2>/dev/null AGENT_METER_FILE=/tmp/meter.jsonl", 1, ""),
                ("true >/dev/null; AGENT_METER_FILE=/tmp/meter.jsonl", 1, ""),
                ("[ -n \"$X\" ] || X=1", 1, ""),
                ("m\u00efni=a|privateToken", 1, ""),
                ("A=\"first\nprivateToken=a|b\"\nB=1", 1, "A: "),
                ("echo \"first\nprivateToken=a|b\"\nB=1", 1, ""),
                ("B=1\nit's privateToken", 2, "")):
            with self.subTest(text=text):
                env.write_text(text + "\n", encoding="utf-8")
                with self.assertRaises(stc.EnvFileError) as caught:
                    stc.read_env("bad", self.envv)
                message = str(caught.exception)
                self.assertTrue(message.startswith(f"{env}:{line}: {name}an un"), message)
                for private in ("privateToken", "abc", "hidden", "first", "ni"):
                    self.assertNotIn(private, message.replace(str(env), ""))

    def test_a_python_only_reader_never_takes_a_shorter_value(self):
        # Review class: where the shell reading and the reading from
        # before sd:10423 differ, a file no shell sources (agent-meter's)
        # refuses by name; `sourced=False` asks for that.
        (self.conf / "meter").mkdir(parents=True)
        env = self.conf / "meter/.env"
        for text in ("AGENT_METER_FILE=/tmp/Release #1/meter.jsonl",
                     "AGENT_METER_FILE=/tmp/Release; archive/meter.jsonl",
                     "AGENT_METER_FILE=/tmp/Release;",
                     "AGENT_METER_FILE=/tmp/Release && X=1",
                     "AGENT_METER_FILE=/tmp/Release # note",
                     "AGENT_METER_FILE=/tmp/Re'lea'se",
                     "AGENT_METER_FILE=/tmp/Re\\ lease",
                     'AGENT_METER_FILE="/tmp/Release" # note',
                     'AGENT_METER_FILE="/tmp/Re\\"lease"',
                     "AGENT_METER_FILE=  # nothing",
                     "AGENT_METER_FILE=~;",
                     "X=1; AGENT_METER_FILE=/tmp/Release"):
            with self.subTest(text=text):
                env.write_text(text + "\n")
                with self.assertRaises(stc.EnvFileError) as caught:
                    stc.read_env("meter", self.envv, sourced=False)
                self.assertRegex(str(caught.exception), rf"^{re.escape(str(env))}:1: (AGENT_METER_FILE|X): ")
        home = self.envv["HOME"]
        env.write_text("A=$HOME/x\nB='two words'\nC=\"$HOME/q\"\nD=~/t\n"
                       "E=\nexport F=1\nG=a b\nH=a#b\n")
        self.assertEqual(stc.read_env("meter", self.envv, sourced=False),
                         {"A": f"{home}/x", "B": "two words", "C": f"{home}/q",
                          "D": f"{home}/t", "E": "", "F": "1", "G": "a b", "H": "a#b"})

    def test_a_multi_word_value_reads_as_before_or_refuses(self):
        # Review: `.` assigns nothing from an unquoted multi-word value,
        # so only Python reads it; it must read as it did before
        # sd:10423, whole, or refuse by name, never a shorter path.
        (self.conf / "meter").mkdir(parents=True)
        env = self.conf / "meter/.env"
        for text in ("AGENT_METER_FILE=$HOME/my ledger #2/meter.jsonl",
                     "AGENT_METER_FILE=$HOME/my ledger;2/meter.jsonl",
                     "AGENT_METER_FILE=$HOME/my ledger && b/meter.jsonl",
                     'AGENT_METER_FILE=$HOME/my "led" ger/meter.jsonl',
                     "AGENT_METER_FILE=$HOME/my led\\ger/meter.jsonl",
                     "AGENT_METER_FILE=$HOME/my ledger # note"):
            with self.subTest(text=text):
                env.write_text(text + "\n")
                with self.assertRaises(stc.EnvFileError) as caught:
                    stc.read_env("meter", self.envv)
                self.assertIn(f"{env}:1: AGENT_METER_FILE:", str(caught.exception))
        env.write_text("A=$HOME/my ledger/meter.jsonl\nB=  x  y  \nC=x y\t\n")
        home = self.envv["HOME"]
        self.assertEqual(stc.read_env("meter", self.envv),
                         {"A": f"{home}/my ledger/meter.jsonl", "B": "x  y", "C": "x y"})

    def test_python_keeps_what_sourcing_does_not_assign(self):
        # `.` leaves these unset (a syntax error, a second word), so
        # only Python reads them: the text stays whole, as before
        # sd:10423; a multi-word one with a `;` refuses (see above).
        (self.conf / "odd").mkdir(parents=True)
        (self.conf / "odd/.env").write_text("G=a(b)\nW=x y\n")
        self.assertEqual(stc.read_env("odd", self.envv),
                         {"G": "a(b)", "W": "x y"})

    def test_python_refuses_a_redirection_pipe_or_background(self):
        # sd:10423 lead ruling: a misread sends agent-meter to the
        # default ledger, so an unquoted < > | & or a skipped || branch
        # refuses the line by file, line and name instead of guessing.
        (self.conf / "bad").mkdir(parents=True)
        env = self.conf / "bad/.env"
        cases = [
            ("AGENT_METER_FILE=/tmp/meter.jsonl 2>/dev/null", "AGENT_METER_FILE"),
            ("X=1 >/dev/null; AGENT_METER_FILE=/tmp/meter.jsonl", "X"),
            ("X=1 || AGENT_METER_FILE=/tmp/meter.jsonl", "X"),
            ("U=http://example.test/?a=1&b=2", "U"),
            ("P=a|b", "P"),
            ("I=a<b", "I"),
            ("X=1 && Y=2 &", "Y"),
        ]
        for text, name in cases:
            with self.subTest(text=text):
                env.write_text("# head\n" + text + "\n")
                with self.assertRaises(stc.EnvFileError) as caught:
                    stc.read_env("bad", self.envv)
                self.assertIn(f"{env}:2: {name}:", str(caught.exception))
                self.assertIsInstance(caught.exception, ValueError)
        env.write_text("Q='a|b>c&d'\nR=\"x<y\"\nS=a\\&b\nT=1 && V=2\n")
        self.assertEqual(stc.read_env("bad", self.envv),
                         {"Q": "a|b>c&d", "R": "x<y", "S": "a&b", "T": "1", "V": "2"})

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
SD_DB = Path(os.environ.get("SD_PACK_ROOT") or Path.home() / "repos/platypeeps/sd-workflow") / "lib"


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

    #: .env lines and the CRON_JOBS_HOST `.` leaves; pack sd:10424's
    #: dashboard reader takes the same cases (sd:10423).
    HOST_LINES = [
        ("CRON_JOBS_HOST=mini # this machine", "mini"),
        ("CRON_JOBS_HOST=mini\t# tab", "mini"),
        ('CRON_JOBS_HOST="mini" # quoted', "mini"),
        ("CRON_JOBS_HOST='mini' # single", "mini"),
        ('CRON_JOBS_HOST="mini # kept"', "mini # kept"),
        ("CRON_JOBS_HOST=mini#kept", "mini#kept"),
        ("CRON_JOBS_HOST=mi'ni'\"x\"", "minix"),
        ("CRON_JOBS_HOST=mini\\ two", "mini two"),
        ('CRON_JOBS_HOST="m\\"i"', 'm"i'),
        ("export CRON_JOBS_HOST=mini  # export", "mini"),
        ("CRON_JOBS_HOST=  # nothing", ""),
        ("CRON_JOBS_HOST=mïni # non-ASCII", "mïni"),
        # Operators `.` honours and assigns across (review round 3).
        ("CRON_JOBS_HOST=mini;", "mini"),
        ("CRON_JOBS_HOST=mini ; X=1", "mini"),
        ("CRON_JOBS_HOST=mini;# note", "mini"),
        ("X=1; CRON_JOBS_HOST=mini", "mini"),
        ("X=1;export CRON_JOBS_HOST=mini; Y=2", "mini"),
        ("X=1 && CRON_JOBS_HOST=mini", "mini"),
        ("CRON_JOBS_HOST=mini&&X=1", "mini"),
        # `||`, `<` and `>` refuse instead: see EnvFile (lead ruling).
        ('CRON_JOBS_HOST="mini;x&&y>z"', "mini;x&&y>z"),
        ("CRON_JOBS_HOST='mini;x'", "mini;x"),
        ("CRON_JOBS_HOST=mini\\;x", "mini;x"),
        ("CRON_JOBS_HOST=mini; CRON_JOBS_HOST=later", "later"),
    ]

    def test_an_inline_comment_ends_the_value_as_sourcing_does(self):
        base = self.cases()[2]
        for line, expected in self.HOST_LINES:
            with self.subTest(line=line):
                (self.conf / "cron-jobs/.env").write_text(line + "\n", encoding="utf-8")
                done = shell('st_source_env cron-jobs; printf "%s" "$CRON_JOBS_HOST"', base)
                self.assertEqual(done.stdout, expected, "the fixture disagrees with sh")
                self.assertEqual(stc.read_env("cron-jobs", base)["CRON_JOBS_HOST"], expected)
                if expected:
                    self.assertEqual(stc.cron_job_dirs(base)[0], self.jobs / expected.lower())

    def test_an_exported_empty_value_beats_the_file(self):
        # sd:3964: cron-jobs.sh keeps an exported empty value (st_source_env),
        # so the file's host and extra folder must not come back here. The
        # pack twin still lets the file win until its own fix lands.
        (self.conf / "cron-jobs/.env").write_text('CRON_JOBS_HOST="mini"\nCRON_JOBS_EXTRA_DIRS=/a\n')
        short = subprocess.run(["hostname", "-s"], capture_output=True, text=True, check=True).stdout.strip()
        env = {**self.cases()[2], "CRON_JOBS_HOST": "", "CRON_JOBS_EXTRA_DIRS": ""}
        self.assertEqual(stc.cron_job_dirs(env), [self.jobs / short.lower(), self.jobs])


class ShellEnv(unittest.TestCase):
    """st_shell_env: env.sh for a headless claude, its CUSTOM_ token mapped (sd:5791)."""

    #: What a child process sees, never the value itself: `same`, `other` or `unset`.
    SEEN = ('case "${CLAUDE_CODE_OAUTH_TOKEN-unset}" in unset) echo unset ;; "$WANT") echo same ;; '
            '*) echo other ;; esac')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def env_sh(self, text):
        (self.home / ".config/shell").mkdir(parents=True, exist_ok=True)
        (self.home / ".config/shell/env.sh").write_text(text)

    def seen(self, want, **extra):
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home), "WANT": want, **extra}
        return shell(f"st_shell_env; echo rc=$?; sh -c '{self.SEEN}'", env)

    def test_the_custom_token_becomes_claude_code_oauth_token(self):
        self.env_sh("export CUSTOM_CLAUDE_CODE_OAUTH_TOKEN=tok-custom\necho noise\n")
        result = self.seen("tok-custom")
        self.assertEqual(result.stdout, "rc=0\nsame\n", result.stderr)
        self.assertEqual(result.stderr, "")

    def test_a_token_already_set_wins(self):
        self.env_sh("export CUSTOM_CLAUDE_CODE_OAUTH_TOKEN=tok-custom\n")
        self.assertEqual(self.seen("tok-own", CLAUDE_CODE_OAUTH_TOKEN="tok-own").stdout, "rc=0\nsame\n")
        self.env_sh("export CUSTOM_CLAUDE_CODE_OAUTH_TOKEN=tok-custom\nexport CLAUDE_CODE_OAUTH_TOKEN=tok-file\n")
        self.assertEqual(self.seen("tok-file").stdout, "rc=0\nsame\n")

    def test_an_empty_token_takes_the_custom_one(self):
        self.env_sh("export CUSTOM_CLAUDE_CODE_OAUTH_TOKEN=tok-custom\n")
        self.assertEqual(self.seen("tok-custom", CLAUDE_CODE_OAUTH_TOKEN="").stdout, "rc=0\nsame\n")

    def test_no_custom_token_exports_nothing(self):
        self.env_sh("export OTHER=1\n")
        self.assertEqual(self.seen("").stdout, "rc=0\nunset\n")
        self.env_sh("export CUSTOM_CLAUDE_CODE_OAUTH_TOKEN=\n")
        self.assertEqual(self.seen("").stdout, "rc=0\nunset\n")

    def test_no_env_sh_still_maps_an_exported_custom_token(self):
        self.assertEqual(self.seen("tok-x", CUSTOM_CLAUDE_CODE_OAUTH_TOKEN="tok-x").stdout, "rc=0\nsame\n")
        self.assertEqual(self.seen("").stdout, "rc=0\nunset\n")

    def test_it_runs_under_set_e(self):
        self.env_sh("export OTHER=1\n")
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home)}
        self.assertEqual(shell("set -e; st_shell_env; echo after", env).stdout, "after\n")

    def test_a_syntax_error_in_env_sh_is_named_and_skipped(self):
        # sd:9652: a half-written env.sh ended the cron run inside the dot,
        # so the FAILED line went nowhere. It must be skipped and named.
        self.env_sh("export CUSTOM_CLAUDE_CODE_OAUTH_TOKEN=tok-custom\nif then fi (\n")
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home)}
        result = shell("set -e; st_shell_env; echo after rc=$?", env)
        self.assertEqual(result.stdout, "after rc=0\n")
        self.assertIn(".config/shell/env.sh", result.stderr)
        self.assertNotIn("tok-custom", result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)

    def test_a_save_between_the_check_and_the_read_cannot_end_the_shell(self):
        # sd:9652 review: the check and the dot read env.sh apart, so a
        # save in between handed the dot a broken file. A fake sh runs
        # the real check, then rewrites env.sh as an in-place save does.
        self.env_sh("export CUSTOM_CLAUDE_CODE_OAUTH_TOKEN=tok-custom\n")
        bin_dir = self.home / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "sh"
        fake.write_text('#!/bin/sh\n/bin/sh "$@"; rc=$?\n'
                        'printf "if then fi (\\n" > "$HOME/.config/shell/env.sh"\n'
                        'exit $rc\n')
        fake.chmod(0o755)
        env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(self.home)}
        result = shell('set -e; st_shell_env; echo "after $CLAUDE_CODE_OAUTH_TOKEN"', env)
        self.assertEqual(result.stdout, "after tok-custom\n", result.stderr)

    def test_a_return_in_env_sh_ends_only_env_sh(self):
        # sd:9652 review: under eval, a `return` in env.sh returned from
        # st_shell_env itself and skipped the mapping, the cleanup and
        # the xtrace restore. A load-once guard returns early too.
        script = ('set -x; st_shell_env; set +x; echo "tok=$CLAUDE_CODE_OAUTH_TOKEN"; '
                  'echo "text=${_st_text-unset}"; echo "path=${_st_shenv-unset}"')
        cases = (
            ("export CUSTOM_CLAUDE_CODE_OAUTH_TOKEN=tok-custom\nreturn 0\necho never\n", {}),
            ('[ -n "${__SHELL_ENV_LOADED:-}" ] && return\n'
             "export CUSTOM_CLAUDE_CODE_OAUTH_TOKEN=tok-other\n",
             {"__SHELL_ENV_LOADED": "1", "CUSTOM_CLAUDE_CODE_OAUTH_TOKEN": "tok-custom"}),
        )
        for text, extra in cases:
            self.env_sh(text)
            env = {"PATH": os.environ["PATH"], "HOME": str(self.home), **extra}
            result = shell(f"{script}; echo rc=$?", env)
            self.assertEqual(result.stdout,
                             "tok=tok-custom\ntext=unset\npath=unset\nrc=0\n", result.stderr)
            # xtrace came back on: the caller's `set +x` shows in the trace.
            self.assertIn("+ set +x", result.stderr)
            self.assertNotIn("never", result.stdout)

    def test_a_failing_line_in_env_sh_cannot_end_a_set_e_caller(self):
        # sd:9652 review: a failing command or a false last test in
        # env.sh ended a `set -e` cron runner with nothing said; so did
        # an unset name under `set -u`. The caller's flags come back.
        self.env_sh("export CUSTOM_CLAUDE_CODE_OAUTH_TOKEN=tok-custom\nfalse\n"
                    'echo "$ST_TEST_UNSET_NAME"\n'
                    '[ -d "$HOME/missing" ] && export PATH="$HOME/missing:$PATH"\n')
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home)}
        result = shell('set -eu; st_shell_env; echo "after $CLAUDE_CODE_OAUTH_TOKEN $-"', env)
        self.assertRegex(result.stdout, r"^after tok-custom \S*e\S*\n$", result.stderr)
        self.assertIn("u", result.stdout.split()[-1])

    def test_env_sh_finds_its_own_folder_through_bash_source(self):
        # sd:9652 review: under eval, BASH_SOURCE named the loader, so
        # env.sh could not dot a file beside itself. bash sets it; dash
        # has none, so this runs under bash.
        self.env_sh('. "${BASH_SOURCE%/*}/extra.sh"\n')
        (self.home / ".config/shell/extra.sh").write_text(
            "export CUSTOM_CLAUDE_CODE_OAUTH_TOKEN=tok-custom\n")
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home)}
        result = subprocess.run(
            ["bash", "-c", f'. "{LIB}/config.sh"; set -e; st_shell_env; '
                           'echo "after $CLAUDE_CODE_OAUTH_TOKEN"; ls -A "$HOME/.config/shell"'],
            env=env, capture_output=True, text=True, check=False)
        # The private copy is gone after the read.
        self.assertEqual(result.stdout, "after tok-custom\nenv.sh\nextra.sh\n", result.stderr)

    def test_an_env_sh_that_cannot_be_copied_is_named_and_skipped(self):
        # sd:9652 review: a failed copy left an empty copy that passed
        # sh -n, so the run went on with no settings and no word why.
        self.env_sh("export CUSTOM_CLAUDE_CODE_OAUTH_TOKEN=tok-custom\n")
        path = self.home / ".config/shell/env.sh"
        path.chmod(0)
        self.addCleanup(path.chmod, 0o600)
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home)}
        result = shell('set -e; st_shell_env; echo "after ${CLAUDE_CODE_OAUTH_TOKEN-unset}"', env)
        self.assertEqual(result.stdout, "after unset\n", result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn(".config/shell/env.sh", result.stderr)
        self.assertEqual(sorted(os.listdir(path.parent)), ["env.sh"])

    def test_a_read_only_folder_still_loads_env_sh_from_a_copy(self):
        # sd:9652 review: a readable env.sh in a read-only folder loaded
        # before the copy step; skipping it lost every export. The copy
        # goes to TMPDIR instead, so a save after the check (a fake sh
        # runs the real check, then rewrites env.sh) still cannot reach
        # the shell, and no copy is left behind.
        self.env_sh("export CUSTOM_CLAUDE_CODE_OAUTH_TOKEN=tok-custom\n")
        folder = self.home / ".config/shell"
        tmp = self.home / "tmp"
        tmp.mkdir()
        bin_dir = self.home / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "sh"
        fake.write_text('#!/bin/sh\n/bin/sh "$@"; rc=$?\n'
                        'printf "if then fi (\\n" > "$HOME/.config/shell/env.sh"\n'
                        'exit $rc\n')
        fake.chmod(0o755)
        folder.chmod(0o500)
        self.addCleanup(folder.chmod, 0o700)
        env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(self.home),
               "TMPDIR": str(tmp)}
        result = shell('set -e; st_shell_env; echo "after ${CLAUDE_CODE_OAUTH_TOKEN-unset}"', env)
        self.assertEqual(result.stdout, "after tok-custom\n", result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(os.listdir(tmp), [])

    def test_xtrace_never_prints_a_value(self):
        # sd:9652: a caller's set -x, or one env.sh turns on, must not
        # trace the token; the caller's own xtrace comes back after.
        for text in ("export CUSTOM_CLAUDE_CODE_OAUTH_TOKEN=tok-custom\n",
                     "set -x\nexport CUSTOM_CLAUDE_CODE_OAUTH_TOKEN=tok-custom\n"):
            self.env_sh(text)
            env = {"PATH": os.environ["PATH"], "HOME": str(self.home)}
            result = shell('set -x; st_shell_env; case $- in *x*) echo on ;; esac', env)
            self.assertNotIn("tok-custom", result.stderr)
            self.assertEqual(result.stdout, "on\n")
            result = shell('st_shell_env; case $- in *x*) echo on ;; esac', env)
            self.assertEqual(result.stdout, "")
            self.assertNotIn("tok-custom", result.stderr)

    def test_a_caller_set_x_traces_no_token_and_stays_on(self):
        self.env_sh("CUSTOM_CLAUDE_CODE_OAUTH_TOKEN=tok-secret\nexport CUSTOM_CLAUDE_CODE_OAUTH_TOKEN\n")
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home), "WANT": "tok-secret"}
        result = shell(f"set -x; st_shell_env; case $- in *x*) echo on ;; esac; sh -c '{self.SEEN}'", env)
        self.assertNotIn("tok-secret", result.stderr)
        self.assertEqual(result.stdout, "on\nsame\n", result.stderr)


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
