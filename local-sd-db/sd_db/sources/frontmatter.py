"""The frontmatter block the migrations read, and nothing more.

Two of the five sources are markdown with a YAML block at the top: a
`docs/work/*/prd.md` and a vault note. `yaml_lite` next door parses the
provider registry, whose shape is a top-level block mapping of *sections*;
frontmatter is flat keys, some of which carry a block sequence. Rather than
widen a parser whose refusals are its value, this reads the second shape.

What it accepts:

    ---
    title: one database holds the state
    status: planning
    description: "a colon: inside quotes"
    tags:
      - blog-idea
      - claude
    my-rating:
    ---

Scalars are bare text to end of line, or a quoted string. A key with nothing
after the colon is `None`, which is a real state in the vault: `my-rating:` is
the operator's field, left empty until they fill it. A block sequence is
`  - item` lines under a key. Nothing nests further, and a document that
needs it is a document this has outgrown -- said out loud rather than guessed
at, the same way `yaml_lite` refuses.
"""

from __future__ import annotations

from typing import Any

FENCE = "---"


class FrontmatterError(ValueError):
    """A frontmatter block this will not guess at. Carries the line number."""

    def __init__(self, message: str, line: int) -> None:
        self.line = line
        super().__init__(f"line {line}: {message}")


def _scalar(text: str) -> Any:
    value = text.strip()
    if not value:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def split(text: str) -> tuple[str, str]:
    """`(frontmatter, body)`. A file with no block yields `("", text)`.

    The body is returned rather than dropped because the vault import stores
    it: an idea's sections are its content, and a row holding the fields
    without them would be a row nobody could read the idea from.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != FENCE:
        return "", text
    for index in range(1, len(lines)):
        if lines[index].strip() == FENCE:
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1:])
    raise FrontmatterError("the frontmatter block is never closed", len(lines))


def load(text: str) -> dict[str, Any]:
    """Parse a frontmatter block. Takes the block, not the whole file."""
    found: dict[str, Any] = {}
    key: str | None = None
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        if "\t" in raw:
            raise FrontmatterError("a tab is a silent misparse in every YAML reader", number)
        if raw.startswith((" ", "-")) and raw.strip().startswith("- "):
            if key is None:
                raise FrontmatterError("a sequence item before any key", number)
            if not isinstance(found.get(key), list):
                found[key] = []
            found[key].append(_scalar(raw.strip()[2:]))
            continue
        if raw.startswith(" "):
            raise FrontmatterError(f"{raw.strip()!r} is indented but is not a `- item`", number)
        if ":" not in raw:
            raise FrontmatterError(f"{raw.strip()!r} is neither a key nor a `- item`", number)
        name, _, rest = raw.partition(":")
        key = name.strip()
        found[key] = _scalar(rest)
    return found


def read(text: str) -> tuple[dict[str, Any], str]:
    """`(frontmatter, body)` for a whole file, in one call."""
    block, body = split(text)
    return (load(block) if block else {}), body
