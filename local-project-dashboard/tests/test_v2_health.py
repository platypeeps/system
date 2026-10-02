"""The v2 Health page (sd:2115).

What this slice promises: `/fleet-health` answers under the shared policy and
loads its script before `shell.js` (`/health` stays the service's own check);
`/api/health` is every area of the design, in its order, each saying whether a
reader covers it. Worktrees come from the fleet child's registrations, grouped
per checkout, and Attribution from `reads.trailer_scan`. An area with no
reader, or whose reader failed, is unknown on the page and names what it does
not read, never a clean lamp. Ports is Operations > Ports' reader with its
counts and warnings; Protection is `protection.rows` as a matrix, one column
per repository, with a table carrying the same cells, and an unread
repository shows no cell.

`health.js` runs under JavaScriptCore (osascript) against the stand-in page
and shell `test_v2_tasks` defines, with the document above as its fetch
answer. The browser half -- focus, the look at 375 px -- is a manual check
recorded on the pull request.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import reads

from sd_dashboard import health_screen, server, v2

from support import NOW, ScreenCase
from test_v2_today import OSASCRIPT, Refused
from test_v2_tasks import SHELL, STAND_IN
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
HEALTH_JS = (V2 / "static" / "health.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")

TREES = [
    {"repo": "group/alpha", "name": "a", "path": "/work/wt-a", "branch": "feat/a", "live": False, "state": "abandoned"},
    {"repo": "group/alpha", "name": "b", "path": "/work/wt-b", "branch": "fix/b", "live": False, "state": "abandoned"},
    {"repo": "beta", "name": "c", "path": "", "branch": "?", "live": False, "state": "unknown"},
    {"repo": "beta", "name": "d", "path": "/work/wt-d", "branch": "main", "live": True, "state": "live"},
]


def fleet_of(trees, root="/checkouts", exists=True):
    def read(area):
        assert area == "sessions", area
        return {"root": root, "rootExists": exists, "worktrees": trees, "processes": [], "processes_error": "",
                "processes_truncated": False, "abandoned": 0, "counts": {}}
    return read


def trailers_of(count, *, no_default=(), no_author=()):
    scan = {"missing": count, "commits": 10, "repos": 2, "no_default": list(no_default), "no_author": list(no_author)}
    return lambda connection, *, now: scan


def ports_snapshot():
    """One configured port listening, one uninspected, and one listener no service claims."""
    return {"complete": True,
            "services": [{"name": "svc-a", "ports": ["9010"], "state": "running", "mark": "+"},
                         {"name": "svc-b", "ports": ["9020"], "state": "stopped", "mark": "."}],
            "listeners": {"9010": {"state": "listening", "source": "lsof", "holder": "svc", "pid": 42},
                          "9020": {"state": "unknown"}},
            "observed": {"complete": False, "ports": {"5000": [{"pid": 7, "command": "other", "address": "*:5000"}]}}}


def flag(check, value, flagged):
    return {"id": check, "value": value, "flagged": flagged, "gap": f"{check} sentence"}


PROTECTION = [
    {"repo": "/checkouts/alpha", "slug": "group/alpha", "status": "protected", "observed_at": "2026-09-05T08:00:00Z",
     "default_branch": "main", "reason": None, "gaps": [{"id": "reviews", "gap": "no approving review is required"}],
     "merge_settings": [flag("squash_message", "PR_TITLE / PR_BODY", False), flag("required_check", "sd/local-gate", True)]},
    {"repo": "/checkouts/beta", "slug": None, "status": "unprotected", "observed_at": "2026-09-05T08:00:00Z",
     "default_branch": "main", "reason": None,
     "gaps": [{"id": "unprotected", "gap": "main has no protection"}, {"id": "required_checks", "gap": "no check is required"}],
     "merge_settings": []},
    {"repo": "/checkouts/gamma", "slug": "group/gamma", "status": "unknown", "observed_at": "2026-09-04T08:00:00Z",
     "default_branch": None, "reason": "token does not reach it", "gaps": [], "merge_settings": [flag("rebase_merge", "allowed", True)]},
    {"repo": "/checkouts/delta", "slug": "group/delta", "status": "protected", "observed_at": "2026-09-05T08:00:00Z",
     "default_branch": "main", "reason": None, "gaps": [], "merge_settings": []},
]


def protection_of(rows):
    return lambda connection: rows


class TheDocument(ScreenCase):
    """`health_screen.document` from the fleet child and the trailer count."""

    def doc(self, trees=TREES, count=3, **kwargs):
        return health_screen.document(self.connection, now=NOW, fleet=kwargs.get("fleet") or fleet_of(trees),
                                      trailers=kwargs.get("trailers") or trailers_of(count),
                                      ports=kwargs.get("ports") or ports_snapshot,
                                      protection=kwargs.get("protection") or protection_of(PROTECTION))

    def test_every_design_area_is_there_in_order_and_says_whether_a_reader_covers_it(self):
        areas = self.doc()["areas"]
        self.assertEqual([(a["id"], a["read"]) for a in areas],
                         [("disk", False), ("cred", False), ("attr", True), ("wt", True),
                          ("br", False), ("dep", False), ("sec", False), ("ports", True), ("prot", True)])
        for area in areas:
            if area["id"] not in ("ports", "prot"):
                self.assertTrue(area["missing"], f"{area['id']} names nothing it does not read")
            if not area["read"]:
                self.assertEqual((area["rows"], area["error"], area["source"]), ([], "", None))

    def test_registrations_whose_directory_is_gone_are_one_row_per_checkout(self):
        wt = self.doc()["areas"][3]
        rows = {row["id"]: row for row in wt["rows"]}
        self.assertEqual(set(rows), {"gone:group/alpha", "unread:beta"})
        gone = rows["gone:group/alpha"]
        self.assertEqual((gone["state"], gone["type"], gone["repo_path"]), ("caution", "worktree registrations", "/checkouts/group/alpha"))
        self.assertEqual(gone["what"], "group/alpha: 2 worktrees registered, directory gone")
        self.assertEqual(gone["facts"]["Registered"], "2")
        self.assertEqual(gone["list"], ["/work/wt-a (feat/a)", "/work/wt-b (fix/b)"])
        unread = rows["unread:beta"]
        self.assertEqual((unread["state"], unread["type"], unread["list"]), ("unknown", "unread registrations", ["(unnamed) (?)"]))

    def test_a_fleet_with_every_registration_present_is_one_ok_row(self):
        (row,) = self.doc(trees=[TREES[3]])["areas"][3]["rows"]
        self.assertEqual((row["id"], row["state"], row["type"]), ("wt:ok", "ok", "check"))
        self.assertIn("checked 1 registration in 1 checkout under /checkouts", row["detail"])

    def test_the_trailer_count_is_a_caution_row_or_an_ok_check(self):
        (row,) = self.doc(count=3)["areas"][2]["rows"]
        self.assertEqual((row["id"], row["state"], row["type"], row["facts"]["Missing"]), ("attr:weeks", "caution", "attribution gap", "3"))
        self.assertEqual(row["what"], "3 of 10 of your commits in 5 weeks lack Authored-with")
        (row,) = self.doc(count=0)["areas"][2]["rows"]
        self.assertEqual((row["id"], row["state"], row["type"]), ("attr:ok", "ok", "check"))

    def test_a_repo_with_no_default_branch_or_no_author_is_named_in_its_own_row(self):
        rows = {row["id"]: row for row in self.doc(trailers=trailers_of(
            0, no_default=["/checkouts/alpha"], no_author=["/checkouts/beta"]))["areas"][2]["rows"]}
        self.assertEqual(list(rows), ["attr:ok", "attr:no_default", "attr:no_author"])
        self.assertEqual(rows["attr:no_default"]["state"], "unknown")
        self.assertIn("/checkouts/alpha", str(rows["attr:no_default"]))
        self.assertIn("remote set-head origin --auto", str(rows["attr:no_default"]))
        self.assertIn("/checkouts/beta", str(rows["attr:no_author"]))

    def test_a_reader_that_fails_is_its_area_error_and_the_other_still_answers(self):
        def broken(area):
            raise ValueError("fleet collection was stopped at its budget: sessions")
        areas = self.doc(fleet=broken)["areas"]
        self.assertEqual((areas[3]["error"], areas[3]["rows"]), ("fleet collection was stopped at its budget: sessions", []))
        self.assertEqual(areas[2]["rows"][0]["id"], "attr:weeks")
        areas = self.doc(fleet=lambda area: {"root": "/checkouts", "worktrees": [{"repo": "x"}]})["areas"]
        self.assertEqual(areas[3]["error"], "fleet collector returned an incomplete sessions document")

    def test_a_checkout_root_that_does_not_exist_is_not_read_rather_than_healthy(self):
        # The collector answers a missing REPO_ROOT with no worktrees, which would read as "checked 0 registrations".
        worktrees = self.doc(fleet=fleet_of([], root="/checkouts/missing", exists=False))["areas"][3]
        self.assertEqual(worktrees["rows"], [], "a missing checkout root was reported as a reading")
        self.assertEqual(worktrees["error"], "the checkout root /checkouts/missing does not exist; REPO_ROOT names it")

    def test_ports_are_the_operations_rows_with_its_counts_and_warnings(self):
        ports = self.doc()["areas"][7]
        rows = {row["id"]: row for row in ports["rows"]}
        self.assertEqual({key: (row["state"], row["type"], row["port"]) for key, row in rows.items()},
                         {"port:observed:5000": ("queued", "port", "5000"), "port:svc-a:9010": ("ok", "port", "9010"),
                          "port:svc-b:9020": ("unknown", "port", "9020")})
        self.assertEqual(rows["port:observed:5000"]["what"], ":5000 observed only")
        self.assertEqual(rows["port:svc-a:9010"]["facts"]["Process"], "svc (pid 42)")
        self.assertEqual(rows["port:svc-b:9020"]["facts"]["Process"], "Listener inspection unavailable")
        self.assertEqual(ports["extra"]["counts"], {"configured": 2, "listening": 2, "unknown": 1})
        self.assertEqual(ports["extra"]["warnings"], [
            "Listener state is unknown for 1 configured ports.",
            "The wider TCP listener inventory is incomplete or unavailable; positive observations remain visible."])
        self.assertEqual(ports["at"], NOW)

    def test_a_port_scan_over_its_budget_is_the_area_error_not_an_empty_inventory(self):
        def refused():
            raise health_screen.ports_screen.OverBudget("lsof ran past 5 s")
        areas = self.doc(ports=refused)["areas"]
        self.assertEqual(areas[7]["rows"], [])
        self.assertIn("exceeded its collection budget and was stopped rather than waited on: lsof ran past 5 s", areas[7]["error"])
        self.assertEqual(areas[3]["rows"][0]["id"], "gone:group/alpha")
        empty = self.doc(ports=lambda: {"services": [], "complete": False})["areas"][7]
        self.assertEqual(empty["error"], "port inventory is unavailable")

    def test_protection_cells_follow_the_classic_screen_and_an_unread_repo_has_none(self):
        prot = self.doc()["areas"][8]
        rows = {row["id"]: row for row in prot["rows"]}
        self.assertEqual(prot["extra"]["columns"],
                         ["prot:/checkouts/beta", "prot:/checkouts/gamma", "prot:/checkouts/alpha", "prot:/checkouts/delta"])
        self.assertEqual(prot["extra"]["counts"], {"unprotected": 1, "unknown": 1, "protected": 2})
        self.assertEqual(prot["extra"]["reasons"], ["token does not reach it"])
        self.assertEqual(prot["at"], "2026-09-05T08:00:00Z")
        self.assertEqual(len(prot["extra"]["checks"]), 11)
        cells = lambda key: {cell[0]: cell[1:] for cell in rows[key]["cells"]}
        alpha = cells("prot:/checkouts/alpha")
        self.assertEqual((alpha["Reviews"], alpha["Admins"], alpha["Squash message"], alpha["Requires ci"], alpha["Rebase merge"]),
                         (["gap", "no approving review is required"], ["ok"], ["ok"],
                          ["gap", "sd/local-gate · required_check sentence"], ["na"]))
        beta = cells("prot:/checkouts/beta")
        self.assertEqual((beta["Checks"], beta["Reviews"], beta["Admins"], beta["Bypass"]),
                         (["gap", "no check is required"], ["ok"], ["na"], ["na"]))
        # Unknown is not protected: no cell at all, not even the merge setting the body carries.
        self.assertEqual(rows["prot:/checkouts/gamma"]["cells"], [])
        self.assertEqual({key: (row["state"], row["what"]) for key, row in rows.items()}, {
            "prot:/checkouts/alpha": ("caution", "group/alpha: 2 gaps"),
            "prot:/checkouts/beta": ("warning", "/checkouts/beta: main is unprotected"),
            "prot:/checkouts/gamma": ("unknown", "group/gamma: protection not read"),
            "prot:/checkouts/delta": ("ok", "group/delta: protected, no gap")})
        self.assertEqual(rows["prot:/checkouts/beta"]["sentence"], "main has no protection")

    def test_an_empty_registry_is_one_unknown_row_not_a_protected_fleet(self):
        (row,) = self.doc(protection=protection_of([]))["areas"][8]["rows"]
        self.assertEqual((row["id"], row["state"]), ("prot:none", "unknown"))

    def test_the_default_protection_reader_shows_an_unobserved_repo_as_unknown(self):
        self.repo("/checkouts/never")
        doc = health_screen.document(self.connection, now=NOW, fleet=fleet_of([]), trailers=trailers_of(0), ports=ports_snapshot)
        (row,) = doc["areas"][8]["rows"]
        self.assertEqual((row["state"], row["reason"], row["cells"]), ("unknown", "not yet observed", []))

    def test_a_slow_git_walk_is_stopped_at_its_budget_and_is_the_attribution_error(self):
        stub = Path(self.tmp.name) / "bin"
        stub.mkdir()
        # exec: the process the budget kills is the one holding the pipes.
        (stub / "git").write_text("#!/bin/sh\nexec sleep 5\n")
        (stub / "git").chmod(0o755)
        for name in ("one", "two"):
            self.repo(f"/checkouts/{name}")
        with patch.dict(os.environ, {"PATH": f"{stub}{os.pathsep}{os.environ['PATH']}"}), \
                patch.object(health_screen, "TRAILER_SECONDS", 0.5, create=True):
            started = time.monotonic()
            doc = health_screen.document(self.connection, now=NOW, fleet=fleet_of([]), ports=ports_snapshot,
                                         protection=protection_of([]))
            elapsed = time.monotonic() - started
        attr = doc["areas"][2]
        self.assertLess(elapsed, 3, "the page waited on the git walk instead of stopping it")
        self.assertEqual((attr["rows"], attr["error"]), ([], "the trailer count ran past its budget of 0.5 seconds "
                                                             "and was stopped rather than waited on"))
        self.assertEqual(doc["areas"][3]["rows"][0]["id"], "wt:ok")

    def test_the_default_trailer_reader_counts_a_registered_repo(self):
        repo = Path(self.tmp.name) / "repo"
        env = {**os.environ, "GIT_AUTHOR_DATE": "2026-09-05T10:00:00Z", "GIT_COMMITTER_DATE": "2026-09-05T10:00:00Z",
               "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.test",
               "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.test"}
        subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.test"], check=True, env=env)
        for message in ("no trailer", "with trailer\n\nAuthored-with: human"):
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", message], check=True, env=env)
        # The default branch as a clone knows it: origin/HEAD, not the checkout's HEAD.
        subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", "HEAD"], check=True, env=env)
        subprocess.run(["git", "-C", str(repo), "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
                       check=True, env=env)
        self.repo(str(repo))
        doc = health_screen.document(self.connection, now=NOW, fleet=fleet_of([]), ports=ports_snapshot)
        self.assertEqual(doc["areas"][2]["rows"][0]["facts"]["Missing"], "1")


class ThePage(BrowserSession):
    fleet_backend = staticmethod(fleet_of(TREES))

    def setUp(self):
        super().setUp()
        # No lsof or docker runs in a test: the Ports area reads a fixture.
        self.listening.RequestHandlerClass.ports_backend = staticmethod(ports_snapshot)

    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/fleet-health")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Health · system</title>", body)
        self.assertRegex(body, r'<meta name="sd-csrf" content="[a-f0-9]{64}"></head>')
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"]+)"', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "icons.js", "sections.js", "health.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)
        # /health is the service's own check, which the runtime reads; the page does not take it over.
        status, _, body = self.request("/health")
        self.assertEqual(json.loads(body)["service"], "sd-dashboard")

    def test_the_data_route_needs_a_session_and_takes_no_query(self):
        self.assertEqual(self.request("/api/health")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/health?area=wt", headers=cookie)[0], 400)
        status, _, body = self.request("/api/health", headers=cookie)
        self.assertEqual(status, 200)
        doc = json.loads(body)
        self.assertRegex(doc["read"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual([row["id"] for row in doc["areas"][3]["rows"]], ["gone:group/alpha", "unread:beta"])
        self.assertEqual(len(doc["areas"][7]["rows"]), 3)

    def test_the_rail_opens_health_at_its_page(self):
        self.assertEqual(v2.SECTIONS["Health"], "/fleet-health")
        self.assertNotIn("Health", v2.CLASSIC)
        self.assertIn("/operations?area=sessions", v2.SCREENS.values())


# Extras the Health script reads from the shell that the Tasks stand-in does not carry.
HEALTH_SHELL = r"""
window.shell.shq = s => "'" + String(s).replace(/'/g, "'\\''") + "'";
window.shell.row = () => null;
window.shell.url = q => { OUT.url = String(q && q.toString ? q.toString() : q); };
window.shell.state = s => OUT.states.push(s);
"""


class TheScript(ScreenCase):
    """health.js against the document `health_screen` builds from the fixtures above."""

    def run_page(self, body, doc=None, status=200):
        doc = doc if doc is not None else health_screen.document(
            self.connection, now=NOW, fleet=fleet_of(TREES), trailers=trailers_of(3), ports=ports_snapshot,
            protection=protection_of(PROTECTION))
        script = (STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL + HEALTH_SHELL
                  + f"\nvar DOC = {json.dumps(doc)}, STATUS = {status};\n"
                  + "URLSearchParams.prototype.toString = function () { return ''; };\n"
                  + "ANSWER = (path, body) => path === '/api/health' ? [STATUS, DOC] : [404, { error: 'no answer' }];\n"
                  + HEALTH_JS + "\nvar R = {};\n(async () => { try {\n(WIN_LISTENERS.DOMContentLoaded || []).forEach(f => f());\n"
                  + "(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def test_every_command_is_registered_with_the_design_id_risk_and_key(self):
        out = self.run_page("""R.reg = REG.map(c => [c.id, c.on, c.risk, c.key || null, typeof c.executes === 'boolean' ? c.executes : null, !!c.bulk]);""")
        self.assertEqual(out["R"]["reg"], [
            ["check.recheck", "check", "safe", "e", False, False],
            ["attribution gap.attribute", "attribution gap", "safe", "a", False, False],
            ["worktree registrations.prune", "worktree registrations", "confirm", "p", False, True],
            ["port.inspect", "port", "safe", "i", False, False],
            ["protection.settings", "branch protection", "safe", "o", False, False],
            ["collector.sync", "collector", "safe", "r", False, False],
            ["worktree registrations.snooze", "worktree registrations", "undo", "z", None, True],
            ["unread registrations.snooze", "unread registrations", "undo", "z", None, True],
            ["attribution gap.snooze", "attribution gap", "undo", "z", None, True],
            ["port.snooze", "port", "undo", "z", None, True],
            ["branch protection.snooze", "branch protection", "undo", "z", None, True],
        ])

    def test_an_area_with_no_reader_is_an_unknown_lamp_that_names_what_it_does_not_read(self):
        out = self.run_page("R.lamps = ELS.annunciator.html; R.areas = ELS.areas.html; R.sub = ELS.subhead.html;")
        self.assertEqual(out["gets"], ["/api/health"])
        lamps = out["R"]["lamps"]
        for area in ("disk", "cred", "br", "dep", "sec"):
            self.assertRegex(lamps, rf'data-area="{area}" data-state="unknown"[^>]*>.*?no reader', area)
        self.assertRegex(lamps, r'data-area="wt" data-state="caution"[^>]*>.*?<b>2</b> dir gone')
        self.assertRegex(lamps, r'data-area="attr" data-state="caution"[^>]*>.*?<b>3</b> missing')
        self.assertRegex(lamps, r'data-area="attr" data-state="caution"[^>]*>.*?your commits · 5 weeks · default branch')
        areas = out["R"]["areas"]
        self.assertIn("<b>No reader yet.</b> The dashboard has no collector for this area, so nothing here is known: volume use (df -k)", areas)
        self.assertIn("<b>Not read here:</b> merged worktrees still on disk", areas)
        self.assertIn('data-id="gone:group/alpha"', areas)
        self.assertIn("9 areas · 1 warning, 3 caution rows want you · 5 areas with no reader yet", out["R"]["sub"])
        self.assertEqual(out["attention"][-1], {"state": "warning", "n": 1, "what": "findings want you"})
        self.assertIsNone(out["states"][-1])

    def test_a_failed_reader_is_a_partial_read_and_its_lamp_is_unknown(self):
        def broken(area):
            raise ValueError("fleet collection was stopped at its budget: sessions")
        doc = health_screen.document(self.connection, now=NOW, fleet=broken, trailers=trailers_of(0), ports=ports_snapshot,
                                     protection=protection_of([]))
        out = self.run_page("R.lamps = ELS.annunciator.html; R.areas = ELS.areas.html;", doc)
        self.assertRegex(out["R"]["lamps"], r'data-area="wt" data-state="unknown"[^>]*>.*?not read')
        self.assertIn("<b>Not read:</b> fleet collection was stopped at its budget: sessions", out["R"]["areas"])
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertEqual(out["attention"][-1], {"state": "ok", "n": 0, "what": "findings to watch"})

    def test_a_trailer_count_over_its_budget_shows_as_the_refusal_not_a_stall(self):
        def refused(connection, *, now):
            raise reads.OverBudget("the trailer count ran past its budget of 10 seconds")
        doc = health_screen.document(self.connection, now=NOW, fleet=fleet_of(TREES), trailers=refused, ports=ports_snapshot,
                                     protection=protection_of([]))
        out = self.run_page("R.lamps = ELS.annunciator.html; R.areas = ELS.areas.html;", doc)
        self.assertRegex(out["R"]["lamps"], r'data-area="attr" data-state="unknown"[^>]*>.*?not read')
        self.assertIn("<b>Not read:</b> the trailer count ran past its budget of 10 seconds and was stopped rather than "
                      "waited on", out["R"]["areas"])
        self.assertNotIn('data-id="attr:', out["R"]["areas"])
        self.assertEqual(out["states"][-1]["kind"], "partial")

    def test_a_document_that_does_not_arrive_leaves_no_row_and_says_so(self):
        out = self.run_page("""R.before = ELS.areas.html;
DOC = { error: 'Open a dashboard page before reading Health.' }; STATUS = 403;
ELS.annunciator.listeners.click[0]({ target: { closest: s => s === 'button.cell' ? { id: 'refresh', dataset: {} } : null } }); await flush();
R.areas = ELS.areas.html; R.lamps = ELS.annunciator.html;""")
        self.assertEqual(out["gets"], ["/api/health", "/api/health"])
        self.assertIn('data-id="gone:group/alpha"', out["R"]["before"])
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertIn("Open a dashboard page before reading Health.", out["states"][-1]["text"])
        self.assertEqual(out["attention"][-1], {"state": "unknown", "n": 0, "what": "rows"})
        self.assertNotIn("data-id=", out["R"]["areas"], "rows from the last read stayed on screen")
        self.assertIn("not read", out["R"]["lamps"])

    def test_the_filter_finds_a_row_by_a_path_in_its_facts_or_its_list(self):
        # The box says "repo, path, branch": a registered worktree's path is in `list`, the checkout's in `facts`.
        out = self.run_page("""const filter = v => { ELS.q.value = v; ELS.q.listeners.input[0](); return ELS.areas.html; };
R.list = filter('/work/wt-b'); R.facts = filter('/checkouts/group/alpha'); R.none = filter('/work/nowhere');""")
        self.assertIn('data-id="gone:group/alpha"', out["R"]["list"], "a path in the row's list did not match")
        self.assertIn('data-id="gone:group/alpha"', out["R"]["facts"], "a path in the row's facts did not match")
        self.assertNotIn('data-id="gone:group/alpha"', out["R"]["none"])

    def test_a_refresh_retires_the_command_objects_and_picks_of_rows_it_no_longer_lists(self):
        # The shell keeps every object put and every pick; the bulk bar must not offer a prune built from a row that is gone.
        out = self.run_page("""var PICKED = new Set();
C.pick = id => { PICKED.has(id) ? PICKED.delete(id) : PICKED.add(id); document.dispatchEvent(new CustomEvent('shell:picked', { detail: [...PICKED] })); };
const refresh = () => ELS.annunciator.listeners.click[0]({ target: { closest: s => s === 'button.cell' ? { id: 'refresh', dataset: {} } : null } });
const live = id => !!OBJ.get(id) && REG.some(c => c.on === OBJ.get(id).type);
C.pick('gone:group/alpha'); C.pick('unread:beta');
DOC = { ...DOC, areas: DOC.areas.map(a => ({ ...a, rows: a.rows.filter(r => r.id !== 'gone:group/alpha') })) };
refresh(); await flush();
R.dropped = [live('gone:group/alpha'), PICKED.has('gone:group/alpha')]; R.kept = [live('unread:beta'), PICKED.has('unread:beta')];
DOC = { error: 'Open a dashboard page before reading Health.' }; STATUS = 403;
refresh(); await flush();
R.failed = [live('unread:beta'), PICKED.has('unread:beta'), PICKED.size];""")
        self.assertEqual(out["R"]["dropped"], [False, False], "a row the refresh dropped kept its command object or pick")
        self.assertEqual(out["R"]["kept"], [True, True], "a row the refresh still lists lost its object or pick")
        self.assertEqual(out["R"]["failed"], [False, False, 0], "a failed refresh kept a row's command object or pick")

    def test_an_older_read_that_answers_last_does_not_replace_the_newer_one(self):
        # Two re-reads overlap on the threaded server and the first one answers last: once as a failed request, once
        # as an older reading.
        out = self.run_page("""var pending = [];
ANSWER = () => new Promise((answer, fail) => pending.push({ answer, fail }));
const refresh = () => ELS.annunciator.listeners.click[0]({ target: { closest: s => s === 'button.cell' ? { id: 'refresh', dataset: {} } : null } });
refresh(); refresh(); await flush();
pending[1].answer([200, DOC]); await flush();
pending[0].fail(new Error('an older read that failed')); await flush();
R.areas = ELS.areas.html;
refresh(); refresh(); await flush();
pending[3].answer([200, DOC]); await flush();
pending[2].answer([200, { ...DOC, read: '2026-09-01T00:00:00Z' }]); await flush();
R.observed = document.body.dataset.observed;""")
        self.assertEqual(out["gets"], ["/api/health"] * 5)
        self.assertNotEqual(out["R"]["observed"], "2026-09-01T00:00:00Z", "an older answer replaced the newer reading")
        self.assertNotIn("an older read that failed", json.dumps(out["states"]), "an older reading replaced the newer one")
        self.assertIsNone(out["states"][-1])
        self.assertIn('data-id="gone:group/alpha"', out["R"]["areas"])

    def test_a_lamp_shows_its_area_only_and_a_state_chip_narrows_the_rows(self):
        click = "ELS.{el}.listeners.click[0]({{ target: {{ closest: s => s === '{sel}' ? {{ id: '', dataset: {data} }} : null }} }});"
        out = self.run_page(click.format(el="annunciator", sel="button.cell", data="{ area: 'disk' }")
                            + "R.disk = ELS.areas.html; R.note = ELS.filtered.html;"
                            + click.format(el="annunciator", sel="button.cell", data="{ area: 'disk' }")
                            + click.format(el="filters", sel="[data-f]", data="{ f: 'state', v: 'unknown' }")
                            + "R.unknown = ELS.areas.html;")
        disk, unknown = out["R"]["disk"], out["R"]["unknown"]
        self.assertEqual(re.findall(r'<section class="area" id="a-(\w+)"', disk), ["disk"])
        self.assertIn("No row matches Disk only.", out["R"]["note"])
        self.assertEqual(re.findall(r'<section class="area" id="a-(\w+)"', unknown), ["wt", "ports", "prot"])
        self.assertEqual(re.findall(r'data-id="([^"]+)"', unknown), ["unread:beta", "port:svc-b:9020", "prot:/checkouts/gamma"])

    def test_a_second_lamp_adds_its_area_and_pressing_one_again_takes_it_out(self):
        click = "ELS.annunciator.listeners.click[0]({{ target: {{ closest: s => s === 'button.cell' ? {{ id: '', dataset: {{ area: '{a}' }} }} : null }} }});"
        out = self.run_page(click.format(a="wt") + click.format(a="ports") + "R.both = ELS.areas.html;"
                            + click.format(a="wt") + "R.one = ELS.areas.html;")
        self.assertEqual(re.findall(r'<section class="area" id="a-(\w+)"', out["R"]["both"]), ["wt", "ports"])
        self.assertEqual(re.findall(r'<section class="area" id="a-(\w+)"', out["R"]["one"]), ["ports"])

    def test_ports_show_the_counts_line_and_warnings_and_protection_has_no_lamp(self):
        out = self.run_page("R.lamps = ELS.annunciator.html; R.areas = ELS.areas.html;")
        lamps, areas = out["R"]["lamps"], out["R"]["areas"]
        self.assertRegex(lamps, r'data-area="ports" data-state="unknown"[^>]*>.*?<b>1</b> unknown</span> · <span class="ph">2 listening')
        self.assertNotIn('data-area="prot"', lamps)
        self.assertIn('<p class="counts">2 configured ports · 2 listening · 1 unknown</p>', areas)
        self.assertIn("Listener state is unknown for 1 configured ports.", areas)

    def test_the_matrix_is_one_column_per_repo_and_an_unread_column_draws_no_cell(self):
        out = self.run_page("R.areas = ELS.areas.html;")
        areas = out["R"]["areas"]
        cols = re.findall(r'<button type="button" class="pmx-col" data-row="([^"]+)" data-status="(\w+)"[^>]*>(.*?)</button>', areas, re.S)
        self.assertEqual([(row, status) for row, status, _ in cols], [
            ("prot:/checkouts/beta", "unprotected"), ("prot:/checkouts/gamma", "unknown"),
            ("prot:/checkouts/alpha", "protected"), ("prot:/checkouts/delta", "protected")])
        svg = {row: body for row, _, body in cols}
        self.assertNotIn("<rect", svg["prot:/checkouts/gamma"])
        self.assertEqual(svg["prot:/checkouts/alpha"].count('class="c-gap"'), 2)
        self.assertEqual(svg["prot:/checkouts/beta"].count('class="c-gap"'), 1)
        self.assertEqual(svg["prot:/checkouts/delta"].count('class="c-gap"'), 0)
        self.assertIn('aria-label="/checkouts/beta: unprotected, 1 gap"', areas)
        self.assertIn('aria-label="group/gamma: unknown"', areas)
        self.assertIn("1 unknown, all for one reason: token does not reach it.", areas)
        self.assertIn('<p class="counts">2 protected · 1 unprotected · 1 unknown · ', areas)
        # The table fallback carries the same cells: GAP with its sentence, ok, and — where a check does not apply.
        table = areas[areas.index('<details class="pmx-table">'):]
        self.assertIn("<summary>Table: 4 repositories × 11 checks</summary>", table)
        self.assertIn('<a href="https://github.com/group/alpha/settings/branches" target="_blank" rel="noopener noreferrer">group/alpha</a>', table)
        self.assertIn("<b>GAP</b> <span class=\"sent\">no approving review is required</span>", table)
        self.assertIn("<th scope=\"row\">/checkouts/beta</th>", table)
        self.assertEqual(table.count("<td>token does not reach it</td>"), 1)
        # An unread repository has no cells: each of its checks reads "not read", never — (does not apply).
        gamma = re.search(r'<tr><th scope="row"><a [^>]*>group/gamma</a></th>.*?</tr>', table, re.S).group(0)
        self.assertEqual(gamma.count("<td>not read</td>"), 11, gamma)
        self.assertEqual(gamma.count("<td>—</td>"), 1, "only the Branch cell, which an unread repository has none of")
        self.assertIn("— does not apply; not read where the repository was not read.", table)

    def test_a_matrix_column_opens_its_repo_with_every_check_in_details(self):
        out = self.run_page("""ELS.areas.listeners.click[0]({ target: { closest: s => s === '.pmx-col' ? { dataset: { row: 'prot:/checkouts/alpha' } } : null } });
R.d = ELS.details.html; R.cli = cmd('protection.settings').cli(C.get('prot:/checkouts/alpha'));
R.off = cmd('protection.settings').when(C.get('prot:/checkouts/beta')); R.sync = cmd('collector.sync').cli(C.get('collector:protection'));
ELS.areas.listeners.click[0]({ target: { closest: s => s === '.pmx-col' ? { dataset: { row: 'prot:/checkouts/gamma' } } : null } });
R.unread = ELS.details.html; R.lsof = cmd('port.inspect').cli(C.get('port:svc-a:9010'));""")
        details = out["R"]["d"]
        self.assertIn("<h2>group/alpha: 2 gaps</h2>", details)
        self.assertIn('<dt>Reviews</dt><dd class="c-gap">GAP · no approving review is required</dd>', details)
        self.assertIn('<dt>Admins</dt><dd class="c-ok">ok</dd>', details)
        self.assertIn('<dt>Rebase merge</dt><dd class="c-na">—</dd>', details)
        self.assertIn("group/alpha · settings/branches", details)
        self.assertEqual(out["R"]["cli"], "open https://github.com/group/alpha/settings/branches")
        self.assertEqual(out["R"]["off"], "no github.com remote: there is no settings page")
        self.assertEqual(out["R"]["sync"], "sd shadow sync")
        self.assertEqual(out["R"]["lsof"], "lsof -nP -iTCP:9010 -sTCP:LISTEN")
        self.assertIn("No cell is shown: token does not reach it.", out["R"]["unread"])
        self.assertNotIn("<dl class=\"checks\">", out["R"]["unread"])

    def test_prune_confirms_first_is_copy_only_and_posts_nothing(self):
        out = self.run_page("""var o = C.get('gone:group/alpha'); R.cli = cmd('worktree registrations.prune').cli(o);
R.why = cmd('worktree registrations.prune').consequence(o); R.snooze = cmd('worktree registrations.snooze').when(o);
shellRun(cmd('worktree registrations.prune'), o); await flush();""")
        self.assertEqual(out["R"]["cli"], "git -C '/checkouts/group/alpha' worktree prune -v")
        self.assertEqual(out["R"]["why"], "Removes 2 worktree registrations whose directories are gone. No directory is touched.")
        self.assertEqual(out["R"]["snooze"], "no CLI verb: sd has no snooze")
        self.assertEqual(out["confirms"], ["worktree registrations.prune"])
        self.assertEqual(out["posts"], [])
        self.assertEqual(out["toasts"][-1][0], "Not run here: copy the line from Details and run it in a terminal · "
                                               "group/alpha: 2 worktrees registered, directory gone")

    def test_details_show_the_facts_and_the_registrations_behind_a_row(self):
        out = self.run_page("document.dispatchEvent(new CustomEvent('shell:open', { detail: 'gone:group/alpha' })); R.d = ELS.details.html;")
        details = out["R"]["d"]
        self.assertIn("<h2>group/alpha: 2 worktrees registered, directory gone</h2>", details)
        self.assertIn("<dt>Repo</dt><dd>/checkouts/group/alpha</dd>", details)
        self.assertIn("<li>/work/wt-a (feat/a)</li>", details)
        self.assertIn("<dt>Source</dt><dd>the fleet&#39;s .git/worktrees registrations</dd>", details)

    def test_the_first_read_opens_the_first_row_in_details(self):
        out = self.run_page("R.d = document.getElementById('details').html || '';")
        self.assertIn("<h2>3 of 10 of your commits in 5 weeks lack Authored-with</h2>", out["R"]["d"],
                      "Details still say the fleet is being read after the rows arrived")

    def test_the_script_adds_no_sink_and_no_inline_style(self):
        self.assertNotIn("innerHTML", HEALTH_JS)
        self.assertNotRegex(HEALTH_JS, r"style=\\?\"")
        self.assertEqual(re.findall(r"window\.markup\b", re.sub(r"const \{ [\w, ]+ \} = window\.markup;", "", HEALTH_JS)), [])
        # The shell owns j / k and Esc through PAGE_LIST; a page-level handler is drift.
        self.assertNotRegex(HEALTH_JS, r"e\.key === '[jk]'")


if __name__ == "__main__":
    unittest.main()
