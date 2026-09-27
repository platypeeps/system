"""Legacy inspection cannot execute repository configuration; cron runs once."""
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sd_dashboard import reports_screen

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fixture_collectors", HERE / "collectors.py")
collectors = importlib.util.module_from_spec(spec); spec.loader.exec_module(collectors)

class Resources(unittest.TestCase):
    def test_research_config_is_data_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); config = root / "research.conf.py"; marker = root / "executed"
            config.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\nA=dict(src='index.md', title='Title')\nDOCS=[dict(**A, out='read' + 'ing')]\n")
            self.assertEqual(collectors.read_research_config(config)["DOCS"], [{"src": "index.md", "title": "Title", "out": "reading"}])
            self.assertFalse(marker.exists())
            config.write_text(f"DOCS=__import__('pathlib').Path({str(marker)!r}).write_text('bad')\n")
            with self.assertRaises(ValueError): collectors.read_research_config(config)
            self.assertFalse(marker.exists())

    def test_research_document_fields_are_validated_before_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "research.conf.py"
            for document in (None, "index.md", {"src": "index.md"}, {"out": "reading"},
                             {"src": 7, "out": "reading"}, {"src": "index.md", "out": []},
                             {"src": " ", "out": "reading"}, {"src": "index.md", "out": ""}):
                with self.subTest(document=document):
                    config.write_text(f"DOCS=[{document!r}]\n")
                    with self.assertRaisesRegex(ValueError, "research document 1"):
                        collectors.read_research_config(config)

    def test_computed_research_config_warns_without_hiding_valid_neighbor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); computed = root / "computed"; literal = root / "literal"
            computed.mkdir(); literal.mkdir(); marker = root / "executed"
            (computed / "research.conf.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n"
                "A=dict(src='index.md', title='Computed')\n"
                "for doc, out in ((A, 'reading'),):\n    doc['out']=out\nDOCS=[A]\n")
            (literal / "research.conf.py").write_text(
                "DOCS=[dict(src='index.md', out='reading', title='Literal document')]\n")
            (literal / "index.md").write_text("A valid document remains observable.\n")
            with patch.object(collectors, "REPO_ROOT", root), patch.object(collectors, "git_facts", return_value=None):
                rows = {row["name"]: row for row in collectors.collect_research()}
            self.assertEqual(set(rows), {"computed", "literal"})
            self.assertIn("requires a declared non-empty string 'out'", rows["computed"]["error"])
            self.assertIn("computed configuration is not executed", rows["computed"]["error"])
            self.assertEqual(rows["computed"]["docs"], [])
            self.assertEqual(rows["literal"]["error"], "")
            self.assertEqual(rows["literal"]["docs"][0]["title"], "Literal document")
            self.assertEqual(rows["literal"]["docs"][0]["words"], 5)
            with patch.dict(os.environ, {"REPO_ROOT": str(root)}):
                html = str(reports_screen.collect("research"))
            self.assertIn("Literal document", html)
            self.assertIn("computed could not be fully observed", html)
            self.assertIn("computed configuration is not executed", html)
            self.assertNotIn("does not run", html)
            self.assertFalse(marker.exists())

    def tile(self, source):
        """A stand-in for `sd_tile.py`: a real child, read the way the real one is."""
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        script = Path(directory.name) / "tile.py"; script.write_text(source)
        return patch.object(reports_screen, "TILE", script), Path(directory.name)

    def test_resource_observations_are_visible_and_escaped_in_every_view(self):
        payload = {"html": "<p>Healthy content</p>", "rows": [
            {"what": "<script>repo alert</script>", "detail": '<img src=x onerror="bad()">'}]}
        tile, _ = self.tile("import os, sys\nsys.stdout.write(os.environ['TILE_PAYLOAD'])\n")
        with tile, patch.dict(os.environ, {"TILE_PAYLOAD": json.dumps(payload)}):
            for area, _ in reports_screen.VIEWS:
                with self.subTest(area=area):
                    html = str(reports_screen.collect(area))
                    self.assertIn("Healthy content", html)
                    self.assertIn("Needs attention", html)
                    self.assertIn("&lt;script&gt;repo alert&lt;/script&gt;", html)
                    self.assertIn("&lt;img", html)
                    self.assertNotIn("<script", html)
                    self.assertNotIn("<img", html)
            payload["rows"] = []
            with patch.dict(os.environ, {"TILE_PAYLOAD": json.dumps(payload)}):
                self.assertNotIn("Needs attention", reports_screen.collect("briefs"))

    def test_resource_observations_refuse_incomplete_rows(self):
        tile, _ = self.tile("import os, sys\nsys.stdout.write(os.environ['TILE_PAYLOAD'])\n")
        for rows in (None, {}, ["warning"], [{"what": "warning"}], [{"what": "warning", "detail": 3}]):
            with self.subTest(rows=rows), tile, patch.dict(os.environ, {"TILE_PAYLOAD": json.dumps({"html": "<p>Content</p>", "rows": rows})}):
                with self.assertRaisesRegex(ValueError, "incomplete observations"):
                    reports_screen.collect("research")

    def test_failed_tile_is_refused_with_its_reason(self):
        tile, _ = self.tile("import sys\nsys.stdout.write('{}')\nsys.exit('research: collector broke')\n")
        with tile, self.assertRaisesRegex(ValueError, "research: collector broke"):
            reports_screen.collect("research")

    def assertGone(self, pid_file):
        pids = [int(line) for line in pid_file.read_text().split()]
        deadline = time.monotonic() + 2
        for pid in pids:
            while True:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                if time.monotonic() > deadline:
                    self.fail(f"process {pid} outlived a refused resource view")
                time.sleep(0.01)

    # A child and a grandchild that each record their pid, so the refusal can
    # be shown to leave neither behind.
    SPAWN = ("import os, subprocess, sys\n"
             "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
             "open(sys.argv[0] + '.pids', 'w').write(f'{os.getpid()}\\n{grandchild.pid}\\n')\n")

    def test_overlong_tile_is_cut_at_the_limit_without_waiting_for_exit(self):
        tile, directory = self.tile(self.SPAWN +
            "while True:\n    sys.stdout.write('x' * 8192); sys.stdout.flush()\n")
        started = time.monotonic()
        with tile, self.assertRaisesRegex(ValueError, "stopped at its budget: .*more than its budget of 64 KB"):
            reports_screen.collect("toolbox")
        # Cut at the byte ceiling while reading: well inside the time ceiling,
        # which a check made after exit could not beat.
        self.assertLess(time.monotonic() - started, reports_screen.VIEW_SECONDS["toolbox"] / 2)
        self.assertGone(directory / "tile.py.pids")

    def test_slow_tile_is_killed_within_its_budget(self):
        tile, directory = self.tile(self.SPAWN + "import time\ntime.sleep(60)\n")
        started = time.monotonic()
        with tile, self.assertRaisesRegex(ValueError, "stopped at its budget: .*ran past its budget of 5 seconds") as refused:
            reports_screen.collect("vault")
        self.assertLess(time.monotonic() - started, reports_screen.VIEW_SECONDS["vault"] + 1)
        self.assertGone(directory / "tile.py.pids")
        # The page shows the refusal and its reason rather than an empty view.
        html = str(reports_screen.resources({"resource": ["vault"]}, backend=Mock(side_effect=refused.exception)))
        self.assertIn("This resource could not be observed: resource inspection was stopped at its budget", html)

    def test_every_view_declares_a_ceiling_inside_the_tile_contract(self):
        self.assertEqual(set(reports_screen.VIEW_SECONDS), {area for area, _ in reports_screen.VIEWS})
        self.assertTrue(all(0 < seconds <= 5 for seconds in reports_screen.VIEW_SECONDS.values()))

    def vault(self):
        """A vault holding the decision databases `sd_tile.py queues` reads."""
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        notes = {"blog": (("Old idea", "inbox", "2026-01-02"), ("New idea", "inbox", "2026-02-03"),
                          ("Declined idea", "declined", "2026-01-01")),
                 "topic": (("Ruled topic", "accepted", "2026-01-01"),)}
        for key, spec in collectors.DBS.items():
            folder = root / spec["folder"]; folder.mkdir(parents=True)
            for stem, status, created in notes.get(key, ()):
                (folder / f"{stem}.md").write_text(f"---\nstatus: {status}\ndateCreated: {created}\n---\nBody\n")
        return root

    def test_queues_view_renders_rows_from_a_fixture_vault(self):
        # The real tile, reading a fixture vault through the page's own path.
        with patch.dict(os.environ, {"VAULT": str(self.vault())}):
            html = str(reports_screen.resources({"resource": ["queues"]}))
        self.assertIn('aria-current="page">Queues</a>', html)
        self.assertNotIn("could not be observed", html)
        self.assertIn("<td>Blog Ideas</td><td>2</td><td>3</td>", html)
        self.assertIn("<td>Topics</td><td>0</td><td>1</td>", html)
        self.assertIn("<td>Old idea</td><td>Blog Ideas</td>", html)
        self.assertIn("<td>New idea</td><td>Blog Ideas</td>", html)
        self.assertNotIn("Declined idea", html)
        self.assertIn("<dt>2 in Blog Ideas to decide</dt>", html)

    def test_queues_collector_failure_surfaces_on_the_screen(self):
        # A vault that is not there is an error the page states, never a calm
        # table of zero queues.
        missing = self.vault() / "moved"
        with patch.dict(os.environ, {"VAULT": str(missing)}):
            html = str(reports_screen.resources({"resource": ["queues"]}))
        self.assertIn('aria-current="page">Queues</a>', html)
        self.assertIn("This resource could not be observed: queues: ", html)
        self.assertIn(f"vault path does not exist: {missing}", html)
        self.assertNotIn("<table", html)

    def test_collector_markup_cannot_add_active_content(self):
        parser = reports_screen.SafeFragment()
        parser.feed('<p onclick="bad()">Text<script>alert(1)</script><a href="javascript:bad()">Link</a><img src=x onerror=bad()></p>')
        result = "".join(parser.output)
        self.assertNotIn("onclick", result); self.assertNotIn("<script", result)
        self.assertNotIn("javascript:", result); self.assertNotIn("<img", result)
        self.assertIn("Text", result)

class CronReports(unittest.TestCase):
    def test_report_hook_preserves_single_run_and_exit_when_database_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); cron = root / "cron"; (cron / "jobs").mkdir(parents=True); (cron / "logs").mkdir()
            shutil.copy2(HERE.parent / "local-cron-jobs/cron-jobs.sh", cron / "cron-jobs.sh")
            (cron / "jobs/fixture.job").write_text('JOB_SCHEDULE="* * * * *"\nJOB_COMMAND="printf \'REPORT BODY\\n\'"\nJOB_DIR=' + str(root) + "\n")
            (cron / "logs/fixture.log").write_text("PREVIOUS RUN\n")
            fake = root / "sd"; calls = root / "calls.jsonl"
            fake.write_text("#!/usr/bin/env python3\nimport json,os,sys\nwith open(os.environ['REPORT_CALLS'],'a') as stream: stream.write(json.dumps(sys.argv[1:])+'\\n')\nsys.exit(int(os.environ.get('REPORT_EXIT','0')))\n")
            fake.chmod(0o755)
            env = {**os.environ, "HOME": str(root), "SD_REPORT_BIN": str(fake), "REPORT_CALLS": str(calls), "REPORT_EXIT": "1"}
            result = subprocess.run(["sh", str(cron / "cron-jobs.sh"), "exec", "fixture"], env=env, capture_output=True, text=True, timeout=10, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            log = (cron / "logs/fixture.log").read_text()
            self.assertEqual(log.count("REPORT BODY"), 1)
            argv = json.loads(calls.read_text().splitlines()[0])
            self.assertEqual(argv[:3], ["reports", "ingest", "fixture"])
            self.assertEqual(argv[argv.index("--offset") + 1], str(len("PREVIOUS RUN\n")))
            self.assertIn("report was not recorded", result.stderr)
            self.assertFalse((cron / "logs/.fixture.lock").exists())
            self.assertFalse((cron / "logs/failures.log").exists())
            notification = root / "osascript"
            notification.write_text("#!/bin/sh\nexit 0\n"); notification.chmod(0o755)
            (cron / "jobs/fixture.job").write_text('JOB_SCHEDULE="* * * * *"\nJOB_COMMAND="exit 7"\nJOB_DIR=' + str(root) + "\n")
            env["PATH"] = str(root) + os.pathsep + env["PATH"]
            result = subprocess.run(["sh", str(cron / "cron-jobs.sh"), "exec", "fixture"], env=env, capture_output=True, text=True, timeout=10, check=False)
            self.assertEqual(result.returncode, 7, result.stderr)
            self.assertEqual(len(calls.read_text().splitlines()), 2)
            self.assertEqual((cron / "logs/failures.log").read_text().count("FAILED rc=7"), 1)
            self.assertFalse((cron / "logs/.fixture.lock").exists())
