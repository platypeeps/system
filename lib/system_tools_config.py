"""Shared config location for the Python tools in this repository.

The Python twin of `lib/config.sh`. Private, per-machine values live in

    ${SYSTEM_TOOLS_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/system}/<tool>/

one subfolder per tool, named after the folder minus `local-`; a `mezmo-*`
folder keeps its full name. The repository keeps only the `.example` files.

Import it by path, since the tools are scripts and not a package:

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
    import system_tools_config
"""

from __future__ import annotations

import os
import re
import socket
from pathlib import Path


def root(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    named = env.get("SYSTEM_TOOLS_CONFIG")
    if named:
        return Path(os.path.expanduser(named))
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".config")
    return Path(os.path.expanduser(base)) / "system"


def shared(name: str, environ: dict[str, str] | None = None) -> str:
    """A name every tool shares, kept once in <config>/shared.env (sd:3990).

    `st_shared` in config.sh. A variable that is set wins, an empty one too;
    else the file; else `""`. A tool's own .env still overrides the file where
    `st_source_env` loads it into the environment.
    """
    env = os.environ if environ is None else environ
    if name in env:
        return env[name]
    return read_env_file(root(env) / "shared.env", env).get(name, "")


def label_prefix(environ: dict[str, str] | None = None) -> str:
    """The launchd label prefix of every job and agent; `st_label_prefix` in config.sh."""
    return shared("SYSTEM_TOOLS_LABEL_PREFIX", environ) or "local.system-tools"


def obsidian_vault(environ: dict[str, str] | None = None) -> Path:
    """The Obsidian vault of every tool that reads one; `st_obsidian_vault` in config.sh."""
    env = os.environ if environ is None else environ
    home = env.get("HOME") or os.path.expanduser("~")
    named = shared("OBSIDIAN_VAULT", env) or "~/Documents/Obsidian Vault"
    return Path(home + named[1:] if named == "~" or named.startswith("~/") else named)


def config_dir(tool: str, environ: dict[str, str] | None = None) -> Path:
    """The tool's config directory. It may not exist."""
    return root(environ) / tool


_HOME = re.compile(r"\$\{HOME\}|\$HOME(?![A-Za-z0-9_])")


def read_env(tool: str, environ: dict[str, str] | None = None) -> dict[str, str]:
    """`KEY=value` lines from <config>/<tool>/.env; `{}` when it is absent.

    Blank lines, comments and an `export ` prefix are allowed; a value is
    read as `.` reads one word (`_shell_word`), so an inline `# comment`
    ends it. `$HOME`, `${HOME}` and a leading `~/` expand
    as they do when `st_source_env` sources the file, except inside single
    quotes; no other variable expands (sd:1868).
    """
    env = os.environ if environ is None else environ
    return read_env_file(config_dir(tool, environ) / ".env", env)


def read_env_file(path: Path, env: dict[str, str]) -> dict[str, str]:
    """`read_env`'s parser for any env file; `{}` when it is absent.
    `env` supplies the HOME the file's `$HOME` and `~/` expand to.

    Raises `EnvFileError` for a line it will not guess at (sd:10423):
    an unquoted `<`, `>`, `|` or `&` on a line that assigns, a quote
    left open, and a value whose shell reading differs from the one
    read before sd:10423 when no shell assigns it: one of several
    unquoted words."""
    home = env.get("HOME") or os.path.expanduser("~")
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    lines = path.read_text(encoding="utf-8").splitlines()
    for number, line in enumerate(lines, 1):
        while line := line.strip():  # one pass per `;` or `&&` statement
            if line.startswith("#"):
                break
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, sep, raw = line.partition("=")
            key = key.strip()
            assigns = bool(sep) and key.isidentifier()
            # Only a name sh assigns at a statement's start is named.
            name = key if assigns and _NAME.fullmatch(key) else ""
            try:
                value, line, several = _shell_word(raw if assigns else line, home)
            except _Operator as e:
                if not sep:
                    break  # the rest of the line assigns nothing
                raise _refused(path, number, name, _OPERATOR % str(e)) from None
            if value is None:
                # The next lines would be read from inside the quote.
                raise _refused(path, number, name, _UNCLOSED)
            if not assigns:
                continue
            if several and value != _legacy_value(raw, home):
                raise _refused(path, number, name, _TWO_WAYS)
            values[key] = value
    return values


class EnvFileError(ValueError):
    """An env file line `.` would read past what this parser models."""


class _Operator(Exception):
    pass


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_OPERATOR = ("an unquoted %r (redirection, pipe, || or background job) "
             "is not read; quote it or drop it")
_UNCLOSED = "an unclosed quote is not read; close it on this line"
_TWO_WAYS = ("an unquoted or partly quoted value reads two ways (a comment, "
             "operator, quote or backslash in it); quote the whole value")


def _refused(path: Path, number: int, name: str, reason: str) -> EnvFileError:
    # Names no value and no text but a variable name sh assigns: an env
    # file may hold a secret.
    return EnvFileError(f"{path}:{number}: {name + ': ' if name else ''}{reason}")


def _legacy_value(text: str, home: str) -> str:
    """The value read_env read before sd:10423: the rest of the line,
    stripped, one pair of matching outer quotes dropped, `$HOME` and a
    leading `~/` expanded outside single quotes."""
    value = text.strip()
    quote = value[0] if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"" else ""
    if quote:
        value = value[1:-1]
    if quote != "'":
        value = _HOME.sub(lambda _: home, value)
        if not quote and (value == "~" or value.startswith("~/")):
            value = home + value[1:]
    return value


_BLANK = " \t"
#: Unquoted operators that end a value; the next statement is read too.
_ENDS = (";", "&&")
#: Unquoted characters read_env_file refuses rather than model: a
#: redirection, a pipe, a skipped `||` branch or a background job.
_REFUSED = "<>|&"


def _operator(text: str, i: int) -> str:
    """The operator at `i`; raises `_Operator` for a refused one."""
    op = next((op for op in _ENDS if text.startswith(op, i)), "")
    if not op and text[i:i + 1] and text[i] in _REFUSED:
        raise _Operator(text[i:i + 2] if text.startswith("||", i) else text[i])
    return op


def _shell_word(text: str, home: str) -> tuple[str | None, str, bool]:
    """The value `.` assigns from `text`, the part after `=`, the text
    of the line's next statement, `""` when none (sd:10423), and
    whether an unquoted inner blank made it several words.

    An unquoted `;` or `&&` ends the value, and so does an unquoted blank
    before one of them, `# comment` or the line end; an unquoted `<`,
    `>`, `|` or `&` raises `_Operator` (refused, not modelled). Any other
    unquoted blank stays, and leading blanks drop, as read_env did
    before: `.` assigns nothing from such a line, nor from one with a
    parenthesis, so only a Python reader sees it and its path may hold a
    space; read_env_file refuses such a value unless it reads as it did
    before sd:10423. Quotes join and are removed, a backslash
    escapes one character (inside double quotes only `$`, `` ` ``, `"`,
    `\\`), and `$HOME`, `${HOME}` and a leading unquoted `~/` expand
    outside single quotes. A None value for an unclosed quote, which `.`
    refuses."""
    if text[:1] in tuple(_BLANK):
        text = text.lstrip(_BLANK)
        if text.startswith("#"):
            return "", "", False
    out: list[str] = []
    several = False
    if text == "~" or text.startswith(("~/", "~ ", "~\t", "~;")):
        out.append(home)
        text = text[1:]
    i, quote = 0, ""
    while i < len(text):
        char = text[i]
        if quote == "'":
            if char == "'":
                quote = ""
            else:
                out.append(char)
        elif char == "\\" and i + 1 < len(text):
            following = text[i + 1]
            if quote and following not in '$`"\\':
                out.append(char)
            else:
                out.append(following)
            i += 1
        elif char == "$" and (match := _HOME.match(text, i)):
            out.append(home)
            i = match.end()
            continue
        elif char == '"':
            quote = "" if quote else '"'
        elif quote:
            out.append(char)
        elif char == "'":
            quote = "'"
        elif op := _operator(text, i):
            return "".join(out), text[i + len(op):], several
        elif char in _BLANK:
            rest = text[i:].lstrip(_BLANK)
            if not rest or rest.startswith("#"):
                break
            if not _operator(rest, 0):
                out.append(char)
                several = True
        else:
            out.append(char)
        i += 1
    return (None if quote else "".join(out)), "", several


def setting(name: str, saved: dict[str, str], environ: dict[str, str] | None = None) -> str | None:
    """`name` as `st_source_env` leaves it: exported, an empty value too, else
    from `saved` (a `read_env` result); None when neither has it (sd:3964)."""
    env = os.environ if environ is None else environ
    return env[name] if name in env else saved.get(name)


def missing(what: str, tool: str, file: str, folder: str | None = None,
            environ: dict[str, str] | None = None) -> str:
    """The standard remedy for a missing value or file, as one message."""
    folder = folder or f"local-{tool}"
    return (f"{what} is not set. Export it, or copy {folder}/{file}.example to "
            f"{config_dir(tool, environ) / file} and fill it in.")


def cron_job_dirs(environ: dict[str, str] | None = None) -> list[Path]:
    """Where `cron-jobs.sh` finds a job file, in its lookup order.

    Each `CRON_JOBS_EXTRA_DIRS` entry (colon-separated) first, then this
    host's `<config>/cron-jobs/jobs/<host>`, then the shared
    `<config>/cron-jobs/jobs`; the first directory holding `<job>.job` defines
    it. `<host>` is `CRON_JOBS_HOST`, else the short host name, lower-cased;
    other hosts' folders are never listed. Both variables come from the
    environment, an empty value too, else from `<config>/cron-jobs/.env`, as
    in the script.
    Change this, its twin in the pack's `lib/sd_db/config.py` and `job_dirs` in
    `local-cron-jobs/cron-jobs.sh` together.
    """
    env = os.environ if environ is None else environ
    saved = read_env("cron-jobs", env)
    extra = setting("CRON_JOBS_EXTRA_DIRS", saved, env) or ""
    host = (setting("CRON_JOBS_HOST", saved, env) or socket.gethostname().split(".")[0]).lower()
    jobs = config_dir("cron-jobs", env) / "jobs"
    return ([Path(os.path.expanduser(d)) for d in extra.split(":") if d]
            + ([jobs / host] if host else []) + [jobs])
