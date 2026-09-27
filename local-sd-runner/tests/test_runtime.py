"""Real Git remotes and owned processes exercise the isolation boundaries."""
import functools
import json
import os
import plistlib
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import runner as store
from sd_db import runner_journal
from sd_db.database import connect
from sd_db.migrate import initialise
from sd_db.writes import create_item, upsert_repo
from sd_runner import controls, gitops, processes, skill_context, storage
from sd_runner.runtime import Config, Runner

ROOT=Path(__file__).resolve().parents[1]


def git(root,*args):
    return subprocess.run(['git','-C',str(root),*args],capture_output=True,text=True,check=True).stdout.strip()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.checkout=self.root/'checkout'; self.remote=self.root/'remote.git'
        subprocess.run(['git','init','-q','--bare','-b','main',str(self.remote)],check=True)
        subprocess.run(['git','init','-q','-b','main',str(self.checkout)],check=True)
        git(self.checkout,'config','user.email','fixture@example.invalid');git(self.checkout,'config','user.name','Fixture')
        (self.checkout/'README').write_text('operator base\n')
        (self.checkout/'.gitignore').write_text('ignored.txt\n')
        git(self.checkout,'add','README','.gitignore');git(self.checkout,'commit','-qm','base')
        git(self.checkout,'remote','add','origin',str(self.remote));git(self.checkout,'push','-q','origin','main')
        git(self.checkout,'checkout','-qb','work/item')
        self.database=self.root/'db/sd.db';initialise(self.database)
        self.db=connect(self.database);self.addCleanup(self.db.close)
        upsert_repo(self.db,str(self.checkout),remote=str(self.remote))
        self.item=create_item(self.db,kind='task',title='fixture work',repo=str(self.checkout),branch='work/item',status='ready')
        self.config=Config(self.database,self.root/'work',self.root/'retained',ROOT.parents[1]/'pack',self.root,interval=.1,floor_gb=.001)
        self.runner=Runner(self.config,freezer=lambda path:None)
        # `serve` probes the operator's login shell (sd:1762). Unit fixtures must
        # not: the probe reads a host profile, and `Popen.wait(timeout=...)` polls
        # with the global time.sleep, which tests that patch `runtime.time.sleep`
        # then count (sd:1778). An empty login path is the failed-probe answer.
        self.runner.resolve_tools=functools.partial(self.runner.resolve_tools,probe=list)
        self.provider=self.root/'provider.py'
        self.provider.write_text('''import os,subprocess
from pathlib import Path
Path('work.txt').write_text('authored work\\n')
Path('ignored.txt').write_text('precious ignored bytes\\n')
subprocess.run(['git','add','work.txt'],check=True)
subprocess.run(['git','commit','-qm','Implement fixture\\n\\nAuthored-with: fixture/fixture'],check=True)
''')

    def claim(self):
        assignment=store.enqueue(self.db,[self.item], who="operator")[0]
        return store.claim(self.db,assignment['id'],owner=self.runner.owner,work_root=self.config.work,retention_root=self.config.retention)

    def run_fixture(self,request):
        return self.runner.execute(self.db,request,command=[sys.executable,str(self.provider)],environment={'PATH':os.environ['PATH'],'HOME':str(self.root)})

    def test_full_run_independent_clone_provider_and_ignored_bytes_retained(self):
        (self.checkout/'README').write_text('uncommitted operator change\n')
        original_head=git(self.checkout,'rev-parse','HEAD')
        request=self.claim(); result=self.run_fixture(request)
        self.assertEqual(result['end_step'],'released',result)
        self.assertEqual(store.queue_state(self.db,request['id'])['status'],'done')
        retained=Path(result['retained_path'])
        self.assertEqual((retained/'ignored.txt').read_text(),'precious ignored bytes\n')
        self.assertFalse((retained/'.git/objects/info/alternates').exists())
        self.assertEqual((self.checkout/'README').read_text(),'uncommitted operator change\n')
        self.assertEqual(git(self.checkout,'rev-parse','HEAD'),original_head)
        self.assertEqual(gitops.remote_head(retained,'work/item'),git(retained,'rev-parse','HEAD'))
        self.assertFalse(Path(result['work_path']).exists())
        self.assertIsNone(processes.start_identity(result['supervisor_pid']))
        self.assertIsNotNone(runner_journal.records(self.database)[0]['released_at'])

    def test_dirty_run_archive_restores_whole_clone_without_overwrite(self):
        self.provider.write_text("from pathlib import Path\nPath('unfinished').write_text('do not lose this')\nPath('ignored.txt').write_text('ignored secret')\n")
        request=self.claim();result=self.run_fixture(request)
        self.assertEqual(result['end_step'],'kept',result)
        self.assertTrue(Path(result['work_path']).exists())
        self.assertIsNone(result['released_at'])
        target=self.root/'restored'
        storage.restore(result,target)
        self.assertEqual((target/'unfinished').read_text(),'do not lose this')
        self.assertEqual((target/'ignored.txt').read_text(),'ignored secret')
        self.assertTrue((target/'.git/index').exists())
        self.assertEqual(storage.restore(result,target), target)
        (target/'unfinished').write_text('operator subsequently changed this')
        with self.assertRaisesRegex(store.RunnerRefused,'overwrites'):
            storage.restore(result,target)

    def test_claimed_crash_recovers_without_git_or_provider(self):
        request=self.claim();self.runner.persist(self.db,request['run']['id'])
        with patch('sd_runner.gitops.durable',side_effect=AssertionError('partial setup must not probe Git')):
            self.assertEqual(self.runner.recover(self.db),[])
        result=store.run_state(self.db,request['run']['id'])
        self.assertEqual(result['end_step'],'released')
        self.assertIn('runner restarted',result['detail'])
        self.assertEqual(store.queue_state(self.db,request['id'])['status'],'blocked')

    def test_partial_clone_kept_without_branch_git_probe(self):
        request=self.claim();run=request['run']
        clone=Path(run['work_path']);clone.mkdir(parents=True);(clone/'partial').write_text('bytes')
        self.runner.persist(self.db,run['id'],start_step='supervised')
        with patch('sd_runner.gitops.durable',side_effect=AssertionError('no Git')):
            self.runner.recover(self.db)
        result=store.run_state(self.db,run['id'])
        self.assertEqual((Path(result['retained_path'])/'partial').read_text(),'bytes')

    def test_stranger_holder_quarantines_without_kill(self):
        request=self.claim();run=request['run'];Path(run['work_path']).mkdir(parents=True)
        child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],cwd=run['work_path'],start_new_session=True)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        store.begin_ending(self.db,run['id'],outcome='blocked',detail='fixture')
        result=self.runner.finish(self.db,run['id'])
        self.assertIsNotNone(result['quarantine'],result)
        self.assertIsNone(result['released_at'])
        self.assertIsNone(child.poll())
        child.terminate();child.wait()
        result=self.runner.finish(self.db,run['id'])
        self.assertIsNotNone(result['released_at'])

    def test_newer_external_run_holds_restored_database(self):
        request=self.claim();self.runner.persist(self.db,request['run']['id'])
        newer=self.runner.persist(self.db,request['run']['id'],start_step='supervised')
        self.db.execute('UPDATE runner_run SET journal_version=0 WHERE id=?',(newer['id'],))
        holds=self.runner.restore_holds(self.db)
        self.assertEqual(holds[0]['reason'],'durable run journal is newer than restored database')
        with self.assertRaises(store.RunnerRefused): self.runner.persist(self.db,newer['id'])

    def test_restore_marker_prevents_dispatch(self):
        (self.database.parent/'runner-restore-intent.json').write_text('{}')
        self.assertTrue(runner_journal.restore_pending(self.database))
        self.assertIn('incomplete restore',self.runner.restore_holds(self.db)[0]['reason'])

    def test_hooks_preserve_helpers_modes_and_forward_other_names(self):
        hooks=self.checkout/'.husky/_';hooks.mkdir(parents=True)
        (hooks/'helper').write_text('marker=forwarded\n')
        (hooks/'pre-commit').write_text('#!/bin/sh\n. "$(dirname "$0")/helper"\nprintf "%s" "$marker" > hook-ran\n')
        (hooks/'pre-commit').chmod(0o755);(hooks/'ignore.sample').write_text('sample')
        git(self.checkout,'config','core.hooksPath','.husky/_')
        request=self.claim();request['lane']='parallel'
        gitops.clone(request);gitops.branch(request)
        clone=Path(request['run']['work_path'])
        git(clone,'commit','--allow-empty','-qm','fixture')
        self.assertEqual((clone/'hook-ran').read_text(),'forwarded')
        self.assertFalse((clone/'.husky/_/ignore.sample').exists())
        self.assertEqual((clone/'.husky/_/pre-commit').stat().st_mode & 0o777,0o755)
        with self.assertRaises(subprocess.CalledProcessError):
            git(clone,'push','origin','HEAD:main')

    def test_diverged_remote_refuses_before_provider(self):
        git(self.checkout,'push','-q','origin','work/item')
        other=self.root/'other'
        subprocess.run(['git','clone','-q',str(self.remote),str(other)],check=True)
        git(other,'config','user.name','Fixture');git(other,'config','user.email','fixture@example.invalid')
        git(other,'checkout','-qb','work/item','origin/work/item')
        (other/'remote-file').write_text('remote');git(other,'add','remote-file');git(other,'commit','-qm','remote');git(other,'push','-q','origin','work/item')
        (self.checkout/'local-file').write_text('local');git(self.checkout,'add','local-file');git(self.checkout,'commit','-qm','local')
        request=self.claim();result=self.run_fixture(request)
        self.assertEqual(store.queue_state(self.db,request['id'])['status'],'blocked')
        self.assertIn('diverged',result['detail'])
        self.assertFalse((Path(result['retained_path'])/'work.txt').exists())

    def test_missing_and_same_version_conflicting_journal_hold(self):
        request = self.claim()
        self.assertIn("no durable", self.runner.restore_holds(self.db)[0]["reason"])
        self.runner.persist(self.db, request["run"]["id"])
        self.db.execute("UPDATE runner_run SET detail='changed without a version' WHERE id=?", (request["run"]["id"],))
        self.assertIn("same-version", self.runner.restore_holds(self.db)[0]["reason"])

    def test_offline_cancel_exact_revision_and_owned_identity(self):
        request = self.claim()
        child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"], start_new_session=True)
        try:
            self.runner.persist(self.db, request["run"]["id"], supervisor_pid=child.pid,
                supervisor_pgid=child.pid, supervisor_start=processes.start_identity(child.pid), start_step="supervised")
            current = store.queue_state(self.db, request["id"])
            with self.assertRaisesRegex(store.RunnerRefused, "refresh"):
                controls.cancel(self.config, request["id"], expected_revision="stale", expected_run=current["run"]["id"], who="operator")
            with self.assertRaisesRegex(store.RunnerRefused, "UUID"):
                controls.cancel(self.config, request["id"], expected_revision=current["revision"], expected_run="another-run", who="operator")
            self.assertIsNone(child.poll())
            result = controls.cancel(self.config, request["id"], expected_revision=current["revision"], expected_run=current["run"]["id"], who="operator")
            child.wait(timeout=5)
            self.assertTrue(result["control"]["signalled_owned_group"])
            self.assertEqual(result["status"], "running")
            self.assertIsNone(result["run"]["released_at"])
            self.runner.recover(self.db)
            self.assertIsNotNone(store.queue_state(self.db, request["id"])["run"]["released_at"])
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_escaped_owned_child_is_quarantined_not_killed(self):
        pid_path = self.root / "escaped-pid"
        self.provider.write_text("import subprocess,sys\nfrom pathlib import Path\n"
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True)\n"
            + f"Path({str(pid_path)!r}).write_text(str(p.pid))\n")
        request = self.claim()
        try:
            result = self.run_fixture(request)
            pid = int(pid_path.read_text())
            self.assertIsNotNone(processes.start_identity(pid))
            self.assertIsNotNone(result["quarantine"])
            self.assertIsNone(result["released_at"])
        finally:
            if pid_path.exists():
                os.kill(int(pid_path.read_text()), 9)

    def test_resume_requeues_in_release_and_preserves_ignored_separately(self):
        self.provider.write_text("from pathlib import Path\nPath('unfinished').write_text('preserve')\nPath('ignored.txt').write_text('long-lived output')\n")
        request = self.claim()
        kept = self.run_fixture(request)
        clone = Path(kept["work_path"])
        git(clone, "add", "unfinished")
        git(clone, "commit", "-qm", "operator resolved")
        current = store.queue_state(self.db, request["id"])
        with patch("sd_runner.controls.Runner", return_value=self.runner):
            result = controls.resume(self.config, request["id"], expected_revision=current["revision"], expected_run=kept["id"], who="operator")
        self.assertEqual(result["status"], "queued", result)
        self.assertEqual(result["run"]["end_action"], "resume")
        self.assertIsNotNone(result["run"]["released_at"])
        self.assertEqual((Path(result["run"]["retained_path"]).parent / "ignored/ignored.txt").read_text(), "long-lived output")
        again = store.claim(self.db, request["id"], owner=self.runner.owner, work_root=self.config.work, retention_root=self.config.retention)
        self.assertEqual(again["run"]["run"], 2)
        gitops.clone(again)
        gitops.branch(again)
        self.assertEqual((Path(again["run"]["work_path"]) / "unfinished").read_text(), "preserve")

    def test_new_named_branch_is_seeded_without_operator_branch_write(self):
        seed = git(self.checkout, "rev-parse", "main")
        binding = {"source_commit": seed, "source_branch": "main", "remote_absent": True}
        self.db.execute("UPDATE item SET branch=?,source_commit=?,fields=? WHERE id=?",
            ("work/new", seed, json.dumps({"runner_branch": binding}), self.item))
        request = self.claim()
        result = self.run_fixture(request)
        self.assertIsNotNone(result["released_at"], result)
        self.assertEqual(gitops.head(self.checkout, "refs/heads/work/new"), "")
        self.assertTrue(gitops.remote_head(self.checkout, "work/new"))
        self.assertNotIn("runner_branch", json.loads(self.db.execute("SELECT fields FROM item WHERE id=?", (self.item,)).fetchone()[0]))

    def test_new_named_branch_remote_collision_refuses_provider(self):
        seed = git(self.checkout, "rev-parse", "main")
        binding = {"source_commit": seed, "source_branch": "main", "remote_absent": True}
        self.db.execute("UPDATE item SET branch=?,source_commit=?,fields=? WHERE id=?",
            ("work/new", seed, json.dumps({"runner_branch": binding}), self.item))
        request = self.claim()
        git(self.checkout, "push", "origin", "HEAD:work/new")
        result = self.run_fixture(request)
        self.assertIn("appeared after preparation", result["detail"])
        self.assertFalse((Path(result["retained_path"]) / "work.txt").exists())

    def test_quarantine_cleanup_retries_even_when_storage_dispatch_paused(self):
        request = self.claim()
        self.runner.persist(self.db, request["run"]["id"])
        store.begin_ending(self.db, request["run"]["id"], outcome="blocked", detail="interrupted")
        with patch.object(self.runner, "pulse", return_value={"storage": {"dispatch_allowed": False}}):
            self.assertEqual(self.runner.tick(self.db), [])
        self.assertIsNotNone(store.run_state(self.db, request["run"]["id"])["released_at"])

    def queue_a_refusal(self, *, refusals=1, behind=None):
        """The queue an operator can reach: rows `claim` refuses, maybe a good one behind them.

        sd:824 -- no supported verb makes a refused row today (`_kind_change`
        refuses a kind change while an assignment is queued), so the fixture
        writes the kind directly. The refusal it produces is the one `_item`
        raises, which is the refusal a done item or a lost remote raises too.

        `behind` is the lane of the trailing runnable row, and the two lanes do
        not behave alike: see the two tests that pass each of them.
        """
        refused = []
        for index in range(refusals):
            branch = f"work/refused-{index}"
            git(self.checkout, "branch", branch)
            item = create_item(self.db, kind="task", title=f"not runnable {index}",
                               repo=str(self.checkout), branch=branch, status="ready")
            refused.append((store.enqueue(self.db, [item], who="operator")[0]["id"], item))
        following = None
        if behind:
            following = store.enqueue(self.db, [self.item], parallel=behind == "parallel",
                                      who="operator")[0]["id"]
        for _, item in refused:
            self.db.execute("UPDATE item SET kind='followup' WHERE id=?", (item,))
        self.db.commit()
        queued = [assignment for assignment, _ in refused] + ([following] if following else [])
        self.assertEqual([row["id"] for row in store.queued(self.db)], queued)
        return [assignment for assignment, _ in refused], following

    def dispatching_tick(self):
        with patch.object(self.runner, "execute", return_value=None), \
             patch.object(self.runner, "pulse", return_value={"storage": {"dispatch_allowed": True}}):
            claimed = [request["id"] for request in self.runner.tick(self.db)]
            for thread in list(self.runner.threads.values()):
                thread.join()
        return claimed, store.heartbeat_state(self.db)

    def test_a_refused_queued_row_is_skipped_and_named_without_stopping_the_next(self):
        (first,), second = self.queue_a_refusal(behind="serial")
        claimed, beat = self.dispatching_tick()
        # The serial row behind the refusal runs on this same tick.
        self.assertEqual(claimed, [second])
        self.assertEqual(store.queue_state(self.db, second)["status"], "running")
        # What an operator reads: the assignment, its reason, and a runner that
        # stays unhealthy while the row sits there.
        self.assertEqual([hold["assignment"] for hold in beat["runtime_holds"]], [first])
        self.assertIn("is a followup item", beat["runtime_holds"][0]["reason"])
        self.assertIs(beat["healthy"], False)
        self.assertIs(beat["ok"], False)

    def test_every_refused_row_is_named_and_not_only_the_first(self):
        """The operator-facing change: an entry per refused row, not one entry.

        sd:824 review F2 -- with one hold entry the difference between naming
        the first refusal and naming them all is invisible, so a queue that
        refuses twice is what pins it.
        """
        refused, third = self.queue_a_refusal(refusals=2, behind="serial")
        claimed, beat = self.dispatching_tick()
        self.assertEqual(claimed, [third])
        self.assertEqual([hold["assignment"] for hold in beat["runtime_holds"]], refused)
        self.assertEqual(len({hold["reason"] for hold in beat["runtime_holds"]}), 2)
        for hold in beat["runtime_holds"]:
            self.assertIn("is a followup item", hold["reason"])

    def test_a_refused_serial_row_still_holds_a_parallel_row_in_its_repo(self):
        """The case the skip does not fix, pinned at what it actually does.

        sd:824 review F1 -- `claim`'s ordering gate holds a parallel-lane author
        row behind any earlier queued serial row in the same repository that
        `_eligible_after` allows, and a skipped row stays queued and stays
        allowed. So this row never dispatches, which is what it did before the
        skip as well. Widening the fix to cover it is a separate decision about
        that gate, not about this loop.
        """
        (first,), parallel = self.queue_a_refusal(behind="parallel")
        self.assertEqual(store.queue_state(self.db, parallel)["lane"], "parallel")
        for _ in range(3):
            claimed, beat = self.dispatching_tick()
            self.assertEqual(claimed, [])
            self.assertEqual(store.queue_state(self.db, parallel)["status"], "queued")
            self.assertEqual([hold["assignment"] for hold in beat["runtime_holds"]], [first])

    def test_a_refused_row_is_retried_and_renamed_each_tick_and_never_blocked(self):
        """Skipping is the choice, so the row keeps its turn and costs a claim.

        `claim` raises the same exception for a refusal that will never clear
        and for one that will, so nothing here writes to the row: clearing it
        is `sd runner cancel` or a repair, and a tick that blocked it would
        have ended queued work on a guess.
        """
        (first,), _ = self.queue_a_refusal()
        for _ in range(2):
            claimed, beat = self.dispatching_tick()
            self.assertEqual(claimed, [])
            self.assertEqual([hold["assignment"] for hold in beat["runtime_holds"]], [first])
            self.assertIs(beat["healthy"], False)
            self.assertEqual(store.queue_state(self.db, first)["status"], "queued")

    def test_selected_skill_source_copy_refuses_drift(self):
        pack = self.root / "pack"
        source = pack / "skills/sd-fixture"
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text("Bound instructions")
        from sd_db.skills_catalog import _hash, _snapshot
        files = _snapshot(pack, source)
        request = self.claim()
        (Path(request["run"]["work_path"]) / ".git").mkdir(parents=True)
        request["scope"] = "skill-use:" + json.dumps({"root": str(pack), "name": "sd-fixture",
            "path": "skills/sd-fixture", "files": files, "source_sha256": _hash(files)})
        result = skill_context.copy(request, str(pack))
        self.assertEqual(Path(result["skill_context"]).read_text(), "Bound instructions")
        (source / "SKILL.md").write_text("Changed instructions")
        with self.assertRaisesRegex(store.RunnerRefused, "changed"):
            skill_context.copy(request, str(pack))

    def test_restore_replays_kill_after_destination_rename_and_preserves_links(self):
        request = self.claim()
        clone = Path(request["run"]["work_path"])
        clone.mkdir(parents=True)
        (clone / "file").write_text("only copy")
        (clone / "outside-link").symlink_to("/outside/no-follow")
        retained = Path(request["run"]["retained_path"])
        storage.retain(clone, retained, freezer=lambda path: None)
        destination = self.root / "restored-again"
        from sd_runner import restoration
        actual_rename = os.rename
        def crash_after_rename(source, target):
            actual_rename(source, target)
            raise OSError("crash immediately after destination rename")
        with patch("sd_runner.restoration.os.rename", side_effect=crash_after_rename), self.assertRaisesRegex(OSError, "crash immediately"):
            storage.restore(request["run"], destination)
        self.assertEqual(storage.restore(request["run"], destination), destination)
        self.assertEqual((destination / "file").read_text(), "only copy")
        self.assertEqual(os.readlink(destination / "outside-link"), "/outside/no-follow")
        self.assertEqual(restoration.inventory(destination), restoration.inventory(retained))

    def test_archive_restore_preserves_external_symlink_without_following_it(self):
        request = self.claim()
        clone = Path(request["run"]["work_path"])
        clone.mkdir(parents=True)
        (clone / "link").symlink_to("/outside/no-follow")
        (clone / "precious").write_text("whole clone")
        storage.archive(clone, Path(request["run"]["retained_path"]).parent / "kept.tar")
        destination = self.root / "archive-restored"
        storage.restore(request["run"], destination)
        self.assertEqual(os.readlink(destination / "link"), "/outside/no-follow")
        self.assertEqual((destination / "precious").read_text(), "whole clone")


    def test_crash_at_each_journal_boundary_recovers_without_checkout_writes(self):
        worker = self.root / "crash-worker.py"
        worker.write_text("""import json,os,sys
from pathlib import Path
from sd_db.database import connect
from sd_runner.runtime import Config,Runner
class CrashRunner(Runner):
 def persist(self,connection,ident,**fields):
  value=super().persist(connection,ident,**fields)
  if fields.get('start_step')==sys.argv[2] or fields.get('end_step')==sys.argv[2]: os._exit(77)
  return value
request=json.loads(Path(sys.argv[1]).read_text())
config=Config(Path(sys.argv[3]),Path(sys.argv[4]),Path(sys.argv[5]),Path(sys.argv[6]),Path(sys.argv[7]),interval=.05,floor_gb=.001)
runner=CrashRunner(config,freezer=lambda path:None)
runner.execute(connect(config.database),request,command=[sys.executable,sys.argv[8]],environment={'PATH':os.environ['PATH'],'HOME':sys.argv[7]})
""")
        operator_head = git(self.checkout, "rev-parse", "HEAD")
        for point in ("supervised", "cloned", "branched", "merged", "started", "retained_clone", "retained"):
            with self.subTest(point=point):
                request = self.claim()
                request_path = self.root / "request.json"
                request_path.write_text(json.dumps(request))
                result = subprocess.run([sys.executable, str(worker), str(request_path), point,
                    str(self.database), str(self.config.work), str(self.config.retention), str(self.config.pack), str(self.root), str(self.provider)],
                    capture_output=True, text=True, timeout=30, check=False)
                self.assertEqual(result.returncode, 77, result.stderr)
                self.assertEqual(self.runner.recover(self.db), [])
                current = store.queue_state(self.db, request["id"])
                self.assertIsNotNone(current["run"]["released_at"], current)
                self.assertTrue(Path(current["run"]["retained_path"]).exists())
                self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), operator_head)
                if current["status"] == "done":
                    self.db.execute("UPDATE item SET status='ready' WHERE id=?", (self.item,))

    def test_live_unjoined_supervisor_never_enters_ending(self):
        request = self.claim()
        child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        actual_wait = child.wait
        child.wait = lambda timeout=None: actual_wait(timeout=.01)
        try:
            with patch("sd_runner.runtime.subprocess.Popen", return_value=child), patch.object(self.runner, "action", side_effect=store.RunnerRefused("setup interrupted")), patch("sd_runner.runtime.processes.terminate_owned", return_value=False):
                result = self.run_fixture(request)
            self.assertIsNone(child.poll())
            self.assertIsNone(result["end_step"])
            self.assertIsNone(result["released_at"])
            self.assertEqual(store.queue_state(self.db, request["id"])["status"], "running")
            child.kill()
            actual_wait(timeout=5)
            child.wait = actual_wait
            with patch.object(self.runner, "pulse", return_value={"storage": {"dispatch_allowed": False}}):
                self.runner.tick(self.db)
            self.assertIsNotNone(store.queue_state(self.db, request["id"])["run"]["released_at"])
        finally:
            if child.poll() is None:
                child.kill()
                actual_wait(timeout=5)


    def test_unresolved_database_restore_holds_dispatch_without_file_marker(self):
        request = self.claim()
        self.runner.persist(self.db, request["run"]["id"])
        self.db.execute("INSERT INTO state(kind,key,timestamp,body) VALUES ('restore','fixture','2026-09-01T00:00:00+00:00','{}')")
        self.assertIn("reimport is incomplete", self.runner.restore_holds(self.db)[0]["reason"])
        with self.assertRaisesRegex(store.RunnerRefused, "reimport"):
            store.enqueue(self.db, [self.item], who="operator")
        result = self.run_fixture(request)
        self.assertTrue(result["recovery_hold"])
        self.assertFalse(Path(result["work_path"]).exists())
        self.assertIsNone(result["supervisor_pid"])

    def test_restore_omitting_active_run_joins_child_and_keeps_external_hold(self):
        request = self.claim()
        actual = self.runner.action
        def interrupted(connection, child, selected, action, deadline, **extra):
            if action == "provider":
                connection.execute("DELETE FROM runner_lease WHERE run=?", (selected["run"]["id"],))
                connection.execute("DELETE FROM runner_run WHERE id=?", (selected["run"]["id"],))
            return actual(connection, child, selected, action, deadline, **extra)
        with patch.object(self.runner, "action", side_effect=interrupted):
            result = self.run_fixture(request)
        self.assertTrue(result["recovery_hold"])
        self.assertIsNone(processes.start_identity(result["supervisor_pid"]))
        self.assertFalse((Path(result["work_path"]) / "work.txt").exists())
        self.assertIn("absent from restored", self.runner.restore_holds(self.db)[0]["reason"])
        with patch.object(self.runner, "pulse", return_value={"storage": {"dispatch_allowed": True}}):
            self.assertEqual(self.runner.tick(self.db), [])

    def test_restore_status_observes_completion_without_replay(self):
        request = self.claim()
        result = self.run_fixture(request)
        destination = self.root / "status-copy"
        from sd_runner import restoration
        self.assertEqual(restoration.status(result, destination)["state"], "unobserved")
        self.assertFalse(destination.exists())
        storage.restore(result, destination)
        self.assertEqual(restoration.status(result, destination)["state"], "complete")
        (destination / "work.txt").write_text("operator changed copy")
        self.assertEqual(restoration.status(result, destination)["state"], "changed")



class StoragePolicy(unittest.TestCase):
    def test_diskutil_receives_resolved_mountpoint_for_nested_and_linked_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            mount = root / "volume"
            work = mount / "worktrees"
            retained = mount / "retained"
            work.mkdir(parents=True)
            retained.mkdir()
            alias = root / "work-link"
            alias.symlink_to(work, target_is_directory=True)
            capacities = [{"device": 1, "free": 90e9, "total": 1e12},
                          {"device": 2, "free": 60e9, "total": 60e9},
                          {"device": 2, "free": 60e9, "total": 60e9}]
            def diskutil(argv, **kwargs):
                if argv[:3] == ["diskutil", "info", "-plist"]:
                    if argv[3] != str(mount):
                        return subprocess.CompletedProcess(argv, 1, b"Could not find disk")
                    return subprocess.CompletedProcess(argv, 0, plistlib.dumps(
                        {"FilesystemType": "apfs", "DeviceIdentifier": "disk9s1"}))
                self.assertEqual(argv, ["diskutil", "apfs", "list", "-plist"])
                return subprocess.CompletedProcess(argv, 0, plistlib.dumps({"Containers": [{"Volumes": [
                    {"DeviceIdentifier": "disk9s1", "CapacityQuota": 60_000_000_000}]}]}))
            for configured in (work, alias):
                with self.subTest(configured=configured), patch("sd_runner.storage.sys.platform", "darwin"), \
                        patch("sd_runner.storage.capacity", side_effect=capacities), \
                        patch.object(Path, "is_mount", autospec=True, side_effect=lambda path: path == mount), \
                        patch("sd_runner.storage.subprocess.run", side_effect=diskutil) as calls:
                    report = storage.preflight(root, configured, retained)
                    self.assertTrue(report["ok"], report)
                    self.assertTrue(report["dispatch_allowed"])
                    self.assertEqual(report["quota_bytes"], 60_000_000_000)
                    self.assertEqual(calls.call_args_list[0].args[0][-1], str(mount))

    def test_separate_unbounded_apfs_volume_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            info = subprocess.CompletedProcess([], 0, plistlib.dumps({"FilesystemType": "apfs", "DeviceIdentifier": "disk9s1"}))
            def inventory(quota):
                return subprocess.CompletedProcess([], 0, plistlib.dumps({"Containers": [{"Volumes": [{"DeviceIdentifier": "disk9s1", "CapacityQuota": quota}]}]}))
            capacities = [{"device": 1, "free": 90e9, "total": 1e12}, {"device": 2, "free": 60e9, "total": 1e12}, {"device": 2, "free": 60e9, "total": 1e12}]
            for quota, expected in ((0, False), (40_000_000_000, False), (60_000_000_000, True)):
                with patch("sd_runner.storage.sys.platform", "darwin"), patch("sd_runner.storage.capacity", side_effect=capacities), patch("sd_runner.storage.subprocess.run", side_effect=[info, inventory(quota)]):
                    report = storage.preflight(root, root, root)
                self.assertEqual(report["ok"], expected, report)
                self.assertEqual(report["dispatch_allowed"], expected)



class Processes(unittest.TestCase):
    def test_kernel_identity_and_recycled_pid_refusal(self):
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True)
        try:
            identity=processes.start_identity(child.pid)
            self.assertIsNotNone(identity)
            run={'supervisor_pid':child.pid,'supervisor_pgid':child.pid,'supervisor_start':identity+'recycled'}
            self.assertFalse(processes.terminate_owned(run,grace_seconds=.1));self.assertIsNone(child.poll())
            run['supervisor_start']=identity
            self.assertTrue(processes.terminate_owned(run,grace_seconds=.1));child.wait(timeout=5)
        finally:
            if child.poll() is None: child.kill();child.wait()

    def test_a_group_that_stops_being_ours_between_check_and_signal(self):
        """The identity check and the killpg cannot be made atomic.

        CI failed a recovery with `{'probe': 'owned_stop', 'reason': '[Errno 1]
        Operation not permitted'}`: the leader exited after the check, the pid
        was reused, and killpg answered EPERM. Both EPERM and ESRCH mean the
        same thing to the caller -- there was no owned group left to signal."""
        for blow_up in (PermissionError(1,'Operation not permitted'),ProcessLookupError(3,'No such process')):
            with self.subTest(raised=type(blow_up).__name__):
                child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True)
                try:
                    identity=processes.start_identity(child.pid)
                    run={'supervisor_pid':child.pid,'supervisor_pgid':child.pid,'supervisor_start':identity}
                    with patch.object(processes.os,'killpg',side_effect=blow_up) as killpg:
                        self.assertFalse(processes.terminate_owned(run,grace_seconds=.1))
                    self.assertEqual(killpg.call_count,1);self.assertIsNone(child.poll())
                finally:
                    child.kill();child.wait()

    def test_the_escalation_losing_the_same_race_still_reports_a_signal(self):
        """SIGTERM landed, so the group dying under the SIGKILL is success."""
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True)
        try:
            identity=processes.start_identity(child.pid)
            run={'supervisor_pid':child.pid,'supervisor_pgid':child.pid,'supervisor_start':identity}
            # No signal is delivered at all: the child outliving the grace is
            # what drives the escalation, and a real SIGTERM would race the
            # interpreter's own startup rather than test anything here.
            sent=[]
            def flaky(pgid,sig):
                sent.append(sig)
                if sig==signal.SIGKILL: raise PermissionError(1,'Operation not permitted')
            with patch.object(processes.os,'killpg',side_effect=flaky):
                self.assertTrue(processes.terminate_owned(run,grace_seconds=.2))
            self.assertEqual(sent,[signal.SIGTERM,signal.SIGKILL]);self.assertIsNone(child.poll())
        finally:
            child.kill();child.wait()

    def test_service_help_and_noarg_contract(self):
        self.assertEqual(subprocess.run([str(ROOT/'runner.sh'),'--help'],capture_output=True, check=False).returncode,0)
        self.assertEqual(subprocess.run([str(ROOT/'runner.sh')],capture_output=True, check=False).returncode,1)


if __name__=='__main__':unittest.main()
