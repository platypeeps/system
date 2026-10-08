"""The optional Jev judgment: which passes a prd actually needs.

Everything here is offline. `SD_PLAN_JEV` replaces Jev's argv the way
`SD_PLAN_AGENT` replaces the agent's, so the stub answers `enabled` with an
exit code and `ask` with the JSON the real `ask` prints -- no key, no network,
and `tests/ci-native.sh` treats a skipped test as a failure, so "skip when offline"
was never available.

What these cases are really about is the contract in `CLAUDE.md`'s "Nothing
may depend on Jev": with `JEV_SD_PLAN` switching the stage off, with `jev off`,
and with Jev failing mid-run, this tool writes exactly what it wrote before.
The judgment is the one path that behaves differently, and since the flip it
is the path an unset variable takes -- unset means on.
"""

import json
import os
import subprocess
import unittest
from unittest import mock
from pathlib import Path

import sd_plan

from tests.test_item import ItemCase

#: Answers the two verbs `sd_plan` asks for, and records what it was asked so
#: a case can assert the payload that went out -- which is the half of the
#: contract a real call could not show either.
JEV = """\
#!/bin/sh
set -e
verb="$1"
shift
dir="$JEV_STUB_DIR"
printf '%%s %%s run=%%s\n' "$verb" "$*" "${JEV_RUN:-}" >> "$dir/calls"
case "$verb" in
  enabled)
    # `enabled STAGE` answers both halves, as the real one does: can Jev
    # answer here (the %(enabled)s baked in by the fixture), and has the named
    # stage variable been used to switch this stage off. Unset means on, and
    # only the FLAG_OFF words switch it off -- an unrecognised word leaves the
    # stage on, so a typo cannot silently stop a lane.
    if [ "%(enabled)s" != 0 ]; then exit %(enabled)s; fi
    if [ -n "${1:-}" ]; then
      eval "word=\\${$1:-}"
      case "$(printf '%%s' "$word" | tr '[:upper:]' '[:lower:]')" in
        0|off|false|no|disabled) exit 3 ;;
      esac
    fi
    exit 0
    ;;
  ask)
    cat > "$dir/questions.json"
    prev=""
    for arg in "$@"; do
      if [ "$prev" = "--state" ]; then cp "$arg" "$dir/state.txt"; fi
      prev="$arg"
    done
    %(answer)s
    exit 0
    ;;
  *)
    exit 1
    ;;
esac
"""

#: Writes what the run asked it for, and nothing else. A real `/sd-plan` reads
#: the sentence in the prompt; a double reads `SD_PLAN_DOCUMENTS`, which
#: carries the same list without a model in between.
AGENT = """\
#!/bin/sh
set -e
slug="$1"
mkdir -p "docs/work/$slug"
printf '%s\\n' "$SD_PLAN_DOCUMENTS" >> "$JEV_STUB_DIR/agent-runs"
for name in $SD_PLAN_DOCUMENTS; do
  if [ ! -f "docs/work/$slug/$name" ]; then
    printf '%s\\n' "---" "title: $slug" "created: 2026-09-11" "---" "" "# $name" \
      > "docs/work/$slug/$name"
  fi
done
"""


class JevCase(ItemCase):
    def setUp(self):
        super().setUp()
        self.stub = self.home / "jev-stub"
        self.stub.mkdir()

    def jev(self, *, enabled=0, answer=None, design=0.9, implement=0.9):
        """A Jev double. `answer` replaces the `ask` body, e.g. to fail."""
        if answer is None:
            payload = json.dumps({"answers": {
                "design": {"noul": design},
                "implement": {"noul": implement},
            }, "model": "jev-stub"})
            answer = "cat <<'JSON'\n%s\nJSON" % payload
        path = self.home / "jev.sh"
        path.write_text(JEV % {"enabled": enabled, "answer": answer}, encoding="utf-8")
        path.chmod(0o755)
        return path

    def obedient_agent(self):
        path = self.home / "obedient-agent.sh"
        path.write_text(AGENT, encoding="utf-8")
        path.chmod(0o755)
        return path

    def run_plan(self, *, stage_off=False, jev=None, agent=None, expect=0, **kwargs):
        # The stage is on unless a case switches it off: unset is the default
        # and the default is on, so the fixture exports nothing by default.
        environment = {"JEV_STUB_DIR": str(self.stub)}
        if stage_off:
            environment[sd_plan.JEV_STAGE] = "0"
        if jev is not None:
            environment["SD_PLAN_JEV"] = str(jev)
        environment.update(kwargs)
        identifier = self.task()
        done = self.plan(str(identifier), expect=expect,
                         agent=agent or self.obedient_agent(), env=environment)
        return identifier, done

    def folder(self):
        items = sorted((self.repo / "docs" / "work").iterdir())
        self.assertEqual(len(items), 1, items)
        return items[0]

    def documents(self):
        return sorted(path.name for path in self.folder().iterdir())

    def agent_runs(self):
        log = self.stub / "agent-runs"
        return log.read_text(encoding="utf-8").split("\n")[:-1] if log.is_file() else []

    def message(self):
        return self.git("log", "-1", "--format=%B")


class TheSwitch(JevCase):
    """Unset is the whole default, and since the flip the default is on."""

    def test_an_unset_switch_asks_jev(self):
        """Nothing exported, so the judgment runs. This is the flip."""
        self.run_plan(jev=self.jev())
        self.assertTrue((self.stub / "questions.json").is_file(),
                        "an unset JEV_SD_PLAN did not reach jev")

    def test_the_switch_off_asks_nothing_and_writes_all_three(self):
        _, done = self.run_plan(stage_off=True, jev=self.jev())
        self.assertEqual(self.documents(), ["design.md", "implement.md", "prd.md"])
        # One planning run, naming all three, exactly as before: the split
        # into two runs is the judged path's and nothing else's.
        self.assertEqual(self.agent_runs(), ["prd.md design.md implement.md"])
        # And Jev was asked no question -- `enabled` calls nothing, `ask` does.
        self.assertFalse((self.stub / "questions.json").is_file())
        self.assertNotIn("Jev:", self.message())
        self.assertNotIn("jev", done.stdout)

    def test_every_off_word_switches_the_stage_off(self):
        # The whole FLAG_OFF vocabulary, and case does not matter. A caller
        # who wrote `off` and got a lane that kept running is the failure
        # this asserts against.
        for value in ("0", "off", "false", "no", "disabled", "OFF", "No"):
            with self.subTest(value=value):
                self.setUp()
                self.run_plan(jev=self.jev(), **{sd_plan.JEV_STAGE: value})
                self.assertEqual(self.documents(),
                                 ["design.md", "implement.md", "prd.md"])
                self.assertFalse((self.stub / "questions.json").is_file())

    def test_a_word_that_is_not_an_off_word_leaves_the_stage_on(self):
        # A typo must not silently stop the lane, so only FLAG_OFF counts.
        # An empty value is unset, which is on.
        for value in ("yes", "", "1", "maybe"):
            with self.subTest(value=value):
                self.setUp()
                self.run_plan(jev=self.jev(), **{sd_plan.JEV_STAGE: value})
                self.assertTrue((self.stub / "questions.json").is_file(),
                                f"{value!r} was read as a switch-off")

    def test_the_default_prompt_is_the_one_it_always_was(self):
        """The sentence the agent reads, byte for byte, on the default path.

        The citation rules sit between the two sentences since sd:990; the
        prompt tests in `test_item.py` pin their wording.
        """
        row = {"id": 42}
        with mock.patch.object(sd_plan.subprocess, "run") as run, \
                mock.patch.object(sd_plan, "claude_binary", return_value="/bin/claude"):
            run.return_value = subprocess.CompletedProcess([], 0)
            sd_plan.agent(Path("/repo"), "2026-09-20-a-slug", row)
        prompt = run.call_args.args[0][2]
        self.assertEqual(prompt, (
            "/sd-plan 2026-09-20-a-slug --from sd:42\n\n"
            "This run is unattended: nobody will answer a question, so record "
            "routine choices on the row and continue. "
            + " ".join(sd_plan.CITATION_RULES)
            + " Leave all three documents "
            "in docs/work/2026-09-20-a-slug/: prd.md, design.md, implement.md."
        ))


class AJevThatCannotAnswer(JevCase):
    """Off, unkeyed, or failing: all of them are today's answer, loudly."""

    def test_a_switched_off_jev_writes_all_three_in_one_run(self):
        # `jev enabled` exits 3, which is also what an unkeyed machine does,
        # so this one case covers both. The split into two agent runs must
        # not happen: the question is settled before the first one.
        _, done = self.run_plan(jev=self.jev(enabled=3))
        self.assertEqual(self.documents(), ["design.md", "implement.md", "prd.md"])
        self.assertEqual(self.agent_runs(), ["prd.md design.md implement.md"])
        self.assertFalse((self.stub / "questions.json").is_file())
        self.assertIn("switched off, unkeyed or unreachable", done.stderr)
        self.assertNotIn("Jev:", self.message())

    def test_a_jev_that_is_not_there_at_all_writes_all_three(self):
        _, done = self.run_plan(jev=self.home / "no-such-jev.sh")
        self.assertEqual(self.documents(), ["design.md", "implement.md", "prd.md"])
        self.assertIn("jev", done.stderr)

    def test_a_failing_ask_writes_all_three_and_says_why(self):
        # The judgment is lost after the prd is written, which is the worst
        # moment for it: the run has to finish the other two anyway.
        _, done = self.run_plan(jev=self.jev(answer='echo "boom" >&2; exit 1'))
        self.assertEqual(self.documents(), ["design.md", "implement.md", "prd.md"])
        self.assertEqual(self.agent_runs(),
                         ["prd.md", "prd.md design.md implement.md"])
        self.assertIn("exited 1", done.stderr)
        self.assertIn("boom", done.stderr)
        self.assertNotIn("Jev:", self.message())

    def test_an_answer_this_cannot_read_writes_all_three(self):
        _, done = self.run_plan(jev=self.jev(answer='echo "not json"'))
        self.assertEqual(self.documents(), ["design.md", "implement.md", "prd.md"])
        self.assertIn("cannot read", done.stderr)


class TheJudgment(JevCase):
    """Two decisions, not one."""

    def test_a_design_that_is_not_needed_and_an_implement_that_is(self):
        _, done = self.run_plan(jev=self.jev(design=0.10, implement=0.92))
        self.assertEqual(self.documents(), ["implement.md", "prd.md"])
        # The prd alone first, because the prd is the state the question is
        # about; then the pass the judgment kept.
        self.assertEqual(self.agent_runs(), ["prd.md", "prd.md implement.md"])
        self.assertIn("plans prd.md, implement.md", done.stdout)
        self.assertIn("design 0.10 (skipped)", done.stdout)
        self.assertIn("implement 0.92 (planned)", done.stdout)
        # And the reason outlives the run's stdout.
        self.assertIn("Jev: design 0.10 (skipped), implement 0.92 (planned)",
                      self.message())
        self.assertIn("Work: sd:", self.message())

    def test_a_design_that_is_needed_and_an_implement_that_is_not(self):
        # The other way round, because a caller that collapsed the two
        # questions into one choice could not express this.
        self.run_plan(jev=self.jev(design=0.88, implement=0.04))
        self.assertEqual(self.documents(), ["design.md", "prd.md"])
        self.assertEqual(self.agent_runs(), ["prd.md", "prd.md design.md"])

    def test_a_prd_that_carries_the_whole_shape_plans_nothing_else(self):
        self.run_plan(jev=self.jev(design=0.05, implement=0.07))
        self.assertEqual(self.documents(), ["prd.md"])
        # One agent run: there was nothing left to ask for.
        self.assertEqual(self.agent_runs(), ["prd.md"])
        # And it is a finished plan, not a refusal: an item whose whole shape
        # is its prd is registered, committed and pushed like any other.
        self.assertEqual(self.pushed(),
                         ["main", self.git("rev-parse", "--abbrev-ref", "HEAD")])
        self.assertEqual(len(self.rows()), 1)

    def test_both_passes_needed_is_today_s_answer_in_two_runs(self):
        self.run_plan(jev=self.jev(design=0.91, implement=0.95))
        self.assertEqual(self.documents(), ["design.md", "implement.md", "prd.md"])
        self.assertEqual(self.agent_runs(),
                         ["prd.md", "prd.md design.md implement.md"])

    def test_the_gate_is_where_the_constant_says(self):
        self.run_plan(jev=self.jev(design=sd_plan.JEV_GATE,
                                   implement=sd_plan.JEV_GATE - 0.01))
        # At the gate the pass is planned; a hair under it is skipped.
        self.assertEqual(self.documents(), ["design.md", "prd.md"])


class WhatLeavesTheMachine(JevCase):
    """A prd is internal planning prose, and it is the only thing sent."""

    def test_the_ask_names_the_item_and_the_run_is_one_id(self):
        """`--subject` is the tracker's number, and every call shares `JEV_RUN` (sd:2953)."""
        identifier, _ = self.run_plan(jev=self.jev(), JEV_RUN=None)
        calls = (self.stub / "calls").read_text(encoding="utf-8").splitlines()
        self.assertEqual([line.split()[0] for line in calls], ["enabled", "ask"])
        self.assertIn(f"--subject sd-plan:sd-{identifier} ", calls[1])
        runs = {line.rsplit("run=", 1)[1] for line in calls}
        self.assertEqual(len(runs), 1, runs)
        self.assertRegex(runs.pop(), r"^sd-plan-\d{8}T\d{6}-[0-9a-f]{4}$")

    def test_the_state_is_the_prd_and_the_questions_are_the_two(self):
        self.run_plan(jev=self.jev(design=0.9, implement=0.9))
        sent = (self.stub / "state.txt").read_text(encoding="utf-8")
        self.assertEqual(sent, (self.folder() / "prd.md").read_text(encoding="utf-8"))
        questions = json.loads((self.stub / "questions.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(questions), ["design", "implement"])
        self.assertEqual([q["type"] for q in questions.values()], ["noul", "noul"])
        # One request, so the two questions run in parallel.
        self.assertEqual(len(questions), 2)

    def test_nothing_about_the_repository_or_the_row_is_in_the_question(self):
        self.run_plan(jev=self.jev())
        questions = (self.stub / "questions.json").read_text(encoding="utf-8")
        branch = self.git("rev-parse", "--abbrev-ref", "HEAD")
        for secret in (str(self.repo), str(self.origin), str(self.home), branch):
            self.assertNotIn(secret, questions)


class ThePrdItself(JevCase):
    """Nothing here rewrites a prd -- the judgment is recorded beside it."""

    #: Every way a run can go, as arguments built after the fixture is, since
    #: each case gets a fresh home and a stub written into the old one is a
    #: stub that is not there any more.
    CASES = {
        "switched off": lambda case: dict(stage_off=True, jev=case.jev()),
        "off": lambda case: dict(jev=case.jev(enabled=3)),
        "failing": lambda case: dict(jev=case.jev(answer="exit 1")),
        "answered": lambda case: dict(jev=case.jev(design=0.02, implement=0.02)),
    }

    def test_the_prd_is_untouched_whether_or_not_jev_ran(self):
        for name, arguments in self.CASES.items():
            with self.subTest(case=name):
                self.setUp()
                self.run_plan(**arguments(self))
                prd = (self.folder() / "prd.md").read_text(encoding="utf-8")
                self.assertEqual(prd, "---\ntitle: %s\ncreated: 2026-09-11\n---\n\n"
                                      "# prd.md\n" % self.folder().name)
                self.assertNotIn("jev", prd.lower())


if __name__ == "__main__":
    unittest.main()
