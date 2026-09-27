"""Native headless modes for authoring and read-only structured skill review."""

from __future__ import annotations

import json
from pathlib import Path

from sd_db.runner import RunnerRefused

SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["version", "item", "source_sha256", "proposals"],
          "properties": {"version": {"type": "integer", "const": 1}, "item": {"type": "integer"},
                         "source_sha256": {"type": "string"}, "proposals": {"type": "array", "maxItems": 40,
                         "items": {"type": "object", "additionalProperties": False,
                                   "required": ["path", "line_start", "line_end", "body"],
                                   "properties": {"path": {"type": "string"}, "line_start": {"type": "integer", "minimum": 1},
                                                  "line_end": {"type": "integer", "minimum": 1}, "body": {"type": "string", "minLength": 1}}}}}}


def argv(command: list[str], *, reviewer: bool, clone: Path) -> list[str]:
    executable = Path(command[0]).name
    if executable == "claude":
        common = ["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
                  "--disable-slash-commands", "--no-session-persistence", "--permission-prompts", "none"]
        if reviewer:
            return command + common + ["--restricted", "--tools", "Read,Grep,Glob", "--allowedTools", "Read,Grep,Glob",
                                       "--permission-mode", "dontAsk", "--output-format", "json", "--json-schema", json.dumps(SCHEMA)]
        # `--output-format json` makes the last line of the retained log the
        # result envelope: `usage` and `total_cost_usd`, which is the total
        # the session reports at its exit and the one `run` cost row's source.
        return command + common + ["--permission-mode", "acceptEdits", "--tools", "Read,Grep,Glob,Edit,Write,Bash",
                                   "--allowedTools", "Read,Grep,Glob,Edit,Write,Bash", "--output-format", "json"]
    if executable == "codex":
        common = ["--ignore-user-config", "--ignore-rules", "--ephemeral"]
        common += ["--sandbox", "read-only"] if reviewer else ["--approve-for-me"]
        if reviewer:
            common += ["--output-schema", str(clone / ".git/sd-review-schema.json"),
                       "--output-last-message", str(clone / ".git/sd-skill-review.json")]
        return command + common + ["-"]
    raise RunnerRefused(f"no verified headless session protocol for {executable}; configure Claude or Codex")


def finish_review(clone: Path, executable: str, log: Path) -> None:
    if executable == "claude":
        if log.stat().st_size > 1024 * 1024:
            raise RunnerRefused("Claude review envelope exceeds 1 MiB")
        envelope = json.loads(log.read_text())
        if envelope.get("type") != "result" or envelope.get("subtype") != "success" or envelope.get("is_error"):
            raise RunnerRefused("Claude skill review did not report successful structured output")
        value = envelope.get("structured_output")
        if not isinstance(value, dict):
            raise RunnerRefused("Claude skill review omitted structured_output")
        (clone / ".git/sd-skill-review.json").write_text(json.dumps(value))
