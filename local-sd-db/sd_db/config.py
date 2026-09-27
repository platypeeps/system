"""Where this machine's private config lives.

The installed twin of the checkout's `lib/system_tools_config.py`: an
installed package cannot import `lib/`, so it carries the same resolution
rule here. Private, per-machine values live in

    ${SYSTEM_TOOLS_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/system}/<tool>/

one subfolder per tool, named after the folder minus `local-`
(`repo-sync`, `cron-jobs`). The repository keeps only the `.example` files.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path


def root(environ: dict[str, str] | None = None) -> Path:
    """The config root. It may not exist."""
    env = os.environ if environ is None else environ
    named = env.get("SYSTEM_TOOLS_CONFIG")
    if named:
        return Path(os.path.expanduser(named))
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".config")
    return Path(os.path.expanduser(base)) / "system"


def config_dir(tool: str, environ: dict[str, str] | None = None) -> Path:
    """The tool's config directory. It may not exist."""
    return root(environ) / tool


def missing(what: str, tool: str, file: str, folder: str | None = None,
            environ: dict[str, str] | None = None) -> str:
    """The standard remedy for a missing value or file, as one message."""
    folder = folder or f"local-{tool}"
    return (f"{what} is not set. Export it, or copy {folder}/{file}.example to "
            f"{config_dir(tool, environ) / file} and fill it in.")


def read_env(tool: str, environ: dict[str, str] | None = None) -> dict[str, str]:
    """`KEY=value` lines from <config>/<tool>/.env; `{}` when it is absent.

    Blank lines, comments and an `export ` prefix are allowed; one layer of
    matching quotes is removed. Nothing is expanded.
    """
    path = config_dir(tool, environ) / ".env"
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
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def cron_job_dirs(environ: dict[str, str] | None = None) -> list[Path]:
    """Where `cron-jobs.sh` finds a job file, in its lookup order.

    Each `CRON_JOBS_EXTRA_DIRS` entry (colon-separated) first, then this
    host's `<config>/cron-jobs/jobs/<host>`, then the shared
    `<config>/cron-jobs/jobs`; the first directory holding `<job>.job` defines
    it. `<host>` is `CRON_JOBS_HOST`, else the short host name, lower-cased;
    other hosts' folders are never listed. Both variables come from the
    environment, else from `<config>/cron-jobs/.env`, as in the script.
    Change this, its twin in the checkout's `lib/system_tools_config.py` and `job_dirs` in
    `local-cron-jobs/cron-jobs.sh` together.
    """
    env = os.environ if environ is None else environ
    saved = read_env("cron-jobs", env)
    extra = env.get("CRON_JOBS_EXTRA_DIRS") or saved.get("CRON_JOBS_EXTRA_DIRS") or ""
    host = (env.get("CRON_JOBS_HOST") or saved.get("CRON_JOBS_HOST")
            or socket.gethostname().split(".")[0]).lower()
    jobs = config_dir("cron-jobs", env) / "jobs"
    return ([Path(os.path.expanduser(d)) for d in extra.split(":") if d]
            + ([jobs / host] if host else []) + [jobs])
