"""Tile actions on their host: this machine answers the hub's requests for its Overview tiles (pack sd:6387).

A tile line in the hub's place-tiles.conf that names this machine as its
`<host>` runs its buttons here (the pack's `dashboard/sd_dashboard/place_tiles.py`).
A button writes one setting row in the hub's database,
`place_tiles.request.<host>.<realm>.<tile>.<action>`: `{id, at, host, realm,
tile, action}`. `repo-sync.sh follow`, the job that already runs every five
minutes here, runs `answer`, as it answers Refresh satellites
(`satellite_refresh.py`, sd:5108). `answer` reads the rows of the tiles and
actions THIS machine's `<config>/project-dashboard/place-tiles.conf` names, and
runs only an act line of that file, through the pack's `place_tiles.run`: the
row names a tile and an action, never a command. It writes
`place_tiles.result.<host>.<realm>.<tile>.<action>` in steps:

1. `starting`, with the attempt, before the start;
2. the start, as the dashboard's own: one lock per action, output in `start.log`;
3. `running` with the start's time, or `refused` when nothing ran (no such
   line here, a folder not here, a run already holding the action's lock);
4. at each later run, a `running` row follows the action's last run here once
   it ends: `done` with its exit and output, or `unknown`.

A row still `starting` at the next run was stopped between steps 1 and 3. A
start here since step 1 is adopted, not run again; none runs once more as
attempt 2, and a second stop is `refused`, until a new request.

A key segment is `[a-z][a-z0-9_]*`: `-` becomes `_`, and a segment that starts
with a digit gets an `x`. Each row also holds its names, and a row that names
another action is refused, never answered. The pack's `place_tiles.key` spells
the same rule; a change to it changes both.

`USAGE` names the verb. It exits 0, 3 when the hub does not answer, 4 when
the hub refuses this build, and 2 on any other failure, named on stderr. The
database is the hub's, through `sd_db.connect`, or `REPO_SYNC_SD_DB` when set.
`sd_db` and `sd_dashboard` come from `$SD_PACK_ROOT`.
"""

import datetime
import json
import os
import pathlib
import re
import socket
import sys

REQUEST, RESULT = "place_tiles.request", "place_tiles.result"
#: Starts per request: a second one finishes what a stopped first one began.
ATTEMPTS = 2
#: The characters of output a result keeps, its end: a setting value holds 16384.
OUTPUT = 2000

FAILED, UNREACHABLE, MISMATCH = 2, 3, 4
USAGE = "usage: satellite_tiles.py answer"


def this_host():
    """This machine's `hostname -s`, lower-cased: the name a tile line's `<host>` uses."""
    return socket.gethostname().split(".")[0].lower()


def _segment(name):
    segment = re.sub(r"[^a-z0-9_]", "_", name.lower())
    return segment if re.match(r"[a-z]", segment) else "x" + segment


def key(kind, host, realm, tile, slug):
    """`<kind>.<host>.<realm>.<tile>.<action>`, as the pack's `place_tiles.key` writes it."""
    return ".".join([kind, *(_segment(name) for name in (host, realm, tile, slug))])


def _when(text):
    try:
        return datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def _row(name, value, names):
    """Row `name`'s object, None when unset; ValueError when it does not read or names another action."""
    if value is None:
        return None
    row = json.loads(value)
    if not isinstance(row, dict):
        raise ValueError(f"{name} holds no object")
    if any(row.get(field) != want for field, want in names.items()):
        raise ValueError(f"{name} holds another action's row: {row.get('tile')} {row.get('action')} on {row.get('host')}")
    return row


class Action:
    """One act line of this machine's file, and its two rows in the hub's database."""

    def __init__(self, connection, tiles, config, host, tile, label):
        self.connection, self.tiles, self.config, self.tile, self.label = connection, tiles, config, tile, label
        self.names = {"host": host, "realm": tile["realm"], "tile": tile["id"], "action": label}
        slug = tiles._slug(label)
        self.asks, self.answers = (key(kind, host, tile["realm"], tile["id"], slug) for kind in (REQUEST, RESULT))
        self.state = tiles._state(tile, label)

    def write(self, request, attempt, state, **fields):
        from sd_db import writes

        if fields.get("output"):
            fields["output"] = fields["output"][-OUTPUT:]
        value = {**self.names, "request": request, "attempt": attempt, "state": state, "written": writes.now(), **fields}
        writes.set_setting(self.connection, self.answers, json.dumps(value, ensure_ascii=False))
        return f"tiles: {self.tile['id']} {self.label}: {state}{': ' + fields['reason'] if fields.get('reason') else ''}"

    def start(self, request, attempt):
        """Steps 1 to 3: `starting`, the start, then `running` or `refused`."""
        from sd_db import writes

        self.write(request, attempt, "starting", started=writes.now())
        try:
            go = self.tiles.run({"realm": self.tile["realm"], "tile": self.tile["id"], "action": self.label},
                                config=self.config)
        except ValueError as error:
            return self.write(request, attempt, "refused", reason=str(error))
        status, body = go(None)
        if status != 202:
            return self.write(request, attempt, "refused", reason=body.get("error") or f"the start answered {status}")
        return self.write(request, attempt, "running", started=body["started"])

    def follow(self, result):
        """Step 4: a `running` row takes the last run here once it ends; it writes nothing while it runs."""
        last = self.tiles._last(self.state)
        if _when(last.get("started")) != _when(result.get("started")):
            return self.write(result.get("request"), result.get("attempt"), "unknown", started=result.get("started"),
                              reason=f"a later run of {self.label} here replaced it, or its log is gone")
        if last["state"] == "running":
            return None
        fields = {k: last[k] for k in ("started", "ended", "exit", "output", "cut", "reason") if k in last}
        state = last["state"] if last["state"] in ("done", "unknown") else "unknown"
        return self.write(result.get("request"), result.get("attempt"), state, **fields)

    def answer(self):
        """The line this run wrote for the action, or None when it had nothing to do."""
        from sd_db import writes

        held = writes.read_settings(self.connection, [self.asks, self.answers])
        asked = _row(self.asks, held.get(self.asks), self.names)
        result = _row(self.answers, held.get(self.answers), self.names) or {}
        if asked is not None and result.get("request") != asked.get("id"):
            return self.start(asked.get("id"), 1)
        if result.get("state") == "running":
            return self.follow(result)
        if result.get("state") != "starting":
            return None
        ran, since = _when(self.tiles._last(self.state).get("started")), _when(result.get("started"))
        if ran and since and ran >= since:
            # Stopped between steps 2 and 3: the start ran, so it is adopted, not run twice.
            return self.follow({**result, "started": self.tiles._last(self.state).get("started")})
        attempt = int(result.get("attempt") or 1)
        if asked is not None and attempt < ATTEMPTS:
            return self.start(asked.get("id"), attempt + 1)
        return self.write(result.get("request"), attempt, "refused", started=result.get("started"),
                          reason=f"attempt {attempt} stopped before it started the command (the job's limit, "
                                 "a boot-out or a reboot); press the button again")


def answer(connection, host, tiles, config, text):
    """`(lines, failures)`: each action's line, and each line the file could not read or action whose rows did not.

    A line parse refuses is a failure, never a silent success: a pack whose parse knows no `<host>` field
    refuses the documented host line, and its tile would otherwise go unanswered with nothing said.
    """
    parsed, problems = tiles.parse(text)
    lines, failures = [], [f"tiles: {pathlib.Path(config).name}: {problem}" for problem in problems]
    for tile in parsed:
        if tile.get("host") and tile["host"] != host:
            continue
        for act in tile["actions"]:
            action = Action(connection, tiles, config, host, tile, act["label"])
            try:
                line = action.answer()
            except Exception as error:  # noqa: BLE001 - name it and go on to the next action
                if type(error).__name__ in ("HubUnreachable", "BuildMismatch"):
                    raise
                failures.append(f"tiles: {tile['id']} {act['label']}: {type(error).__name__}: {error}")
                continue
            if line:
                lines.append(line)
    return lines, failures


def _exit_for(error):
    name = type(error).__name__
    if name == "HubUnreachable":
        return UNREACHABLE
    if name == "BuildMismatch":
        return MISMATCH
    return FAILED


def main(argv):
    if argv[1:] != ["answer"]:
        print(USAGE, file=sys.stderr)
        return FAILED
    pack = pathlib.Path(os.environ.get("SD_PACK_ROOT") or pathlib.Path.home() / "repos/platypeeps/sd-ai-command-pack")
    sys.path.insert(0, str(pack / "lib"))
    sys.path.insert(0, str(pack / "dashboard"))
    try:
        import sd_db
        from sd_dashboard import place_tiles

        try:
            text = place_tiles.CONFIG.read_text(encoding="utf-8")
        except FileNotFoundError:
            # No tile here: nothing to answer, and the hub is not asked.
            return 0
        connection = sd_db.connect(os.environ.get("REPO_SYNC_SD_DB") or None, write=True)
        try:
            lines, failures = answer(connection, this_host(), place_tiles, place_tiles.CONFIG, text)
        finally:
            connection.close()
    except Exception as error:  # noqa: BLE001 - every failure is named and is unknown, never "nothing asked"
        print(f"satellite_tiles.py answer: {type(error).__name__}: {error}", file=sys.stderr)
        return _exit_for(error)
    for line in lines:
        print(line)
    for line in failures:
        print(line, file=sys.stderr)
    return FAILED if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
