"""Every folder's entrypoint is executable and keeps conventions 1 and 6 (sd:2648, sd:3552, sd:3554).

Convention 1 gives each runnable folder one entrypoint, and `./<name>.sh`
only runs when git tracks it as mode 100755. An entrypoint once tracked as
100644 exited 126 when run directly and nothing noticed: `sh <file>`
works either way, and that is how every suite calls it.

The entrypoints are read from `git ls-files -s`, not listed here, so a new
folder is checked the day it is tracked. A folder's entrypoint is the one
local-health-check probes: `<stem>.sh`, where the stem is the folder name
minus `local-` or `mezmo-`, or else the folder's only root `*.sh`. A folder
with neither (`lib/`, `tests/`) has no entrypoint and is skipped.

Each entrypoint also runs in a sandbox: a bare call exits 1 with usage on
stderr, and `help`, `-h` and `--help` exit 0 with usage on stdout. A folder
whose bare call acts, and a folder whose entrypoint is not a shell script,
are named on CLAUDE.md "Deliberate deviations" lines, which this reads.

Each entrypoint whose `status` answers unlike an unknown verb also runs it
on an empty machine: a scratch HOME and config folder, stubs first on PATH
for every service command a status asks, stdin closed. Nothing is set up
there, so convention 6 wants exit 3, nothing to check. A status that is a
report and exits 0 is named on its CLAUDE.md "Deliberate deviations" line.

Stdlib and git only. This runs in the `tests/ci-native.sh` preflight as
`python3 tests/test_entrypoint_mode.py` from the repository root, and the
preflight's unwired-suite guard fails when `tests/ci-native.sh` stops naming it.
"""

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from test_product_name import PREFIX as VENDOR

ROOT = Path(__file__).resolve().parent.parent

#: Commands a probe could reach on this machine. Each stub fails, so a
#: service reads as not running and nothing real is asked; the launchctl
#: guard of tests/check.sh sits behind them.
STUBS = ("launchctl", "docker", "kind", "kubectl", "curl", "tailscale", "osascript", "claude", "go")
#: Seconds one probe may take; the health-check sweep allows help 5.
PROBE_BOUND = 30
#: The verb no entrypoint has; `status` that answers like it is no verb.
UNKNOWN = "no-such-verb"


def index(root=ROOT):
    """`git ls-files -s` as {path: mode}."""
    out = subprocess.run(
        ["git", "ls-files", "-s", "-z"], cwd=root, capture_output=True, text=True, check=True,
    )
    entries = {}
    for record in filter(None, out.stdout.split("\0")):
        meta, path = record.split("\t", 1)
        entries[path] = meta.split()[0]
    return entries


def entrypoints(entries):
    """{entrypoint path: mode} for each top-level folder that has one."""
    roots = {}
    for path, mode in entries.items():
        parts = path.split("/")
        if len(parts) == 2 and parts[1].endswith(".sh"):
            roots.setdefault(parts[0], {})[parts[1]] = mode
    found = {}
    for folder, scripts in roots.items():
        stem = folder.removeprefix("local-").removeprefix(VENDOR)
        name = f"{stem}.sh" if f"{stem}.sh" in scripts else None
        if name is None and len(scripts) == 1:
            name = next(iter(scripts))
        if name is not None:
            found[f"{folder}/{name}"] = scripts[name]
    return found


def recorded(lead, text=None):
    """The folders that CLAUDE.md's deviation line `- <lead>...` names in backticks."""
    if text is None:
        text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.startswith("- " + lead)]
    if len(lines) != 1:
        raise AssertionError(f"CLAUDE.md holds {len(lines)} deviation lines starting {lead!r}, not 1")
    return set(re.findall(r"`([a-z0-9]+(?:-[a-z0-9]+)+)`", lines[0]))


class Sandbox:
    """An empty machine for probes: scratch HOME and config, the stubs first on PATH.

    Probes run a copy of the tracked tree, since some scripts write beside
    themselves before reading a verb (milvus's sudo shim, log folders).
    """

    def __init__(self, tmp, root=None):
        if root is None:
            root = tmp / "tree"
            for path in index():
                if (ROOT / path).is_file():
                    (root / path).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(ROOT / path, root / path)
        stubs = tmp / "stubs"
        stubs.mkdir()
        for name in STUBS:
            (stubs / name).write_text("#!/bin/sh\nexit 1\n")
            (stubs / name).chmod(0o755)
        (tmp / "home").mkdir()
        self.root, self.cwd = root, tmp
        self.env = {"PATH": f"{stubs}:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(tmp / "home"),
                    "SYSTEM_TOOLS_CONFIG": str(tmp / "config"), "TMPDIR": str(tmp)}

    def run(self, path, *args):
        """(exit code, stdout, stderr) of `sh <path> <args>`."""
        done = subprocess.run(
            ["sh", str(self.root / path), *args], env=self.env, cwd=self.cwd,
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=PROBE_BOUND,
        )
        return done.returncode, done.stdout, done.stderr

    def status_codes(self, paths):
        """{path: exit code of `status`} for each entrypoint that has the verb.

        A script without it answers `status` as it answers UNKNOWN: the same
        code and text once both words are struck out.
        """
        def strike(answer):
            return answer[0], *(re.sub(rf"\bstatus\b|{UNKNOWN}", "VERB", text) for text in answer[1:])

        def probe(path):
            asked = self.run(path, "status")
            return path, asked[0], strike(asked) != strike(self.run(path, UNKNOWN))

        with ThreadPoolExecutor(max_workers=8) as pool:
            return {path: code for path, code, has in pool.map(probe, sorted(paths)) if has}


class TheTree(unittest.TestCase):
    def test_a_folder_that_runs_another_language_is_recorded(self):
        entries = index()
        roots = {}
        for path in entries:
            parts = path.split("/")
            if len(parts) == 2:
                roots.setdefault(parts[0], set()).add(Path(parts[1]).suffix)
        programs = {f for f, suffixes in roots.items() if suffixes & {".py", ".js"} and ".sh" not in suffixes}
        self.assertIn("local-agent-meter", programs, "the enumeration found no program folders")
        self.assertEqual(set(), programs - recorded("Not a shell entrypoint"),
                         "a root *.py or *.js with no *.sh beside it: give the folder its <name>.sh, "
                         "or name it on CLAUDE.md's `Not a shell entrypoint` line")

    def test_each_deviation_names_a_folder_that_exists(self):
        text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        section = text.split("Deliberate deviations:", 1)[1].split("\n## ", 1)[0]
        named = set(re.findall(r"`([a-z0-9]+(?:-[a-z0-9]+)+)`", section))
        folders = {path.split("/")[0] for path in index()}
        self.assertIn("local-cswap", named)
        self.assertEqual(set(), named - folders, "a deviation names a folder that is gone; drop it")

    def test_every_entrypoint_is_tracked_executable(self):
        found = entrypoints(index())
        self.assertIn("local-repo-sync/repo-sync.sh", found, "the enumeration found no entrypoints")
        bad = sorted(p for p, mode in found.items() if mode != "100755")
        self.assertEqual([], bad, "tracked without the executable bit; run "
                         "`git update-index --chmod=+x <path>` for each")

    def test_the_preflight_runs_this_file(self):
        lines = (ROOT / "tests/ci-native.sh").read_text(encoding="utf-8").splitlines()
        self.assertTrue(
            "python3 tests/test_entrypoint_mode.py" in lines,
            "the tests/ci-native.sh preflight no longer runs this file",
        )


class UsageOnAnEmptyMachine(unittest.TestCase):
    def setUp(self):
        self.paths = sorted(entrypoints(index()))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sandbox = Sandbox(Path(tmp.name))

    def answers(self, paths, *args):
        with ThreadPoolExecutor(max_workers=8) as pool:
            return dict(zip(paths, pool.map(lambda p: self.sandbox.run(p, *args), paths)))

    def test_a_bare_call_prints_usage_to_stderr_and_exits_1(self):
        acts = recorded("No-arg runs the action")
        paths = [p for p in self.paths if p.split("/")[0] not in acts]
        wrong = {p: (code, err[:80]) for p, (code, _, err) in self.answers(paths).items()
                 if code != 1 or not err.strip()}
        self.assertEqual({}, wrong, "convention 1: no-arg prints usage to stderr and exits 1; "
                         "a bare call that acts goes on CLAUDE.md's `No-arg runs the action` line")

    def test_each_help_form_prints_usage_to_stdout_and_exits_0(self):
        for form in ("help", "-h", "--help"):
            wrong = {p: (code, err[:80]) for p, (code, out, err) in self.answers(self.paths, form).items()
                     if code != 0 or not out.strip()}
            self.assertEqual({}, wrong, f"convention 1: `{form}` prints usage to stdout and exits 0")


class StatusOnAnEmptyMachine(unittest.TestCase):
    def test_every_status_has_nothing_to_check(self):
        reports = recorded("`status` reports")
        probed = list(entrypoints(index()))
        with tempfile.TemporaryDirectory() as tmp:
            codes = Sandbox(Path(tmp)).status_codes(probed)
        self.assertIn("local-cswap/cswap.sh", codes, "the probe found no status verb")
        wrong = {p: code for p, code in codes.items()
                 if code != (0 if p.split("/")[0] in reports else 3)}
        self.assertEqual({}, wrong, "status on a machine with nothing set up must exit 3 "
                         "(convention 6); a report that exits 0 goes on CLAUDE.md's "
                         "`status` reports deviation line")

    def test_probes_run_a_copy_of_the_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Sandbox(Path(tmp))
            self.assertTrue(sandbox.root.is_relative_to(tmp), "probes would write into the checkout")
            self.assertTrue((sandbox.root / "lib/config.sh").is_file())

    def test_each_report_on_the_deviation_line_has_a_status(self):
        reports = recorded("`status` reports")
        found = {p.split("/")[0] for p in entrypoints(index())}
        self.assertEqual(set(), reports - found, "a deviation names a folder with no entrypoint")


class TheRule(unittest.TestCase):
    def test_the_stem_wins_and_a_lone_script_stands_in(self):
        found = entrypoints({
            "local-x/x.sh": "100755", "local-x/helper.sh": "100644",
            "mezmo-pipeline/pipeline.sh": "100644",
            "plain/plain.sh": "100755",
            "local-runner/runner.sh": "100644",
            "lib/a.sh": "100644", "lib/b.sh": "100644",
            "local-x/tests/deep.sh": "100644", "top.sh": "100644",
        })
        self.assertEqual({
            "local-x/x.sh": "100755", "mezmo-pipeline/pipeline.sh": "100644",
            "plain/plain.sh": "100755", "local-runner/runner.sh": "100644",
        }, found)


class TheStatusProbe(unittest.TestCase):
    SCRIPTS = {
        "nothing": 'case "$1" in status) echo "SKIP"; exit 3 ;; *) echo "usage: x" >&2; exit 1 ;; esac',
        "notloaded": 'case "$1" in status) echo "label: x"; exit 2 ;; *) echo "usage: x" >&2; exit 1 ;; esac',
        "down": 'case "$1" in status) docker ps >/dev/null || echo "not running"; exit 0 ;; '
                '*) echo "usage: x status" >&2; exit 1 ;; esac',
        "noverb": 'echo "usage: noverb.sh start|stop (not $1)" >&2; exit 1',
        "early": 'echo "early: missing config" >&2; exit 1',
    }

    def test_a_script_without_the_verb_is_not_probed_and_the_rest_report_their_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tree"
            for name, body in self.SCRIPTS.items():
                (root / name).mkdir(parents=True)
                (root / name / f"{name}.sh").write_text("#!/bin/sh\n" + body + "\n")
            (Path(tmp) / "box").mkdir()
            codes = Sandbox(Path(tmp) / "box", root).status_codes(
                [f"{n}/{n}.sh" for n in self.SCRIPTS])
        self.assertEqual({"nothing/nothing.sh": 3, "notloaded/notloaded.sh": 2,
                          "down/down.sh": 0}, codes)

    def test_the_deviation_line_is_read_whole_and_only_once(self):
        text = "- `status` reports: `local-a`, `vendor-b`; `tests/x.sh` and `state/`.\n- Other: `local-c`.\n"
        self.assertEqual({"local-a", "vendor-b"}, recorded("`status` reports", text))
        with self.assertRaises(AssertionError):
            recorded("`status` reports", text + text)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
