"""sd:234 criterion 19, the dashboard half through HTTP: a review on a skill in
each of the catalog's three states, a fixture reviewer's proposals on the item
page, and one apply assignment from two accepted proposals."""
import inspect
import json
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

from sd_db import registry, skills_catalog
from sd_db.writes import upsert_repo
from sd_dashboard import server, skills_screen

from .test_controls_actions import REGISTRY
from .test_workflow_actions import BrowserSession


class SkillStates(BrowserSession):
    """The library queues intent only, so the pack checkout is a real git
    repository whose skill commit carries the `Authored-with:` trailer
    `skills_catalog._authorship` reads; `skills/sd-onpath` is on the `build`
    path, `contrib/sd-trial` is put on trial through the route, and
    `contrib/sd-contrib` is neither."""

    SKILL = "---\nname: {name}\ndescription: A fixture skill.\n---\n## When to use\nFor a bounded fixture.\n"

    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name).resolve()
        provider_file = root / "providers.yaml"; provider_file.write_text(REGISTRY)
        original = registry.read
        replacement = patch.object(registry, "read", side_effect=lambda path=None, **kw: original(path or provider_file, **kw))
        replacement.start(); self.addCleanup(replacement.stop)
        self.pack = root / "pack"
        for name, source in (("sd-onpath", "skills"), ("sd-trial", "contrib"), ("sd-contrib", "contrib")):
            (self.pack / source / name).mkdir(parents=True)
            (self.pack / source / name / "SKILL.md").write_text(self.SKILL.format(name=name))
        (self.pack / "skills/paths.json").write_text(json.dumps({"paths": {"build": {"skills": ["sd-onpath"]}}}))
        self.git("init", "-b", "main"); self.git("config", "user.name", "Fixture"); self.git("config", "user.email", "fixture@example.invalid")
        self.git("add", "."); self.git("commit", "-m", "Fixture\n\nAuthored-with: codex/openai")
        upsert_repo(self.connection, str(self.pack), remote="git@example.invalid:pack.git")
        replacement = patch.object(skills_catalog, "location", return_value=(self.pack, {}))
        replacement.start(); self.addCleanup(replacement.stop)
        self.assertEqual(self.post("/api/skills/sd-trial/try", {"revision": self.skill("sd-trial")["revision"]})[0], 200)
        self.assertEqual({name: self.skill(name)["status"] for name in ("sd-onpath", "sd-trial", "sd-contrib")},
                         {"sd-onpath": "path", "sd-trial": "trial", "sd-contrib": "contrib"})

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.pack), *args], check=True, capture_output=True, text=True).stdout.strip()

    def skill(self, name):
        return next(skill for skill in skills_catalog.catalog(self.connection)["skills"] if skill["name"] == name)

    def act(self, name, action, **payload):
        return self.post(f"/api/skills/{name}/{action}", {"revision": self.skill(name)["revision"], **payload})

    def reviewed(self, name):
        """A review queued through HTTP and answered by a fixture reviewer:
        the assignment is marked running for `claude`, and two proposals are
        recorded the way the runner records them."""
        status, _, state = self.act(name, "review")
        self.assertEqual(status, 200, state)
        item, assignment = state["item"]["id"], state["assignments"][0]["id"]
        registry.seed(self.connection, registry.read(connection=self.connection))
        self.connection.execute("UPDATE assignment SET status='running',provider='claude' WHERE id=?", (assignment,))
        self.connection.commit()
        source = json.loads(state["item"]["fields"])["skill_review"]
        proposals = [{"path": source["path"], "line_start": 5, "line_end": 6, "body": "Clarify entry condition"},
                     {"path": source["path"], "line_start": 6, "line_end": 6, "body": "Give a bounded example"}]
        document = {"version": 1, "item": item, "source_sha256": source["source_sha256"], "proposals": proposals}
        state = skills_catalog.record_review_proposals(self.connection, item, assignment, "claude", document)
        return item, assignment, proposals, state

    def test_a_review_queues_one_reviewer_assignment_in_every_skill_state(self):
        before_git = self.git("status", "--porcelain")
        for name in ("sd-onpath", "sd-trial", "sd-contrib"):
            status, _, state = self.act(name, "review")
            self.assertEqual(status, 200, state)
            self.assertEqual(state["item"]["kind"], "skill-review", name)
            self.assertEqual(state["item"]["title"], f"Review {name}")
            self.assertEqual([(row["role"], row["scope"]) for row in state["assignments"]], [("reviewer", "skill-review")], name)
        self.assertEqual(self.git("status", "--porcelain"), before_git)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM item WHERE kind='skill-review'").fetchone()[0], 3)

    def test_the_item_page_offers_each_recorded_proposal_under_one_apply_form(self):
        item, _, proposals, _ = self.reviewed("sd-trial")
        page = self.request(f"/item/{item}")[2]
        forms = re.findall(rf'<form[^>]*action="/api/skill-reviews/{item}/apply"[^>]*>.*?</form>', page, re.S)
        self.assertEqual(len(forms), 1, page)
        boxes = re.findall(r'<input[^>]*type="checkbox"[^>]*name="notes"[^>]*>', forms[0])
        self.assertEqual(len(boxes), 2)
        self.assertIn("Accept and apply selected", forms[0])
        for proposal in proposals:
            self.assertIn(f"{proposal['path']}:{proposal['line_start']}–{proposal['line_end']} · {proposal['body']}", forms[0])

    def test_applying_both_proposals_queues_one_apply_assignment_carrying_exactly_them(self):
        item, assignment, proposals, state = self.reviewed("sd-contrib")
        self.connection.execute("UPDATE assignment SET status='done' WHERE id=?", (assignment,)); self.connection.commit()
        notes = [note["id"] for note in state["notes"] if note["kind"] == "proposal"]
        status, _, result = self.post(f"/api/skill-reviews/{item}/apply", {"revision": state["revision"], "notes": notes})
        self.assertEqual(status, 200, result)
        self.assertEqual([(row["role"], row["scope"]) for row in result["assignments"]], [("author", "skill-apply")])
        self.assertEqual(self.connection.execute("SELECT count(*) FROM assignment WHERE scope='skill-apply'").fetchone()[0], 1)
        self.assertEqual(result["item"]["kind"], "task")
        text = json.loads(result["item"]["body"])["text"]
        self.assertEqual(json.loads(text.split("\n\n", 1)[1]),
                         [{"note": note, "assignment": assignment, "provider": "claude", **proposal} for note, proposal in zip(notes, proposals)])

    def test_promote_and_demote_refuse_the_wrong_state_and_queue_a_task_for_the_right_one(self):
        before = self.snapshot()
        status, _, result = self.act("sd-onpath", "promote", path_name="build")
        self.assertEqual((status, result.get("error")), (400, "choose a declared path for a contrib skill"))
        status, _, result = self.act("sd-trial", "demote")
        self.assertEqual((status, result.get("error")), (400, "only a skill on a path can be demoted"))
        self.assertEqual(before, self.snapshot())
        status, _, result = self.act("sd-trial", "promote", path_name="build")
        self.assertEqual(status, 200, result)
        self.assertEqual((result["item"]["kind"], result["item"]["title"]), ("task", "Promote sd-trial"))
        self.assertEqual([(row["role"], row["scope"]) for row in result["assignments"]], [("author", "skill-apply")])
        status, _, result = self.act("sd-onpath", "demote")
        self.assertEqual(status, 200, result)
        self.assertEqual((result["item"]["kind"], result["item"]["title"]), ("task", "Demote sd-onpath"))
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_the_skill_arms_of_the_dashboard_call_no_git_or_gh(self):
        """Criterion 13's `no git or gh call`, narrowed the way the pack's
        #775 narrowed it: the screen and the two route arms name none of
        the mutation surfaces. The arms are cut from the route function at
        its `match =`/`if path ==` lines, so a reordered route still lands in
        the right segment."""
        source = inspect.getsource(server)
        arms = [segment for segment in re.split(r"\n    (?=match = re\.fullmatch\(|if path == )", source)
                if re.match(r'(?:match = re\.fullmatch\(|if path == )r?"/api/skill', segment)]
        self.assertEqual(len(arms), 2, [arm.splitlines()[0] for arm in arms])
        banned = re.compile(r"--method|git push|pr create|subprocess")
        for text in [inspect.getsource(skills_screen), *arms]:
            self.assertIn("skills_catalog", text)
            self.assertIsNone(banned.search(text), text)
