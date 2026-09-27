# local-herdr

One named persistent [herdr](https://herdr.dev) session, and the agents in it
brought back after a restart: on start,
each pane resumes the Claude Code or Codex session it held, read from a small
state file this wrapper keeps.

## What herdr 0.9.0 already does, and what that leaves for the wrapper

Read from the installed binary (`herdr 0.9.0`, `/opt/homebrew/bin/herdr`) on
2026-09-12, and from the docs it points at:

- **Named persistent sessions**: `herdr --session <name>` launches or attaches
  one; `herdr session list|attach|stop|delete` manage them; `HERDR_SESSION`
  "selects a named session for CLI commands", so every control command below
  can be pointed at the session by exporting it.
- **It knows each pane's agent session id.** The installed integrations
  (`herdr integration status`: claude v9, codex v8, and others) run as agent
  hooks and call `herdr pane report-agent-session --agent-session-id <id>`.
  `herdr agent list` and `herdr pane get <id>` then answer with
  `agent_session: {"kind": "id", "value": "<uuid>", "source": "herdr:claude"}`
  next to `cwd`, `pane_id`, `workspace_id` and `tab_id`.
- **It resumes them itself.** `[session] resume_agents_on_restore = true`
  (the default): "Herdr can use official integration-reported session
  references to restart supported agent panes after a Herdr server restart"
  — `claude --resume <id>`, `codex resume <id>` — "after a client attaches".
  Workspaces, tabs, panes, cwd, layout and focus restore too; panes it cannot
  resume come back as new shells in their saved directories.
- **A command can be started in a pane** with the agent's own arguments:
  `herdr agent start <name> --kind claude --pane <id> -- --resume <id>`, on a
  pane at its shell prompt; `herdr pane run <id> <command>` for anything else;
  `herdr workspace create --cwd <dir>` for a new pane at a directory.

So the session model carries the resume natively, and the wrapper is thinner
than requirement 8 planned for: it does not have to teach panes to report
their ids, and on the common path — server restart, client attaches — it does
not resume anything, herdr does. What it adds is a record this repository
owns, and a resume from that record for the cases herdr's own restore does
not cover: a session that was stopped and deleted, a pane closed by hand, an
agent that exited before the restart, a machine where the integration hook
was not installed. The upstream request requirement 8 reserved for the other
branch is therefore not filed; the state file's shape below is what it would
have carried.

## Verbs

    herdr.sh start      attach the session (default `sd`), creating it when absent;
                        runs `resume` first when the session is already running
    herdr.sh resume     bring every recorded pane back that is not already live
    herdr.sh snapshot   record every live agent that reports a session id
    herdr.sh record <pane> <claude|codex> <session-id> [cwd]
    herdr.sh list       print the state file
    herdr.sh forget <pane>
    herdr.sh status     0 / 3 / 1, convention 6 (local-health-check reads it)
    herdr.sh test       the unittest suite, against a fake herdr

`HERDR_SESSION` names the session (default `sd`); it is also the variable
herdr's own CLI reads to pick a named session, and the wrapper exports it on
every call together with that session's `HERDR_SOCKET_PATH` from `herdr
session list --json`, so a pane inside some other session (which inherits
that session's socket) still talks to `sd`; `start` attaches through that
socket too. `HERDR_STATE_DIR` moves the
state file (tests); `PYTHON` picks the interpreter for the JSON verbs
(default `python3`, the same dependency herdr's own Claude Code hook has).

`resume` decides per recorded pane, asking `herdr pane get` and, for a pane
with no agent, `herdr pane process-info`:

| the pane                                   | what happens                                             |
|--------------------------------------------|----------------------------------------------------------|
| holds the recorded agent session already   | `live`; nothing                                          |
| holds a different agent or session         | `busy`; left alone, said on stderr                       |
| runs something other than its shell in the foreground (an editor, a server) | `busy`; left alone, said on stderr |
| is a bare shell at its prompt              | `cd` to the recorded directory when its `foreground_cwd` is elsewhere, then `agent start` with the resume arguments |
| is gone (`pane_not_found`)                 | `workspace create --cwd <recorded>`, `agent start` in its root pane, and the record moves to the new pane id |

Exit 1 when any pane failed; `start` attaches anyway and says so.

`start` when the session is **not** running just attaches (`herdr --session
sd`), because herdr's own restore runs as the client attaches and resuming
the same panes twice would start each agent twice; run `herdr.sh resume`
from a pane afterwards for anything it missed, and `herdr.sh status` to see
whether it missed anything.

## The state file

`$HERDR_STATE_DIR/<session>.json`, default `$XDG_STATE_HOME/sd-herdr/` and so
`~/.local/state/sd-herdr/sd.json`. One JSON object, pretty-printed, sorted
keys, one entry per pane:

```json
{
  "panes": [
    {
      "agent": "claude",
      "cwd": "/Users/example/repos/system",
      "name": "sd-w4-p2",
      "pane": "w4:p2",
      "recorded_at": "2026-09-12T07:15:17Z",
      "session_id": "ff6d8010-91a7-4316-aa11-b7047b59a490"
    },
    {
      "agent": "codex",
      "cwd": "/Users/example/repos/platypeeps/sd-ai-command-pack",
      "name": "sd-w4-p4",
      "pane": "w4:p4",
      "recorded_at": "2026-09-12T07:15:17Z",
      "session_id": "019935a0-1c2e-7e4b-9d3f-000000000001"
    }
  ],
  "session": "sd"
}
```

- `pane` — herdr's public pane id (`w<workspace>:p<pane>`), stable until the
  pane closes; a pane `resume` recreates gets the new id written back.
- `agent` — the kind, as `herdr agent start --kind` spells it; only `claude`
  and `codex` are accepted, because those are the two whose resume form
  herdr documents and this wrapper knows.
- `session_id` — what the agent resumes on: `claude --resume <id>`,
  `codex resume <id>`.
- `cwd` — where the agent is resumed; the pane is `cd`'d there first when
  herdr restored it elsewhere.
- `recorded_at` — UTC, seconds.
- `name` — the live agent name `agent start` is given, derived from the pane
  id (`w4:p2` gives `sd-w4-p2`); herdr requires `[a-z][a-z0-9_-]{0,31}`,
  unique among live agents. An id the short form cannot spell back (upper
  case, another character, or too long) keeps a readable prefix and ends in
  8 hex digits of its SHA-256, so `w4:pG` and `w4:pg` get different names.

`snapshot` fills it from `herdr agent list` — the thin path, since the ids are
already known to herdr. `record` is for a pane to call as its agent starts
when that is not enough (a `SessionStart` hook that runs
`herdr.sh record "$HERDR_PANE_ID" claude "$session_id"` would do it), and
both replace an existing entry for the same pane.

Every writer (`record`, `snapshot`, `forget`, and `resume` when a pane
moved) takes an exclusive `flock` on `<session>.json.lock` beside the file
for its read-modify-write, and writes through a temporary file of its own,
so pane hooks that record at the same moment all land. `resume` holds the
lock only to write back, never while herdr works, and re-reads the file
first: a resumed agent's own hook can record while `resume` still runs.

## Verifying by hand

Criterion 17 is asserted by hand once and recorded on the item with the
`herdr` version; no test closes it, and none pretends to. The steps, on a
machine with `herdr` and both agents installed:

1. `herdr integration status` — `claude` and `codex` must read `current`;
   `herdr --version` — note it.
2. `./local-herdr/herdr.sh start` — creates and attaches the `sd` session.
3. In it, two panes: in one, `cd ~/repos/system && claude`; split, and in
   the other, `cd ~/repos/platypeeps/sd-ai-command-pack && codex`. Give each
   one prompt so the session has content to recognise it by.
4. From a third pane (or any terminal with `HERDR_SESSION=sd` exported):
   `./local-herdr/herdr.sh snapshot`, then `herdr.sh list` — two rows, one
   `claude`, one `codex`, each with a session id and the right directory.
   `herdr.sh status` — exit 0.
5. Kill herdr: `herdr session stop sd` (a clean stop), or `pkill -f 'herdr
   server'` for the ungraceful case the criterion is about. Detach.
6. `./local-herdr/herdr.sh start` again — it says the session is not running
   and attaches; herdr restores the layout and resumes both agents.
   Each pane's agent shows the earlier prompt in its history.
7. `./local-herdr/herdr.sh status` from a pane — exit 0, "2 recorded pane(s)
   live": the resumed agents report the same session ids the state file
   holds. If it exits 1, `herdr.sh resume` brings back what herdr missed and
   prints `resumed` per pane; run `status` again.
8. Then the case herdr does not cover: `herdr session stop sd && herdr
   session delete sd`, `herdr.sh start` (a fresh, empty session), and from
   a pane `herdr.sh resume` — two workspaces are created at the recorded
   directories with the agents resumed in them, and `herdr.sh list` shows
   the new pane ids.
9. Record the outcome and the `herdr` version on the item's `## Log`.

## Tests

`./local-herdr/herdr.sh test -v`. `tests/doubles/herdr` is a POSIX-sh
stand-in for the 0.9.0 CLI: it journals every argv line and answers `session
list --json`, `pane get`, `agent list`, `agent start`, `pane run` and
`workspace create` and `pane process-info` from files the tests write under a
fixture directory. The
suite prepends that directory to PATH and asserts the double is the `herdr`
it resolves, so the installed binary is never called — the CI runner has
no herdr and needs none. Wired into `.github/workflows/system-native.yml` as
`run_suite herdr`.

What the double cannot prove is that the real binary answers the way it
does. The suite assumes these 0.9.x shapes, read from the installed
binary's help, changelog and embedded schema, never from a live session:
`agent_session`, `cwd` and `foreground_cwd` in `pane get` and `agent list`;
`socket_path` and `running` in `session list --json`; `shell_pid`,
`foreground_process_group_id` and `foreground_processes` in `pane
process-info`; and that `HERDR_SOCKET_PATH` selects the session. Only the
hand run above checks them against a live session, and until it is recorded
the restart and delete flow is unverified.
