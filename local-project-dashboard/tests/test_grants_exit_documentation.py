"""sd:856. Every document that names an exit status of `dashboard.sh grants` names every route to it.

sd:845 added a second way for the verb to reach 0 -- a control that ran out
its wait is read as one kept out -- and updated the README and the root
`CLAUDE.md`, but not the verb's own `--help`, which went on saying 0 came
"only when both hold the grant". That is the unsafe direction: a reader who
trusts `--help` reads a 0 as proof both interpreters hold the grant. Fixing
the sentence alone recreates the defect the next time one side moves, so this
pins the documents against the code and so against each other.

What is pinned is the SET OF ROUTES TO EACH EXIT STATUS, not the prose. The
routes are named conditions, and a document claims one by saying it in any
words that carry the same concepts, so the three documents here -- a help
screen, a README bullet and a TCC rules bullet, none of which share a
sentence -- all satisfy the same signatures. `RewordingIsNotADifference`
below holds a paraphrase of the help sentence that must still read as the
same set, and `TruncationIsADifference` holds the pre-fix help sentence
verbatim, which must not.

The two control answers that are easiest to confuse are the reason the route
names read the way they do. `collectors.control_listing` returns '' from
three arms, not one: `Popen` raised, so the control never started;
`communicate` raised, so it started and the call failed; or it came back
nonzero with stderr that names neither `Operation not permitted` nor
`Permission denied`, so it ran and said something else. Two of those three
did run, and all three land on 3. `waited` is a fourth answer and lands on 0,
because running out the whole wait is how a read nobody can grant refuses.
So "the control did not run" describes one arm of one answer and is not a
name for either route:

    control_inconclusive -> 3    the control came back with nothing that
                                 settles it, or never started at all
    control_timed_out    -> 0    the control was still waiting when its
                                 timeout ran out

`AmbiguityIsADifference` holds the wording this file replaced, where 3 was
"when the control did not run at all" and 0 was "when the control answered
nothing at all". Those read as one sentence and route to different statuses,
which is a sharper form of the defect sd:856 exists to fix, and neither
phrase claims a route now.

The authoritative set is measured from `sd_tile.vault_grants` rather than
written down here: each of `collectors.control_listing`'s four answers is
driven through the real function and the exit status it produces is recorded.
`test_every_control_answer_in_the_code_has_a_route_name` fails when the code
grows a fifth answer that no document has been taught to name.

A document may leave a status out entirely -- `.claude/rules/macos-tcc.md` says nothing about
exit 1 -- but a document that names a status must name every route to it.
Naming some of them is what made `--help` misread.

A status can also be characterised instead of conditioned, and that is a
second rule (sd:875). `--help` closed with "Run it from launchd, where a
child's access is its own, for a conclusive 0", which names no route: it adds
nothing to the set compared above and so passes it, while telling a reader
that any 0 is conclusive, which the second route to 0 makes false.
`ConclusivenessNeedsACondition` below holds that a span calling a status
conclusive states a condition in the same span.
"""
import ast
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import sd_tile

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parent

# The verb, as prose names it. The help screen names it differently -- it is
# the entry in its own usage list -- so that document has its own test below.
VERB = re.compile(r"`?dashboard\.sh grants`?")

# A sentence ends at `.`, `!` or `?` followed by whitespace. Identifiers that
# carry an inner dot (`sd_tile.py`, `.env`) are not followed by whitespace and
# so do not split.
SENTENCE = re.compile(r"(?<=[.!?])\s+")

# Which `control_listing` answer each route is. The names are this file's
# vocabulary; which exit status each reaches is measured, not asserted here.
# '' is three arms of that function and not one (the module docstring), so its
# route is named for what the answer settles and not for what happened.
#
# The answers themselves are not this file's to list. `control_answers` below
# reads them off `control_listing`'s own `return` statements, and
# `test_every_control_answer_in_the_code_has_a_route_name` holds this map
# equal to that set. Before that, the map was the only copy of the answer set
# and drove every route measurement itself: a fifth answer that `sd_tile`
# never learned a line for -- silent, like `refused` -- would have reached
# some exit status with no route name, no document, and no failing test
# (review-381).
CONTROL_ROUTE = {
    "refused": "both_granted",
    "waited": "control_timed_out",
    "listed": "caller_reached",
    "": "control_inconclusive",
}
# The route that outranks the control: a probed path that cannot read.
REFUSAL_ROUTE = "refusal"


def control_answers():
    """Every value `collectors.control_listing` can return, read from its source.

    Measured from the function's `return` statements rather than written
    down, so an answer added to the function is in this set whether or not
    anything else was taught about it. A `return` of something other than a
    string literal is refused here: the set cannot then be enumerated, and a
    guard that guesses at it pins nothing.
    """
    module = ast.parse((HERE / "collectors.py").read_text(encoding="utf-8"))
    functions = [node for node in ast.walk(module)
                 if isinstance(node, ast.FunctionDef) and node.name == "control_listing"]
    if len(functions) != 1:
        raise AssertionError(f"collectors.py defines control_listing {len(functions)} times")
    answers = set()
    for node in ast.walk(functions[0]):
        if not isinstance(node, ast.Return):
            continue
        if not (isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            raise AssertionError(f"control_listing returns a non-literal at line {node.lineno}; "
                                 "the answer set cannot be read from its source")
        answers.add(node.value.value)
    return answers

# One route, one signature: a tuple of concept groups, every group an
# alternation of ways to say that concept. A span claims the route when every
# group matches somewhere in it. Groups are the point -- they are what keeps
# this from demanding a sentence -- and the conjunction is what keeps it from
# passing on any prose that happens to mention an exit.
#
# `control_inconclusive` and `control_timed_out` share their first group and
# share no wording in their second, which is the property that makes a
# document distinguish them in words a reader can apply. Neither accepts "did
# not run": that is one arm of the inconclusive answer, and a document that
# offers it as the whole route has narrowed the route.
ROUTE_SIGNATURE = {
    "refusal": (
        r"\bpath\b|\bbinar|\binterpreter|\bprobe",
        r"cannot read|can(?:no|')t read|refus|denied|not an executable",
    ),
    "caller_reached": (
        r"\bshell\b|\bterminal\b|\bcaller\b|this process",
        r"reach|own access|lists the vault too|leak",
    ),
    "control_inconclusive": (
        r"\bcontrol\b",
        r"inconclusive|no conclusive|not conclusive|nothing conclusive|never started|did not start",
    ),
    "both_granted": (
        r"\bboth\b",
        r"\bhold|\bhas\b|\bhave\b",
        r"\bgrant",
    ),
    "control_timed_out": (
        r"\bcontrol\b",
        r"still waiting|timed out|timeout|deadline|out the clock|whole wait|left waiting|left hanging",
    ),
}


class Control:
    """The collectors `vault_grants` reads, with the probe and the control scripted.

    The same shape `test_grants.Stub` uses, kept separate so that file's
    fixtures stay free to move: what this one needs is only that the real
    `vault_grants` decides the exit status.
    """

    VAULT_CONTROL = "/bin/ls"
    VAULT_PROBE_SECONDS = 15

    def __init__(self, answers, control):
        self.answers, self.control = answers, control
        self.VAULT = Path("/vault-sd-856")

    def probe_vault(self, executable):
        return self.answers[executable]

    def vault_refusal(self, executable, answer):
        return "" if answer == "ok" else f"refused: grant {executable}"

    def control_listing(self):
        return self.control


def routes_from_the_code():
    """{exit status: {route name}} as `sd_tile.vault_grants` actually decides it."""
    routes = {}
    with tempfile.TemporaryDirectory(prefix="grants-doc-") as directory:
        paths = []
        for name in ("a", "b"):
            script = Path(directory) / name
            script.write_text("#!/bin/sh\necho ok\n")
            script.chmod(0o755)
            paths.append(str(script))
        named = [f"DASHBOARD_PYTHON={paths[0]}", f"SD_DASHBOARD_PYTHON={paths[1]}"]
        for answer in sorted(control_answers()):
            # A `KeyError` here is an answer the code can give that this file
            # has no name for; the guard test says so in words, but a run of
            # any other test lands here first and this is what it means.
            route = CONTROL_ROUTE[answer]
            for probes, claimed in (({p: "ok" for p in paths}, route),
                                    ({paths[0]: "denied", paths[1]: "ok"}, REFUSAL_ROUTE)):
                _, code = sd_tile.vault_grants(Control(probes, answer), named)
                routes.setdefault(code, set()).add(claimed)
    return routes


def status_pattern(statuses):
    """A bare status digit, not one inside an identifier.

    `python@3.13`, `sd:845`, `review-376 B1`, `96 rows` and `2026-09-14` all
    carry digits that are not exit statuses; each is ruled out by what sits
    against it rather than by a list of exceptions.
    """
    digits = "".join(sorted(str(status) for status in statuses))
    return re.compile(r"(?<![\w:@.\-])([%s])(?![\w@%%\-]|\.\d)" % digits)


def status_spans(block, status):
    """(exit status, span, sentence) for every exit status one block of prose names.

    A sentence naming one status is read whole, which is what carries a claim
    written the other way round ("a silent probe is a refusal and exit 1").
    A sentence naming several splits at them, each status taking the text up
    to the next one, which is what carries a list ("Exits 1 when ..., 3 when
    ..., and 0 when ..."); the first status also takes the text before it, so
    a lead-in still counts.
    """
    # Wrapping is not meaning: a help screen's column and a markdown bullet's
    # indent both put runs of spaces inside a phrase that is one phrase.
    for sentence in SENTENCE.split(re.sub(r"\s+", " ", block).strip()):
        hits = list(status.finditer(sentence))
        for index, hit in enumerate(hits):
            start = 0 if index == 0 else hit.end()
            end = hits[index + 1].start() if index + 1 < len(hits) else len(sentence)
            yield int(hit.group(1)), sentence[start:end], sentence


def routes_of(span):
    """{route name} the signatures find in one span."""
    return {route for route, groups in ROUTE_SIGNATURE.items()
            if all(re.search(group, span, re.IGNORECASE) for group in groups)}


def routes_in(block, status):
    """{exit status: {route name}} claimed by one block of prose."""
    claimed = {}
    for code, span, _ in status_spans(block, status):
        claimed.setdefault(code, set()).update(routes_of(span))
    return claimed


# Words that tell a reader the status settles what the verb set out to measure.
# A document may say that, but only of a condition it states in the same
# breath. "Run it from launchd ... for a conclusive 0" states none, so it says
# every 0 is conclusive, and the second route to 0 makes that false (sd:875).
# The route-set comparison cannot see this shape: a span that claims no route
# adds nothing to the set being compared, so it passes by contributing
# nothing. The bar below is one route and not the whole set -- it asks that
# the conclusiveness be attached to a condition, and the comparison already
# holds the document to naming the rest.
#
# A negation is not a claim, and `conclusive` sits inside `inconclusive`, which
# is the word both other documents use for the exit-3 control answer. So the
# first alternative takes a word boundary and refuses the three negations, or
# the rule fires on the sentence it exists to allow.
CONCLUSIVE = re.compile(
    r"(?<!no )(?<!not )(?<!never )\bconclusive|\bproof\b|\bprove|\bguarantee|\bsettles\b",
    re.IGNORECASE)


def unqualified_conclusiveness(block, status):
    """[(exit status, sentence)] for each span calling a status conclusive under no condition."""
    return [(code, sentence) for code, span, sentence in status_spans(block, status)
            if CONCLUSIVE.search(span) and not routes_of(span)]


def help_blocks(text):
    """The entries of the usage screen: each starts at two spaces and a word."""
    block = []
    for line in text.splitlines():
        if re.match(r"^\S|^ {2}\S", line):
            if block:
                yield "\n".join(block)
            block = []
        block.append(line)
    if block:
        yield "\n".join(block)


def markdown_blocks(text):
    """Paragraphs, top-level list items, headings and table rows."""
    block = []
    for line in text.splitlines():
        blank = not line.strip()
        if blank or re.match(r"^(?:[-*+] |#|\|)", line):
            if block:
                yield "\n".join(block)
            block = []
            if blank:
                continue
        block.append(line)
    if block:
        yield "\n".join(block)


def claiming_documents(status):
    """{label: block} for every prose block that names the verb and an exit status.

    Enumerated from the filesystem -- every `*.md` in the checkout, plus what
    `dashboard.sh --help` prints -- rather than from a list, because a list is
    the thing that goes stale when a fourth document starts making the claim.
    """
    found = {}
    help_text = subprocess.run(["sh", str(HERE / "dashboard.sh"), "--help"],
                               capture_output=True, text=True, check=True).stdout
    for block in help_blocks(help_text):
        if re.match(r"^ {2}grants\b", block) and status.search(block):
            found.setdefault("dashboard.sh --help", []).append(block)
    skip = {".git", "node_modules", ".venv", "venv", "site-packages", "__pycache__"}
    for path in sorted(ROOT.rglob("*.md")):
        if skip & set(path.relative_to(ROOT).parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not VERB.search(text):
            continue
        for block in markdown_blocks(text):
            if VERB.search(block) and status.search(block):
                found.setdefault(str(path.relative_to(ROOT)), []).append(block)
    return {label: "\n\n".join(blocks) for label, blocks in found.items()}


class Routes(unittest.TestCase):
    def setUp(self):
        self.code = routes_from_the_code()
        self.status = status_pattern(self.code)
        self.documents = claiming_documents(self.status)

    def test_the_verb_has_the_three_outcomes_the_route_names_describe(self):
        # The vocabulary above is exhaustive for what the code can return, so
        # a document is compared against the whole of it and not a slice.
        self.assertEqual(self.code, {1: {"refusal"},
                                     3: {"caller_reached", "control_inconclusive"},
                                     0: {"both_granted", "control_timed_out"}})

    def test_every_control_answer_in_the_code_has_a_route_name(self):
        # The answer set is `control_listing`'s `return` statements, read
        # from its source. Equality, not containment: an answer the code can
        # give with no route name would be documented by nobody, and a route
        # named for an answer the code cannot give is a claim about nothing.
        # `sd_tile`'s two maps are held inside that set too, since a line or
        # an exit for an answer the control never gives is the same dead
        # claim from the other side.
        answers = control_answers()
        self.assertEqual(answers, set(CONTROL_ROUTE))
        self.assertLessEqual(set(sd_tile.CONTROL_LINE) | set(sd_tile.CONTROL_UNMEASURED), answers)

    def test_the_inconclusive_line_does_not_claim_the_control_never_ran(self):
        # The line a reader sees at exit 3, which is the one surface this
        # file's prose comparison does not cover. '' is three arms of
        # `collectors.control_listing` and two of them did run, so saying the
        # control did not run is false for those two.
        self.assertNotIn("did not run", sd_tile.CONTROL_LINE[""])

    def test_the_documents_that_claim_an_exit_status_are_the_ones_expected(self):
        # Not a list to maintain: a new document making the claim is welcome,
        # and is held to the same rule by the test below. This fails when the
        # extraction stops finding the documents that do make it, which is how
        # a comparison quietly comes to pin nothing.
        self.assertLessEqual({"dashboard.sh --help", "local-project-dashboard/README.md", ".claude/rules/macos-tcc.md"},
                             set(self.documents))

    def test_every_document_that_names_a_status_names_every_route_to_it(self):
        for label, block in self.documents.items():
            claimed = routes_in(block, self.status)
            with self.subTest(document=label):
                self.assertTrue(claimed, f"{label} matched the status pattern but claims no status")
                self.assertEqual(claimed, {status: self.code[status] for status in claimed})

    def test_no_document_calls_a_status_conclusive_under_no_condition(self):
        # sd:875. The help screen named both routes to 0 and then closed with
        # "Run it from launchd ... for a conclusive 0", which tells the reader
        # the second route is not there. The sentence claims no route, so the
        # comparison above reads it as saying nothing at all.
        for label, block in self.documents.items():
            with self.subTest(document=label):
                self.assertEqual(unqualified_conclusiveness(block, self.status), [])

    def test_every_route_is_claimed_by_some_document(self):
        # The other half of the equality: a signature that matches nothing
        # would let a document drop that route without a failure, so each one
        # has to be carrying its weight somewhere.
        claimed = set()
        for block in self.documents.values():
            for routes in routes_in(block, self.status).values():
                claimed |= routes
        self.assertEqual(claimed, set().union(*self.code.values()))


HELP_BEFORE_SD_856 = (
    "Exits 1 when a path cannot read, 3 when the answers were not measured as "
    "the binaries' own -- this shell's Documents access reached them, or the "
    "control did not run -- and 0 only when both hold the grant. Run it from "
    "launchd, where a child's access is its own, for a conclusive 0")

HELP_SD_856_FIRST_ROUND = (
    "Exits 1 when a path cannot read, 3 when the answers were not measured as "
    "the binaries' own -- this shell's Documents access reached them, or the "
    "control did not run -- and 0 when both hold the grant, or when the "
    "control answered nothing at all and so counts as one kept out. Run it "
    "from launchd, where a child's access is its own, for a conclusive 0")

HELP_REWORDED = (
    "Exit 1 if either interpreter is refused the vault, 3 if the answers were "
    "never measured as those binaries' -- this terminal's own Documents access "
    "reached them, or the control result was inconclusive -- and 0 if both "
    "hold the grant, or if the control ran out the clock, which is how an "
    "unanswerable prompt refuses.")


HELP_AFTER_SD_875 = (
    "Run it from launchd, where a child's access is its own, for a conclusive "
    "0 when both hold the grant")


class TruncationIsADifference(unittest.TestCase):
    """The pre-sd:856 help sentence, so the comparison cannot go loose unnoticed."""

    def test_the_sentence_sd_856_replaced_claims_one_route_to_zero(self):
        status = status_pattern({0, 1, 3})
        self.assertEqual(routes_in(HELP_BEFORE_SD_856, status),
                         {1: {"refusal"}, 3: {"caller_reached"}, 0: {"both_granted"}})


class AmbiguityIsADifference(unittest.TestCase):
    """The first-round sd:856 wording, which named both routes to 0 but not apart.

    Its exit 3 read "when the control did not run at all" and its exit 0 read
    "when the control answered nothing at all". A reader takes those for the
    same sentence, and they route to different statuses -- the defect sd:856
    exists to fix, in a sharper form. Neither phrase claims a route now, so
    replacing one of them with the other cannot pass.
    """

    def test_neither_half_of_the_ambiguous_pair_claims_a_control_route(self):
        status = status_pattern({0, 1, 3})
        claimed = routes_in(HELP_SD_856_FIRST_ROUND, status)
        self.assertNotIn("control_inconclusive", claimed.get(3, set()))
        self.assertNotIn("control_timed_out", claimed.get(0, set()))

    def test_the_two_control_routes_share_no_wording(self):
        # The property that makes them tellable apart: a phrase that claims
        # one must not claim the other, whatever else the sentence says.
        inconclusive = ROUTE_SIGNATURE["control_inconclusive"][1]
        timed_out = ROUTE_SIGNATURE["control_timed_out"][1]
        for phrase in inconclusive.split("|"):
            self.assertIsNone(re.search(timed_out, phrase, re.IGNORECASE), phrase)
        for phrase in timed_out.split("|"):
            self.assertIsNone(re.search(inconclusive, phrase, re.IGNORECASE), phrase)


class RewordingIsNotADifference(unittest.TestCase):
    """A rewrite of the same claims, so the comparison cannot go literal unnoticed."""

    def test_the_same_routes_said_in_other_words_read_the_same(self):
        status = status_pattern({0, 1, 3})
        self.assertEqual(routes_in(HELP_REWORDED, status),
                         {1: {"refusal"}, 3: {"caller_reached", "control_inconclusive"},
                          0: {"both_granted", "control_timed_out"}})


class ConclusivenessNeedsACondition(unittest.TestCase):
    """sd:875. The trailing sentence, which the route-set comparison cannot see."""

    def test_the_trailing_sentence_sd_875_replaced_is_caught(self):
        status = status_pattern({0, 1, 3})
        for fixture in (HELP_BEFORE_SD_856, HELP_SD_856_FIRST_ROUND):
            with self.subTest(fixture=fixture[-40:]):
                self.assertEqual([code for code, _ in
                                  unqualified_conclusiveness(fixture, status)], [0])

    def test_the_route_set_comparison_does_not_see_it(self):
        # Why this rule exists rather than the route-set one being widened:
        # the trailing sentence claims no route, so striking it out changes
        # nothing in the set `routes_in` compares.
        status = status_pattern({0, 1, 3})
        without = HELP_SD_856_FIRST_ROUND.partition("Run it from launchd")[0]
        self.assertEqual(routes_in(HELP_SD_856_FIRST_ROUND, status),
                         routes_in(without, status))

    def test_a_negated_conclusiveness_is_not_a_claim(self):
        # `inconclusive` carries `conclusive` inside it, and `no conclusive`
        # and `not conclusive` say the opposite of what this rule looks for.
        # Without the boundary and the negations, a span saying exit 3 is
        # inconclusive and naming no route reads as claiming 3 is conclusive,
        # which is the rule firing on the sentence it exists to allow.
        status = status_pattern({0, 1, 3})
        for sentence in ("Exit 3 is inconclusive.",
                         "Exit 3 is where the run gave no conclusive result.",
                         "Exit 3 is not conclusive."):
            with self.subTest(sentence=sentence):
                self.assertEqual(unqualified_conclusiveness(sentence, status), [])

    def test_conclusiveness_tied_to_a_condition_is_not_caught(self):
        # The other half. A sentence that says which 0 is the conclusive one
        # is the point of the rule, not a casualty of it -- this is the shape
        # the README already ships -- so the rule must pass it.
        status = status_pattern({0, 1, 3})
        self.assertEqual(unqualified_conclusiveness(HELP_AFTER_SD_875, status), [])
        self.assertEqual(routes_in(HELP_AFTER_SD_875, status), {0: {"both_granted"}})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
