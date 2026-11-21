"""Regression tests for cswap.sh: one Claude account fleet-wide (sd:7323).

Each case runs a throwaway copy of the script, laid out beside copies of
lib/config.sh and lib/bounded.sh the way the tree is, with HOME pointed at a
directory of its own. Every program the script asks is a fake on PATH or
under a fake pack root:

- `cswap` keeps its accounts in a JSON file and logs each call;
- `sd-db fence` prints the role the case names;
- `sd config get|set sys.cswap_account` keeps the row in a file;
- `launchctl` reports the retired cswap-auto agent loaded or not.

So no case asks the real launchd, cswap or workflow database anything.
"""

import json
import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

FOLDER = pathlib.Path(__file__).resolve().parent.parent
LIB = FOLDER.parent / "lib"

ONE = "11111111-1111-4111-8111-111111111111"
TWO = "22222222-2222-4222-8222-222222222222"
THREE = "33333333-3333-4333-8333-333333333333"
STRANGER = "99999999-9999-4999-8999-999999999999"

# The accounts as `cswap list --json` names them. `auto --once` switches to
# FAKE_AUTO_TO when it is set and exits FAKE_AUTO_RC (default 2, no action).
CSWAP = r"""#!/usr/bin/env python3
import json, os, sys
state = os.environ["FAKE_CSWAP_STATE"]
with open(os.environ["FAKE_CALLS"], "a") as log:
    log.write("cswap " + " ".join(sys.argv[1:]) + "\n")
with open(state) as f:
    data = json.load(f)
def save():
    with open(state, "w") as f:
        json.dump(data, f)
args = sys.argv[1:]
if args[:2] == ["list", "--json"]:
    if os.environ.get("FAKE_LIST_FAIL"):
        sys.exit(1)
    print(json.dumps({"schemaVersion": 1, "activeAccountNumber": data["active"],
                      "accounts": [{"number": n, "email": f"user{n}@example.test",
                                    "organizationUuid": u, "active": n == data["active"]}
                                   for n, u in data["accounts"]]}))
elif args[:1] == ["auto"]:
    to = os.environ.get("FAKE_AUTO_TO")
    if to:
        data["active"] = int(to)
        save()
    sys.exit(int(os.environ.get("FAKE_AUTO_RC", "2")))
elif args[:1] == ["switch"]:
    if os.environ.get("FAKE_SWITCH_FAIL"):
        sys.exit(1)
    data["active"] = int(args[1])
    save()
else:
    sys.exit(64)
"""

# `fence` prints the state FAKE_FENCE names and exits FAKE_FENCE_RC, else 0
# for holder and off, 1 for any other, as the pack's verb does.
SD_DB = r"""#!/bin/sh
echo "sd-db $*" >> "$FAKE_CALLS"
[ "$1" = fence ] || exit 64
echo "sd-db: fence: $FAKE_FENCE: a reason"
if [ -n "${FAKE_FENCE_RC:-}" ]; then exit "$FAKE_FENCE_RC"; fi
case "$FAKE_FENCE" in holder|off) exit 0 ;; *) exit 1 ;; esac
"""

# The row lives in FAKE_ROW. FAKE_SD_DOWN is a database that does not open.
# FAKE_SD_MISMATCH is a hub on another sd_db build: as the pack's bin/sd
# does, it starts a follow unless SD_FOLLOWED is set (logged as `follow`),
# then prints the refusal the pack's tests pin (test_sd_follow.py).
SD = r"""#!/bin/sh
echo "sd $*" >> "$FAKE_CALLS"
[ "$1 $3" = "config sys.cswap_account" ] || exit 64
if [ -n "${FAKE_SD_DOWN:-}" ]; then
  echo "sd: the workflow database did not open: OSError: no route to hub" >&2
  exit 1
fi
if [ -n "${FAKE_SD_UNREACHABLE:-}" ]; then
  echo "sd: the workflow database did not open: $FAKE_SD_UNREACHABLE" >&2
  exit 1
fi
if [ -n "${FAKE_SD_MISMATCH:-}" ]; then
  if [ -z "${SD_FOLLOWED:-}" ]; then
    echo "follow" >> "$FAKE_CALLS"
    echo "sd: sd_db build 0123456789abcdef here, fedcba9876543210 on the hub: following the hub's pins" >&2
  fi
  echo "sd: sd_db build mismatch: build digest 0123456789abcdef on this satellite, fedcba9876543210 on the hub; run \`repo-sync.sh follow\` on this satellite for sd_db fedcba9876543210. The hub ran no statement" >&2
  exit 1
fi
case "$2" in
  get)
    if [ -s "$FAKE_ROW" ]; then cat "$FAKE_ROW"; exit 0; fi
    echo "sd: sys.cswap_account is not set (the account). Run:" >&2
    exit 1 ;;
  set)
    [ -z "${FAKE_SET_FAIL:-}" ] || { echo "sd: not written" >&2; exit 1; }
    printf '%s\n' "$4" > "$FAKE_ROW" ;;
  *) exit 64 ;;
esac
"""

# launchd holds the retired agent when FAKE_LOADED is set; FAKE_BOOTOUT_RC
# fails its bootout.
LAUNCHCTL = r"""#!/bin/sh
echo "launchctl $*" >> "$FAKE_CALLS"
case "$1" in
  print) [ -n "${FAKE_LOADED:-}" ] || exit 113; echo "$2 = {" ;;
  bootout) exit "${FAKE_BOOTOUT_RC:-0}" ;;
esac
exit 0
"""

# herdr's panes live in FAKE_HERDR as JSON: [pane, agent, status, session,
# screen] rows, and an optional sixth {"status", "session", "screen"}: the
# pane's state once its screen was read once, as a pane that changes
# between the read and the send. With "reads": N it is the state from
# the Nth read on, that read included: a pane that changes during a
# read; with "gets": N, from the Nth `agent get` on. `agent list` fails with FAKE_HERDR_DOWN
# (no server); `agent get` answers one pane; `agent read` prints its
# screen; `agent prompt` is logged and sends nothing.
HERDR = r"""#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_CALLS"], "a") as log:
    log.write("herdr " + " ".join(sys.argv[1:]) + "\n")
panes = json.load(open(os.environ["FAKE_HERDR"]))
args = sys.argv[1:]
# Like herdr: HERDR_SESSION names the session, and an inherited
# HERDR_SOCKET_PATH overrides it. The panes live in the session
# FAKE_HERDR_SESSION names (default sd); any other is running and empty.
if (os.environ.get("HERDR_SESSION") or "default") != os.environ.get("FAKE_HERDR_SESSION", "sd") \
        or os.environ.get("HERDR_SOCKET_PATH"):
    panes = []
read = os.environ["FAKE_HERDR"] + ".read"
seen = open(read).read().split() if os.path.exists(read) else []
def state(row):
    p, a, st, se, sc = row[:5]
    later = row[5] if len(row) > 5 else {}
    if "reads" in later:
        reading = args[:2] == ["agent", "read"] and args[2] == p
        later = later if seen.count(p) + reading >= later["reads"] else {}
    elif "gets" in later:
        # The state from the Nth `agent get` of the pane on, that get
        # included: a pane that changes while it is asked.
        gets = open(os.environ["FAKE_CALLS"]).read().splitlines().count("herdr agent get " + p)
        later = later if gets >= later["gets"] else {}
    elif p not in seen:
        later = {}
    return p, a, later.get("status", st), later.get("session", se), later.get("screen", sc)
def switch(path):
    # A new switch mark, later than the last, as owe_rc writes it.
    import time
    try:
        last = int(open(path).read().split()[0])
    except (OSError, ValueError, IndexError):
        last = 0
    with open(path, "w") as f:
        f.write("%d 424242\n" % max(time.time(), last + 1))
def agent(row):
    p, a, st, se, _ = state(row)
    # A session of None is herdr naming none: agent_session false.
    return {"agent": a, "agent_status": st, "pane_id": p,
            "agent_session": se is not None and {"agent": a, "kind": "id", "value": se}}
if args[:2] == ["agent", "list"]:
    import time
    # A switch by another sync that lands while this list runs.
    if os.environ.get("FAKE_SWITCH_DURING_LIST"):
        switch(os.environ["FAKE_SWITCH_DURING_LIST"])
    time.sleep(float(os.environ.get("FAKE_HERDR_LIST_SLOW") or 0))
    if os.environ.get("FAKE_HERDR_DOWN"):
        print("herdr: server not running", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"id": "cli:agent:list", "result": {"agents": [agent(r) for r in panes]}}))
elif args[:2] == ["agent", "get"]:
    import time
    # A switch by another sync that lands during the pass.
    if os.environ.get("FAKE_SWITCH_DURING_GET"):
        switch(os.environ["FAKE_SWITCH_DURING_GET"])
    time.sleep(float(os.environ.get("FAKE_HERDR_GET_SLOW") or 0))
    # FAKE_OWED_AT_GET: rc-owed as the get finds it, copied there.
    if os.environ.get("FAKE_OWED_AT_GET"):
        import shutil
        shutil.copy(os.path.join(os.environ["FAKE_STATE_DIR"], "rc-owed"),
                    os.environ["FAKE_OWED_AT_GET"])
    if os.environ.get("FAKE_HERDR_GET_FAIL"):
        print("herdr: no such pane", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"id": "cli:agent:get", "result": {"type": "agent_info", "agent": next(
        agent(r) for r in panes if r[0] == args[2])}}))
elif args[:2] == ["agent", "read"]:
    # Like herdr: the ANSI codes only with --format ansi. FAKE_HERDR_SLOW
    # seconds per read make a slow pane.
    import re, time
    time.sleep(float(os.environ.get("FAKE_HERDR_SLOW") or 0))
    # FAKE_HERDR_READ_FAIL=N fails the Nth read; FAKE_CLOCK_JUMP_AT=N
    # moves the stub clock FAKE_CLOCK_JUMP seconds on at the Nth read.
    nth = len(seen) + 1
    if os.environ.get("FAKE_HERDR_READ_FAIL") == str(nth):
        print("herdr: read failed", file=sys.stderr)
        sys.exit(1)
    if os.environ.get("FAKE_CLOCK_JUMP_AT") == str(nth):
        with open(os.environ["FAKE_CLOCK"], "w") as f:
            f.write(os.environ["FAKE_CLOCK_JUMP"])
    screen = next(state(r)[4] for r in panes if r[0] == args[2])
    with open(read, "a") as f:
        f.write(args[2] + "\n")
    if args[3:5] != ["--format", "ansi"]:
        screen = re.sub(r"\x1b\[[0-9;]*m", "", screen)
    print(screen)
elif args[:2] == ["agent", "prompt"]:
    # Like herdr 0.9.5: --timeout without --wait is refused before any
    # key is sent. FAKE_HERDR_PROMPT_SLOW seconds make a slow delivery.
    import time
    if "--timeout" in args and "--wait" not in args:
        print("error: --timeout requires --wait", file=sys.stderr)
        sys.exit(2)
    time.sleep(float(os.environ.get("FAKE_HERDR_PROMPT_SLOW") or 0))
    # The keys reach the pane once the delivery delay is over.
    if os.environ.get("FAKE_DELIVERED"):
        with open(os.environ["FAKE_DELIVERED"], "a") as f:
            f.write(args[2] + "\n")
    # FAKE_HERDR_PROMPT_LATE seconds after the keys: herdr acknowledges
    # a submission late, past its --timeout.
    time.sleep(float(os.environ.get("FAKE_HERDR_PROMPT_LATE") or 0))
else:
    sys.exit(64)
"""

# A pane's screen: a Remote Control line, then the input box. INPUT is the
# empty input line; the screen keeps older lines above the newest.
# The box rules as Claude draws them, in their own colour (read from a
# hub pane, 2026-11-20); a rule typed into a draft is plain text.
RULE = "\x1b[0m\x1b[38;2;136;136;136m" + "\u2500" * 20 + "\x1b[0m"
TYPED_RULE = "\u2500" * 20
INPUT = f"{RULE}\n\u276f\u00a0\n{RULE}\n  [model] | git:(main)"
# The banners as Claude prints them, colours and all (a hub pane, read
# with --format ansi). Prose that quotes their text is no banner.
YELLOW = "\x1b[0m\x1b[38;2;255;193;7m"
DROP_LINE = (f"{YELLOW}\u23fa\x1b[0m \x1b[0m{YELLOW}Remote Control disconnected \u2014 signed-in "
             "claude.ai account or organization changed on this machine \u2014 run\x1b[0m")
ACTIVE_LINE = ("  /remote-control is active\x1b[0m\x1b[38;2;153;153;153m \u00b7 Continue here, "
               "on your phone, or at \x1b[0m")
DROPPED = f"{DROP_LINE}\n{INPUT}"
CONNECTED = f"{ACTIVE_LINE}\n{INPUT}"


def newest_bash():
    """A bash 4 or later when the machine has one, else PATH's bash. macOS
    /bin/bash 3.2 reads `local x` as set, bash 5 as unset under set -u:
    a sync passed here and died under cron's Homebrew bash (sd:9427
    review), so the suite runs the bash that is stricter."""
    for candidate in ("/opt/homebrew/bin/bash", "/usr/local/bin/bash", shutil.which("bash")):
        if candidate and os.access(candidate, os.X_OK):
            done = subprocess.run([candidate, "-c", "echo ${BASH_VERSINFO[0]}"],
                                  capture_output=True, text=True)
            if done.stdout.strip().isdigit() and int(done.stdout) >= 4:
                return candidate
    return "bash"


BASH = newest_bash()


def executable(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class Machine(unittest.TestCase):
    """One machine: its accounts, its role and the published row."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="cswap-test."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        folder = self.tmp / "local-cswap"
        folder.mkdir()
        for name in ("cswap.sh", "cswap-auto.conf"):
            shutil.copy(FOLDER / name, folder / name)
        (self.tmp / "lib").mkdir()
        for name in ("config.sh", "bounded.sh"):
            shutil.copy(LIB / name, self.tmp / "lib" / name)
        self.script = folder / "cswap.sh"
        self.home = self.tmp / "home"
        (self.home / ".local/bin").mkdir(parents=True)
        executable(self.home / ".local/bin/cswap", CSWAP)
        pack = self.tmp / "pack" / "bin"
        pack.mkdir(parents=True)
        executable(pack / "sd-db", SD_DB)
        executable(pack / "sd", SD)
        stubs = self.tmp / "stub-bin"
        stubs.mkdir()
        executable(stubs / "launchctl", LAUNCHCTL)
        self.calls = self.tmp / "calls.txt"
        self.calls.touch()
        self.state = self.tmp / "cswap.json"
        self.row = self.tmp / "row"
        self.agent = self.home / "Library/LaunchAgents/local.system-tools.cswap-auto.plist"
        self.env = {"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(self.home),
                    "SYSTEM_TOOLS_CONFIG": str(self.tmp / "config"),
                    "SD_PACK_ROOT": str(self.tmp / "pack"), "FAKE_CALLS": str(self.calls),
                    "FAKE_CSWAP_STATE": str(self.state), "FAKE_ROW": str(self.row),
                    # Never the machine's own herdr: a test must not send /rc.
                    "CSWAP_HERDR_DIRS": ""}
        self.stubs = stubs
        self.accounts([(1, ONE), (2, TWO), (3, THREE)], active=1)

    def accounts(self, pairs, active):
        self.state.write_text(json.dumps({"accounts": pairs, "active": active}))

    def active(self):
        return json.loads(self.state.read_text())["active"]

    def publish(self, uuid):
        self.row.write_text(uuid + "\n")

    def published(self):
        return self.row.read_text().strip() if self.row.exists() else None

    def run_verb(self, *args, **extra):
        self.calls.write_text("")
        return subprocess.run([BASH, str(self.script), *args], env={**self.env, **extra},
                              capture_output=True, text=True, timeout=60)

    def sync(self, fence, code=0, **extra):
        result = self.run_verb("sync", FAKE_FENCE=fence, **extra)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def called(self, prefix):
        return [line for line in self.calls.read_text().splitlines() if line.startswith(prefix)]


class TheHubLeads(Machine):
    def test_the_hub_runs_auto_consume_first_and_publishes_its_account(self):
        self.accounts([(1, ONE), (2, TWO), (3, THREE)], active=2)
        self.sync("holder")
        self.assertEqual(self.called("cswap auto"), ["cswap auto --once --strategy consume-first"])
        self.assertEqual(self.published(), TWO)

    def test_the_hub_publishes_the_account_auto_switched_to(self):
        self.publish(ONE)
        self.sync("holder", FAKE_AUTO_TO="3", FAKE_AUTO_RC="0")
        self.assertEqual(self.published(), THREE)

    def test_the_hub_keeps_the_configured_threshold(self):
        self.sync("holder", CSWAP_AUTO_ARGS="--threshold 85")
        self.assertEqual(self.called("cswap auto"),
                         ["cswap auto --once --threshold 85 --strategy consume-first"])

    def test_an_unchanged_account_writes_no_row(self):
        # One `setting` row per write: a minute job must not add 1440 a day.
        self.publish(ONE)
        self.sync("holder")
        self.assertEqual(self.called("sd config set"), [])

    def test_the_hub_never_switches_to_follow(self):
        self.publish(THREE)
        self.sync("holder")
        self.assertEqual(self.called("cswap switch"), [])
        self.assertEqual(self.published(), ONE)

    def test_a_failed_publish_fails_and_the_next_run_publishes(self):
        # Step: auto switched, then the row write failed. Recovery: the next
        # run compares the row with the active account again.
        self.publish(ONE)
        result = self.sync("holder", code=1, FAKE_AUTO_TO="2", FAKE_AUTO_RC="0", FAKE_SET_FAIL="1")
        self.assertIn("SD_REPORT_ATTENTION: data safe, one step failed: cswap sync publish",
                      result.stdout)
        self.assertEqual(self.published(), ONE)
        self.sync("holder")
        self.assertEqual(self.published(), TWO)

    def test_a_failed_auto_still_publishes_the_active_account_and_fails(self):
        result = self.sync("holder", code=1, FAKE_AUTO_RC="1")
        self.assertEqual(self.published(), ONE)
        self.assertIn("cswap auto exited 1", result.stderr)

    def test_a_promoted_satellite_leads(self):
        # Failover: the satellite's fence reads holder, so it runs auto and
        # publishes under its own numbering; it follows nobody.
        self.accounts([(1, THREE), (2, ONE), (3, TWO)], active=1)
        self.publish(ONE)
        self.sync("holder")
        self.assertEqual(self.called("cswap auto"), ["cswap auto --once --strategy consume-first"])
        self.assertEqual(self.called("cswap switch"), [])
        self.assertEqual(self.published(), THREE)

    def test_a_fence_that_fails_does_not_lead(self):
        # Failures: the words count only with exit 0. An unknown fence must
        # not make a second leader; this one follows the row.
        self.publish(ONE)
        self.sync("holder", FAKE_FENCE_RC="1")
        self.assertEqual(self.called("cswap auto"), [])
        self.assertEqual(self.called("sd config set"), [])


class SatellitesFollow(Machine):
    def test_a_satellite_switches_to_the_published_account(self):
        self.publish(THREE)
        result = self.sync("elsewhere")
        self.assertEqual(self.called("cswap switch"), ["cswap switch 3"])
        self.assertEqual(self.active(), 3)
        self.assertEqual(self.called("cswap auto"), [])
        self.assertEqual(self.called("sd config set"), [])
        self.assertIn("switched to account 3", result.stdout)

    def test_a_satellite_maps_the_account_by_its_uuid_not_its_number(self):
        self.accounts([(1, THREE), (2, ONE), (3, TWO)], active=2)
        self.publish(TWO)
        self.sync("elsewhere")
        self.assertEqual(self.called("cswap switch"), ["cswap switch 3"])

    def test_a_satellite_on_the_account_does_not_switch(self):
        self.publish(ONE)
        result = self.sync("elsewhere")
        self.assertEqual(self.called("cswap switch"), [])
        self.assertIn("account 1 already active", result.stdout)

    def test_an_unknown_uuid_never_switches(self):
        self.publish(STRANGER)
        result = self.sync("elsewhere", code=1)
        self.assertEqual(self.called("cswap switch"), [])
        self.assertIn(f"no account here has organizationUuid {STRANGER}", result.stderr)

    def test_a_uuid_two_accounts_share_never_switches(self):
        self.accounts([(1, ONE), (2, TWO), (3, TWO)], active=1)
        self.publish(TWO)
        result = self.sync("elsewhere", code=1)
        self.assertEqual(self.called("cswap switch"), [])
        self.assertIn("2 accounts here have organizationUuid", result.stderr)

    def test_an_unreadable_hub_never_switches(self):
        self.publish(THREE)
        result = self.sync("unknown", code=1, FAKE_SD_DOWN="1")
        self.assertEqual(self.called("cswap switch"), [])
        self.assertIn("cannot read the hub's account", result.stderr)
        self.assertIn("SD_REPORT_ATTENTION: data safe, one step failed: cswap sync read",
                      result.stdout)

    def test_an_unpublished_account_never_switches(self):
        result = self.sync("elsewhere", code=1)
        self.assertEqual(self.called("cswap switch"), [])
        self.assertIn("is not set", result.stderr)

    def test_a_row_that_is_no_uuid_never_switches(self):
        self.publish("not-a-uuid")
        self.sync("elsewhere", code=1)
        self.assertEqual(self.called("cswap switch"), [])

    def test_a_failed_switch_fails_and_the_next_run_switches(self):
        self.publish(TWO)
        self.sync("elsewhere", code=1, FAKE_SWITCH_FAIL="1")
        self.assertEqual(self.active(), 1)
        self.sync("elsewhere")
        self.assertEqual(self.active(), 2)

    def test_a_failed_account_list_never_switches(self):
        self.publish(TWO)
        self.sync("elsewhere", code=1, FAKE_LIST_FAIL="1")
        self.assertEqual(self.called("cswap switch"), [])


# What sd prints when the tailnet drops: the hub times out, or Tailscale
# cannot name this node (two satellites, 2026-11-05).
UNREACHABLE = ("HubUnreachable: the sd hub at hub.example.test:8769 is unreachable: "
               "timed out (named in /home/.config/sd/hub.json)")
TAILNET_DOWN = ("Fenced: refused: this machine is not the hub: Tailscale does not name "
                "this node, so it cannot match hub.example.test: this node is not "
                "connected to the Tailscale control plane, so its name may be stale")


class APinFollow(Machine):
    """The hub moved its sd_db build; this satellite has not followed yet.

    `repo-sync.sh follow` catches up within its job's slot and its 600 s
    bound, so a read refused for the build is no failure until it lasts
    (system sd:7407).
    """

    def count(self):
        path = self.tmp / "local-cswap/state/build-refusals"
        return path.read_text().strip() if path.exists() else None

    def seed(self, text):
        (self.tmp / "local-cswap/state").mkdir(exist_ok=True)
        (self.tmp / "local-cswap/state/build-refusals").write_text(text)

    def test_a_read_the_hub_refuses_for_its_build_skips_and_switches_nothing(self):
        self.publish(THREE)
        result = self.sync("elsewhere", FAKE_SD_MISMATCH="1")
        self.assertIn("skipped: the hub refuses this sd_db build until repo-sync.sh follow "
                      "catches up (run 1 of 30); nothing switched", result.stdout)
        self.assertNotIn("SD_REPORT_ATTENTION", result.stdout)
        self.assertEqual(self.called("cswap switch"), [])
        self.assertEqual(self.active(), 1)
        self.assertEqual(self.count(), "1")

    def test_the_read_starts_no_follow_its_bound_would_cut(self):
        # sd's own follow takes up to 600 s; the 60 s bound killed it each
        # minute, part way through its move.
        self.publish(THREE)
        self.sync("elsewhere", FAKE_SD_MISMATCH="1")
        self.assertEqual(self.called("follow"), [])

    def test_a_refusal_that_outlasts_the_follow_window_fails(self):
        self.publish(THREE)
        self.seed("28\n")
        self.sync("elsewhere", FAKE_SD_MISMATCH="1")
        self.assertEqual(self.count(), "29")
        result = self.sync("elsewhere", code=1, FAKE_SD_MISMATCH="1")
        self.assertIn("SD_REPORT_ATTENTION: data safe, one step failed: cswap sync read",
                      result.stdout)
        self.assertIn("30 runs in a row could not read the hub's account", result.stderr)
        self.assertIn("this run: the hub refuses this sd_db build", result.stderr)
        self.assertEqual(self.called("cswap switch"), [])
        # It keeps failing, one open report, until a read passes.
        self.sync("elsewhere", code=1, FAKE_SD_MISMATCH="1")
        self.assertEqual(self.count(), "31")

    def test_a_good_read_clears_the_count(self):
        self.publish(THREE)
        self.seed("29\n")
        self.sync("elsewhere")
        self.assertIsNone(self.count())
        self.assertEqual(self.active(), 3)
        self.sync("elsewhere", FAKE_SD_MISMATCH="1")
        self.assertEqual(self.count(), "1")

    def test_a_count_that_is_no_number_starts_again(self):
        # Older state: a file a killed write left half written.
        self.publish(THREE)
        self.seed("")
        self.sync("elsewhere", FAKE_SD_MISMATCH="1")
        self.assertEqual(self.count(), "1")

    def test_a_count_that_cannot_be_written_fails(self):
        # Failures: a run that cannot count would skip for ever.
        self.publish(THREE)
        (self.tmp / "local-cswap/state").write_text("a file where the folder goes\n")
        result = self.sync("elsewhere", code=1, FAKE_SD_MISMATCH="1")
        self.assertIn("SD_REPORT_ATTENTION: data safe, one step failed: cswap sync read",
                      result.stdout)
        self.assertEqual(self.called("cswap switch"), [])
        # A good read still switches: a count it cannot clear only fails sooner.
        self.sync("elsewhere")
        self.assertEqual(self.active(), 3)

    def test_another_read_failure_still_fails_at_once(self):
        self.publish(THREE)
        self.seed("3\n")
        result = self.sync("elsewhere", code=1, FAKE_SD_DOWN="1")
        self.assertIn("SD_REPORT_ATTENTION: data safe, one step failed: cswap sync read",
                      result.stdout)
        self.assertEqual(self.count(), "3")

    def test_an_unreachable_hub_skips_until_it_lasts(self):
        # A tailnet blip of a minute or two opened a report each time on
        # both satellites (sd:7650): an unreachable hub counts like a
        # build refusal and fails from the 30th run in a row.
        for text in (UNREACHABLE, TAILNET_DOWN):
            with self.subTest(text[:20]):
                self.publish(THREE)
                self.seed("0\n")
                result = self.sync("elsewhere", FAKE_SD_UNREACHABLE=text)
                self.assertIn("skipped: the hub is unreachable (run 1 of 30); "
                              "nothing switched", result.stdout)
                self.assertNotIn("SD_REPORT_ATTENTION", result.stdout)
                self.assertEqual(self.called("cswap switch"), [])
                self.assertEqual(self.count(), "1")
                self.seed("29\n")
                result = self.sync("elsewhere", code=1, FAKE_SD_UNREACHABLE=text)
                self.assertIn("SD_REPORT_ATTENTION: data safe, one step failed: cswap sync read",
                              result.stdout)
                self.assertIn("30 runs in a row could not read the hub's account",
                              result.stderr)
                self.assertIn("this run: the hub is unreachable", result.stderr)

    def test_a_mixed_run_of_causes_fails_naming_both(self):
        # Build refusals and unreachable runs share one count (sd:7722):
        # 29 refusals and one unreachable run are no 30 unreachable runs.
        self.publish(THREE)
        self.seed("0\n")
        self.sync("elsewhere", FAKE_SD_MISMATCH="1")
        self.seed("29\n")
        result = self.sync("elsewhere", code=1, FAKE_SD_UNREACHABLE=UNREACHABLE)
        self.assertNotIn("unreachable for 30 runs", result.stderr)
        self.assertIn("30 runs in a row could not read the hub's account, each one "
                      "a refused sd_db build or an unreachable hub; this run: the hub "
                      "is unreachable", result.stderr)

    def test_status_starts_no_follow_either(self):
        self.publish(ONE)
        result = self.run_verb("status", FAKE_FENCE="elsewhere", FAKE_SD_MISMATCH="1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.called("follow"), [])


class RemoteControlReconnects(Machine):
    """sd:9427: a switch drops every Remote Control link; /rc restores it."""

    def setUp(self):
        super().setUp()
        # The screen-shape tests test no timing: a gap a loaded machine
        # meets (sd:9427 triage). Timing tests pass their own.
        self.env["CSWAP_RC_GAP_MS"] = "5000"

    def panes(self, *rows, where=None):
        path = self.tmp / "herdr.json"
        path.write_text(json.dumps(rows))
        self.env["FAKE_HERDR"] = str(path)
        executable((where or self.stubs) / "herdr", HERDR)

    def prompts(self):
        return self.called("herdr agent prompt")

    def owe(self, rows, since):
        """Seed rc-owed with rows of the switch at `since`, that switch's
        mark, and sd as listed for it: only the current switch's rows
        take a /rc."""
        state = self.script.parent / "state"
        state.mkdir(exist_ok=True)
        (state / "rc-pending").write_text(f"{since} 42\n")
        (state / "rc-listed").write_text(f"{since} 42 sd\n")
        (state / "rc-owed").write_text(rows)
        return state / "rc-owed"

    def owed(self):
        path = self.script.parent / "state/rc-owed"
        # A row with " sent" added is a pane sent /rc: no longer owed.
        return [line.split()[1] for line in path.read_text().splitlines()
                if not line.endswith(" sent")] if path.exists() else []

    def test_a_switch_sends_rc_to_idle_panes_only(self):
        self.publish(THREE)
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED],
                   ["w1:p2", "claude", "done", "s2", DROPPED],
                   ["w1:p3", "claude", "working", "s3", DROPPED],
                   ["w1:p4", "claude", "blocked", "s4", DROPPED],
                   ["w1:p5", "codex", "idle", "s5", DROPPED])
        result = self.sync("elsewhere")
        self.assertEqual(self.prompts(), ["herdr agent prompt w1:p1 /rc", "herdr agent prompt w1:p2 /rc"])
        self.assertEqual(self.owed(), ["w1:p3", "w1:p4"])
        # herdr took each send (sd:9427 review: --timeout without --wait
        # was refused before any key went out).
        self.assertIn("reconnect: sent /rc to w1:p1\n", result.stdout)
        self.assertIn("reconnect: sent /rc to w1:p2\n", result.stdout)
        self.assertIn("2 pane(s) still owe /rc", result.stdout)

    def test_a_busy_pane_gets_rc_once_it_is_idle(self):
        self.publish(THREE)
        self.panes(["w1:p3", "claude", "working", "s3", DROPPED])
        self.sync("elsewhere")
        self.assertEqual(self.prompts(), [])
        self.panes(["w1:p3", "claude", "idle", "s3", DROPPED])
        self.sync("elsewhere")
        self.assertEqual(self.prompts(), ["herdr agent prompt w1:p3 /rc"])
        self.assertEqual(self.owed(), [])

    def test_slow_panes_neither_delay_the_switch_nor_outrun_the_budget(self):
        # sd:9282 lane review: owed panes were read before the account
        # sync, with no shared deadline. Now the switch comes first, and
        # the pass stops at its budget; the panes it did not reach stay
        # owed, and a later sync sends them.
        owed = self.script.parent / "state/rc-owed"
        owed.parent.mkdir(exist_ok=True)
        now = int(time.time())
        owed.write_text("".join(f"sd w1:p{n} s{n} {now}\n" for n in (1, 2, 3)))
        self.publish(THREE)
        self.panes(*(["w1:p%d" % n, "claude", "idle", "s%d" % n, DROPPED] for n in (1, 2, 3)))
        result = self.sync("elsewhere", FAKE_HERDR_SLOW="1", CSWAP_RC_BUDGET="5", CSWAP_RC_GAP_MS="5000")
        calls = self.calls.read_text().splitlines()
        first_read = next(i for i, c in enumerate(calls) if c.startswith("herdr agent read"))
        self.assertLess(next(i for i, c in enumerate(calls) if c.startswith("cswap switch")), first_read)
        self.assertEqual(self.prompts(), ["herdr agent prompt w1:p1 /rc"])
        self.assertEqual(self.owed(), ["w1:p2", "w1:p3"])
        self.assertIn("reconnect: the 5s budget is spent; w1:p3 stays owed", result.stdout)
        self.sync("elsewhere")
        self.assertEqual(self.prompts(), ["herdr agent prompt w1:p2 /rc", "herdr agent prompt w1:p3 /rc"])
        self.assertEqual(self.owed(), [])

    def test_the_budget_bounds_each_herdr_call_not_only_pane_entry(self):
        # sd:9427 advisory: a pane entered near the deadline still made
        # herdr calls of 10 s each, and the agent list ran outside it.
        now = int(time.time())
        self.owe("sd w1:p1 s1 %d\n" % now, now)
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
        for slow in ({"FAKE_HERDR_SLOW": "8"}, {"FAKE_HERDR_LIST_SLOW": "8"}):
            with self.subTest(slow=slow):
                self.publish(ONE)
                start = time.monotonic()
                self.sync("elsewhere", CSWAP_RC_BUDGET="2", **slow)
                self.assertLess(time.monotonic() - start, 6)
                self.assertEqual(self.prompts(), [])
                self.assertEqual(self.owed(), ["w1:p1"])
        self.assertEqual(len(self.called("herdr agent list")), 1)

    def test_two_switches_in_one_second_each_owe_their_panes(self):
        # Owed rows carry their switch's second: a second switch within
        # it kept the first one's sent row, and its pane got no /rc. A
        # `date` stub holds the clock; each mark is later than the last.
        executable(self.stubs / "date", '#!/bin/sh\n[ "$1" = +%s ] && { echo 1795000000; exit 0; }\n'
                                        'exec /bin/date "$@"\n')
        for account in (THREE, ONE):
            self.publish(account)
            self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
            result = self.sync("elsewhere")
            self.assertEqual(self.prompts(), ["herdr agent prompt w1:p1 /rc"], result.stdout)

    def test_the_managed_herdr_session_is_the_one_asked(self):
        # Review on sd:9427: cron's herdr calls went to herdr's default
        # session, not local-herdr's `sd`; an empty default list cleared
        # the pending switch. A pane inside another session inherits its
        # socket path, which must not win either. HERDR_SESSION overrides,
        # as in local-herdr.
        for extra in ({}, {"HERDR_SOCKET_PATH": "/nowhere.sock"},
                      {"HERDR_SESSION": "work", "FAKE_HERDR_SESSION": "work"}):
            with self.subTest(extra=extra):
                self.publish(ONE if self.active() == 3 else THREE)
                self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
                self.sync("elsewhere", **extra)
                self.assertEqual(self.prompts(), ["herdr agent prompt w1:p1 /rc"])
                self.assertEqual(self.owed(), [])

    def test_a_pane_owed_in_another_herdr_session_outlives_this_sync(self):
        # sd:9427 blocker (cswap.sh:409): the owed list kept no herdr
        # session, so a cron sync over sd read a pane owed in session
        # work as gone and dropped it. Each sync touches its own rows.
        work = {"HERDR_SESSION": "work", "FAKE_HERDR_SESSION": "work"}
        self.publish(THREE)
        self.panes(["w1:p1", "claude", "working", "s1", DROPPED])
        self.sync("elsewhere", **work)
        self.assertEqual(self.owed(), ["w1:p1"])
        # A later switch, listed in sd, owes sd's panes beside it.
        self.publish(ONE)
        result = self.sync("elsewhere", FAKE_HERDR_SESSION="work")
        self.assertNotIn("w1:p1 is gone", result.stdout)
        self.assertEqual(self.owed(), ["w1:p1"])
        self.sync("elsewhere", FAKE_HERDR_SESSION="work")
        self.assertEqual(self.owed(), ["w1:p1"])
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
        self.sync("elsewhere", **work)
        self.assertEqual(self.prompts(), ["herdr agent prompt w1:p1 /rc"])
        self.assertEqual(self.owed(), [])

    def test_each_herdr_session_lists_its_panes_for_a_switch(self):
        # Review on sd:9427 (cswap.sh:374): the first session to list,
        # even an empty one, cleared the machine-wide mark, so the panes
        # of session work were never owed for that switch.
        self.publish(THREE)
        self.panes(["w1:p1", "claude", "working", "s1", DROPPED])
        self.sync("elsewhere", FAKE_HERDR_SESSION="work")
        self.assertEqual(self.owed(), [])
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
        self.sync("elsewhere", HERDR_SESSION="work", FAKE_HERDR_SESSION="work")
        self.assertEqual(self.prompts(), ["herdr agent prompt w1:p1 /rc"])
        # Once listed, a session lists no second time for one switch.
        self.sync("elsewhere", HERDR_SESSION="work", FAKE_HERDR_SESSION="work")
        self.assertEqual(self.prompts(), [])

    def test_a_sync_while_another_reconnects_leaves_the_state_alone(self):
        # Review on sd:9427 (cswap.sh:467): a cron sync and a manual one
        # each rewrote rc-owed through rc-owed.new; the last writer lost
        # the other's rows. One reconnect pass runs at a time: the other
        # sync skips, and its rows stay for the next.
        import fcntl
        now = int(time.time())
        rows = "sd w1:p1 s1 %d\n" % now
        owed = self.owe(rows, now)
        self.publish(ONE)
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
        with open(self.script.parent / "state/rc-lock", "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            result = self.sync("elsewhere")
        self.assertEqual(self.prompts(), [])
        self.assertEqual(owed.read_text(), rows)
        self.assertIn("reconnect: skipped: another sync is reconnecting", result.stdout)
        self.sync("elsewhere")
        self.assertEqual(self.prompts(), ["herdr agent prompt w1:p1 /rc"])

    def test_no_sync_deletes_a_switch_mark(self):
        # Review on sd:9427 (cswap.sh:348): the expiry branch deleted
        # the mark, and a switch could land between its read and the
        # delete. Only a switch writes the mark; an old one owes nothing.
        mark = self.script.parent / "state/rc-pending"
        mark.parent.mkdir(exist_ok=True)
        mark.write_text("1000 1\n")
        self.publish(ONE)
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
        self.sync("elsewhere")
        self.assertEqual(mark.read_text(), "1000 1\n")
        self.assertEqual(self.called("herdr"), [])

    def test_a_list_marks_only_the_switch_it_read(self):
        # Review on sd:9427: a switch during the list must not be marked
        # listed by that list; the next sync lists again for it.
        mark = self.script.parent / "state/rc-pending"
        self.publish(THREE)
        self.panes(["w1:p1", "claude", "working", "s1", DROPPED])
        self.sync("elsewhere", FAKE_SWITCH_DURING_LIST=str(mark))
        self.sync("elsewhere")
        self.assertEqual(len(self.called("herdr agent list")), 1)
        listed = (self.script.parent / "state/rc-listed").read_text().split("\n")
        self.assertIn(mark.read_text().strip() + " sd", listed)

    def test_rows_of_an_older_switch_get_no_rc(self):
        # Class pass, last round on sd:9427: the pass acted on owed rows
        # that were not the current switch's. Each row below is one way
        # an older switch's rows reach the pass; each sent /rc then, and
        # the newer switch's listing sent a second one to a pane whose
        # drop banner was not yet redrawn.
        # 1. The newer switch's owed write failed (review finding).
        state = self.script.parent / "state"
        state.mkdir(exist_ok=True)
        now = int(time.time())
        (state / "rc-pending").write_text(f"{now} 42\n")
        (state / "rc-owed").write_text(f"sd w1:p1 s1 {now - 60}\n")
        failing = self.tmp / "failing-printf.sh"
        failing.write_text(f'printf() {{ case "$*" in *"w1:p1 s1 {now}"*) return 1;; esac; builtin printf "$@"; }}\n')
        self.publish(ONE)
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
        result = self.sync("elsewhere", BASH_ENV=str(failing))
        self.assertEqual(self.prompts(), [], result.stdout)
        result = self.sync("elsewhere")
        self.assertEqual(self.prompts(), ["herdr agent prompt w1:p1 /rc"], result.stdout)

    def test_a_switch_during_the_listing_leaves_its_rows_unsent(self):
        # 2. A newer switch landed while this sync listed the older one.
        mark = self.script.parent / "state/rc-pending"
        self.publish(THREE)
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
        result = self.sync("elsewhere", FAKE_SWITCH_DURING_LIST=str(mark))
        self.assertEqual(self.prompts(), [], result.stdout)
        result = self.sync("elsewhere")
        self.assertEqual(self.prompts(), ["herdr agent prompt w1:p1 /rc"], result.stdout)

    def test_a_switch_during_the_pass_stops_the_send(self):
        # 3. A newer switch landed between the plan and the keys.
        mark = self.script.parent / "state/rc-pending"
        self.publish(THREE)
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
        result = self.sync("elsewhere", FAKE_SWITCH_DURING_GET=str(mark))
        self.assertEqual(self.prompts(), [], result.stdout)
        result = self.sync("elsewhere")
        self.assertEqual(self.prompts(), ["herdr agent prompt w1:p1 /rc"], result.stdout)

    def test_an_unreadable_owed_list_is_never_replaced(self):
        # Review on sd:9427 (cswap.sh:380): a failed read of the other
        # sessions' rows was masked, and the rewrite dropped them.
        owed = self.script.parent / "state/rc-owed"
        owed.parent.mkdir(exist_ok=True)
        rows = "work w9:p9 s9 %d\n" % int(time.time())
        owed.write_text(rows)
        owed.chmod(0)
        self.addCleanup(owed.chmod, 0o600)
        self.publish(THREE)
        self.panes(["w1:p1", "claude", "working", "s1", DROPPED])
        result = self.sync("elsewhere")
        owed.chmod(0o600)
        self.assertEqual(owed.read_text(), rows)
        self.assertIn("could not read", result.stdout)
        # Not marked listed: once the list reads, the next sync owes sd's panes.
        self.sync("elsewhere")
        self.assertEqual(sorted(self.owed()), ["w1:p1", "w9:p9"])

    def test_a_failed_write_publishes_no_partial_state_file(self):
        # Round-8 blocker on sd:9427 (cswap.sh:387): a grouped write
        # checked only its last command, so a failed write of the other
        # session's rows still moved a partial rc-owed or rc-listed in.
        # A printf that fails on the work session's rows stands in.
        state = self.script.parent / "state"
        state.mkdir(exist_ok=True)
        now = int(time.time())
        mark = f"{now} 42"
        (state / "rc-pending").write_text(mark + "\n")
        owed_rows = f"work w9:p9 s9 {now}\n"
        (state / "rc-owed").write_text(owed_rows)
        listed = f"{mark} work\n"
        (state / "rc-listed").write_text(listed)
        env_file = self.tmp / "failing-printf.sh"
        env_file.write_text('printf() { case "$*" in *w9:p9*|*" work"*) return 1;; esac; '
                            'builtin printf "$@"; }\n')
        self.publish(ONE)
        self.panes(["w1:p1", "claude", "working", "s1", DROPPED])
        result = self.sync("elsewhere", BASH_ENV=str(env_file))
        self.assertEqual((state / "rc-owed").read_text(), owed_rows, result.stdout)
        self.assertIn("could not write", result.stdout)
        self.assertEqual((state / "rc-listed").read_text(), listed, result.stdout)
        # rc-owed did not move, so sd stays unlisted; now let it list.
        (state / "rc-owed").write_text("")
        result = self.sync("elsewhere", BASH_ENV=str(env_file))
        self.assertEqual((state / "rc-listed").read_text(), listed, result.stdout)
        self.assertIn("could not mark sd listed", result.stdout)

    def test_no_switch_sends_nothing(self):
        self.publish(ONE)
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
        self.sync("elsewhere")
        self.assertEqual(self.called("herdr"), [])

    def test_the_latest_remote_control_line_decides(self):
        # Lead review: an old "is active" line above a newer drop kept the
        # pane from its /rc; a newer "is active" means connected.
        self.publish(THREE)
        self.panes(["w1:p1", "claude", "idle", "s1", CONNECTED + "\n" + DROPPED],
                   ["w1:p2", "claude", "idle", "s2", DROPPED + "\n" + CONNECTED],
                   ["w1:p3", "claude", "idle", "s3", "  nothing yet\n" + INPUT])
        self.sync("elsewhere")
        self.assertEqual(self.prompts(), ["herdr agent prompt w1:p1 /rc"])
        # Not dropped yet: it stays owed, and is sent once it drops. An
        # active banner may be the old account's (sd:9427): it stays too.
        self.assertEqual(self.owed(), ["w1:p2", "w1:p3"])

    def test_an_old_active_banner_keeps_the_pane_owed_until_it_drops(self):
        # sd:9427 blocker: the first pass dropped a pane that still showed
        # the old account's active banner; its drop, printed later, was
        # never sent /rc.
        self.publish(THREE)
        self.panes(["w1:p1", "claude", "idle", "s1", CONNECTED])
        result = self.sync("elsewhere")
        self.assertEqual(self.prompts(), [])
        self.assertEqual(self.owed(), ["w1:p1"])
        self.assertNotIn("is connected; not owed", result.stdout)
        self.panes(["w1:p1", "claude", "idle", "s1", CONNECTED + "\n" + DROPPED])
        self.sync("elsewhere")
        self.assertEqual(self.prompts(), ["herdr agent prompt w1:p1 /rc"])
        self.assertEqual(self.owed(), [])

    def test_a_pane_with_typed_input_or_no_input_line_gets_no_keys(self):
        self.publish(THREE)
        typed = DROPPED.replace("\u276f\u00a0", "\u276f half a prompt")
        self.panes(["w1:p1", "claude", "idle", "s1", typed],
                   ["w1:p2", "claude", "idle", "s2", DROP_LINE])
        result = self.sync("elsewhere")
        self.assertEqual(self.prompts(), [])
        self.assertEqual(self.owed(), ["w1:p1", "w1:p2"])
        self.assertIn("reconnect: w1:p1 wait: its input box holds text", result.stdout)
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
        self.sync("elsewhere")
        self.assertEqual(self.prompts(), ["herdr agent prompt w1:p1 /rc"])

    def test_a_dim_suggestion_on_the_input_line_is_no_typed_text(self):
        # Lead live check: an idle pane showed `❯ what next here`, a dim
        # (SGR 2) prompt suggestion, and never got its /rc. Typed text,
        # even in a truecolor run whose parameters hold a 2, still blocks.
        self.publish(THREE)
        dim = DROPPED.replace("\u276f\u00a0", "\u276f\u00a0\x1b[0m\x1b[2mwhat next here\x1b[0m")
        typed = DROPPED.replace("\u276f\u00a0", "\u276f\u00a0\x1b[38;2;200;200;200mfix the\x1b[0m")
        mixed = DROPPED.replace("\u276f\u00a0", "\u276f\u00a0x\x1b[2m suggestion\x1b[22m")
        self.panes(["w1:p1", "claude", "idle", "s1", dim],
                   ["w1:p2", "claude", "idle", "s2", typed],
                   ["w1:p3", "claude", "idle", "s3", mixed])
        self.sync("elsewhere")
        self.assertEqual(self.prompts(), ["herdr agent prompt w1:p1 /rc"])
        self.assertEqual(self.owed(), ["w1:p2", "w1:p3"])

    def test_each_way_a_screen_can_mislead(self):
        # Class pass on the screen read (lead re-review): each pane is one
        # row of the PR's failure table; True means it gets /rc.
        box = lambda *rows: "\n".join([RULE, *rows, RULE, "  [model] | git:(main)"])
        empty = "\u276f\u00a0"
        cases = {
            # Banner text quoted in prose, or both texts on one line.
            "quoted-drop": (f"{ACTIVE_LINE}\n  the job waits for Remote Control disconnected \u2014 x\n"
                            + box(empty), False),
            "quoted-active": (f"{DROP_LINE}\n> quote:   /remote-control is active \u00b7 here\n"
                              + box(empty), True),
            "both-one-line": (f"{ACTIVE_LINE}\n\u23fa Remote Control disconnected \u2014 then "
                              "/remote-control is active \u00b7 x\n" + box(empty), False),
            # A draft whose text is on a continuation line, typed or dim.
            "draft-line-2": (f"{DROP_LINE}\n" + box(empty, "  second line"), False),
            "dim-then-typed": (f"{DROP_LINE}\n" + box(empty + "\x1b[2mtry this\x1b[0m", "  typed"), False),
            # A box of two lines is doubt, even when both are dim.
            "dim-two-lines": (f"{DROP_LINE}\n" + box(empty + "\x1b[2mone", "  two\x1b[22m"), False),
            # SGR forms: colon truecolour and 256 colour hold a 2 that is no
            # dim; 0;2 is dim; a bare reset ends it.
            "colon-truecolour": (f"{DROP_LINE}\n" + box(empty + "\x1b[38:2::9:2:2mtyped\x1b[0m"), False),
            "256-colour": (f"{DROP_LINE}\n" + box(empty + "\x1b[38;5;2mtyped\x1b[0m"), False),
            "dim-combined": (f"{DROP_LINE}\n" + box(empty + "\x1b[0;2msuggestion\x1b[0m"), True),
            "bare-reset": (f"{DROP_LINE}\n" + box(empty + "\x1b[2mhint\x1b[mtyped"), False),
            # The box: a past prompt above it, or no rule below the ❯ line.
            "past-prompt": (f"\u276f an old prompt\n{DROP_LINE}\n" + box(empty), True),
            # Lane review (sd:9427): a turn finished after the drop; its
            # old prompt is history above the live box, not input.
            "finished-turn": (f"{DROP_LINE}\n\u276f\u00a0fix the tests\n"
                              "\u23fa Done.\n" + box(empty), True),
            "no-bottom-rule": (f"{DROP_LINE}\n{RULE}\n{empty}", False),
            "labelled-rule": (f"{DROP_LINE}\n" + box(empty).replace(RULE, RULE + " control \u2500", 1), True),
            # Bundle review: a draft that holds a rule line and a bare ❯
            # line looks like a box of its own; the screen waits.
            "draft-fake-box": (f"{DROP_LINE}\n" + box(empty, RULE, "\u276f"), False),
            "draft-text-fake-box": (f"{DROP_LINE}\n" + box(empty + "a", "  b", RULE, "\u276f\u00a0"), False),
            "two-boxes": (f"{DROP_LINE}\n" + box(empty) + "\n" + box(empty), False),
            # Review on sd:9427: a draft that types a drop banner, a rule
            # and a bare ❯ hid the real prompt above its fake banner, and
            # the real bottom rule closed the fake box. Typed text is
            # plain; Claude's own banner and rules carry colour.
            "draft-fake-banner": (f"{DROP_LINE}\n" + box(
                empty + "first line", "\u23fa Remote Control disconnected \u2014 x",
                TYPED_RULE, "\u276f"), False),
            # Class pass (sd:9427 review round 4): the check proved the
            # box empty by deleting what it read and finding nothing left.
            # Each deletion that can hide a draft is one row; the box is
            # empty only when the line is exactly ❯ and its NBSP, plus at
            # most a dim suggestion.
            # 1. strip(): a whitespace-only draft, ASCII or Unicode.
            "nbsp-draft": (f"{DROP_LINE}\n" + box(empty + "\u00a0"), False),
            "space-draft": (f"{DROP_LINE}\n" + box(empty + " "), False),
            "tab-draft": (f"{DROP_LINE}\n" + box(empty + "\t"), False),
            "ideographic-space-draft": (f"{DROP_LINE}\n" + box(empty + "\u3000"), False),
            # 2. plain(): a control sequence that is no colour, such as a
            #    cursor move that stands for typed blanks.
            "cursor-forward-draft": (f"{DROP_LINE}\n" + box(empty + "\x1b[3C"), False),
            "erase-after-separator": (f"{DROP_LINE}\n" + box(empty + "x\x1b[1D\x1b[K"), False),
            # 3. undimmed(): typed blanks before the dim suggestion.
            "space-then-dim": (f"{DROP_LINE}\n" + box(empty + " \x1b[2mhint\x1b[0m"), False),
            # The live shape of a suggestion (read 2026-11-20) still sends.
            "live-suggestion": (f"{DROP_LINE}\n" + box(empty + "\x1b[0m\x1b[2mwalk me\x1b[0m"), True),
            # Round 5 (sd:9427): an old empty box with a permission dialog
            # drawn below it. The box must be the last thing on screen:
            # below its bottom rule only footer lines, no dialog.
            "dialog-framed-below": (f"{DROP_LINE}\n" + box(empty) + "\n╭──╮\n"
                                    "│ Bash command │\n╰──╯", False),
            "dialog-numbered-below": (f"{DROP_LINE}\n" + box(empty) + "\n Do you want to proceed?\n"
                                      "   1. Yes\n   2. No", False),
            "dialog-esc-below": (f"{DROP_LINE}\n" + box(empty) + "\n Esc to cancel", False),
            "dialog-cursor-below": (f"{DROP_LINE}\n" + box(empty) + "\n ❯ 1. Yes\n   2. No", False),
            "dialog-rule-below": (f"{DROP_LINE}\n" + box(empty) + f"\n{RULE}\n Bash command", False),
            "dialog-typed-rule-below": (f"{DROP_LINE}\n" + box(empty) + f"\n{TYPED_RULE}\n Bash", False),
            "dialog-long-below": (f"{DROP_LINE}\n" + box(empty) + "\n line" * 5, False),
            # Footer lines Claude draws below the box still send.
            "footer-mode": (f"{DROP_LINE}\n" + box(empty) + "\n  ⏵⏵ auto mode on (shift+tab to cycle)",
                            True),
            "footer-bars": (f"{DROP_LINE}\n" + box(empty).replace("|", "│"), True),
            "draft-fake-banner-flush": (f"{DROP_LINE}\n" + box(
                empty + "first line", "\u23fa Remote Control disconnected \u2014 x",
                TYPED_RULE, "\u276f\u00a0"), False),
        }
        self.publish(THREE)
        self.panes(*[[f"w1:{name}", "claude", "idle", name, screen]
                     for name, (screen, _) in cases.items()])
        self.sync("elsewhere")
        sent = {line.split()[3][3:] for line in self.prompts()}
        self.assertEqual(sent, {name for name, (_, gets) in cases.items() if gets})

    def test_a_pane_that_changes_before_the_send_gets_no_keys(self):
        # Operator ruling 2026-11-18: re-read right before the send. Each
        # pane says send on its first read, then changes: text typed, a
        # turn started, another session, a reconnect. None gets keys; each
        # stays owed. The unchanged pane gets /rc.
        self.publish(THREE)
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED, {"screen": DROPPED.replace("\u276f\u00a0", "\u276f\u00a0\u00a0")}],
                   ["w1:p2", "claude", "idle", "s2", DROPPED, {"status": "working"}],
                   ["w1:p3", "claude", "idle", "s3", DROPPED, {"session": "s9"}],
                   ["w1:p4", "claude", "idle", "s4", DROPPED, {"screen": CONNECTED}],
                   ["w1:p5", "claude", "idle", "s5", DROPPED])
        result = self.sync("elsewhere")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.prompts(), ["herdr agent prompt w1:p5 /rc"])
        self.assertEqual(self.owed(), ["w1:p1", "w1:p2", "w1:p3", "w1:p4"])
        self.assertIn("reconnect: w1:p2 changed before the send (w1:p2 s2 working); still owed", result.stdout)
        self.assertIn("reconnect: w1:p1 changed before the send (wait: its input box holds text); still owed",
                      result.stdout)

    def test_a_session_that_changes_during_the_last_read_gets_no_keys(self):
        # Review on sd:9427: the last session check ran before a screen
        # read of up to 10 s. A new session that took the pane during
        # that read showed its own drop and empty box, and got /rc.
        self.publish(THREE)
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED, {"session": "s9", "reads": 2}])
        result = self.sync("elsewhere")
        self.assertEqual(self.prompts(), [])
        self.assertEqual(self.owed(), ["w1:p1"])
        self.assertIn("reconnect: w1:p1 changed before the send (w1:p1 s9 idle); still owed",
                      result.stdout)

    def test_a_draft_typed_during_the_last_get_gets_no_keys(self):
        # Review on sd:9427: typing during the final `agent get` left
        # session and status unchanged; within the gap, /rc joined the
        # draft. The screen is read again after the get and must match.
        self.publish(THREE)
        draft = DROPPED.replace("\u276f\u00a0", "\u276f\u00a0hello")
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED, {"screen": draft, "gets": 1}])
        result = self.sync("elsewhere")
        self.assertEqual(self.prompts(), [])
        self.assertEqual(self.owed(), ["w1:p1"])
        self.assertIn("reconnect: w1:p1 changed before the send", result.stdout)

    def test_a_slow_last_check_sends_no_keys(self):
        # Class pass (sd:9427 review round 4): each check before the keys
        # goes stale while a later herdr call runs. Typing during a slow
        # `agent get` left session and status unchanged, and /rc joined
        # the draft. No order closes it: the whole recheck, from the
        # screen read to the end of the get, must fit CSWAP_RC_GAP_MS.
        # Rows: a slow get (screen stale), a slow read (its snapshot and
        # the get both late): each call is cut at the gap. Two calls each
        # under the gap whose sum is over it: the timer refuses.
        rows = [({"FAKE_HERDR_GET_SLOW": "1.5"}, "1000", "herdr agent get w1:p1 failed"),
                ({"FAKE_HERDR_SLOW": "1.5"}, "1000", "herdr agent read w1:p1 failed"),
                ({"FAKE_HERDR_SLOW": "0.4", "FAKE_HERDR_GET_SLOW": "0.4"}, "600",
                 r"w1:p1 took \d+ ms to check, over 600 ms")]
        for slow, gap, why in rows:
            with self.subTest(slow=slow):
                self.publish(ONE if self.active() == 3 else THREE)
                self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
                result = self.sync("elsewhere", CSWAP_RC_GAP_MS=gap, **slow)
                self.assertEqual(self.prompts(), [])
                self.assertEqual(self.owed(), ["w1:p1"])
                self.assertRegex(result.stdout, rf"reconnect: {why}.*; still owed")
                (self.script.parent / "state/rc-owed").unlink()

    def test_a_slow_send_counts_in_the_gap(self):
        # Review on sd:9427: the gap ended before `agent prompt`, which
        # may take 10 s; a draft typed meanwhile took the /rc. The send
        # gets the gap's time left as its bound, and the whole
        # span from the last screen read to the send's end must fit it.
        # Review on sd:9427, next round: a send that ran over the gap left
        # the pane owed, and its drop banner, not yet redrawn, took a
        # second /rc. A /rc that went out, or may have (herdr: a timeout
        # does not prove no delivery), keeps its sent mark.
        # Rows: herdr times out; herdr acknowledges past the gap.
        rows = [({"FAKE_HERDR_PROMPT_SLOW": "1.5"}, "1000"),
                ({"FAKE_HERDR_PROMPT_LATE": "1.2"}, "1000")]
        for slow, gap in rows:
            with self.subTest(slow=slow):
                self.publish(ONE if self.active() == 3 else THREE)
                self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
                result = self.sync("elsewhere", CSWAP_RC_GAP_MS=gap, **slow)
                self.assertEqual(self.owed(), [], result.stdout)
                self.assertRegex(result.stdout, rf"reconnect: /rc to w1:p1 .*over {gap} ms; not sent again")
                self.sync("elsewhere")
                self.assertEqual(self.prompts(), [])
                (self.script.parent / "state/rc-owed").unlink()

    def test_a_budget_spent_before_the_send_keeps_the_pane_owed(self):
        # sd:9427 triage, HIGH: the budget ran out after the last screen
        # read; herdr's prompt never ran, yet the pane was marked sent
        # and printed "not sent again". The stub clock jumps past the
        # budget at the third read, the last one before the keys.
        clock = self.tmp / "clock"
        executable(self.stubs / "date", '#!/bin/sh\n'
                   'if [ "$1" = +%s ] && [ -f "$FAKE_CLOCK" ]; then\n'
                   '  echo $(( $(/bin/date +%s) + $(cat "$FAKE_CLOCK") )); exit 0\nfi\n'
                   'exec /bin/date "$@"\n')
        self.publish(THREE)
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
        result = self.sync("elsewhere", FAKE_CLOCK=str(clock), FAKE_CLOCK_JUMP_AT="3",
                           FAKE_CLOCK_JUMP="61")
        self.assertEqual(self.prompts(), [])
        self.assertEqual(self.owed(), ["w1:p1"], result.stdout)
        self.assertIn("reconnect: the 60s budget is spent before the /rc to w1:p1; still owed",
                      result.stdout)
        self.assertNotIn("not sent again", result.stdout)
        self.sync("elsewhere", FAKE_CLOCK=str(clock))
        self.assertEqual(self.prompts(), ["herdr agent prompt w1:p1 /rc"])

    def test_a_failed_herdr_call_is_named_as_the_cause(self):
        # sd:9427 triage: a read that failed was logged as the pane having
        # changed, or not at all. Rows: the pass's read, the send's two
        # reads, the get. Each keeps the pane owed and names the call.
        rows = [({"FAKE_HERDR_READ_FAIL": "1"}, "read"), ({"FAKE_HERDR_READ_FAIL": "2"}, "read"),
                ({"FAKE_HERDR_READ_FAIL": "3"}, "read"), ({"FAKE_HERDR_GET_FAIL": "1"}, "get")]
        for fail, call in rows:
            with self.subTest(fail=fail):
                self.publish(ONE if self.active() == 3 else THREE)
                self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
                pathlib.Path(self.env["FAKE_HERDR"] + ".read").unlink(missing_ok=True)
                result = self.sync("elsewhere", **fail)
                self.assertEqual(self.prompts(), [])
                self.assertEqual(self.owed(), ["w1:p1"])
                self.assertIn(f"reconnect: herdr agent {call} w1:p1 failed (exit 1); still owed",
                              result.stdout)
                self.assertNotIn("changed before the send", result.stdout)
                (self.script.parent / "state/rc-owed").unlink()

    def test_a_herdr_down_with_panes_owed_is_one_skip_line(self):
        # sd:9427 triage: a failed list at a new switch was asked again
        # for the panes already owed: two calls, two skip lines.
        old = int(time.time()) - 5
        self.owe(f"sd w1:p1 s1 {old}\n", old)
        self.publish(THREE)
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
        result = self.sync("elsewhere", FAKE_HERDR_DOWN="1")
        self.assertEqual(len(self.called("herdr agent list")), 1)
        self.assertEqual(result.stdout.count("reconnect: skipped"), 1, result.stdout)

    def test_the_send_is_cut_at_the_gap_not_the_next_second(self):
        # sd:9427 review: the send's bound was the gap's time left
        # rounded up to a whole second, so with 600 ms of a 1000 ms gap
        # spent, a delivery 500 ms later still reached the pane, past the
        # gap, into whatever was typed meanwhile. The bound is the time
        # left to the millisecond.
        delivered = self.tmp / "delivered"
        self.publish(THREE)
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
        result = self.sync("elsewhere", CSWAP_RC_GAP_MS="1000", FAKE_HERDR_GET_SLOW="0.6",
                           FAKE_HERDR_PROMPT_SLOW="0.5", FAKE_DELIVERED=str(delivered))
        self.assertFalse(delivered.exists(), result.stdout)

    def test_a_herdr_down_at_the_switch_is_asked_again(self):
        # Fold-in: a list that failed at the switch owed nothing, and the
        # next sync (no switch) never asked again.
        self.publish(THREE)
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
        self.sync("elsewhere", FAKE_HERDR_DOWN="1")
        self.assertEqual(self.prompts(), [])
        self.sync("elsewhere")
        self.assertEqual(self.prompts(), ["herdr agent prompt w1:p1 /rc"])
        # The mark stays for other herdr sessions, but sd listed once:
        # a later sync with nothing owed asks herdr nothing.
        self.sync("elsewhere")
        self.assertEqual(self.called("herdr"), [])

    def test_a_list_that_is_not_the_agent_list_is_a_parse_failure(self):
        self.publish(THREE)
        for answer in ("null", '{"result": null}', '{"result": {"agents": [null]}}',
                       '{"result": {"agents": [{"agent": "claude", "pane_id": "w1:p1", "agent_session": 7}]}}'):
            executable(self.stubs / "herdr", f"#!/bin/sh\necho '{answer}'\n")
            result = self.run_verb("sync", FAKE_FENCE="elsewhere")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("gave no agent list this can parse", result.stdout, answer)
            self.assertNotIn("could not write", result.stdout, answer)
            self.assertTrue((self.script.parent / "state/rc-pending").exists(), answer)

    def test_a_pane_with_no_session_id_is_never_owed_or_sent(self):
        # Review on sd:9427: a missing or false session read as "-" in
        # both list and get, so a new session in the same pane passed
        # the recheck and got /rc.
        self.publish(THREE)
        self.panes(["w1:p1", "claude", "idle", None, DROPPED],
                   ["w1:p2", "claude", "idle", "", DROPPED])
        self.sync("elsewhere")
        self.assertEqual(self.prompts(), [])
        self.assertEqual(self.owed(), [])

    def test_a_pane_whose_session_herdr_misses_once_stays_owed(self):
        # Review on sd:9427: herdr naming no session for a pane read as
        # the pane gone; its row went, and rc-listed kept the switch from
        # listing it again. A pane that exists keeps its row, and an
        # unknown session at the listing lists again next sync.
        self.publish(THREE)
        self.panes(["w1:p1", "claude", "working", "s1", DROPPED])
        self.sync("elsewhere")
        self.panes(["w1:p1", "claude", "idle", None, DROPPED])
        result = self.sync("elsewhere")
        self.assertEqual(self.owed(), ["w1:p1"], result.stdout)
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
        self.sync("elsewhere")
        self.assertEqual(self.prompts(), ["herdr agent prompt w1:p1 /rc"])

    def test_a_pane_with_no_session_at_the_listing_is_listed_again(self):
        self.publish(THREE)
        self.panes(["w1:p1", "claude", "idle", None, DROPPED],
                   ["w1:p2", "claude", "working", "s2", DROPPED])
        result = self.sync("elsewhere")
        self.assertEqual(self.owed(), ["w1:p2"])
        self.assertIn("reconnect: w1:p1 names no session yet; sd lists again next sync", result.stdout)
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED],
                   ["w1:p2", "claude", "working", "s2", DROPPED])
        self.sync("elsewhere")
        self.assertEqual(self.prompts(), ["herdr agent prompt w1:p1 /rc"])
        self.assertEqual(self.owed(), ["w1:p2"])

    def test_a_retried_listing_owes_no_sent_pane_again(self):
        # Review on sd:9427: a listing retried for a pane with no session
        # id rebuilt every row of the switch, and a pane already sent
        # /rc, its drop banner still on screen, got a second one.
        self.publish(THREE)
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED],
                   ["w1:p2", "claude", "working", None, DROPPED])
        self.sync("elsewhere")
        self.assertEqual(self.prompts(), ["herdr agent prompt w1:p1 /rc"])
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED],
                   ["w1:p2", "claude", "idle", "s2", DROPPED])
        result = self.sync("elsewhere")
        self.assertEqual(self.prompts(), ["herdr agent prompt w1:p2 /rc"], result.stdout)
        self.assertEqual(self.owed(), [])
        # The listing is complete now: a later sync sends nothing.
        self.sync("elsewhere")
        self.assertEqual(self.prompts(), [])

    def test_a_failed_owed_write_leaves_its_panes_to_the_next_listing(self):
        # Review on sd:9427: rc-listed recorded each pane before rc-owed
        # was written; a failed rc-owed write left the panes recorded,
        # and the next sync owed none of them.
        failing = self.tmp / "failing-printf.sh"
        failing.write_text('printf() { case "$*" in *"w1:p1 s1"*) return 1;; esac; builtin printf "$@"; }\n')
        self.publish(THREE)
        self.panes(["w1:p1", "claude", "working", "s1", DROPPED])
        result = self.sync("elsewhere", BASH_ENV=str(failing))
        self.assertIn("could not write", result.stdout)
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
        result = self.sync("elsewhere")
        self.assertEqual(self.prompts(), ["herdr agent prompt w1:p1 /rc"], result.stdout)

    def test_a_failed_write_after_a_send_owes_the_pane_no_second_rc(self):
        # Class pass: the pass's last write after a send failed, the
        # sent row stayed owed, and its drop banner took a second /rc.
        state = self.script.parent / "state"
        state.mkdir(exist_ok=True)
        now = int(time.time())
        (state / "rc-pending").write_text(f"{now} 42\n")
        (state / "rc-listed").write_text(f"{now} 42 sd\n")
        (state / "rc-owed").write_text(f"sd w1:p1 s1 {now}\nsd w1:p2 s2 {now}\n")
        failing = self.tmp / "failing-printf.sh"
        failing.write_text('printf() { case "$*" in *w1:p2*) return 1;; esac; builtin printf "$@"; }\n')
        self.publish(ONE)  # no switch: the seeded mark is the one listed
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED],
                   ["w1:p2", "claude", "working", "s2", DROPPED])
        result = self.sync("elsewhere", BASH_ENV=str(failing))
        self.assertEqual(self.prompts(), ["herdr agent prompt w1:p1 /rc"], result.stdout)
        self.assertIn("could not update", result.stdout)
        result = self.sync("elsewhere")
        self.assertEqual(self.prompts(), [], result.stdout)

    def test_a_pane_is_marked_sent_only_after_the_last_checks(self):
        # sd:9427 review: the sent mark was written before the send's
        # checks. A sync stopped during them, or a check that refused
        # and a last write that failed, left a pane that never had /rc
        # marked sent. The mark is written right before the prompt.
        snap = self.tmp / "owed-at-get"
        self.publish(THREE)
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED, {"status": "working", "gets": 1}])
        failing = self.tmp / "failing-printf.sh"
        # Only the pass's last write ("%s", no newline) fails.
        failing.write_text('printf() { [ "$1" = %s ] && case "$2" in *w1:p1*) return 1;; esac;'
                           ' builtin printf "$@"; }\n')
        result = self.sync("elsewhere", FAKE_OWED_AT_GET=str(snap), BASH_ENV=str(failing),
                           FAKE_STATE_DIR=str(self.script.parent / "state"))
        self.assertEqual(self.prompts(), [])
        self.assertNotIn(" sent", snap.read_text())
        self.assertIn("could not update", result.stdout)
        self.assertEqual(self.owed(), ["w1:p1"], result.stdout)
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
        self.sync("elsewhere")
        self.assertEqual(self.prompts(), ["herdr agent prompt w1:p1 /rc"])

    def test_a_slow_sent_mark_sends_no_keys(self):
        # sd:9427 review: the sent mark is written inside the gap, and a
        # slow write used it up; /rc still went with the earlier
        # timeout, into whatever the pane drew meanwhile. The gap is
        # checked again after the mark; past it, the pane stays owed.
        slow = self.tmp / "slow-mark.sh"
        slow.write_text('awk() { case "${2-}" in row=*) sleep 1.2;; esac; command awk "$@"; }\n')
        self.publish(THREE)
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
        result = self.sync("elsewhere", BASH_ENV=str(slow), CSWAP_RC_GAP_MS="1000")
        self.assertEqual(self.prompts(), [], result.stdout)
        self.assertEqual(self.owed(), ["w1:p1"], result.stdout)
        self.assertRegex(result.stdout, r"reconnect: w1:p1 took \d+ ms to check and mark, over 1000 ms; still owed")
        self.sync("elsewhere")
        self.assertEqual(self.prompts(), ["herdr agent prompt w1:p1 /rc"])

    def test_a_switch_during_the_sent_mark_stops_the_send(self):
        # sd:9427 review: a switch runs outside the reconnect lock, and
        # one that landed while the sent mark was written still got the
        # old row's /rc; the newer switch then owes the pane again. The
        # switch is read again after the mark.
        during = self.tmp / "switch-at-mark.sh"
        during.write_text('awk() { local s _; case "${2-}" in row=*)\n'
                          '  read -r s _ <"$RC_PENDING"; builtin printf "%s 4343\\n" $((s + 1)) >"$RC_PENDING";;\n'
                          'esac; command awk "$@"; }\n')
        self.publish(THREE)
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
        result = self.sync("elsewhere", BASH_ENV=str(during))
        self.assertEqual(self.prompts(), [], result.stdout)
        self.assertIn("reconnect: w1:p1 changed before the send (a newer switch)", result.stdout)

    def test_a_session_herdr_stops_naming_gets_no_keys(self):
        self.publish(THREE)
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED, {"session": None}])
        result = self.sync("elsewhere")
        self.assertEqual(self.prompts(), [])
        self.assertEqual(self.owed(), ["w1:p1"])
        self.assertIn("w1:p1 changed before the send (no claude pane in the answer)", result.stdout)

    def test_a_gone_pane_or_new_session_is_not_owed(self):
        self.publish(THREE)
        self.panes(["w1:p1", "claude", "working", "s1", DROPPED],
                   ["w1:p2", "claude", "working", "s2", DROPPED])
        self.sync("elsewhere")
        self.panes(["w1:p2", "claude", "idle", "s9", DROPPED])
        self.sync("elsewhere")
        self.assertEqual(self.prompts(), [])
        self.assertEqual(self.owed(), [])

    def test_the_hub_s_auto_switch_sends_rc_even_when_its_publish_fails(self):
        # Step: auto switched, then the publish failed and the run exited 1.
        # Recovery: the owed list was written first; the next run sends.
        self.publish(ONE)
        self.panes(["w1:p1", "claude", "working", "s1", DROPPED])
        self.sync("holder", code=1, FAKE_AUTO_TO="2", FAKE_AUTO_RC="0", FAKE_SET_FAIL="1")
        self.assertTrue((self.script.parent / "state/rc-pending").exists())
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
        self.sync("holder")
        self.assertEqual(self.prompts(), ["herdr agent prompt w1:p1 /rc"])

    def test_a_long_owed_pane_is_dropped_and_an_unreadable_list_keeps_it(self):
        self.publish(THREE)
        self.panes(["w1:p1", "claude", "working", "s1", DROPPED])
        self.sync("elsewhere")
        self.owe("sd w1:p1 s1 1000\n", 1000)
        self.env["FAKE_HERDR"] = str(self.tmp / "missing.json")
        executable(self.stubs / "herdr", "#!/bin/sh\necho '{not json'\n")
        result = self.sync("elsewhere")
        self.assertIn("gave no agent list this can parse", result.stdout)
        self.assertEqual(self.owed(), ["w1:p1"])
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
        result = self.sync("elsewhere")
        self.assertIn("reconnect: w1:p1 was owed for a switch that is no longer current", result.stdout)
        self.assertEqual(self.prompts(), [])
        self.assertEqual(self.owed(), [])

    def test_cron_s_path_without_herdr_finds_it_in_a_fallback_dir(self):
        self.publish(THREE)
        brew = self.tmp / "brew-bin"
        brew.mkdir()
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED], where=brew)
        self.sync("elsewhere", CSWAP_HERDR_DIRS=f"{self.tmp}/none {brew}")
        self.assertEqual(self.prompts(), ["herdr agent prompt w1:p1 /rc"])

    def test_no_herdr_is_one_skip_line_and_the_switch_stands(self):
        self.publish(THREE)
        result = self.sync("elsewhere")
        self.assertEqual(self.active(), 3)
        self.assertEqual([l for l in result.stdout.splitlines() if l.startswith("reconnect")],
                         ["reconnect: skipped: no herdr on PATH"])
        # Only a switch writes the mark: a sync with no herdr keeps it,
        # as a switch may have rewritten it meanwhile (sd:9427 review).
        self.assertTrue((self.script.parent / "state/rc-pending").exists())

    def test_a_herdr_that_does_not_answer_is_one_skip_line(self):
        self.publish(THREE)
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
        result = self.sync("elsewhere", FAKE_HERDR_DOWN="1")
        self.assertEqual(self.prompts(), [])
        self.assertIn("reconnect: skipped: herdr agent list did not answer", result.stdout)
        self.assertEqual(result.stdout.count("reconnect:"), 1)


class TheRetiredAgent(Machine):
    def test_sync_boots_out_a_loaded_cswap_auto_agent_and_removes_its_plist(self):
        # Older state: the always-on `cswap auto` loop would rotate on its own.
        self.agent.parent.mkdir(parents=True)
        self.agent.write_text("<plist/>\n")
        self.publish(TWO)
        self.sync("elsewhere", FAKE_LOADED="1")
        uid = os.getuid()
        self.assertEqual(self.called("launchctl bootout"),
                         [f"launchctl bootout gui/{uid}/local.system-tools.cswap-auto"])
        self.assertFalse(self.agent.exists())
        self.assertEqual(self.active(), 2)

    def test_sync_removes_a_plist_launchd_no_longer_holds(self):
        # Steps: a run stopped after the bootout left the plist, which
        # launchd would load again at the next login.
        self.agent.parent.mkdir(parents=True)
        self.agent.write_text("<plist/>\n")
        self.publish(ONE)
        self.sync("elsewhere")
        self.assertFalse(self.agent.exists())
        self.assertEqual(self.called("launchctl bootout"), [])

    def test_a_failed_bootout_fails_before_any_switch(self):
        self.publish(TWO)
        self.sync("elsewhere", code=1, FAKE_LOADED="1", FAKE_BOOTOUT_RC="5")
        self.assertEqual(self.called("cswap"), [])
        self.assertEqual(self.active(), 1)


class Status(Machine):
    def status(self, fence, **extra):
        return self.run_verb("status", FAKE_FENCE=fence, **extra)

    def test_a_satellite_on_the_hubs_account_is_healthy(self):
        self.publish(ONE)
        result = self.status("elsewhere")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("following the hub, same account (1)", result.stdout)

    def test_a_satellite_on_another_account_is_broken(self):
        self.publish(THREE)
        result = self.status("elsewhere")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("account 1 here, the hub is on account 3", result.stdout)

    def test_a_hub_account_unknown_here_is_broken(self):
        self.publish(STRANGER)
        result = self.status("elsewhere")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("no account here", result.stdout)

    def test_an_unreadable_hub_is_broken(self):
        result = self.status("elsewhere", FAKE_SD_DOWN="1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_the_hub_that_published_its_account_is_healthy(self):
        self.publish(ONE)
        result = self.status("holder")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("leads", result.stdout)
        self.assertEqual(self.called("cswap auto"), [])

    def test_the_hub_whose_row_differs_is_broken(self):
        self.publish(TWO)
        result = self.status("holder")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_no_published_account_is_nothing_to_check(self):
        result = self.status("elsewhere")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)

    def test_no_cswap_is_nothing_to_check(self):
        (self.home / ".local/bin/cswap").unlink()
        result = self.status("elsewhere")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)

    def test_a_loaded_cswap_auto_agent_is_broken(self):
        self.publish(ONE)
        result = self.status("elsewhere", FAKE_LOADED="1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("retired", result.stdout)
        self.assertEqual(self.called("launchctl bootout"), [])


class Verbs(Machine):
    def test_help_declares_the_codes_to_the_health_check(self):
        result = self.run_verb("help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("local-health-check reads", result.stdout)
        self.assertIn("sync", result.stdout)

    def test_sync_without_cswap_fails_and_asks_nothing(self):
        # Guard: the deleted `run` refused a missing cswap with exit 78.
        (self.home / ".local/bin/cswap").unlink()
        self.publish(TWO)
        result = self.sync("elsewhere", code=1)
        self.assertIn("cswap not executable", result.stderr)
        self.assertEqual(self.called("sd"), [])

    def test_the_agent_verbs_are_gone(self):
        for verb in ("run", "start", "stop"):
            result = self.run_verb(verb)
            self.assertEqual(result.returncode, 1, verb)
            self.assertIn("usage:", result.stderr)
        self.assertFalse((FOLDER / "cswap-auto.plist.template").exists())


class TheJob(unittest.TestCase):
    def test_the_catalogue_runs_sync_every_minute(self):
        job = (FOLDER.parent / "local-cron-jobs/examples/cswap-sync.job").read_text()
        self.assertIn('JOB_SCHEDULE="* * * * *"', job)
        self.assertIn("local-cswap/cswap.sh\\\" sync", job)
        self.assertIn("JOB_TIMEOUT=", job)


if __name__ == "__main__":
    unittest.main()
