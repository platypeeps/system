"""A Jev caller degrades loudly, and never prints a fallback that reads as a judgment.

`local-jev` is optional by design: every caller keeps the mechanism it had,
and a machine with no key, a placeholder key, an unparsable `JEV_TIMEOUT` or
the switch off behaves identically. That contract lives in eleven call sites
and nothing enforced it. This does.

The defect class it closes was found three times in one afternoon, each time
by running code rather than reading it, and each time it wore the same shape:
**a signal that measured nothing reading exactly like a signal that measured
everything.**

* `jev ask` had no `--fallback`, so every batch call died on its arguments.
* A `sd-docs-lint` batch that answered nothing printed `read 2 citation(s),
  0 under 0.5` -- byte-identical to two confident answers.
* `--fallback unsure` collided with what `choice --unsure-below` itself
  prints. Both exit 0 and print one word, so a pass that judged nothing was
  indistinguishable from one that judged every claim and was uncertain. It
  fails in the safe-looking direction, because `unsure` reads as a
  measurement that happened.

Two layers, because one is not enough.

**The inventory** is coarse and very hard to evade: a folder is a caller if
any tracked file of its own names `jev` outside a comment. That is compared
against `KNOWN_CALLERS`, so a twelfth caller cannot be added silently -- it
fails here naming itself, and whoever adds it has to say so. A folder that
stops calling fails too: the set only changes deliberately.

**The contract** is folder-level on purpose. A syntactic scan of call sites
cannot see a wrapped call, and two callers here wrap: `sd_plan.py` builds its
argv through `jev_argv("ask", ...)`, and `notify.sh` defines `jev_ask()` and
calls `jev_ask choice ...`. A rule that reads argv would miss both and report
a pass -- the very shape this file exists to ban. So the rules ask about the
flags a folder uses anywhere in its own code, which no amount of wrapping
hides.

Stdlib and git only. This runs in the `tests/ci-native.sh` preflight
beside `tests/test_citations.py`, before the virtualenv exists, once per
`make check` and before any leg. `python3 tests/test_jev_contract.py` from the repository root is
the whole invocation, and the preflight's unwired-suite guard fails when
`tests/ci-native.sh` stops naming it.
"""

import ast
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: The tool itself, which answers for its own contract in its own suite.
TOOL = "local-jev/"

#: The verbs that leave the machine. `enabled`, `on`, `off`, `status` and
#: `test` call nothing, so they carry no contract.
VERBS = ("noul", "choice", "score", "ask")

#: What the tool prints on its own, and therefore what a fallback token may
#: never be. `noul --gate P` prints `yes` or `no`; `choice --unsure-below P`
#: prints `unsure`. A fallback spelled as one of these is the sd-fact-check
#: bug: a pass that judged nothing, wearing the words of one that judged.
#: Checked whether or not the caller passes the flag today -- a caller that
#: adds `--unsure-below` tomorrow must not have to remember this file.
RESERVED = ("yes", "no", "unsure")

#: Every folder that calls Jev. A row is a decision: each of these degrades
#: to the mechanism it had, and each was checked against a real `jev.sh` when
#: it landed. Adding a caller means adding a row and reading the rules below.
KNOWN_CALLERS = frozenset({
    "local-adversarial-gate",
    "local-drive-intake",
    "local-health-check",
    "local-mail-intake",
    "local-notify",
    "local-obsidian-review",
    "local-obsidian-tasks",
    "local-project-dashboard",
    "local-scan-for-secrets",
    "local-sd-plan",
})

#: Callers whose input may not leave the machine: every `jev` call they make
#: passes `--local-only`, so it reaches the local Kev or nothing. Their hits
#: are candidate credentials (sd:2761).
LOCAL_ONLY_CALLERS = frozenset({"local-scan-for-secrets"})

#: Folders whose `--criteria` is built at runtime, so no literal exists for
#: `test_no_fallback_token_is_a_name_its_own_criteria_offers` to compare a
#: fallback against. Each is a place that rule cannot see, written down so it
#: cannot grow silently. `drive_intake.py` reads its route names from the
#: user's conf on purpose -- a hand-kept list would offer a route the conf no
#: longer defines -- so the collision it risks is a runtime one, and
#: `jev_route` refuses it there instead.
RUNTIME_CRITERIA = frozenset({"local-drive-intake"})

#: This file names `jev` on nearly every line, so discovery finds it and
#: reports `tests` as a twelfth caller. `test_one_double.py` has the same
#: shape and the same answer: exclude yourself by path. Rename this file
#: without updating the constant and `tests` comes back as a surprise, which
#: fails loudly rather than passing quietly.
SELF = "tests/test_jev_contract.py"

#: The native suites' script names `jev` as a suite to run -- `run_suite jev`
#: and `local-jev/jev.sh test` -- and as this file's gate in its usage text. It
#: asks Jev nothing, so it is excluded by path the same way.
SUITE_RUNNER = "tests/ci-native.sh"

#: Folders that name `jev` without asking it anything, named here rather than
#: pattern-matched away so that a day one of them does starts an argument.
#: `local-bin-links` puts `jev` on PATH. `local-sd-db` is the
#: store the calls are recorded in: `sd_db/judgment.py` names the two arms a
#: row can carry, one of which is spelled `jev`, and a schema that could not
#: name the thing it counts would be a worse schema. It holds no key, opens no
#: socket and would still be correct if this folder were deleted.
#: `local-machine-setup` runs `jev shadow on` on a satellite whose shadow file
#: is missing (sd:2838): it sets a switch, asks Jev nothing, and skips the step
#: where `jev` is not on PATH.
NOT_CALLERS = frozenset({
    "local-bin-links",
    "local-machine-setup",
    "local-sd-db",
})


def tracked(*globs):
    out = subprocess.run(
        ["git", "ls-files", "--", *globs],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def code_lines(rel):
    """Non-comment, non-blank lines of a tracked file, as (number, text).

    A comment naming `jev` is prose -- `weekly-digest.sh` mentioning the
    sweep in a comment is exactly what over-reported the health-check
    declarations before #468 -- so comments are dropped before anything is
    counted. A `#` inside a string survives, which costs a false positive
    here and never a false pass.
    """
    path = ROOT / rel
    if path.is_symlink() or not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    rows = []
    for number, line in enumerate(text.split("\n"), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        rows.append((number, line))
    return rows


def caller_folders():
    """Every top-level folder whose own code names `jev`.

    Coarse on purpose. A call can be wrapped, aliased, built from a variable
    or spelled in a language this file does not parse; what it cannot be is
    made without the word appearing somewhere in the folder that makes it.
    """
    folders = {}
    for rel in tracked("*/*.py", "*/*.sh", "*/*/*.py", "*/*/*.sh"):
        if rel.startswith(TOOL) or rel in (SELF, SUITE_RUNNER):
            continue
        folder = rel.split("/")[0]
        for number, line in code_lines(rel):
            if re.search(r"\bjev\b", line, re.IGNORECASE):
                folders.setdefault(folder, []).append((rel, number))
                break
    return folders


def folder_files(folder):
    """One (rel, rows) pair per tracked code file of a folder, tests included.

    Tests are read too: a caller's own suite is where a banned spelling shows
    up first, and exempting it would let the fixture disagree with the tool.
    """
    units = []
    for rel in tracked(f"{folder}/*.py", f"{folder}/*.sh", f"{folder}/*/*.py", f"{folder}/*/*.sh"):
        rows = code_lines(rel)
        if rows:
            units.append((rel, rows))
    return units


def folder_code(folder):
    """Every code line of one folder, flattened, for the per-line rules."""
    return [(rel, number, line)
            for rel, rows in folder_files(folder)
            for number, line in rows]


def joined(rows):
    """One file's code lines as one string, with an offset -> line index.

    A flag and its value are one argument and two lines whenever the caller
    is Python: `drive_intake.py` writes `"--fallback",` and `"noise",` under
    each other, and a per-line rule reads that folder as having no fallback
    at all -- a false pass on the rule that matters most. Joining per file,
    never across files, keeps a match inside the call that made it.
    """
    text, index, pos = [], [], 0
    for number, line in rows:
        index.append((pos, number))
        text.append(line)
        pos += len(line) + 1
    return "\n".join(text), index


def line_at(index, offset):
    found = index[0][1]
    for start, number in index:
        if start > offset:
            break
        found = number
    return found


#: `--fallback X`, `--fallback=X`, `"--fallback", "X"`. One spelling per
#: language and all three in the tree today.
FALLBACK_RE = re.compile(
    r"""--fallback["']?(?:\s*[=,]\s*|\s+)["']?([A-Za-z0-9_.:-]+)""")

#: `--criteria 'a=...,b=...'` -- the literal form only. A criteria string
#: built at runtime is reported by its own test rather than guessed at.
CRITERIA_RE = re.compile(r"""--criteria["']?(?:\s*[=,]\s*|\s+)["']([^"']+)["']""")


#: `NAME = "value"` at the start of a line. A caller that names its fallback
#: -- which `drive_intake.py` does, as JEV_DEGRADED -- would otherwise hand
#: the rules below the identifier instead of the token, and every one of them
#: would compare a constant name against reserved words, match nothing and
#: pass. That is the shape this whole file exists to ban, so resolve it.
CONSTANT_RE = re.compile(r"""^([A-Z][A-Z0-9_]*)\s*=\s*["']([^"'\n]+)["']""", re.M)


def constants(units):
    out = {}
    for _, rows in units:
        text, _ = joined(rows)
        for match in CONSTANT_RE.finditer(text):
            out.setdefault(match.group(1), match.group(2))
    return out


def shell_calls(rows):
    """A shell file's code lines with each `\\` continuation joined, as (first line number, text).

    A shell call is one logical line, not one physical one: `health-check.sh`
    spells its `jev ask` across two with a trailing backslash. A rule that
    reads physical lines sees half a call there, and a flag moved to the
    second half passes it.
    """
    calls, parts, start = [], [], None
    for number, line in rows:
        start = number if start is None else start
        text = line.rstrip()
        if text.endswith("\\"):
            parts.append(text[:-1])
            continue
        parts.append(line)
        calls.append((start, " ".join(parts)))
        parts, start = [], None
    if parts:
        calls.append((start, " ".join(parts)))
    return calls


def argv_sequences(rel, source=None):
    """Every literal argument sequence in a Python file, in source order.

    A list, tuple or call's positional arguments, as the string constants it
    holds with a `None` where a name or a starred expression sits. Python
    argv here is written one element per line -- `drive_intake.py` spells the
    whole call across eleven of them -- so a rule that reads physical lines
    cannot see the order of anything real. `ast` can. Calls are included
    because `sd_plan.py` builds its argv as `jev_argv("ask", ...)`.

    Returns (line number, [str | None]) pairs. A file that does not parse is
    skipped rather than failing the suite: this rule is not a syntax gate.
    `source` stands in for the file, so the readers can be tested on their own.
    """
    try:
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8") if source is None else source)
    except (SyntaxError, ValueError, OSError):
        return []

    def constants(nodes):
        out = []
        for node in nodes:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                out.append(node.value)
            else:
                out.append(None)
        return out

    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            found.append((node.lineno, constants(node.elts)))
        elif isinstance(node, ast.Call):
            found.append((node.lineno, constants(node.args)))
    return found


def fallback_tokens(units):
    """Every `--fallback` answer a folder passes, constants resolved."""
    named = constants(units)
    out = []
    for rel, rows in units:
        text, index = joined(rows)
        for match in FALLBACK_RE.finditer(text):
            token = match.group(1)
            out.append((rel, line_at(index, match.start()),
                        named.get(token, token)))
    return out


def criteria_keys(units):
    """The answer names a literal `--criteria` string offers."""
    keys = []
    for rel, rows in units:
        text, index = joined(rows)
        for match in CRITERIA_RE.finditer(text):
            number = line_at(index, match.start())
            for part in match.group(1).split(","):
                name = part.split("=", 1)[0].strip()
                if name:
                    keys.append((rel, number, name))
    return keys

#: `--caller X`, `--stage X`, and the three spellings `FALLBACK_RE` already
#: covers, because a Python caller writes the flag and its value on two lines.
def _flag_re(flag):
    return re.compile(
        rf"""{flag}["']?(?:\s*[=,]\s*|\s+)["']?([A-Za-z0-9_.:-]+)""")


CALLER_RE = _flag_re("--caller")
STAGE_RE = _flag_re("--stage")
#: The flag itself and never its value, because the rule below turns on
#: whether a caller names a cause at all and not on which word it picks. A
#: pattern that insisted on a literal value read three callers as clean, since
#: a shell helper writes `--decline "$1"` and `$` is not in the value class.
#:
#: Backticked is prose and not argv. `code_lines` drops `#` comments, so a
#: shell caller's rationale is gone before this looks, but a Python docstring
#: survives it -- and every caller explains in one when it does and does not
#: pass `--decline`. Without the lookbehind those sentences read as argv.
DECLINE_RE = re.compile(r"(?<![`\w-])--decline\b")

#: `--arm baseline`, the flag that makes a row the control arm's own.
ARM_RE = _flag_re("--arm")

#: A deadline the **caller** owns: when it expires the caller kills `jev`
#: rather than reading what it returned. `subprocess.run(..., timeout=N)`
#: terminates the child, and the shell caller sends the signal itself.
#:
#: `jev` flushes its measurement after the answer is printed and installs no
#: signal handler, so a call that ends this way writes no row at all. That is
#: the whole reason the rule below has three cases and not two.
DEADLINE_RE = re.compile(r"kill\s+-(?:TERM|KILL)\b|timeout\s*=")

#: `jev`, in every spelling a caller reaches it by. A deadline counts only
#: when one of these is close in front of it, because every one of these
#: folders also bounds something that is not `jev` -- a reviewer subprocess,
#: an MCP client, an `open` of a URL, the planning skill itself.
JEV_TOKEN_RE = re.compile(r"\bjev|\bJEV|\$\{?JEV")

#: How far in front of a deadline `jev` may be named for the deadline to be
#: read as sitting on a call to it. One `subprocess.run` of argv, generously.
JEV_REACH = 600

#: `record`, as an argv token. Used only to subtract: the bookkeeping call
#: carries a `timeout=` of its own, and a deadline on the row-writer is not a
#: deadline on a judgment.
RECORD_TOKEN_RE = re.compile(r"""["'\s]record["'\s]""")

#: How far after a `record` token that call's own `timeout=` may sit. Wide
#: enough for the longest of them, which spans four lines of argv.
RECORD_CALL = 400

#: `jev enabled STAGE`, in every spelling that is a *call* in the tree:
#: `"$JEV" enabled JEV_X` in shell, `"enabled", "JEV_X"` in a list,
#: `"enabled", JEV_STAGE` through a constant, and `jev_argv("enabled",
#: JEV_STAGE)` through a wrapper. The name is resolved through the folder's
#: constants, because two of the Python callers pass it as one and a rule that
#: read the literal would miss both -- the wrapping this file's header is
#: about.
#:
#: The leading `"` is load-bearing, and it is the whole difference between a
#: call and a sentence. Every folder here also *writes about* its gate -- in a
#: docstring, in `help` text, in an error message -- and always as
#: ``jev enabled JEV_X``, where a bare space precedes the verb. A call has a
#: quoted command word or a quoted verb in front of it. Reading the sentences
#: as gates reported six of them as unrecorded declines, which is a rule that
#: fails on prose nobody can fix.
GATE_RE = re.compile(
    r"""(?:"\s+enabled\s+|["']enabled["']\s*,\s*)["']?([A-Za-z_][A-Za-z0-9_]*)""")

#: How far after the stage name `--record` may sit and still be the same call.
#: Every gate in the tree puts it within one argument or one continuation
#: line; the window is generous because the alternative is parsing four
#: languages, and a folder that moves it further is asking to be looked at.
RECORD_WINDOW = 200

#: The ledger's own grammar and cap, copied from
#: `local-sd-db/sd_db/judgment.py`. A name that fails either is not refused,
#: it is silently filed under `unknown` -- which is a row that counts toward
#: nothing and reads exactly like a row from a caller that never named itself.
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
MAX_NAME = 96


def tool_files(folder):
    """One (rel, rows) pair per tracked code file the *tool* itself is.

    `folder_files` reads the suite too, which is right for a rule about how a
    name is spelled: a fixture that disagrees with the tool is the bug. It is
    wrong for the rule about deadlines and causes below. A suite bounds the
    tool it drives -- `subprocess.run(..., timeout=60)` around the script --
    and that is the harness protecting itself, not the tool killing `jev`. A
    fixture also asserts the argv its tool passes, `--decline` included, and
    reading that as the tool passing it would make the rule unfixable.

    The depth is filtered here and not in the pathspec: `git ls-files` treats
    `*` as matching `/` too, so `local-notify/*.py` lists the suite as well.
    """
    units = []
    for rel in tracked(f"{folder}/*.py", f"{folder}/*.sh"):
        if rel.count("/") != 1:
            continue
        rows = code_lines(rel)
        if rows:
            units.append((rel, rows))
    return units


def deadlines(folder):
    """(rel, line) for every caller-owned deadline on something but a record.

    Coarse, like `caller_folders` above and for the same reason: which branch
    a deadline can fire on is not a thing a regular expression knows. What it
    does know is whether a folder can kill `jev` at all, which is what
    decides whether that folder's baseline rows may be the only record of a
    decline.
    """
    found = []
    for rel, rows in tool_files(folder):
        text, index = joined(rows)
        spent = [(m.end(), m.end() + RECORD_CALL)
                 for m in RECORD_TOKEN_RE.finditer(text)]
        for match in DEADLINE_RE.finditer(text):
            if any(start <= match.start() < stop for start, stop in spent):
                continue
            near = text[max(0, match.start() - JEV_REACH):match.start()]
            if not JEV_TOKEN_RE.search(near):
                continue
            found.append((rel, line_at(index, match.start())))
    return found


def records_baseline(folder):
    """Whether a folder writes a control-arm row of its own at all."""
    return any(value == "baseline"
               for _, _, value in flag_values(tool_files(folder), ARM_RE))


def flag_sites(units, pattern):
    """Every (file, line) where a folder writes one flag, value ignored."""
    out = []
    for rel, rows in units:
        text, index = joined(rows)
        for match in pattern.finditer(text):
            out.append((rel, line_at(index, match.start())))
    return out


def flag_values(units, pattern):
    """Every value a folder passes to one flag, constants resolved."""
    named = constants(units)
    out = []
    for rel, rows in units:
        text, index = joined(rows)
        for match in pattern.finditer(text):
            token = match.group(1)
            out.append((rel, line_at(index, match.start()),
                        named.get(token, token)))
    return out


def gates(folder):
    """(rel, line, stage, records) for every `jev enabled STAGE` in a folder.

    `records` says whether `--record` sits in the same call. A gate without
    it costs nothing and counts nothing: the caller's own mechanism runs and
    no row anywhere says it did.
    """
    units = folder_files(folder)
    named = constants(units)
    found = []
    for rel, rows in units:
        text, index = joined(rows)
        for match in GATE_RE.finditer(text):
            stage = named.get(match.group(1), match.group(1))
            if not stage.startswith("JEV_"):
                continue
            window = text[match.end():match.end() + RECORD_WINDOW]
            found.append((rel, line_at(index, match.start()), stage,
                          "--record" in window))
    return found


def fallback_before_verb(rel, rows, source=None):
    """Every call in one file that passes `--fallback` before its verb, as `rel:line (reader)`."""
    verbs = "|".join(VERBS)
    before = re.compile(
        rf"""--fallback\b(?:(?!\b(?:{verbs})\b).)*?["'\s,]({verbs})["'\s,]""")
    bad = []
    # Shell spells a call on one logical line, so a line scan is the right
    # reader for `.sh` once continuations are joined. Python does not, and
    # this used to be the only reader: `drive_intake.py` writes its argv one
    # element per line, so moving `--fallback` above the verb there passed,
    # because no single line held both.
    if rel.endswith(".sh"):
        bad += [f"{rel}:{number} (line)" for number, line in shell_calls(rows) if before.search(line)]
    if rel.endswith(".py"):
        for number, argv in argv_sequences(rel, source):
            if "--fallback" not in argv:
                continue
            verb = next((i for i, a in enumerate(argv) if a in VERBS), None)
            if verb is None:
                # Naming no verb, this is not argv: `argv.index(
                # "--fallback")` in a test reads the same otherwise.
                continue
            if argv.index("--fallback") < verb:
                bad.append(f"{rel}:{number} (argv)")
    return bad


def _reads_stdin(argv, flag):
    """Whether a literal argv hands `-` to `flag`, as two elements or as `flag=-`."""
    return any(a == f"{flag}=-" or (a == flag and i + 1 < len(argv) and argv[i + 1] == "-")
               for i, a in enumerate(argv))


def stdin_twice(rel, rows, source=None):
    """Every call in one file that hands `-` to both `--questions` and `--state`, as `rel:line (reader)`."""
    both = re.compile(
        r"""--questions["']?(?:\s*[=,]\s*|\s+)["']?-["'\s,]"""
        r"""(?:(?!--state).)*--state["']?(?:\s*[=,]\s*|\s+)["']?-["'\s,)]""")
    lines = shell_calls(rows) if rel.endswith(".sh") else rows
    bad = [f"{rel}:{number} (line)" for number, line in lines if both.search(line)]
    # A Python argv puts `"--questions", "-",` and `"--state", "-"` on two
    # lines as readily as on one, and no single line then holds both.
    if rel.endswith(".py"):
        bad += [f"{rel}:{number} (argv)" for number, argv in argv_sequences(rel, source)
                if _reads_stdin(argv, "--questions") and _reads_stdin(argv, "--state")]
    return bad


class TheInventory(unittest.TestCase):
    """Who calls Jev is read from the filesystem, never from a list."""

    def setUp(self):
        self.found = caller_folders()

    def test_every_caller_folder_is_one_this_file_knows(self):
        surprises = sorted(set(self.found) - KNOWN_CALLERS - NOT_CALLERS)
        detail = "; ".join(
            f"{folder} (first at {self.found[folder][0][0]}:{self.found[folder][0][1]})"
            for folder in surprises
        )
        self.assertEqual(
            surprises, [],
            "a folder names jev and no row here says so -- add it to "
            f"KNOWN_CALLERS and read the rules, or to NOT_CALLERS with a reason: {detail}",
        )

    def test_every_known_caller_still_calls(self):
        gone = sorted(KNOWN_CALLERS - set(self.found))
        self.assertEqual(
            gone, [],
            "KNOWN_CALLERS names a folder that no longer mentions jev; "
            f"drop the row deliberately rather than leaving it to rot: {gone}",
        )

    def test_the_inventory_is_not_empty(self):
        """The one way this whole file passes while measuring nothing."""
        self.assertGreaterEqual(
            len(set(self.found) & KNOWN_CALLERS), len(KNOWN_CALLERS),
            "discovery found fewer callers than are known to exist, so every "
            "rule below ran on a corpus smaller than the real one",
        )


class EveryCallerHasADegradePath(unittest.TestCase):
    """A caller either asks whether Jev is on, or supplies its own answer."""

    def test_each_caller_gates_on_enabled_or_passes_a_fallback(self):
        missing = []
        for folder in sorted(KNOWN_CALLERS):
            rows = folder_code(folder)
            gates = any(re.search(r"""["'\s]enabled\b""", line) for _, _, line in rows)
            falls = bool(fallback_tokens(folder_files(folder)))
            if not gates and not falls:
                missing.append(folder)
        self.assertEqual(
            missing, [],
            "a caller neither gates on `jev enabled` nor passes `--fallback`, "
            "so an unkeyed machine reaches the request and fails there instead "
            f"of degrading: {missing}",
        )


class AFallbackNeverReadsAsAJudgment(unittest.TestCase):
    """The three ways a supplied answer can be mistaken for a measured one."""

    def test_no_fallback_token_is_a_word_the_tool_itself_prints(self):
        bad = []
        for folder in sorted(KNOWN_CALLERS):
            for rel, number, token in fallback_tokens(folder_files(folder)):
                if token.lower() in RESERVED:
                    bad.append(f"{rel}:{number} --fallback {token}")
        self.assertEqual(
            bad, [],
            "a fallback is spelled as a word the tool prints on its own "
            f"({', '.join(RESERVED)}), so a pass that judged nothing is "
            f"indistinguishable from one that judged: {bad}",
        )

    def test_no_fallback_token_is_a_name_its_own_criteria_offers(self):
        bad = []
        for folder in sorted(KNOWN_CALLERS):
            units = folder_files(folder)
            offered = {name for _, _, name in criteria_keys(units)}
            for rel, number, token in fallback_tokens(units):
                if token in offered:
                    bad.append(f"{rel}:{number} --fallback {token}")
        self.assertEqual(
            bad, [],
            "a fallback token is one of the answers its own --criteria "
            f"offers, so the two cannot be told apart: {bad}",
        )

    def test_the_criteria_rule_is_never_silently_dormant(self):
        """The rule above compares nothing today, and that must be on purpose.

        `--criteria` is `choice`'s alone, and the comparison needs both sides
        as literals in the same folder. No folder has both: `drive_intake.py`
        builds its criteria from the routes its conf defines, so the names it
        offers do not exist until it runs. The comparison therefore reads an
        empty set and passes -- which is this file's own subject, one layer
        up. So say which folders it cannot reach and why, and fail on a new
        one. A caller that grows a literal `--criteria` leaves this list and
        starts being compared for real.
        """
        dormant = []
        for folder in sorted(KNOWN_CALLERS):
            units = folder_files(folder)
            if not fallback_tokens(units):
                continue
            if not any("choice" in line for _, _, line in folder_code(folder)):
                continue  # noul and score offer no criteria to collide with
            if criteria_keys(units):
                continue  # the comparison ran against something
            dormant.append(folder)
        self.assertEqual(
            sorted(RUNTIME_CRITERIA), dormant,
            "a folder passes --fallback to `choice` but offers no literal "
            "--criteria, so the collision rule reads an empty set there and "
            "passes without comparing. Record it in RUNTIME_CRITERIA with the "
            f"reason, or give it a literal criteria: {dormant}",
        )

    def test_a_fallback_is_never_passed_before_the_verb(self):
        """`--fallback` is a subparser argument and dies with exit 2 before it.

        This is the `jev ask` bug of #460 in the shape a caller can carry: an
        argv that puts the flag first parses nowhere, and the caller sees a
        failure it will read as the service being down.
        """
        bad = []
        for folder in sorted(KNOWN_CALLERS):
            for rel, rows in folder_files(folder):
                bad += fallback_before_verb(rel, rows)
        self.assertEqual(
            bad, [],
            "--fallback is passed before the verb; it is a subparser "
            f"argument and argparse exits 2 before the verb is read: {bad}",
        )


class ALocalOnlyCallerNeverAsksAHostedModel(unittest.TestCase):
    """A folder in LOCAL_ONLY_CALLERS passes `--local-only` on every call."""

    def test_every_call_passes_local_only(self):
        bad, calls = [], 0
        for folder in sorted(LOCAL_ONLY_CALLERS):
            for rel, rows in folder_files(folder):
                for number, text in shell_calls(rows):
                    if re.search(r"""(?:\bjev|["']\$\{?JEV\}?["'])\s+(?:noul|choice|score|ask|enabled)\b""", text):
                        calls += 1
                        if "--local-only" not in text:
                            bad.append(f"{rel}:{number}")
        self.assertGreater(calls, 0, "no call found, so the rule checked nothing")
        self.assertEqual(bad, [], f"a call without --local-only: {bad}")


class OnlyOneOptionReadsStdin(unittest.TestCase):
    """`--state` defaults to `-`, so piping into `--questions -` is refused."""

    def test_no_call_hands_a_dash_to_both_questions_and_state(self):
        bad = []
        for folder in sorted(KNOWN_CALLERS):
            for rel, rows in folder_files(folder):
                bad += stdin_twice(rel, rows)
        self.assertEqual(
            bad, [],
            "both --questions and --state read stdin in one call; the tool "
            f"refuses it, and before it did it sent an empty state: {bad}",
        )


class TheReadersSeeACallAcrossLines(unittest.TestCase):
    """The two order rules above, on calls spelled the way the tree spells them.

    The tree passes both rules today, so a reader that went blind would pass
    too. These hand each reader a call that breaks its rule across lines.
    """

    @staticmethod
    def rows(text):
        return list(enumerate(text.split("\n"), start=1))

    @staticmethod
    def site(rel, reader):
        """What a rule reports for a call that starts on the first line."""
        return f"{rel}:{1} ({reader})"

    def test_a_python_argv_over_two_lines_that_hands_stdin_to_both(self):
        source = 'argv = ["jev", "ask", "--questions", "-",\n        "--state", "-"]\n'
        self.assertEqual(stdin_twice("x.py", self.rows(source), source), [self.site("x.py", "argv")])
        source = 'argv = ["jev", "ask", "--questions=-",\n        "--state=-"]\n'
        self.assertEqual(stdin_twice("x.py", self.rows(source), source), [self.site("x.py", "argv")])

    def test_a_shell_call_continued_over_two_lines_that_hands_stdin_to_both(self):
        rows = self.rows('jev ask --questions - \\\n  --state - --caller x')
        self.assertEqual(stdin_twice("x.sh", rows), [self.site("x.sh", "line")])

    def test_a_shell_call_continued_over_two_lines_with_the_fallback_first(self):
        rows = self.rows('"$JEV" --fallback desk \\\n  choice "Which?" --criteria "a=b"')
        self.assertEqual(fallback_before_verb("x.sh", rows), [self.site("x.sh", "line")])

    def test_a_call_that_keeps_the_rules_passes_them(self):
        rows = self.rows('jev ask --questions "$q" \\\n  --state - --caller x')
        self.assertEqual(stdin_twice("x.sh", rows), [])
        rows = self.rows('"$JEV" choice "Which?" \\\n  --fallback desk')
        self.assertEqual(fallback_before_verb("x.sh", rows), [])
        source = 'argv = ["jev", "noul", "q", "--state", "-",\n        "--fallback", "off"]\n'
        self.assertEqual(stdin_twice("x.py", self.rows(source), source), [])
        self.assertEqual(fallback_before_verb("x.py", self.rows(source), source), [])


class EveryCallerNamesItselfInTheLedger(unittest.TestCase):
    """A row filed under `unknown` counts toward nothing.

    `jev` records one row per judgment, and the per-stage report in
    `local-sd-db/sd_db/judgment.py` groups by stage and arm. A caller that
    passes neither `--caller` nor `--stage` still gets its row -- under
    `unknown`, beside every other unnamed caller's, in a block no reader can
    attribute and no arm can be compared against. That is the shape this
    class bans, and it is the shape every caller had the day the ledger
    landed: the schema shipped, the recorder shipped, and no caller was
    touched.

    Read off the filesystem, like the inventory above: the stage a folder
    must name is the one it already hands `jev enabled`, not one written
    down here. One name across the switch, the ledger and the report, or the
    two arms of a decision file under two stages and compare nothing.
    """

    def test_each_caller_passes_its_own_folder_name_as_the_caller(self):
        missing = []
        for folder in sorted(KNOWN_CALLERS):
            names = {name for _, _, name in
                     flag_values(folder_files(folder), CALLER_RE)}
            if folder not in names:
                missing.append(f"{folder} passes {sorted(names) or 'nothing'}")
        self.assertEqual(
            missing, [],
            "a caller does not pass `--caller <its own folder name>`, so its "
            "rows file under `unknown` and the report cannot say who asked: "
            f"{missing}",
        )

    def test_each_caller_passes_the_stage_its_own_switch_reads(self):
        missing = []
        for folder in sorted(KNOWN_CALLERS):
            wanted = {stage for _, _, stage, _ in gates(folder)}
            passed = {name for _, _, name in
                      flag_values(folder_files(folder), STAGE_RE)}
            for stage in sorted(wanted - passed):
                missing.append(f"{folder} gates on {stage} and never passes it")
        self.assertEqual(
            missing, [],
            "a caller gates on one stage name and records under another (or "
            "under none), so the judgment and the old path it is meant to be "
            f"compared against land in two different blocks: {missing}",
        )

    def test_every_gate_that_can_decline_records_the_decline(self):
        """`enabled` counts nothing unless it is asked to.

        Most runs of most of these callers end at the gate. Without
        `--record` the old mechanism runs and nothing anywhere says so, and a
        stage whose callers only ever decline has no number at all -- which
        is the one number `judgment.py`'s `GATES` exists to give it. It costs
        nothing extra: the row is written by the process the gate already
        starts.
        """
        silent = []
        for folder in sorted(KNOWN_CALLERS):
            for rel, number, stage, records in gates(folder):
                if not records:
                    silent.append(f"{rel}:{number} enabled {stage}")
        self.assertEqual(
            silent, [],
            "a gate declines without recording it, so the old path runs and "
            f"no row says it did: {silent}",
        )

    def test_the_gate_rule_is_never_silently_dormant(self):
        """Every known caller must be reached by the rule above.

        A gate spelled in a way `GATE_RE` cannot read would make that test
        pass by finding nothing, which is this file's own subject one layer
        up. So assert the corpus: each known caller gates, and every gate
        names a stage.
        """
        ungated = sorted(f for f in KNOWN_CALLERS if not gates(f))
        self.assertEqual(
            ungated, [],
            "a known caller has no gate this file can see, so the rule above "
            f"ran on a corpus smaller than the real one: {ungated}",
        )

    def test_a_cause_is_written_once_by_whoever_still_can(self):
        """Three cases, and the middle one is the only silent row.

        `judgment.py`'s `DECLINES` groups by stage and cause **across both
        arms**, with no deduplication and nothing tying the two rows of one
        decision together. So whether a caller passes `--decline` decides
        between a decline counted twice and a decline counted not at all,
        and which of those it is depends on whether `jev` got to write.

        **1. At the gate.** No call was made. `jev enabled --record` wrote
        the cause itself, from `why_unusable`, in the process the gate
        already started. The caller adds nothing, and `GATE_PRIMITIVE` keeps
        that row out of the decision counts.

        **2. After a call that returned.** A non-zero exit, an answer the
        caller cannot read: `jev` reached its flush and the cause is on its
        row. A caller that repeats it turns one decision into two declines --
        measured, on a scratch ledger: `('JEV_SD_PLAN', 'unavailable', 2)`
        for a single failed ask, against `('JEV_SD_PLAN', 'unavailable', 1)`
        without the flag.

        **3. After a call the caller killed.** `jev` flushes after the answer
        is printed and installs no signal handler, so a call ended by a
        deadline writes nothing at all. Measured the same way: a SIGTERM
        mid-request leaves `0` rows, and `local-notify` timing out twice left
        `declines {}` with two `baseline/ok` rows carrying no cause. Here the
        caller's row is the only record there will ever be, and the cause
        belongs on it.

        So: a folder that can kill `jev` must name a cause somewhere, and a
        folder that always waits for it must not. The corpus is every folder
        in `KNOWN_CALLERS`, read off the filesystem, so a caller added later
        lands in one case or the other without a list to edit.

        The rule is folder-level and says so. `deadlines` cannot tell which
        branch a timeout fires on, only that one exists; the per-branch facts
        are pinned in each caller's own suite, where the argv is visible.

        `local-drive-intake` is in neither case and is skipped by
        `records_baseline`: it writes no control-arm row anywhere, because
        `jev_route` runs once per unmatched path and a `jev record`
        subprocess per path is the cost that shape exists to avoid. Its
        timeouts are unrecorded, which is a known gap and not this rule's.
        """
        wrong = []
        for folder in sorted(KNOWN_CALLERS):
            if not records_baseline(folder):
                continue
            named = flag_sites(tool_files(folder), DECLINE_RE)
            killed = deadlines(folder)
            if killed and not named:
                where = ", ".join(f"{rel}:{n}" for rel, n in killed[:3])
                wrong.append(f"{folder} kills jev ({where}) and names no "
                             "cause, so a killed call is recorded by nobody")
            if not killed and named:
                where = ", ".join(f"{rel}:{n}" for rel, n in named)
                wrong.append(f"{folder} waits for jev yet passes --decline "
                             f"({where}), so one decision counts as two")
        self.assertEqual(wrong, [], "; ".join(wrong))

    def test_the_cause_rule_is_never_silently_dormant(self):
        """Both halves of the rule above must have something to stand on.

        A corpus where nothing kills `jev`, or where nothing waits for it,
        would leave one half passing on the empty set -- the same failure
        `test_the_gate_rule_is_never_silently_dormant` guards against for
        gates. Assert that both cases are populated.
        """
        arms = [f for f in sorted(KNOWN_CALLERS) if records_baseline(f)]
        killers = [f for f in arms if deadlines(f)]
        waiters = [f for f in arms if not deadlines(f)]
        self.assertTrue(
            killers, "no caller kills jev, so case 3 of the rule above "
                     "checked nothing at all")
        self.assertTrue(
            waiters, "every caller kills jev, so case 2 of the rule above "
                     "checked nothing at all")

    def test_every_name_satisfies_the_grammar_the_ledger_enforces(self):
        """A name the ledger refuses is dropped to `unknown`, not raised.

        `jev.named` holds every `--caller` and `--stage` to `IDENTIFIER`
        before the row is built, precisely so a bad name loses one field
        rather than the whole row. The cost is that a typo is invisible: the
        call succeeds, the row lands, and it says `unknown`. Check the names
        here instead, where a typo is a failure with a file and a line.
        """
        bad, checked = [], []
        for folder in sorted(KNOWN_CALLERS):
            units = folder_files(folder)
            names = 0
            for flag, pattern in (("--caller", CALLER_RE), ("--stage", STAGE_RE)):
                for rel, number, name in flag_values(units, pattern):
                    names += 1
                    if len(name) > MAX_NAME or not IDENTIFIER.match(name):
                        bad.append(f"{rel}:{number} {flag} {name}")
            if not names:
                checked.append(folder)
        # Said first, because a rule with nothing to compare passes. Before
        # the callers were wired, every one of them passed both flags nowhere
        # and this read as nine clean folders.
        self.assertEqual(
            checked, [],
            "a caller passes neither --caller nor --stage anywhere, so this "
            f"rule read an empty set there and checked nothing: {checked}",
        )
        self.assertEqual(
            bad, [],
            "a name fails the grammar `local-sd-db/sd_db/judgment.py` "
            "enforces, so `jev` drops it and the row files under `unknown` "
            f"with nothing said: {bad}",
        )


class TheWorkflowRunsThisFile(unittest.TestCase):
    """Deleting the preflight line must fail here, not go quietly green."""

    def test_the_preflight_names_this_file(self):
        workflow = (ROOT / "tests/ci-native.sh").read_text(encoding="utf-8")
        wanted = re.compile(r"^\s*python3 tests/test_jev_contract\.py\s*$", re.MULTILINE)
        # assertTrue rather than assertRegex: a failing assertRegex prints the
        # whole script, and nobody reads three hundred lines of shell to find
        # out that one line is missing.
        self.assertTrue(
            wanted.search(workflow),
            "the tests/ci-native.sh preflight no longer runs this file, so none of "
            "these rules is enforced anywhere; add the line "
            "`python3 tests/test_jev_contract.py` beside the citations gate",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
