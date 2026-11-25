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
    `env` supplies the HOME the file's `$HOME` and `~/` expand to."""
    home = env.get("HOME") or os.path.expanduser("~")
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep or not key.strip().isidentifier():
            continue
        value = _shell_word(value, home)
        if value is not None:
            values[key.strip()] = value
    return values


def _shell_word(text: str, home: str) -> str | None:
    """The value `.` assigns from `text`, the part after `=` (sd:10423).

    The value ends at the first unquoted blank, so an inline `# comment`
    is dropped; quotes join and are removed, a backslash escapes one
    character (inside double quotes only `$`, `` ` ``, `"`, `\\`), and
    `$HOME`, `${HOME}` and a leading unquoted `~/` expand outside single
    quotes. None for an unclosed quote, which `.` refuses."""
    out: list[str] = []
    if text == "~" or text.startswith(("~/", "~ ", "~\t")):
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
        elif char == "'" and not quote:
            quote = "'"
        elif char in " \t" and not quote:
            break
        else:
            out.append(char)
        i += 1
    return None if quote else "".join(out)


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
