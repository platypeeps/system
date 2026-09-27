#!/usr/bin/env python3
"""Render this repository's design documents into `docs/dashboard/`.

The dashboard serves finished HTML pages out of `docs/dashboard` in any
checkout it finds (`sd_dashboard/documents.py`). Research repos fill that
directory with `sd-research-kit render`. This repository is not a research
repo -- giving it a `research.conf.py` would make `collect_research` list it
as a research project, which it is not -- so it renders its own pages here,
from Markdown sources tracked in `docs/design/`.

Three things this is deliberately not:

**Not hand-written HTML.** `sd_research_render` exists because hand-wrapping
publishable HTML drifts from the renderer that made the rest; the same
argument applies to a second set of pages on the same shelf. Markdown in,
one code path out.

**Not a second visual identity.** The page shell reuses the pack's
`TOKENS_CSS` and `FONTS_CSS` verbatim, read from `$SD_PACK_ROOT` on each run.
A missing pack checkout is a SKIP with a plain fallback stylesheet, never a
failure: the pack is a different repository and may not be cloned here.

**Not an inventory.** The documents are whatever `docs/design/*.md` holds,
and each page's title comes from its own frontmatter. A list in this file is
one more thing that goes stale while looking authoritative.

Diagrams come from Archify (`~/.claude/skills/archify`). Each `.json` under
`docs/design/diagrams/` is rendered twice: once as its own standalone page,
which keeps Archify's whole feature set -- theme toggle, navigation, motion,
the export menu -- and once lifted into the document that names it, as a
self-contained `<svg>` with every `var()` already resolved to a literal. The
lifted copy has no cascade dependency on anything, which is what lets it sit
inside a page wearing a different stylesheet.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

#: Where the pack's stylesheet and embedded fonts come from. Same default and
#: same variable as `local-bin-links`, so one export moves both.
PACK_ROOT = Path(os.environ.get("SD_PACK_ROOT", "~/repos/platypeeps/sd-ai-command-pack")).expanduser()

#: The Archify checkout. A skill directory rather than a command on PATH,
#: which is what it is: the skill ships its own renderer.
ARCHIFY = Path(os.environ.get("ARCHIFY_HOME", "~/.claude/skills/archify")).expanduser()

#: Sources in, pages out. Both repo-relative; the repository is the checkout
#: this file sits in, never an argument (R10-D6).
SOURCES = "docs/design"
PUBLISHED = "docs/dashboard"

#: `sd_dashboard.documents.NAME` will not serve a name this does not match,
#: so a source whose stem cannot make one is refused here rather than
#: rendered into a file nothing lists.
STEM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

#: A diagram's standalone page is `diagram-<spec>.html`, so a document stem
#: starting `diagram-` can claim the same output name. It did: the document
#: page overwrote the interactive one, and `--check` then reported the same
#: output stale on every run for ever (#471, found in audit). The prefix is
#: this renderer's, not a document's.
RESERVED = "diagram-"

#: The five Archify diagram types, each with its own schema.
TYPES = ("architecture", "workflow", "sequence", "dataflow", "lifecycle")

#: A diagram reference in a source document, alone on its own line:
#:     @diagram coding-pipeline
#: The name is the `.json` stem under `docs/design/diagrams/`. A fenced or
#: indented line is not a reference -- the pattern is anchored and the fence
#: state is tracked, because a document explaining this syntax has to be able
#: to print it.
DIAGRAM = re.compile(r"^@diagram\s+([A-Za-z0-9][A-Za-z0-9._-]*)\s*$")
JOBS = re.compile(r"^@jobs\s*$")

#: What `expand_diagrams` leaves behind for `build_page` to substitute, and the
#: only raw HTML the converter emits. Markdown would wrap an SVG in a paragraph
#: and escape parts of it, so the element goes in after conversion.
PLACEHOLDER = re.compile(r"<!--DIAGRAM:([A-Za-z0-9][A-Za-z0-9._-]*)-->")
#: A job's blurb is its first sentence. `;` was in this class, so two live
#: blurbs published with a dangling semicolon (#471, found in audit).
SENTENCE = re.compile(r"(?<=\.)\s")

FALLBACK_CSS = """
:root{--ink:#0F1617;--ink-soft:#465658;--ground:#EEF2F2;--surface:#fff;--line:#D3DEDE;--accent:#0E7C86}
@media (prefers-color-scheme:dark){:root{--ink:#E3EAE9;--ink-soft:#A1B2B1;--ground:#0C1112;--surface:#131A1B;--line:#2A3637;--accent:#3FB3BC}}
body{background:var(--ground);color:var(--ink);font:16px/1.6 Georgia,serif;margin:0}
.wrap{max-width:52rem;margin:0 auto;padding:3rem 1.5rem 6rem}
a{color:var(--accent)} code,pre{font-family:ui-monospace,Menlo,monospace}
pre{background:var(--surface);border:1px solid var(--line);padding:1rem;overflow-x:auto}
table{border-collapse:collapse;width:100%} th,td{border:1px solid var(--line);padding:.4rem .6rem;text-align:left}
"""


class Skip(Exception):
    """A dependency this machine does not have. Reported, never fatal."""


class Broken(Exception):
    """Input in the tree that cannot be built. Reported, and always fatal.

    The distinction is the whole point: a machine without archify or node
    renders the prose and says so, and that is a complete answer. A spec
    archify *rejects* is a defect in this repository, and a build that prints
    it and exits 0 is a gate that never fails (#471, found in review).
    """


def repo_root(start: Path | None = None) -> Path:
    """The checkout this file lives in."""
    return Path(__file__).resolve().parent.parent if start is None else start


def frontmatter(text: str) -> tuple[dict, str]:
    """A leading `---` block as flat key/value pairs, plus the body.

    Deliberately not YAML: the fields are a title and a sentence, `sd_db`'s
    own readers parse the same shape, and adding a parser dependency to a
    page renderer buys nothing.
    """
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    meta = {}
    for line in text[4:end].splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, text[end + 5:]


# --------------------------------------------------------------------------
# Archify


def has_archify() -> bool:
    """Whether this machine can render a diagram at all."""
    return (ARCHIFY / "bin" / "archify.mjs").is_file() and bool(shutil.which("node"))


def archify(*args: str) -> None:
    """Run the Archify CLI, raising Skip when it is not installed."""
    cli = ARCHIFY / "bin" / "archify.mjs"
    if not cli.is_file():
        raise Skip(f"archify not found: {ARCHIFY}")
    if not shutil.which("node"):
        raise Skip("node not on PATH; archify needs node >= 18")
    done = subprocess.run(["node", str(cli), *args], capture_output=True, text=True, timeout=180)
    if done.returncode != 0:
        raise Broken(f"archify {args[0]} failed: {(done.stderr or done.stdout).strip()[:300]}")


def diagram_type(spec: Path) -> str:
    """The Archify schema a diagram declares, read from the file itself.

    Encoding it in the filename would make a rename change the schema, and
    keeping a table here would be an inventory. The file says what it is.
    """
    data = json.loads(spec.read_text())
    # `diagram_type` and nothing else. A `type` fallback was this renderer's
    # own early mistake, and it survived the fix as a fallback -- which is
    # worse than the bug was: archify rejects a spec keyed that way, so the
    # fallback only ever accepts a file that cannot render.
    kind = data.get("diagram_type")
    if kind not in TYPES:
        # `Broken`, not `Skip`: a spec that declares no schema is wrong in this
        # repository and is wrong on every machine. Raising `Skip` here meant
        # the one malformation this function exists to catch was the one the
        # build did not fail on (#471, found in review).
        raise Broken(f"{spec.name}: needs a top-level \"diagram_type\" of one of {', '.join(TYPES)}")
    return kind


def strip_remote(page: str) -> str:
    """Remove Archify's Google Fonts link and the one inline handler on it.

    The served policy blocks both, so the link buys nothing and costs every
    reader a call to a third party the moment the page is opened from disk,
    where no policy applies. Taking the element removes the `onload` with it.
    """
    page = re.sub(r'<link[^>]*fonts\.(?:googleapis|gstatic)\.com[^>]*>', "", page)
    return re.sub(r'<noscript>\s*<link[^>]*fonts\.[^>]*>\s*</noscript>', "", page)


def resolve_vars(body: str, palette: dict[str, str]) -> str:
    """Replace every `var(--x[, fallback])` with its literal value.

    A lifted diagram has to survive inside a page whose `:root` belongs to
    somebody else. Resolving here means the embedded SVG depends on no
    custom property at all, which is checkable: no `var(` may remain.
    """
    def one(match: re.Match) -> str:
        name = match.group(1)
        default = (match.group(2) or "").strip()
        return palette.get(name, default or "currentColor")

    # Bounded, because a variable defined in terms of itself is a stylesheet
    # this build cannot resolve -- and an unbounded loop over it never returns
    # and never says why (#471, found in audit). The depth is far past any real
    # chain; `lift` refuses the result if a `var(` survives it.
    previous = None
    for _ in range(16):
        if previous == body:
            break
        previous = body
        body = re.sub(r"var\(\s*(--[A-Za-z0-9-]+)\s*(?:,([^()]*))?\)", one, body)
    return body


PRESET = re.compile(r'\[data-preset="([^"]+)"\]')
THEME = re.compile(r'\[data-theme="([^"]+)"\]')


def governs(selector: str, preset: str, theme: str) -> bool:
    """Whether a palette block applies to the one element being lifted.

    Substring tests read `[data-preset="editorial"][data-theme="light"]` as a
    light-theme block, and archify writes its preset palettes last, so every
    lifted diagram came out in the editorial palette's cream and brown
    (#471, found in audit). A qualifier the element does not carry disqualifies
    the block; a comma list is several selectors and any one of them can match.
    """
    for part in (p.strip() for p in selector.split(",")):
        presets = PRESET.findall(part)
        if presets and preset not in presets:
            continue
        themes = THEME.findall(part)
        if themes:
            if theme in themes:
                return True
            continue
        if ":root" in part:
            return True
    return False


def palette_of(css: str, selector_test) -> dict[str, str]:
    """Custom properties from every rule whose selector the test accepts."""
    found: dict[str, str] = {}
    for match in re.finditer(r"(?P<sel>[^{}@]+)\{(?P<body>[^{}]*)\}", css):
        selector = re.sub(r"/\*.*?\*/", "", match.group("sel"), flags=re.S).strip()
        if not selector or not selector_test(selector):
            continue
        for name, value in re.findall(r"(--[A-Za-z0-9-]+)\s*:\s*([^;]+)", match.group("body")):
            found[name] = value.strip()
    return found


def lift(page: str) -> str:
    """The page's `<svg>`, made self-contained.

    Archify draws everything through 32 namespaced classes and CSS custom
    properties; the element on its own renders as unstyled black shapes. So
    the rules that mention those classes come with it, with their variables
    already resolved against Archify's own light and dark palettes -- light
    inline, dark behind the same two triggers the house stylesheet uses.
    """
    # Every failure below is `Broken`, not `Skip`. They all describe a page
    # archify did produce and this code could not use -- a defect that is the
    # same on every machine -- and `Skip` is now reserved for the one thing a
    # machine can simply lack.
    svg = re.search(r"<svg.*?</svg>", page, re.S)
    styles = re.findall(r"<style[^>]*>(.*?)</style>", page, re.S)
    if not svg or not styles:
        raise Broken("archify page carried no svg or no stylesheet")
    svg, css = svg.group(0), max(styles, key=len)

    used: set[str] = set()
    for attr in re.findall(r'class="([^"]+)"', svg):
        used.update(attr.split())

    # `@media print` redefines the palette for paper; lifting it would apply
    # it to the screen. Drop the block, keep everything else.
    css = re.sub(r"@media\s+print\s*\{(?:[^{}]|\{[^{}]*\})*\}", "", css)

    # The element's own preset decides which palette blocks reach it. It
    # carries no `data-theme`, so both themes are resolved here instead.
    named = re.search(r'<svg[^>]*\bdata-preset="([^"]+)"', svg)
    preset = named.group(1) if named else ""
    light = palette_of(css, lambda sel: governs(sel, preset, "light"))
    dark = palette_of(css, lambda sel: governs(sel, preset, "dark"))

    kept = []
    for match in re.finditer(r"(?P<sel>[^{}@]+)\{(?P<body>[^{}]*)\}", css):
        selector = re.sub(r"/\*.*?\*/", "", match.group("sel"), flags=re.S).strip()
        body = match.group("body").strip()
        if not selector or not body or selector.startswith("@"):
            continue
        if not any(re.search(r"\." + re.escape(name) + r"\b", selector) for name in used):
            continue
        kept.append((selector, body))

    missing = {c for c in used
               if not any(re.search(r"\." + re.escape(c) + r"\b", sel) for sel, _ in kept)}
    if missing:
        raise Broken("diagram classes carry no rule: " + ", ".join(sorted(missing)))

    def scope(selector: str, prefix: str) -> str:
        """Every arm of a comma list gets the prefix, not just the first.

        `.diagram a, b{...}` scopes `a` and leaves `b` loose in the page --
        one rule in the published set did exactly that (#471, found in audit).
        """
        return ", ".join(f"{prefix} {part.strip()}" for part in selector.split(","))

    def sheet(palette: dict[str, str], prefix: str) -> str:
        return "\n".join(f"{scope(sel, prefix)}{{{resolve_vars(body, palette)}}}"
                          for sel, body in kept)

    # The dark prefix goes on every rule, not once in front of the block: the
    # guard used to attach to the first of thirty-five and leave the rest
    # unconditional. Both triggers the house stylesheet uses are emitted --
    # the OS preference, and an explicit `data-theme` on the page.
    by_preference = sheet(dark, ':root:not([data-theme="light"]) .diagram').replace("\n", " ")
    by_attribute = sheet(dark, ':root[data-theme="dark"] .diagram').replace("\n", " ")
    scoped = (
        f"<style>\n{sheet(light, '.diagram')}\n"
        f"@media (prefers-color-scheme:dark){{{by_preference}}}\n"
        f"{by_attribute}\n"
        f"</style>"
    )
    # `check` is what proves the resolution was complete; assert it here too,
    # because a stray `var()` renders as a missing colour and nothing else.
    if "var(" in scoped:
        raise Broken("a diagram rule still references an unresolved custom property")
    svg = re.sub(r"<svg\b", '<svg class="diagram-svg" preserveAspectRatio="xMidYMid meet"', svg, count=1)
    return f'<figure class="diagram">{scoped}{svg}</figure>'


def build_diagrams(root: Path, out_dir: Path, notes: list[str],
                   failures: list[str]) -> dict[str, str]:
    """Render every diagram, returning the embeddable form for each name.

    A diagram that fails is still not a crash -- the other five pages are worth
    having -- but it goes in `failures`, which decides the exit status. Only
    `Skip` stays silent in that sense, and `Skip` now means one thing: this
    machine has no archify.
    """
    embeds: dict[str, str] = {}
    specs = sorted((root / SOURCES / "diagrams").glob("*.json"))
    for spec in specs:
        target = out_dir / f"diagram-{spec.stem}.html"
        try:
            kind = diagram_type(spec)
            archify("render", kind, str(spec), str(target))
            page = strip_remote(target.read_text())
            target.write_text(page)
            embeds[spec.stem] = lift(page)
            notes.append(f"  diagram  {spec.stem:28} {kind:13} {len(page)//1024:4d} KB")
        except Skip as why:
            notes.append(f"  SKIP     {spec.stem:28} {why}")
        except Exception as why:  # a broken diagram is a finding, not a crash
            notes.append(f"  FAILED   {spec.stem:28} {why}")
            failures.append(spec.stem)
            # Archify may have written the page before `lift` rejected it.
            # Leaving it there publishes a diagram nobody could use and, worse,
            # leaves a file whose mtime beats its spec -- which is exactly what
            # `--check` reads as fresh (#471, found in review).
            target.unlink(missing_ok=True)
    return embeds


# --------------------------------------------------------------------------
# Pages


def house_css() -> tuple[str, str]:
    """The pack's stylesheet and embedded fonts, or a plain local fallback."""
    tokens = PACK_ROOT / "bin" / "sd_research_tokens.py"
    fonts = PACK_ROOT / "bin" / "sd_research_fonts.py"
    if not tokens.is_file():
        return FALLBACK_CSS, ""
    scope: dict = {}
    exec(compile(tokens.read_text(), str(tokens), "exec"), scope)  # nosec B102
    css = scope.get("TOKENS_CSS", FALLBACK_CSS)
    embedded = ""
    if fonts.is_file():
        scope = {}
        exec(compile(fonts.read_text(), str(fonts), "exec"), scope)  # nosec B102
        embedded = scope.get("FONTS_CSS", "")
    return css, embedded


DIAGRAM_CSS = """
.diagram{margin:2rem 0;padding:0;border:1px solid var(--line,#ccc);border-radius:4px;
  background:var(--surface,#fff);overflow:hidden}
.diagram .diagram-svg{display:block;width:100%;height:auto}
.diagram figcaption{font-family:var(--sans,sans-serif);font-size:.78rem;color:var(--ink-faint,#777);
  padding:.55rem .8rem;border-top:1px solid var(--line-soft,#eee)}
.diagram figcaption a{color:inherit}
"""


#: Inline spans, applied in this order inside a text run. Code first, because
#: what is inside backticks is not markup -- `**x**` in a code span is two
#: asterisks and a letter, and running emphasis first would eat them.
LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
STRONG = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.S)
EMPH = re.compile(r"(?<![\w*])\*(?=\S)([^*]+?)(?<=\S)\*(?![\w*])", re.S)
CODE = re.compile(r"``(.+?)``|`([^`]+)`", re.S)
FENCE = re.compile(r"^```\s*([A-Za-z0-9_+-]*)\s*$")
HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
BULLET = re.compile(r"^[-*]\s+(.*)$")
NUMBER = re.compile(r"^(\d+)\.\s+(.*)$")
QUOTE = re.compile(r"^>\s?(.*)$")
RULE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")
CELLS = re.compile(r"^\s*\|(.+)\|\s*$")
ALIGN = re.compile(r"^\s*:?-{2,}:?\s*$")
SLUG = re.compile(r"[^a-z0-9]+")


def inline(text: str) -> str:
    """One run of text, with code spans protected from every other rule.

    Markdown conversion here is deliberately small rather than a dependency:
    nothing on this machine ships python-markdown, the CI job installs no
    extras, and a suite that skips is a suite this repository fails. The
    subset is exactly what `docs/design/*.md` uses, and the tests assert that
    -- an unsupported construct is a test failure, not a silent mis-render.
    """
    kept: list[str] = []

    def stash(match: re.Match) -> str:
        # A ``double`` span is one code element whose content may itself hold
        # backticks -- the citation-anchor form the docs guide spells out. One
        # leading and trailing space is the fence, not content.
        raw = match.group(1) if match.group(1) is not None else match.group(2)
        if match.group(1) is not None and raw.startswith(" ") and raw.endswith(" "):
            raw = raw[1:-1]
        kept.append(f"<code>{html.escape(raw, quote=False)}</code>")
        return f"\x00{len(kept) - 1}\x00"

    text = CODE.sub(stash, text)
    text = html.escape(text, quote=False)
    # The text is already escaped, so the URL's `&` is already `&amp;` --
    # escaping it again published `&amp;amp;` (#471, found in audit). Only the
    # quote characters, which `escape(quote=False)` left alone, are handled.
    text = LINK.sub(lambda m: '<a href="{}">{}</a>'.format(
        m.group(2).replace('"', "&quot;"), m.group(1)), text)
    text = STRONG.sub(r"<strong>\1</strong>", text)
    text = EMPH.sub(r"<em>\1</em>", text)
    text = re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], text)
    return text


def slug(text: str, taken: set[str]) -> str:
    base = SLUG.sub("-", html.unescape(re.sub(r"<[^>]+>", "", text)).lower()).strip("-") or "section"
    candidate, n = base, 1
    while candidate in taken:
        n += 1
        candidate = f"{base}-{n}"
    taken.add(candidate)
    return candidate


def cells(line: str) -> list[str]:
    """One table row, split on the pipes that are actually separators.

    `jobs_table` escapes a pipe in a job's own prose as `\\|`, and a plain
    `split("|")` turned that into an extra cell with a visible backslash; a
    pipe inside a code span did the same (#471, found in audit).
    """
    inner = CELLS.match(line).group(1)
    spans: list[str] = []

    def stash(match: re.Match) -> str:
        spans.append(match.group(0))
        return f"\x00{len(spans) - 1}\x00"

    inner = CODE.sub(stash, inner)
    parts = re.split(r"(?<!\\)\|", inner)
    return [re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], part)
            .replace("\\|", "|").strip() for part in parts]


def table(block: list[str]) -> str:
    rows = [cells(line) for line in block]
    head, body = rows[0], rows[2:]
    out = ["<table>", "<thead><tr>",
           *(f"<th>{inline(cell)}</th>" for cell in head), "</tr></thead>", "<tbody>"]
    for row in body:
        out.append("<tr>")
        out += [f"<td>{inline(cell)}</td>" for cell in row]
        out.append("</tr>")
    out += ["</tbody>", "</table>"]
    return "".join(out)


def markdown_to_html(body: str) -> tuple[str, list[dict]]:
    """The document subset, as HTML, plus the headings for the rail nav."""
    lines = body.split("\n")
    out: list[str] = []
    headings: list[dict] = []
    taken: set[str] = set()
    index = 0

    def paragraph(buffer: list[str]) -> None:
        if buffer:
            out.append(f"<p>{inline(' '.join(buffer))}</p>")
            buffer.clear()

    para: list[str] = []
    while index < len(lines):
        line = lines[index]

        fence = FENCE.match(line)
        if fence:
            paragraph(para)
            index += 1
            code = []
            while index < len(lines) and not lines[index].startswith("```"):
                code.append(lines[index])
                index += 1
            index += 1
            language = f' class="language-{fence.group(1)}"' if fence.group(1) else ""
            out.append(f"<pre><code{language}>"
                       f"{html.escape(chr(10).join(code), quote=False)}</code></pre>")
            continue

        if PLACEHOLDER.fullmatch(line.strip()):
            # The renderer's own token, and nothing else. "starts with `<!--`
            # and ends with `-->`" let a whole line through, so a source could
            # write `<!-- --><script>…</script><!-- -->` and have it emitted
            # verbatim (#471, found in audit). A document that wants to show
            # the token writes it in a code span, which never reaches here.
            paragraph(para)
            out.append(line.strip())
            index += 1
            continue

        head = HEADING.match(line)
        if head:
            paragraph(para)
            level, text = len(head.group(1)), inline(head.group(2))
            anchor = slug(text, taken)
            headings.append({"level": level, "id": anchor, "name": text})
            out.append(f'<h{level} id="{anchor}">{text}</h{level}>')
            index += 1
            continue

        if not line.strip():
            paragraph(para)
            index += 1
            continue

        if RULE.match(line.strip()):
            paragraph(para)
            out.append("<hr>")
            index += 1
            continue

        if CELLS.match(line) and index + 1 < len(lines) and CELLS.match(lines[index + 1]) \
                and all(ALIGN.match(cell) for cell in CELLS.match(lines[index + 1]).group(1).split("|")):
            paragraph(para)
            block = []
            while index < len(lines) and CELLS.match(lines[index]):
                block.append(lines[index])
                index += 1
            out.append(table(block))
            continue

        if BULLET.match(line) or NUMBER.match(line):
            paragraph(para)
            ordered = bool(NUMBER.match(line))
            pattern = NUMBER if ordered else BULLET
            items: list[list[str]] = []
            while index < len(lines):
                current = lines[index]
                match = pattern.match(current)
                if match:
                    items.append([match.group(2) if ordered else match.group(1)])
                elif current.startswith(("  ", "\t")) and items:
                    items[-1].append(current.strip())
                elif not current.strip() and index + 1 < len(lines) \
                        and lines[index + 1].startswith(("  ", "\t")):
                    pass
                else:
                    break
                index += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{inline(' '.join(item))}</li>"
                                            for item in items) + f"</{tag}>")
            continue

        quote = QUOTE.match(line)
        if quote:
            paragraph(para)
            block = []
            while index < len(lines) and QUOTE.match(lines[index]):
                block.append(QUOTE.match(lines[index]).group(1))
                index += 1
            out.append(f"<blockquote><p>{inline(' '.join(block))}</p></blockquote>")
            continue

        para.append(line.strip())
        index += 1

    paragraph(para)
    return "\n".join(out), headings


def expand_diagrams(body: str, embeds: dict[str, str], notes: list[str],
                    root: Path, failures: list[str]) -> str:
    """Swap each `@diagram <name>` line for a placeholder the renderer keeps.

    Markdown would wrap raw SVG in a paragraph and escape parts of it, so the
    element goes in after conversion, against a token markdown leaves alone.
    """
    # A source that writes the token itself would have a figure substituted
    # into it by `build_page`, twice if it names a diagram the page already
    # carries. The token is this renderer's, so it is removed from the input
    # before any is produced (#471, found in audit).
    body = PLACEHOLDER.sub("", body)
    out, fenced = [], False
    for line in body.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        match = None if fenced else DIAGRAM.match(line)
        if match:
            name = match.group(1)
            if name in embeds:
                out.append(f"\n<!--DIAGRAM:{name}-->\n")
            else:
                spec = root / SOURCES / "diagrams" / f"{name}.json"
                why = ("did not render" if spec.is_file()
                       else f"has no {SOURCES}/diagrams/{name}.json")
                notes.append(f"  missing  @diagram {name} {why}")
                # A name with no spec behind it is a broken document, whatever
                # this machine has installed. A name whose spec exists but did
                # not render was already counted where it failed, and on a
                # machine without archify it did not fail at all.
                if not spec.is_file():
                    failures.append(name)
                out.append(f"\n> Diagram `{name}` is not available in this build.\n")
        else:
            out.append(line)
    return "\n".join(out)


def shell_value(raw: str) -> str:
    """The value of a shell assignment, without the comment after it.

    `strip('"')` was close enough to look right and wrong where it mattered:
    `JOB_SCHEDULE="0 6 * * 6"   # Saturdays` kept everything from the closing
    quote onwards, and the published schedule column carried the comment.
    """
    raw = raw.strip()
    quote = raw[:1]
    if quote not in ('"', "'"):
        return raw.split("#", 1)[0].strip()
    out, i = [], 1
    while i < len(raw):
        if raw[i] == "\\" and i + 1 < len(raw):
            out.append(raw[i + 1])
            i += 2
            continue
        if raw[i] == quote:
            break
        out.append(raw[i])
        i += 1
    return "".join(out)


def jobs_table(root: Path) -> str:
    """Every scheduled job, read out of the job files rather than listed here.

    A table of jobs written by hand is one more inventory that goes stale the
    first time one is added -- the same failure this repository's guide keeps
    warning about. This enumerates `local-cron-jobs/jobs/*.job` and the profile
    manifests on every build, so a new job appears without anyone editing prose.
    """
    jobs_dir = root / "local-cron-jobs" / "jobs"
    if not jobs_dir.is_dir():
        raise Skip("local-cron-jobs/jobs is not in this checkout")

    profiles: dict[str, list[str]] = {}
    manifests = sorted((root / "local-machine-setup" / "profiles").glob("*.cron"))
    for manifest in manifests:
        for line in manifest.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            profiles.setdefault(line.split()[0], []).append(manifest.stem)

    rows = []
    for job in sorted(jobs_dir.glob("*.job")):
        text = job.read_text()
        lead = []
        for line in text.splitlines():
            if line.startswith("#"):
                lead.append(line.lstrip("#").strip())
            elif lead:
                break
        blurb = SENTENCE.split(" ".join(lead).strip(), 1)[0] if lead else ""
        schedule, verbs = "", {}
        for line in text.splitlines():
            key, sep, value = line.partition("=")
            if not sep or key not in ("JOB_SCHEDULE", "JOB_PROMPT", "JOB_COMMAND"):
                continue
            value = shell_value(value)
            if key == "JOB_SCHEDULE":
                schedule = value
            elif value:
                verbs[key] = value
        # The runner's own precedence: a set-but-empty assignment is not a
        # declaration, and `JOB_COMMAND` wins (it refuses a job that sets both
        # to something). Reading the key alone called every shell job that
        # clears `JOB_PROMPT` an agent job -- `agent-meter` is one.
        kind = "shell" if "JOB_COMMAND" in verbs else "agent" if verbs else ""
        where = ", ".join(sorted(profiles.get(job.stem, []))) or "—"
        rows.append((job.stem, schedule or "—", kind or "—", where, blurb))

    head = ("| Job | Schedule | Runs | Profiles | What it does |\n"
            "|---|---|---|---|---|\n")
    body = "".join(
        "| `{}` | `{}` | {} | {} | {} |\n".format(
            name, schedule, kind, where, blurb.replace("|", "\\|"))
        for name, schedule, kind, where, blurb in rows)
    count = "\n*{} job files, {} profile manifests, read from the filesystem at build time.*\n".format(
        len(rows), len(manifests))
    return head + body + count


def expand_jobs(body: str, root: Path, notes: list[str],
                gaps: list[str] | None = None) -> str:
    """Swap each `@jobs` line for the generated table.

    A table this build could not read is a hole in the page, and it says so in
    `gaps`. Reporting it only in the notes let a page with no schedule in it be
    recorded as complete (#471, found in audit).
    """
    out, fenced = [], False
    for line in body.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if not fenced and JOBS.match(line):
            try:
                out.append("\n" + jobs_table(root) + "\n")
            except Skip as reason:
                notes.append(f"  SKIP     @jobs: {reason}")
                if gaps is not None:
                    gaps.append("the job table")
                out.append("\n> The job table is not available in this build.\n")
        else:
            out.append(line)
    return "\n".join(out)


def build_page(source: Path, embeds: dict[str, str], css: str, fonts: str,
               siblings: list[tuple[str, str]], notes: list[str],
               root: Path, failures: list[str], gaps: list[str] | None = None) -> str:
    meta, body = frontmatter(source.read_text())
    title = meta.get("title") or source.stem.replace("-", " ").title()
    # The checkout being rendered, not the one this file lives in. They are the
    # same in production and different under a fixture, and passing the wrong
    # one made a test build read the developer's real job files.
    body = expand_jobs(body, root, notes, gaps)
    body = expand_diagrams(body, embeds, notes, root, failures)
    rendered, headings = markdown_to_html(body)

    for name, figure in embeds.items():
        caption = (f'<figcaption>Diagram: {html.escape(name)} — '
                   f'<a href="diagram-{html.escape(name)}.html">open the interactive version</a>'
                   f' (theme, navigation and image export)</figcaption>')
        rendered = rendered.replace(
            f"<!--DIAGRAM:{name}-->", figure.replace("</figure>", caption + "</figure>"))

    rendered = rendered.replace("<table>", '<div class="table-wrap"><table>') \
                       .replace("</table>", "</table></div>")

    nav = []
    for item in headings:
        if item["level"] != 2:
            continue
        label = html.unescape(re.sub(r"<[^>]+>", "", item["name"]))
        nav.append(f'<a href="#{item["id"]}"><span>{html.escape(label, quote=False)}</span></a>')

    others = "".join(
        f'<a href="{html.escape(name)}.html">{html.escape(label)}</a><br>'
        for name, label in siblings if name != source.stem)

    head = "\n".join(filter(None, [
        f"<title>{html.escape(title)}</title>",
        f"<style>{fonts}</style>" if fonts else "",
        f"<style>{css}</style>",
        f"<style>{DIAGRAM_CSS}</style>",
    ]))
    parts = [
        '<div class="wrap">', '<aside class="rail">',
        '  <div class="badge"><b>system</b><span>design</span></div>',
        f"  <nav>{''.join(nav)}</nav>",
        f'  <div class="railnote"><strong>The other documents</strong>{others}</div>' if others else "",
        f'  <div class="railnote"><strong>Source</strong><code>{SOURCES}/{source.name}</code></div>',
        "</aside>", "<main>",
        f'  <p class="eyebrow">{html.escape(meta["eyebrow"])}</p>' if meta.get("eyebrow") else "",
        f"  <h1>{html.escape(title)}</h1>",
        f'  <p class="standfirst">{html.escape(meta["stand"])}</p>' if meta.get("stand") else "",
        f'  <article class="doc">{rendered}</article>',
        "</main>", "</div>",
    ]
    return ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
            + head + "\n</head>\n<body>\n"
            + "\n".join(p for p in parts if p) + "\n</body>\n</html>\n")


def dependencies(source: Path, root: Path) -> list[Path]:
    """Every file a page is built from, read out of the page's own directives.

    A hand-kept list here would be the same stale inventory `@jobs` exists to
    avoid, so the directives are the list: the regexes below are the ones the
    renderer expands with, and a document that stops naming a diagram stops
    depending on it in the same edit.
    """
    _, body = frontmatter(source.read_text())
    deps, fenced = [source], False
    for line in body.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        named = DIAGRAM.match(line)
        if named:
            deps.append(root / SOURCES / "diagrams" / f"{named.group(1)}.json")
        elif JOBS.match(line):
            deps += sorted((root / "local-cron-jobs" / "jobs").glob("*.job"))
            deps += sorted((root / "local-machine-setup" / "profiles").glob("*.cron"))
    # No existence filter on what the document names. A named input that is
    # gone is the thing `--check` most needs to see, and filtering it out is
    # what made a deleted job file invisible to it. The shared inputs are
    # filtered, above, because absent means "not used here".
    deps += [s for s in siblings(root) if s != source]
    return deps + shared_inputs(diagram=any(d.suffix == ".json" for d in deps))


def siblings(root: Path) -> list[Path]:
    """The other documents, which every page carries in its navigation.

    A page embeds each sibling's title and links to its page, so withdrawing or
    retitling one document leaves stale navigation in pages nothing else
    touched -- and `--check` certified them, because it read only the page's
    own directives (#473, found in review).

    The dependency is coarser than the fact: a page turns stale when a sibling
    is edited at all, not only when its title changes. Nothing can watch a
    frontmatter field's mtime, and a build writes every page anyway, so the
    cost is a check that says rebuild slightly more often than it must.
    """
    src = root / SOURCES
    if not src.is_dir():
        return []
    return sorted(p for p in src.glob("*.md")
                  if STEM.fullmatch(p.stem) and not p.stem.startswith(RESERVED))


def shared_inputs(diagram: bool = False) -> list[Path]:
    """What every page is rendered *by*, as against what it is rendered *from*.

    A renderer fix or a restyled house stylesheet changes every page, and
    `--check` read none of it -- it compared each page against its markdown and
    called a tree built by last month's code current (#471, found in review).
    Only files that are here are named: a machine without the pack renders the
    fallback stylesheet, and a missing file must not read as a deleted input.
    """
    here = [Path(__file__).resolve(),
            PACK_ROOT / "bin" / "sd_research_tokens.py",
            PACK_ROOT / "bin" / "sd_research_fonts.py"]
    if diagram:
        here.append(ARCHIFY / "bin" / "archify.mjs")
    return [path for path in here if path.is_file()]


def wanted_diagrams(source: Path, root: Path) -> set[str]:
    """The diagram names a document asks for, by the same reading as above."""
    diagrams = root / SOURCES / "diagrams"
    return {d.stem for d in dependencies(source, root) if d.parent == diagrams}


def outputs(root: Path, sources: list[Path]) -> list[tuple[Path, list[Path]]]:
    """Each file a build writes, with the inputs that decide whether it is current.

    Both kinds, because a build writes both: one page per document, and one
    standalone interactive page per diagram spec. Checking only the documents
    reported a set as fresh while a diagram page was missing outright -- the
    embedded copy and the linked one come from the same spec and go stale
    together (#471, found in review).
    """
    out_dir = root / PUBLISHED
    pairs = [(out_dir / f"{s.stem}.html", dependencies(s, root)) for s in sources]
    pairs += [(out_dir / f"diagram-{spec.stem}.html", [spec] + shared_inputs(diagram=True))
              for spec in sorted((root / SOURCES / "diagrams").glob("*.json"))]
    return pairs


#: Written beside the pages, naming what each was built from. It is not an
#: inventory to maintain -- every build overwrites it from `dependencies()` --
#: and it exists for the one question the filesystem cannot answer afterwards:
#: which inputs were there last time. A deleted job file leaves every surviving
#: file older than the page, so without this record `--check` reports fresh
#: while the published table still lists a job nobody runs (#471, in review).
#: The leading dot keeps it unservable: the document server matches names
#: starting with an alphanumeric and ending in `.html`.
MANIFEST = ".inputs.json"


def label(path: Path, root: Path) -> str:
    """How an input is named in the record: repo-relative, or absolute.

    The tools a page is rendered by live outside the checkout, and there is no
    relative name for those -- the record is a build artefact beside the pages
    it describes, never shared between machines, so an absolute one is fine.
    """
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def write_manifest(root: Path, out_dir: Path, sources: list[Path],
                   produced: set[str], written: set[str]) -> None:
    """Record every file this build wrote, and whether it finished writing it.

    Two separate facts, and folding them into one record cost two rounds of
    review. Recording only what a build *set out* to write let a failed build
    certify itself -- exit 1, then a `--check` that exits 0 over the same tree.
    Then recording only what completed lost the other fact: a partial page was
    owned by nobody, so withdrawing its source left it published for good.

    So: an entry means "this build wrote this file", which is what makes it
    ours to withdraw later; `complete` says whether the file is the one its
    source asks for, which is what `--check` reads.
    """
    record = {}
    for target, inputs in outputs(root, sources):
        if target.name not in written:
            # Existence is not authorship. A file somebody else put in the
            # published folder under a name this build would have used was
            # claimed as ours and deleted by a later sweep (#471, found in
            # audit) -- `Skip` writes nothing, and the file was still there.
            continue
        record[target.name] = {
            "inputs": sorted(label(i, root) for i in inputs),
            "complete": target.name in produced,
        }
    (out_dir / MANIFEST).write_text(json.dumps(record, indent=1, sort_keys=True))


def read_manifest(out_dir: Path) -> dict[str, dict]:
    """The last build's record, or nothing at all -- never a partial answer.

    Entries are normalised here so every reader sees one shape, including an
    entry written before `complete` existed: a file on disk from an older
    build should read as owned, not as a crash.
    """
    try:
        record = json.loads((out_dir / MANIFEST).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(record, dict):
        return {}
    entries: dict[str, dict] = {}
    for name, entry in record.items():
        if isinstance(entry, list):
            entries[name] = {"inputs": entry, "complete": True}
        elif isinstance(entry, dict) and isinstance(entry.get("inputs"), list):
            entries[name] = {"inputs": entry["inputs"],
                             "complete": bool(entry.get("complete"))}
    return entries


def orphans(out_dir: Path, recorded: dict[str, dict],
            current: list[tuple[Path, list[Path]]]) -> set[str]:
    """Pages the last build wrote that this one would not.

    Deleting a source used to leave its page published and served for good --
    the check walked the outputs a build *would* write, so a withdrawn document
    was simply not looked at, and a rebuild never removed it (#471, found in
    review). The manifest is what makes this answerable: only a file the last
    build recorded writing is ever named here, so nothing else in the folder is
    touched.
    """
    # Asked of the filesystem, not of the string. `docs/dashboard` is usually
    # on a case-insensitive volume, where renaming `Alpha.md` to `alpha.md`
    # leaves `Alpha.html` in the record, `alpha.html` in the plan, and one file
    # on disk -- so the sweep deleted the page the same build had just written,
    # and still exited 0 (#471, found in audit). `samefile` compares device and
    # inode, which is the only reading that survives a volume's own case rules;
    # `os.path.normcase` is the identity on this platform and answers nothing.
    keep = {target.name for target, _ in current}

    def is_kept(name: str) -> bool:
        if name in keep:
            return True
        here = out_dir / name
        for target, _ in current:
            try:
                if here.samefile(target):
                    return True
            except OSError:
                continue
        return False

    return {name for name in recorded
            if (out_dir / name).exists() and not is_kept(name)}


def render(root: Path | None = None, check: bool = False) -> int:
    root = repo_root(root)
    src_dir, out_dir = root / SOURCES, root / PUBLISHED
    # No early return on an empty or absent source folder. The pages from the
    # last build are still published and still served, and withdrawing the last
    # document -- or the whole folder -- used to be the one way to leave them
    # there for good: the run that should have swept them exited first (#471,
    # found in review). `missing` is reported at the end, after the sweep.
    missing = (f"no {SOURCES}/ in {root}" if not src_dir.is_dir()
               else f"no documents in {SOURCES}/" if not any(
                   STEM.fullmatch(p.stem) and not p.stem.startswith(RESERVED)
                   for p in src_dir.glob("*.md"))
               else "")
    sources = sorted(p for p in src_dir.glob("*.md")
                     if STEM.fullmatch(p.stem)
                     and not p.stem.startswith(RESERVED)) if src_dir.is_dir() else []
    reserved = sorted(p.name for p in src_dir.glob(f"{RESERVED}*.md")) if src_dir.is_dir() else []

    titles, unreadable = [], []
    for path in sources:
        try:
            meta, _ = frontmatter(path.read_text())
        except OSError as why:
            # A dangling symlink or an unreadable file used to come out as a
            # raw traceback from both modes (#471, found in audit).
            unreadable.append(f"{path.name}: {why.strerror or why}")
            continue
        titles.append((path.stem, meta.get("title") or path.stem))
    sources = [p for p in sources if p.stem in {stem for stem, _ in titles}]

    if check:
        recorded = read_manifest(out_dir)
        current = outputs(root, sources)
        # Each finding carries whether the absence of archify explains it. The
        # first answer to #471 was a blanket "not configured here" for any tree
        # holding a spec, and that made every other finding vanish on a machine
        # with no archify -- CI is such a machine, and it went green over four
        # real ones (#473, found in CI). A finding archify cannot cause is a
        # verdict, and a verdict outranks a machine's limits.
        stale: list[tuple[str, bool]] = [
            (f"{name:28} published, but no source produces it any more", False)
            for name in sorted(orphans(out_dir, recorded, current))]
        for target, inputs in current:
            named = sorted(label(i, root) for i in inputs)
            # Only these two findings can be archify's doing, and only on a
            # target a diagram goes into: a page is never drawn, or is written
            # with a hole where the diagram was.
            drawn = (target.name.startswith(RESERVED)
                     or any(i.suffix == ".json" for i in inputs))
            if not target.exists():
                stale.append((f"{target.name:28} not rendered", drawn))
                continue
            was = recorded.get(target.name)
            if was is None:
                stale.append((f"{target.name:28} built before its inputs were recorded",
                              False))
                continue
            if not was["complete"]:
                stale.append((f"{target.name:28} built without everything it names",
                              drawn))
                continue
            if was["inputs"] != named:
                # The deletion case, which no comparison of surviving files can
                # reach: the page still carries a job that is gone, and every
                # file still on disk is older than it.
                changed = ([f"-{n}" for n in was["inputs"] if n not in named]
                           + [f"+{n}" for n in named if n not in was["inputs"]])
                stale.append((f"{target.name:28} inputs changed: {', '.join(changed)}",
                              False))
                continue
            built = target.stat().st_mtime
            gone = sorted(i.name for i in inputs if not i.exists())
            if gone:
                stale.append((f"{target.name:28} names {', '.join(gone)}, which is gone",
                              False))
                continue
            newer = sorted(i.name for i in inputs if i.stat().st_mtime > built)
            if newer:
                stale.append((f"{target.name:28} older than {', '.join(newer)}", False))
        for name in reserved:
            stale.append((f"{name:28} claims a name this renderer writes", False))
        for why in unreadable:
            stale.append((f"{why:28} cannot be read", False))
        for line, _ in stale:
            print(f"STALE    {line}")
        print(f"design documents: {len(sources)} source(s), {len(stale)} stale")
        if any(not soft for _, soft in stale):
            return 1
        if stale and not has_archify():
            # The two modes used to contradict each other here: the build
            # exited 0, as the `Skip` contract says it should, and the check
            # exited 1 for ever over the same tree (#471, found in audit). A
            # machine that cannot render a diagram cannot judge one either, so
            # the answer is the repository's "not configured here", not a
            # verdict.
            print(f"archify is not on this machine ({ARCHIFY}); "
                  f"the diagram pages cannot be checked", file=sys.stderr)
            return 3
        if stale:
            return 1
        if missing:
            print(missing, file=sys.stderr)
            return 3
        return 0

    if missing and not out_dir.is_dir():
        print(missing, file=sys.stderr)
        return 3  # nothing to render and nothing published: nothing to do

    out_dir.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    failures: list[str] = []
    embeds = build_diagrams(root, out_dir, notes, failures)

    wrote = {f"{RESERVED}{name}.html" for name in embeds}
    produced = set(wrote)
    written = 0
    for path in sources:
        gaps: list[str] = []
        try:
            page = build_page(path, embeds, *house_css(), titles, notes, root,
                              failures, gaps)
        except Skip as why:
            notes.append(f"  SKIP     {path.name:28} {why}")
            continue
        target = out_dir / f"{path.stem}.html"
        target.write_text(page)
        written += 1
        wrote.add(target.name)
        notes.append(f"  page     {path.stem:28} {len(page)//1024:4d} KB")
        # A page carrying "this is not available" is written, because the prose
        # is worth having, and is not recorded as complete, because it is not
        # the page the source asks for. `gaps` is how the page reports its own
        # holes: a skipped `@jobs` table was invisible here, so a page with no
        # schedule in it was recorded complete (#471, found in audit).
        gaps += sorted(wanted_diagrams(path, root) - set(embeds))
        if gaps:
            notes.append(f"  partial  {path.stem:28} without {', '.join(sorted(set(gaps)))}")
        else:
            produced.add(target.name)

    for name in sorted(orphans(out_dir, read_manifest(out_dir), outputs(root, sources))):
        (out_dir / name).unlink()
        notes.append(f"  removed  {name:28} no source produces it any more")
    write_manifest(root, out_dir, sources, produced, wrote)

    print(f"{root.name}  ->  {PUBLISHED}/")
    for line in notes:
        print(line)
    print(f"  {written} page(s), {len(embeds)} diagram(s)")
    if failures:
        print(f"  FAILED   {len(failures)} diagram(s): {', '.join(sorted(set(failures)))}")
    for name in reserved:
        print(f"  REFUSED  {name:28} claims a name this renderer writes", file=sys.stderr)
    for why in unreadable:
        print(f"  UNREAD   {why}", file=sys.stderr)
    if missing:
        print(missing, file=sys.stderr)
        return 3
    return (0 if written == len(sources) and not failures
            and not reserved and not unreadable else 1)


def hashes(root: Path | None = None) -> int:
    """Print the CSP digests the rendered diagram pages actually need.

    `documents.py` pins these rather than deriving them per file, so an archify
    upgrade that changes either script leaves the diagram pages inert until the
    pin moves. This is the command that says what to move it to.

    It exits 0 only when the pin is right: the scripts on the pages are exactly
    `documents.DIAGRAM_SCRIPTS`, and every one of them is on every page. "Every
    script is shared by every page" was the test, and zero scripts or one pass
    it -- so an archify that dropped a script, or both, exited 0 over a pin that
    no longer matched anything (#473, found in review). A consistent set the pin
    does not name is still printed, as the lines to paste, and still exits 1:
    until the pin moves, every diagram page is inert under the served policy.
    """
    import base64
    import hashlib

    # Read at call time, from the module that sends the policy, so a test that
    # stands in for the pin and the pin itself are the same object.
    from sd_dashboard.documents import DIAGRAM_SCRIPTS as pinned

    out_dir = repo_root(root) / PUBLISHED
    pages = sorted(out_dir.glob("diagram-*.html"))
    if not pages:
        print(f"no rendered diagram pages in {PUBLISHED}/; run without --hashes first",
              file=sys.stderr)
        return 3

    # Pages per digest, not occurrences: a page carrying one script twice
    # counted as two pages, and could make a script missing from another page
    # read as shared by all of them.
    seen: dict[str, set[str]] = {}
    for page in pages:
        for attrs, body in re.findall(r"<script([^>]*)>(.*?)</script>", page.read_text(), re.S):
            if "application/json" in attrs:
                continue
            digest = "sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
            seen.setdefault(digest, set()).add(page.name)

    for digest, where in sorted(seen.items()):
        print(f'    "{digest}",   # {len(where)}/{len(pages)} pages')
    shared = {d for d, where in seen.items() if len(where) == len(pages)}
    print(f"  {len(seen)} distinct script(s) over {len(pages)} page(s); "
          f"{len(shared)} shared by all")

    problems = []
    if not seen:
        problems.append("no page carries an inline script")
    problems += [f"{d} is on {len(seen[d])}/{len(pages)} pages" for d in sorted(set(seen) - shared)]
    problems += [f"{d} is pinned and on no page" for d in sorted(set(pinned) - set(seen))]
    problems += [f"{d} is on every page and not pinned" for d in sorted(shared - set(pinned))]
    for why in problems:
        print(f"  STALE    {why}", file=sys.stderr)
    if problems and seen and set(seen) == shared:
        print("  the set is consistent: move sd_dashboard/documents.py DIAGRAM_SCRIPTS "
              "to the digests above", file=sys.stderr)
    return 1 if problems else 0


def main(argv: list[str]) -> int:
    modes = {"--check", "--hashes"}
    unknown = [a for a in argv[1:] if a not in modes]
    if unknown:
        print(f"usage: design_docs.py [--check|--hashes]  (got {unknown[0]!r})", file=sys.stderr)
        return 2
    if "--hashes" in argv[1:]:
        return hashes()
    return render(check="--check" in argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
