"""The verbs behind herdr.sh that read or write JSON.

Everything herdr answers is JSON, and so is the state file, which is why this
half is Python and not sh. herdr.sh dispatches here; nothing calls this file
directly. Stdlib only, so the interpreter is whatever ``python3`` is -- the
same dependency herdr's own Claude Code integration hook already has.

The one seam for tests is PATH: every herdr call goes through ``herdr_cmd``,
which runs the first ``herdr`` on PATH with ``HERDR_SESSION`` exported. The
suite puts a shell double there and reads what it journals.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

DEFAULT_SESSION = "sd"

# herdr 0.9.0 documents the resume form of these two ("Native agent session
# restore": claude --resume <id>, codex resume <id>). `herdr agent start`
# takes the agent's own arguments after `--`, so these are those arguments.
RESUME_ARGS = {
    "claude": lambda sid: ["--resume", sid],
    "codex": lambda sid: ["resume", sid],
}

# `herdr agent start` requires a name matching this, unique among live agents.
AGENT_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")


class Fail(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


# --- environment --------------------------------------------------------------


def session_name() -> str:
    return os.environ.get("HERDR_SESSION") or DEFAULT_SESSION


def state_dir() -> str:
    explicit = os.environ.get("HERDR_STATE_DIR")
    if explicit:
        return explicit
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return os.path.join(xdg, "sd-herdr")


def state_path(session: str) -> str:
    return os.path.join(state_dir(), f"{session}.json")


def now_iso() -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# --- state file ---------------------------------------------------------------


def load_state(session: str) -> dict | None:
    path = state_path(session)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or not isinstance(data.get("panes"), list):
        raise Fail(f"{path}: not a state file (expected an object with a panes list)")
    return data


@contextlib.contextmanager
def state_lock(session: str):
    """Hold the session's state lock across one read-modify-write.

    Every pane's startup hook may call `record` at once, and `snapshot`,
    `forget` and `resume` rewrite the same file; without the lock the last
    writer drops the others' entries. Held only around file work, never
    while herdr is asked anything, so a hook that `agent start` sets off
    can record while `resume` is still running.
    """
    path = state_path(session)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def save_state(session: str, data: dict) -> str:
    """Write the state file atomically; the caller holds ``state_lock``."""
    path = state_path(session)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=os.path.basename(path) + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    return path


def upsert_pane(data: dict, entry: dict) -> None:
    panes = data["panes"]
    for i, existing in enumerate(panes):
        if existing.get("pane") == entry["pane"]:
            panes[i] = entry
            return
    panes.append(entry)


def make_entry(pane: str, agent: str, session_id: str, cwd: str) -> dict:
    if agent not in RESUME_ARGS:
        raise Fail(
            f"agent kind {agent!r}: no known resume form; "
            f"this wrapper knows {', '.join(sorted(RESUME_ARGS))}",
            2,
        )
    if not pane or not session_id:
        raise Fail("pane and session id must not be empty", 2)
    return {
        "pane": pane,
        "agent": agent,
        "session_id": session_id,
        "cwd": cwd,
        "recorded_at": now_iso(),
        "name": agent_name_for(pane),
    }


def agent_name_for(pane: str) -> str:
    # w4:p4 -> sd-w4-p4. herdr's name grammar is lowercase and 32 long, so
    # an id that cannot be spelled back from the short form (upper case, a
    # character other than [a-z0-9_:], or too long) keeps a readable prefix
    # and ends in a hash of the whole id: w4:pG and w4:pg stay distinct.
    # The pane id itself stays as recorded.
    folded = "sd-" + re.sub(r"[^a-z0-9_-]", "-", pane.lower())
    if not (re.fullmatch(r"[a-z0-9_:]+", pane) and len(folded) <= 32):
        digest = hashlib.sha256(pane.encode("utf-8")).hexdigest()[:8]
        folded = folded[:23].rstrip("-") + "-" + digest
    if not AGENT_NAME_RE.fullmatch(folded):
        raise Fail(f"pane id {pane!r} yields no usable agent name", 2)
    return folded


# --- herdr --------------------------------------------------------------------


def herdr_installed() -> bool:
    return shutil.which("herdr") is not None


# The socket of the session being targeted, once `session list` has named
# it. Exported beside HERDR_SESSION because a pane inside another session
# inherits that session's HERDR_SOCKET_PATH, and the docs call the socket
# path the low-level override -- so it is set to the target's, not left.
_SOCKET: dict[str, str] = {}


def herdr_env(session: str) -> dict:
    env = dict(os.environ, HERDR_SESSION=session)
    env.pop("HERDR_SOCKET_PATH", None)
    if session in _SOCKET:
        env["HERDR_SOCKET_PATH"] = _SOCKET[session]
    return env


def herdr_cmd(session: str, *args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["herdr", *args],
        env=herdr_env(session),
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def herdr_json(session: str, *args: str) -> dict:
    """Run a control command and return its parsed JSON.

    herdr prints results as ``{"result": ...}`` on stdout and errors as
    ``{"error": {...}}`` -- on stderr with exit 1 by its documentation, on
    stdout in one observed case -- so both streams are tried.
    """
    rc, out, err = herdr_cmd(session, *args)
    for stream in (out, err):
        stream = stream.strip()
        if not stream:
            continue
        try:
            parsed = json.loads(stream)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise Fail(f"herdr {' '.join(args)}: exit {rc}, no JSON answer: {err.strip() or out.strip()}")


def session_info(session: str) -> dict | None:
    rc, out, err = herdr_cmd(session, "session", "list", "--json")
    if rc != 0:
        raise Fail(f"herdr session list --json: exit {rc}: {err.strip()}")
    try:
        sessions = json.loads(out).get("sessions", [])
    except (json.JSONDecodeError, AttributeError):
        raise Fail(f"herdr session list --json: not JSON: {out.strip()!r}")
    for entry in sessions:
        if entry.get("name") == session:
            if entry.get("running") and entry.get("socket_path"):
                _SOCKET[session] = entry["socket_path"]
            return entry
    return None


def session_running(session: str) -> bool:
    info = session_info(session)
    return bool(info and info.get("running"))


def pane_get(session: str, pane: str) -> dict | None:
    """The pane's info, or None when herdr says pane_not_found."""
    answer = herdr_json(session, "pane", "get", pane)
    error = answer.get("error")
    if error:
        if error.get("code") == "pane_not_found":
            return None
        raise Fail(f"herdr pane get {pane}: {error.get('code')}: {error.get('message')}")
    return answer.get("result", {}).get("pane") or {}


def foreground_other_than_shell(session: str, pane: str) -> str | None:
    """What holds the pane's foreground when it is not the shell at its
    prompt, or None when the shell is. No `agent` in `pane get` only means
    herdr recognises no agent; an editor or a server has none either, and
    `agent start` needs the shell itself in the foreground."""
    answer = herdr_json(session, "pane", "process-info", "--pane", pane)
    error = answer.get("error")
    if error:
        raise Fail(f"herdr pane process-info {pane}: {error.get('code')}: {error.get('message')}")
    info = answer.get("result", {}).get("process_info") or {}
    shell = info.get("shell_pid")
    group = info.get("foreground_process_group_id")
    if shell is not None and group == shell:
        return None
    names = [p.get("name") or str(p.get("pid")) for p in info.get("foreground_processes") or []]
    return " ".join(n for n in names if n) or "an unknown foreground process"


def live_session_id(pane_info: dict) -> str | None:
    ref = pane_info.get("agent_session")
    if isinstance(ref, dict) and ref.get("kind") == "id":
        return ref.get("value")
    return None


# --- verbs --------------------------------------------------------------------


def verb_record(session: str, argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        raise Fail("usage: herdr.sh record <pane> <claude|codex> <session-id> [cwd]", 2)
    pane, agent, session_id = argv[:3]
    cwd = os.path.abspath(argv[3]) if len(argv) == 4 else os.getcwd()
    entry = make_entry(pane, agent, session_id, cwd)
    with state_lock(session):
        data = load_state(session) or {"session": session, "panes": []}
        upsert_pane(data, entry)
        path = save_state(session, data)
    print(f"recorded {pane}: {agent} {session_id} in {cwd} -> {path}")
    return 0


def verb_forget(session: str, argv: list[str]) -> int:
    if len(argv) != 1:
        raise Fail("usage: herdr.sh forget <pane>", 2)
    with state_lock(session):
        data = load_state(session)
        if data is None:
            raise Fail(f"no state file for session {session}: {state_path(session)}", 3)
        before = len(data["panes"])
        data["panes"] = [p for p in data["panes"] if p.get("pane") != argv[0]]
        if len(data["panes"]) == before:
            raise Fail(f"{argv[0]}: not in the state file")
        save_state(session, data)
    print(f"forgot {argv[0]}")
    return 0


def verb_list(session: str, argv: list[str]) -> int:
    data = load_state(session)
    path = state_path(session)
    if data is None:
        print(f"no state file for session {session}: {path}")
        return 0
    print(f"session {session}: {path}")
    for p in data["panes"]:
        print(
            f"{p.get('pane', '?'):10} {p.get('agent', '?'):7} {p.get('session_id', '?'):38} "
            f"{p.get('recorded_at', '?'):20} {p.get('cwd', '?')}"
        )
    return 0


def verb_snapshot(session: str, argv: list[str]) -> int:
    if not herdr_installed():
        raise Fail("herdr is not installed", 3)
    if not session_running(session):
        raise Fail(f"session {session} is not running; nothing to snapshot")
    answer = herdr_json(session, "agent", "list")
    if answer.get("error"):
        raise Fail(f"herdr agent list: {answer['error']}")
    agents = answer.get("result", {}).get("agents", [])
    entries = []
    for a in agents:
        kind = a.get("agent")
        sid = live_session_id(a)
        pane = a.get("pane_id")
        if not (kind in RESUME_ARGS and sid and pane):
            continue
        entries.append(make_entry(pane, kind, sid, a.get("cwd") or os.getcwd()))
    with state_lock(session):
        data = load_state(session) or {"session": session, "panes": []}
        for entry in entries:
            upsert_pane(data, entry)
        path = save_state(session, data)
    print(f"snapshot: {len(entries)} agent(s) with a session id recorded -> {path}")
    return 0


def resume_pane(session: str, entry: dict) -> tuple[str, dict]:
    """Bring one recorded pane back. Returns (outcome, possibly-updated entry).

    Outcomes: live (already holds the session), resumed, busy (pane holds
    something else; left alone), failed.
    """
    pane = entry["pane"]
    kind = entry["agent"]
    sid = entry["session_id"]
    cwd = entry.get("cwd") or os.getcwd()
    info = pane_get(session, pane)
    if info is None:
        created = herdr_json(session, "workspace", "create", "--cwd", cwd, "--no-focus")
        if created.get("error"):
            print(f"{pane}: gone, and workspace create failed: {created['error']}", file=sys.stderr)
            return "failed", entry
        new_pane = created.get("result", {}).get("root_pane", {}).get("pane_id")
        if not new_pane:
            print(f"{pane}: gone, and workspace create answered no root_pane", file=sys.stderr)
            return "failed", entry
        entry = dict(entry, pane=new_pane, name=agent_name_for(new_pane))
        pane = new_pane
    else:
        if live_session_id(info) == sid:
            return "live", entry
        if info.get("agent"):
            print(
                f"{pane}: holds {info.get('agent')} {live_session_id(info) or '(no session id)'}, "
                f"not {kind} {sid}; left alone",
                file=sys.stderr,
            )
            return "busy", entry
        try:
            other = foreground_other_than_shell(session, pane)
        except Fail as exc:
            print(f"{pane}: {exc}", file=sys.stderr)
            return "failed", entry
        if other:
            print(f"{pane}: runs {other}, not a shell at its prompt; left alone", file=sys.stderr)
            return "busy", entry
        # `cwd` is where herdr opened the pane; the shell may have moved.
        here = info.get("foreground_cwd") or info.get("cwd")
        if here and here != cwd:
            ran = herdr_json(session, "pane", "run", pane, "cd " + shell_quote(cwd))
            if ran.get("error"):
                print(f"{pane}: cd {cwd} failed: {ran['error']}", file=sys.stderr)
                return "failed", entry
    name = entry.get("name") or agent_name_for(pane)
    started = herdr_json(
        session,
        "agent", "start", name, "--kind", kind, "--pane", pane, "--", *RESUME_ARGS[kind](sid),
    )
    if started.get("error"):
        print(f"{pane}: agent start failed: {started['error']}", file=sys.stderr)
        return "failed", entry
    return "resumed", entry


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def verb_resume(session: str, argv: list[str]) -> int:
    if not herdr_installed():
        raise Fail("herdr is not installed", 3)
    data = load_state(session)
    if data is None:
        print(f"no state file for session {session}; nothing to resume")
        return 0
    if not session_running(session):
        raise Fail(f"session {session} is not running; run herdr.sh start first")
    failed = 0
    moved = []
    for entry in data["panes"]:
        outcome, updated = resume_pane(session, entry)
        if updated is not entry:
            moved.append((entry, updated))
        print(f"{updated['pane']}: {outcome} ({updated['agent']} {updated['session_id']})")
        if outcome == "failed":
            failed += 1
    if moved:
        # Re-read under the lock: records written while herdr worked (a
        # resumed agent's own startup hook) stay. Only an entry still as
        # this run read it moves to its new pane.
        with state_lock(session):
            current = load_state(session) or {"session": session, "panes": []}
            for old, new in moved:
                for i, p in enumerate(current["panes"]):
                    if p == old:
                        current["panes"][i] = new
            save_state(session, current)
    return 1 if failed else 0


def verb_start(session: str, argv: list[str]) -> int:
    if not herdr_installed():
        raise Fail("herdr is not installed", 3)
    if session_running(session):
        rc = verb_resume(session, [])
        if rc:
            print("some panes did not resume; attaching anyway", file=sys.stderr)
    elif load_state(session) is not None:
        print(
            f"session {session} is not running: attaching starts it, and herdr restores "
            f"its own agent sessions as the client attaches; run `herdr.sh resume` from a "
            f"pane afterwards for anything it missed",
            file=sys.stderr,
        )
    # Through the target's socket when `session list` named one, as every
    # other call does: an inherited HERDR_SOCKET_PATH belongs to another session.
    env = herdr_env(session)
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvpe("herdr", ["herdr", "--session", session], env)
    return 1  # not reached


def verb_status(session: str, argv: list[str]) -> int:
    if not herdr_installed():
        print("herdr is not installed; nothing to check")
        return 3
    data = load_state(session)
    if data is None:
        print(f"no state file for session {session}; nothing to check")
        return 3
    if not session_running(session):
        print(f"session {session} is not running but {len(data['panes'])} pane(s) are recorded; run herdr.sh start")
        return 1
    broken = []
    for entry in data["panes"]:
        info = pane_get(session, entry["pane"])
        if info is None:
            broken.append(f"{entry['pane']}: gone")
        elif live_session_id(info) != entry["session_id"]:
            broken.append(
                f"{entry['pane']}: holds {info.get('agent') or 'no agent'} "
                f"{live_session_id(info) or ''}, recorded {entry['agent']} {entry['session_id']}".rstrip()
            )
    if broken:
        print(f"{len(broken)} of {len(data['panes'])} recorded pane(s) not as recorded; run herdr.sh resume")
        for line in broken:
            print("  " + line)
        return 1
    print(f"session {session} running, {len(data['panes'])} recorded pane(s) live")
    return 0


VERBS = {
    "start": verb_start,
    "resume": verb_resume,
    "snapshot": verb_snapshot,
    "record": verb_record,
    "list": verb_list,
    "forget": verb_forget,
    "status": verb_status,
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in VERBS:
        print("herdr_wrap.py: verb required: " + ", ".join(VERBS), file=sys.stderr)
        return 2
    try:
        return VERBS[argv[0]](session_name(), argv[1:])
    except Fail as exc:
        print(f"herdr.sh {argv[0]}: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
