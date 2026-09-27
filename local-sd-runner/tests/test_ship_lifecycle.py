"""Run the real runner, ship CLI, and review CLI against temporary Git/GitHub.

The adjacent pack is required for this cross-repository acceptance fixture.
Only the external model responses and GitHub transport are doubles. The Python
interpreter must have a noneditable sd_db wheel installed, as production does.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from sd_db import runner as store
from sd_db import runner_journal, ship
from sd_db.testing.remote import FixtureRemote
from sd_db.writes import create_item, upsert_repo
from sd_runner import processes, storage
from sd_runner.runtime import Runner

from . import test_runtime

PACK = Path(os.environ.get("SD_ACCEPTANCE_PACK", test_runtime.ROOT.parents[1] / "pack"))


class ExistingRemote(FixtureRemote):
    """Attach the GitHub model to the runner fixture's existing bare remote."""

    def _make_bare(self):
        self.seed = self.root / "github-seed"
        subprocess.run(["git", "clone", "-q", str(self.path), str(self.seed)], check=True)
        test_runtime.git(self.seed, "config", "user.name", "Fixture")
        test_runtime.git(self.seed, "config", "user.email", "fixture@example.invalid")


@unittest.skipUnless((PACK / "bin/sd-ship").is_file(), "cross-repository sd-ship checkout is required")
@unittest.skipUnless(sys.platform == "darwin", "native immutable retention requires macOS")
class ShipLifecycle(unittest.TestCase):
    def setUp(self):
        # Composition reuses setup without discovering every runtime test twice.
        self.fixture = test_runtime.Fixture(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        for name in ("root", "checkout", "database", "db", "item"):
            setattr(self, name, getattr(self.fixture, name))
        self.config = replace(self.fixture.config, pack=PACK)
        self.runner = Runner(self.config)
        self.addCleanup(self._thaw_fixture_retention)
        self.programs = self.root / "programs"
        self.programs.mkdir()
        self.real_git = shutil.which("git")
        self.remote_url = "https://github.com/fixture/repo.git"
        self.remote = ExistingRemote(self.root)
        self.remote.protection = {
            "enforce_admins": {"enabled": True},
            "required_pull_request_reviews": {"required_approving_review_count": 0},
            "required_status_checks": {
                "strict": True, "contexts": ["check"],
                "checks": [{"context": "check", "app_id": 7}],
            },
        }
        # Reuse the pack's external-service double, never its ShipCase tests.
        spec = importlib.util.spec_from_file_location("runner_ship_double", PACK / "tests/test_sd_ship.py")
        self.pack_fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.pack_fixture)
        self.double = self.pack_fixture.ShipDouble(self.remote)
        self.double.__enter__()
        self.addCleanup(self.double.close)
        self._programs()
        self._registry()
        # `HOME` is a directory beside the checkout, not above it. The fixture
        # registered the checkout before this patch, under the real home, so
        # its row holds the absolute path; a home above the checkout would
        # make that a legacy row the library no longer writes (sd:1439). The
        # pack's `sd-ship` reads the row by that absolute path until its own
        # sites move to `sd_db.paths`.
        home = self.root / "home"
        home.mkdir()
        self.environment = {
            "PATH": str(self.programs) + os.pathsep + os.environ["PATH"],
            "HOME": str(home), "LANG": "en_US.UTF-8", "TERM": "dumb",
        }
        self.patch = patch.dict(os.environ, self.environment, clear=True)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        test_runtime.git(self.checkout, "remote", "set-url", "origin", self.remote_url)
        upsert_repo(self.db, str(self.checkout), remote=self.remote_url, runner_merge="auto")
        (self.checkout / "Makefile").write_text("check:\n\t@echo fixture-check-pass\n")
        (self.checkout / "CLAUDE.local.md").write_text(
            "<!-- SD-AI-COMMAND-PACK:LOCAL:START -->\nmode: full\n"
            f"reviewers: {self.reviewers}\n<!-- SD-AI-COMMAND-PACK:LOCAL:END -->\n"
        )
        test_runtime.git(self.checkout, "add", "Makefile", "CLAUDE.local.md")
        test_runtime.git(self.checkout, "commit", "-qm", "Fixture policy and checks")
        test_runtime.git(self.checkout, "push", "-q", "origin", "HEAD:main")
        self.original_head = test_runtime.git(self.checkout, "rev-parse", "HEAD")
        (self.checkout / "README").write_text("uncommitted operator change\n")
        self.original_status = test_runtime.git(self.checkout, "status", "--porcelain")

    def _executable(self, name, source):
        path = self.programs / name
        path.write_text(f"#!{sys.executable}\n" + source)
        path.chmod(0o755)
        return path

    def _thaw_fixture_retention(self):
        # Only remove immutable flags within this test's temporary root so the
        # standard fixture cleanup can remove its own retained clones.
        self.config.retention.resolve().relative_to(self.root)
        if self.config.retention.exists():
            subprocess.run(["chflags", "-R", "nouchg", str(self.config.retention)], check=True)

    def _programs(self):
        self._executable("git", "import os,sys\n"
            f"binary={self.real_git!r}\nargs=sys.argv[1:]\n"
            f"rewrite=['-c', 'url.{self.remote.path}.insteadOf={self.remote_url}']\n"
            "if any(a in ('clone','fetch','push','ls-remote') for a in args): args=rewrite+args\n"
            "os.execv(binary,[binary]+args)\n")
        # The loopback address is compiled into the transport. Sanitized runner
        # environments cannot accidentally select a live GitHub destination.
        self._executable("gh", "import json,sys,urllib.request,urllib.error\n"
            "from pathlib import Path\nargs=sys.argv[1:]\n"
            f"with Path({str(self.root / 'gh-calls.jsonl')!r}).open('a') as log: log.write(json.dumps(args)+'\\n')\n"
            "if args[:2]==['pr','checks']:\n print('fixture checks passed');sys.exit(0)\n"
            "assert args[0]=='api',args\n"
            # `--include` puts the status line and a blank line before the
            # body on stdout, error or not, as gh does: the pack's
            # `api_status` reads the status from it (pack #1086).
            "include='--include' in args\nargs=[a for a in args if a!='--include']\npath=args[1]\n"
            "method=args[args.index('--method')+1] if '--method' in args else 'GET'\n"
            "data=sys.stdin.read().encode() if '--input' in args else None\n"
            f"req=urllib.request.Request({self.double.base_url!r}+'/'+path,data=data,method=method,headers={{'Content-Type':'application/json'}})\n"
            "try:\n"
            " with urllib.request.urlopen(req,timeout=10) as response:\n"
            "  print((f'HTTP/1.1 {response.status}\\n\\n' if include else '')+response.read().decode())\n"
            "except urllib.error.HTTPError as error:\n"
            " text=error.read().decode()\n"
            " if include: print(f'HTTP/1.1 {error.code}\\n\\n'+text)\n"
            " print(text,file=sys.stderr);sys.exit(1)\n")
        self.author = self._executable("claude", "import subprocess,sys\nfrom pathlib import Path\n"
            "prompt=sys.stdin.read()\nassert 'isolated clone' in prompt\n"
            "Path('work.py').write_text('value = 1\\n')\n"
            "Path('ignored.txt').write_text('precious ignored bytes\\n')\n"
            "subprocess.run(['git','add','work.py'],check=True)\n"
            "subprocess.run(['git','commit','-qm',"
            f"'Implement fixture\\n\\nNeeded-by: sd:{self.item}\\nAuthored-with: author/firstvendor'],check=True)\n")
        self.reviewer = self._executable("review-fixture", "import json,sys\nfrom pathlib import Path\n"
            "request=sys.argv[-1]\n"
            f"with Path({str(self.root / 'review-calls.jsonl')!r}).open('a') as log: log.write(json.dumps({{'argv':sys.argv[1:],'prompt':request}})+'\\n')\n"
            "print(json.dumps({'type':'result','subtype':'success','structured_output':{'findings':[]}}))\n")

    def _registry(self):
        registry_path = self.root / ".local/share/sd/providers.yaml"
        registry_path.parent.mkdir(parents=True)
        registry_path.write_text(f"""bills:
  fixture: {{ cost: subscription }}
providers:
  author: {{ start: '{self.author}', vendor: firstvendor, bill: fixture, roles: [author], reader: claude-json }}
  reviewer: {{ start: '{self.reviewer}', vendor: secondvendor, bill: fixture, roles: [reviewer], reader: claude-json }}
  reviewer2: {{ start: '{self.reviewer}', vendor: thirdvendor, bill: fixture, roles: [reviewer], reader: claude-json }}
roles:
  author: [author]
  reviewer: [reviewer, reviewer2]
""")
        import sd_registry
        from sd_db.registry import read, seed
        seed(self.db, read(registry_path))
        registry = sd_registry.read_file(registry_path)
        self.reviewers = ", ".join(str(sd_registry.recipient(registry.providers[name]))
                                   for name in ("reviewer", "reviewer2"))

    def _claim(self, assignment):
        return store.claim(self.db, assignment["id"], owner=self.runner.owner,
                           work_root=self.config.work, retention_root=self.config.retention)

    def _assert_exclusive(self, request):
        self.assertEqual(request["lane"], "serial")
        lease = self.db.execute("SELECT exclusive,released_at FROM runner_lease WHERE run=?",
                                (request["run"]["id"],)).fetchone()
        self.assertEqual(lease["exclusive"], 1)
        self.assertIsNone(lease["released_at"])

    def _assert_operator_preserved(self):
        self.assertEqual(test_runtime.git(self.checkout, "rev-parse", "HEAD"), self.original_head)
        self.assertEqual(test_runtime.git(self.checkout, "status", "--porcelain"), self.original_status)
        self.assertEqual((self.checkout / "README").read_text(), "uncommitted operator change\n")

    def _assert_released(self, request, result):
        self.assertEqual(result["outcome"], "done", result)
        self.assertEqual(result["end_step"], "released", result)
        self.assertIsNotNone(result["released_at"], result)
        self.assertEqual(store.queue_state(self.db, request["id"])["status"], "done")
        self.assertTrue(Path(result["retained_path"]).is_dir())
        with self.assertRaises(PermissionError):
            (Path(result["retained_path"]) / "work.py").write_text("must remain immutable\n")
        self.assertFalse(Path(result["work_path"]).exists())
        self.assertIsNone(processes.start_identity(result["supervisor_pid"]))
        lease = self.db.execute("SELECT released_at FROM runner_lease WHERE run=?", (result["id"],)).fetchone()
        self.assertIsNotNone(lease["released_at"])
        records = {row["id"]: row for row in runner_journal.records(self.database)}
        self.assertEqual(records[result["id"]]["released_at"], result["released_at"])
        self._assert_operator_preserved()

    def _prepare(self):
        request = self._claim(store.enqueue(self.db, [self.item], who="operator")[0])
        result = self.runner.execute(self.db, request)
        self._assert_released(request, result)
        self.assertEqual(result["reviewed_head"], result["authored_head"])
        self.assertEqual((Path(result["retained_path"]) / "ignored.txt").read_text(), "precious ignored bytes\n")
        key = ship.receipt_key("fixture/repo", "work/item", self.item)
        _, receipt = ship.read(self.db, key)
        self.assertEqual(receipt["phase"], "ready_to_send")
        self.assertEqual(receipt["reviewed_head"], self.remote.rev_parse("work/item"))
        report = receipt["passes"][-1]["report"]
        self.assertEqual(report["check"]["status"], "pass")
        self.assertGreater(report["completed_reviews"], 0)
        self.assertEqual(report["completed_reviews"], report["requested_reviews"])
        # How many the pack's route asks for is its policy (a standard change
        # is one lane since its tiered routing); what this suite pins is that
        # the merge that follows asks for none of its own.
        self.requested_reviews = report["requested_reviews"]
        self.assertEqual(len(self.remote.pull_requests), 1)
        self.assertEqual(self.remote.rev_parse("main"), self.original_head)
        self.assertFalse(any(call.method == "PUT" for call in self.remote.calls))
        return result

    def test_author_real_prepare_then_separate_exclusive_merge(self):
        authored = self._prepare()
        queued = store.queue_automatic_merges(self.db)
        self.assertEqual(len(queued), 1)
        self.assertEqual(store.queue_automatic_merges(self.db), [])
        request = self._claim(queued[0])
        self.assertEqual(request["role"], "merge")
        self._assert_exclusive(request)
        merged = self.runner.execute(self.db, request)
        self._assert_released(request, merged)
        assignment = store.queue_state(self.db, request["id"])
        self.assertEqual(assignment["phase"], "merged")
        evidence = json.loads(assignment["result"])
        self.assertEqual(evidence["head"], authored["reviewed_head"])
        self.assertEqual(evidence["merge_commit"], self.remote.rev_parse("main"))
        self.assertNotEqual(self.remote.rev_parse("main"), self.original_head)
        merges = [call for call in self.remote.calls if call.method == "PUT"]
        self.assertEqual(len(merges), 1)
        self.assertEqual(merges[0].body["sha"], authored["reviewed_head"])
        self.assertIn("Authored-with: author/firstvendor", merges[0].body["commit_message"])
        self.assertEqual(len((self.root / "review-calls.jsonl").read_text().splitlines()),
                         self.requested_reviews)
        calls = [json.loads(line) for line in (self.root / "gh-calls.jsonl").read_text().splitlines()]
        self.assertTrue(any(call[:2] == ["pr", "checks"] and "--watch" in call for call in calls))
        self.assertEqual(self.db.execute("SELECT status FROM item WHERE id=?", (self.item,)).fetchone()["status"], "ready_to_send")
        # This is an associated code slice. It carries no whole-item delivery claim.
        self.assertIsNone(merged["delivery_proof"])

    def test_observed_manual_merge_reconciles_in_owned_clone(self):
        authored = self._prepare()
        upsert_repo(self.db, str(self.checkout), remote=self.remote_url, runner_merge="manual")
        self.assertEqual(store.queue_automatic_merges(self.db), [])
        pull = self.remote.pull(1)
        pull.merged_head = authored["reviewed_head"]
        pull.title = f"Implement fixture (#1)\n\nItem: sd:{self.item}\nAuthored-with: author/firstvendor\n"
        remote_commit = self.remote.merge(1, sha=authored["reviewed_head"])
        before = len(self.remote.calls)
        self.runner.watch_deliveries()
        queued = self.db.execute("SELECT id FROM assignment WHERE item=? AND role='merge'", (self.item,)).fetchall()
        self.assertEqual(len(queued), 1, self.runner.delivery_watch_result)
        request = self._claim(store.queue_state(self.db, queued[0]["id"]))
        self.assertEqual(request["scope"], "reconcile")
        self._assert_exclusive(request)
        reconciled = self.runner.execute(self.db, request)
        self._assert_released(request, reconciled)
        evidence = json.loads(store.queue_state(self.db, request["id"])["result"])
        self.assertEqual(evidence["merge_commit"], remote_commit)
        self.assertEqual(evidence["head"], authored["reviewed_head"])
        self.assertTrue(all(call.method == "GET" for call in self.remote.calls[before:]))
        self.assertEqual(self.remote.rev_parse("main"), remote_commit)

    def test_explicit_work_delivery_finalizes_after_retention(self):
        self.item = create_item(self.db, kind="work", title="Deliver fixture work", status="ready",
                                repo=str(self.checkout), branch="work/item")
        upsert_repo(self.db, str(self.checkout), remote=self.remote_url, runner_merge="auto", status_source="row")
        self._programs()
        # The external author submits its acceptance through the actual CLI.
        # Subsequent runner prepare must preserve this explicit delivery claim.
        claim = {"item": self.item, "complete": True, "criteria": [
            {"criterion": "authored fixture change", "passed": True, "evidence": "fixture-check-pass"},
        ]}
        with self.author.open("a") as source:
            source.write("import json\nacceptance=Path('.git/acceptance.json')\n"
                f"acceptance.write_text(json.dumps({claim!r}))\n"
                f"subprocess.run([sys.executable,{str(PACK / 'bin/sd-ship')!r},'prepare',"
                f"'--database',{str(self.database)!r},'--item',{str(self.item)!r},"
                "'--deliver','--acceptance-file',str(acceptance),'--json'],check=True)\n")
        authored = self._prepare()
        request = self._claim(store.queue_automatic_merges(self.db)[0])
        self._assert_exclusive(request)
        at_retention = []

        def observe_retention(_path):
            run = store.run_state(self.db, request["run"]["id"])
            item = self.db.execute("SELECT status FROM item WHERE id=?", (self.item,)).fetchone()
            lease = self.db.execute("SELECT released_at FROM runner_lease WHERE run=?", (run["id"],)).fetchone()
            at_retention.append({"item_status": item["status"], "proof": run["delivery_proof"],
                                 "released_at": run["released_at"], "lease_released_at": lease["released_at"]})
            storage.freeze(_path)

        self.runner.freezer = observe_retention
        delivered = self.runner.execute(self.db, request)
        self._assert_released(request, delivered)
        self.assertEqual(len(at_retention), 1)
        self.assertEqual(at_retention[0]["item_status"], "ready_to_send")
        self.assertIsNotNone(at_retention[0]["proof"])
        self.assertIsNone(at_retention[0]["released_at"])
        self.assertIsNone(at_retention[0]["lease_released_at"])
        proof = json.loads(delivered["delivery_proof"])
        self.assertEqual(proof["commit"], self.remote.rev_parse("main"))
        verified = self.db.execute("SELECT body,resolved_at FROM state WHERE id=?", (proof["receipt"],)).fetchone()
        self.assertEqual(json.loads(verified["body"])["reviewed_head"], authored["reviewed_head"])
        self.assertIsNotNone(verified["resolved_at"])
        self.assertEqual(self.db.execute("SELECT status FROM item WHERE id=?", (self.item,)).fetchone()["status"], "done")


if __name__ == "__main__":
    unittest.main()
