"""The small slice of YAML the provider registry is written in.

The registry file is the one document this library parses, its shape is
fixed by the pack's `WORKFLOW.md`, and the package has no dependencies --
both repositories install it as a copy at a tag, and a dependency would be a
second thing to pin, install and keep in step for one file.

So this parses that shape and refuses everything else, loudly. What it
accepts:

    section:                        # a top-level block mapping
      name: scalar
      name: { key: value, key: [a, b], key: { in: 1, out: 2 } }
      name: [a, b, c]
      name: { key: value,
              key: value }          # a flow value may wrap while it is open

Scalars are bare words, `"double quoted"` or `'single quoted'` strings, and
the words `true`, `false` and `null`; a bare word that looks like a number
becomes one. `#` starts a comment outside quotes. Tabs are refused, because
a tab in an indented block is a silent misparse in every YAML
implementation.

What it does not accept, and says so: block sequences (`- item`), nesting
deeper than two levels outside a flow value, anchors, aliases, multi-line
scalars, and documents. A registry needing any of those is a registry that
has outgrown this file, and the refusal is the signal to say so rather than
to guess.
"""

from __future__ import annotations

from typing import Any

CONSTANTS = {"true": True, "false": False, "null": None, "~": None}


class YamlLiteError(ValueError):
    """A line this parser will not guess at. Carries the line number."""

    def __init__(self, message: str, line: int, text: str = "") -> None:
        self.line = line
        super().__init__(f"line {line}: {message}" + (f": {text!r}" if text else ""))


def _strip_comment(text: str) -> str:
    """Remove a `#` comment, ignoring one inside a quoted scalar."""
    quote = ""
    for index, character in enumerate(text):
        if quote:
            if character == quote:
                quote = ""
            continue
        if character in "\"'":
            quote = character
            continue
        if character == "#" and (index == 0 or text[index - 1] in " \t"):
            return text[:index]
    return text


def _open_depth(text: str) -> int:
    """How many flow collections the line leaves open."""
    depth = 0
    quote = ""
    for character in text:
        if quote:
            if character == quote:
                quote = ""
            continue
        if character in "\"'":
            quote = character
        elif character in "{[":
            depth += 1
        elif character in "}]":
            depth -= 1
    return depth


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """`(line number, text)` with comments gone and wrapped flows joined."""
    joined: list[tuple[int, str]] = []
    buffer = ""
    start = 0
    depth = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw.expandtabs(1).replace(raw, raw) and "\t" in raw:
            raise YamlLiteError("tab in indentation or content", number, raw)
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            if depth:
                raise YamlLiteError("blank line inside a flow collection", number)
            continue
        if depth:
            buffer += " " + line.strip()
        else:
            buffer = line
            start = number
        depth += _open_depth(line)
        if depth < 0:
            raise YamlLiteError("unbalanced `}` or `]`", number, raw)
        if depth == 0:
            joined.append((start, buffer))
    if depth:
        raise YamlLiteError("flow collection left open at end of file", start)
    return joined


class _Flow:
    """A recursive-descent reader for one flow value."""

    def __init__(self, text: str, line: int) -> None:
        self.text = text
        self.line = line
        self.at = 0

    def error(self, message: str) -> YamlLiteError:
        return YamlLiteError(f"{message} at column {self.at + 1}", self.line, self.text)

    def skip(self) -> None:
        while self.at < len(self.text) and self.text[self.at] in " \t":
            self.at += 1

    def value(self) -> Any:
        self.skip()
        if self.at >= len(self.text):
            return None
        character = self.text[self.at]
        if character == "{":
            return self.mapping()
        if character == "[":
            return self.sequence()
        return self.scalar()

    def mapping(self) -> dict[str, Any]:
        self.at += 1
        result: dict[str, Any] = {}
        self.skip()
        if self.at < len(self.text) and self.text[self.at] == "}":
            self.at += 1
            return result
        while True:
            self.skip()
            key = self.scalar(as_key=True)
            self.skip()
            if self.at >= len(self.text) or self.text[self.at] != ":":
                raise self.error("expected `:` after a mapping key")
            self.at += 1
            result[str(key)] = self.value()
            self.skip()
            if self.at >= len(self.text):
                raise self.error("mapping is not closed")
            if self.text[self.at] == ",":
                self.at += 1
                continue
            if self.text[self.at] == "}":
                self.at += 1
                return result
            raise self.error("expected `,` or `}`")

    def sequence(self) -> list[Any]:
        self.at += 1
        result: list[Any] = []
        self.skip()
        if self.at < len(self.text) and self.text[self.at] == "]":
            self.at += 1
            return result
        while True:
            result.append(self.value())
            self.skip()
            if self.at >= len(self.text):
                raise self.error("sequence is not closed")
            if self.text[self.at] == ",":
                self.at += 1
                continue
            if self.text[self.at] == "]":
                self.at += 1
                return result
            raise self.error("expected `,` or `]`")

    def scalar(self, *, as_key: bool = False) -> Any:
        self.skip()
        if self.at >= len(self.text):
            return None
        character = self.text[self.at]
        if character in "\"'":
            self.at += 1
            start = self.at
            while self.at < len(self.text) and self.text[self.at] != character:
                self.at += 1
            if self.at >= len(self.text):
                raise self.error("quoted scalar is not closed")
            text = self.text[start:self.at]
            self.at += 1
            return text
        stop = " \t,}]" + (":" if as_key else "")
        start = self.at
        while self.at < len(self.text) and self.text[self.at] not in stop:
            self.at += 1
        return _bare(self.text[start:self.at])


def _bare(text: str) -> Any:
    word = text.strip()
    if word in CONSTANTS:
        return CONSTANTS[word]
    try:
        return int(word)
    except ValueError:
        pass
    try:
        return float(word)
    except ValueError:
        return word


def _split_key(text: str, line: int) -> tuple[str, str]:
    quote = ""
    for index, character in enumerate(text):
        if quote:
            if character == quote:
                quote = ""
            continue
        if character in "\"'":
            quote = character
        elif character == ":":
            return text[:index].strip(), text[index + 1:].strip()
    raise YamlLiteError("expected `key: value`", line, text)


def load(text: str) -> dict[str, Any]:
    """Parse the subset above into plain dicts, lists and scalars."""
    document: dict[str, Any] = {}
    section: dict[str, Any] | None = None
    for line, content in _logical_lines(text):
        indent = len(content) - len(content.lstrip(" "))
        stripped = content.strip()
        if indent == 0:
            key, value = _split_key(stripped, line)
            if value:
                document[key] = _Flow(value, line).value()
                section = None
            else:
                section = {}
                document[key] = section
            continue
        if indent != 2:
            raise YamlLiteError(
                f"indent {indent}; this parser takes two levels, at 0 and 2",
                line, content,
            )
        if section is None:
            raise YamlLiteError("an indented line with no section above it", line, content)
        key, value = _split_key(stripped, line)
        section[key] = _Flow(value, line).value() if value else {}
    return document
