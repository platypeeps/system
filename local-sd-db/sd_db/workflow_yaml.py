"""The YAML GitHub workflow files are written in, read as structure.

`protection` credits an aggregate job only when its keys and values are
exactly the template `design.md` prescribes, and a line match cannot tell
that: YAML spells one key many ways -- quoted, in a flow mapping, explicit,
merged, aliased -- and each spelling the matcher missed was a review round
(sd:1741, sd:1820). PyYAML is not stdlib and this package has no
dependencies, and `yaml_lite` refuses block sequences, which every workflow
has. So this reads the subset workflows use into dicts, lists and strings:

    key: plain, 'single' or "double" scalar
    key: [flow, sequence]               # and { flow: mapping }, on one line
    key: |                              # literal; `>` folded, `-`/`+` chomping
      block scalar
    key:
      - item                            # block sequences, `- key: value` items
    "quoted key": value

Every scalar stays a string: `true` is `"true"`, and an empty value is
None. What it refuses, with `Refused` and the line number: anchors,
aliases, tags, merge keys (`<<`), explicit keys (`?`), duplicate keys,
directives, a second document, tabs in indentation, scalars that span
lines (plain or quoted), flow collections that span lines, an indentation
indicator on a block scalar, a folded scalar whose lines are not all
plain, and nesting deeper than Python's recursion limit. A file needing any of those is read as unknown by the caller, never
guessed at.
"""

from __future__ import annotations

from typing import Any

#: Double-quoted escapes read; any other refuses.
_ESCAPES = {"\\": "\\", '"': '"', "/": "/", "n": "\n", "t": "\t", "r": "\r", "0": "\0", " ": " "}
#: Characters that start an anchor, alias or tag, or are reserved.
_REFUSED_START = {"&": "an anchor", "*": "an alias", "!": "a tag", "%": "a directive",
                  "@": "a reserved indicator", "`": "a reserved indicator"}


class Refused(ValueError):
    """YAML this reader will not guess at. Carries the 1-based line."""

    def __init__(self, message: str, line: int) -> None:
        self.line = line
        super().__init__(f"line {line}: {message}")


def load(text: str) -> Any:
    """The document's value: a dict, a list, a string or None."""
    try:
        return _Reader(text.removeprefix("\ufeff")).document()
    except RecursionError:
        raise Refused("nesting deeper than this reader follows", 1) from None


def _is_item(text: str) -> bool:
    return text == "-" or text.startswith(("- ", "-\t"))


def _comment_or_end(tail: str) -> bool:
    """Whether `tail`, after a value, is nothing or a comment."""
    return not tail.strip() or (tail[0] in " \t" and tail.lstrip(" \t").startswith("#"))


class _Reader:
    def __init__(self, text: str) -> None:
        self.raw = text.split("\n")
        # [raw index, indent, text] per line that is neither blank nor a
        # comment. A block scalar's lines are read from `raw` and skipped here.
        self.rows: list[list[Any]] = []
        for index, line in enumerate(self.raw):
            content = line.lstrip(" ")
            if content.strip(" \t") and not content.startswith("#"):
                self.rows.append([index, len(line) - len(content), content.rstrip(" \t")])
        self.at = 0

    def refuse(self, message: str, index: int | None = None) -> Refused:
        if index is None:
            index = self.rows[self.at][0] if self.at < len(self.rows) else len(self.raw) - 1
        return Refused(message, index + 1)

    def document(self) -> Any:
        if self.rows and self.rows[0][1] == 0 and _comment_or_end(self.rows[0][2][3:]) \
                and self.rows[0][2].startswith("---"):
            self.at = 1
        if self.at >= len(self.rows):
            return None
        value = self.node(self.rows[self.at][1])
        if self.at < len(self.rows):
            raise self.refuse("content after the document's top node")
        return value

    def node(self, indent: int) -> Any:
        """The block node whose first row is the current one, at `indent`."""
        index, _, text = self.rows[self.at]
        if _is_item(text):
            return self.sequence(indent)
        if self.key(text, index) is not None:
            return self.mapping(indent)
        if text[0] in "|>":
            raise self.refuse("a block scalar with no key or item")
        self.at += 1
        return self.inline(text, index)

    def mapping(self, indent: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        while self.at < len(self.rows) and self.rows[self.at][1] == indent:
            index, _, text = self.rows[self.at]
            split = self.key(text, index)
            if split is None:
                raise self.refuse("expected `key: value`")
            key, rest = split
            if key in result:
                raise self.refuse(f"duplicate key {key!r}")
            self.at += 1
            result[key] = self.value(rest, indent, index, compact=True)
        self.dedented(indent)
        return result

    def sequence(self, indent: int) -> list[Any]:
        result: list[Any] = []
        while self.at < len(self.rows) and self.rows[self.at][1] == indent \
                and _is_item(self.rows[self.at][2]):
            row = self.rows[self.at]
            index, text = row[0], row[2]
            rest = text[1:].lstrip(" ")
            if rest.startswith("\t"):
                raise self.refuse("a tab in indentation")
            if rest and not rest.startswith("#") and (_is_item(rest) or self.key(rest, index) is not None):
                # `- key: value` or `- - item`: the rest is a node at its own column.
                row[1], row[2] = indent + len(text) - len(rest), rest
                result.append(self.node(row[1]))
                continue
            self.at += 1
            result.append(self.value(rest, indent, index, compact=False))
        self.dedented(indent)
        return result

    def dedented(self, indent: int) -> None:
        if self.at < len(self.rows) and self.rows[self.at][1] > indent:
            raise self.refuse("unexpected indentation")

    def value(self, rest: str, indent: int, index: int, *, compact: bool) -> Any:
        """The value after `key:` or `-` on row `index`, whose parent is at
        `indent`; `compact` lets a mapping's value be a block sequence at
        the key's own indent."""
        if not rest or rest.startswith("#"):
            if self.at < len(self.rows):
                below, text = self.rows[self.at][1], self.rows[self.at][2]
                if below > indent:
                    return self.node(below)
                if compact and below == indent and _is_item(text):
                    return self.sequence(indent)
            return None
        if rest[0] in "|>":
            return self.block(rest, indent, index)
        return self.inline(rest, index)

    def key(self, text: str, index: int) -> tuple[str, str] | None:
        """`(key, rest)` when `text` is a mapping entry, else None."""
        if text.startswith("\t"):
            raise Refused("a tab in indentation", index + 1)
        if text == "?" or text.startswith(("? ", "?\t")):
            raise Refused("an explicit key (`?`)", index + 1)
        if text[0] in "\"'":
            key, end = self.quoted(text, 0, index)
            tail = text[end:].lstrip(" \t")
            if not tail.startswith(":") or (len(tail) > 1 and tail[1] not in " \t"):
                return None
            return key, tail[1:].strip(" \t")
        if text[0] in "[{":
            return None
        for at, char in enumerate(text):
            if char == "#" and at and text[at - 1] in " \t":
                return None
            if char == ":" and (at + 1 == len(text) or text[at + 1] in " \t"):
                break
        else:
            return None
        key = text[:at].rstrip(" \t")
        if not key:
            return None
        if key[0] in _REFUSED_START:
            raise Refused(f"{_REFUSED_START[key[0]]} in a key", index + 1)
        if key == "<<":
            raise Refused("a merge key (`<<`)", index + 1)
        return key, text[at + 1:].strip(" \t")

    def inline(self, text: str, index: int) -> Any:
        """A value that ends on its own line: flow, quoted or plain."""
        first = text[0]
        if first in _REFUSED_START:
            raise Refused(_REFUSED_START[first], index + 1)
        if first in "[{":
            value, end = self.flow(text, 0, index)
        elif first in "\"'":
            value, end = self.quoted(text, 0, index)
        else:
            if _is_item(text) or text == "?" or text.startswith("? ") or first in "]},":
                raise Refused("a block entry or indicator inside a value", index + 1)
            end = len(text)
            for at in range(1, len(text)):
                if text[at] == "#" and text[at - 1] in " \t":
                    end = at - 1
                    break
            value = text[:end].rstrip(" \t")
            if ": " in value or ":\t" in value or value.endswith(":"):
                raise Refused("a mapping inside a plain scalar", index + 1)
        if not _comment_or_end(text[end:]):
            raise Refused("content after a value", index + 1)
        return value

    def quoted(self, text: str, start: int, index: int) -> tuple[str, int]:
        """The quoted scalar at `start` and the offset just past it."""
        quote = text[start]
        out: list[str] = []
        at = start + 1
        while at < len(text):
            char = text[at]
            if quote == "'" and char == "'":
                if text[at + 1:at + 2] == "'":
                    out.append("'")
                    at += 2
                    continue
                return "".join(out), at + 1
            if quote == '"' and char == '"':
                return "".join(out), at + 1
            if quote == '"' and char == "\\":
                escape = text[at + 1:at + 2]
                if escape not in _ESCAPES:
                    raise Refused(f"the escape \\{escape}", index + 1)
                out.append(_ESCAPES[escape])
                at += 2
                continue
            out.append(char)
            at += 1
        raise Refused("a quoted scalar that spans lines", index + 1)

    def flow(self, text: str, at: int, index: int) -> tuple[Any, int]:
        """The flow collection at `at`, which must close on this line."""
        closing = "]" if text[at] == "[" else "}"
        result: Any = [] if closing == "]" else {}
        at += 1
        while True:
            at = self.skip(text, at, index)
            if text[at] == closing:
                return result, at + 1
            if closing == "]":
                item, at = self.flow_node(text, at, index, key=False)
                result.append(item)
            else:
                key, at = self.flow_node(text, at, index, key=True)
                if not isinstance(key, str):
                    raise Refused("a collection as a key", index + 1)
                at = self.skip(text, at, index)
                if text[at] != ":":
                    raise Refused("a flow mapping key with no value", index + 1)
                if key in result:
                    raise Refused(f"duplicate key {key!r}", index + 1)
                result[key], at = self.flow_node(text, self.skip(text, at + 1, index), index, key=False)
            at = self.skip(text, at, index)
            if text[at] == ",":
                at += 1
            elif text[at] != closing:
                raise Refused(f"expected `,` or `{closing}`", index + 1)

    def skip(self, text: str, at: int, index: int) -> int:
        while at < len(text) and text[at] in " \t":
            at += 1
        if at >= len(text) or text[at] == "#":
            raise Refused("a flow collection that spans lines", index + 1)
        return at

    def flow_node(self, text: str, at: int, index: int, *, key: bool) -> tuple[Any, int]:
        char = text[at]
        if char in _REFUSED_START or char == "?":
            raise Refused(_REFUSED_START.get(char, "an explicit key (`?`)"), index + 1)
        if char in "[{":
            return self.flow(text, at, index)
        if char in "\"'":
            return self.quoted(text, at, index)
        if char in ",]}:":
            raise Refused("an empty flow entry", index + 1)
        start = at
        while at < len(text) and text[at] not in ",[]{}":
            if text[at] == ":" and (at + 1 == len(text) or text[at + 1] in " \t,]}"):
                break
            if text[at] == "#" and text[at - 1] in " \t":
                break
            at += 1
        if not key and at < len(text) and text[at] == ":":
            raise Refused("a mapping inside a flow sequence", index + 1)
        return text[start:at].rstrip(" \t"), at

    def block(self, header: str, indent: int, index: int) -> str:
        """The block scalar whose header (`|`, `>-`, ...) is on row `index`."""
        style, chomp, tail = header[0], "", header[1:]
        if tail[:1] in ("-", "+"):
            chomp, tail = tail[0], tail[1:]
        if tail[:1].isdigit():
            raise Refused("an indentation indicator on a block scalar", index + 1)
        if not _comment_or_end(tail):
            raise Refused("content after a block scalar header", index + 1)
        lines: list[str] = []
        width = None
        end = index + 1
        while end < len(self.raw):
            line = self.raw[end]
            if line.strip(" ") == "":
                lines.append(line[width:] if width is not None else "")
            else:
                own = len(line) - len(line.lstrip(" "))
                if width is None:
                    if own <= indent:
                        break
                    width = own
                elif own < width:
                    break
                lines.append(line[width:])
            end += 1
        while self.at < len(self.rows) and self.rows[self.at][0] < end:
            self.at += 1
        body = lines
        while body and body[-1] == "":
            body = body[:-1]
        trailing = len(lines) - len(body)
        if style == ">":
            if any(not line or line[0] in " \t" for line in body):
                raise Refused("a folded scalar with blank or indented lines", index + 1)
            text = " ".join(body)
        else:
            text = "\n".join(body)
        if not body:
            return "\n" * trailing if chomp == "+" else ""
        if end == len(self.raw) and not trailing and not self.raw[-1] == "":
            return text  # the file ends inside the scalar, with no line break
        if chomp == "-":
            return text
        if chomp == "+":
            return text + "\n" * (1 + trailing)
        return text + "\n"
