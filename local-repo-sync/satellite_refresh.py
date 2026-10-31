"""Refresh satellites: the request row the dashboard writes and the result each satellite writes back (sd:5108).

The dashboard's Refresh satellites control writes one `setting` row,
`refresh.request`, in the hub's workflow database: `{"id", "at"}`. Each
satellite's `repo-sync.sh follow`, the job that already runs every five
minutes there, asks `check`. A request whose id differs from the one its own
row names is pending: follow drains the lanes, follows the hub's pins, then
runs `machine-setup.sh update --apply`, and writes `start` before that and
`finish` after it into `refresh.result.<host>`. A remote session reaches the
hub over the tailnet (`sd_db.remote`); nothing here runs SSH.

One request serves every satellite: the hub holds no list of them, so it
could not address one row per host before a satellite answered. The
dashboard lists the hosts whose follow job reported a run: cron-jobs records
each job's newest run per host as the heartbeat `cron-chain:<job>@<host>`
(the pack's `reporting.CHAIN_KEY`), a clean run too.

A result row says `running` from `start` until `finish`. A row still
`running` for the request at the next check is a run that was stopped (the
job's limit, a boot-out, a reboot) or whose `finish` was lost: it runs once
more as attempt 2. A run still `running` at attempt 2 is written as failed
and not run again, until a new request.

The hub's refresh asks `request` when the pin it publishes moved across a
change to local-machine-setup/ (sd:6949); it prints the new id.

`USAGE` names the verbs. `check` prints `pending <request> <attempt>` and exits 0, or exits 1 when
nothing is pending. Every verb exits 3 when the hub does not answer, 4 when
the hub refuses this build (follow moves the pack to the hub's), and 2 on any
other failure, named on stderr: a failed read is unknown, never "nothing
pending". The database is the hub's, through `sd_db.connect`, or
`REPO_SYNC_SD_DB` when set. `sd_db` comes from `$SD_PACK_ROOT/lib`.
"""

import json
import os
import pathlib
import re
import secrets
import socket
import sys

REQUEST = "refresh.request"
RESULT = "refresh.result."
#: The cron job that runs `repo-sync.sh follow` on each satellite (local-cron-jobs/examples/repo-sync-follow.job).
JOB = "repo-sync-follow"
#: Runs per request: a second one finishes what a stopped first one began.
ATTEMPTS = 2
#: The longest last line a result keeps.
LINE = 300

PENDING, NONE, FAILED, UNREACHABLE, MISMATCH = 0, 1, 2, 3, 4
USAGE = """usage: satellite_refresh.py check
       satellite_refresh.py request
       satellite_refresh.py start <request> <attempt>
       satellite_refresh.py finish <request> <attempt> <exit> <step> <line>"""


def this_host():
    """This machine's `hostname -s`, lower-cased: the per-host name the cron jobs use."""
    return socket.gethostname().split(".")[0].lower()


def host_key(host):
    """The setting key for `host`'s result: a key segment is `[a-z][a-z0-9_]*`, and a host may hold `-`."""
    segment = re.sub(r"[^a-z0-9_]", "_", host.lower())
    return RESULT + (segment if re.match(r"[a-z]", segment) else "h" + segment)


def _parse(key, value):
    """A row's JSON object, or None for no row; a row that does not read raises ValueError."""
    if value is None:
        return None
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise ValueError(f"the {key} row holds no object")
    return loaded


def _owned(key, value, host):
    """`host`'s result row as an object ({} for none); a row another host wrote raises ValueError.

    Two hosts whose names map to one key (`host_key`): neither may answer or show for the other.
    """
    result = _parse(key, value) or {}
    if result.get("host", host) != host:
        raise ValueError(f"{key} holds {result.get('host')}'s result, not {host}'s")
    return result


def _read(connection, keys):
    from sd_db import writes

    held = writes.read_settings(connection, keys)
    return {key: held.get(key) for key in keys}


def request(connection):
    """The dashboard's write: a new request id, which every satellite answers once."""
    from sd_db import writes

    value = {"id": secrets.token_hex(8), "at": writes.now()}
    writes.set_setting(connection, REQUEST, json.dumps(value))
    return value


def _followed(connection):
    """`{host: end of its newest follow run}` for each host but this one whose `JOB` reported a run."""
    from sd_db import reporting

    prefix = f"{reporting.CHAIN_KEY}{JOB}@"
    ended = {}
    for key, body in connection.execute("SELECT key, body FROM state WHERE kind='heartbeat'"
                                        " AND substr(key, 1, ?) = ? ORDER BY id", (len(prefix), prefix)):
        try:
            ended[key[len(prefix):]] = json.loads(body).get("ended")
        except (TypeError, ValueError, AttributeError):
            ended[key[len(prefix):]] = None
    # The reader is the hub: a follow run there answers no request.
    ended.pop(this_host(), None)
    return ended


def document(connection):
    """The request, and each satellite with its last follow run, its last result and its state for that request.

    The satellites are the hosts whose `JOB` reported a run (`_followed`).
    `state` is `never` (no result yet), `pending` (its result answers an
    older request), `running` or `done`; a row that does not read is
    `unread` with its reason. A failed read of the request raises.
    """
    wanted = _parse(REQUEST, _read(connection, [REQUEST])[REQUEST])
    followed = _followed(connection)
    hosts = sorted(followed)
    rows = []
    results = _read(connection, [host_key(host) for host in hosts]) if hosts else {}
    for host in hosts:
        key = host_key(host)
        try:
            result = _owned(key, results[key], host) or None
        except ValueError as error:
            rows.append({"host": host, "state": "unread", "reason": str(error), "result": None,
                         "followed": followed[host]})
            continue
        if result is None:
            state = "never"
        elif wanted is not None and result.get("request") != wanted.get("id"):
            state = "pending"
        else:
            state = result.get("state") if result.get("state") in ("running", "done") else "unread"
        rows.append({"host": host, "state": state, "reason": "", "result": result, "followed": followed[host]})
    return {"request": wanted, "satellites": rows}


def check(connection, host):
    """`(PENDING, request, attempt)` or `(NONE, None, 0)`."""
    from sd_db import database

    key = host_key(host)
    with database.transaction(connection):
        held = _read(connection, [REQUEST, key])
        wanted = _parse(REQUEST, held[REQUEST])
        result = _owned(key, held[key], host)
        if wanted is None or result.get("request") == wanted["id"] and result.get("state") == "done":
            return NONE, None, 0
        if result.get("request") != wanted["id"]:
            return PENDING, wanted["id"], 1
        attempt = int(result.get("attempt") or 1)
        if attempt < ATTEMPTS:
            return PENDING, wanted["id"], attempt + 1
        # The last attempt was stopped too: written as failed, run no more.
        _write(connection, host, wanted["id"], attempt, "done", started=result.get("started"),
               code=1, step="interrupted",
               line=f"attempt {attempt} stopped before it finished (the job's limit, a boot-out or a reboot); "
                    "run `repo-sync.sh follow` and `machine-setup.sh update --apply` by hand, or ask again")
        return NONE, None, 0


def _write(connection, host, request_id, attempt, state, *, started, code=None, step="", line=""):
    from sd_db import writes

    value = {"host": host, "request": request_id, "attempt": attempt, "state": state, "started": started,
             "finished": writes.now() if state == "done" else None, "exit": code, "step": step,
             "line": line[:LINE]}
    writes.set_setting(connection, host_key(host), json.dumps(value))


def start(connection, host, request_id, attempt):
    from sd_db import database, writes

    with database.transaction(connection):
        _owned(host_key(host), _read(connection, [host_key(host)])[host_key(host)], host)
        _write(connection, host, request_id, attempt, "running", started=writes.now())


def finish(connection, host, request_id, attempt, code, step, line):
    from sd_db import database

    with database.transaction(connection):
        result = _owned(host_key(host), _read(connection, [host_key(host)])[host_key(host)], host)
        started = result.get("started") if result.get("request") == request_id else None
        _write(connection, host, request_id, attempt, "done", started=started, code=code, step=step, line=line)


def _exit_for(error):
    name = type(error).__name__
    if name == "HubUnreachable":
        return UNREACHABLE
    if name == "BuildMismatch":
        return MISMATCH
    return FAILED


def main(argv):
    verbs = {"check": 0, "request": 0, "start": 2, "finish": 5}
    if len(argv) < 2 or argv[1] not in verbs or len(argv) - 2 != verbs[argv[1]]:
        print(USAGE, file=sys.stderr)
        return FAILED
    verb, args = argv[1], argv[2:]
    pack = pathlib.Path(os.environ.get("SD_PACK_ROOT") or pathlib.Path.home() / "repos/platypeeps/sd-ai-command-pack")
    sys.path.insert(0, str(pack / "lib"))
    try:
        import sd_db

        connection = sd_db.connect(os.environ.get("REPO_SYNC_SD_DB") or None, write=True)
        try:
            host = this_host()
            if verb == "check":
                state, request_id, attempt = check(connection, host)
                if state == PENDING:
                    print(f"pending {request_id} {attempt}")
                return state
            if verb == "request":
                print(request(connection)["id"])
                return 0
            if verb == "start":
                start(connection, host, args[0], int(args[1]))
            else:
                finish(connection, host, args[0], int(args[1]), int(args[2]), args[3], args[4])
            return 0
        finally:
            connection.close()
    except Exception as error:  # noqa: BLE001 - every failure is named and is unknown, never "nothing pending"
        print(f"satellite_refresh.py {verb}: {type(error).__name__}: {error}", file=sys.stderr)
        return _exit_for(error)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
