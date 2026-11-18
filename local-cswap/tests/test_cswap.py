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
# screen] rows. `agent list` fails with FAKE_HERDR_DOWN (no server); `agent
# read` prints the pane's screen; `agent prompt` is logged and sends nothing.
HERDR = r"""#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_CALLS"], "a") as log:
    log.write("herdr " + " ".join(sys.argv[1:]) + "\n")
panes = json.load(open(os.environ["FAKE_HERDR"]))
args = sys.argv[1:]
if args[:2] == ["agent", "list"]:
    if os.environ.get("FAKE_HERDR_DOWN"):
        print("herdr: server not running", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"id": "cli:agent:list", "result": {"agents": [
        {"agent": a, "agent_status": st, "pane_id": p,
         "agent_session": {"agent": a, "kind": "id", "value": se}}
        for p, a, st, se, _ in panes]}}))
elif args[:2] == ["agent", "read"]:
    print(next(sc for p, *_, sc in panes if p == args[2]))
elif args[:2] == ["agent", "prompt"]:
    pass
else:
    sys.exit(64)
"""

# A pane's screen: a Remote Control line, then the input box. INPUT is the
# empty input line; the screen keeps older lines above the newest.
RULE = "\u2500" * 20
INPUT = f"{RULE}\n\u276f\u00a0\n{RULE}\n  [model] | git:(main)"
DROPPED = f"  Remote Control disconnected\n{INPUT}"
CONNECTED = f"  /remote-control is active \u00b7 Continue here\n{INPUT}"


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
        return subprocess.run(["bash", str(self.script), *args], env={**self.env, **extra},
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

    def panes(self, *rows, where=None):
        path = self.tmp / "herdr.json"
        path.write_text(json.dumps(rows))
        self.env["FAKE_HERDR"] = str(path)
        executable((where or self.stubs) / "herdr", HERDR)

    def prompts(self):
        return self.called("herdr agent prompt")

    def owed(self):
        path = self.script.parent / "state/rc-owed"
        return [line.split()[0] for line in path.read_text().splitlines()] if path.exists() else []

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
        # Not dropped yet: it stays owed, and is sent once it drops.
        self.assertEqual(self.owed(), ["w1:p3"])

    def test_a_pane_with_typed_input_or_no_input_line_gets_no_keys(self):
        self.publish(THREE)
        typed = DROPPED.replace("\u276f\u00a0", "\u276f half a prompt")
        self.panes(["w1:p1", "claude", "idle", "s1", typed],
                   ["w1:p2", "claude", "idle", "s2", "  Remote Control disconnected"])
        result = self.sync("elsewhere")
        self.assertEqual(self.prompts(), [])
        self.assertEqual(self.owed(), ["w1:p1", "w1:p2"])
        self.assertIn("reconnect: w1:p1 wait: its input line holds text", result.stdout)
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
        self.sync("elsewhere")
        self.assertEqual(self.prompts(), ["herdr agent prompt w1:p1 /rc"])

    def test_a_herdr_down_at_the_switch_is_asked_again(self):
        # Fold-in: a list that failed at the switch owed nothing, and the
        # next sync (no switch) never asked again.
        self.publish(THREE)
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
        self.sync("elsewhere", FAKE_HERDR_DOWN="1")
        self.assertEqual(self.prompts(), [])
        self.sync("elsewhere")
        self.assertEqual(self.prompts(), ["herdr agent prompt w1:p1 /rc"])
        self.assertFalse((self.script.parent / "state/rc-pending").exists())

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
        owed = self.script.parent / "state/rc-owed"
        owed.write_text("w1:p1 s1 1000\n")
        self.env["FAKE_HERDR"] = str(self.tmp / "missing.json")
        executable(self.stubs / "herdr", "#!/bin/sh\necho '{not json'\n")
        result = self.sync("elsewhere")
        self.assertIn("gave no agent list this can parse", result.stdout)
        self.assertEqual(self.owed(), ["w1:p1"])
        self.panes(["w1:p1", "claude", "idle", "s1", DROPPED])
        result = self.sync("elsewhere")
        self.assertIn("reconnect: w1:p1 stayed busy", result.stdout)
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
        # Nothing to owe on a machine with no herdr: no mark stays behind.
        self.assertFalse((self.script.parent / "state/rc-pending").exists())

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
