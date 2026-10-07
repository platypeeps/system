"""`sd-db.sh claim`, `unclaim` and `satellite-stale` (sd:2918).

`claim` runs on a satellite and writes over the wire; the hub watches its own
builders. `satellite-stale` reads branches in the hub's checkouts, so its
report and `--notify` run on the hub only. Its `status` answers convention 6:
0 no claim stale, 1 one is, 3 nothing to check (no database, no open claim,
or a satellite, which holds no claims to watch).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .. import satellite_stale as stale
from ..database import connect, default_path, refuse_hub_only, served_by
from ..errors import SdDbError

CLAIM_USAGE = "sd-db claim: usage: claim ITEM [--branch B] [--quiet-until ISO] [--host H]"


def _home() -> Path:
    return Path(os.environ.get("HOME", "~")).expanduser()


def _item(word: str) -> int:
    try:
        return int(word.removeprefix("sd:"))
    except ValueError:
        raise SdDbError(f"{word!r} is not an item number") from None


def _options(argv: list[str], allowed: tuple[str, ...]) -> tuple[list[str], dict[str, str]]:
    plain, options = [], {}
    words = iter(argv)
    for word in words:
        if word in allowed:
            value = next(words, None)
            if value is None:
                raise SdDbError(f"{word} needs a value")
            options[word] = value
        elif word.startswith("--"):
            raise SdDbError(f"unknown option {word}")
        else:
            plain.append(word)
    return plain, options


def _open(write: bool = True):
    path = default_path(_home())
    if not path.exists():
        raise SdDbError(f"no database at {path}; run `sd-db.sh init`")
    return path, connect(path, write=write)


def command_claim(argv: list[str]) -> int:
    plain, options = _options(argv, ("--branch", "--quiet-until", "--host"))
    if len(plain) != 1:
        print(CLAIM_USAGE, file=sys.stderr)
        return 1
    item = _item(plain[0])
    path = default_path(_home())
    if served_by(path, _home()) is None:
        raise SdDbError("claim runs on a satellite; this machine holds the database, and the hub "
                        "lead watches its own sessions")
    host = options.get("--host") or socket.gethostname().split(".")[0].lower()
    _, connection = _open()
    try:
        stale.claim(connection, item, host=host, branch=options.get("--branch"),
                    quiet_until=options.get("--quiet-until"))
    finally:
        connection.close()
    print(f"claimed sd:{item} for {host}")
    return 0


def command_unclaim(argv: list[str]) -> int:
    if len(argv) != 1:
        print("sd-db unclaim: usage: unclaim ITEM", file=sys.stderr)
        return 1
    item = _item(argv[0])
    _, connection = _open()
    try:
        released = stale.unclaim(connection, item)
    finally:
        connection.close()
    print(f"unclaimed sd:{item}" if released else f"sd:{item} held no open claim")
    return 0


def _now(options: dict[str, str]) -> datetime:
    text = options.get("--now")
    return datetime.fromisoformat(text).astimezone() if text else datetime.now().astimezone()


def _line(verdict: stale.Verdict) -> str:
    age = f"{verdict.age_hours:.1f}h" if verdict.age_hours is not None else "-"
    return (f"{verdict.state:<7} sd:{verdict.claim.item}  {verdict.claim.host}  "
            f"{verdict.branch or '-'}  {age} ({verdict.source})  {verdict.title}")


def _status(options: dict[str, str]) -> int:
    home = _home()
    path = default_path(home)
    if served_by(path, home) is not None:
        print("satellite-stale: runs on the hub; a satellite has no claims to watch")
        return 3
    if not path.exists():
        print(f"satellite-stale: no database at {path}")
        return 3
    hours = stale.parse_hours(os.environ.get("SD_SATELLITE_STALE_HOURS"))
    connection = connect(path, write=False)
    try:
        verdicts = stale.assess(connection, at=_now(options), hours=hours,
                                branch_time=stale.branch_time_last_fetched)
    finally:
        connection.close()
    if not verdicts:
        print("satellite-stale: no open claim")
        return 3
    stalled = [v for v in verdicts if v.state == "stale"]
    if not stalled:
        print(f"satellite-stale: {len(verdicts)} claim(s), none stale")
        return 0
    names = ", ".join(f"sd:{v.claim.item} ({v.claim.host})" for v in stalled)
    print(f"satellite-stale: {len(stalled)} stale over {hours:g}h: {names}")
    return 1


def _sender(notifier: str):
    channels = os.environ.get("SD_SATELLITE_STALE_CHANNELS") or "ntfy,email"

    def send(alert: stale.Alert) -> None:
        done = subprocess.run(["sh", notifier, "-t", alert.title, "-k", "status", "-F", "-c", channels, "-b",
                               alert.body], capture_output=True, text=True)
        if done.returncode != 0:
            raise stale.SendFailed(f"alert for sd:{alert.item} not delivered: "
                                   f"{(done.stderr or done.stdout).strip() or f'exit {done.returncode}'}")

    return send


def command_satellite_stale(argv: list[str]) -> int:
    notify = "--notify" in argv
    plain, options = _options([word for word in argv if word != "--notify"], ("--now",))
    if plain == ["status"]:
        return _status(options)
    if plain:
        print("sd-db satellite-stale: usage: satellite-stale [status | --notify] [--now ISO]", file=sys.stderr)
        return 1
    hours = stale.parse_hours(os.environ.get("SD_SATELLITE_STALE_HOURS"))
    window = stale.parse_window(os.environ.get("SD_SATELLITE_STALE_WINDOW"))
    notifier = os.environ.get("SD_NOTIFY")
    refuse_hub_only(default_path(_home()), "satellite-stale", _home())
    if notify and not notifier:
        raise SdDbError("--notify needs SD_NOTIFY, the notifier to run; "
                        "local-satellite-stale/satellite-stale.sh run sets it")
    moment = _now(options)
    _, connection = _open(write=notify)
    try:
        verdicts = stale.assess(connection, at=moment, hours=hours, branch_time=stale.branch_time_on_disk)
        for verdict in verdicts:
            print(_line(verdict))
        if not notify:
            return 0
        if not stale.in_window(moment, window):
            print(f"satellite-stale: outside the alert window {window[0]:02d}-{window[1]:02d}; nothing sent")
            return 0
        sent = stale.alert(connection, verdicts, send=_sender(str(notifier)))
        print(f"satellite-stale: {len(sent)} alert(s) sent")
        return 0
    finally:
        connection.close()
