"""`nightly`: what it selects, what it skips, and what it refuses to do.

Selection is the whole of this verb -- enqueueing is two calls into `sd_db`
that the runner's own suite already covers. So most of these drive
`selectable()` directly, and the rest go through the entrypoint to pin the two
decisions that live in the shell rather than in Python: that the participation
list is parsed once, and that a runner which is not dispatching stops the
night before anything is queued.

`WhatItQueues` does enqueue, and only into the fixture's own store under a
temporary `HOME`. No live runner reads that store, so nothing it queues can
start an unattended planning run. It exists because a nightly that was only
ever dry-run passed the item id as a string for as long as it existed, and
`runner_exec._values` refused every enqueue while the night reported
"nothing to plan".

A few cases run `nightly` or `enqueue` in this process instead, against the
same temporary `HOME`: they read what the night asks of `sd_db`, and a child
process cannot be watched doing that.
"""

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from sd_db import create_item, paths, runner, runner_controls, runner_exec, upsert_repo
from sd_db.workflow import capture_task, edit_item, item_state

import sd_plan

from tests.test_item import ItemCase, FOLDER, ENTRYPOINT

#: Answers `status` the way the real runner does, with the two fields the
#: entrypoint reads. Four lines, so the suite needs no runner installed.
RUNNER = """\
#!/bin/sh
printf '%%s\\n' '{"healthy": %s, "storage": {"dispatch_allowed": %s}}'
"""


class NightlyCase(ItemCase):
    def runner(self, *, healthy=True, dispatching=True):
        path = self.home / "runner.sh"
        path.write_text(
            RUNNER % ("true" if healthy else "false",
                      "true" if dispatching else "false"),
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def stopped_runner(self):
        """A runner whose agent is not loaded: `status` exits 3 and says nothing."""
        path = self.home / "stopped-runner.sh"
        path.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def conf(self, *lines):
        path = self.home / "repos.fixture.conf"
        path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
        return path

    def nightly(self, *args, expect=0, runner=None, conf=None):
        """Run the verb through the entrypoint, with the conf staged in place.

        The entrypoint reads `<config>/sd-plan/repos.<profile>.conf`, and the
        config directory here is this case's temporary folder, so staging
        never touches a developer's own list (sd:1235).
        """
        profile = f"fixture-{self.home.name}"
        config = self.home / "config"
        (config / "sd-plan").mkdir(parents=True, exist_ok=True)
        staged = config / "sd-plan" / f"repos.{profile}.conf"
        if conf is not None:
            # A case that runs several nights stages over its own file.
            if not getattr(self, "staged", None):
                self.assertFalse(staged.exists(), f"{staged} predates the case")
                self.staged = staged
                self.addCleanup(staged.unlink, missing_ok=True)
            staged.write_text(Path(conf).read_text(encoding="utf-8"), encoding="utf-8")
        environment = dict(os.environ)
        environment["HOME"] = str(self.home)
        environment["SD_PLAN_PROFILE"] = profile
        environment["SYSTEM_TOOLS_CONFIG"] = str(config)
        environment["MACHINE_SETUP_STATE"] = str(self.home / "absent")
        environment["SD_PLAN_RUNNER"] = str(runner if runner is not None else self.runner())
        environment.pop("PYTHONPATH", None)
        done = subprocess.run(
            ["/bin/sh", str(ENTRYPOINT), "nightly", *args],
            capture_output=True, text=True, input="", env=environment,
        )
        self.assertEqual(done.returncode, expect, done.stdout + done.stderr)
        return done

    def assignments(self):
        return [dict(row) for row in self.connection.execute("SELECT * FROM assignment")]

    def queued(self, identifier):
        return [row for row in self.assignments()
                if row["item"] == identifier and row["status"] == "queued"]

    def palette(self, screens=("item",), mutates=True):
        """The `plan-item` entry `local-sd-runner/commands.example.yaml` ships.

        Its argv[0] is a stub rather than the entrypoint: the catalog hashes
        an owned executable, and nothing here runs it. `screens` is the one
        thing a case varies: the nightly asks for the item screen, and an
        entry registered anywhere else is a refusal it cannot queue past.
        `mutates` is the other: an entry that does not mutate is never
        queued, and the night refuses it too (sd:814).
        """
        program = self.home / "plan-stub"
        program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        program.chmod(0o755)
        entry = {"label": "Write the planning documents", "argv": [str(program), "item", "{item}"],
                 "screens": list(screens), "mutates": mutates, "scope": "worktree",
                 "placeholders": {"item": "item"}}
        path = self.home / ".local/share/sd/commands.yaml"
        path.write_text(f"version: 1\ncommands:\n  plan-item: {json.dumps(entry)}\n", encoding="utf-8")
        path.chmod(0o644)
        return path

    def second_repository(self):
        """Another registered repository with its own origin, sorted after `repo`."""
        origin = self.home / "second.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)],
                       check=True, capture_output=True)
        second = self.home / "second"
        second.mkdir()
        for args in (("init", "-q", "-b", "main"),
                     ("config", "user.email", "fixture@example.invalid"),
                     ("config", "user.name", "Fixture"),
                     ("remote", "add", "origin", str(origin)),
                     ("commit", "-q", "--allow-empty", "-m", "first"),
                     ("push", "-q", "-u", "origin", "main")):
            self.git(*args, cwd=second)
        upsert_repo(self.connection, paths.key(str(second.resolve())), remote=str(origin),
                    status_source="row")
        return second.resolve()

    def restore_pending(self):
        """A refusal that hits every row: `runner_exec.prepare` waits for restore recovery."""
        self.connection.execute(
            "INSERT INTO state (kind, key, timestamp, body) "
            "VALUES ('restore', 'fixture', '2026-09-13T00:00:00Z', '{}')")
        self.connection.commit()

    def restore_finished(self):
        self.connection.execute("DELETE FROM state WHERE kind = 'restore'")
        self.connection.commit()

    def night_in_process(self, *repositories, dry_run=False):
        """`nightly` in this process, so what it asks of `sd_db` can be read.

        `dry_run` for the one case that has to watch a dry run ask nothing
        (sd:877): the entrypoint runs in a subprocess, where no patch on
        `sd_plan.runner_exec` reaches it.
        """
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}), \
                mock.patch("sys.stdin", io.StringIO("".join(f"{path}\n" for path in repositories))), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sd_plan.nightly(argparse.Namespace(dry_run=dry_run))
        return code, out.getvalue(), err.getvalue()


class WhatItSelects(NightlyCase):
    def test_it_takes_one_row_per_repository_and_not_two(self):
        for title in ("first shape", "second shape", "third shape"):
            self.task(title=title)
        chosen = sd_plan.selectable(self.connection, str(self.repo.resolve()),
                                    sd_plan.PER_REPOSITORY)
        self.assertEqual(len(chosen), 1, chosen)

    def test_the_constant_is_one_and_lives_in_exactly_one_place(self):
        self.assertEqual(sd_plan.PER_REPOSITORY, 1)
        source = (FOLDER / "sd_plan.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("PER_REPOSITORY ="), 1)

    def test_priority_order_decides_which_row_is_taken(self):
        self.task(title="ordinary shape")
        urgent = self.task(title="urgent shape", priority=1)
        chosen = sd_plan.selectable(self.connection, str(self.repo.resolve()), 5)
        self.assertEqual(chosen[0]["id"], urgent,
                         "backlog_items orders by priority; selection must not reorder it")

    def test_a_row_whose_folder_already_exists_is_not_a_candidate(self):
        identifier = self.task(title="already planned")
        chosen = sd_plan.selectable(self.connection, str(self.repo.resolve()), 5)
        slug = next(row["slug"] for row in chosen if row["id"] == identifier)
        (self.repo / "docs" / "work" / slug).mkdir(parents=True)
        again = sd_plan.selectable(self.connection, str(self.repo.resolve()), 5)
        self.assertNotIn(identifier, [row["id"] for row in again])

    def test_a_row_with_no_usable_date_is_skipped_rather_than_failing_the_night(self):
        identifier = self.task(title="undated shape")
        self.connection.execute("UPDATE item SET created_at = '' WHERE id = ?", (identifier,))
        chosen = sd_plan.selectable(self.connection, str(self.repo.resolve()), 5)
        self.assertNotIn(identifier, [row["id"] for row in chosen])

    def test_a_row_whose_date_has_the_shape_and_no_day_is_skipped(self):
        """Parsed, not shape-matched: `2026-02-30` is no date (sd:1181)."""
        identifier = self.task(title="impossibly dated shape")
        self.connection.execute("UPDATE item SET created_at = '2026-02-30T00:00:00+00:00' "
                                "WHERE id = ?", (identifier,))
        chosen = sd_plan.selectable(self.connection, str(self.repo.resolve()), 5)
        self.assertNotIn(identifier, [row["id"] for row in chosen])

    def test_a_title_that_yields_no_slug_is_skipped_rather_than_failing_the_night(self):
        identifier = self.task(title="!!! ???")
        chosen = sd_plan.selectable(self.connection, str(self.repo.resolve()), 5)
        self.assertNotIn(identifier, [row["id"] for row in chosen])

    def test_the_docstring_of_published_says_what_an_unreachable_remote_costs(self):
        """A remote that cannot be asked lets the row through; setup then refuses it.

        The docstring said the row is queued, which is true only for a branch
        set by hand outside `plan/` (README.md, "Setup then asks the remote
        again"). A sentence that is false about the refusal it describes is
        how the next reader learns the wrong rule (sd:793).
        """
        text = " ".join((sd_plan.published.__doc__ or "").split())
        self.assertNotIn("the row is then queued", text)
        self.assertIn("Setup then asks the remote again", text)
        readme = " ".join((FOLDER / "README.md").read_text(encoding="utf-8").split())
        self.assertIn("Setup then asks the remote again", readme)

    def test_the_branch_it_would_set_is_the_one_item_would_derive(self):
        self.task(title="a shape to name")
        chosen = sd_plan.selectable(self.connection, str(self.repo.resolve()), 1)
        self.assertIsNone(chosen[0]["branch"],
                          "a row from capture_task carries no branch; that is the premise")


class WhatItRefuses(NightlyCase):
    def test_a_runner_that_is_not_dispatching_stops_the_night(self):
        done = self.nightly(expect=1, runner=self.runner(dispatching=False),
                            conf=self.conf(str(self.repo.resolve())))
        self.assertIn("not dispatching", done.stderr)
        self.assertEqual(self.assignments(), [])

    def test_an_unhealthy_runner_stops_the_night(self):
        self.nightly(expect=1, runner=self.runner(healthy=False),
                     conf=self.conf(str(self.repo.resolve())))
        self.assertEqual(self.assignments(), [])

    def test_no_participation_list_is_not_a_failure(self):
        done = self.nightly()
        self.assertIn("no repository participates", done.stdout)

    def test_an_unconfigured_machine_does_not_fail_on_a_runner_it_never_uses(self):
        """sd:1202. The list is read before the runner is probed.

        2026-09-20 read the other order: this machine has never had a
        `repos.personal.conf`, the runner happened not to be up at 04:45, and
        a night that was going to enqueue nothing logged `FAILED rc=1`. The
        finding named a runner the night had no work for.
        """
        done = self.nightly(runner=self.stopped_runner())
        self.assertIn("no repository participates", done.stdout)
        self.assertNotIn("runner", done.stderr)
        self.assertEqual(self.assignments(), [])

    def test_a_participating_machine_still_fails_on_a_stopped_runner(self):
        """The reorder changes when the runner is asked, not what the answer costs.

        A stopped runner reads as 1, the same as one that answers and refuses
        to dispatch. `cron-jobs.sh` notifies on every non-zero code, so a
        distinct code here would page anyway; see the README section.
        """
        done = self.nightly(expect=1, runner=self.stopped_runner(),
                            conf=self.conf(str(self.repo.resolve())))
        self.assertIn("not dispatching", done.stderr)
        self.assertEqual(self.assignments(), [])

    def test_an_empty_participation_list_is_not_a_failure(self):
        done = self.nightly(conf=self.conf("# nobody yet", ""))
        self.assertIn("no repository participates", done.stdout)

    def test_a_palette_that_cannot_be_read_fails_the_night_rather_than_one_repository(self):
        """"Could not ask" is a failure; a repository declining is not.

        The distinction is load-bearing: the per-repository guard swallows a
        refusal and moves on, and a missing catalog inside that guard would
        turn "nothing is configured" into a silent no-op across the fleet.
        """
        self.task(title="a shape worth planning")
        done = self.nightly(expect=1, conf=self.conf(str(self.repo.resolve())))
        self.assertIn("No command palette is configured", done.stderr)
        self.assertEqual(self.assignments(), [])

    def test_a_participant_that_is_not_registered_is_named_and_not_guessed_at(self):
        done = self.nightly(conf=self.conf("/nowhere/at/all"))
        self.assertIn("/nowhere/at/all", done.stderr)
        self.assertIn("not a registered repository", done.stderr)


class WhatItLeavesBesideTheScript(unittest.TestCase):
    def test_a_night_leaves_a_conf_it_did_not_write_alone(self):
        """sd:1235. `nightly()` staged its conf as `repos.fixture.conf` and
        unlinked it afterwards, whoever's it was. It now stages in a config
        directory inside the case's temporary folder, so the script's folder
        must come back unchanged."""
        before = sorted(path.name for path in FOLDER.iterdir())
        result = unittest.TestResult()
        WhatItRefuses("test_an_empty_participation_list_is_not_a_failure").run(result)
        self.assertEqual([], [text for _, text in result.errors + result.failures])
        self.assertEqual(before, sorted(path.name for path in FOLDER.iterdir()),
                         "the night wrote beside the script")


class WhatItQueues(NightlyCase):
    def test_a_selected_row_is_queued_as_the_plan_item_command(self):
        identifier = self.task(title="a shape worth planning")
        self.palette()
        done = self.nightly(conf=self.conf(str(self.repo.resolve())))
        self.assertNotIn("was not enqueued", done.stderr)
        self.assertIn(f"queued item {identifier}", done.stdout)
        assignment, = self.queued(identifier)
        self.assertEqual(assignment["role"], "exec")
        request = json.loads(assignment["scope"].removeprefix("palette:"))
        self.assertEqual(request["command"], "plan-item")
        self.assertEqual(request["values"], {"item": identifier})

    def refusing(self, title, repo=None):
        """A row the queue refuses after selection: its branch was set by hand to
        one the runner will not take, so `runner.enqueue` raises `RunnerRefused`
        ("needs a valid branch"), which is not a `WorkflowError`."""
        identifier = capture_task(self.connection, title=title,
                                  repo=str(repo or self.repo.resolve()), who="operator")["item"]["id"]
        self.connection.execute("UPDATE item SET branch = 'not a branch' WHERE id = ?", (identifier,))
        self.connection.commit()
        return identifier

    def exec_notes(self, identifier):
        return self.connection.execute(
            "SELECT COUNT(*) FROM note WHERE item = ? AND kind = 'exec'", (identifier,)).fetchone()[0]

    def test_a_repository_that_refuses_does_not_cost_the_rest_of_the_fleet(self):
        first = str(self.repo.resolve())
        held = self.refusing("a shape the queue will not take")
        self.palette()
        second = self.second_repository()
        waiting = capture_task(self.connection, title="a shape in the next repository",
                               repo=str(second), who="operator")["item"]["id"]
        done = self.nightly(conf=self.conf(first, str(second)))
        self.assertNotIn("Traceback", done.stderr)
        self.assertIn(f"item {held} in {first} was not enqueued", done.stderr)
        self.assertIn("needs a valid branch", done.stderr)
        self.assertEqual(len(self.queued(waiting)), 1, done.stdout + done.stderr)
        # The refusal rolled back whole: no authorization is left for the held row.
        self.assertEqual(self.exec_notes(held), 0)

    def test_a_refusal_moves_on_to_the_next_row_in_the_same_repository(self):
        """`PER_REPOSITORY` counts rows queued, not rows tried."""
        held = self.refusing("a shape the queue will not take")
        waiting = self.task(title="a shape behind it")
        self.palette()
        done = self.nightly(conf=self.conf(str(self.repo.resolve())))
        self.assertIn(f"item {held} in {self.repo.resolve()} was not enqueued", done.stderr)
        self.assertEqual(len(self.queued(waiting)), 1, done.stdout + done.stderr)
        self.assertNotIn("nothing to plan tonight", done.stdout)

    def test_the_standing_refusal_is_asked_once_per_repository_on_the_first_row_tried(self):
        """PIN (sd:806). One question per repository, put to the first row the night tries.

        The first row here is refused on its own account, by the queue, which
        is not a standing refusal, so the night moves on to the row behind it.
        That row is not asked about again, and the next repository is asked
        about its own first row. Read once, the answer can go stale for the
        rows after it; the comment in `nightly` says how, and what it costs.
        """
        first = str(self.repo.resolve())
        held = self.refusing("a shape the queue will not take")
        waiting = self.task(title="a shape behind it")
        self.palette()
        second = self.second_repository()
        elsewhere = capture_task(self.connection, title="a shape in the next repository",
                                 repo=str(second), who="operator")["item"]["id"]
        asked = []
        original = runner_exec.standing_refusal

        def counted(connection, command, values, **options):
            asked.append(values["item"])
            return original(connection, command, values, **options)

        with mock.patch.object(sd_plan.runner_exec, "standing_refusal", side_effect=counted):
            code, out, err = self.night_in_process(first, str(second))
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"item {held} in {first} was not enqueued", err)
        self.assertEqual(len(self.queued(waiting)), 1, out + err)
        self.assertEqual(len(self.queued(elsewhere)), 1, out + err)
        self.assertEqual(asked, [held, elsewhere],
                         "standing_refusal must be asked once per repository, about its first row tried")

    def test_enqueue_always_sets_the_row_up(self):
        """PIN (sd:806, sd:826). `enqueue` has no way to queue a row unset up.

        The setup is the half that keeps a row's saved base current (sd:778).
        It was once optional, for the night whose standing answer refused;
        that night now stops at the refusal (sd:826), so the option is gone
        and this holds it gone. Without the setup this row, which has no
        branch yet, is refused by the queue.
        """
        identifier = self.task(title="a shape queued by a caller that does not say")
        self.palette()
        candidate, = sd_plan.selectable(self.connection, str(self.repo.resolve()), 1)
        self.assertEqual(candidate["id"], identifier)
        try:
            with mock.patch.dict(os.environ, {"HOME": str(self.home)}), \
                    contextlib.redirect_stdout(io.StringIO()):
                sd_plan.enqueue(self.connection, str(self.repo.resolve()), candidate,
                                runner_exec.catalog(home=self.home)["sha256"])
        except runner.RunnerRefused as refusal:
            self.fail(f"enqueue did not set the row up: {refusal}")
        self.assertEqual(len(self.queued(identifier)), 1)
        branch = self.connection.execute("SELECT branch FROM item WHERE id = ?", (identifier,)).fetchone()[0]
        self.assertEqual(branch, f"plan/{candidate['slug']}")

    def test_an_empty_branch_is_set_up_on_its_plan_branch_as_no_branch_is(self):
        """'' is no branch, as NULL is: the row is set up on `plan/<slug>` and queued (sd:806).

        Neither `configure_item` nor the item form stores '', so only a direct
        write reaches this. Treated as a branch of its own, the row is refused
        instead: by the queue when setup skips it, and by setup when '' is
        what setup is handed.
        """
        identifier = self.task(title="a shape with an empty branch")
        slug = sd_plan.selectable(self.connection, str(self.repo.resolve()), 1)[0]["slug"]
        self.connection.execute("UPDATE item SET branch = '' WHERE id = ?", (identifier,))
        self.connection.commit()
        self.palette()
        done = self.nightly(conf=self.conf(str(self.repo.resolve())))
        self.assertNotIn("was not enqueued", done.stderr)
        self.assertEqual(len(self.queued(identifier)), 1, done.stdout + done.stderr)
        branch = self.connection.execute("SELECT branch FROM item WHERE id = ?", (identifier,)).fetchone()[0]
        self.assertEqual(branch, f"plan/{slug}")


class WhatTheNextNightPlans(NightlyCase):
    """A planned row stays `planning`, and its folder stays off this checkout.

    The plan lands on `plan/<slug>` in a runner clone, and a mutating worktree
    command leaves the task's status alone, so "the folder exists here" is not
    true until that branch merges and the checkout pulls. Without more than
    that check the nightly queued the same top row every night, and the row
    behind it was never planned.
    """

    def first_night(self):
        self.first = self.task(title="the first shape")
        self.second = self.task(title="the second shape")
        self.palette()
        self.nightly(conf=self.conf(str(self.repo.resolve())))
        assignment, = self.queued(self.first)
        self.assertEqual(self.queued(self.second), [])
        self.branch = self.connection.execute(
            "SELECT branch FROM item WHERE id = ?", (self.first,)).fetchone()[0]
        self.assertTrue(self.branch.startswith("plan/"), self.branch)
        return assignment["id"]

    def set_status(self, assignment, status):
        self.connection.execute("UPDATE assignment SET status = ? WHERE id = ?", (status, assignment))
        self.connection.commit()

    def register_folder(self):
        """The row `sd work register` makes for the first row's folder."""
        slug = self.branch.removeprefix("plan/")
        relative = f"docs/work/{slug}/prd.md"
        # Keyed as the pack keys it: the repository's `~/...` key (sd:1439).
        key = paths.key(str(self.repo.resolve()))
        create_item(self.connection, kind="work", title=slug, repo=key,
                    branch=self.branch, path=relative, source="docs/work",
                    external_id=f"{key}::{relative}")

    def publish_branch(self):
        self.git("push", "-q", "origin", f"main:refs/heads/{self.branch}")

    def second_night(self):
        def first_row():
            return [row for row in self.assignments() if row["item"] == self.first]
        before = first_row()
        done = self.nightly(conf=self.conf(str(self.repo.resolve())))
        self.assertEqual(len(self.queued(self.second)), 1,
                         "night two did not reach the next row: " + done.stdout + done.stderr)
        self.assertEqual(first_row(), before, "night two queued the first row again")
        return done

    def test_night_two_plans_the_second_row_after_the_first_is_planned(self):
        self.set_status(self.first_night(), "done")
        self.register_folder()
        self.publish_branch()
        self.second_night()

    def test_a_registered_folder_alone_passes_the_row_over(self):
        self.set_status(self.first_night(), "done")
        self.register_folder()
        self.second_night()

    def test_a_published_plan_branch_alone_passes_the_row_over(self):
        self.set_status(self.first_night(), "done")
        self.publish_branch()
        self.second_night()

    def test_a_kept_run_passes_the_row_over_without_a_refusal(self):
        self.set_status(self.first_night(), "ending")
        done = self.second_night()
        self.assertNotIn("was not enqueued", done.stderr)

    def test_a_blocked_run_passes_the_row_over(self):
        self.set_status(self.first_night(), "blocked")
        self.second_night()

    def test_a_row_held_by_a_blocked_run_is_named_on_stderr(self):
        """A blocked row is held every night after, so a silent skip loses it for good."""
        self.set_status(self.first_night(), "blocked")
        done = self.second_night()
        self.assertIn(f"item {self.first} in {self.repo.resolve()} is held by a blocked run; "
                      "plan it again from the item's button", done.stderr)

    def recovery_run(self):
        """What the operator's button leaves on a blocked row: a second assignment.

        The blocked one stays on the row, so every state below has both.
        """
        self.set_status(self.first_night(), "blocked")
        runner_exec.prepare(
            self.connection, self.first, "plan-item", {"item": self.first},
            expected_revision=item_state(self.connection, self.first)["revision"],
            expected_catalog=runner_exec.catalog(home=self.home)["sha256"],
            screen="item", home=self.home, who="operator")
        assignment, = self.queued(self.first)
        return assignment["id"]

    def test_a_row_planned_again_after_a_blocked_run_is_not_named(self):
        """Blocked plus queued is the state the recovery leaves: the new run holds the row."""
        self.recovery_run()
        self.assertEqual(len(self.queued(self.first)), 1)
        done = self.second_night()
        self.assertNotIn("held by a blocked run", done.stderr)

    def test_a_row_whose_recovery_run_is_running_is_not_named(self):
        """Blocked plus running: the recovery is under way, and that is its own news."""
        self.set_status(self.recovery_run(), "running")
        done = self.second_night()
        self.assertNotIn("held by a blocked run", done.stderr)

    def test_a_row_whose_recovery_run_has_finished_is_not_named(self):
        """A finished recovery is in no holding status, so the blocked run is all that is left.

        Naming the row then tells the operator to plan again what they have
        already planned again, every night until `plan/<slug>` merges and this
        checkout pulls (sd:793).
        """
        self.set_status(self.recovery_run(), "done")
        done = self.second_night()
        self.assertNotIn("held by a blocked run", done.stderr)

    def another_rows_run(self, status):
        """The store's newest assignment, on a row that is not the one under test.

        In every case above the row under test also owns the newest assignment
        in the whole store when the night reads it, so the newest-assignment
        query answered the same with its `item = ?` gone. This row sorts after
        both of the fixture's, so night two reaches it only after the second
        row is queued.
        """
        other = self.task(title="a shape on another row")
        repo = str(self.repo.resolve())
        runner_controls.configure_item(
            self.connection, other, repo=repo, branch="plan/another-row",
            expected_revision=item_state(self.connection, other)["revision"], who="operator")
        runner_exec.prepare(
            self.connection, other, "plan-item", {"item": other},
            expected_revision=item_state(self.connection, other)["revision"],
            expected_catalog=runner_exec.catalog(home=self.home)["sha256"],
            screen="item", home=self.home, who="operator")
        assignment, = self.queued(other)
        self.set_status(assignment["id"], status)
        newest = self.connection.execute(
            "SELECT item, status FROM assignment ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual((newest["item"], newest["status"]), (other, status),
                         "the premise: another row owns the store's newest assignment")
        return other

    def test_a_blocked_row_is_named_when_another_row_has_the_newest_assignment(self):
        """The newest assignment read is the row's own, not the store's (sd:806).

        Read store-wide, another row's finished run would stand in for this
        row's blocked one, and the row would be passed over in silence.
        """
        self.set_status(self.first_night(), "blocked")
        self.another_rows_run("done")
        done = self.second_night()
        self.assertIn(f"item {self.first} in {self.repo.resolve()} is held by a blocked run; "
                      "plan it again from the item's button", done.stderr)

    def test_a_finished_recovery_is_not_named_when_another_row_is_blocked(self):
        """The mirror: another row's blocked run does not name this row (sd:806).

        Read store-wide, the other row's blocked run would stand in for this
        row's finished recovery, and the row would be told to plan again what
        it already has. The other row is named, which is what shows there was a
        blocked run in the store for a store-wide read to find.
        """
        self.set_status(self.recovery_run(), "done")
        other = self.another_rows_run("blocked")
        done = self.second_night()
        self.assertNotIn(f"item {self.first} in {self.repo.resolve()} is held by a blocked run",
                         done.stderr)
        self.assertIn(f"item {other} in {self.repo.resolve()} is held by a blocked run", done.stderr)

    def test_an_empty_branch_is_asked_of_the_remote_as_its_plan_branch(self):
        """'' is no branch, as NULL is: the remote is asked about `plan/<slug>` (sd:806).

        Neither `configure_item` nor the item form stores '', so only a direct
        write reaches this. Asked about '' instead, the remote is asked for
        `refs/heads/` and the published plan branch goes unseen. Read from the
        question put to `published`, not only from the row being passed over,
        because a row can be passed over for another reason.
        """
        self.set_status(self.first_night(), "done")
        self.publish_branch()
        self.connection.execute("UPDATE item SET branch = '' WHERE id = ?", (self.first,))
        self.connection.commit()
        with mock.patch.object(sd_plan, "published", wraps=sd_plan.published) as asked:
            chosen = sd_plan.selectable(self.connection, str(self.repo.resolve()), 5)
        branches = [call.args[1] for call in asked.call_args_list]
        self.assertIn(mock.call(str(self.origin), self.branch), asked.call_args_list, branches)
        self.assertNotIn("", branches)
        self.assertNotIn(self.first, [row["id"] for row in chosen])
        self.second_night()

    def test_a_renamed_row_whose_branch_is_published_is_not_queued_again(self):
        """The row keeps the branch it ran on, and its title can be edited after.

        Asking the remote about the slug the new title yields finds nothing,
        and the row was queued again every night after (sd:793, the sd:774
        class).
        """
        self.set_status(self.first_night(), "done")
        self.publish_branch()
        edit_item(self.connection, self.first, {"title": "the first shape, renamed"},
                  who="operator",
                  expected_revision=item_state(self.connection, self.first)["revision"])
        self.second_night()

    def test_the_named_recovery_is_not_a_requeue(self):
        """`runner.requeue` refuses every exec assignment, so the docs must not offer it."""
        assignment = self.first_night()
        self.set_status(assignment, "blocked")
        with self.assertRaisesRegex(runner.RunnerRefused, "single-use"):
            runner.requeue(self.connection, assignment,
                           expected_revision=runner.queue_state(self.connection, assignment)["revision"],
                           who="operator")
        for path in (FOLDER / "README.md", FOLDER / "sd_plan.py"):
            text = " ".join(path.read_text(encoding="utf-8").split())
            self.assertFalse("operator's to requeue" in text, path)
            self.assertTrue("plan it again from the item's button" in text, path)

    def test_a_queued_run_passes_the_row_over(self):
        self.first_night()
        done = self.second_night()
        self.assertNotIn("was not enqueued", done.stderr)
        self.assertNotIn("held by a blocked run", done.stderr)


class WhatSetupLeavesBehind(NightlyCase):
    """Setup is a write of its own, and it outlives the night that made it.

    `configure_item` saves the branch and the default branch's head. If the
    default branch moved before the row ran and the nightly did not set the
    row up again, the runner's branch step refused the run as "base changed
    after preparation", the run ended blocked, and `HOLDING` passed the row
    over on every night after that (sd:778).

    A night whose `prepare` refuses for the whole repository sets no row up at
    all, so it leaves neither a decision note nor a bumped revision behind
    (sd:786). Since sd:826 it does not reach `prepare` for those rows either:
    the standing answer stops the repository where it is read, so a refusal
    that ends later in the same night cannot queue a row on a base no setup
    refreshed.
    """

    def move_main(self):
        """The default branch moves on the remote, as it does in an active repository.

        A file of its own each time: a test that runs several nights needs a
        commit per night, and the same content twice commits nothing.
        """
        self.moves = getattr(self, "moves", 0) + 1
        name = f"later-{self.moves}.txt"
        (self.repo / name).write_text(f"later {self.moves}\n", encoding="utf-8")
        self.git("add", name)
        self.git("commit", "-qm", name)
        self.git("push", "-q", "origin", "main")
        return self.git("rev-parse", "main")

    def record(self, identifier):
        return dict(self.connection.execute("SELECT * FROM item WHERE id = ?", (identifier,)).fetchone())

    def seed(self, identifier):
        return json.loads(self.record(identifier)["fields"] or "{}").get("runner_branch")

    def branch_step(self, identifier):
        """The runner's own branch step, on a fresh clone of origin, for this row.

        Loaded from its file: `gitops` needs only `sd_db`, and importing the
        runner package would ask this suite for more than it installs.
        """
        spec = importlib.util.spec_from_file_location(
            "sd_plan_fixture_gitops", FOLDER.parent / "local-sd-runner" / "sd_runner" / "gitops.py")
        gitops = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gitops)
        work = self.home / f"work-{identifier}"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(work)],
                       check=True, capture_output=True)
        record = self.record(identifier)
        request = {"run": {"work_path": str(work), "repo": str(self.repo.resolve()),
                           "branch": record["branch"]},
                   "item_record": record, "scope": "palette:{}", "role": "exec", "lane": "serial"}
        return gitops.branch(request)

    def decisions(self, identifier):
        return self.connection.execute(
            "SELECT COUNT(*) FROM note WHERE item = ? AND kind = 'decision'",
            (identifier,)).fetchone()[0]

    def revision(self, identifier):
        return item_state(self.connection, identifier)["revision"]

    def off_screen(self):
        """A refusal that hits every row: `plan-item` is registered, elsewhere.

        The palette is an operator's file, so this one stands until somebody
        edits it -- longer than a restore takes, and the reason enumerating
        the refusals beat special casing the restore (sd:805).
        """
        self.palette(screens=["today"])

    def on_screen(self):
        self.palette()

    def store(self):
        """The sha256 of the store's whole SQL dump: every row, and the schema.

        A count of notes says nothing about a row rewritten in place, so the
        whole dump is what a night that must write nothing is held to.

        Not the file's bytes. The store is in WAL mode, so `sd.db` alone stays
        byte-identical through a night that wrote; and `sd.db` with its WAL
        moved on CI's SQLite build through nights whose dump did not change
        at all, while it held still locally. The night's subprocess opens and
        closes the store each time, and what that does to the WAL's bytes is
        the library's business rather than this repository's. Bytes that move
        with nothing written cannot say whether anything was.
        """
        return hashlib.sha256("\n".join(self.connection.iterdump()).encode()).hexdigest()

    def refused_night(self, identifier, hold=None, release=None,
                      because="restore recovery must finish"):
        """A night `prepare` refuses for every row, with the default branch moving.

        The moved branch is the whole point: it is what setup would write a
        note about. The repository is still named on stderr, because a
        refusal is news either way, and `because` pins which refusal did it
        -- a night silenced by the wrong one would pass every count below.
        The line names the repository and not the row: since sd:826 the
        answer stops the repository before any row is tried, so no row is at
        fault and none is named.

        `expect=1` since sd:842: a night that skipped a repository exits
        non-zero, which is what puts the skip in front of a person. It is
        asserted here rather than left to the caller because every one of
        these nights is refused by construction.
        """
        (hold or self.restore_pending)()
        self.move_main()
        done = self.nightly(expect=1, conf=self.conf(str(self.repo.resolve())))
        self.assertIn(f"sd-plan: {self.repo.resolve()} is not planned tonight", done.stderr)
        self.assertIn(because, done.stderr)
        self.assertEqual(self.queued(identifier), [])
        (release or self.restore_finished)()
        return done

    def left_by_a_cancelled_run(self, title):
        """Setup that outlived its night: the run was cancelled before it started.

        A refused night no longer leaves setup behind (sd:786), so this is how
        the state a stale base makes is reached here: `configure_item` ran, and
        no `HOLDING` status is left on the row.
        """
        identifier = self.task(title=title)
        self.palette()
        done = self.nightly(conf=self.conf(str(self.repo.resolve())))
        assignment, = self.queued(identifier)
        self.connection.execute("UPDATE assignment SET status = 'cancelled' WHERE id = ?",
                                (assignment["id"],))
        self.connection.commit()
        self.assertTrue(self.record(identifier)["branch"].startswith("plan/"),
                        done.stdout + done.stderr)
        return identifier, self.seed(identifier)["source_commit"]

    def next_night_runs(self, identifier, stale, moved):
        """The next night queues the row on a current base, and the branch step takes it."""
        self.assertNotEqual(stale, moved)
        done = self.nightly(conf=self.conf(str(self.repo.resolve())))
        self.assertEqual(len(self.queued(identifier)), 1, done.stdout + done.stderr)
        self.assertEqual(self.seed(identifier)["source_commit"], moved,
                         "the saved base is still the one the refused night saw")
        # This raises `RunnerRefused` ("new branch base changed after
        # preparation") when the saved base is stale.
        self.branch_step(identifier)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.home / f"work-{identifier}"), moved)

    def test_a_stale_setup_then_a_moved_default_branch_still_queues_and_runs_the_row(self):
        identifier, stale = self.left_by_a_cancelled_run("a shape set up once")
        self.next_night_runs(identifier, stale, self.move_main())

    def test_a_row_renamed_after_its_setup_still_runs_on_its_own_branch(self):
        """The slug follows the title, and the title can be edited at any time.

        The row keeps the branch it was set up on, so the refresh must follow
        the row's branch rather than the slug the new title yields.
        """
        identifier, stale = self.left_by_a_cancelled_run("a shape named once")
        branch = self.record(identifier)["branch"]
        edit_item(self.connection, identifier, {"title": "a shape named twice"}, who="operator",
                  expected_revision=item_state(self.connection, identifier)["revision"])
        self.next_night_runs(identifier, stale, self.move_main())
        self.assertEqual(self.record(identifier)["branch"], branch)

    def test_three_refused_nights_leave_no_note_and_no_revision_behind(self):
        """A row with no branch yet: setup would give it one, and a note with it.

        Three nights that queue nothing wrote three decision notes and three
        revisions into an item an operator may be reading (sd:786). The night
        after the refusal ends still sets the row up on a current base.
        """
        identifier = self.task(title="a shape no night can queue yet")
        self.palette()
        before = (self.decisions(identifier), self.revision(identifier))
        for _ in range(3):
            self.refused_night(identifier)
        self.assertEqual((self.decisions(identifier), self.revision(identifier)), before)
        self.assertIsNone(self.record(identifier)["branch"])
        stale = self.git("rev-parse", "main")
        self.next_night_runs(identifier, stale, self.move_main())

    def test_three_nights_refused_off_the_item_screen_write_nothing_at_all(self):
        """The second trigger of the sd:786 defect, and the one it did not cover.

        A restore ends by itself; `plan-item` registered on another screen
        stands until an operator edits the catalog. The night cannot queue the
        row either way, and before sd:805 it set the row up anyway -- a
        decision note and a bumped revision every night, for as long as the
        entry stayed put. Held to the whole store rather than to a note count,
        because the item row is rewritten in place and a count alone would
        pass a fix that still rewrote it.

        The last night is the other half: the refusal lifted, the row is set
        up and queued, and the store must move. A fix that made every night a
        no-op would pass everything above this line.
        """
        identifier = self.task(title="a shape no screen can queue yet")
        before = self.store()
        for _ in range(3):
            self.refused_night(identifier, hold=self.off_screen, release=self.on_screen,
                               because="this command is not registered on the current screen")
            self.assertEqual(self.store(), before)
        self.assertEqual(self.decisions(identifier), 0)
        self.assertIsNone(self.record(identifier)["branch"])
        stale = self.git("rev-parse", "main")
        self.next_night_runs(identifier, stale, self.move_main())
        self.assertNotEqual(self.store(), before,
                            "a night that is not refused must still set its row up")
        self.assertGreater(self.decisions(identifier), 0)

    def test_three_nights_with_a_plan_item_that_does_not_mutate_write_nothing_and_queue_nothing(self):
        """sd:814: `prepare` runs such an entry at once and never queues it.

        Before, each night set the row up, recorded an exec note for a
        command that never ran, and printed "queued item" -- one note, one
        decision and one false line a night. Held to the whole store, like
        the screen case above, and the last night lifts the refusal: a
        mutating entry sets the row up and queues it again.
        """
        identifier = self.task(title="a shape a read-only entry cannot queue")
        before = self.store()
        for _ in range(3):
            done = self.refused_night(identifier, hold=lambda: self.palette(mutates=False),
                                      release=lambda: None,
                                      because="plan-item is not a mutating worktree command, so it would not be queued")
            self.assertNotIn("queued item", done.stdout)
            self.assertEqual(self.store(), before)
        self.assertEqual(self.decisions(identifier), 0)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM note WHERE item = ? AND kind = 'exec'", (identifier,)).fetchone()[0], 0)
        self.assertIsNone(self.record(identifier)["branch"])
        self.palette()
        stale = self.git("rev-parse", "main")
        self.next_night_runs(identifier, stale, self.move_main())
        self.assertNotEqual(self.store(), before,
                            "a night with a mutating entry must still set its row up")
        self.assertGreater(self.decisions(identifier), 0)

    def test_a_plan_branch_set_by_hand_gains_nothing_off_the_item_screen_either(self):
        """The row a night rewrites even though it already has a branch.

        `configure_item` runs on it every night for the saved base, so it is
        the row a standing refusal costs the most: its branch never changes,
        only the note and the revision, which is why the store and not the
        branch is what this reads.

        The last night lifts the refusal and the store must move, as in the
        test above: this connection is the one every dump above was read
        through, and a dump that could not see a write would pass them all.
        """
        identifier = self.by_hand("a shape on a plan branch off screen", "plan/by-hand-offscreen")
        before, setup = self.store(), self.decisions(identifier)
        for _ in range(3):
            self.refused_night(identifier, hold=self.off_screen, release=self.on_screen,
                               because="this command is not registered on the current screen")
            self.assertEqual(self.store(), before)
        self.assertEqual(self.record(identifier)["branch"], "plan/by-hand-offscreen")
        self.assertEqual(self.decisions(identifier), setup)
        stale = self.git("rev-parse", "main")
        self.next_night_runs(identifier, stale, self.move_main())
        self.assertNotEqual(self.store(), before,
                            "a night that is not refused must still set its row up")
        self.assertGreater(self.decisions(identifier), setup)
        self.assertEqual(self.record(identifier)["branch"], "plan/by-hand-offscreen")

    def test_a_plan_branch_set_by_hand_gains_nothing_from_a_refused_night(self):
        """The same on a row the operator put on a `plan/` branch of their own.

        That row is refreshed every night the nightly reaches it, so the noise
        a refused night left was not only the new rows' (sd:786).
        """
        identifier = self.by_hand("a shape on a plan branch of its own", "plan/by-hand-noisy")
        self.palette()
        before = (self.decisions(identifier), self.revision(identifier))
        for _ in range(3):
            self.refused_night(identifier)
        self.assertEqual((self.decisions(identifier), self.revision(identifier)), before)
        self.assertEqual(self.record(identifier)["branch"], "plan/by-hand-noisy")

    def by_hand(self, title, branch):
        identifier = self.task(title=title)
        runner_controls.configure_item(
            self.connection, identifier, repo=str(self.repo.resolve()), branch=branch,
            expected_revision=item_state(self.connection, identifier)["revision"], who="operator")
        return identifier

    def test_a_branch_set_by_hand_is_not_set_up_again(self):
        """A branch outside `plan/` is the operator's, and so is its setup.

        `plans/by-hand` is here because the prefix is `plan/` and not `plan`: a
        check that drops the slash takes every branch whose name starts with
        those four letters. Each row is queued in turn, so the one before it
        holds its own row and the next night reaches the next.
        """
        self.palette()
        for branch in ("topic/by-hand", "plans/by-hand"):
            with self.subTest(branch=branch):
                identifier = self.by_hand(f"a shape on {branch}", branch)
                before = self.record(identifier)
                self.move_main()
                done = self.nightly(conf=self.conf(str(self.repo.resolve())))
                self.assertEqual(len(self.queued(identifier)), 1, done.stdout + done.stderr)
                after = self.record(identifier)
                self.assertEqual((after["branch"], after["fields"], after["source_commit"]),
                                 (before["branch"], before["fields"], before["source_commit"]))

    def test_a_plan_branch_set_by_hand_keeps_its_name_and_gets_a_current_base(self):
        identifier = self.by_hand("a shape on a plan branch", "plan/by-hand")
        stale = self.seed(identifier)["source_commit"]
        moved = self.move_main()
        self.palette()
        self.next_night_runs(identifier, stale, moved)
        self.assertEqual(self.record(identifier)["branch"], "plan/by-hand")

    @contextlib.contextmanager
    def watching_prepare(self):
        """The item ids the night asks `runner_exec.prepare` about, in order.

        The count is the whole of sd:826: the standing answer was asked once
        and every remaining row of the repository was prepared on it anyway.
        """
        asked = []
        original = runner_exec.prepare

        def counted(connection, item, *args, **options):
            asked.append(item)
            return original(connection, item, *args, **options)

        with mock.patch.object(sd_plan.runner_exec, "prepare", side_effect=counted):
            yield asked

    def test_a_standing_refusal_stops_the_repository_before_any_row_is_prepared(self):
        """sd:826. The answer stands for every row, so no row is tried on it.

        Three rows and one pending restore. Before this, the night asked once
        and then called `prepare` for all three -- three refused transactions,
        and three stderr lines naming rows that were not at fault. Now the
        repository is named once and no row is prepared.
        """
        rows = [self.task(title=title) for title
                in ("a first shape", "a second shape", "a third shape")]
        self.palette()
        self.restore_pending()
        self.move_main()
        before = self.store()
        with self.watching_prepare() as asked:
            code, out, err = self.night_in_process(str(self.repo.resolve()))
        self.assertEqual(code, 1, out + err)
        self.assertEqual(asked, [], "no row may be prepared once the standing answer refuses")
        self.assertIn(f"sd-plan: {self.repo.resolve()} is not planned tonight", err)
        self.assertIn("restore recovery must finish", err)
        self.assertEqual(err.count("is not planned tonight"), 1, err)
        for identifier in rows:
            self.assertEqual(self.queued(identifier), [])
        self.assertEqual(self.store(), before)
        # sd:842. This line used to be here: a skipped night said the one
        # sentence a night with an empty backlog says, and exited as it does.
        self.assertNotIn("nothing to plan tonight", out)

    def test_a_palette_unreadable_only_at_the_standing_check_queues_no_row(self):
        """sd:806 note 2003, closed. Refused when asked, readable again at `prepare`.

        A palette rewritten in place is unreadable for an instant. When that
        instant covered the standing check alone, the night skipped the setup
        on the refusal and then queued the row anyway, because `prepare` read
        the same bytes back and agreed. The row went to the runner on the
        base its last setup saved; the default branch had moved, so the
        branch step refused it as "base changed after preparation", the run
        ended blocked, and `HOLDING` passed the row over for good (sd:778).
        The row waits one night instead, and the next night queues it on a
        current base.
        """
        identifier, stale = self.left_by_a_cancelled_run("a shape a flickering palette must not queue")
        moved = self.move_main()
        self.assertNotEqual(stale, moved)
        before = (self.store(), self.decisions(identifier))
        catalog = self.home / ".local/share/sd/commands.yaml"
        original = runner_exec.standing_refusal

        def unreadable(*args, **options):
            catalog.chmod(0o000)
            try:
                return original(*args, **options)
            finally:
                catalog.chmod(0o644)

        with mock.patch.object(sd_plan.runner_exec, "standing_refusal", side_effect=unreadable), \
                self.watching_prepare() as asked:
            code, out, err = self.night_in_process(str(self.repo.resolve()))
        self.assertEqual(code, 1, out + err)
        self.assertEqual(asked, [], "a row must not be prepared on an answer that refused")
        self.assertEqual(self.queued(identifier), [], out + err)
        self.assertIn("command catalog could not be read safely", err)
        self.assertEqual((self.store(), self.decisions(identifier)), before)
        self.assertEqual(self.seed(identifier)["source_commit"], stale)
        self.next_night_runs(identifier, stale, moved)

    def test_a_refusal_in_one_repository_leaves_the_next_repository_planned(self):
        """sd:826. The skip is one repository's, and the next is asked again.

        Every standing refusal is machine state, and a restore ends by
        itself: here it ends between the two questions. A skip that stopped
        the night rather than the repository would strand every repository
        sorted after the one that refused, night after night.
        """
        first = str(self.repo.resolve())
        held = self.by_hand("a shape the restore stops", "plan/stopped-by-a-restore")
        self.palette()
        second = self.second_repository()
        elsewhere = capture_task(self.connection, title="a shape in the next repository",
                                 repo=str(second), who="operator")["item"]["id"]
        self.move_main()
        self.restore_pending()
        original = runner_exec.standing_refusal

        def clearing(*args, **options):
            answer = original(*args, **options)
            self.restore_finished()
            return answer

        with mock.patch.object(sd_plan.runner_exec, "standing_refusal", side_effect=clearing), \
                self.watching_prepare() as asked:
            code, out, err = self.night_in_process(first, str(second))
        # One repository's skip, and the night still exits non-zero for it
        # (sd:842) -- the point of the skip being one repository's is that the
        # rest of the fleet is still planned, not that nobody is told.
        self.assertEqual(code, 1, out + err)
        self.assertEqual(asked, [elsewhere],
                         "the skip is one repository's; the next repository is asked again")
        self.assertEqual(self.queued(held), [], out + err)
        self.assertEqual(len(self.queued(elsewhere)), 1, out + err)
        self.assertIn(f"sd-plan: {first} is not planned tonight", err)
        self.assertNotIn(f"sd-plan: {second} is not planned tonight", err)



class WhatASkippedNightTells(NightlyCase):
    """sd:842. A repository skipped for the night has to reach a person.

    Before this, the only trace of a skip was the stderr line naming the
    repository, which under launchd lands in
    `local-cron-jobs/logs/sd-plan-nightly.log` and is read by nobody. The
    night still exited 0 and still printed "nothing to plan tonight" -- the
    two observables of a night whose backlog is genuinely empty. A nightly
    that had stopped planning a repository was indistinguishable from one
    that ran and found nothing, which is the shape sd:766 already filed once
    for the nightly never enqueuing at all.

    The signal is the night's exit status, because two readers already watch
    that one field and neither needs anything new built: `cron-jobs.sh`
    pages on a non-zero exit (`notify_failure` -- failures.log, a macOS
    notification and an ntfy push), and `sd_db.operations` parses the same
    exit out of launchd into the dashboard's `/operations` Jobs area, where
    a job with a non-zero `last exit code` reads `failed` and offers Retry
    (`local-sd-db/sd_db/operations.py`, `LaunchdBackend._parse`).

    Only a standing refusal does this. Every one of them is machine state to
    fix -- a restore still to finish, a palette that cannot be read or that
    changed, `plan-item` registered on another screen or rejected, an entry
    that would not be queued -- and none of them is "nothing to plan". A row
    the queue refuses on its own account is the ordinary case and still
    leaves the night at 0; `WhatItQueues` pins that half.
    """

    def test_a_repository_skipped_for_the_night_fails_the_night(self):
        """The whole of sd:842: a stopped nightly must not exit like a quiet one."""
        identifier = self.task(title="a shape the restore stops")
        self.palette()
        self.restore_pending()
        code, out, err = self.night_in_process(str(self.repo.resolve()))
        self.assertIn(f"sd-plan: {self.repo.resolve()} is not planned tonight", err)
        self.assertEqual(self.queued(identifier), [], out + err)
        self.assertEqual(code, 1, "a skipped repository must not exit like a quiet night")
        self.assertNotIn("nothing to plan tonight", out,
                         "a night that skipped a repository did not find nothing")

    def test_the_night_names_how_many_repositories_it_skipped_and_which(self):
        """The summary is what a reader of the log has, once the exit sent them there."""
        self.task(title="a shape the restore stops")
        self.palette()
        self.restore_pending()
        code, out, err = self.night_in_process(str(self.repo.resolve()))
        self.assertEqual(code, 1, out + err)
        self.assertIn(f"sd-plan: 1 repository(ies) not planned tonight: {self.repo.resolve()}", err)

    def test_a_skip_beside_a_planned_repository_still_fails_the_night(self):
        """One repository of five silently stopping is the failure, not all five.

        A night that only failed when it queued nothing would stay green
        forever while one repository was never planned again.
        """
        first = str(self.repo.resolve())
        held = self.task(title="a shape the restore stops")
        self.palette()
        second = self.second_repository()
        elsewhere = capture_task(self.connection, title="a shape in the next repository",
                                 repo=str(second), who="operator")["item"]["id"]
        self.restore_pending()
        original = runner_exec.standing_refusal

        def clearing(*args, **options):
            answer = original(*args, **options)
            self.restore_finished()
            return answer

        with mock.patch.object(sd_plan.runner_exec, "standing_refusal", side_effect=clearing):
            code, out, err = self.night_in_process(first, str(second))
        self.assertEqual(self.queued(held), [], out + err)
        self.assertEqual(len(self.queued(elsewhere)), 1, out + err)
        self.assertEqual(code, 1, "a queued row elsewhere does not excuse a skipped repository")
        self.assertIn(f"sd-plan: 1 repository(ies) not planned tonight: {first}", err)

    def test_a_night_that_really_found_nothing_still_exits_zero(self):
        """The other half: the signal must not fire on the state it has to be told from."""
        self.palette()
        code, out, err = self.night_in_process(str(self.repo.resolve()))
        self.assertEqual(code, 0, out + err)
        self.assertIn("nothing to plan tonight", out)
        self.assertNotIn("not planned tonight", err)


class WhatANightWithNoCandidateDoesNotAsk(NightlyCase):
    """sd:877. A repository with no candidate is not asked, and that is right.

    sd:842 made a skipped repository fail the night. The standing question it
    reads is put on the first row the night tries, so a repository whose
    every row is passed over puts no question at all: the night prints
    "nothing to plan tonight" and exits 0 while a refusal stands. sd:877
    filed that as the remainder of sd:842's hole, because from outside it is
    the same two observables.

    It is not the same failure, and these cases are why. A refusal stops the
    repository before the first `enqueue`, so it writes nothing and cannot
    pass a row over: every row skipped on such a night was skipped on its own
    account, and no row is waiting on the refusal. The first night a row is a
    candidate the question is put and the night is red -- the first night the
    refusal costs anything. So the silence is bounded by the transition from
    an empty backlog to a full one, and it costs one night, once.

    Asking ahead of the loop would cost more than that, every night, forever,
    and the last case here is the guard on it: the question reads the palette
    before all but one of its refusals, so a night that asks it before it has
    anything to queue fails on every machine with no palette configured --
    which is a machine doing exactly what it should.
    """

    def already_planned(self, title):
        """A row `candidates` passes over: its folder is already in this checkout."""
        identifier = self.task(title=title)
        chosen = sd_plan.selectable(self.connection, str(self.repo.resolve()), 5)
        slug = next(row["slug"] for row in chosen if row["id"] == identifier)
        (self.repo / "docs" / "work" / slug).mkdir(parents=True)
        self.assertEqual(sd_plan.selectable(self.connection, str(self.repo.resolve()), 5), [],
                         "premise: the repository offers no candidate")
        return identifier

    @contextlib.contextmanager
    def watching_the_question(self):
        """The arguments the night puts the standing question with, in order."""
        put = []
        original = runner_exec.standing_refusal

        def counted(connection, command, values, **options):
            put.append(values)
            return original(connection, command, values, **options)

        with mock.patch.object(sd_plan.runner_exec, "standing_refusal", side_effect=counted):
            yield put

    def test_a_repository_with_no_candidate_is_never_asked_the_standing_question(self):
        """The measured shape of sd:877, pinned as the behaviour it is."""
        self.already_planned("a shape already planned")
        self.palette()
        self.restore_pending()
        with self.watching_the_question() as put:
            code, out, err = self.night_in_process(str(self.repo.resolve()))
        self.assertEqual(put, [], "no row was tried, so no question may be put")
        self.assertEqual(code, 0, out + err)
        self.assertIn("nothing to plan tonight", out)
        self.assertNotIn("not planned tonight", err)

    def test_the_same_refusal_fails_the_night_once_a_row_is_a_candidate(self):
        """Why the silence above is safe: it ends on the first plannable row.

        The same pending restore, the same machine. One row `candidates`
        yields, and sd:842's signal fires.
        """
        self.task(title="a shape the restore stops")
        self.palette()
        self.restore_pending()
        with self.watching_the_question() as put:
            code, out, err = self.night_in_process(str(self.repo.resolve()))
        self.assertEqual(len(put), 1, "the question is put on the first row tried")
        self.assertEqual(code, 1, out + err)
        self.assertIn(f"sd-plan: 1 repository(ies) not planned tonight: {self.repo.resolve()}", err)

    def test_a_refused_night_leaves_the_row_a_candidate_for_the_next_night(self):
        """The load-bearing premise: a refusal cannot shrink the candidate set.

        If it could, a repository could be talked out of its own candidates
        and then go silent, and the case above would be a real hole rather
        than an empty backlog.
        """
        identifier = self.task(title="a shape the restore stops")
        self.palette()
        self.restore_pending()
        code, out, err = self.night_in_process(str(self.repo.resolve()))
        self.assertEqual(code, 1, out + err)
        self.assertEqual([row["id"] for row
                          in sd_plan.selectable(self.connection, str(self.repo.resolve()), 5)],
                         [identifier],
                         "a refused row must still be a candidate tomorrow")

    def test_a_machine_with_no_palette_and_no_candidate_still_exits_zero(self):
        """The guard on the rejected fix, and the reason it was rejected.

        Nothing is configured here at all. Asking the standing question
        before the loop reads the palette, and reading the palette here is
        the failure `nightly` already refuses to make on a quiet night --
        `WhatItRefuses` pins the other half, that the same missing palette
        fails a night which does have a row to queue.
        """
        self.already_planned("a shape already planned")
        code, out, err = self.night_in_process(str(self.repo.resolve()))
        self.assertEqual(code, 0, out + err)
        self.assertIn("nothing to plan tonight", out)
        self.assertNotIn("No command palette is configured", err)

    def test_a_dry_run_asks_nothing_even_with_a_candidate_and_a_refusal(self):
        """Why every sentence above says "a real night".

        `--dry-run` reports the candidate and moves on before the palette is
        read, so it never reaches the question -- for any repository, and
        whatever it found. Nothing is being guarded: a dry run enqueues
        nothing, so there is no setup for the standing answer to precede.
        Pinned because the prose would otherwise be false about the one
        invocation that has a candidate and still exits 0.
        """
        self.task(title="a shape the restore stops")
        self.palette()
        self.restore_pending()
        with self.watching_the_question() as put:
            code, out, err = self.night_in_process(str(self.repo.resolve()), dry_run=True)
        self.assertEqual(put, [], "a dry run enqueues nothing, so it guards nothing")
        self.assertEqual(code, 0, out + err)
        self.assertIn("would plan item", out)
        self.assertNotIn("not planned tonight", err)
        self.assertEqual(self.assignments(), [], out + err)

    def test_why_the_question_is_not_asked_ahead_of_the_loop_is_written_down(self):
        """A decision nobody can read is one the next reader takes again."""
        text = " ".join((sd_plan.nightly.__doc__ or "").split())
        self.assertIn("A repository that offers no candidate is never asked, "
                      "and the night is green (sd:877).", text)
        self.assertIn("Asking ahead of the loop instead was rejected twice over.", text)
        self.assertIn("the first night the refusal costs anything", text)
        readme = " ".join((FOLDER / "README.md").read_text(encoding="utf-8").split())
        self.assertIn("A repository with no candidate is never asked, "
                      "and the night is green", readme)
        self.assertIn("Asking ahead of the loop was weighed and rejected.", readme)
        self.assertIn("the first night the refusal costs anything", readme)

    def test_the_dry_run_qualification_is_written_down_too(self):
        """The finding this answers was that the rule read as unconditional."""
        text = " ".join((sd_plan.nightly.__doc__ or "").split())
        self.assertIn("The question is put on the first row a real night tries", text)
        self.assertIn("A real night, because `--dry-run` asks nothing at all, "
                      "for any repository", text)
        readme = " ".join((FOLDER / "README.md").read_text(encoding="utf-8").split())
        self.assertIn("The standing question is put on the first row a real night tries.", readme)
        self.assertIn("because `--dry-run` asks nothing at all, for any repository", readme)


class WhatDryRunDoes(NightlyCase):
    def test_it_reports_a_selection_and_queues_nothing(self):
        self.task(title="a shape worth planning")
        done = self.nightly("--dry-run", conf=self.conf(str(self.repo.resolve())))
        self.assertIn("would plan item", done.stdout)
        self.assertEqual(self.assignments(), [],
                         "--dry-run must not enqueue; on a live machine that starts an agent")

    def test_it_says_so_when_there_is_nothing_to_plan(self):
        done = self.nightly("--dry-run", conf=self.conf(str(self.repo.resolve())))
        self.assertIn("nothing to plan", done.stdout)


if __name__ == "__main__":
    unittest.main()
