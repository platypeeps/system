"""`adversarial-gate rank`, against a stub `jev` — never the network.

The contract this pins is the one that makes an experimental model safe to
point at a hostile review: the ordering is ADDITIVE and it NEVER drops a
finding. It is no longer off by default -- `JEV_ADVERSARIAL_GATE` switches it
off and unset means on -- which is exactly why ADDITIVE has to hold: a stage
that runs unasked may only ever reorder what the reviewer already wrote.

Declining has two halves, and both are tested against a byte-for-byte
comparison with the reviewer's own file: JEV_ADVERSARIAL_GATE switching this
stage off, and Jev switched off or unkeyed on this machine. Every failure path -- a stub that exits
nonzero, one that answers for only some findings, one that prints something
that is not JSON -- is tested the same way, because "degrades to today's
output" is a claim about bytes and nothing weaker checks it.

`test_the_set_of_findings_is_identical_with_and_without_jev` is the one that
matters most. Ordering a hostile read is a courtesy; filtering it is the
defect, and a reorder that silently lost the SPECULATIVE finding would pass
every other case here.

The stub is a shell script on a path handed in via ADVERSARIAL_GATE_JEV,
and it records the request it was given, so a case can assert what left the
machine rather than trusting that it was only the findings.
"""

import json
import pathlib
import re
import subprocess
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "adversarial-gate.sh"

# A gate result in the shape core.md asks for: a quoted line, an objection,
# what would have to be true, a confidence tag. Deliberately NOT in Jev order
# -- the reviewer put the SPECULATIVE one first -- so a reorder is visible.
# The prose around the findings is what a reorder must not move, and
# SENTINEL is a line of the document under review that must never be sent.
SENTINEL = "The brief itself says throughput tripled in Q3."
RESULT = f"""# Adversarial review of the brief

These are hypotheses, not findings. {SENTINEL}

## Findings

### 1. The comparison year is never named

> "throughput tripled"

Tripled against what year? The document never says.

What would have to be true: the reader cannot reconstruct the base year from
anything else on the page.

Confidence: **SPECULATIVE**

### 2. The 40% figure has no denominator

> "40% of runs now pass"

Forty per cent of which runs, over what window?

What would have to be true: no denominator appears anywhere in the document.

Confidence: **CERTAIN**

### 3. The conclusion rests on one unstated assumption

> "so the migration paid for itself"

Only if the engineering time is counted at zero.

What would have to be true: the cost section omits engineering time.

Confidence: **LIKELY**

## Verdict

Does the central conclusion survive hostile reading? NO.
"""

# What the stub answers. The reviewer's order is 1, 2, 3; by these the order
# is 2, 3, 1 -- every position changes, so a case cannot pass by accident.
SCORES = {"f1": 0.11, "f2": 0.94, "f3": 0.62}

# Answers every question from a table baked in, and records the request.
STUB_OK = """
case "$1" in
  enabled)
    # `jev enabled STAGE` reads the stage variable itself; a stub that ignores
    # it would rank a review whose stage the caller switched off.
    if [ -n "${2:-}" ]; then
      eval "word=\\${$2:-}"
      case "$(printf %s "$word" | tr 'A-Z' 'a-z')" in
        0|off|false|no|disabled) exit 3 ;;
      esac
    fi
    exit 0 ;;
  ask) ;;
  *) echo "stub: unexpected verb $1" >&2; exit 1 ;;
esac
shift
while [ $# -gt 0 ]; do
  case "$1" in
    --questions) cp "$2" "$JEV_QUESTIONS_LOG"; shift 2 ;;
    --state) cp "$2" "$JEV_STATE_LOG"; shift 2 ;;
    *) shift ;;
  esac
done
cat "$JEV_ANSWERS"
"""


def stub(body):
    return "#!/bin/sh\n" + body


class RankAgainstAStubJev(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="adversarial-gate-rank."))
        self.addCleanup(self._destroy)
        self.result = self.tmp / "adversarial.md"
        self.result.write_text(RESULT)
        self.questions_log = self.tmp / "questions.json"
        self.state_log = self.tmp / "state.json"
        self.answers = self.tmp / "answers.json"
        self.answers.write_text(json.dumps(
            {"answers": {qid: {"noul": p} for qid, p in SCORES.items()}}))

    def _destroy(self):
        for path in sorted(self.tmp.rglob("*"), reverse=True):
            path.rmdir() if path.is_dir() else path.unlink()
        self.tmp.rmdir()

    def rank(self, body=STUB_OK, stage=None):
        jev = self.tmp / "jev.sh"
        jev.write_text(stub(body))
        jev.chmod(0o755)
        env = {
            "PATH": "/usr/bin:/bin",
            "ADVERSARIAL_GATE_JEV": str(jev),
            "JEV_QUESTIONS_LOG": str(self.questions_log),
            "JEV_STATE_LOG": str(self.state_log),
            "JEV_ANSWERS": str(self.answers),
        }
        if stage is not None:
            env["JEV_ADVERSARIAL_GATE"] = stage
        return subprocess.run(
            ["sh", str(SCRIPT), "rank", "--in", str(self.result)],
            env=env, capture_output=True, text=True, timeout=60,
        )

    def headings(self, text=None):
        """The finding headings, in the order the file has them."""
        text = self.result.read_text() if text is None else text
        return [line for line in text.splitlines() if line.startswith("### ")]

    def assertUnchanged(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.result.read_text(), RESULT)
        self.assertNotEqual(result.stderr.strip(), "", "it degraded silently")

    # --- on unless switched off -------------------------------------------

    def test_the_stage_unset_ranks(self):
        # The flip. This stage was opt-in, and an opt-in that defaults to off
        # makes every integration added after it silently never run. Unset is
        # now on, and the machine-wide answers still decide whether it can run.
        before = self.headings()
        result = self.rank(stage=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.questions_log.exists(), "the stage did not run")
        self.assertNotEqual(self.headings(), before)

    def test_the_stage_switched_off_leaves_the_file_byte_for_byte(self):
        # `jev enabled STAGE` reads the variable, so nothing is asked of the
        # model and the reviewer's result is what stays on disk.
        result = self.rank(stage="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.result.read_text(), RESULT)
        self.assertFalse(self.questions_log.exists(),
                         "jev was asked with the stage switched off")

    def test_the_words_that_switch_a_stage_off_are_the_switch_file_s_words(self):
        # One vocabulary, in `local-jev/jev.py`, and not a second one here.
        for value in ("0", "off", "false", "no", "disabled", "OFF"):
            with self.subTest(value=value):
                self.result.write_text(RESULT)
                self.questions_log.unlink(missing_ok=True)
                self.assertEqual(self.rank(stage=value).returncode, 0)
                self.assertEqual(self.result.read_text(), RESULT)
                self.assertFalse(self.questions_log.exists())

    def test_anything_else_leaves_the_stage_on(self):
        # A switch nobody set is not an outage, and neither is one set to a
        # word this does not know: `read_flag` answers the same way.
        for value in ("", "1", "yes", "true", "maybe"):
            with self.subTest(value=value):
                self.result.write_text(RESULT)
                self.questions_log.unlink(missing_ok=True)
                self.assertEqual(self.rank(stage=value).returncode, 0)
                self.assertTrue(self.questions_log.exists())

    def test_jev_disabled_leaves_the_file_byte_for_byte_and_says_so(self):
        # `jev enabled` exits 3 on a machine that is switched off or unkeyed,
        # and the two are deliberately indistinguishable. Neither is an error.
        result = self.rank(body="[ \"$1\" = enabled ] && exit 3\nexit 1\n")
        self.assertUnchanged(result)
        self.assertIn("off or unkeyed", result.stderr)
        self.assertFalse(self.questions_log.exists(), "it asked a jev that said no")

    def test_a_missing_jev_leaves_the_file_byte_for_byte(self):
        self.result.write_text(RESULT)
        env_result = subprocess.run(
            ["sh", str(SCRIPT), "rank", "--in", str(self.result)],
            env={"PATH": "/usr/bin:/bin",
                 "ADVERSARIAL_GATE_JEV": str(self.tmp / "no-such-jev.sh")},
            capture_output=True, text=True, timeout=60,
        )
        self.assertUnchanged(env_result)
        self.assertIn("no jev at", env_result.stderr)

    # --- on, and answering ------------------------------------------------

    def test_jev_answering_reorders_the_findings_and_labels_them(self):
        before = self.headings()
        result = self.rank()
        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.headings()
        self.assertEqual(after, [before[1], before[2], before[0]],
                         "the findings were not ordered by the stub's answers")
        text = self.result.read_text()
        for score in SCORES.values():
            self.assertIn(f"real defect: {score:.2f}", text)
        self.assertIn("Nothing was removed, filtered or", text)
        # The prose the findings sit under does not move with them.
        self.assertLess(text.index("## Findings"), text.index(after[0]))
        self.assertGreater(text.index("## Verdict"), text.index(after[2]))

    def test_the_set_of_findings_is_identical_with_and_without_jev(self):
        # The one that matters. A reorder that quietly lost the SPECULATIVE
        # finding -- the one a confidence filter would drop first -- passes
        # every other case in this file and fails this one.
        without = set(self.headings(RESULT))
        self.rank()
        with_jev = set(self.headings())
        self.assertEqual(with_jev, without)
        ranked = self.result.read_text()
        for tag in ("SPECULATIVE", "CERTAIN", "LIKELY"):
            self.assertEqual(ranked.count(tag), RESULT.count(tag), tag)
        for quoted in ('"throughput tripled"', '"40% of runs now pass"',
                       '"so the migration paid for itself"'):
            self.assertIn(quoted, ranked)

    def test_ordering_a_file_twice_is_refused(self):
        self.assertEqual(self.rank().returncode, 0)
        once = self.result.read_text()
        second = self.rank()
        self.assertEqual(self.result.read_text(), once)
        self.assertIn("ordered by Jev already", second.stderr)

    # --- what leaves the machine -----------------------------------------

    def test_only_the_findings_leave_the_machine(self):
        self.rank()
        state = json.loads(self.state_log.read_text())
        self.assertEqual(set(state), {"findings"})
        self.assertEqual(set(state["findings"]), set(SCORES))
        blob = self.state_log.read_text() + self.questions_log.read_text()
        self.assertNotIn(SENTINEL, blob, "a line of the document under review was sent")
        self.assertNotIn("## Verdict", blob)
        for text in state["findings"].values():
            self.assertLessEqual(len(text), 1500)

    def test_every_finding_is_asked_in_one_request(self):
        # Questions in a single `ask` run in parallel; N findings cost one
        # round trip, not N.
        self.rank()
        questions = json.loads(self.questions_log.read_text())
        self.assertEqual(set(questions), set(SCORES))
        self.assertTrue(all(q["type"] == "noul" for q in questions.values()))

    # --- every failure degrades to today's order and today's text ---------

    def test_a_jev_that_fails_leaves_the_file_byte_for_byte(self):
        result = self.rank(body="[ \"$1\" = enabled ] && exit 0\n"
                                "echo 'HTTP 500' >&2\nexit 1\n")
        self.assertUnchanged(result)
        self.assertIn("exited 1", result.stderr)

    def test_a_jev_that_prints_nonsense_leaves_the_file_byte_for_byte(self):
        result = self.rank(body="[ \"$1\" = enabled ] && exit 0\n"
                                "echo 'not json at all'\n")
        self.assertUnchanged(result)
        self.assertIn("no answers block", result.stderr)

    def test_a_partial_answer_leaves_the_file_byte_for_byte(self):
        # Sorting the findings Jev answered for and leaving the rest wherever
        # they fall is an order nobody chose. Refuse the whole thing.
        self.answers.write_text(json.dumps({"answers": {"f1": {"noul": 0.9}}}))
        self.assertUnchanged(self.rank())

    def test_a_result_with_no_confidence_tags_is_left_alone(self):
        self.result.write_text("# Review\n\n## Nothing to report\n\nAll axes clean.\n")
        before = self.result.read_text()
        result = self.rank()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.result.read_text(), before)
        self.assertFalse(self.questions_log.exists())

    def test_an_empty_result_file_is_left_alone(self):
        self.result.write_text("")
        result = self.rank()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.result.read_text(), "")
        self.assertIn("empty or absent", result.stderr)

    def test_a_non_finite_or_out_of_range_score_leaves_the_file_byte_for_byte(self):
        # `float` reads NaN and Infinity, and JSON carries both. A score
        # outside 0..1 is no answer, so the file stays the reviewer's own.
        for bad in ("NaN", "Infinity", "-Infinity", "1.5", "-0.2"):
            with self.subTest(score=bad):
                self.result.write_text(RESULT)
                self.answers.write_text(
                    '{"answers": {"f1": {"noul": %s}, "f2": {"noul": 0.9}, '
                    '"f3": {"noul": 0.5}}}' % bad)
                result = self.rank()
                self.assertUnchanged(result)
                self.assertIn("did not answer for f1", result.stderr)


class TheWriteIsWholeOrNothing(unittest.TestCase):
    """The gate result is the only copy of the review. A write that fails
    part way must leave the reviewer's file, not an empty one."""

    def test_a_failed_write_leaves_the_original_and_no_temp_file(self):
        import importlib.util
        from unittest import mock
        spec = importlib.util.spec_from_file_location("rank", HERE.parent / "rank.py")
        rank = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rank)
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "adversarial.md"
            path.write_text(RESULT)
            # A value `write` refuses, so the write fails after any open.
            with mock.patch.object(rank, "rank_text", return_value=12345), \
                    self.assertRaises(TypeError):
                rank.main(["--in", str(path), "--jev", "/nonexistent"])
            self.assertEqual(path.read_text(), RESULT)
            self.assertEqual(sorted(p.name for p in pathlib.Path(tmp).iterdir()),
                             ["adversarial.md"])


class RunDoesNotRankUnlessAskedTo(unittest.TestCase):
    """`run` reaches the ranker through the same two conditions, or not at all."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="adversarial-gate-runrank."))
        self.addCleanup(self._destroy)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.out = self.tmp / "adversarial.md"
        # A codex that writes a result, as the real one does with `-o`.
        codex = self.bin / "codex"
        codex.write_text("#!/bin/sh\ncat > /dev/null\ncp \"$CODEX_RESULT\" \"$CODEX_OUT\"\n")
        codex.chmod(0o755)
        self.fixture = self.tmp / "fixture.md"
        self.fixture.write_text(RESULT)

    def _destroy(self):
        for path in sorted(self.tmp.rglob("*"), reverse=True):
            path.rmdir() if path.is_dir() else path.unlink()
        self.tmp.rmdir()

    def run_gate(self, stage=None):
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "CODEX_RESULT": str(self.fixture),
            "CODEX_OUT": str(self.out),
            "ADVERSARIAL_GATE_JEV": str(self.tmp / "no-such-jev.sh"),
        }
        if stage is not None:
            env["JEV_ADVERSARIAL_GATE"] = stage
        return subprocess.run(
            ["sh", str(SCRIPT), "run", "--lens", "research-brief",
             "--repo", str(self.tmp), "--out", str(self.out)],
            env=env, capture_output=True, text=True, timeout=60,
        )

    def test_run_with_the_stage_unset_writes_what_the_reviewer_wrote(self):
        # Unset is on, so this is the live path and not the quiet one the
        # old name promised. What it pins is that a stage that runs with no
        # `jev` to run against still writes the reviewer's bytes.
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.out.read_text(), RESULT)
        self.assertNotIn("Jev", result.stderr)

    def test_run_with_the_stage_set_on_and_no_jev_writes_what_the_reviewer_wrote(self):
        # `"1"` is merely not an off word. It is the same run as the case
        # above, kept because `1` is what every caller used to have to
        # export and a machine still carrying one must not behave
        # differently from a machine that dropped it.
        result = self.run_gate(stage="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.out.read_text(), RESULT)
        self.assertIn("no jev at", result.stderr)


if __name__ == "__main__":
    unittest.main()
