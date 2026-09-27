"""Canonical final-publication renderer, shared by preview and claim validation.

The small Markdown renderer comes from the writing pack's established review
renderer. Publication snapshots local image bytes and rejects missing assets.
"""

import base64
import hashlib
import html as htmllib
import json
import mimetypes
import os
import re
import struct
from pathlib import Path

from . import paths as sdpaths
from .sources.frontmatter import read as read_frontmatter
from .sources.vault import vault_root
from .workflow import WorkflowError

DOC_MAX_W = 624
DOC_MAX_H = 800
BLOCK_STYLE = "margin:0 0 12pt 0"
HEAD_STYLE = "margin:18pt 0 8pt 0"


def die(message):
    raise WorkflowError(message)


def _image_size(path: str) -> "tuple[int, int] | None":
    """Intrinsic (width, height) without a third-party imaging library.

    None means "could not tell" — the caller then ships the image unsized,
    which is what it did before any of this. A zero dimension is a malformed
    header and reports as None too, so _fit never divides by it.
    """
    def ok(w, h):
        return (int(w), int(h)) if w and h else None

    with open(path, "rb") as f:
        head = f.read(26)
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", head[16:24])
            return ok(w, h)
        if head[:2] == b"\xff\xd8":            # JPEG: walk the segment markers
            f.seek(2)
            while True:
                b = f.read(1)
                while b and b != b"\xff":
                    b = f.read(1)
                marker = f.read(1)
                while marker == b"\xff":
                    marker = f.read(1)
                if not marker:
                    return None
                if marker[0] in range(0xC0, 0xCF) and marker[0] not in (0xC4, 0xC8, 0xCC):
                    f.read(3)
                    h, w = struct.unpack(">HH", f.read(4))
                    return ok(w, h)
                seg = f.read(2)
                if len(seg) < 2:
                    return None
                f.seek(struct.unpack(">H", seg)[0] - 2, 1)
    return None


def _fit(w: int, h: int) -> "tuple[int, int]":
    """Scale down to fit the page box, preserving aspect ratio. Never scales up."""
    scale = min(DOC_MAX_W / w, DOC_MAX_H / h, 1.0)
    return max(1, round(w * scale)), max(1, round(h * scale))


def _md_spans(s: str, piece_dir: str) -> str:
    """Inline markdown -> HTML. Code first, then links, then emphasis."""
    codes: list[str] = []

    def stash_code(m):
        codes.append(m.group(1))
        return f"\x00CODE{len(codes) - 1}\x00"

    s = re.sub(r"`([^`]+)`", stash_code, s)

    links: list[tuple[str, str, str]] = []

    def stash(kind):
        def go(m):
            links.append((kind, m.group(1), m.group(2)))
            return f"\x00LINK{len(links) - 1}\x00"
        return go

    s = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", stash("img"), s)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", stash("a"), s)

    s = htmllib.escape(s, quote=False)
    s = re.sub(r"~~(.+?)~~", r"<s>\1</s>", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\*\w])\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", s)

    for i, (kind, text, target) in enumerate(links):
        if kind == "img":
            repl = _embed_image(target, text, piece_dir)
        else:
            repl = (f'<a href="{htmllib.escape(target, quote=True)}">'
                    f'{htmllib.escape(text, quote=False)}</a>')
        s = s.replace(f"\x00LINK{i}\x00", repl)
    for i, c in enumerate(codes):
        s = s.replace(f"\x00CODE{i}\x00",
                      f"<code>{htmllib.escape(c, quote=False)}</code>")
    return s


def _embed_image(rel: str, alt: str, piece_dir: str) -> str:
    path = rel if os.path.isabs(rel) else os.path.join(piece_dir, rel)
    if not os.path.exists(path):
        # Loud on purpose: a missing screenshot has to be visible in the doc
        # rather than a silently absent figure a reviewer never knows about.
        return (f'<p><strong>[MISSING IMAGE: '
                f'{htmllib.escape(rel)}]</strong></p>')
    mime = mimetypes.guess_type(path)[0] or "image/png"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    size = _image_size(path)
    dims = ""
    if size:
        w, h = _fit(*size)
        dims = f' width="{w}" height="{h}"'
    img = (f'<img src="data:{mime};base64,{b64}" '
           f'alt="{htmllib.escape(alt, quote=True)}"{dims} />')
    if not alt:
        return img
    # Caption = the alt text, generated not authored; see bug 3 above and the
    # matching drop rule in sdw-review-pull step 2b.
    return f'{img}<br /><em>{htmllib.escape(alt, quote=False)}</em>'


def _fence_closer(line: str):
    opening = re.match(r"^(`{3,}|~{3,})", line)
    if opening:
        fence = opening.group(1)
        return re.compile(re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*$")
    return None


def md_to_html(md: str, piece_dir: str) -> str:
    lines = md.split("\n")
    out, i, n = [], 0, len(lines)
    depths = []
    closing = None
    # Code comments must not set the document's prose heading depth.
    for line in lines:
        s = line.strip()
        if closing is not None:
            if closing.fullmatch(s):
                closing = None
            continue
        closing = _fence_closer(s)
        if closing is None:
            m = re.match(r"^(#{1,6})\s+\S", s)
            if m:
                depths.append(len(m.group(1)))
    shift = (min(depths) - 2) if depths else 0

    while i < n:
        raw = lines[i]
        s = raw.strip()
        if not s:
            i += 1
            continue
        closing = _fence_closer(s)
        if closing is not None:
            code = []
            i += 1
            while i < n and not closing.fullmatch(lines[i].strip()):
                code.append(lines[i])
                i += 1
            if i == n:
                die("unclosed fenced code block in document")
            out.append(f'<pre><code>{htmllib.escape(chr(10).join(code))}</code></pre>')
            i += 1
            continue
        if s == "---":
            out.append("<hr />")
            i += 1
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            lvl = max(2, min(len(m.group(1)) - shift, 6))
            out.append(f'<h{lvl} style="{HEAD_STYLE}">'
                       f"{_md_spans(m.group(2), piece_dir)}</h{lvl}>")
            i += 1
            continue

        if (s.startswith("|") and i + 1 < n
                and re.match(r"^\|[\s:|-]+\|?$", lines[i + 1].strip())):
            header = [c.strip() for c in s.strip("|").split("|")]
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append([c.strip()
                             for c in lines[i].strip().strip("|").split("|")])
                i += 1
            t = ['<table border="1" cellspacing="0" cellpadding="4"><thead><tr>']
            t += [f"<th>{_md_spans(c, piece_dir)}</th>" for c in header]
            t.append("</tr></thead><tbody>")
            for r in rows:
                t.append("<tr>" + "".join(
                    f"<td>{_md_spans(c, piece_dir)}</td>" for c in r) + "</tr>")
            t.append("</tbody></table>")
            out.append("".join(t))
            continue

        if re.match(r"^[-*]\s+", s):
            items: list[str] = []
            while i < n:
                cur, cs = lines[i], lines[i].strip()
                if re.match(r"^[-*]\s+", cs):
                    items.append(re.sub(r"^[-*]\s+", "", cs))
                    i += 1
                elif cs and items and (len(cur) - len(cur.lstrip())) >= 2:
                    items[-1] += " " + cs      # continuation; see bug 2 above
                    i += 1
                else:
                    break
            out.append("<ul>" + "".join(
                f'<li style="{BLOCK_STYLE}">{_md_spans(x, piece_dir)}</li>'
                for x in items) + "</ul>")
            continue

        if re.match(r"^!\[[^\]]*\]\([^)]+\)$", s):
            out.append(f'<p style="{BLOCK_STYLE}">{_md_spans(s, piece_dir)}</p>')
            i += 1
            continue

        para = []
        while i < n:
            cs = lines[i].strip()
            if (not cs or re.match(r"^(#{1,6})\s+", cs)
                    or re.match(r"^(`{3,}|~{3,})", cs)
                    or re.match(r"^[-*]\s+", cs)
                    or (cs.startswith("|") and i + 1 < n and re.match(r"^\|[\s:|-]+\|?$", lines[i + 1].strip()))
                    or cs == "---" or re.match(r"^!\[[^\]]*\]\([^)]+\)$", cs)):
                break
            para.append(cs)
            i += 1
        if para:
            out.append(f'<p style="{BLOCK_STYLE}">'
                       f"{_md_spans(' '.join(para), piece_dir)}</p>")
    return "\n".join(out)


def draft_word_count(draft: str) -> int:
    """Words of prose: no image lines, no link targets, no markup punctuation."""
    body = "\n".join(line for line in draft.split("\n")
                     if not line.strip().startswith("!["))
    body = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", body)
    return len(re.sub(r"[#*`|~]", " ", body).split())

def build(snapshot, metadata, repo):
    title = metadata.get("title")
    if not isinstance(title, str) or not title.strip():
        die("publication needs a title")
    match = re.search(r"(?ms)^## Draft\s*\n(.*)\Z", snapshot["text"])
    if not match or not match.group(1).strip():
        die("publication needs a nonempty Draft section")
    markdown = match.group(1).strip("\n")
    tip = metadata.get("tip") or ""
    tip_source = None
    if tip:
        if not isinstance(tip, str) or Path(tip).name != tip:
            die("attached tip must be one note title")
        manifest = json.loads((sdpaths.disk(repo) / "sd-plugin.json").read_text())
        store = manifest.get("store") or {}
        relative = (store.get("bases") or {}).get("tip")
        raw_root = store.get("root")
        if not isinstance(relative, str) or not isinstance(raw_root, str):
            die("writing manifest has no tip store")
        root = vault_root() if raw_root == "$OBSIDIAN_VAULT" else Path(os.path.expandvars(os.path.expanduser(raw_root)))
        path = root / relative / (tip + ".md")
        if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
            die("attached tip is missing or outside its declared vault")
        data = path.read_bytes()
        fields, prose = read_frontmatter(data.decode("utf-8"))
        if fields.get("status") != "approved" or fields.get("used-by"):
            die("attached tip must still be approved and unused")
        found = re.search(r"(?ms)^## Tip\s*\n(.*?)(?=^## |\Z)", prose)
        if not found or not found.group(1).strip():
            die("attached tip has no usable Tip section")
        tip_source = {"path": str(path.resolve()), "sha256": hashlib.sha256(data).hexdigest()}
        markdown += "\n\n---\n\n**Tip — " + tip + "**\n\n" + found.group(1).strip()
    directory = snapshot["path"].parent
    assets = {}
    for relative in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", markdown):
        path = directory / relative
        if Path(relative).is_absolute() or not path.resolve().is_relative_to(directory.resolve()) or not path.is_file():
            die("publication image is missing or outside the piece: " + relative)
        if _image_size(str(path)) is None:
            die("publication image dimensions are unavailable: " + relative)
        assets[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    html = ('<!doctype html><html><head><meta charset="utf-8"></head><body>'
            f'<h1>{htmllib.escape(title)}</h1>\n{md_to_html(markdown, str(directory))}\n</body></html>')
    for relative, digest in assets.items():
        if hashlib.sha256((directory / relative).read_bytes()).hexdigest() != digest:
            die("publication image changed while rendering: " + relative)
    return {"title": title, "html": html, "source_hashes": snapshot["hashes"],
            "assets": assets, "tip": tip, "tip_source": tip_source,
            "words": draft_word_count(markdown)}
