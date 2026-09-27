"""github-rulesets.sh against a stub `gh` on PATH; no call leaves the machine."""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = pathlib.Path(__file__).resolve().parent
TOOL = HERE.parent / "github-rulesets.sh"
REPO_ROOT = HERE.parent.parent
REPO = "example-org/example-repo"

# Serves GETs from $GH_STUB_DIR: the list call from list.json, a single
# ruleset from <id>.json. Every call, with its stdin, is logged as one JSON
# line to $GH_STUB_DIR/calls.log. A write returns {}.
STUB = textwrap.dedent(
    """\
    import json, os, pathlib, sys
    root = pathlib.Path(os.environ["GH_STUB_DIR"])
    args = sys.argv[1:]
    body = sys.stdin.read()
    with open(root / "calls.log", "a") as log:
        log.write(json.dumps({"args": args, "stdin": body}) + "\\n")
    if args[0] != "api":
        sys.exit("stub gh: only api is served")
    if "--method" in args:
        print("{}")
        sys.exit(0)
    path = args[1].split("?")[0]
    name = "list.json" if path.endswith("/rulesets") else path.rsplit("/", 1)[1] + ".json"
    try:
        sys.stdout.write((root / name).read_text())
    except FileNotFoundError:
        sys.exit("stub gh: HTTP 404 for " + args[1])
    """
)


def live(ruleset_id, data, **extra):
    """A ruleset as a GET returns it: the file plus the read-only fields."""
    out = dict(data)
    out.update(
        id=ruleset_id,
        node_id="RRS_x%d" % ruleset_id,
        source_type="Repository",
        source=REPO,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        current_user_can_bypass="never",
        _links={"self": {"href": "https://api.example.test/%d" % ruleset_id}},
    )
    out.update(extra)
    return out


class RulesetsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.stub = self.tmp / "stub"
        self.stub.mkdir()
        bindir = self.tmp / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text("#!%s\n%s" % (sys.executable, STUB))
        gh.chmod(0o755)
        self.files = self.tmp / "rulesets"
        shutil.copytree(REPO_ROOT / ".github" / "rulesets", self.files)
        self.env = dict(os.environ, PATH="%s%s%s" % (bindir, os.pathsep, os.environ["PATH"]),
                        GH_STUB_DIR=str(self.stub), GITHUB_RULESETS_DIR=str(self.files))
        self.env.pop("PYTHON", None)
        self.env["PYTHON"] = sys.executable
        self.committed = {
            p.name: json.loads(p.read_text()) for p in sorted(self.files.glob("*.json"))
        }

    def serve(self, rulesets):
        listed = [{k: r[k] for k in ("id", "name", "source_type", "source")} for r in rulesets]
        (self.stub / "list.json").write_text(json.dumps(listed))
        for r in rulesets:
            (self.stub / ("%d.json" % r["id"])).write_text(json.dumps(r))

    def run_tool(self, *args):
        return subprocess.run(["sh", str(TOOL), *args], env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60)

    def calls(self):
        log = self.stub / "calls.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def writes(self):
        return [c for c in self.calls() if "--method" in c["args"]]

    def serve_matching(self):
        rulesets = [live(100 + i, data) for i, data in enumerate(self.committed.values())]
        self.serve(rulesets)
        return rulesets

    # --- entrypoint conventions -------------------------------------------

    def test_no_argument_prints_usage_and_exits_1(self):
        proc = self.run_tool()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Usage:", proc.stderr)

    def test_help_exits_0(self):
        for flag in ("-h", "--help", "help"):
            proc = self.run_tool(flag)
            self.assertEqual(proc.returncode, 0, flag)
            self.assertIn("diff OWNER/REPO", proc.stderr)

    def test_a_malformed_repository_is_refused(self):
        proc = self.run_tool("diff", "not-a-repo")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.calls(), [])

    # --- committed files ----------------------------------------------------

    def test_committed_files_hold_only_writable_fields(self):
        self.assertTrue(self.committed)
        for name, data in self.committed.items():
            self.assertLessEqual(
                set(data), {"name", "target", "enforcement", "conditions", "rules", "bypass_actors"}, name
            )
            for actor in data.get("bypass_actors", []):
                self.assertNotIn(actor["actor_type"], ("User", "Team"), name)

    def test_required_contexts_name_jobs_the_workflows_define(self):
        workflows = "".join(
            p.read_text() for p in (REPO_ROOT / ".github" / "workflows").glob("*.yml")
        )
        legs = []
        for line in workflows.splitlines():
            stripped = line.strip()
            if stripped.startswith("leg: ["):
                legs = [leg.strip() for leg in stripped[len("leg: ["):-1].split(",")]
        names = {line.split("name:", 1)[1].strip() for line in workflows.splitlines()
                 if line.startswith("    name:")}
        jobs = set()
        for name in names:
            if "${{ matrix.leg }}" in name:
                jobs.update(name.replace("${{ matrix.leg }}", leg) for leg in legs)
            else:
                jobs.add(name)
        for data in self.committed.values():
            for rule in data["rules"]:
                for check in rule.get("parameters", {}).get("required_status_checks", []):
                    self.assertIn(check["context"], jobs)

    # --- diff ----------------------------------------------------------------

    def test_diff_reports_no_drift_when_live_matches(self):
        self.serve_matching()
        proc = self.run_tool("diff", REPO)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.count("same: "), len(self.committed))
        self.assertEqual(self.writes(), [])

    def test_diff_ignores_order_github_may_list_in(self):
        rulesets = self.serve_matching()
        for r in rulesets:
            r["rules"] = list(reversed(r["rules"]))
            for rule in r["rules"]:
                checks = rule.get("parameters", {}).get("required_status_checks")
                if checks:
                    checks.reverse()
        self.serve(rulesets)
        proc = self.run_tool("diff", REPO)
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_diff_shows_a_drifted_rule(self):
        rulesets = self.serve_matching()
        rulesets[0]["enforcement"] = "disabled"
        self.serve(rulesets)
        proc = self.run_tool("diff", REPO)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("differs: %s" % rulesets[0]["name"], proc.stdout)
        self.assertIn('-  "enforcement": "disabled"', proc.stdout)
        self.assertIn('+  "enforcement": "active"', proc.stdout)
        self.assertEqual(self.writes(), [])

    def test_diff_reports_missing_and_extra(self):
        rulesets = self.serve_matching()
        extra = live(999, {"name": "someone else's rule", "target": "tag", "enforcement": "active",
                           "conditions": {}, "rules": [], "bypass_actors": []})
        self.serve(rulesets[1:] + [extra])
        proc = self.run_tool("diff", REPO)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("missing: %s" % rulesets[0]["name"], proc.stdout)
        self.assertIn("extra: someone else's rule", proc.stdout)

    def test_diff_skips_rulesets_inherited_from_the_organisation(self):
        rulesets = self.serve_matching()
        org = live(500, {"name": "org rule", "target": "branch", "enforcement": "active",
                         "conditions": {}, "rules": [], "bypass_actors": []},
                   source_type="Organization", source="example-org")
        self.serve(rulesets + [org])
        proc = self.run_tool("diff", REPO)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertNotIn("org rule", proc.stdout)

    def test_diff_exits_2_when_gh_fails(self):
        proc = self.run_tool("diff", REPO)  # nothing served: the stub 404s
        self.assertEqual(proc.returncode, 2)
        self.assertIn("HTTP 404", proc.stderr)

    # --- apply ---------------------------------------------------------------

    def test_apply_is_a_dry_run_by_default(self):
        rulesets = self.serve_matching()
        rulesets[0]["enforcement"] = "evaluate"
        self.serve(rulesets[:1])
        for args in (("apply", REPO), ("apply", REPO, "--dry-run")):
            proc = self.run_tool(*args)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("update: gh api --method PUT repos/%s/rulesets/100" % REPO, proc.stdout)
            self.assertIn("create: gh api --method POST repos/%s/rulesets" % REPO, proc.stdout)
            self.assertIn("dry run: nothing changed", proc.stdout)
        self.assertEqual(self.writes(), [])

    def test_apply_creates_updates_and_never_deletes(self):
        rulesets = self.serve_matching()
        rulesets[0]["enforcement"] = "evaluate"
        extra = live(999, {"name": "keep me", "target": "tag", "enforcement": "active",
                           "conditions": {}, "rules": [], "bypass_actors": []})
        self.serve([rulesets[0], extra])
        proc = self.run_tool("apply", REPO, "--apply")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("extra: keep me (live only; not deleted)", proc.stdout)
        writes = self.writes()
        methods = sorted((c["args"][c["args"].index("--method") + 1], c["args"][3]) for c in writes)
        self.assertEqual(
            methods,
            [("POST", "repos/%s/rulesets" % REPO), ("PUT", "repos/%s/rulesets/100" % REPO)],
        )
        for call in writes:
            body = json.loads(call["stdin"])
            self.assertEqual(set(body), {"name", "target", "enforcement", "conditions", "rules", "bypass_actors"})
            self.assertEqual(body["enforcement"], "active")
            self.assertNotIn("id", body)

    def test_apply_with_no_drift_writes_nothing(self):
        self.serve_matching()
        proc = self.run_tool("apply", REPO, "--apply")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.writes(), [])

    # --- export ---------------------------------------------------------------

    def test_export_normalises_and_drops_personal_bypass_actors(self):
        data = dict(next(iter(self.committed.values())))
        data["bypass_actors"] = [
            {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"},
            {"actor_id": 4242, "actor_type": "User", "bypass_mode": "always"},
            {"actor_id": 77, "actor_type": "Team", "bypass_mode": "pull_request"},
        ]
        self.serve([live(300, data)])
        out = self.tmp / "exported"
        proc = self.run_tool("export", REPO, "--dir", str(out))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("dropped bypass actor from %s: User 4242" % data["name"], proc.stderr)
        self.assertIn("Team 77", proc.stderr)
        (written,) = out.glob("*.json")
        body = json.loads(written.read_text())
        self.assertEqual(body["bypass_actors"], [data["bypass_actors"][0]])
        for key in ("id", "node_id", "created_at", "updated_at", "_links", "source", "source_type",
                    "current_user_can_bypass"):
            self.assertNotIn(key, body)
        self.assertEqual(self.writes(), [])


if __name__ == "__main__":
    unittest.main()
