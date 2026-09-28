"""Identity, completion evidence and tracker freshness cross real boundaries."""

import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from sd_db import connect, create_item, upsert_repo
from sd_db.migrate import initialise
from sd_db.progress import (
    cancel_work, completion_record, deliver_associated_work, deliver_work, item_for_artifact,
    relink_artifact, tracker_freshness, tracker_items,
)
from sd_db.shadow_sync import write_watermark
from sd_db.workflow import StaleItem, WorkflowError, allowed_statuses, change_status, edit_item, item_state
from sd_db.writes import TransitionRefused, record_state, transition, upsert_shadow


class ProgressCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Workflow test")
        self.git("config", "user.email", "workflow@example.test")
        self.git("commit", "--allow-empty", "-m", "Initial")
        self.path = self.root / "sd.db"
        initialise(self.path)
        self.db = connect(self.path)
        self.addCleanup(self.db.close)
        upsert_repo(self.db, str(self.repo), status_source="row")
        self.item = create_item(self.db, kind="work", title="Stable work", repo=str(self.repo),
                                path="docs/work/original/prd.md", source="docs/work",
                                external_id=f"{self.repo}::docs/work/original/prd.md")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], text=True, capture_output=True,
                              check=True).stdout.strip()

    def commit(self, trailer=None):
        message = "A slice" + (f"\n\n{trailer}" if trailer else "")
        self.git("commit", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def remote(self):
        remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True,
                       capture_output=True)
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "-u", "origin", "main")
        return remote


class ArtifactIdentity(ProgressCase):
    def test_relink_preserves_stable_identity_and_history(self):
        artifact = self.repo / "docs/work/archive/2026-09/renamed/prd.md"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("# Same work, moved\n")
        before = item_state(self.db, self.item)
        result = relink_artifact(self.db, self.item, str(artifact.relative_to(self.repo)),
                                 expected_revision=before["revision"], who="operator")
        for name in ("id", "source", "external_id", "repo", "status"):
            self.assertEqual(result["item"][name], before["item"][name])
        self.assertEqual(result["notes"][:len(before["notes"])], before["notes"])
        self.assertEqual(result["item"]["path"], "docs/work/archive/2026-09/renamed/prd.md")
        self.assertIn("Relinked artifact", result["notes"][-1]["body"])
        self.assertEqual(item_for_artifact(self.db, str(self.repo), result["item"]["path"])["id"], self.item)
        self.assertIsNone(item_for_artifact(self.db, str(self.repo), "docs/work/original/prd.md"))

    def test_archive_resolution_uses_original_identity_without_title_matching(self):
        found = item_for_artifact(self.db, str(self.repo), "docs/work/archive/2026-09/original/prd.md")
        self.assertEqual(found["id"], self.item)
        self.assertIsNone(item_for_artifact(self.db, str(self.repo), "docs/work/same-title/prd.md"))

    def test_ambiguous_exact_artifact_refuses_instead_of_selecting_one_row(self):
        create_item(self.db, kind="work", title="Duplicate path", repo=str(self.repo),
                    path="docs/work/original/prd.md")
        with self.assertRaisesRegex(WorkflowError, "multiple"):
            item_for_artifact(self.db, str(self.repo), "docs/work/original/prd.md")

    def test_relink_refuses_missing_escaping_or_claimed_paths(self):
        (self.root / "outside.md").write_text("outside")
        linked = self.repo / "link.md"
        linked.symlink_to(self.root / "outside.md")
        for path in ("missing.md", "../outside.md", str(self.root / "outside.md"), "link.md"):
            before = item_state(self.db, self.item)
            with self.subTest(path=path), self.assertRaises(WorkflowError):
                relink_artifact(self.db, self.item, path, who="operator")
            self.assertEqual(item_state(self.db, self.item), before)
        (self.repo / "other.md").write_text("other")
        create_item(self.db, kind="work", title="Other", repo=str(self.repo), path="other.md")
        with self.assertRaisesRegex(WorkflowError, "already"):
            relink_artifact(self.db, self.item, "other.md", who="operator")


class WorkCompletion(ProgressCase):
    def test_public_transition_cannot_bypass_work_completion(self):
        with self.assertRaisesRegex(TransitionRefused, "deliver_work|completion"):
            transition(self.db, self.item, "done", who="dashboard")

    def test_cancellation_needs_reason_and_never_marks_shipped(self):
        before = item_state(self.db, self.item)
        with self.assertRaises(WorkflowError):
            cancel_work(self.db, self.item, reason=" ", who="operator")
        self.assertEqual(item_state(self.db, self.item), before)
        result = cancel_work(self.db, self.item, reason="No longer needed", who="operator")
        self.assertEqual(result["item"]["status"], "done")
        self.assertIsNone(result["item"]["shipped_at"])
        self.assertEqual(json.loads(result["item"]["fields"])["completion"]["outcome"], "cancelled")
        self.assertIn("cancelled", result["notes"][-1]["body"])
        self.assertEqual(cancel_work(self.db, self.item, reason="No longer needed", who="operator"), result)
        self.assertEqual(allowed_statuses(self.db, self.item), [])
        with self.assertRaisesRegex(TransitionRefused, "terminal"):
            change_status(self.db, self.item, "planning", who="operator")

    def test_delivery_verifies_real_remote_commit_and_is_idempotent(self):
        self.remote()
        commit = self.commit(f"Delivers: sd:{self.item}")
        self.git("push", "origin", "main")
        result = deliver_work(self.db, self.item, commit, who="operator")
        self.assertEqual(result["item"]["status"], "done")
        self.assertTrue(result["item"]["shipped_at"])
        evidence = json.loads(result["item"]["fields"])["completion"]
        self.assertEqual((evidence["outcome"], evidence["commit"]), ("delivered", commit))
        self.assertEqual(deliver_work(self.db, self.item, commit, who="operator"), result)
        with self.assertRaises(WorkflowError):
            cancel_work(self.db, self.item, reason="Cannot rewrite delivery", who="operator")

    def test_legacy_delivery_trailer_uses_original_source_slug_after_relink(self):
        artifact = self.repo / "new-name.md"
        artifact.write_text("moved")
        relink_artifact(self.db, self.item, "new-name.md", who="operator")
        commit = self.commit("Delivers: original")
        self.assertEqual(deliver_work(self.db, self.item, commit, who="operator")["item"]["status"], "done")

    def test_slice_cancel_trailer_wrong_item_and_unpushed_delivery_are_refused(self):
        self.remote()
        for trailer in (f"Item: sd:{self.item}", f"Closes: sd:{self.item}", "Delivers: unrelated"):
            commit = self.commit(trailer)
            self.git("push", "origin", "main")
            before = item_state(self.db, self.item)
            with self.subTest(trailer=trailer), self.assertRaises(WorkflowError):
                deliver_work(self.db, self.item, commit, who="operator")
            self.assertEqual(item_state(self.db, self.item), before)
        commit = self.commit(f"Delivers: sd:{self.item}")
        with self.assertRaisesRegex(WorkflowError, "reachable"):
            deliver_work(self.db, self.item, commit, who="operator")

    def test_remote_failure_does_not_trust_stale_tracking_ref(self):
        remote = self.remote()
        commit = self.commit(f"Delivers: sd:{self.item}")
        self.git("push", "origin", "main")
        remote.rename(self.root / "unavailable.git")
        before = item_state(self.db, self.item)
        with self.assertRaisesRegex(WorkflowError, "verify|remote|failed"):
            deliver_work(self.db, self.item, commit, who="operator")
        self.assertEqual(item_state(self.db, self.item), before)

    def test_non_commit_strings_and_stale_requests_are_refused(self):
        before = item_state(self.db, self.item)
        for commit in ("HEAD", "--help", "abcd"):
            with self.subTest(commit=commit), self.assertRaises(WorkflowError):
                deliver_work(self.db, self.item, commit, who="operator")
        with self.assertRaises(StaleItem):
            cancel_work(self.db, self.item, reason="Cancelled", expected_revision="old", who="operator")
        self.assertEqual(item_state(self.db, self.item), before)

    def test_failed_completion_history_rolls_back_status_and_evidence(self):
        before = item_state(self.db, self.item)
        with patch("sd_db.progress._transition", side_effect=RuntimeError("disk error")):
            with self.assertRaises(RuntimeError):
                cancel_work(self.db, self.item, reason="Cancel", who="operator")
        self.assertEqual(item_state(self.db, self.item), before)

    def test_completion_receipt_must_be_bound_to_this_item_and_repository(self):
        cancelled = cancel_work(self.db, self.item, reason="Cancelled", who="operator")
        row = cancelled["item"]
        self.assertEqual(completion_record(row)["outcome"], "cancelled")
        fields = json.loads(row["fields"])
        for key, value in (("item", self.item + 1), ("repo", "/another/repo"), ("at", "yesterday"),
                           ("reason", ""), ("who", ""), ("outcome", "anything")):
            corrupt = json.loads(json.dumps(fields))
            corrupt["completion"][key] = value
            with self.subTest(key=key):
                self.assertIsNone(completion_record({**row, "fields": json.dumps(corrupt)}))
        self.assertIsNone(completion_record({**row, "shipped_at": "2026-09-08T12:00:00+00:00"}))

    def test_edit_during_delivery_verification_cannot_be_overwritten(self):
        from sd_db import progress
        commit = self.commit(f"Delivers: sd:{self.item}")
        verify = progress._delivery_evidence

        def concurrent_edit(row, candidate):
            evidence = verify(row, candidate)
            edit_item(self.db, self.item, {"priority": 1}, who="operator")
            return evidence

        with patch("sd_db.progress._delivery_evidence", side_effect=concurrent_edit):
            with self.assertRaises(StaleItem):
                deliver_work(self.db, self.item, commit, who="operator")
        state = item_state(self.db, self.item)
        self.assertEqual(state["item"]["status"], "planning")
        self.assertEqual(state["item"]["priority"], 1)
        self.assertIsNone(state["item"]["shipped_at"])

    def test_remote_tip_moving_during_verification_requires_a_fresh_retry(self):
        from sd_db import progress
        self.remote()
        commit = self.commit(f"Delivers: sd:{self.item}")
        self.git("push", "origin", "main")
        run = progress._git

        def remote_moves(root, *args, **kwargs):
            if args[:1] == ("fetch",):
                self.commit("Item: another")
                self.git("push", "origin", "main")
            return run(root, *args, **kwargs)

        before = item_state(self.db, self.item)
        with patch("sd_db.progress._git", side_effect=remote_moves):
            with self.assertRaisesRegex(WorkflowError, "changed during"):
                deliver_work(self.db, self.item, commit, who="operator")
        self.assertEqual(item_state(self.db, self.item), before)


class AfterTheFactDelivery(ProgressCase):
    """A merge that carried `Item: sd:N` where `Delivers:` was meant (pack sd:1913).

    `deliver_work` refuses it and keeps refusing it; the named path accepts
    it with a reason, after the same reachability check.
    """

    REASON = "prepared without --deliver; the merge is the whole item"

    def test_an_item_merge_on_the_default_branch_delivers_with_a_reason(self):
        self.remote()
        commit = self.commit(f"Item: sd:{self.item}")
        self.git("push", "origin", "main")
        with self.assertRaisesRegex(WorkflowError, "Delivers"):
            deliver_work(self.db, self.item, commit, who="operator")
        result = deliver_associated_work(self.db, self.item, commit, who="operator", reason=self.REASON)
        self.assertEqual(result["item"]["status"], "done")
        self.assertTrue(result["item"]["shipped_at"])
        evidence = completion_record(result["item"])
        self.assertEqual((evidence["outcome"], evidence["commit"], evidence["trailer"]), ("delivered", commit, "Item"))
        self.assertEqual(evidence["after_the_fact"], self.REASON)
        # The sentence every delivery writes, so one query finds this one too.
        self.assertIn(f"delivered at {commit} on {evidence['verified_ref']}", result["notes"][-1]["body"])
        self.assertEqual(deliver_associated_work(self.db, self.item, commit, who="operator", reason=self.REASON), result)

    def test_a_blank_reason_refuses_before_any_check(self):
        commit = self.commit(f"Item: sd:{self.item}")
        before = item_state(self.db, self.item)
        for reason in ("", "  ", None):
            with self.subTest(reason=reason), self.assertRaisesRegex(WorkflowError, "reason"):
                deliver_associated_work(self.db, self.item, commit, who="operator", reason=reason)
        self.assertEqual(item_state(self.db, self.item), before)

    def test_another_trailer_or_an_unpushed_commit_is_refused(self):
        self.remote()
        for trailer in (f"Closes: sd:{self.item}", f"Item: sd:{self.item + 1}", "Item: original", None):
            commit = self.commit(trailer)
            self.git("push", "origin", "main")
            before = item_state(self.db, self.item)
            with self.subTest(trailer=trailer), self.assertRaisesRegex(WorkflowError, "Item"):
                deliver_associated_work(self.db, self.item, commit, who="operator", reason=self.REASON)
            self.assertEqual(item_state(self.db, self.item), before)
        commit = self.commit(f"Item: sd:{self.item}")
        with self.assertRaisesRegex(WorkflowError, "reachable"):
            deliver_associated_work(self.db, self.item, commit, who="operator", reason=self.REASON)

    def test_an_item_line_outside_the_trailer_block_is_refused(self):
        self.remote()
        self.git("commit", "--allow-empty", "-m", f"A slice\n\nItem: sd:{self.item}\n\nAuthored-with: human")
        commit = self.git("rev-parse", "HEAD")
        self.git("push", "origin", "main")
        with self.assertRaisesRegex(WorkflowError, "Item"):
            deliver_associated_work(self.db, self.item, commit, who="operator", reason=self.REASON)

    def test_a_cancelled_row_is_not_reclassified(self):
        self.remote()
        commit = self.commit(f"Item: sd:{self.item}")
        self.git("push", "origin", "main")
        cancel_work(self.db, self.item, reason="No longer needed", who="operator")
        before = item_state(self.db, self.item)
        with self.assertRaisesRegex(WorkflowError, "cancelled or previously completed"):
            deliver_associated_work(self.db, self.item, commit, who="operator", reason=self.REASON)
        self.assertEqual(item_state(self.db, self.item), before)


class TrackerFreshness(ProgressCase):
    def test_freshness_comes_from_successful_sync_not_row_last_seen(self):
        now = datetime(2026, 9, 8, 12, tzinfo=UTC)
        self.assertEqual(tracker_freshness(self.db, now=now)["state"], "never")
        write_watermark(self.db, "github", "2026-09-08T11:00:00+00:00", "2026-09-07T00:00:00+00:00")
        fresh = tracker_freshness(self.db, now=now)
        self.assertEqual((fresh["state"], fresh["age_seconds"]), ("fresh", 3600))
        self.assertEqual(tracker_freshness(self.db, now=now, max_age_seconds=1800)["state"], "stale")

    def test_failed_attempt_does_not_advance_successful_sync_or_appear_fresh(self):
        write_watermark(self.db, "github", "2026-09-08T11:00:00+00:00", "2026-09-07T00:00:00+00:00")
        record_state(self.db, "heartbeat", key="tracker-sync:github", timestamp="2026-09-08T11:30:00+00:00",
                     body={"ok": False, "reason": "Remote unavailable"})
        state = tracker_freshness(self.db, now=datetime(2026, 9, 8, 12, tzinfo=UTC))
        self.assertEqual(state["state"], "degraded")
        self.assertEqual(state["last_success_at"], "2026-09-08T11:00:00+00:00")
        self.assertEqual(state["reason"], "Remote unavailable")

    def test_tracker_projection_never_invents_match_reasons_or_changes_local_items(self):
        before = item_state(self.db, self.item)
        upsert_shadow(self.db, tracker="github", url="https://example.test/issues/1", state="OPEN")
        upsert_shadow(self.db, tracker="github", url="https://example.test/issues/2", state="CLOSED")
        rows = tracker_items(self.db)
        self.assertEqual([row["url"] for row in rows], ["https://example.test/issues/1"])
        self.assertEqual(rows[0]["why"], [])
        self.assertEqual(item_state(self.db, self.item), before)


if __name__ == "__main__":
    unittest.main()
