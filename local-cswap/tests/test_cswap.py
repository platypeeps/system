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
SD = r"""#!/bin/sh
echo "sd $*" >> "$FAKE_CALLS"
[ "$1 $3" = "config sys.cswap_account" ] || exit 64
if [ -n "${FAKE_SD_DOWN:-}" ]; then
  echo "sd: the workflow database did not open: OSError: no route to hub" >&2
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
                    "FAKE_CSWAP_STATE": str(self.state), "FAKE_ROW": str(self.row)}
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
