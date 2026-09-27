"""The optional Jev ordering of findings, with the Jev call stubbed.

`health-check.sh check` is run once per contract case, against a
fixture tools root and a stub standing in for `local-jev/jev.sh`
(`HEALTH_CHECK_JEV`). No run touches the network: the stub answers from the
questions file it was handed, and CI would otherwise spend tokens and go red
on someone else's outage.

Every run also gets a stub `PATH` for the machine stages -- `log`, `sysctl`,
`route`, `ping`, `dscacheutil`, `diskutil`, `smartctl`, `hostname`, `date`,
`uptime` -- and a `HOME` in a temp dir. Not for speed alone (`log show --last
24h` takes tens of seconds on a lived-in Mac): a byte-for-byte comparison
between two runs needs the findings to be the same findings, and a real
machine does not promise that. The stages themselves are unchanged; only what
they read is fixed.

Python `unittest` and not sh for the reason CLAUDE.md gives for the sibling
suites: the CI wrapper asserts a unittest summary and refuses skips.
"""

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "health-check.sh"
PHRASE = "local-health-check reads these codes"
GATEWAY = "10.11.12.13"
HOSTNAME = "fixturehost"
# Private words the fixture puts where the machine stages read them: launchd
# labels named after personal routines, a crashing app, a faulting process.
# Each one is a string the report may print and the Jev payload must not carry.
NOT_LOADED = "local.system-tools.cron.diary-reminders"
# A second job in the same state: its shareable form is identical to the
# first one's, so Jev cannot tell the two apart.
NOT_LOADED_2 = "local.system-tools.cron.birthday-plans"
CRON_FAILED = "local.system-tools.cron.club-morning-digest"
LAST_EXIT = "local.system-tools.market-watch"
CRASHED_APP = "PrivateDiaryApp"
FAULTING = "SecretProcName"
PRIVATE_WORDS = ("diary-reminders", "birthday-plans", "club-morning-digest", "market-watch",
                 "local.system-tools", CRASHED_APP, FAULTING)
RUN_TIMEOUT = 300

STUB_PY = '''\
import json, os, shutil, sys
log = os.environ["JEV_STUB_LOG"]
mode = os.environ.get("JEV_STUB_MODE", "answer")
verb = sys.argv[1] if len(sys.argv) > 1 else ""
with open(log, "a") as fh:
    fh.write(verb + "\\n")
# Everything handed to jev besides the files it is pointed at: its argv and
# its stdin, for every verb, so a test can read all of what one run sent.
with open(os.path.join(os.environ["JEV_STUB_DIR"], "wire"), "a") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read()}) + "\\n")
if verb == "enabled":
    # `jev enabled STAGE` reads the stage variable itself, and the stub has to
    # honour it or a caller that switched its stage off would still be ordered.
    stage = sys.argv[2] if len(sys.argv) > 2 else ""
    if stage and os.environ.get(stage, "").strip().lower() in (
            "0", "off", "false", "no", "disabled"):
        sys.exit(3)
    sys.exit(3 if mode == "disabled" else 0)
if verb != "ask":
    sys.exit(1)
args = sys.argv[2:]
opts = dict(zip(args[0::2], args[1::2]))
out = os.environ["JEV_STUB_DIR"]
shutil.copy(opts["--questions"], os.path.join(out, "questions.json"))
shutil.copy(opts["--state"], os.path.join(out, "state.json"))
questions = json.load(open(opts["--questions"]))
if mode == "fail":
    sys.stderr.write("stub: the endpoint said no\\n")
    sys.exit(1)
ids = sorted(questions, key=lambda k: int(k[1:]))
if mode == "short":
    ids = ids[:-1]
# As many answers as questions, but not the questions' ids: one id nobody
# asked in place of the last one.
if mode == "unknown":
    ids = ids[:-1] + ["f%d" % (len(questions) + 1)]
n = len(questions) + 1
answers = {qid: {"noul": round((i + 1) / n, 3)} for i, qid in enumerate(ids)}
text = json.dumps({"answers": answers}, indent=2, sort_keys=True)
# The same count again, with the first id answered twice and the last never:
# JSON with a repeated key, which only text can carry.
if mode == "dup":
    text = text.replace('"%s":' % ids[-1], '"%s":' % ids[0])
print(text)
'''


def _tool(root, folder, stem, body):
    d = root / folder
    d.mkdir()
    path = d / (stem + ".sh")
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def _stub(d, name, body):
    path = d / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


class JevOrdering(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = pathlib.Path(tempfile.mkdtemp(prefix="health-check-jev."))
        cls.home = cls.tmp / "home"
        cls.home.mkdir()

        # The stub jev.sh: a POSIX sh entrypoint, because health-check.sh calls
        # it with `sh`, wrapping the Python that does the answering.
        jevdir = cls.tmp / "jev"
        jevdir.mkdir()
        (jevdir / "stub.py").write_text(STUB_PY)
        cls.jev = jevdir / "jev.sh"
        cls.jev.write_text(
            "#!/bin/sh\nexec %s %s \"$@\"\n" % (sys.executable, jevdir / "stub.py"))
        cls.jev.chmod(0o755)

        # PATH stubs: the machine stages read these and nothing else.
        cls.bin = cls.tmp / "bin"
        cls.bin.mkdir()
        # 60001 faults against a seeded baseline of 1: the fault stage raises
        # its jump finding and names the top process.
        _stub(cls.bin, "log", """\
awk 'BEGIN { for (i = 0; i <= 60000; i++)
  print "2026-01-01 00:00:00.000 F  %s[1a:2b] fault" }'
""" % FAULTING)
        # Silent unless a run hands it a SMART status: then that text is a
        # machine-supplied word inside a finding's shareable form.
        _stub(cls.bin, "diskutil",
              '[ -z "$SMART_STUB" ] || echo "   SMART Status: $SMART_STUB"\n')
        _stub(cls.bin, "smartctl", "exit 0\n")
        # Two labels are not loaded; every other one is loaded, holds no pid,
        # and last exited 1.
        _stub(cls.bin, "launchctl", """\
case "$2" in
  *%s|*%s) exit 1 ;;
  *) printf '\\tlast exit code = 1\\n' ;;
esac
""" % (NOT_LOADED, NOT_LOADED_2))
        agents = cls.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        for label in (NOT_LOADED, NOT_LOADED_2, CRON_FAILED, LAST_EXIT):
            (agents / (label + ".plist")).write_text("<plist/>\n")
        reports = cls.home / "Library" / "Logs" / "DiagnosticReports"
        reports.mkdir(parents=True)
        (reports / (CRASHED_APP + "-2026-01-01-000000.ips")).write_text("{}\n")
        _stub(cls.bin, "route", 'echo "  gateway: %s"\n' % GATEWAY)
        _stub(cls.bin, "ping", "exit 1\n")
        _stub(cls.bin, "dscacheutil", "exit 0\n")
        _stub(cls.bin, "uptime", 'echo "up 1 day, 0 users, load averages: 1 1 1"\n')
        _stub(cls.bin, "hostname", 'echo "%s"\n' % HOSTNAME)
        _stub(cls.bin, "date", 'echo "2026-01-01 00:00"\n')
        _stub(cls.bin, "sysctl", """\
case "$2" in
  kern.memorystatus_vm_pressure_level) echo 1 ;;
  hw.ncpu) echo 8 ;;
  vm.loadavg) echo "{ 1.00 1.00 1.00 }" ;;
  vm.swapusage) echo "total = 0.00M  used = 0.00M  free = 0.00M" ;;
  *) exit 1 ;;
esac
""")

        # Two tools roots: the ordinary one, and one where local-jev itself is
        # the tool that raised the finding.
        cls.root = cls.tmp / "tools"
        cls.root.mkdir()
        cls.broken_line = (
            "FAIL — widget down on %s at %s — see %s/Library/Logs/widget.log"
            % (HOSTNAME, GATEWAY, cls.home))
        cls.broken = _tool(cls.root, "local-declares-broken", "declares-broken", f"""\
case "${{1:-}}" in
  help)
    echo "usage: declares-broken.sh status"
    echo "  status  0 healthy, 3 nothing to check, 1 broken; {PHRASE}"
    exit 0 ;;
  status) echo "{cls.broken_line}"; exit 1 ;;
  *) echo "usage: declares-broken.sh status" >&2; exit 1 ;;
esac
""")
        _tool(cls.root, "local-declares-skip", "declares-skip", f"""\
case "${{1:-}}" in
  help)
    echo "usage: declares-skip.sh status"
    echo "  status  0 healthy, 3 nothing to check, 1 broken; {PHRASE}"
    exit 0 ;;
  status) echo "SKIP — not configured on this machine"; exit 3 ;;
  *) echo "usage: declares-skip.sh status" >&2; exit 1 ;;
esac
""")
        # local-cron-jobs, as the launchd stage and the sweep both ask it: a
        # job's own record says its last run failed, and the sweep's verdict
        # line names the job, the way the real tool's does.
        cron_job = CRON_FAILED[len("local.system-tools.cron."):]
        _tool(cls.root, "local-cron-jobs", "cron-jobs", f"""\
case "${{1:-}}" in
  help)
    echo "usage: cron-jobs.sh status [job]"
    echo "  status  0 healthy, 3 nothing installed, 1 a job failed; {PHRASE}"
    exit 0 ;;
  status)
    case "${{2:-}}" in
      ''|{cron_job}) echo "local-cron-jobs: FAIL — 1 job(s) whose last run failed: {cron_job}"; exit 1 ;;
      *) exit 3 ;;
    esac ;;
  *) echo "usage: cron-jobs.sh status" >&2; exit 1 ;;
esac
""")
        cls.jevroot = cls.tmp / "tools-with-jev"
        cls.jevroot.mkdir()
        _tool(cls.jevroot, "local-jev", "jev", f"""\
case "${{1:-}}" in
  help)
    echo "usage: jev.sh status"
    echo "  status  0 ok, 3 no key here, 1 configured and failing; {PHRASE}"
    exit 0 ;;
  status) echo "FAIL — the endpoint did not answer"; exit 1 ;;
  *) echo "usage: jev.sh status" >&2; exit 1 ;;
esac
""")
        # local-jev declares convention 6 in its own help, so the nightly
        # probes it. A machine with the switch off or no key answers 3, and a
        # 3 is silence: it must not become a finding, and it must not stop the
        # ordering either -- `jev enabled` is what decides that, one line down.
        cls.offroot = cls.tmp / "tools-with-jev-off"
        cls.offroot.mkdir()
        _tool(cls.offroot, "local-jev", "jev", f"""\
case "${{1:-}}" in
  help)
    echo "usage: jev.sh status"
    echo "  status  0 ok, 3 no key here, 1 configured and failing; {PHRASE}"
    exit 0 ;;
  status) echo "SKIP — no key on this machine"; exit 3 ;;
  *) echo "usage: jev.sh status" >&2; exit 1 ;;
esac
""")
        _tool(cls.offroot, "local-declares-broken", "declares-broken", f"""\
case "${{1:-}}" in
  help)
    echo "usage: declares-broken.sh status"
    echo "  status  0 healthy, 3 nothing to check, 1 broken; {PHRASE}"
    exit 0 ;;
  status) echo "{cls.broken_line}"; exit 1 ;;
  *) echo "usage: declares-broken.sh status" >&2; exit 1 ;;
esac
""")

        cls.runs = {}
        # `JEV_HEALTH_CHECK` switches the stage off and nothing switches it
        # on: unset is on. So the baseline -- today's output, with no Jev in
        # it -- is the run that sets it to 0, and `unset` is the run that
        # proves a machine nobody configured now gets the ordering.
        cls.runs["baseline"] = cls.run_check("baseline", {"JEV_HEALTH_CHECK": "0"})
        cls.runs["off"] = cls.run_check(
            "off", {"JEV_HEALTH_CHECK": "0", "JEV_STUB_MODE": "answer"})
        cls.runs["unset"] = cls.run_check("unset", {"JEV_STUB_MODE": "answer"})
        cls.runs["disabled"] = cls.run_check(
            "disabled", {"JEV_STUB_MODE": "disabled"})
        cls.runs["ordered"] = cls.run_check(
            "ordered", {"JEV_HEALTH_CHECK": "1", "JEV_STUB_MODE": "answer"})
        cls.runs["failed"] = cls.run_check(
            "failed", {"JEV_HEALTH_CHECK": "1", "JEV_STUB_MODE": "fail"})
        cls.runs["short"] = cls.run_check(
            "short", {"JEV_HEALTH_CHECK": "1", "JEV_STUB_MODE": "short"})
        cls.runs["unknown"] = cls.run_check(
            "unknown", {"JEV_HEALTH_CHECK": "1", "JEV_STUB_MODE": "unknown"})
        cls.runs["dup"] = cls.run_check(
            "dup", {"JEV_HEALTH_CHECK": "1", "JEV_STUB_MODE": "dup"})
        # A path with a space in it, once under /Users and once under $HOME:
        # the redaction must take the words after the space too (sd:1224).
        cls.runs["spaced-users"] = cls.run_check(
            "spaced-users", {"JEV_HEALTH_CHECK": "1", "JEV_STUB_MODE": "answer",
                             "SMART_STUB": "Failing /Users/alice/Secret Project/budget.xlsx"})
        cls.runs["spaced-home"] = cls.run_check(
            "spaced-home", {"JEV_HEALTH_CHECK": "1", "JEV_STUB_MODE": "answer",
                            "SMART_STUB": "Failing %s/Hidden Folder/diary.txt" % cls.home})
        cls.runs["jevbroke"] = cls.run_check(
            "jevbroke", {"JEV_HEALTH_CHECK": "1", "JEV_STUB_MODE": "answer"},
            root=cls.jevroot)
        cls.runs["jevoff"] = cls.run_check(
            "jevoff", {"JEV_HEALTH_CHECK": "1", "JEV_STUB_MODE": "answer"},
            root=cls.offroot)
        # A copy of the script in which one finding forgot its shareable form,
        # which is what the next finding added to it would look like.
        cls.dns_site = ('"check DNS servers in System Settings > Network; '
                        'try dscacheutil -flushcache" =')
        text = SCRIPT.read_text()
        cls.dns_site_count = text.count(cls.dns_site)
        cls.forgetful = cls.tmp / "forgetful" / "health-check.sh"
        cls.forgetful.parent.mkdir()
        cls.forgetful.write_text(text.replace(cls.dns_site, cls.dns_site[:-2]))
        cls.runs["unshareable"] = cls.run_check(
            "unshareable", {"JEV_HEALTH_CHECK": "1", "JEV_STUB_MODE": "answer"},
            script=cls.forgetful)

    @classmethod
    def run_check(cls, name, extra, root=None, script=SCRIPT):
        """One `check`, with its own state dir, stub log and payload dir."""
        d = cls.tmp / ("run-" + name)
        d.mkdir()
        (d / "state").mkdir()
        (d / "payload").mkdir()
        # Seeded baselines, so the fault and persistence stages compare and
        # raise their findings instead of seeding on this first run.
        (d / "state" / "fault-count").write_text("1\n")
        (d / "state" / "launchd-baseline").write_text("")
        env = dict(os.environ)
        env.update({
            "HOME": str(cls.home),
            "PATH": "%s:%s" % (cls.bin, env.get("PATH", "")),
            "HEALTH_CHECK_TOOLS_ROOT": str(root or cls.root),
            "HEALTH_CHECK_STATE": str(d / "state"),
            "HEALTH_CHECK_STATUS_BOUND": "5",
            "HEALTH_CHECK_JEV": str(cls.jev),
            "JEV_STUB_LOG": str(d / "calls"),
            "JEV_STUB_DIR": str(d / "payload"),
        })
        env.pop("JEV_HEALTH_CHECK", None)
        # The fixture labels carry the default prefix.
        env.pop("SYSTEM_TOOLS_LABEL_PREFIX", None)
        env.update(extra)
        proc = subprocess.run(
            ["sh", str(script), "check"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
            timeout=RUN_TIMEOUT)
        return {
            "rc": proc.returncode, "out": proc.stdout, "err": proc.stderr,
            "dir": d,
            "calls": (d / "calls").read_text().split() if (d / "calls").exists() else [],
        }

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------

    def block(self, run, header):
        lines = run["out"].splitlines()
        if header not in lines:
            return []
        start = lines.index(header) + 1
        out = []
        for line in lines[start:]:
            if line and not line.startswith(" ") and line.endswith(":"):
                break
            if line.strip():
                out.append(line)
        return out

    def headlines(self, run, strip_label=True):
        heads = [l[2:] for l in self.block(run, "findings:") if l.startswith("- ")]
        if strip_label:
            heads = [h.split("  [jev: ")[0] for h in heads]
        return heads

    def payload(self, run, name):
        return (run["dir"] / "payload" / name).read_text()

    def shared_lines(self, run):
        """The line Jev saw for each question, by its 1-based number."""
        state = json.loads(self.payload(run, "state.json"))
        return {int(k[1:]): v for k, v in state["findings"].items()}

    def question_of(self, run):
        """The question each finding became, by the finding's 1-based number.

        The fixtures make exactly one pair of findings share a shareable form:
        the two unloaded jobs. Every other finding is its own question, and
        questions are numbered in the order their first finding appears.
        """
        heads = self.headlines(self.runs["baseline"])
        pair = [i for i, h in enumerate(heads, 1)
                if h.endswith(" has a plist but is not loaded")]
        self.assertEqual(len(pair), 2, heads)
        self.assertEqual(len(self.shared_lines(run)), len(heads) - 1)
        out, q = {}, 0
        for i in range(1, len(heads) + 1):
            if i == pair[1]:
                out[i] = out[pair[0]]
            else:
                q += 1
                out[i] = q
        return out

    def expected_order(self, run):
        question = self.question_of(run)
        # The stub scores question q as q/(n+1); every finding takes its
        # question's score, and ties keep the stages' order.
        return sorted(question, key=lambda i: (-question[i], i))

    # -- cases -------------------------------------------------------------

    def test_the_fixtures_produce_something_to_order(self):
        heads = self.headlines(self.runs["baseline"])
        self.assertIn("local-declares-broken: " + self.broken_line, heads)
        self.assertTrue(any(GATEWAY in h for h in heads), heads)
        self.assertGreaterEqual(len(heads), 2, heads)
        self.assertEqual(self.runs["baseline"]["rc"], 0, self.runs["baseline"]["err"])

    def test_the_stage_switched_off_is_byte_for_byte_todays_output(self):
        self.assertEqual(self.runs["off"]["out"], self.runs["baseline"]["out"])
        self.assertEqual(self.runs["off"]["rc"], self.runs["baseline"]["rc"])
        # Nothing was asked, not even whether Jev is enabled. `jev enabled`
        # reads the variable itself, so this is what switched off costs: one
        # local process, no network, and no ordering.
        self.assertEqual(self.runs["off"]["calls"], ["enabled"])

    def test_the_stage_unset_orders_like_the_stage_switched_on(self):
        # The flip: an integration nobody opted into runs. Every one of these
        # was opt-in at first, and a per-caller switch that defaults to off
        # makes each one added after it silently never run.
        self.assertEqual(self.runs["unset"]["calls"], ["enabled", "ask"])
        self.assertEqual(self.headlines(self.runs["unset"]),
                         self.headlines(self.runs["ordered"]))

    def test_jev_disabled_is_byte_for_byte_todays_output(self):
        self.assertEqual(self.runs["disabled"]["out"], self.runs["baseline"]["out"])
        self.assertEqual(self.runs["disabled"]["rc"], self.runs["baseline"]["rc"])
        # It asked the free question and stopped there.
        self.assertEqual(self.runs["disabled"]["calls"], ["enabled"])

    def test_an_answer_reorders_and_labels(self):
        run = self.runs["ordered"]
        self.assertEqual(run["calls"], ["enabled", "ask"])
        # The stub scores finding i as i/(n+1), so the last finding is the
        # loudest. Findings that sent Jev the same line form one group that
        # carries its highest score and keeps the stages' order inside it.
        self.assertEqual(self.headlines(run),
                         [self.headlines(self.runs["baseline"])[i - 1]
                          for i in self.expected_order(run)])
        labelled = [l for l in self.block(run, "findings:")
                    if l.startswith("- ") and "  [jev: needs a human tonight " in l]
        self.assertEqual(len(labelled), len(self.headlines(run)))
        self.assertIn('- jev ordered %d finding(s) by "needs a human tonight" (one request)'
                      % len(self.headlines(run)), self.block(run, "notes:"))

    def test_findings_jev_cannot_tell_apart_share_one_rank(self):
        run = self.runs["ordered"]
        base = self.headlines(self.runs["baseline"])
        pair = [h for h in base if h.endswith(" has a plist but is not loaded")]
        self.assertEqual(len(pair), 2, base)
        # Asked once, not twice: one judgment for identical evidence, so a
        # larger group cannot draw a higher answer than a lone finding.
        lines = list(self.shared_lines(run).values())
        self.assertEqual(len(lines), len(set(lines)), lines)
        self.assertEqual(len(lines), len(base) - 1, lines)
        labelled = [l[2:] for l in self.block(run, "findings:") if l.startswith("- ")]
        got = [l for l in labelled if l.split("  [jev: ")[0] in pair]
        # One number for both, and the stages' order between them.
        self.assertEqual([l.split("  [jev: ")[0] for l in got], pair)
        self.assertEqual(len({l.split("  [jev: ")[1] for l in got}), 1, got)

    def test_one_request_carries_every_finding(self):
        questions = json.loads(self.payload(self.runs["ordered"], "questions.json"))
        self.assertEqual(sorted(questions),
                         sorted("f%d" % q for q in self.shared_lines(self.runs["ordered"])))
        self.assertEqual(set(self.question_of(self.runs["ordered"]).values()),
                         set(self.shared_lines(self.runs["ordered"])))
        self.assertTrue(all(q["type"] == "noul" for q in questions.values()), questions)
        # One `ask`, not one call per finding.
        self.assertEqual(self.runs["ordered"]["calls"].count("ask"), 1)

    def test_the_fixtures_print_the_private_words_in_the_report(self):
        # The report keeps every name: it stays on this machine. Without this
        # the privacy test below could pass on a fixture that raised nothing.
        out = self.runs["baseline"]["out"]
        for word in PRIVATE_WORDS:
            self.assertIn(word, out)
        heads = self.headlines(self.runs["baseline"])
        for start in ("launchd: %s has a plist but is not loaded" % NOT_LOADED,
                      "launchd: %s has a plist but is not loaded" % NOT_LOADED_2,
                      "launchd: %s reports a failed run" % CRON_FAILED,
                      "launchd: %s last exited 1" % LAST_EXIT,
                      "app crash report(s) in the last day: " + CRASHED_APP,
                      "log faults jumped: ",
                      "new launch daemon(s)/agent(s) since last run: ",
                      "local-cron-jobs: FAIL"):
            self.assertTrue(any(h.startswith(start) for h in heads), (start, heads))

    def sent(self, run):
        """Everything one run handed to jev: argv, stdin and both files."""
        d = run["dir"] / "payload"
        return {name: (d / name).read_text()
                for name in ("wire", "state.json", "questions.json")
                if (d / name).exists()}

    def test_nothing_private_leaves_the_machine(self):
        sent = self.sent(self.runs["ordered"])
        self.assertEqual(sorted(sent), ["questions.json", "state.json", "wire"])
        for name, text in sent.items():
            for word in PRIVATE_WORDS + (
                    str(self.home), "/Users/", "/Library/", GATEWAY, HOSTNAME,
                    ".ts.net", "widget down"):
                self.assertNotIn(word, text, name)
        state = json.loads(sent["state.json"])
        findings = sorted(state["findings"].values())
        # Each finding is still recognisable by its kind: the stage's own
        # words, a placeholder for the name, and the numbers.
        for line in ("launchd: <cron job> has a plist but is not loaded",
                     "launchd: <cron job> reports a failed run that nothing has superseded",
                     "launchd: <agent> last exited 1",
                     "log faults jumped: 60001 in 24h (baseline 1)",
                     "local-cron-jobs: status reports broken (exit 1)",
                     "local-declares-broken: status reports broken (exit 1)",
                     "default gateway not answering ping"):
            self.assertIn(line, findings)
        # The crash and persistence stages also read /Library, which is the
        # real machine's, so only their shape is fixed: counts, never names.
        for shape in (r"^app crash report\(s\) in the last day: "
                      r"[1-9][0-9]* report\(s\) from [1-9][0-9]* app\(s\)$",
                      r"^new launch daemon\(s\)/agent\(s\) since last run: [1-9][0-9]*$"):
            self.assertEqual(len([f for f in findings if re.match(shape, f)]), 1,
                             (shape, findings))
        # Fix lines never leave: they are nothing but absolute paths.
        self.assertNotIn("by hand", " ".join(findings))

    def test_a_finding_without_a_shareable_form_stops_the_ordering(self):
        self.assertEqual(self.dns_site_count, 1, "the DNS finding site moved")
        run = self.runs["unshareable"]
        # Fails closed: the free gate ran, the control arm was recorded (which
        # sends nothing), and nothing was asked.
        self.assertEqual(run["calls"], ["enabled", "record"])
        self.assertEqual(sorted(self.sent(run)), ["wire"])
        self.assertEqual(self.block(run, "findings:"),
                         self.block(self.runs["baseline"], "findings:"))
        self.assertIn("- jev ordering skipped: 1 finding(s) have no shareable form",
                      self.block(run, "notes:"))

    def test_nothing_at_all_leaves_with_the_stage_off(self):
        for case in ("baseline", "off", "disabled"):
            wire = self.sent(self.runs[case])
            self.assertEqual(sorted(wire), ["wire"], case)
            calls = [json.loads(l) for l in wire["wire"].splitlines()]
            self.assertEqual([c["argv"][0] for c in calls], ["enabled"], case)
            self.assertEqual([c["stdin"] for c in calls], [""], case)

    def test_a_failed_ask_leaves_the_findings_and_the_exit_code_intact(self):
        run = self.runs["failed"]
        self.assertEqual(run["rc"], self.runs["baseline"]["rc"])
        self.assertEqual(self.block(run, "findings:"),
                         self.block(self.runs["baseline"], "findings:"))
        notes = self.block(run, "notes:")
        self.assertTrue(any(n.startswith("- jev ordering skipped: ask failed")
                            for n in notes), notes)

    def test_a_short_answer_set_orders_nothing(self):
        run = self.runs["short"]
        self.assertEqual(self.block(run, "findings:"),
                         self.block(self.runs["baseline"], "findings:"))
        notes = self.block(run, "notes:")
        self.assertTrue(any("usable answer(s) for" in n for n in notes), notes)

    def test_answers_under_ids_nobody_asked_order_nothing(self):
        # The count matches and the ids do not: an unknown id in place of a
        # real one, or one id answered twice. Either leaves a finding with no
        # answer of its own, so the stages' order stands (sd:1224).
        for case in ("unknown", "dup"):
            with self.subTest(case=case):
                run = self.runs[case]
                self.assertEqual(run["calls"], ["enabled", "ask", "record"])
                self.assertEqual(self.block(run, "findings:"),
                                 self.block(self.runs["baseline"], "findings:"))
                notes = self.block(run, "notes:")
                self.assertTrue(any(n.startswith("- jev ordering skipped: ")
                                    and "answer" in n for n in notes), notes)

    def test_a_path_with_a_space_leaves_no_word_behind(self):
        for case, words in (("spaced-users", ("Secret", "Project", "budget")),
                            ("spaced-home", ("Hidden", "Folder", "diary"))):
            with self.subTest(case=case):
                run = self.runs[case]
                # The report prints the path whole: it stays on this machine.
                self.assertIn("disk0 SMART status: Failing ", run["out"])
                self.assertIn(words[1], run["out"])
                sent = self.sent(run)
                self.assertIn("disk0 SMART status: Failing <path>",
                              json.loads(sent["state.json"])["findings"].values())
                for name, text in sent.items():
                    for word in words:
                        self.assertNotIn(word, text, name)

    def test_the_set_of_findings_is_identical_with_and_without_jev(self):
        # The whole point: ordering and a label, never a filter.
        for case in ("off", "disabled", "ordered", "failed", "short",
                     "unknown", "dup"):
            self.assertEqual(sorted(self.headlines(self.runs[case])),
                             sorted(self.headlines(self.runs["baseline"])),
                             case)

    def test_jevs_own_finding_stops_the_triage(self):
        run = self.runs["jevbroke"]
        heads = self.headlines(run)
        self.assertTrue(any(h.startswith("local-jev: ") for h in heads), heads)
        # The sweep asked local-jev's status, which is a request of its own.
        # Nothing asks the broken judge to rank its own failure.
        self.assertEqual(run["calls"], ["enabled"])
        self.assertIn("- jev ordering skipped: local-jev raised the finding it "
                      "would be asked about", self.block(run, "notes:"))

    def test_a_switched_off_jev_stays_silent_and_stops_nothing(self):
        # The sweep reads a 3 as silence, and this stage did not change that:
        # local-jev raises no finding, so the ordering is free to run.
        run = self.runs["jevoff"]
        self.assertNotIn("local-jev", run["out"])
        self.assertEqual(run["calls"], ["enabled", "ask"])
        heads = self.headlines(run)
        self.assertTrue(any(h.startswith("local-declares-broken: ") for h in heads), heads)
        labelled = [l for l in self.block(run, "findings:")
                    if l.startswith("- ") and "  [jev: needs a human tonight " in l]
        self.assertEqual(len(labelled), len(heads))

    def test_no_marker_word_moved(self):
        # A stage that stops printing its marker word makes the drift check
        # lie, so the ordering must not touch the words a stage printed.
        for case in ("ordered", "failed", "short"):
            for head in self.headlines(self.runs[case]):
                self.assertIn(head, self.runs["baseline"]["out"], case)


if __name__ == "__main__":
    unittest.main()
