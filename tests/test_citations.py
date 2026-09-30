"""A citation into code names an anchor, never a line number (sd:828).

A line number is invalidated by any insertion above its target, and nothing
can check that the line it names still carries the claim the prose makes
about it: the pack's adjacency rule, ported unchanged, skipped all three of
the citations sd:825 was filed for (`no-adjacent-anchor`), and a rule that
only asks "is the cited line blank or a comment" catches two of the six
drifted sites this item counted and none of the next ones. So this gate does
what the pack ruled in sd:525 and what #369 did for one citation: it bans the
`path:line` shape into code and checks an anchor instead.

The corpus is every tracked `.md`, `.py` and `.sh`, asked of git. The pages
of a delivered `docs/work` item are records and are never asked to change:
their citations are carried in the ratchet under their own heading rather
than excluded by status, because the preflight cannot read a status -- the
checkout is shallow, so the closing trailers `sd-docs-lint` falls back to are
not in it, and the database is not there either. A rule this file could only
evaluate locally would print a different answer in CI, and the ratchet's
liveness test turns that difference into a red build. Three forms are
accepted, each checked against the file it names:

1. A backticked snippet: `` `<snippet>`, in `<path>` `` or
   `` `<snippet>`, in `<symbol>` of `<path>` ``, with no line number. Every
   backticked span before the path must occur word-bounded in the file, so a
   rename that leaves the old name as a prefix of the new one still fails.
   The path resolves from the repository root, then beside the citing file,
   then, when it has no directory part, as a filename exactly one tracked
   file carries; the file it lands on must be tracked, because the CI
   checkout holds nothing else.
2. `source:<path>::<symbol>`: a `.py` target declares the symbol exactly once
   at module level or one level into a class, read by `ast`; a `.sh` target
   holds a `<symbol>() {` or `<symbol>=` line.
3. `[quoted: <reason>]` on the same line after an example token, for a page
   that has to spell the banned shape out to explain it. The reason is free
   text and must be there; an exemption nobody has to justify is a silencer.

The sites that predate the gate are carried in `KNOWN_LINE_INTO_CODE`, a
ratchet keyed by the citing file and line, and each key must still be live,
so the list only shrinks. `KNOWN_WRONG_OWNER` held the two sites on sd:438's
open design page for that item to repoint, reported on a line of their own
without failing; sd:438 repointed both to form 1 and the set is empty.

Every read stays inside the checkout: a corpus path, a form-1 target and a
form-2 target are each resolved first, and one that a symlink carries out of
the tree, or that is not a regular file, is not read (sd:963). `git ls-files`
constrains the name of a tracked symlink and nothing about its target.

Stdlib and git only, on purpose: this runs in the `tests/ci-native.sh`
preflight right after `sd-docs-lint`, before the virtualenv exists, once
per `make check` and before any leg. `python3 tests/test_citations.py` from the repository root is the
whole invocation, and a test here fails when `tests/ci-native.sh` stops
naming it.
"""

import ast
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
#: The native suites' script, which the workflow and `make check` both run.
WORKFLOW = "tests/ci-native.sh"
WORK_DIR = "docs/work"

#: The extensions a citation "into code" names. Markdown targets are
#: `sd-docs-lint` rule 6's; this gate never resolves one.
CODE_SUFFIXES = r"(?:py|sh|js|ya?ml)"
PATH_CHARS = r"[A-Za-z0-9_./-]"

#: A `path:line` or `path:start-end` into code. The look-behind keeps a token
#: from starting mid-path; the look-ahead keeps `:12` from being read out of
#: `:123`.
TOKEN = re.compile(
    rf"(?<!{PATH_CHARS})(?P<path>{PATH_CHARS}+\.{CODE_SUFFIXES}):"
    r"(?P<start>\d+)(?:-(?P<end>\d+))?(?!\d)"
)

#: Form 3. Every line terminator the grammar names, not only `\n`, so a reason
#: cannot span a line and cover a token on the next one. A marker covers the
#: one token right before it on its line and any token inside its own reason.
MARKER = re.compile(r"\[quoted:[ \t]*(?P<reason>[^\]\s][^\]\n\r\u2028\u2029]*?)[ \t]*\]")

#: The same terminator set, for walking a file line by line: what MARKER
#: refuses to span is what ends a line, so a marker past one of these never
#: covers the token before it. CRLF is one terminator, so a `\n` or CRLF
#: file numbers as an editor shows it.
LINE_END = re.compile(r"\r\n|[\n\r\u2028\u2029]")

#: The line of the preflight that runs `sd-docs-lint`: the binary's path at the
#: start of a line, with or without `--pr-body` after it. The `test -f` that
#: checks the binary exists names the same path and is not an invocation.
LINT_INVOCATION = re.compile(r'^\s*"\$SD_ACCEPTANCE_PACK/bin/sd-docs-lint"(?:\s|$)')

#: Form 2. `.py` and `.sh` only: those are the two kinds of file this gate
#: knows how to find a declaration in.
SOURCE = re.compile(
    rf"source:(?P<path>{PATH_CHARS}+\.(?P<kind>py|sh))::(?P<symbol>[A-Za-z_][A-Za-z0-9_]*)"
)

#: Form 1. `\s+` rather than a space between the parts, because a docstring
#: wraps the sentence and the path lands on the next line, indented.
SNIPPET = re.compile(
    r"`(?P<snippet>[^`\n]+)`,?\s+in\s+(?:`(?P<within>[^`\n]+)`\s+of\s+)?"
    rf"`(?P<path>{PATH_CHARS}+\.{CODE_SUFFIXES})`"
)

#: The live `path:line` citations into code this gate was built over.
#:
#: A ratchet, not an exemption. Each entry is `(citing file, the line it is
#: written on, cited path as written, start, end)`, `end` equal to `start`
#: for a single line, and `test_every_carried_citation_is_still_live` fails
#: for any entry the corpus no longer holds at that key -- so an entry leaves
#: when its citation is repointed to a form above, or when its page moves
#: under it, and never arrives. Deleting this set is how the item finishes.
#:
#: Seeded at a24b6a78 with the 400 live sites: 352 on the five open
#: `docs/work` items (sd:754's page 202, sd:755's 86, sd:438's 29, sd:234's
#: 24, sd:235's 11), 22 on the delivered sd:460's page, which is a record
#: (sd:426's page, also delivered, carries none), and 26 in code and
#: READMEs. The four sites the item found pointing at the wrong line are not
#: here; they were repointed in the same change. The two on sd:438's page
#: were in `KNOWN_WRONG_OWNER` until sd:438 repointed them. Two left at
#: sd:963, the sites #402's own insertions into `system-native.yml` had
#: shifted, repointed to form 1. One left with the claude-mem provider
#: chain: `mem_pro_watchdog`'s `cron-jobs.sh` citation, repointed to form 1.
KNOWN_LINE_INTO_CODE = frozenset({
    # The delivered sd:460's page, a record: these leave when it is archived.
    # The open items' pages and the code, in path order.
    ("docs/work/2026-09-05-one-database-one-front-door/implement.md",
     122, "tests/test_sd_dashboard.py", 328, 328),
    ("docs/work/2026-09-05-one-database-one-front-door/implement.md",
     123, "tests/test_dashboard_actions.py", 292, 292),
    ("docs/work/2026-09-05-one-database-one-front-door/implement.md",
     124, "tests/test_sd_ledger.py", 183, 183),
    ("docs/work/2026-09-05-one-database-one-front-door/implement.md",
     186, "dashboard/store.py", 89, 89),
    ("docs/work/2026-09-05-one-database-one-front-door/implement.md",
     1159, "local-sd-db/sd_db/writes.py", 354, 354),
    ("docs/work/2026-09-05-one-database-one-front-door/implement.md",
     1160, "local-sd-db/sd_db/writes.py", 381, 381),
    ("docs/work/2026-09-05-one-database-one-front-door/implement.md",
     1161, "local-sd-db/sd_db/reads.py", 195, 195),
    ("docs/work/2026-09-05-one-database-one-front-door/implement.md",
     1162, "local-project-dashboard/sd_dashboard/screens.py", 163, 163),
    ("docs/work/2026-09-05-one-database-one-front-door/implement.md",
     1163, "local-sd-db/sd_db/reads.py", 817, 817),
    ("docs/work/2026-09-05-one-database-one-front-door/implement.md",
     1164, "local-project-dashboard/sd_dashboard/server.py", 269, 269),
    ("docs/work/2026-09-05-one-database-one-front-door/implement.md",
     1167, "local-sd-db/sd_db/reads.py", 212, 212),
    ("docs/work/2026-09-05-one-database-one-front-door/implement.md",
     1167, "local-sd-db/sd_db/reads.py", 240, 240),
    ("docs/work/2026-09-05-one-database-one-front-door/implement.md",
     1171, "local-sd-db/sd_db/brief.py", 133, 133),
    ("docs/work/2026-09-05-one-database-one-front-door/implement.md",
     1182, "local-project-dashboard/sd_dashboard/controls.py", 135, 135),
    ("docs/work/2026-09-05-one-database-one-front-door/implement.md",
     1223, "local-herdr/herdr_wrap.py", 149, 149),
    ("docs/work/2026-09-05-one-database-one-front-door/implement.md",
     1227, "local-herdr/herdr_wrap.py", 269, 269),
    ("docs/work/2026-09-05-one-database-one-front-door/implement.md",
     1228, "local-herdr/herdr_wrap.py", 227, 227),
    ("docs/work/2026-09-05-one-database-one-front-door/implement.md",
     1233, "local-herdr/herdr_wrap.py", 293, 293),
    ("docs/work/2026-09-05-one-database-one-front-door/implement.md",
     1236, "local-herdr/herdr_wrap.py", 365, 365),
    ("docs/work/2026-09-05-one-database-one-front-door/implement.md",
     1237, "local-herdr/herdr.sh", 36, 36),
    ("docs/work/2026-09-05-one-database-one-front-door/implement.md",
     1239, "local-herdr/herdr_wrap.py", 387, 387),
    ("local-msgsnap/tests/test_eintr_retry.py", 87, "eintr-retry.sh", 74, 75),
    ("local-project-dashboard/sd_dashboard/markup.py", 81, "charts.py", 150, 150),
    ("local-sd-db/sd-db.sh", 9, "sd_db/backup.py", 45, 45),
    ("local-sd-db/sd-db.sh", 15, "local-sd-plan/sd-plan.sh", 15, 15),
    ("local-sd-db/sd_db/runner_exec.py",
     314, "local-project-dashboard/sd_dashboard/server.py", 548, 548),
    ("local-sd-db/tests/support.py", 110, "sd_lib.py", 254, 254),
    ("local-sd-db/tests/test_brief.py",
     98, "local-project-dashboard/sd_dashboard/screens.py", 163, 163),
    ("local-sd-plan/README.md", 39, "local-sd-db/sd_db/runner.py", 53, 53),
    ("local-sd-plan/README.md", 41, "local-sd-db/sd_db/runner.py", 333, 333),
    ("local-sd-plan/README.md", 43, "local-sd-runner/sd_runner/gitops.py", 160, 160),
    ("local-sd-plan/README.md", 66, "local-sd-runner/sd_runner/runtime.py", 650, 655),
    ("local-sd-plan/sd_plan.py", 194, "local-sd-runner/sd_runner/runtime.py", 650, 655),
})

#: Citations this gate knows point at the wrong line and does not own the
#: page of. Reported on a line of their own by `test_known_wrong_sites_are_
#: reported_for_their_owner`, never failed here, and each must still be live
#: exactly like a ratchet entry: the day its owner repoints one, the entry
#: goes. Empty since sd:438 repointed the two it held, lines 14 and 81 of
#: its design page, which cited lines 372 and 418 of
#: `local-cron-jobs/cron-jobs.sh` -- both mid-comment at a24b6a78 -- and now
#: cite `No overlapping runs of the same job` and `"$claude_bin" -p
#: "$JOB_PROMPT"` in `cmd_exec` of that file by snippet. Kept, empty, so a
#: wrong-line site another item owns has a place to go.
KNOWN_WRONG_OWNER = frozenset()


# ------------------------------------------------------------------- corpus


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout


def tracked() -> list[str]:
    """Every tracked path, once per process. Git's list, not a directory walk:
    a scratch file or an ignored build product is not a page this gate reads."""
    global _TRACKED
    if _TRACKED is None:
        _TRACKED = git("ls-files", "-z").split("\0")[:-1]
    return _TRACKED


_TRACKED: "list[str] | None" = None


def corpus(paths=None, root: Path = ROOT) -> list[str]:
    """Every tracked `.md`, `.py` and `.sh` that is a regular file inside the
    checkout. A page under `docs/work/archive/` is done by virtue of where it
    lives, as the pack rules, and is left out; any other `docs/work` page is
    in, delivered or not, for the reason the module docstring gives.

    `tracked()` constrains the name only: a tracked symlink is read through
    its target, which can sit outside the checkout or be a device that never
    returns. So each path is resolved, and one that escapes `root` or is not
    a regular file is left out as well (sd:963). A link whose target git does
    not track is a regular file here and dangles in the CI checkout, so the
    walk asks both ends, as `resolve` and `source_error` do, and leaves that
    one out too (sd:964)."""
    tracked_files = tracked_under(root, paths)
    files = []
    for path in tracked() if paths is None else paths:
        if not path.endswith((".md", ".py", ".sh")):
            continue
        if path.startswith(f"{WORK_DIR}/archive/"):
            continue
        resolved = regular_file_within(root, root / path)
        if resolved is None:
            continue
        if not held_by_ci(root, root / path, resolved, tracked_files):
            continue
        files.append(path)
    return files


def regular_file_within(root: Path, candidate: Path) -> "Path | None":
    """`candidate` resolved, when that lands on a regular file inside `root`;
    `None` for one that cannot be resolved, leaves the checkout through `..`
    or a symlink, or is a directory or a special file. The boundary every
    read in this file goes through."""
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        # A symlink loop: `OSError` in strict mode, and `RuntimeError` from
        # an interpreter older than 3.13, where the preflight never runs.
        # 3.13 resolves it as far as it goes and `is_file` answers no.
        return None
    if not resolved.is_relative_to(root.resolve()):
        return None
    if not resolved.is_file():
        return None
    return resolved


def tracked_under(root: Path, files) -> "list[str] | None":
    """The paths git tracks under `root`, relative to it: `files` when the
    caller names them, git's answer for the checkout, and `None`, meaning no
    constraint, for any other root -- a temp-directory fixture has no
    repository under it to ask."""
    if files is not None:
        return list(files)
    if root.resolve() == ROOT.resolve():
        return tracked()
    return None


def held_by_ci(root: Path, candidate: Path, resolved: Path, tracked_files) -> bool:
    """Whether the CI checkout holds `candidate`: its name, lexically under
    `root`, is tracked, and so is every symlink it goes through and the
    regular file it lands on. Git tracks a symlink by name and not by
    target, so an untracked local link to a tracked file is not in the
    checkout at all, and a tracked link to an untracked file dangles there;
    either passes a read here and fails in CI unless both ends are asked.
    A tracked link to an untracked link to a tracked file is the same
    dangle one hop in, so each hop is asked, not only the two ends (#410
    review). `None` for the list means no constraint."""
    if tracked_files is None:
        return True
    root_resolved = root.resolve()
    try:
        hops = [Path(os.path.normpath(candidate)).relative_to(os.path.normpath(root))]
        current = Path(os.path.normpath(candidate))
        while current.is_symlink():
            if len(hops) > MAX_LINK_HOPS:
                return False
            current = Path(os.path.normpath(current.parent / os.readlink(current)))
            hops.append((Path(os.path.realpath(current.parent)) / current.name)
                        .relative_to(root_resolved))
        hops.append(resolved.relative_to(root_resolved))
    except (OSError, ValueError):
        return False
    return all(str(hop) in tracked_files for hop in hops)


# The kernel gives up on a chain this long; a loop is refused before here by
# `regular_file_within`, and this bound keeps the walk finite regardless.
MAX_LINK_HOPS = 40


def line_of(text: str, offset: int) -> int:
    """The 1-based line `offset` sits on, counting the terminators `LINE_END`
    names, so a diagnostic numbers a CR or U+2028 file as `tokens_in` does."""
    return len(LINE_END.findall(text, 0, offset)) + 1


# ------------------------------------------------------------------- tokens


class Token:
    """One `path:line` into code, where it is written, and whether a
    `[quoted: ...]` marker right after it on its line, or around it, says it
    is an example."""

    __slots__ = ("citing", "line", "path", "start", "end", "quoted")

    def __init__(self, citing, line, path, start, end, quoted):
        self.citing, self.line, self.path = citing, line, path
        self.start, self.end, self.quoted = start, end, quoted

    @property
    def key(self):
        return (self.citing, self.line, self.path, self.start, self.end)

    def __str__(self):
        span = f"{self.start}" if self.start == self.end else f"{self.start}-{self.end}"
        return f"{self.citing}:{self.line} cites {self.path}:{span}"


def tokens_in(citing: str, text: str) -> list:
    found = []
    for number, line in enumerate(LINE_END.split(text), 1):
        markers = [m.span() for m in MARKER.finditer(line)]
        matches = list(TOKEN.finditer(line))
        # Covered by a marker that holds the token inside its own reason, or
        # by one that starts where the token ends or later with no other
        # token between them: a marker names one example, not the whole
        # line before it (sd:1175).
        covered = {i for i, match in enumerate(matches)
                   if any(at <= match.start() <= match.end() <= to for at, to in markers)}
        for at, _ in markers:
            before = [i for i, match in enumerate(matches)
                      if match.end() <= at and i not in covered]
            if before:
                covered.add(before[-1])
        for i, match in enumerate(matches):
            start = int(match.group("start"))
            end = int(match.group("end") or start)
            found.append(Token(citing, number, match.group("path"), start, end, i in covered))
    return found


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8", errors="replace")


def scan(files) -> list:
    found = []
    for path in files:
        found.extend(tokens_in(path, read(path)))
    return found


# ------------------------------------------------------------------ anchors


def word_bounded(anchor: str) -> "re.Pattern[str]":
    """`anchor` as a whole token, so a longer name containing it is not a hit.

    Plain `in` passed a mutation that renamed `cmd_exec` to `cmd_execute`:
    the old name is a prefix of the new one, so the substring survived a
    rename that is exactly the drift this gate exists to catch. The
    lookarounds are conditional on the edge character being a word character
    at all, because an anchor may begin or end in punctuation -- a shell
    snippet ends in a quote, and `\\b` against a quote asserts the opposite of
    what is wanted.
    """
    start = r"(?<!\w)" if anchor[:1].isalnum() or anchor[:1] == "_" else ""
    end = r"(?!\w)" if anchor[-1:].isalnum() or anchor[-1:] == "_" else ""
    return re.compile(start + re.escape(anchor) + end)


def resolve(citing: str, path: str, root: Path = ROOT, files=None) -> "Path | None":
    """Where a form-1 path points: from the root, then beside the citing file,
    then, for a bare filename, the one tracked file of that name. Nowhere
    outside the checkout, whatever the path says, and nowhere git does not
    track: an ignored local copy would pass here and fail in the shallow CI
    checkout, which holds only what is tracked. The filename fallback is for
    a name with no directory part; `src/foo.py` cited and gone is not found
    again as `other/src/foo.py` (sd:963)."""
    tracked_files = tracked_under(root, files)
    candidates = [root / path, (root / citing).parent / path]
    if "/" not in path and tracked_files is not None:
        named = [root / p for p in tracked_files if Path(p).name == path]
        if len(named) == 1:
            candidates.append(named[0])
    for candidate in candidates:
        resolved = regular_file_within(root, candidate)
        if resolved is None:
            continue
        if not held_by_ci(root, candidate, resolved, tracked_files):
            continue
        return resolved
    return None


def snippet_errors(citing: str, text: str) -> list:
    """Form 1: every backticked span before the path occurs in the file."""
    errors = []
    for match in SNIPPET.finditer(text):
        line = line_of(text, match.start())
        target = resolve(citing, match.group("path"))
        if target is None:
            errors.append(f"{citing}:{line} cites `{match.group('path')}`, "
                          "which is not one tracked file")
            continue
        body = target.read_text(encoding="utf-8", errors="replace")
        snippet, within = match.group("snippet"), match.group("within")
        if within:
            error = scoped_snippet_error(body, target.suffix, snippet, within)
            if error:
                errors.append(f"{citing}:{line} cites `{snippet}` in `{within}` of "
                              f"{match.group('path')}; {error} -- re-anchor the "
                              "citation on what the code says now")
            continue
        if not word_bounded(snippet).search(body):
            errors.append(
                f"{citing}:{line} cites `{snippet}` in {match.group('path')}, "
                "which no longer holds it -- re-anchor the citation on what "
                "the code says now"
            )
    return errors


def symbol_spans(body: str, suffix: str, symbol: str) -> "list | None":
    """The `(first, last)` lines `symbol` declares in a `.py` or `.sh` body,
    or `None` where this gate cannot bound them, which leaves the caller the
    file-wide check. A shell assignment is its own line, and so is a
    `name() { ...; }` function with no `#` on its line. A `name() {`
    function, with or without a comment after the brace, runs to a
    standalone `}` at its own indentation, provided no other line at that
    indentation or less comes first. Any other shell body -- a brace on the
    next line, `} >&2`, a subshell, a `#` inside a one-line body -- is `None` for the whole symbol: a guessed span
    either runs into the next function or finds nothing, and both are
    wrong answers where the file-wide check is only a weaker one."""
    if suffix == ".py":
        try:
            return declared_spans(ast.parse(body), symbol)
        except SyntaxError:
            return []
    if suffix != ".sh":
        return None
    lines = LINE_END.split(body)
    name = re.escape(symbol)
    opens = re.compile(rf"^\s*(?:function\s+{name}\b|{name}\s*\(\s*\))\s*(?P<rest>.*)$")
    spans = []
    for first in shell_declarations(body, symbol):
        declared = opens.match(lines[first - 1])
        if not declared:
            spans.append((first, first))
            continue
        rest = declared.group("rest").rstrip()
        # A `#` anywhere after the brace may start a comment, and a `}` in
        # a comment closes nothing, so such a line is never a one-liner.
        if rest.startswith("{") and rest.endswith("}") and len(rest) > 1 and "#" not in rest:
            spans.append((first, first))
            continue
        if not re.fullmatch(r"\{(?:\s+#.*)?", rest):
            return None
        indent = len(lines[first - 1]) - len(lines[first - 1].lstrip())
        last = None
        for number in range(first + 1, len(lines) + 1):
            text = lines[number - 1]
            if not text.strip() or text.lstrip().startswith("#"):
                continue
            if len(text) - len(text.lstrip()) <= indent:
                if text.strip() == "}" and len(text) - len(text.lstrip()) == indent:
                    last = number
                break
        if last is None:
            return None
        spans.append((first, last))
    return spans


def scoped_snippet_error(body: str, suffix: str, snippet: str, within: str) -> "str | None":
    """Form 1 with a symbol: the snippet must sit inside the symbol's own
    declaration, not anywhere in the file. Matching the two as independent
    substrings passed a snippet moved out of the function it was cited in,
    and a symbol renamed while a comment still mentioned it (sd:1235). A
    `.js` or `.yml` target has no declaration reader here, and a shell body
    `symbol_spans` cannot bound is not guessed at; both keep the file-wide
    check for both spans."""
    spans = symbol_spans(body, suffix, within)
    if spans is None:
        missing = [a for a in (snippet, within) if not word_bounded(a).search(body)]
        return f"the file no longer holds `{missing[0]}`" if missing else None
    if not spans:
        return f"nothing in the file declares `{within}`"
    lines = LINE_END.split(body)
    pattern = word_bounded(snippet)
    if any(pattern.search("\n".join(lines[first - 1:last])) for first, last in spans):
        return None
    return f"`{within}` no longer holds `{snippet}`"


def declared_spans(tree, symbol: str) -> list:
    """The `(first, last)` lines at which a parsed module declares `symbol`:
    module level, then one level into each class. A test method is a
    declaration a page cites by name as readily as a function is. Not deeper:
    a name bound inside a function body is a local with no stable identity."""
    bodies = [tree.body]
    bodies += [node.body for node in tree.body if isinstance(node, ast.ClassDef)]
    found = []
    for body in bodies:
        for node in body:
            span = (node.lineno, getattr(node, "end_lineno", None) or node.lineno)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                found += [span] * (node.name == symbol)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                found += [span] * sum(
                    isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store)
                    and name.id == symbol
                    for target in targets for name in ast.walk(target)
                )
    return found


def shell_declarations(text: str, symbol: str) -> list:
    """The lines of a shell file that declare `symbol`: `name() {` (or
    `function name`) and `name=`, the latter after an optional `export`,
    `readonly` or `local`. A use of the name, or a comment about it, is not
    a declaration and cannot keep a deleted one's citation green."""
    name = re.escape(symbol)
    pattern = re.compile(
        rf"^\s*(?:function\s+{name}\b|{name}\s*\(\s*\)|(?:export|readonly|local)\s+{name}=|{name}=)"
    )
    return [n for n, line in enumerate(LINE_END.split(text), 1) if pattern.match(line)]


def source_error(path: str, kind: str, symbol: str, root: Path = ROOT,
                 files=None) -> "str | None":
    """Form 2, resolved without reading outside the checkout. Each cause is
    named before the last one is assumed: a missing file, a directory, a file
    git does not track, a file that will not parse, and only then a symbol
    declared other than once. `files` is the tracked list for a root that is
    not the checkout; see `tracked_under`."""
    try:
        resolved = (root / path).resolve()
    except (OSError, RuntimeError) as error:
        return f"{path}: cannot resolve: {error}"
    if not resolved.is_relative_to(root.resolve()):
        return f"{path}: escapes the checkout"
    if not resolved.exists():
        return f"{path}: target is missing"
    if not resolved.is_file():
        return f"{path}: target is not a regular file"
    if not held_by_ci(root, root / path, resolved, tracked_under(root, files)):
        return f"{path}: is not tracked, or resolves to a file that is not, so the CI checkout will not hold it"
    try:
        text = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        return f"{path}: cannot be read: {error}"
    if kind == "py":
        try:
            declarations = declared_spans(ast.parse(text, filename=path), symbol)
        except SyntaxError as error:
            return f"{path}: does not parse as Python: {error}"
        if len(declarations) != 1:
            return (f"{path}::{symbol}: expected one declaration at module or "
                    f"class level, found {len(declarations)}")
        return None
    declarations = shell_declarations(text, symbol)
    if not declarations:
        return f"{path}::{symbol}: no `{symbol}() {{` or `{symbol}=` line declares it"
    return None


def source_errors(citing: str, text: str) -> list:
    errors = []
    for match in SOURCE.finditer(text):
        line = line_of(text, match.start())
        error = source_error(match.group("path"), match.group("kind"), match.group("symbol"))
        if error:
            errors.append(f"{citing}:{line} cites source:{match.group('path')}::"
                          f"{match.group('symbol')}; {error}")
    return errors


# -------------------------------------------------------------------- tests


def preflight_order_errors(text: str) -> list:
    """What is wrong with where the gate sits in a workflow text: it must be
    named once as a bare `python3 tests/test_citations.py`, after every line
    that invokes `sd-docs-lint` and before the virtualenv is built. The lint
    line is the invocation, `LINT_INVOCATION`, and not any line naming the
    binary's path: the `test -f` existence check names it too, and matching
    that kept the order test green with the invocation deleted (sd:963)."""
    lines = LINE_END.split(text)
    gate = [n for n, l in enumerate(lines) if l.strip() == "python3 tests/test_citations.py"]
    lint = [n for n, l in enumerate(lines) if LINT_INVOCATION.match(l)]
    venv = [n for n, l in enumerate(lines) if "python3 -m venv" in l]
    if len(gate) != 1:
        return [f"names `python3 tests/test_citations.py` {len(gate)} times, not once"]
    errors = []
    if not lint:
        errors.append("never invokes sd-docs-lint")
    elif max(lint) > gate[0]:
        errors.append("invokes sd-docs-lint after the gate, not before it")
    if not venv:
        errors.append("never builds the venv")
    elif min(venv) < gate[0]:
        errors.append("builds the venv before the gate, not after it")
    return errors


def describe(key) -> str:
    citing, line, path, start, end = key
    span = f"{start}" if start == end else f"{start}-{end}"
    return f"{citing}:{line} cites {path}:{span}"


class CorpusCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = corpus()
        cls.tokens = scan(cls.files)
        cls.by_key = {token.key: token for token in cls.tokens}

    def test_no_line_number_citation_into_code(self):
        """The gate. Every `path:line` into code is carried, held for its
        owner, or marked as an example; anything else names its citing line."""
        offenders = [
            str(token) for token in self.tokens
            if not token.quoted
            and token.key not in KNOWN_LINE_INTO_CODE
            and token.key not in KNOWN_WRONG_OWNER
        ]
        self.assertEqual(
            [], offenders,
            "cite code by an anchor, not a line number -- a backticked snippet "
            "`in` the file, `source:<path>::<symbol>`, or `[quoted: <reason>]` "
            "after an example (tests/test_citations.py):\n  " + "\n  ".join(offenders),
        )

    def moved(self, key) -> str:
        """Where a gone key's citation sits now, if the same token is still on
        its page at another line: an insertion above it moved it, and the
        repair is to re-key, not to remove. Empty when it really left."""
        citing, _, path, start, end = key
        lines = sorted(t.line for t in self.tokens
                       if (t.citing, t.path, t.start, t.end) == (citing, path, start, end)
                       and t.key not in KNOWN_LINE_INTO_CODE and t.key not in KNOWN_WRONG_OWNER)
        return f" -- now at line {', '.join(map(str, lines))}; re-key it" if lines else ""

    def test_every_carried_citation_is_still_live(self):
        """The ratchet only shrinks: a key the corpus no longer holds leaves.
        A page edited above a carried citation moves the key, and the message
        says where to; that is the price of a key that names the line, and
        the reason an edit to a carried page is the moment to repoint it."""
        gone = sorted(describe(key) + self.moved(key)
                      for key in KNOWN_LINE_INTO_CODE if key not in self.by_key)
        self.assertEqual(
            [], gone,
            "KNOWN_LINE_INTO_CODE carries a citation the corpus no longer holds "
            "at that key; remove it, or re-key it if the page moved under it:\n  "
            + "\n  ".join(gone),
        )

    def test_carried_citations_are_not_examples(self):
        """A carried key that has since gained a marker is carried twice."""
        marked = sorted(describe(k) for k in KNOWN_LINE_INTO_CODE
                        if k in self.by_key and self.by_key[k].quoted)
        self.assertEqual([], marked,
                         "these carry a [quoted: ...] marker; drop them from the ratchet")

    def test_known_wrong_sites_are_reported_for_their_owner(self):
        """A held site is named on a line of their own and fails nothing
        here; each must still be live, so the day its page repoints it the
        entry leaves with it. Empty since sd:438 repointed its two."""
        self.assertFalse(KNOWN_WRONG_OWNER & KNOWN_LINE_INTO_CODE,
                         "a key is in both KNOWN_WRONG_OWNER and KNOWN_LINE_INTO_CODE")
        gone = sorted(describe(key) for key in KNOWN_WRONG_OWNER if key not in self.by_key)
        self.assertEqual(
            [], gone,
            "KNOWN_WRONG_OWNER names a citation the corpus no longer holds at "
            "that key; its owner repointed it, so remove the entry:\n  "
            + "\n  ".join(gone),
        )
        held = sorted(describe(key) for key in KNOWN_WRONG_OWNER)
        print(f"citations: {len(held)} known-wrong site(s) held for their owner, "
              "not failed here" + (": " + "; ".join(held) if held else ""), file=sys.stderr)

    def test_snippet_anchors_hold(self):
        """Form 1, over the whole corpus. This folds in #369's pin: the
        `agent` docstring in `local-sd-plan/sd_plan.py` cites cron-jobs.sh by
        snippet, and `test_the_cron_jobs_anchor_is_still_cited_by_snippet`
        keeps that one sentence from being rewritten out of this form."""
        errors = []
        for path in self.files:
            errors.extend(snippet_errors(path, read(path)))
        self.assertEqual([], errors, "\n  " + "\n  ".join(errors))

    def test_the_cron_jobs_anchor_is_still_cited_by_snippet(self):
        """The one citation sd:825 fixed is still in form 1, and still holds.
        Anchored on the word that introduces it and the path it ends with, so
        a rewrite that drops either fails here rather than silently leaving
        `test_snippet_anchors_hold` with nothing to check in that docstring."""
        text = read("local-sd-plan/sd_plan.py")
        match = re.search(r"invocation:(?P<anchors>.*?)`local-cron-jobs/cron-jobs\.sh`",
                          text, re.DOTALL)
        self.assertIsNotNone(match, "sd_plan.py no longer cites cron-jobs.sh by snippet")
        anchors = re.findall(r"`([^`]+)`", match.group("anchors"))
        self.assertTrue(anchors, "the citation names no anchor to check")
        body = read("local-cron-jobs/cron-jobs.sh")
        for anchor in anchors:
            with self.subTest(anchor=anchor):
                # `assertTrue` and not `assertIn`: the haystack is a 700-line
                # shell script, and `assertIn` prints the whole of it.
                self.assertTrue(
                    word_bounded(anchor).search(body),
                    f"sd_plan.py cites `{anchor}` in cron-jobs.sh, which no longer "
                    "holds it -- re-anchor the citation on what the code says now",
                )

    def test_source_anchors_resolve(self):
        """Form 2, over the whole corpus."""
        errors = []
        for path in self.files:
            errors.extend(source_errors(path, read(path)))
        self.assertEqual([], errors, "\n  " + "\n  ".join(errors))

    def test_buckets_sum_to_the_tokens_found(self):
        """Conservation: every token lands in exactly one bucket, and the
        census is printed so a reader can see the ratchet shrink."""
        carried = sum(t.key in KNOWN_LINE_INTO_CODE for t in self.tokens)
        owner = sum(t.key in KNOWN_WRONG_OWNER for t in self.tokens)
        quoted = sum(t.quoted and t.key not in KNOWN_LINE_INTO_CODE
                     and t.key not in KNOWN_WRONG_OWNER for t in self.tokens)
        offending = len(self.tokens) - carried - owner - quoted
        self.assertEqual(len(self.tokens), carried + owner + quoted + offending)
        self.assertEqual(len(self.by_key), len(self.tokens), "a key is written twice on one line")
        print(f"citations: {len(self.tokens)} path:line token(s) into code across "
              f"{len(self.files)} file(s): {carried} carried, {owner} held for owner, "
              f"{quoted} quoted, {offending} offending", file=sys.stderr)


class WorkflowCase(unittest.TestCase):
    def test_the_preflight_runs_this_file_before_the_venv_exists(self):
        """The preflight's unwired-suite guard demands a `python3 <file>`
        line for every root `tests/test_*.py`, so deleting the line fails
        the workflow itself, on every leg, whether or not this file ran.
        This test pins where the line sits: after the lint, before the
        virtualenv, as a bare `python3` call."""
        self.assertEqual([], preflight_order_errors(read(WORKFLOW)), WORKFLOW)

    def test_the_order_check_reads_the_lint_invocation_and_not_its_existence_check(self):
        """A scratch copy of the workflow with the `sd-docs-lint` invocation
        deleted, the `test -f` that names the same binary left in place, is
        a workflow in which the lint never runs, and the check says so."""
        with tempfile.TemporaryDirectory() as scratch:
            copy = Path(scratch) / "ci-native.sh"
            kept = [l for l in LINE_END.split(read(WORKFLOW)) if not LINT_INVOCATION.match(l)]
            copy.write_text("\n".join(kept), encoding="utf-8")
            text = copy.read_text(encoding="utf-8")
        self.assertIn('test -f "$SD_ACCEPTANCE_PACK/bin/sd-docs-lint"', text)
        self.assertEqual(["never invokes sd-docs-lint"], preflight_order_errors(text))


class ShapeCase(unittest.TestCase):
    """The grammar, on fixtures, so a regex edit is caught here and not by
    the corpus happening to exercise it."""

    def keys(self, text):
        return [(t.path, t.start, t.end, t.quoted) for t in tokens_in("x.md", text)]

    # This file is in the corpus it scans, with no exemption for the file that
    # defines the gate; each fixture below carries the form-3 marker in a
    # trailing comment, which is the gate's own rule applied to itself.
    def test_token_shapes(self):
        self.assertEqual([("a/b.py", 12, 12, False)], self.keys("see a/b.py:12."))  # [quoted: fixture]
        self.assertEqual([("b.sh", 3, 9, False)], self.keys("`b.sh:3-9`"))  # [quoted: fixture]
        self.assertEqual([("c.yml", 132, 136, False)], self.keys("(c.yml:132-136)"))  # [quoted: fixture]
        self.assertEqual([], self.keys("a.md:12 and a.txt:3 and 1.2.3:4"))
        self.assertEqual([("a.py", 1, 1, False)], self.keys("a.py:1"))  # [quoted: fixture]

    def test_a_marker_covers_the_token_before_it_on_its_line(self):
        self.assertEqual([("a.py", 3, 3, True)], self.keys("a.py:3 [quoted: an example]"))
        self.assertEqual([("a.py", 3, 3, True)], self.keys('"a.py:3 s"  # [quoted: fixture]'))
        self.assertEqual([("a.py", 3, 3, False)], self.keys("[quoted: x] a.py:3"))  # [quoted: fixture]
        self.assertEqual([("a.py", 3, 3, False)], self.keys("a.py:3 [quoted: ]"))  # [quoted: fixture]
        self.assertEqual([("a.py", 3, 3, False)], self.keys("a.py:3\n[quoted: next line]"))

    def test_a_marker_covers_only_the_token_it_follows(self):
        """A real citation earlier on the line is not the example a later
        marker names, so the gate must still fail it (sd:1175). The first
        token is built in two parts, so this line of the corpus holds only
        the one the fixture's own marker covers."""
        real = "a.py" + ":3"
        self.assertEqual([("a.py", 3, 3, False), ("b.py", 4, 4, True)],
                         self.keys(real + " then b.py:4 [quoted: the example]"))
        self.assertEqual([("a.py", 3, 3, True), ("b.py", 4, 4, True)],
                         self.keys("a.py:3 [quoted: x] b.py:4 [quoted: y]"))

    def test_a_marker_covers_a_token_inside_its_reason_and_one_it_touches(self):
        """A reason that spells the banned shape out is the example the
        marker exempts, and a marker that starts where the token ends is on
        the same line after it, whitespace or not. Under `at > match.end()`
        the first was offending because the marker began before the token,
        and the second because the boundary was exclusive (sd:963)."""
        self.assertEqual([("a.py", 3, 3, True)], self.keys("[quoted: see a.py:3]"))
        self.assertEqual([("a.py", 3, 3, True)], self.keys("a.py:3[quoted: x]"))
        self.assertEqual([("a.py", 3, 3, True)], self.keys("`[quoted: a.py:3 is the shape]`"))

    def test_lines_end_where_the_marker_grammar_says_they_do(self):
        """`MARKER` names `\r`, U+2028 and U+2029 as terminators, so the line
        walk must split on the same set, or a marker after one of them is
        read as on the token's line and covers it. A CRLF pair is one
        terminator, and a `\n` file numbers as an editor does."""
        self.assertEqual([("a.py", 3, 3, False)], self.keys("a.py:3\u2028[quoted: next line]"))
        self.assertEqual([("a.py", 3, 3, False)], self.keys("a.py:3\u2029[quoted: next line]"))
        self.assertEqual([("a.py", 3, 3, False)], self.keys("a.py:3\r[quoted: next line]"))
        self.assertEqual([2], [t.line for t in tokens_in("x.md", "x\ra.py:3")])  # [quoted: fixture]
        self.assertEqual([3], [t.line for t in tokens_in("x.md", "x\r\ny\r\na.py:3\r\n")])  # [quoted: fixture]
        self.assertEqual([3], [t.line for t in tokens_in("x.md", "x\ny\na.py:3\n")])  # [quoted: fixture]

    def test_declarations_and_diagnostics_end_lines_where_the_marker_grammar_does(self):
        """`LINE_END` drives the token walk; it drives the shell declaration
        walk and the two diagnostic line numbers too, or a CR-separated
        shell file declares nothing and an error names the wrong line."""
        self.assertEqual([2], shell_declarations("x=1\rfoo() {\r:\r}\r", "foo"))
        self.assertEqual([2], shell_declarations("x=1\r\nfoo() {\r\n:\r\n}\r\n", "foo"))
        self.assertEqual([2], shell_declarations("x=1\u2028foo() {\u2028:\u2028}\u2028", "foo"))
        # Each fixture citation is joined at run time so this file, which is
        # in the corpus, does not itself cite an anchor that is not there.
        errors = snippet_errors("x.md", "a\r`zzz_not_there`, in " + "`local-sd-plan/sd_plan.py`")
        self.assertEqual(1, len(errors))
        self.assertTrue(errors[0].startswith("x.md:2 "), errors[0])
        errors = source_errors("x.md", "a\rb\rsource:" + "local-sd-plan/sd_plan.py::zzz_not_there")
        self.assertEqual(1, len(errors))
        self.assertTrue(errors[0].startswith("x.md:3 "), errors[0])

    def test_word_bounded(self):
        self.assertIsNone(word_bounded("cmd_exec").search("cmd_execute() {"))
        self.assertIsNotNone(word_bounded("cmd_exec").search("cmd_exec() {"))
        self.assertIsNotNone(word_bounded('-p "$X"').search('run -p "$X" now'))

    def test_shell_declarations(self):
        text = 'x=1\nfoo() {\n  x=2\n}\nfunction bar {\nexport y=3\n# baz() {\nfoo\n'
        self.assertEqual([2], shell_declarations(text, "foo"))
        self.assertEqual([5], shell_declarations(text, "bar"))
        self.assertEqual([1, 3], shell_declarations(text, "x"))
        self.assertEqual([6], shell_declarations(text, "y"))
        self.assertEqual([], shell_declarations(text, "baz"))

    def test_a_snippet_cited_in_a_symbol_must_sit_inside_it(self):
        """sd:1235. Both spans once matched the whole file independently, so
        a comment naming the function kept a renamed one green, and a line
        moved to another function still counted."""
        shell = ('# cmd_exec tees its output\n'
                 'cmd_exec() {\n    run "$job"\n}\nother() {\n    notify "$job"\n}\n')
        self.assertIsNone(scoped_snippet_error(shell, ".sh", 'run "$job"', "cmd_exec"))
        self.assertIn("no longer holds",
                      scoped_snippet_error(shell, ".sh", 'notify "$job"', "cmd_exec") or "")
        renamed = shell.replace("cmd_exec() {", "cmd_run() {")
        self.assertIn("declares", scoped_snippet_error(renamed, ".sh", 'run "$job"', "cmd_exec") or "")
        python = ('"""connect is documented here: no database at"""\n'
                  'def connect():\n    return open_local()\n'
                  'def open_local():\n    raise OSError("no database at")\n')
        self.assertIsNone(scoped_snippet_error(python, ".py", "no database at", "open_local"))
        self.assertIn("no longer holds",
                      scoped_snippet_error(python, ".py", "no database at", "connect") or "")
        self.assertEqual([(2, 4)], symbol_spans("x=1\nf() {\n  :\n}\n", ".sh", "f"))
        self.assertEqual([(1, 3)], symbol_spans("f() {  # why\n  :\n}\n", ".sh", "f"))
        self.assertIsNone(symbol_spans("a: 1\n", ".yml", "a"))

    def test_a_one_line_shell_function_is_its_own_scope(self):
        """A `name() { ...; }` body ends on its own line. Searching on for a
        standalone `}` ran into the next function, so a snippet from there
        passed, and a file with no later `}` declared nothing at all."""
        shell = ('label_for()  { echo "local.system-tools.cron.$1"; }\n'
                 'job_file() {\n    echo "$JOBS_DIR/$1.job"\n}\n')
        self.assertEqual([(1, 1)], symbol_spans(shell, ".sh", "label_for"))
        self.assertIn("no longer holds",
                      scoped_snippet_error(shell, ".sh", '"$JOBS_DIR/$1.job"', "label_for") or "")
        self.assertIsNone(scoped_snippet_error("f() { echo hello; }\n", ".sh", "echo hello", "f"))

    def test_a_brace_in_the_openers_comment_does_not_close_the_function(self):
        """A `#` after the opening brace starts a comment, and a `}` inside
        it closes nothing: `bash -n` accepts this body. The one-line reader
        once saw that `}` and bounded the function to its first line."""
        shell = "f() { # close with }\n    echo hello\n}\ng() {\n    other\n}\n"
        self.assertEqual([(1, 3)], symbol_spans(shell, ".sh", "f"))
        self.assertIsNone(scoped_snippet_error(shell, ".sh", "echo hello", "f"))
        self.assertIn("no longer holds", scoped_snippet_error(shell, ".sh", "other", "f") or "")

    def test_a_shell_body_it_cannot_bound_falls_back_to_the_file(self):
        """A brace on the next line, a closing `} >&2`, or a subshell body is
        not a shape this reader bounds reliably. Those return `None`, the
        file-wide check the gate ran before sd:1235, rather than a span that
        runs into the next function or a false "nothing declares it"."""
        for body in ('f()\n{\n    run\n}\ng() {\n    other\n}\n',
                     'f() {\n    run\n} >&2\ng() {\n    other\n}\n',
                     'f() (\n    run\n)\ng() {\n    other\n}\n'):
            with self.subTest(body=body):
                self.assertIsNone(symbol_spans(body, ".sh", "f"))
                self.assertIsNone(scoped_snippet_error(body, ".sh", "run", "f"))
                self.assertIn("no longer holds",
                              scoped_snippet_error(body, ".sh", "absent", "f") or "")

    def test_python_declarations(self):
        tree = ast.parse("A = 1\ndef f():\n    g = 2\nclass C:\n    def m(self): pass\n")
        self.assertEqual([(1, 1)], declared_spans(tree, "A"))
        self.assertEqual([(2, 3)], declared_spans(tree, "f"))
        self.assertEqual([(5, 5)], declared_spans(tree, "m"))
        self.assertEqual([], declared_spans(tree, "g"))

    def test_corpus_is_md_py_sh_minus_the_archive(self):
        paths = ["a.md", "b/c.py", "d.sh", "e.txt", "docs/work/x/design.md",
                 "docs/work/archive/2026-09/x/prd.md", "f.yml"]
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            for path in paths:
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_text("x\n", encoding="utf-8")
            self.assertEqual(["a.md", "b/c.py", "d.sh", "docs/work/x/design.md"],
                             corpus(paths, root))

    def test_corpus_reads_no_symlink_out_of_the_checkout_and_no_special_file(self):
        """`git ls-files` constrains a symlink's name and nothing about its
        target: a tracked `.md` link can point at a file outside the checkout
        or at a device that never returns. The walk resolves each path and
        keeps the regular files inside the root; a link to one of those is
        read as that file (sd:963)."""
        with tempfile.TemporaryDirectory() as scratch, tempfile.TemporaryDirectory() as far:
            root = Path(scratch)
            (root / "in.md").write_text("x\n", encoding="utf-8")
            (Path(far) / "out.md").write_text("x\n", encoding="utf-8")
            (root / "escape.md").symlink_to(Path(far) / "out.md")
            (root / "inside.md").symlink_to(root / "in.md")
            (root / "dangling.md").symlink_to(root / "nowhere.md")
            (root / "dir.py").mkdir()
            os.mkfifo(root / "pipe.sh")
            (root / "loop_a.py").symlink_to(root / "loop_b.py")
            (root / "loop_b.py").symlink_to(root / "loop_a.py")
            paths = ["in.md", "escape.md", "inside.md", "dangling.md", "dir.py", "pipe.sh",
                     "loop_a.py"]
            self.assertEqual(["in.md", "inside.md"], corpus(paths, root))
            # A two-link loop is skipped by every read, not raised: 3.13
            # resolves it as far as it goes, an older interpreter raises
            # RuntimeError, and both land here as "not a regular file".
            self.assertIsNone(resolve("x.md", "loop_a.py", root, ["loop_a.py", "loop_b.py"]))
            self.assertIn("loop_a.py: ", source_error("loop_a.py", "py", "f", root))

    def test_corpus_skips_a_tracked_link_to_an_untracked_target(self):
        """`resolve` and `source_error` ask both ends of a symlink, and so
        does the walk: a tracked `link.py` to an untracked `real.py` is a
        regular file inside the root on the author's machine and a dangling
        link in the CI checkout, so scanning it locally makes the corpus and
        the census differ between the two. A tracked link to a tracked file
        is read as that file, as before (sd:964, #404 review)."""
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / "real.py").write_text("x\n", encoding="utf-8")
            (root / "kept.py").write_text("x\n", encoding="utf-8")
            (root / "link.py").symlink_to(root / "real.py")
            (root / "both.py").symlink_to(root / "kept.py")
            listed = ["link.py", "both.py", "kept.py", "x.md"]
            self.assertEqual(["both.py", "kept.py"], corpus(listed, root))

    def test_every_link_on_the_way_to_the_target_is_tracked(self):
        """A tracked `link.py` to an untracked `middle.py` to a tracked
        `real.py` resolves to a tracked regular file, and both ends pass;
        but the CI checkout has no `middle.py`, so `link.py` dangles there.
        Every symlink the candidate traverses must be tracked, not only the
        first name and the last file, in the walk and in both resolvers
        (#410 review)."""
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch).resolve()
            (root / "real.py").write_text("def f():\n    pass\n", encoding="utf-8")
            (root / "middle.py").symlink_to(root / "real.py")
            (root / "link.py").symlink_to(root / "middle.py")
            (root / "hop.py").symlink_to(root / "real.py")
            (root / "chain.py").symlink_to(root / "hop.py")
            listed = ["link.py", "real.py", "chain.py", "hop.py", "x.md"]
            self.assertEqual(["real.py", "chain.py", "hop.py"], corpus(listed, root))
            self.assertIsNone(resolve("x.md", "link.py", root, listed))
            self.assertEqual(root / "real.py", resolve("x.md", "chain.py", root, listed))
            self.assertIn("not tracked", source_error("link.py", "py", "f", root, listed) or "")
            self.assertIsNone(source_error("chain.py", "py", "f", root, listed))

    def test_a_target_is_tracked_when_the_root_is_the_checkout(self):
        """An ignored local `foo.py` passes a form-1 or form-2 anchor on the
        author's machine and fails it in the shallow CI checkout, which holds
        only what git tracks. So a candidate that resolves is accepted only
        when the tracked list holds it; a temp root with no list is a fixture
        with no repository to ask, and keeps its old behaviour (sd:963)."""
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch).resolve()
            (root / "m.py").write_text("def f():\n    pass\n", encoding="utf-8")
            (root / "loose.py").write_text("def f():\n    pass\n", encoding="utf-8")
            listed = ["m.py", "x.md"]
            self.assertEqual(root / "m.py", resolve("x.md", "m.py", root, listed))
            self.assertIsNone(resolve("x.md", "loose.py", root, listed))
            self.assertIsNone(source_error("m.py", "py", "f", root, listed))
            error = source_error("loose.py", "py", "f", root, listed)
            self.assertIsNotNone(error, "an untracked target passed form 2")
            self.assertIn("not tracked", error)
            # No list and not the checkout: the fixture behaviour, unconstrained.
            self.assertEqual(root / "loose.py", resolve("x.md", "loose.py", root))
            self.assertIsNone(source_error("loose.py", "py", "f", root))
        # Both ends of a symlink must be tracked: git tracks the link by name
        # and not by target, so an untracked link to a tracked file is not in
        # the CI checkout and a tracked link to an untracked file dangles
        # there. Only a tracked link to a tracked file is read (#404 review).
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch).resolve()
            (root / "real.py").write_text("def f():\n    pass\n", encoding="utf-8")
            (root / "loose.py").write_text("def f():\n    pass\n", encoding="utf-8")
            (root / "alias.py").symlink_to(root / "real.py")
            (root / "link.py").symlink_to(root / "loose.py")
            (root / "both.py").symlink_to(root / "real.py")
            listed = ["real.py", "link.py", "both.py", "x.md"]
            self.assertIsNone(resolve("x.md", "alias.py", root, listed))
            self.assertIsNone(resolve("x.md", "link.py", root, listed))
            self.assertEqual(root / "real.py", resolve("x.md", "both.py", root, listed))
            self.assertIn("not tracked", source_error("alias.py", "py", "f", root, listed) or "")
            self.assertIn("not tracked", source_error("link.py", "py", "f", root, listed) or "")
            self.assertIsNone(source_error("both.py", "py", "f", root, listed))
        # The checkout itself asks git: this file is tracked, its bytecode is not.
        self.assertEqual(ROOT / "tests/test_citations.py", resolve("x.md", "tests/test_citations.py"))
        self.assertIsNone(resolve("x.md", "tests/__pycache__/never_tracked.py"))

    def test_the_filename_fallback_is_a_bare_name_and_not_a_suffix(self):
        """`src/foo.py` cited from a page and since moved is not found again
        as `other/src/foo.py`; the fallback is for a name with no directory
        part, and only when exactly one tracked file carries it (sd:963)."""
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch).resolve()
            for path in ["other/src/foo.py", "third/foo.py", "third/bar.py"]:
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_text("x = 1\n", encoding="utf-8")
            one = ["other/src/foo.py", "third/bar.py", "x.md"]
            self.assertIsNone(resolve("x.md", "src/foo.py", root, one))
            self.assertEqual(root / "other/src/foo.py", resolve("x.md", "foo.py", root, one))
            self.assertEqual(root / "third/bar.py", resolve("x.md", "bar.py", root, one))
            two = ["other/src/foo.py", "third/foo.py", "x.md"]
            self.assertIsNone(resolve("x.md", "foo.py", root, two))

    def test_source_error_names_each_cause(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / "m.py").write_text("def f():\n    pass\nf = 1\n", encoding="utf-8")
            (root / "s.sh").write_text("g() {\n:\n}\n", encoding="utf-8")
            (root / "bad.py").write_text("def (:\n", encoding="utf-8")
            (root / "d").mkdir()
            self.assertIn("found 2", source_error("m.py", "py", "f", root))
            self.assertIn("found 0", source_error("m.py", "py", "h", root))
            self.assertIn("does not parse", source_error("bad.py", "py", "f", root))
            self.assertIn("target is missing", source_error("gone.py", "py", "f", root))
            self.assertIn("not a regular file", source_error("d", "py", "f", root))
            self.assertIsNone(source_error("s.sh", "sh", "g", root))
            self.assertIn("no `h() {`", source_error("s.sh", "sh", "h", root))
            self.assertIn("escapes", source_error("../outside.py", "py", "f", root))


if __name__ == "__main__":
    unittest.main()
