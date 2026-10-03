"""The watcher's contract, asserted against a GitHub double and a registry double.

Test names follow `prd.md`'s acceptance criteria: `c2` is discovery through
the gates and the writer check, `c3` the three probes true and false with the
malformed and unknown outcomes, `c4` the lift sequence and its idempotence,
`c6` supersession. Criteria 5, 7 and 8 belong to the job and its first run,
which is PR 2, and are not here.

Every test runs the entrypoint the way the job will, `dependabot.sh holds
--repo ...`, with `SD_HOLDS_GH` and `SD_HOLDS_NPM` pointing at the doubles
under `doubles/`. The doubles journal each write, and the journal is what
the order and idempotence assertions read; the log is what a person reads,
so it is asserted line by line too.

Python rather than shell for the reason `local-repo-sync/README.md` gives:
the CI wrapper asserts a `Ran N tests` summary and fails on skips.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

FOLDER = Path(__file__).resolve().parent.parent
ENTRYPOINT = FOLDER / "dependabot.sh"
DOUBLES = FOLDER / "tests" / "doubles"
sys.path.insert(0, str(FOLDER))
import holds  # noqa: E402

ME = "example-owner"
TODAY = "2026-09-24"
ADMITTED = "platypeeps/admitted"


def block(*probes, since="2026-09-10", reason="vitest 5 Assertion<R, T>; jest-dom 7.0.1 augments Assertion<T>",
          extra=()):
    lines = ["```yaml", "sd-hold:", f"  since: {since}", f"  reason: {reason}", "  lifts-when:"]
    lines.extend(f"    - {probe}" for probe in probes)
    lines.extend(f"  {line}" for line in extra)
    lines.append("```")
    return "\n".join(lines)


def pull(number, state="open", title="deps: bump nodemailer from 9.1.1 to 10.0.1", comments=1):
    # The shape `repos/{o}/{r}/issues?creator=dependabot[bot]` returns,
    # trimmed to what the watcher reads.
    return {"number": number, "state": state, "title": title, "draft": False, "comments": comments,
            "user": {"login": "dependabot[bot]", "type": "Bot"}, "author_association": "NONE",
            "pull_request": {"url": f"https://api.github.com/repos/{ADMITTED}/pulls/{number}"}}


def comment(cid, body, login=ME, kind="User", association="MEMBER"):
    return {"id": cid, "user": {"login": login, "type": kind}, "author_association": association,
            "body": body}


def dependabot_says(cid, body):
    # `Superseded by #13978.` as Dependabot writes it (gohugoio/hugo#13976).
    return comment(cid, body, login="dependabot[bot]", kind="Bot", association="CONTRIBUTOR")


class HoldsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "state.json"
        self.state = {
            "viewer_login": ME,
            "viewer": {ADMITTED: "ADMIN"},
            "repos": {ADMITTED: {"pulls": [], "comments": {}, "permission": {
                ME: {"permission": "admin", "role_name": "admin"},
                "stranger": {"permission": "read", "role_name": "read"},
                "maintainer": {"permission": "write", "role_name": "maintain"},
            }}},
            "issues": {
                "testing-library/jest-dom#738": {"state": "open",
                                                 "html_url": "https://github.com/testing-library/jest-dom/issues/738"},
                "testing-library/jest-dom#700": {"state": "closed",
                                                 "html_url": "https://github.com/testing-library/jest-dom/issues/700"},
            },
            "npm": {
                "@testing-library/jest-dom": {"version": "7.0.1", "dependencies": {"redent": "^3.0.0"}},
                "@payloadcms/email-nodemailer": {"version": "3.89.0", "dependencies": {"nodemailer": "^9.0.1"}},
                "ms": {"version": "2.1.3", "dependencies": {}},
            },
            "errors": {},
            "npm_errors": {},
            "next_comment_id": 9000,
        }

    # -- fixture helpers --

    def repo(self, slug=ADMITTED):
        return self.state["repos"][slug]

    def add_pull(self, number, body, state="open", login=ME, slug=ADMITTED, cid=None, more=()):
        cid = cid or 5000 + number
        self.repo(slug)["pulls"].append(pull(number, state=state, comments=1 + len(more)))
        self.repo(slug)["comments"][str(number)] = [comment(cid, body, login=login), *more]
        return cid

    def run_holds(self, *repos, dry_run=False, expect=0, env=None):
        self.state_path.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        environment = dict(os.environ)
        environment.update({
            "PYTHON": sys.executable,
            "SD_HOLDS_GH": f"{sys.executable} {DOUBLES / 'gh.py'}",
            "SD_HOLDS_NPM": f"{sys.executable} {DOUBLES / 'npm.py'}",
            "SD_HOLDS_DOUBLE_STATE": str(self.state_path),
            "SD_HOLDS_TODAY": TODAY,
        })
        environment.update(env or {})
        args = ["holds"] + (["--dry-run"] if dry_run else [])
        for slug in repos or (ADMITTED,):
            args += ["--repo", slug]
        done = subprocess.run(["/bin/sh", str(ENTRYPOINT), *args], capture_output=True, text=True,
                              env=environment)
        self.assertEqual(done.returncode, expect, done.stdout + done.stderr)
        self.stderr = done.stderr
        self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
        return done.stdout.splitlines()

    def journal(self):
        path = Path(str(self.state_path) + ".journal")
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def clear_journal(self):
        Path(str(self.state_path) + ".journal").unlink(missing_ok=True)

    def comment_body(self, number, cid, slug=ADMITTED):
        for c in self.repo(slug)["comments"][str(number)]:
            if c["id"] == cid:
                return c["body"]
        raise AssertionError(f"no comment {cid} on #{number}")

    def lines(self, log, word):
        return [line for line in log if line.startswith(word + " ")]


# --- criterion 2: discovery through the gates, and only a writer's block ---


class C2Discovery(HoldsCase):
    def test_c2_evaluates_exactly_the_admitted_repository_and_names_the_denied_one(self):
        denied = "otherorg/elsewhere"
        self.state["viewer"][denied] = "ADMIN"
        self.state["repos"][denied] = {"pulls": [], "comments": {},
                                       "permission": {ME: {"permission": "admin", "role_name": "admin"}}}
        self.add_pull(308, block("issue-closed: testing-library/jest-dom#738"))
        self.add_pull(41, block("issue-closed: testing-library/jest-dom#738"), slug=denied)
        log = self.run_holds(ADMITTED, denied)
        self.assertIn(f"HELD #308 {ADMITTED} 1 probes, none lifted", log)
        self.assertIn(f"SKIP {denied} owner not authorized", log)
        self.assertEqual([l for l in log if denied in l], [f"SKIP {denied} owner not authorized"])
        self.assertEqual(self.journal(), [])

    def test_c2_a_repository_without_write_access_is_skipped_by_name(self):
        readonly = "platypeeps/readonly"
        self.state["viewer"][readonly] = "READ"
        self.state["repos"][readonly] = {"pulls": [pull(7)], "comments": {"7": [comment(1, block("npm-version: \"ms > 1.0.0\""))]},
                                         "permission": {ME: {"permission": "admin", "role_name": "admin"}}}
        log = self.run_holds(readonly)
        self.assertEqual([l for l in log if readonly in l], [f"SKIP {readonly} no write access"])
        self.assertEqual(self.journal(), [])

    def test_c2_a_repository_denied_by_name_is_skipped_before_the_owner_rule_admits_it(self):
        log = self.run_holds("platypeeps/Trellis")
        self.assertIn("SKIP platypeeps/Trellis denied by name", log)

    def test_c2_a_readers_block_is_ignored_and_a_writers_block_is_evaluated(self):
        self.add_pull(308, block("issue-closed: testing-library/jest-dom#738"), login="stranger")
        self.add_pull(309, block("issue-closed: testing-library/jest-dom#738"), login="maintainer")
        log = self.run_holds()
        self.assertIn(f"IGNORED #308 {ADMITTED} stranger no write access", log)
        self.assertNotIn(f"HELD #308 {ADMITTED} 1 probes, none lifted", log)
        self.assertIn(f"HELD #309 {ADMITTED} 1 probes, none lifted", log)
        self.assertEqual(self.journal(), [])

    def test_c2_a_readers_block_on_a_closed_pull_request_is_not_carried(self):
        # Even with Dependabot's own supersession comment and a true probe.
        self.add_pull(309, block("issue-closed: testing-library/jest-dom#700"), state="closed",
                      login="stranger", more=[dependabot_says(6001, "Superseded by #320.")])
        self.repo()["pulls"].append(pull(320, comments=0))
        log = self.run_holds()
        self.assertIn(f"IGNORED #309 {ADMITTED} stranger no write access", log)
        self.assertEqual(self.lines(log, "CARRIED"), [])
        self.assertEqual(self.journal(), [])

    def test_c2_the_owner_gate_here_is_the_one_routine_md_writes(self):
        text = (FOLDER / "ROUTINE.md").read_text(encoding="utf-8")
        recited = re.findall(r"^ {7}([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)$", text, re.MULTILINE)
        self.assertEqual(sorted(recited), sorted(holds.DENIED))
        self.assertIn("allowed-repos.conf", text)
        self.assertTrue(all(slug.startswith("platypeeps/") for slug in holds.DENIED))
        self.assertIn("`platypeeps/*` — every repository, always", text)
        self.assertEqual(holds.ALLOWED_OWNERS, ("platypeeps",))


# --- criterion 3: each probe true and false; malformed; unknown ---


class C3Probes(HoldsCase):
    def test_c3_issue_closed_is_false_on_an_open_issue_and_true_on_a_closed_one(self):
        self.add_pull(308, block("issue-closed: testing-library/jest-dom#738"))
        self.add_pull(309, block("issue-closed: testing-library/jest-dom#700"))
        log = self.run_holds()
        self.assertIn(f"HELD #308 {ADMITTED} 1 probes, none lifted", log)
        self.assertEqual(self.lines(log, "LIFTED"),
                         [f"LIFTED #309 {ADMITTED} issue-closed testing-library/jest-dom#700 "
                          "https://github.com/testing-library/jest-dom/issues/700 closed"])

    def test_c3_npm_version_is_false_below_the_bound_and_true_above_it(self):
        self.add_pull(308, block('npm-version: "@testing-library/jest-dom > 7.0.1"'))
        self.add_pull(309, block('npm-version: "@testing-library/jest-dom >= 7.0.1"'))
        log = self.run_holds()
        self.assertIn(f"HELD #308 {ADMITTED} 1 probes, none lifted", log)
        self.assertEqual(self.lines(log, "LIFTED"),
                         [f"LIFTED #309 {ADMITTED} npm-version @testing-library/jest-dom >= 7.0.1 latest 7.0.1"])

    def test_c3_npm_dep_is_false_when_the_declared_range_excludes_and_true_when_it_admits(self):
        self.add_pull(317, block('npm-dep: "@payloadcms/email-nodemailer nodemailer ^10"'))
        self.add_pull(318, block('npm-dep: "@payloadcms/email-nodemailer nodemailer ^9"'))
        log = self.run_holds()
        self.assertIn(f"HELD #317 {ADMITTED} 1 probes, none lifted", log)
        self.assertEqual(self.lines(log, "LIFTED"),
                         [f"LIFTED #318 {ADMITTED} npm-dep @payloadcms/email-nodemailer nodemailer ^9 "
                          "satisfied by 3.89.0 (nodemailer ^9.0.1)"])

    def test_c3_npm_dep_is_true_once_the_registry_admits_the_major(self):
        self.state["npm"]["@payloadcms/email-nodemailer"] = {"version": "3.90.0",
                                                             "dependencies": {"nodemailer": "^9.0.1 || ^10.0.0"}}
        self.add_pull(317, block('npm-dep: "@payloadcms/email-nodemailer nodemailer ^10"'))
        log = self.run_holds()
        self.assertEqual(self.lines(log, "LIFTED"),
                         [f"LIFTED #317 {ADMITTED} npm-dep @payloadcms/email-nodemailer nodemailer ^10 "
                          "satisfied by 3.90.0 (nodemailer ^9.0.1 || ^10.0.0)"])

    def test_c3_npm_dep_is_false_when_the_dependency_is_not_declared_at_all(self):
        self.add_pull(317, block('npm-dep: "ms nodemailer ^10"'))
        log = self.run_holds()
        self.assertIn(f"HELD #317 {ADMITTED} 1 probes, none lifted", log)

    def test_c3_a_malformed_probe_alone_is_reported_and_touches_nothing(self):
        self.add_pull(308, block("issue-closed: testing-library/jest-dom#738", "npm-latest: jest-dom > 7"))
        log = self.run_holds()
        self.assertIn(f"MALFORMED #308 {ADMITTED} - npm-latest: jest-dom > 7", log)
        self.assertEqual(self.lines(log, "HELD"), [])
        self.assertEqual(self.lines(log, "LIFTED"), [])
        self.assertEqual(self.journal(), [])

    def test_c3_a_malformed_probe_beside_a_true_one_lifts_and_is_still_named(self):
        self.add_pull(308, block("npm-version: \"@testing-library/jest-dom 7.0.1\"",
                                 "issue-closed: testing-library/jest-dom#700"))
        log = self.run_holds()
        self.assertIn(f'MALFORMED #308 {ADMITTED} - npm-version: "@testing-library/jest-dom 7.0.1"', log)
        self.assertEqual(len(self.lines(log, "LIFTED")), 1)
        self.assertEqual([w["method"] for w in self.journal()], ["PATCH", "POST", "POST", "PATCH"])

    def test_c3_a_failing_read_is_unknown_names_the_error_and_touches_nothing(self):
        self.state["errors"]["repos/testing-library/jest-dom/issues/738"] = "HTTP 502: Bad Gateway"
        self.state["npm_errors"]["@testing-library/jest-dom"] = "npm error code ECONNRESET"
        self.add_pull(308, block("issue-closed: testing-library/jest-dom#738",
                                 'npm-version: "@testing-library/jest-dom > 7.0.1"'))
        log = self.run_holds()
        self.assertIn(f"UNKNOWN #308 {ADMITTED} issue-closed testing-library/jest-dom#738: HTTP 502: Bad Gateway", log)
        self.assertIn(f"UNKNOWN #308 {ADMITTED} npm-version @testing-library/jest-dom > 7.0.1: "
                      "npm error code ECONNRESET", log)
        self.assertEqual(self.lines(log, "HELD"), [])
        self.assertEqual(self.journal(), [])

    def test_c3_a_missing_package_is_unknown_not_false(self):
        self.add_pull(308, block('npm-version: "no-such-package > 1.0.0"'))
        log = self.run_holds()
        self.assertEqual(len(self.lines(log, "UNKNOWN")), 1)
        self.assertIn("E404", self.lines(log, "UNKNOWN")[0])
        self.assertEqual(self.lines(log, "HELD"), [])

    def test_c3_a_failing_read_beside_a_true_probe_still_lifts(self):
        self.state["errors"]["repos/testing-library/jest-dom/issues/738"] = "HTTP 502: Bad Gateway"
        self.add_pull(308, block("issue-closed: testing-library/jest-dom#738",
                                 "issue-closed: testing-library/jest-dom#700"))
        log = self.run_holds()
        self.assertEqual(len(self.lines(log, "UNKNOWN")), 1)
        self.assertEqual(len(self.lines(log, "LIFTED")), 1)
        self.assertEqual([w["method"] for w in self.journal()], ["PATCH", "POST", "POST", "PATCH"])


# --- criterion 4: the lift sequence, in order, once ---


class C4Lift(HoldsCase):
    PROBE = "issue-closed: testing-library/jest-dom#700"
    DETAIL = "issue-closed testing-library/jest-dom#700 https://github.com/testing-library/jest-dom/issues/700 closed"

    def test_c4_a_lift_edits_then_comments_then_rebases_then_edits_then_logs(self):
        cid = self.add_pull(308, "Holding on jest-dom.\n\n" + block(self.PROBE) + "\n")
        log = self.run_holds()
        writes = self.journal()
        self.assertEqual([(w["method"], w["path"]) for w in writes], [
            ("PATCH", f"repos/{ADMITTED}/issues/comments/{cid}"),
            ("POST", f"repos/{ADMITTED}/issues/308/comments"),
            ("POST", f"repos/{ADMITTED}/issues/308/comments"),
            ("PATCH", f"repos/{ADMITTED}/issues/comments/{cid}"),
        ])
        self.assertIn(f"  lifting: {TODAY} {self.DETAIL}\n", writes[0]["body"])
        self.assertTrue(writes[1]["body"].startswith("sd-hold-lifted #308: `issue-closed testing-library/jest-dom#700`"))
        self.assertIn("https://github.com/testing-library/jest-dom/issues/700 closed", writes[1]["body"])
        self.assertEqual(writes[2]["body"], "@dependabot rebase")
        self.assertIn(f"  lifted: {TODAY} {self.DETAIL}\n", writes[3]["body"])
        self.assertNotIn("lifting:", writes[3]["body"])
        self.assertTrue(writes[3]["body"].startswith("Holding on jest-dom."), "the prose around the block survives")
        self.assertEqual(log[-2], f"LIFTED #308 {ADMITTED} {self.DETAIL}")
        self.assertTrue(log[-1].startswith("TOTAL "))

    def test_c4_no_change_posts_nothing_and_logs_held_with_the_probe_count(self):
        self.add_pull(308, block("issue-closed: testing-library/jest-dom#738",
                                 'npm-version: "@testing-library/jest-dom > 7.0.1"'))
        log = self.run_holds()
        self.assertIn(f"HELD #308 {ADMITTED} 2 probes, none lifted", log)
        self.assertEqual(self.journal(), [])

    def test_c4_a_second_run_against_the_same_state_edits_nothing_and_logs_lifted_earlier(self):
        self.add_pull(308, block(self.PROBE))
        self.run_holds()
        self.assertEqual(len(self.journal()), 4)
        self.clear_journal()
        log = self.run_holds()
        self.assertEqual(self.journal(), [])
        self.assertIn(f"LIFTED-EARLIER #308 {ADMITTED} {TODAY}", log)
        self.assertEqual(self.lines(log, "LIFTED"), [])

    def test_c4_a_record_left_at_lifting_with_the_comment_posted_is_resumed_without_a_second_comment(self):
        detail = f"{TODAY} {self.DETAIL}"
        cid = self.add_pull(308, block(self.PROBE, extra=[f"lifting: {detail}"]),
                            more=[comment(7001, f"sd-hold-lifted #308: `issue-closed ...` -- closed")])
        log = self.run_holds()
        writes = self.journal()
        self.assertEqual([(w["method"], w["path"]) for w in writes], [
            ("POST", f"repos/{ADMITTED}/issues/308/comments"),
            ("PATCH", f"repos/{ADMITTED}/issues/comments/{cid}"),
        ])
        self.assertEqual(writes[0]["body"], "@dependabot rebase")
        self.assertIn(f"  lifted: {detail}\n", writes[1]["body"])
        self.assertNotIn("lifting:", writes[1]["body"])
        self.assertIn(f"LIFTED (resumed) #308 {ADMITTED} {detail}", log)

    def test_c4_a_record_left_at_lifting_before_any_comment_finishes_the_whole_sequence(self):
        detail = f"{TODAY} {self.DETAIL}"
        self.add_pull(308, block(self.PROBE, extra=[f"lifting: {detail}"]))
        log = self.run_holds()
        self.assertEqual([w["method"] for w in self.journal()], ["POST", "POST", "PATCH"])
        self.assertIn(f"LIFTED (resumed) #308 {ADMITTED} {detail}", log)

    def test_c4_dry_run_writes_nothing_and_says_what_it_would_lift(self):
        self.add_pull(308, block(self.PROBE))
        log = self.run_holds(dry_run=True)
        self.assertEqual(self.journal(), [])
        self.assertEqual(len(self.lines(log, "WOULD-LIFT")), 1)


# --- criterion 6: supersession, on Dependabot's word only ---


class C6Supersession(HoldsCase):
    PROBES = ("issue-closed: testing-library/jest-dom#738", 'npm-version: "@testing-library/jest-dom > 7.0.1"')

    def test_c6_a_closed_record_with_dependabots_superseded_comment_is_carried_and_evaluated(self):
        old = self.add_pull(309, block(*self.PROBES), state="closed",
                            more=[dependabot_says(6001, "Superseded by #320.")])
        self.repo()["pulls"].append(pull(320, title="deps-dev: bump vitest from 4.1.11 to 5.0.1", comments=0))
        log = self.run_holds()
        writes = self.journal()
        self.assertEqual([(w["method"], w["path"]) for w in writes], [
            ("POST", f"repos/{ADMITTED}/issues/320/comments"),
            ("PATCH", f"repos/{ADMITTED}/issues/comments/{old}"),
        ])
        self.assertIn("  since: 2026-09-10\n", writes[0]["body"])
        self.assertIn("  carried-from: #309\n", writes[0]["body"])
        for probe in self.PROBES:
            self.assertIn(f"    - {probe}\n", writes[0]["body"])
        self.assertIn("  carried-to: #320\n", writes[1]["body"])
        self.assertIn(f"CARRIED #309 -> #320 {ADMITTED}", log)
        self.assertIn(f"HELD #320 {ADMITTED} 2 probes, none lifted", log)
        self.assertLess(log.index(f"CARRIED #309 -> #320 {ADMITTED}"), log.index(f"HELD #320 {ADMITTED} 2 probes, none lifted"))
        # A second run carries nothing.
        self.clear_journal()
        log = self.run_holds()
        self.assertEqual(self.journal(), [])
        self.assertEqual(self.lines(log, "CARRIED"), [])
        self.assertIn(f"SKIP #309 {ADMITTED} closed, already carried to #320", log)
        self.assertIn(f"HELD #320 {ADMITTED} 2 probes, none lifted", log)

    def test_c6_a_carried_record_that_is_already_true_lifts_on_the_new_pull_request_in_the_same_run(self):
        self.add_pull(309, block("issue-closed: testing-library/jest-dom#700"), state="closed",
                      more=[dependabot_says(6001, "Superseded by #320.")])
        self.repo()["pulls"].append(pull(320, comments=0))
        log = self.run_holds()
        self.assertEqual([(w["method"], w["path"].rsplit("/", 2)[-2:]) for w in self.journal()][:2],
                         [("POST", ["320", "comments"]), ("PATCH", ["comments", "5309"])])
        self.assertEqual([w["method"] for w in self.journal()][2:], ["PATCH", "POST", "POST", "PATCH"])
        self.assertEqual(len(self.lines(log, "LIFTED")), 1)
        self.assertIn("#320", self.lines(log, "LIFTED")[0])

    def test_c6_a_closed_record_without_dependabots_comment_is_not_carried_even_to_the_same_package(self):
        # The website repo #307's real shape: a *person* wrote `Superseded by
        # #310.`, Dependabot only said it was no longer needed, and #310 is an
        # open Dependabot pull request on the same package.
        self.add_pull(307, block("issue-closed: testing-library/jest-dom#700"), state="closed", more=[
            comment(5605081872, "Superseded by #310.\n\nThe container scan on #310 caught GHSA-8m3c-c648-2xjj."),
            dependabot_says(5605270900, "Looks like nodemailer is updatable in another way, so this is no longer needed."),
        ])
        self.repo()["pulls"].append(pull(310, title="deps: bump nodemailer from 9.1.0 to 9.1.1", comments=0))
        log = self.run_holds()
        self.assertEqual(self.journal(), [])
        self.assertEqual(self.lines(log, "CARRIED"), [])
        self.assertIn(f"SKIP #307 {ADMITTED} closed, no `Superseded by` comment from Dependabot", log)
        self.assertEqual([l for l in log if "#310" in l], [])

    def test_c6_a_lifted_closed_record_is_not_carried(self):
        self.add_pull(309, block("issue-closed: testing-library/jest-dom#700",
                                 extra=["lifted: 2026-09-17 issue-closed testing-library/jest-dom#700 closed"]),
                      state="closed", more=[dependabot_says(6001, "Superseded by #320.")])
        self.repo()["pulls"].append(pull(320, comments=0))
        log = self.run_holds()
        self.assertEqual(self.journal(), [])
        self.assertIn(f"SKIP #309 {ADMITTED} closed, lifted 2026-09-17", log)

    def test_c6_a_successor_that_already_carries_a_record_is_left_alone(self):
        self.add_pull(309, block("issue-closed: testing-library/jest-dom#738"), state="closed",
                      more=[dependabot_says(6001, "Superseded by #320.")])
        self.add_pull(320, block("issue-closed: testing-library/jest-dom#738"))
        log = self.run_holds()
        self.assertEqual(self.journal(), [])
        self.assertEqual(self.lines(log, "CARRIED"), [])
        self.assertIn(f"HELD #320 {ADMITTED} 1 probes, none lifted", log)


# --- the pieces the probes stand on ---


class Ranges(unittest.TestCase):
    def test_a_caret_nine_does_not_admit_ten(self):
        self.assertFalse(holds.ranges_overlap("^9.0.1", "^10"))

    def test_a_union_admits_either_side(self):
        self.assertTrue(holds.ranges_overlap("^9.0.1 || ^10.0.0", "^10"))

    def test_open_ended_and_x_ranges(self):
        self.assertTrue(holds.ranges_overlap(">=9", "^10"))
        self.assertTrue(holds.ranges_overlap("10.x", "^10"))
        self.assertTrue(holds.ranges_overlap("*", "^10"))
        self.assertTrue(holds.ranges_overlap("~10.1.0", "^10"))
        self.assertFalse(holds.ranges_overlap("~10.1.0", "^10.2.0"))
        self.assertTrue(holds.ranges_overlap(">=9.0.0 <11", "^10"))
        self.assertFalse(holds.ranges_overlap(">=9.0.0 <10", "^10"))
        self.assertTrue(holds.ranges_overlap("9.0.0 - 10.2.0", "^10"))

    def test_zero_majors_follow_npm(self):
        self.assertFalse(holds.ranges_overlap("^0.2.3", "^0.3.0"))
        self.assertTrue(holds.ranges_overlap("^0.2.3", "0.2.9"))

    def test_versions_compare_with_prereleases_below_releases(self):
        self.assertTrue(holds.compare("7.1.0", ">", "7.0.1"))
        self.assertFalse(holds.compare("7.1.0-rc.1", ">=", "7.1.0"))
        self.assertTrue(holds.compare("v7.1.0", "=", "7.1.0"))


class AllowedReposFile(unittest.TestCase):
    """The consent list lives in <config>/dependabot/, outside the checkout."""

    def test_the_default_file_is_in_the_config_dir_and_the_override_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "dependabot"
            conf.mkdir()
            (conf / "allowed-repos.conf").write_text("example-org/one # note\n\nexample-org/one\n")
            other = Path(tmp) / "other.conf"
            other.write_text("example-org/two\n")
            env = {k: v for k, v in os.environ.items() if k != "DEPENDABOT_ALLOWED_REPOS_FILE"}
            env["SYSTEM_TOOLS_CONFIG"] = tmp
            with unittest.mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(holds._allowed_repos(), ("example-org/one",))
                os.environ["DEPENDABOT_ALLOWED_REPOS_FILE"] = str(other)
                self.assertEqual(holds._allowed_repos(), ("example-org/two",))
                del os.environ["DEPENDABOT_ALLOWED_REPOS_FILE"]
                os.environ["SYSTEM_TOOLS_CONFIG"] = str(Path(tmp) / "absent")
                self.assertEqual(holds._allowed_repos(), ())


class Parsing(unittest.TestCase):
    def test_each_probe_kind_parses_and_the_rest_is_malformed(self):
        self.assertEqual(holds.parse_probe("    - issue-closed: testing-library/jest-dom#738").parts,
                         ("testing-library/jest-dom", 738))
        self.assertEqual(holds.parse_probe('    - npm-version: "@testing-library/jest-dom > 7.0.1"').parts,
                         ("@testing-library/jest-dom", ">", "7.0.1"))
        self.assertEqual(holds.parse_probe('    - npm-dep: "@payloadcms/email-nodemailer nodemailer ^10"').parts,
                         ("@payloadcms/email-nodemailer", "nodemailer", "^10"))
        for line in ("    - issue-closed: jest-dom#738", "    - npm-version: \"jest-dom 7.0.1\"",
                     "    - npm-version: \"jest-dom > seven\"", "    - npm-dep: \"a b\"",
                     "    - npm-dep: \"a b 1 - \"", "    - issue-open: a/b#1", "    - not a probe"):
            self.assertTrue(holds.parse_probe(line).malformed, line)

    def test_a_block_written_with_crlf_is_read(self):
        body = ("Holding.\r\n\r\n" + block("issue-closed: testing-library/jest-dom#738")).replace("\n", "\r\n")
        record = holds.parse_record(ADMITTED, 1, "open", comment(1, body))
        self.assertIsNotNone(record)
        self.assertEqual(record.fields["since"], "2026-09-10")
        self.assertEqual(len(record.probes), 1)


class TheConventions(HoldsCase):
    def test_it_is_posix_sh_named_after_the_folder_and_resolves_its_own_folder(self):
        text = ENTRYPOINT.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn("set -e", text)
        self.assertIn('DIR="$(cd "$(dirname "$0")" && pwd)"', text)
        self.assertEqual(ENTRYPOINT.name, FOLDER.name.removeprefix("local-") + ".sh")
        self.assertEqual(subprocess.run(["/bin/sh", "-n", str(ENTRYPOINT)]).returncode, 0)

    def test_no_argument_is_usage_on_stderr_and_exit_1_and_help_is_exit_0(self):
        done = subprocess.run(["/bin/sh", str(ENTRYPOINT)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 1)
        self.assertIn("Usage:", done.stderr)
        self.assertEqual(done.stdout, "")
        for flag in ("help", "-h", "--help"):
            done = subprocess.run(["/bin/sh", str(ENTRYPOINT), flag], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, flag)
            self.assertIn("holds", done.stderr)

    def test_the_watcher_exits_1_only_when_it_cannot_run_at_all(self):
        del self.state["viewer_login"]
        self.state["errors"]["user"] = "HTTP 401: Bad credentials"
        self.run_holds(expect=1)


# --- review findings carried on sd:1170 ---


class ReviewFindings(HoldsCase):
    def test_prerelease_identifiers_compare_field_by_field_numerics_as_numbers(self):
        self.assertTrue(holds.compare("1.0.0-rc.10", ">", "1.0.0-rc.2"))
        self.assertFalse(holds.compare("1.0.0-rc.10", "<", "1.0.0-rc.2"))
        # semver 2.0.0 section 11, in order.
        ordered = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2",
                   "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0"]
        for lower, higher in zip(ordered, ordered[1:]):
            self.assertTrue(holds.compare(lower, "<", higher), (lower, higher))

    def test_an_npm_dep_range_with_spaces_parses(self):
        for wanted in ("^9.0.1 || ^10.0.0", "9.0.0 - 10.2.0", ">=9.0.0 <11"):
            probe = holds.parse_probe(f'    - npm-dep: "@payloadcms/email-nodemailer nodemailer {wanted}"')
            self.assertEqual(probe.parts, ("@payloadcms/email-nodemailer", "nodemailer", wanted))

    def test_an_operator_spaced_from_its_version_reads_as_npm_reads_it(self):
        # npm: `>= 9.0.0` is `>=9.0.0`, which admits 10.0.0.
        self.assertTrue(holds.ranges_overlap(">= 9.0.0", "10.0.0"))
        self.assertTrue(holds.ranges_overlap("10.0.0", ">= 9.0.0 < 11"))
        self.assertFalse(holds.ranges_overlap("> 10.0.0", "10.0.0"))
        self.assertTrue(holds.ranges_overlap("^ 9.1.0", "9.2.0"))

    def test_an_npm_dep_range_with_spaces_is_evaluated(self):
        self.add_pull(308, block('npm-dep: "@payloadcms/email-nodemailer nodemailer ^8 || ^9"'))
        log = self.run_holds(dry_run=True)
        self.assertEqual(self.lines(log, "MALFORMED"), [])
        self.assertEqual(len(self.lines(log, "WOULD-LIFT")), 1)

    def test_a_declared_prerelease_is_unknown_and_never_lifts(self):
        # npm's semver: `satisfies("10.0.0-rc.1", ">=9 <11")` is false, as a
        # prerelease matches only a comparator that names its own triple.
        self.state["npm"]["@payloadcms/email-nodemailer"] = {"version": "3.90.0",
                                                             "dependencies": {"nodemailer": "=10.0.0-rc.1"}}
        self.add_pull(308, block('npm-dep: "@payloadcms/email-nodemailer nodemailer >=9 <11"'))
        log = self.run_holds(dry_run=True)
        self.assertEqual(self.lines(log, "WOULD-LIFT"), [])
        self.assertEqual(self.lines(log, "UNKNOWN"),
                         [f"UNKNOWN #308 {ADMITTED} npm-dep @payloadcms/email-nodemailer nodemailer >=9 <11: "
                          "3.90.0 declares nodemailer '=10.0.0-rc.1', which is not a range this reads"])

    def test_a_wanted_prerelease_is_malformed(self):
        for wanted in (">=10.0.0-rc.1 <11", "10.0.0-rc.1 - 11", ">=9 <10.0.0-0", "^9 || =10.0.0-beta"):
            probe = holds.parse_probe(f'    - npm-dep: "@payloadcms/email-nodemailer nodemailer {wanted}"')
            self.assertTrue(probe.malformed, wanted)

    def test_an_overlap_holding_only_prereleases_is_no_overlap(self):
        # `>=10` starts at 10.0.0-0 and `<10.0.0` stops at 10.0.0: only
        # prereleases lie between, and npm's ranges exclude them.
        self.assertFalse(holds.ranges_overlap("<10.0.0", ">=10"))
        self.assertFalse(holds.ranges_overlap(">=10 <10.0.0", "*"))
        self.assertFalse(holds.ranges_overlap("<=9.9.9 || <10.0.0", "^10"))
        self.assertTrue(holds.ranges_overlap("<=10.0.0", ">=10"))
        self.assertTrue(holds.ranges_overlap(">9.9.9", "<10.0.0"))
        self.assertTrue(holds.ranges_overlap(">10.0.0 <10.0.2", "*"))
        self.assertFalse(holds.ranges_overlap(">10.0.0 <10.0.1", "*"))

    def test_only_a_yaml_fence_is_a_block(self):
        for fence in ("```", "```text", "```json"):
            body = block("issue-closed: testing-library/jest-dom#738").replace("```yaml", fence, 1)
            self.assertIsNone(holds.parse_record(ADMITTED, 1, "open", comment(1, body)), fence)

    def test_a_block_without_a_required_field_is_malformed_and_never_lifts(self):
        # A true probe would lift a well-formed record; this one names no reason.
        body = block("issue-closed: testing-library/jest-dom#700").replace(
            "  reason: vitest 5 Assertion<R, T>; jest-dom 7.0.1 augments Assertion<T>\n", "")
        self.add_pull(308, body)
        self.add_pull(309, "\n".join(["```yaml", "sd-hold:", "  since: 2026-09-10", "  reason: r",
                                      "  lifts-when:", "```"]))
        log = self.run_holds()
        self.assertEqual(self.journal(), [])
        self.assertIn(f"MALFORMED #308 {ADMITTED} missing reason", log)
        self.assertIn(f"MALFORMED #309 {ADMITTED} lifts-when names no probe", log)
        self.assertEqual(self.lines(log, "LIFTED") + self.lines(log, "HELD"), [])

    def test_a_missing_runner_is_a_read_failure_not_a_traceback(self):
        self.add_pull(308, block('npm-version: "@testing-library/jest-dom > 7.0.1"'))
        log = self.run_holds(env={"SD_HOLDS_NPM": "/nonexistent/npm"})
        self.assertEqual(len([l for l in self.lines(log, "UNKNOWN") if "#308" in l]), 1)
        self.run_holds(env={"SD_HOLDS_GH": "/nonexistent/gh"}, expect=1)
        self.assertIn("dependabot holds: cannot read the authenticated login", self.stderr)
        self.assertNotIn("Traceback", self.stderr)

    def test_scanned_comes_before_the_repositorys_record_lines(self):
        self.add_pull(308, block("issue-closed: testing-library/jest-dom#738"), login="stranger")
        log = self.run_holds()
        self.assertLess(log.index(f"SCANNED {ADMITTED} 1 Dependabot pull requests, 0 records"),
                        log.index(f"IGNORED #308 {ADMITTED} stranger no write access"))

    def test_a_dry_run_carry_evaluates_the_record_it_would_carry(self):
        self.add_pull(309, block("issue-closed: testing-library/jest-dom#700"), state="closed",
                      more=[dependabot_says(6001, "Superseded by #320.")])
        self.repo()["pulls"].append(pull(320, comments=0))
        log = self.run_holds(dry_run=True)
        self.assertEqual(self.journal(), [])
        self.assertIn(f"WOULD-CARRY #309 -> #320 {ADMITTED}", log)
        self.assertEqual([l for l in self.lines(log, "WOULD-LIFT") if l.startswith("WOULD-LIFT #320 ")],
                         [f"WOULD-LIFT #320 {ADMITTED} issue-closed testing-library/jest-dom#700 "
                          "https://github.com/testing-library/jest-dom/issues/700 closed"])


class TheFleet(HoldsCase):
    def checkout(self, root, relative, slug):
        path = root / relative
        path.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", f"https://github.com/{slug}.git"],
                       check=True)

    def test_the_fleet_is_every_checkout_one_or_two_levels_down_as_routine_md_finds_it(self):
        # ROUTINE.md: find "$HOME/repos" -mindepth 2 -maxdepth 3 -name .git
        root = Path(self.tmp.name) / "repos"
        self.checkout(root, "direct", "platypeeps/direct")
        self.checkout(root, "group/grouped", "platypeeps/grouped")
        self.checkout(root, "group/nested/too-deep", "platypeeps/too-deep")
        self.assertEqual(holds.fleet(root), ["platypeeps/direct", "platypeeps/grouped"])

    def test_a_repository_that_fails_to_read_is_named_and_the_pass_goes_on(self):
        second = "platypeeps/second"
        self.state["viewer"][second] = "ADMIN"
        self.state["repos"][second] = {"pulls": [], "comments": {}, "permission": {
            ME: {"permission": "admin", "role_name": "admin"}}}
        self.state["errors"][f"repos/{ADMITTED}/issues"] = "HTTP 403: API rate limit exceeded"
        self.add_pull(330, block("issue-closed: testing-library/jest-dom#738"), slug=second)
        log = self.run_holds(ADMITTED, second)
        self.assertIn(f"UNREADABLE {ADMITTED}: HTTP 403: API rate limit exceeded", log)
        self.assertIn(f"HELD #330 {second} 1 probes, none lifted", log)
        self.assertTrue(log[-1].startswith("TOTAL "), log)


    def test_a_failed_write_still_fails_the_run_and_is_not_called_unreadable(self):
        cid = self.add_pull(331, block("issue-closed: testing-library/jest-dom#700"))
        self.state["errors"][f"repos/{ADMITTED}/issues/comments/{cid}"] = "HTTP 403: Resource not accessible"
        log = self.run_holds(expect=1)
        self.assertEqual(self.lines(log, "UNREADABLE"), [])



class FenceBacktracking(unittest.TestCase):
    def test_indent_heavy_comment_without_a_closing_fence_parses_at_once(self):
        # Code scanning py/redos: the old pattern took seconds at 20 tabs and
        # grew about sevenfold per two more.
        body = "```yaml\nsd-hold:\n" + "\t" * 40
        started = time.monotonic()
        self.assertIsNone(holds.parse_record("o/r", 1, "open", {"id": 1, "user": {"login": "x"}, "body": body}))
        self.assertLess(time.monotonic() - started, 0.5)



class HelpNeedsNoPython(unittest.TestCase):
    # Review of #274: interpreter discovery ran before dispatch, so on a
    # machine without a 3.11 `--help` printed the Python error and exited 1.
    def run_entrypoint(self, *args):
        environment = dict(os.environ, PYTHON="/nonexistent/python3")
        return subprocess.run(["/bin/sh", str(ENTRYPOINT), *args], capture_output=True,
                              text=True, env=environment)

    def test_help_answers_and_exits_0_with_no_usable_python(self):
        for word in ("help", "-h", "--help"):
            with self.subTest(word=word):
                done = self.run_entrypoint(word)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn("Usage: dependabot.sh <command>", done.stderr)

    def test_no_argument_prints_usage_not_the_python_error(self):
        done = self.run_entrypoint()
        self.assertEqual(done.returncode, 1)
        self.assertIn("Usage: dependabot.sh <command>", done.stderr)
        self.assertNotIn("Python", done.stderr)

    def test_holds_still_names_the_python_it_cannot_use(self):
        done = self.run_entrypoint("holds")
        self.assertEqual(done.returncode, 1)
        self.assertIn("/nonexistent/python3 is too old", done.stderr)


if __name__ == "__main__":
    unittest.main()
