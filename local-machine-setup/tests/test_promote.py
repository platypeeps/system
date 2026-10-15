"""`machine-setup.sh promote` and the hub role as a state (hub failover slice 4, sd:4012).

docs/work/2026-10-10-hub-failover/design.md, "The verb and its steps" and its
failure table. Every case runs the script against a fixture home with stubs:
`sd-db` (fence and restore --newest), `sd`, `tailscale`, `launchctl` and the
step-9 session never reach this Mac, its Tailscale node or a hub. Each step's
case stops the run there and checks the next run finishes it or undoes it.
"""

import datetime
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import unittest

from tests import fixture_config
from tests.test_backbone import PACK, ROOT, ROUTE_PRESENT, Fixture, write_stub

PREFIX = fixture_config.LABEL_PREFIX
HUB = "hub.example.test"


def utc_today():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
ROLE_AGENTS = [f"{PREFIX}.sd-dashboard", f"{PREFIX}.sd-serve", f"{PREFIX}.task-actions"]
ROLE_JOBS = ["sd-db-backup", "sd-db-backup-hourly", "offsite-verify", "shadow-sync-nightly"]

# The pack's bin/sd-db: `fence` prints the state in $STUB_FENCE (a file, so
# `tailscale set` can move it), `restore --newest` installs a database named
# after the first root's newest folder, or fails with STUB_RESTORE_RC.
SD_DB_STUB = r'''
echo "$*" >> "$STUB_LOG"
case "$1" in
  fence)
    # STUB_FENCE_FAIL_FROM: from that call on, the probe fails with no answer.
    n=$(( $(cat "$STUB_LOG.fences" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$STUB_LOG.fences"
    if [ -n "$STUB_FENCE_FAIL_FROM" ] && [ "$n" -ge "$STUB_FENCE_FAIL_FROM" ]; then exit 3; fi
    state=$(cat "$STUB_FENCE")
    echo "sd-db: fence: $state: fixture"
    [ -z "$STUB_FENCE_RC" ] || exit "$STUB_FENCE_RC"
    case "$state" in holder|off) exit 0 ;; esac
    exit 1 ;;
  restore)
    if [ -n "$STUB_RESTORE_RC" ]; then echo "sd-db: refused: fixture" >&2; exit "$STUB_RESTORE_RC"; fi
    # As the pack reports a root it cannot list, and goes on with the others.
    for r in "$@"; do case $r in restore|--newest) ;; *) [ -d "$r" ] || echo "sd-db: refused $r: [Errno 2] No such file or directory" ;; esac; done
    newest=$(ls "$3" | sort | tail -1)
    mkdir -p "$HOME/.local/share/sd"
    # The row `restore` writes, which step 9 compares through the session; the pack builds the file.
    "$STUB_REAL_PYTHON" -I -c 'import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from sd_db import database, writes
from sd_db.migrate import initialise
path = Path(sys.argv[2])
if not path.exists():
    initialise(path)
c = database.open_local(path, write=True, create=False)
writes.record_state(c, "restore", key=sys.argv[3], body={"restored_at": "fixture"})
c.commit()
c.close()' "$STUB_PYTHONPATH" "$HOME/.local/share/sd/sd.db" "$newest"
    echo "sd-db: restored $3/$newest (checkpoint 2026-10-14T08:50:00+00:00) to $HOME/.local/share/sd"
    echo "sd-db: released 1 lease(s) abandoned at promotion: run-7"
    exit 0 ;;
  repo)
    # STUB_REPO_DEAD: the registry read goes to a hub that is gone.
    if [ -n "$STUB_REPO_DEAD" ]; then echo "sd-db: hub unavailable: connection refused" >&2; exit 1; fi
    printf '%s' "$STUB_REPOS"; exit 0 ;;
  # The drill's loopback server: the pack's own.
  serve) shift; exec "$STUB_REAL_PYTHON" -I -c 'import sys
sys.path.insert(0, sys.argv[1])
from sd_db import serve
sys.exit(serve.main(sys.argv[2:]))' "$STUB_PYTHONPATH" "$@" ;;
  backup)
    if [ -n "$STUB_BACKUP_RC" ]; then echo "sd-db backup: fixture failure" >&2; exit "$STUB_BACKUP_RC"; fi
    name=${STUB_BACKUP_NAME:-2026-10-14.6}
    mkdir -p "$3/$name"
    echo '{"run_id": "run-handback"}' > "$3/$name/backup-manifest.json"
    echo "sd-db backup: $3/$name — 3 row(s) across 1 table(s), restored and compared; fixture; row prune skipped (--no-row-prune)"
    exit 0 ;;
esac
exit 2
'''

# tailscale: `status --json` lists $STUB_PEER as a peer when set; `set
# --hostname=` logs and makes the fence read holder; `serve status` as the
# backbone's stub.
TAILSCALE_STUB = r'''
echo "tailscale $*" >> "$STUB_LOG"
case "$1 $2" in
  "serve status") cat "$STUB_SERVE_STATUS"; exit 0 ;;
  "status --json")
    if [ -n "$STUB_PEER" ]; then
      printf '{"Peer": {"k": {"HostName": "%s", "DNSName": "%s.tailnet.example.test."}}}\n' "$STUB_PEER" "$STUB_PEER"
    else
      echo '{"Peer": {}}'
    fi
    exit 0 ;;
esac
case "$1" in set) echo holder > "$STUB_FENCE" ;; esac
exit 0
'''

# sd: the satellite's own `config get sd.hub_name` and the drain's gate read.
SD_STUB = r'''
case "$*" in
  "config get sd.hub_name") [ -n "$STUB_HUB_NAME" ] || exit 1; echo "$STUB_HUB_NAME" ;;
  "config get sd.lane_root")
    # STUB_SD_DEAD: sd reaches the dead hub, as a satellite's sd may.
    if [ -n "$STUB_SD_DEAD" ]; then echo "sd: hub unavailable: connection refused" >&2; exit 1; fi
    echo "sd.lane_root is not set" >&2; exit 1 ;;
  "gate status --json")
    # STUB_GATE_COUNT: a gate from before holds a slot for the first two reads.
    n=$(cat "$STUB_GATE_COUNT" 2>/dev/null || echo 2); echo $((n + 1)) > "${STUB_GATE_COUNT:-/dev/null}"
    # STUB_GATE_QUEUED, a pid: it waits at the first read, then is listed
    # nowhere, as between its ticket and its holder record.
    if [ -n "$STUB_GATE_QUEUED" ] && [ "$n" = 0 ]; then echo "{\"holders\": [], \"waiters\": [{\"place\": 1, \"pid\": $STUB_GATE_QUEUED}]}"
    elif [ -n "$STUB_GATE_QUEUED" ]; then echo '{"holders": [], "waiters": []}'
    elif [ "$n" -lt 2 ]; then echo '{"holders": [{"slot": 1, "pid": 7, "since": "t0"}], "waiters": []}'
    else echo '{"holders": [], "waiters": []}'; fi ;;
  *) exit 2 ;;
esac
'''

# The step-9 session: the pack's interpreter, asked to connect. Asked for the
# satellite check, it prints STUB_SATELLITE_LINE, the proof line by default.
SATELLITE_CASE = """case "$5" in sd_db.satellite)
  line=${STUB_SATELLITE_LINE-ok}
  [ "$line" != ok ] || line="  ok      sd_db fixture build 1 matches the hub's"
  echo "$line"; exit 0 ;;
esac
"""
PROVE_STUB = SATELLITE_CASE + 'shift 3\necho "prove $*" >> "$STUB_LOG"\nexit "${STUB_PROVE_RC:-0}"\n'

# git: the hub-pin tag and the two checkouts' heads; anything else is the real git.
GIT_STUB = r'''
case "$*" in
  *"rev-parse -q --verify refs/tags/hub-pin^{commit}") [ -n "$STUB_PIN" ] || exit 1; echo "$STUB_PIN" ;;
  *"tag -l --format=%(contents) hub-pin") printf 'hub-pin\n\npack=%s\n' "$STUB_PIN_PACK" ;;
  "-C $SD_PACK_ROOT rev-parse HEAD") echo "$STUB_PACK_HEAD" ;;
  *"rev-parse HEAD") echo "$STUB_SYSTEM_HEAD" ;;
  *) exec "$STUB_REAL_GIT" "$@" ;;
esac
'''

# gh: `api graphql` prints STUB_GH_JSON and logs the query.
GH_STUB = 'echo "gh $*" >> "$STUB_LOG"\nprintf "%s" "$STUB_GH_JSON"\n[ -n "$STUB_GH_JSON" ] || exit 1\n'

LAUNCHCTL_STUB = ('echo "launchctl $*" >> "$STUB_LOG"\n'
                  'case "$1" in print) exit 113 ;; bootstrap) exit "${STUB_BOOTSTRAP_RC:-0}" ;; esac\nexit 0\n')


class PromoteCase(unittest.TestCase):
    """A satellite of a dead hub, ready to be promoted; no tests of its own."""

    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.destroy)
        f = self.fixture
        self.tmp = pathlib.Path(f.tmp.name)
        (f.state / "profile").write_text("work\n")
        self.config = fixture_config.copy_config(self.tmp)
        self.log = self.tmp / "stub.log"
        self.fence = self.tmp / "fence"
        self.fence.write_text("elsewhere\n")
        write_stub(f.stubs, "tailscale", TAILSCALE_STUB)
        write_stub(f.stubs, "sd", SD_STUB)
        write_stub(f.stubs, "launchctl", LAUNCHCTL_STUB)
        self.prove = write_stub(f.stubs, "prove-python", PROVE_STUB)
        write_stub(f.stubs, "git", GIT_STUB)
        write_stub(f.stubs, "gh", GH_STUB)
        write_stub(f.stubs, "notify", "exit 0\n")
        write_stub(f.stubs, "jev", "exit 0\n")
        fixture_config.seal(self, f.stubs)
        (f.pack / "bin").mkdir()
        # The pack's settings reader, which the drain imports; a link, never written.
        (f.pack / "bin/sd_lib.py").symlink_to(PACK / "bin/sd_lib.py")
        write_stub(f.pack / "bin", "sd-db", SD_DB_STUB)
        # The NAS: two roots, each with dated snapshot folders.
        self.nas = self.tmp / "nas"
        for folder, names in (("sd-backups-hourly", ("2026-10-14.4", "2026-10-14.5")),
                              ("sd-backups", ("2026-10-13.1",))):
            for name in names:
                (self.nas / folder / name).mkdir(parents=True)
                (self.nas / folder / name / "backup-manifest.json").write_text("{}\n")
        # This satellite's own hub-role job files; the hourly one names a local root.
        # Named as the job example's default local root, not as the NAS's hourly root.
        self.hourly = self.tmp / "local-backups" / "sd-backups"
        self.hourly.parent.mkdir()
        jobs = self.config / "cron-jobs" / "jobs"
        jobs.mkdir(parents=True)
        for job in ROLE_JOBS + ["mirror-sync-nightly"]:
            body = 'JOB_SCHEDULE="50 * * * *"\nJOB_COMMAND="true"\n'
            if job == "sd-db-backup-hourly":
                body = f'SD_DB_BACKUP_HOURLY_DESTINATION="{self.hourly}"\n' + body
            (jobs / f"{job}.job").write_text(body)
        # task-actions from the config folder, so its own installer stays out of the run.
        agents = self.config / "machine-setup" / "launchagents"
        (agents / f"{PREFIX}.task-actions.plist").write_text(
            (agents / f"{PREFIX}.sd-serve.plist").read_text())
        self.hub_json = f.home / ".config/sd/hub.json"
        self.hub_json.parent.mkdir(parents=True)
        self.port = self.closed_port()
        self.hub_json.write_text(json.dumps({"hub": HUB, "port": self.port}) + "\n")
        self.marker = f.home / ".local/state/sd/promote-intent"
        self.lanes = self.tmp / "lanes"
        self.lanes.mkdir()
        (self.config / "notify").mkdir(exist_ok=True)
        (self.config / "notify/.env").write_text("NOTIFY_EMAIL_TO=ops@example.test\nEMAIL_FROM=ops@example.test\n")
        # The satellite stage's Jev step finds its shadow file.
        (f.home / ".config/jev").mkdir(parents=True)
        (f.home / ".config/jev/shadow").write_text("on\n")

    @staticmethod
    def closed_port():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def run_promote(self, *args, expect=0, **extra):
        env = {**fixture_config.env(self.config), "STUB_LOG": self.log, "STUB_FENCE": self.fence,
               "OFFSITE_VERIFY_ROOT": self.nas, "SD_DB_PYTHON": self.prove,
               "XDG_STATE_HOME": self.fixture.home / ".local/state", "SD_LANE_ROOT": self.lanes,
               "PROMOTE_PROBE_SECONDS": "1", "STUB_HUB_NAME": HUB, "STUB_REAL_GIT": shutil.which("git"),
               "STUB_PIN": "a" * 40, "STUB_SYSTEM_HEAD": "a" * 40, "STUB_PIN_PACK": "b" * 40,
               "STUB_PACK_HEAD": "b" * 40, **extra}
        result = self.fixture.run("promote", *args, **env)
        self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return result

    def marker_step(self):
        if not self.marker.exists():
            return None
        return dict(line.split("=", 1) for line in self.marker.read_text().splitlines())["step"]

    def calls(self, word):
        return [line for line in self.log.read_text().splitlines() if word in line] if self.log.exists() else []

    def installed(self):
        folder = self.fixture.home / "Library/LaunchAgents"
        return sorted(p.stem for p in folder.glob("*.plist")) if folder.exists() else []

    def moved(self):
        return sorted(self.hub_json.parent.glob("hub.json.promoted-*"))



class Promote(PromoteCase):
    # Dry run and step 1.

    def test_a_dry_run_prints_hub_ready_and_moves_nothing(self):
        out = self.run_promote().stdout
        self.assertIn("== hub-ready", out)
        self.assertIn(f"  ok      sd.hub_name is {HUB}", out)
        self.assertNotIn("NOT-READY", out)
        self.assertTrue(self.hub_json.exists())
        self.assertIsNone(self.marker_step())
        self.assertEqual([], self.calls("restore"))

    def test_step_1_refuses_while_the_hub_answers(self):
        with socket.socket() as hub:
            hub.bind(("127.0.0.1", 0))
            hub.listen()
            self.hub_json.write_text(json.dumps({"hub": "127.0.0.1", "port": hub.getsockname()[1]}))
            result = self.run_promote("--apply", expect=1)
        self.assertIn("answers; promote only a dead hub's satellite", result.stderr)
        self.assertTrue(self.hub_json.exists())
        self.assertIsNone(self.marker_step())

    def test_step_1_refuses_an_old_hub_with_no_hub_json(self):
        """The hand-back entry: an old hub that skipped the copy of hub.json."""
        self.hub_json.unlink()
        self.fixture.db.parent.mkdir(parents=True)
        self.fixture.db.write_text("")
        result = self.run_promote("--apply", expect=1)
        self.assertIn("this machine is not a satellite", result.stderr)
        self.assertIsNone(self.marker_step())

    def test_step_1_refuses_a_hub_ready_line_that_is_not_ok(self):
        (self.config / "cron-jobs/jobs/offsite-verify.job").unlink()
        result = self.run_promote("--apply", expect=1)
        self.assertIn("NOT-READY no job file offsite-verify.job", result.stdout)
        self.assertIn("a hub-ready line is not ok; nothing moved", result.stderr)
        self.assertTrue(self.hub_json.exists())
        self.assertEqual([], self.calls("restore"))

    def test_step_1_refuses_with_no_backup_root(self):
        result = self.run_promote("--apply", expect=1, OFFSITE_VERIFY_ROOT="")
        self.assertIn("NOT-READY no backup root", result.stdout)
        self.assertTrue(self.hub_json.exists())

    def promote_from_usb(self):
        """Round 3: the NAS unmounted, the hub's USB disk passed as --from."""
        # The stub restores from the first root; the hourly root holds the
        # names its NAS copy took today.
        usb = self.tmp / "usb" / "sd-backups"
        (usb / "2026-10-14.7").mkdir(parents=True)
        (usb / "2026-10-14.7" / "backup-manifest.json").write_text("{}\n")
        usb_hourly = self.tmp / "usb" / "sd-backups-hourly"
        (usb_hourly / f"{utc_today()}.3").mkdir(parents=True)
        gone = self.tmp / "nas-unmounted"
        args = ("--apply", "--from", str(usb), "--from", str(usb_hourly))
        return usb, usb_hourly, gone, args, self.run_promote(*args, expect=1, OFFSITE_VERIFY_ROOT=gone)

    def test_a_usb_snapshot_promotes_while_the_nas_roots_cannot_be_read(self):
        """One readable root with a snapshot is enough; restore --newest reports the others."""
        today = utc_today()
        usb, usb_hourly, gone, _, result = self.promote_from_usb()
        self.assertIn(f"  --      backup root not readable now: {gone}/sd-backups-hourly", result.stdout)
        self.assertNotIn("NOT-READY", result.stdout)
        self.assertTrue(self.fixture.db.exists())
        self.assertIn(f"restore --newest {usb} {usb_hourly} {gone}/sd-backups-hourly", self.calls("restore")[0])
        self.assertIn(f"not read    : {gone}/sd-backups-hourly {gone}/sd-backups", result.stdout)
        # The unread NAS cannot name today's snapshots; the USB root does, so they are held.
        self.assertTrue((self.hourly / "2026-10-14.7" / "backup-manifest.json").exists(), "the restored snapshot was not seeded")
        self.assertEqual([], list((self.hourly / f"{today}.3").iterdir()))
        # The hub serves, but the NAS's names of today are not held: the
        # promotion pauses at its hold step, and hub-ready says so.
        self.assertIn("hub failover report", result.stdout)
        self.assertIn("promote: paused: the hub serves; the NAS copies wait until promote --apply runs again after this:\n"
                      f"  {gone}/sd-backups-hourly cannot be listed\n  {gone}/sd-backups cannot be listed\n", result.stderr)
        self.assertEqual("hold", self.marker_step())
        self.assertIn(f"NOT-READY promote stopped before its end: {self.marker} names step hold", self.run_promote().stdout)

    def test_a_paused_promotion_holds_the_nas_names_once_the_nas_is_back(self):
        _, _, gone, args, _ = self.promote_from_usb()
        (gone / "sd-backups-hourly" / f"{utc_today()}.9").mkdir(parents=True)
        # A nightly name is no pair of the hourly root's snapshot of that name.
        (gone / "sd-backups" / f"{utc_today()}.1").mkdir(parents=True)
        (gone / "sd-backups" / f"{utc_today()}.1" / "backup-manifest.json").write_text("nightly\n")
        (self.hourly / f"{utc_today()}.1").mkdir()
        (self.hourly / f"{utc_today()}.1" / "backup-manifest.json").write_text("hourly\n")
        self.run_promote(*args, OFFSITE_VERIFY_ROOT=gone)
        self.assertEqual([], list((self.hourly / f"{utc_today()}.9").iterdir()))
        self.assertIsNone(self.marker_step())

    def test_a_paused_promotion_that_cannot_read_the_nas_setting_stays_paused(self):
        """A failed read of OFFSITE_VERIFY_ROOT is no answer, not a NAS with nothing to hold."""
        _, _, _, args, _ = self.promote_from_usb()
        env = self.config / "mirror-sync" / ".env"
        env.parent.mkdir(exist_ok=True)
        env.write_text("OFFSITE_VERIFY_ROOT=/Volumes/example\n")
        env.chmod(0)
        self.addCleanup(env.chmod, 0o600)
        result = self.run_promote(*args, expect=1, OFFSITE_VERIFY_ROOT="")
        self.assertIn("could not read OFFSITE_VERIFY_ROOT for the NAS roots", result.stderr)
        self.assertEqual("hold", self.marker_step())

    def test_a_snapshot_taken_while_paused_under_a_nas_name_keeps_the_pause(self):
        """The hourly job writes here while its NAS copy waits; that name may be on the NAS."""
        _, _, gone, args, _ = self.promote_from_usb()
        name = f"{utc_today()}.9"
        # The rerun may come after UTC midnight: yesterday's snapshot still collides.
        before = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)).strftime("%Y-%m-%d.9")
        (gone / "sd-backups").mkdir(parents=True)
        for root, body in ((gone / "sd-backups-hourly", "nas\n"), (self.hourly, "taken here\n")):
            for n in (name, before):
                (root / n).mkdir(parents=True)
                (root / n / "backup-manifest.json").write_text(body)
        result = self.run_promote(*args, expect=1, OFFSITE_VERIFY_ROOT=gone)
        for n in (name, before):
            self.assertIn(f"  {self.hourly}/{n} is not the snapshot of that name in {gone}/sd-backups-hourly; move it aside",
                          result.stderr)
        self.assertEqual("hold", self.marker_step())
        # Moved aside, today's name is held, and the promotion ends.
        (self.hourly / name).rename(self.tmp / "aside")
        (self.hourly / before).rename(self.tmp / "aside-before")
        self.run_promote(*args, OFFSITE_VERIFY_ROOT=gone)
        self.assertEqual([], list((self.hourly / name).iterdir()))
        self.assertIsNone(self.marker_step())

    def test_a_paused_promotion_stays_paused_while_the_nas_cannot_be_listed(self):
        _, _, gone, args, _ = self.promote_from_usb()
        result = self.run_promote(*args, expect=1, OFFSITE_VERIFY_ROOT=gone)
        self.assertIn("promote: paused:", result.stderr)
        self.assertEqual("hold", self.marker_step())
        self.assertEqual(1, len(self.calls("restore")))

    def test_no_readable_root_is_not_ready(self):
        """With no root it can read, promote has no path: it refuses before anything moves."""
        result = self.run_promote("--apply", expect=1, OFFSITE_VERIFY_ROOT=self.tmp / "nas-unmounted")
        self.assertIn("NOT-READY backup root not readable:", result.stdout)
        self.assertTrue(self.hub_json.exists())
        self.assertEqual([], self.calls("restore"))

    # The whole run.

    def test_a_promotion_restores_seeds_takes_the_name_and_installs_the_role(self):
        result = self.run_promote("--apply")

        self.assertFalse(self.hub_json.exists())
        self.assertEqual(1, len(self.moved()))
        self.assertTrue(self.fixture.db.exists())
        self.assertIn(f"restore --newest {self.nas}/sd-backups-hourly {self.nas}/sd-backups", self.calls("restore")[0])
        self.assertTrue((self.hourly / "2026-10-14.5" / "backup-manifest.json").exists(), "the hourly root was not seeded")
        self.assertEqual(["tailscale set --hostname=hub"], self.calls("tailscale set"))
        self.assertEqual(sorted(ROLE_AGENTS + [f"{PREFIX}.cron.{j}" for j in ROLE_JOBS]), self.installed())
        self.assertEqual([f"prove {self.fixture.pack}/lib {HUB} {self.port} {self.fixture.db} 2026-10-14.5"],
                         self.calls("prove"))
        self.assertIn("writes after this time are lost", result.stdout)
        self.assertIn("leases released: run-7", result.stdout)
        self.assertIsNone(self.marker_step())

    def test_an_apply_holds_the_lanes_first(self):
        """Step 2: the run goes through refresh_drain.py, which holds each lane's lock."""
        (self.lanes / "some-repo").mkdir()
        result = self.run_promote("--apply")
        self.assertIn("lanes held:", result.stdout + result.stderr)

    def test_the_drain_reads_the_lane_root_here_while_sd_cannot_reach_the_dead_hub(self):
        """Step 2 runs while the hub is dead: the lane root comes from this machine's pack config, not sd."""
        (self.lanes / "some-repo/lane").mkdir(parents=True)
        env = self.fixture.env()
        config = pathlib.Path(env.get("XDG_CONFIG_HOME") or self.fixture.home / ".config")
        (config / "sd-ai-command-pack").mkdir(parents=True, exist_ok=True)
        (config / "sd-ai-command-pack/config.json").write_text(
            json.dumps({"config": {"sd": {"lane_root": str(self.lanes)}}}) + "\n")
        result = self.run_promote("--apply", SD_LANE_ROOT="", STUB_SD_DEAD="1")
        self.assertIn(f"lanes held: 1 runner lock(s) under {self.lanes}", result.stdout + result.stderr)

    def test_a_lane_root_that_cannot_be_read_here_refuses_and_nothing_moves(self):
        """No pack settings reader: no answer, so no guess at the default root."""
        (self.fixture.pack / "bin/sd_lib.py").unlink()
        result = self.run_promote("--apply", expect=1, SD_LANE_ROOT="")
        self.assertIn("sd.lane_root was not read from this machine's pack config", result.stderr)
        self.assertTrue(self.hub_json.exists())
        self.assertEqual([], self.calls("restore"))

    def test_a_hub_name_the_dead_hub_cannot_give_is_unread_not_refused(self):
        """The preflight's one read through the hub: unanswered, it reads `--`, and step 7 checks the restored copy."""
        result = self.run_promote(STUB_HUB_NAME="")
        self.assertIn("  --      sd.hub_name not read from the hub", result.stdout)
        self.assertNotIn("NOT-READY", result.stdout)

    def test_the_drain_waits_for_a_gate_from_before(self):
        """A gate run by hand outside every lane lock ends before the role moves."""
        count = self.tmp / "gate.count"
        count.write_text("0\n")
        result = self.run_promote("--apply", STUB_GATE_COUNT=count)
        self.assertIn("waiting for the gate: 1 holder(s) that started before the lanes were held (pid 7)",
                      result.stdout)
        self.assertIsNone(self.marker_step())

    def test_the_drain_waits_for_a_gate_queued_before_it(self):
        """A gate queued before the drain runs once a slot frees; the role does not move until its process ends."""
        count = self.tmp / "gate.count"
        count.write_text("0\n")
        # Started from a shell that exits, so init reaps it as it would a real gate's parent.
        pid = int(subprocess.run(["sh", "-c", "sleep 3 >/dev/null 2>&1 & echo $!"],
                                 capture_output=True, text=True, check=True).stdout)
        self.addCleanup(lambda: subprocess.run(["kill", "-9", str(pid)], capture_output=True))
        result = self.run_promote("--apply", STUB_GATE_COUNT=count, STUB_GATE_QUEUED=pid)
        self.assertIn(f"(pid {pid})", result.stdout)
        gone = subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode != 0
        self.assertTrue(gone, "the role moved while the queued gate ran")

    def test_a_held_ship_lock_stops_the_drain_and_nothing_moves(self):
        """A ship run by hand holds its satellite ship lock; the role does not move under it."""
        lock = self.fixture.home / ".local/state/sd/ship-locks" / ("a" * 64 + ".lock")
        lock.parent.mkdir(parents=True)
        holder = subprocess.Popen([sys.executable, "-c", "import fcntl, sys, time\nh = open(sys.argv[1], 'a')\n"
                                   "fcntl.flock(h, fcntl.LOCK_EX)\nprint('locked', flush=True)\ntime.sleep(60)\n",
                                   str(lock)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (holder.kill(), holder.wait(), holder.stdout.close()))
        self.assertEqual("locked", holder.stdout.readline().strip())
        result = self.run_promote("--apply", expect=1, REPO_SYNC_DRAIN_WAIT="2")
        self.assertIn(f"a ship (it holds {lock})", result.stderr)
        self.assertTrue(self.hub_json.exists())
        self.assertEqual([], self.calls("restore"))

    # Step 5.

    def test_a_restore_that_restores_nothing_puts_hub_json_back(self):
        result = self.run_promote("--apply", expect=1, STUB_RESTORE_RC="1")
        self.assertIn("nothing restored", result.stderr)
        self.assertTrue(self.hub_json.exists())
        self.assertEqual([], self.moved())
        self.assertIsNone(self.marker_step())
        self.assertEqual([], self.installed())

    # Kills between steps: the marker is where a killed run left it.

    def write_marker(self, **fields):
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        base = {"hub": HUB, "port": self.port}
        self.marker.write_text("".join(f"{k}={v}\n" for k, v in {**base, **fields}.items()))

    def test_killed_after_the_marker_a_rerun_moves_hub_json_and_goes_on(self):
        """Between 3 and 4: the marker names step 4, hub.json is still there."""
        self.write_marker(step=4)
        self.run_promote("--apply")
        self.assertFalse(self.hub_json.exists())
        self.assertTrue(self.fixture.db.exists())
        self.assertIsNone(self.marker_step())

    def test_killed_after_the_move_a_rerun_restores(self):
        """Between 4 and 5: hub.json aside under the name the marker holds."""
        moved = self.hub_json.parent / "hub.json.promoted-20261014T090000Z"
        self.write_marker(step=4, moved=moved)
        self.hub_json.rename(moved)
        self.run_promote("--apply")
        self.assertEqual([moved], self.moved())
        self.assertTrue(self.fixture.db.exists())
        self.assertIsNone(self.marker_step())

    def test_killed_after_the_restore_a_rerun_seeds_without_a_second_restore(self):
        moved = self.hub_json.parent / "hub.json.promoted-20261014T090000Z"
        self.hub_json.rename(moved)
        self.fixture.db.parent.mkdir(parents=True)
        self.fixture.db.write_text("")
        self.write_marker(step=6, moved=moved, snapshot=self.nas / "sd-backups-hourly/2026-10-14.5")
        self.run_promote("--apply")
        self.assertEqual([], self.calls("restore"))
        self.assertTrue((self.hourly / "2026-10-14.5").is_dir())

    def test_a_seed_cut_short_is_copied_again(self):
        moved = self.hub_json.parent / "hub.json.promoted-20261014T090000Z"
        self.hub_json.rename(moved)
        self.fixture.db.parent.mkdir(parents=True)
        self.fixture.db.write_text("")
        (self.hourly / "2026-10-14.5.seeding").mkdir(parents=True)
        self.write_marker(step=6, moved=moved, snapshot=self.nas / "sd-backups-hourly/2026-10-14.5")
        self.run_promote("--apply")
        self.assertTrue((self.hourly / "2026-10-14.5" / "backup-manifest.json").exists())
        self.assertFalse((self.hourly / "2026-10-14.5.seeding").exists())

    def test_killed_inside_step_3_a_marker_with_no_step_starts_again(self):
        """codex: a marker cut short before its step names nothing moved."""
        self.marker.parent.mkdir(parents=True)
        self.marker.write_text(f"hub={HUB}\n")
        self.run_promote("--apply")
        self.assertFalse(self.hub_json.exists())
        self.assertIsNone(self.marker_step())

    # Step 7.

    def test_a_fence_that_says_holder_but_fails_is_no_holder(self):
        """codex: the exit status counts, not only the words."""
        self.fence.write_text("holder\n")
        result = self.run_promote("--apply", expect=1, STUB_FENCE_RC="1")
        self.assertEqual("7", self.marker_step(), result.stdout + result.stderr)
        self.assertEqual([], self.installed())

    def test_a_peer_holding_the_name_stops_at_step_7_with_the_console_step(self):
        result = self.run_promote("--apply", expect=1, STUB_PEER="hub")
        self.assertIn("Stop that machine first: power it off, or unload its sd-serve and lane agents.", result.stderr)
        self.assertIn("If you cannot reach it, remove it in the Tailscale admin console.", result.stderr)
        self.assertIn("Then rename or remove it in the admin console, and rerun:", result.stderr)
        self.assertEqual("7", self.marker_step())
        self.assertEqual([], self.calls("tailscale set"))
        self.assertEqual([], self.installed(), "a hub-role service went in before the name")

        self.run_promote("--apply")
        self.assertIsNone(self.marker_step())
        self.assertIn(f"{PREFIX}.sd-serve", self.installed())

    def test_an_unset_hub_name_in_the_restored_database_refuses_before_the_name(self):
        """The A decision on sd:4044: promote refuses while sd.hub_name is unset."""
        self.fence.write_text("off\n")
        result = self.run_promote("--apply", expect=1)
        self.assertIn(f"sd.hub_name is unset in the restored database", result.stderr)
        self.assertIn(f"sd config set sd.hub_name {HUB}", result.stderr)
        self.assertEqual("7", self.marker_step())
        self.assertEqual([], self.calls("tailscale set"))
        self.assertEqual([], self.installed())

    # Step 8.

    def test_a_stage_that_fails_part_way_stops_at_step_8_and_a_rerun_installs_the_rest(self):
        result = self.run_promote("--apply", expect=1, STUB_BOOTSTRAP_RC="5")
        self.assertIn("stage did not finish; rerun", result.stderr)
        self.assertEqual("8", self.marker_step())
        self.run_promote("--apply")
        self.assertIsNone(self.marker_step())
        self.assertEqual(sorted(ROLE_AGENTS + [f"{PREFIX}.cron.{j}" for j in ROLE_JOBS]), self.installed())

    def test_a_fence_that_fails_during_the_stages_stops_at_step_8(self):
        """codex: the stages skip the role on an unread fence and exit 0;
        step 8 checks the role is installed before it moves on."""
        result = self.run_promote("--apply", expect=1, STUB_FENCE_FAIL_FROM="3")
        self.assertEqual("8", self.marker_step(), result.stdout + result.stderr)
        self.assertIn("the hub role is not installed", result.stderr)
        (self.log.parent / "stub.log.fences").unlink()
        self.run_promote("--apply")
        self.assertIsNone(self.marker_step())
        self.assertEqual(sorted(ROLE_AGENTS + [f"{PREFIX}.cron.{j}" for j in ROLE_JOBS]), self.installed())

    # Step 9.

    # Hand-back: the repaired hub keeps its hub profile (round 2, sd:4012).

    def hub_profile(self, satellite=False):
        """The repaired old hub, demoted by the README: hub.json copied in, its hub profile kept."""
        profiles = self.config / "machine-setup/profiles"
        (profiles / "personal.agent").write_text("\n".join(ROLE_AGENTS) + "\n")
        (profiles / "personal.cron").write_text("\n".join(ROLE_JOBS) + "\n")
        if satellite:
            (profiles / "personal.satellite").write_text(f"{HUB}:8769\n")
        (self.fixture.state / "profile").write_text("personal\n")

    def test_a_hub_profile_takes_the_role_back(self):
        """Hand-back step 2: its profile lists the role, and the fence, not the profile, says it holds it."""
        self.hub_profile()
        self.run_promote("--apply")
        self.assertEqual(sorted(ROLE_AGENTS + [f"{PREFIX}.cron.{j}" for j in ROLE_JOBS]), self.installed())
        self.assertEqual(1, len(self.calls("prove")))
        self.assertIsNone(self.marker_step())

    def test_a_hub_profile_that_rejoined_as_a_satellite_takes_the_role_back(self):
        """A hub profile that also names a satellite: holding the role still installs its hub-only labels."""
        self.hub_profile(satellite=True)
        self.run_promote("--apply")
        self.assertEqual(sorted(ROLE_AGENTS + [f"{PREFIX}.cron.{j}" for j in ROLE_JOBS]), self.installed())
        self.assertIsNone(self.marker_step())

    def test_a_hub_profile_stopped_at_step_9_reads_as_the_holder(self):
        """hub-ready names the role it holds, not a missing hub.json."""
        self.hub_profile()
        self.run_promote("--apply", expect=1, STUB_PROVE_RC="1")
        self.assertEqual("9", self.marker_step())
        out = self.run_promote().stdout
        self.assertIn("  ok      this machine holds the hub role", out)
        self.assertNotIn("is not a satellite", out)

    def test_a_hub_profile_that_lacks_a_role_label_names_the_profile(self):
        """Its profile, not the role, decides what it installs; a gap stops step 8 and says where."""
        self.hub_profile()
        (self.config / "machine-setup/profiles/personal.cron").write_text("sd-db-backup\n")
        result = self.run_promote("--apply", expect=1)
        self.assertEqual("8", self.marker_step())
        self.assertIn("personal.cron", result.stderr)

    def test_a_failed_session_keeps_the_marker_and_a_rerun_proves_again(self):
        result = self.run_promote("--apply", expect=1, STUB_PROVE_RC="1")
        self.assertIn("no session on", result.stderr)
        self.assertEqual("9", self.marker_step())
        self.run_promote("--apply")
        self.assertIsNone(self.marker_step())

    def test_a_rerun_at_step_9_asks_the_fence_again(self):
        """codex pass: a rerun at step 9 follows a change of holder; it proves nothing for another hub."""
        self.run_promote("--apply", expect=1, STUB_PROVE_RC="1")
        self.assertEqual("9", self.marker_step())
        self.fence.write_text("elsewhere\n")
        before = len(self.calls("prove"))
        result = self.run_promote("--apply", expect=1)
        self.assertIn("this machine does not hold the hub role now", result.stderr)
        self.assertEqual("9", self.marker_step())
        self.assertEqual(before, len(self.calls("prove")))

    def test_step_9_refuses_a_session_that_reaches_another_database(self):
        """codex pass: the session must reach this machine's restored database, not any server by that name."""
        # A wrapper runs the proof with the pack's own `remote.connect` answering from STUB_REMOTE_DB.
        import sys
        prelude = ("import os, sys\nfrom pathlib import Path\nsys.path.insert(0, sys.argv[1])\n"
                   "from sd_db import database, remote\n"
                   "remote.connect = lambda *a, **k: database.open_local(Path(os.environ['STUB_REMOTE_DB']), "
                   "write=False, create=False)\n")
        other = self.tmp / "other.db"
        subprocess.run([sys.executable, "-I", "-c", "import sys\nsys.path.insert(0, sys.argv[1])\n"
                        "from sd_db.migrate import initialise\ninitialise(sys.argv[2])",
                        str(PACK / "lib"),
                        str(other)], check=True, capture_output=True, timeout=60)
        python = write_stub(self.fixture.stubs, "fake-lib-python",
                            f'code=$3; shift 3\nexec "{sys.executable}" -I -c "{prelude}$code" "$@"\n')
        result = self.run_promote("--apply", expect=1, SD_DB_PYTHON=python, STUB_REMOTE_DB=str(other))
        self.assertIn("reached a database without this restore", result.stderr)
        self.assertEqual("9", self.marker_step())
        self.run_promote("--apply", SD_DB_PYTHON=python, STUB_REMOTE_DB=str(self.fixture.db))
        self.assertIsNone(self.marker_step())

    def test_the_seed_holds_every_nas_name_of_the_day(self):
        """codex pass: the hourly job takes the first free name of the UTC day, so each NAS name is held."""
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        today, yesterday = now.strftime("%Y-%m-%d"), (now - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        for name in (f"{yesterday}.2", today, f"{today}.9"):
            (self.nas / "sd-backups-hourly" / name).mkdir(exist_ok=True)
            (self.nas / "sd-backups-hourly" / name / "backup-manifest.json").write_text("{}\n")
        self.run_promote("--apply")
        self.assertTrue((self.hourly / f"{today}.9" / "backup-manifest.json").exists(), "the restored snapshot was not copied")
        self.assertEqual([], list((self.hourly / today).iterdir()), "an earlier name is held by an empty folder")
        self.assertFalse((self.hourly / f"{yesterday}.2").exists(), "an earlier day was held")

    def test_hub_ready_names_a_marker_left_behind(self):
        self.write_marker(step=7)
        out = self.run_promote().stdout
        self.assertIn("NOT-READY promote stopped before its end", out)
        self.assertIn("names step 7", out)

    # The readiness lines --undo's slice adds (sd:4048).

    def test_hub_ready_refuses_checkouts_off_the_hub_pin(self):
        result = self.run_promote("--apply", expect=1, STUB_PACK_HEAD="c" * 40)
        self.assertIn(f"NOT-READY system at {'a' * 40} and pack at {'c' * 40}, not hub-pin's", result.stdout)
        self.assertIn("a hub-ready line is not ok; nothing moved", result.stderr)
        self.assertTrue(self.hub_json.exists())

    def test_hub_ready_names_a_missing_hub_pin_tag(self):
        self.assertIn("NOT-READY no hub-pin tag", self.run_promote(STUB_PIN="").stdout)

    def test_hub_ready_names_a_build_the_hub_refuses(self):
        out = self.run_promote(STUB_SATELLITE_LINE="  DIFFERS sd_db build: satellite 1, hub 2 — follow").stdout
        self.assertIn("NOT-READY sd_db build: satellite 1, hub 2", out)

    def test_a_dead_hub_leaves_the_build_unread_not_ok(self):
        out = self.run_promote(STUB_SATELLITE_LINE="  SKIP    sd hub did not answer").stdout
        self.assertIn("  --      the hub did not answer; the sd_db build was not compared", out)
        self.assertNotIn("sd_db build matches", out)

    def lane_repos(self):
        host = subprocess.run(["hostname", "-s"], capture_output=True, text=True, check=True).stdout.strip().lower()
        return ("sd-db: ~/repos/a  git@github.com:owner-a/one.git  yes  local  hub  auto\n"
                f"sd-db: ~/repos/b b  https://github.com/owner-b/two  yes  local  {host}  auto\n"
                "sd-db: ~/repos/c  https://github.com/owner-c/three  yes  local  other-host  auto\n")

    def test_hub_ready_names_lane_repositories_the_gh_login_cannot_push(self):
        data = {"data": {"viewer": {"login": "fixture"}, "r0": {"viewerPermission": "WRITE"}, "r1": None}}
        result = self.run_promote("--apply", expect=1, STUB_REPOS=self.lane_repos(), STUB_GH_JSON=json.dumps(data))
        self.assertIn("NOT-READY gh login fixture cannot push to 1 lane repositories: owner-b/two", result.stdout)
        query = self.calls("gh api graphql")[0]
        self.assertIn('owner: "owner-a", name: "one"', query)
        self.assertNotIn("owner-c", query, "another machine's lane was asked for")

    def test_hub_ready_reads_push_access_for_every_lane_repository(self):
        data = {"data": {"viewer": {"login": "fixture"}, "r0": {"viewerPermission": "ADMIN"},
                         "r1": {"viewerPermission": "WRITE"}}}
        out = self.run_promote(STUB_REPOS=self.lane_repos(), STUB_GH_JSON=json.dumps(data)).stdout
        self.assertIn("  ok      gh login fixture pushes to all 2 lane repositories", out)

    def test_a_gh_that_does_not_answer_is_not_ok(self):
        out = self.run_promote(STUB_REPOS=self.lane_repos(), STUB_GH_JSON="").stdout
        self.assertIn("  --      gh api did not answer; push access not checked", out)
        self.assertNotIn("  ok      gh", out)

    def test_a_repository_list_the_dead_hub_cannot_give_is_unread_not_refused(self):
        """The gh line reads the registry from the hub promote replaces; no answer is `--`, never NOT-READY."""
        out = self.run_promote(STUB_REPOS=self.lane_repos(), STUB_REPO_DEAD="1").stdout
        self.assertIn("  --      the repository list was not read from the hub; gh push access not checked", out)
        self.assertNotIn("NOT-READY gh", out)

    def test_hub_ready_refuses_an_unset_notify_email_channel(self):
        (self.config / "notify/.env").write_text("NOTIFY_EMAIL_TO=change-me@example.test\n")
        result = self.run_promote("--apply", expect=1)
        self.assertIn("NOT-READY notify's email channel lacks NOTIFY_EMAIL_TO EMAIL_FROM", result.stdout)
        self.assertTrue(self.hub_json.exists())


class HubRole(unittest.TestCase):
    """The agents and cron stages add the hub-role labels only for the holder."""

    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.destroy)
        f = self.fixture
        self.tmp = pathlib.Path(f.tmp.name)
        (f.state / "profile").write_text("work\n")
        self.config = fixture_config.copy_config(self.tmp)
        self.log = self.tmp / "stub.log"
        self.fence = self.tmp / "fence"
        write_stub(f.stubs, "tailscale", TAILSCALE_STUB)
        write_stub(f.stubs, "launchctl", LAUNCHCTL_STUB)
        fixture_config.seal(self, f.stubs)
        (f.pack / "bin").mkdir()
        # The pack's settings reader, which the drain imports; a link, never written.
        (f.pack / "bin/sd_lib.py").symlink_to(PACK / "bin/sd_lib.py")
        write_stub(f.pack / "bin", "sd-db", SD_DB_STUB)
        f.db.parent.mkdir(parents=True)
        f.db.write_text("")
        (f.home / "Library/LaunchAgents").mkdir(parents=True)
        jobs = self.config / "cron-jobs" / "jobs"
        jobs.mkdir(parents=True)
        for job in ROLE_JOBS + ["mirror-sync-nightly"]:
            (jobs / f"{job}.job").write_text('JOB_SCHEDULE="50 * * * *"\nJOB_COMMAND="true"\n')
        agents = self.config / "machine-setup" / "launchagents"
        (agents / f"{PREFIX}.task-actions.plist").write_text(
            (agents / f"{PREFIX}.sd-serve.plist").read_text())

    def update(self, stage, fence):
        self.fence.write_text(fence + "\n")
        result = self.fixture.run("update", stage, "--apply", **fixture_config.env(self.config),
                                  STUB_LOG=self.log, STUB_FENCE=self.fence)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def installed(self):
        folder = self.fixture.home / "Library/LaunchAgents"
        return sorted(p.stem for p in folder.glob("*.plist")) if folder.exists() else []

    def test_no_hub_role_label_goes_in_before_the_fence_reads_holder(self):
        """The 7 to 8 order: elsewhere, unknown and off all install nothing."""
        for fence in ("elsewhere", "unknown", "off"):
            with self.subTest(fence=fence):
                self.update("agents", fence)
                self.update("cron", fence)
                self.assertEqual([], self.installed())

    def test_the_holder_gets_the_role_but_the_icloud_mirror(self):
        self.update("agents", "holder")
        self.update("cron", "holder")
        self.assertEqual(sorted(ROLE_AGENTS + [f"{PREFIX}.cron.{j}" for j in ROLE_JOBS]), self.installed())

    def test_the_live_hub_asks_no_fence(self):
        """A hub profile with no hub.json and no satellite: every stage runs as before the role existed."""
        (self.fixture.state / "profile").write_text("personal\n")
        for stage in ("sd", "cron", "agents", "satellite"):
            with self.subTest(stage=stage):
                self.update(stage, "holder")
                self.assertFalse(pathlib.Path(f"{self.log}.fences").exists(), f"the {stage} stage asked the fence")

    def test_a_demoted_hub_profile_says_so_and_builds_no_database(self):
        """Between demotion and take-back: hub profile, hub.json, no database. Not the hub's EXTRA, no init."""
        (self.fixture.state / "profile").write_text("personal\n")
        self.fixture.db.unlink()
        hub_json = self.fixture.home / ".config/sd/hub.json"
        hub_json.parent.mkdir(parents=True)
        hub_json.write_text(json.dumps({"hub": HUB, "port": 8769}))
        out = self.update("sd", "elsewhere")
        self.assertIn("this hub profile is demoted", out)
        self.assertNotIn("remove it", out)
        self.assertEqual([], [line for line in self.log.read_text().splitlines() if line == "init"]
                         if self.log.exists() else [])

    def test_doctor_checks_the_sd_pieces_on_a_promoted_satellite(self):
        """sd:4648: the holder, not the profile, decides; a satellite still skips them."""
        env = dict(**fixture_config.env(self.config), STUB_LOG=self.log, STUB_FENCE=self.fence)
        self.fence.write_text("holder\n")
        out = self.fixture.run("doctor", "sd", **env).stdout
        self.assertNotIn("sd pieces not checked", out)
        self.assertIn(f"database {self.fixture.db}", out)
        self.fence.write_text("elsewhere\n")
        self.assertIn("sd pieces not checked", self.fixture.run("doctor", "sd", **env).stdout)

    def test_a_satellite_profile_still_holds_the_role_once_promoted(self):
        """A `<profile>.satellite` file keeps is_satellite true after hub.json moved."""
        (self.config / "machine-setup/profiles/work.satellite").write_text(f"{HUB}:8769\n")
        out = self.update("agents", "holder")
        self.assertNotIn("SKIP", out)
        self.assertIn(f"{PREFIX}.sd-serve", self.installed())
        sat = self.update("satellite", "holder")
        self.assertIn("this machine holds the hub role", sat)

    def test_the_role_stays_off_while_hub_json_is_here(self):
        hub_json = self.fixture.home / ".config/sd/hub.json"
        hub_json.parent.mkdir(parents=True)
        hub_json.write_text(json.dumps({"hub": HUB, "port": 8769}))
        self.update("agents", "holder")
        self.assertEqual([], self.installed())
        self.assertNotIn("fence", self.log.read_text() if self.log.exists() else "")


if __name__ == "__main__":
    unittest.main()
