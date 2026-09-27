"""`item <id>`: the executor, and the four ways it must refuse to run.

The agent is doubled. `SD_PLAN_AGENT` replaces its whole argv, so these cases
exercise everything around the planning run -- the refusals, the branch, the
commit, the registration, the push -- without a model, a network or a token.
What they cannot test is whether the documents are any good, which is why
`prd.md`'s last acceptance criterion is a person reading one.

The fixture is a real checkout with a real bare origin, because the two things
most likely to break are the two that only exist against real git: the push,
and the refusal to run against a dirty tree.
"""

import json
import re
import os
import pwd
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from sd_db import connect, paths, upsert_repo
from sd_db.migrate import initialise
from sd_db.workflow import capture_task

import sd_plan

FOLDER = Path(__file__).resolve().parent.parent
ENTRYPOINT = FOLDER / "sd-plan.sh"

#: The pack whose `sd work register` makes the row. CI checks it out at its pin
#: beside this repository and names it; `sd-plan.sh test` finds the usual
#: checkout when nothing names one.
PACK = Path(os.environ.get("SD_ACCEPTANCE_PACK", FOLDER.parents[1] / "pack"))

#: Writes what `/sd-plan` writes and nothing else. Argument one is the slug,
#: argument two the item id, as `sd_plan.agent` appends them.
AGENT = """\
#!/bin/sh
set -e
slug="$1"
mkdir -p "docs/work/$slug"
for name in %s; do
  printf '%%s\\n' "---" "title: $slug" "created: 2026-09-11" "---" "" "# $name" > "docs/work/$slug/$name"
done
"""


class ItemCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / ".local/share/sd").mkdir(parents=True)
        self.database = self.home / ".local/share/sd/sd.db"
        initialise(self.database)

        self.origin = self.home / "origin.git"
        # `-b main` on the *bare* repository too, not only on the checkout:
        # without it the origin's HEAD follows the machine's
        # `init.defaultBranch`, and a clone of it comes up with an unborn HEAD
        # wherever that is not `main`. Local runs passed and CI did not.
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)],
                       check=True, capture_output=True)
        self.repo = self.home / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        self.git("remote", "add", "origin", str(self.origin))
        (self.repo / "README.md").write_text("fixture\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-qm", "first")
        self.git("push", "-q", "-u", "origin", "main")

        # The library keys a repository under `$HOME` as `~/...` (sd:1439), and
        # the entrypoint runs under this home, so the rows this process writes
        # and the ones the entrypoint writes agree only when both read it. Set
        # before the row is written, because the row's key is taken from it.
        home = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        home.start()
        self.addCleanup(home.stop)

        connection = connect(self.database)
        self.addCleanup(connection.close)
        # The remote, as production records it: the runner clones from it and
        # `item` uses it to recognise that a clone is still this repository.
        upsert_repo(connection, paths.key(str(self.repo.resolve())), remote=str(self.origin),
                    status_source="row")
        self.connection = connection

    def git(self, *args, cwd=None):
        done = subprocess.run(["git", "-C", str(cwd or self.repo), *args],
                              capture_output=True, text=True, check=True)
        return done.stdout.strip()

    def task(self, *, title="a shape worth agreeing on", repo=True, **fields):
        state = capture_task(
            self.connection, title=title,
            repo=str(self.repo.resolve()) if repo else None, who="operator", **fields,
        )
        return state["item"]["id"]

    def agent(self, *documents):
        path = self.home / "agent.sh"
        path.write_text(AGENT % " ".join(documents or sd_plan.DOCUMENTS), encoding="utf-8")
        path.chmod(0o755)
        return path

    def plan(self, *args, expect=0, agent=None, cwd=None, env=None):
        environment = dict(os.environ)
        environment["HOME"] = str(self.home)
        environment.pop("PYTHONPATH", None)
        environment["SD_PLAN_AGENT"] = str(agent if agent is not None else self.agent())
        # The interpreter running this suite, so `sd_db` is the one it has
        # installed and not whatever a temporary HOME leaves the entrypoint to
        # find; and the pack the suite was given -- CI's checkout at its pin,
        # or the checkout `sd-plan.sh test` found when nothing named one.
        environment["SD_PLAN_PYTHON"] = sys.executable
        environment["SD_PACK_ROOT"] = str(PACK)
        for name, value in (env or {}).items():
            if value is None:
                environment.pop(name, None)
            else:
                environment[name] = value
        done = subprocess.run(
            ["/bin/sh", str(ENTRYPOINT), "item", *args],
            capture_output=True, text=True, input="",
            cwd=str(cwd or self.repo), env=environment,
        )
        self.assertEqual(done.returncode, expect, done.stdout + done.stderr)
        return done

    def rows(self):
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM item WHERE kind = 'work' ORDER BY id")]

    def pushed(self):
        out = subprocess.run(["git", "ls-remote", "--heads", str(self.origin)],
                             capture_output=True, text=True, check=True).stdout
        return sorted(line.split("refs/heads/")[1] for line in out.splitlines() if line)


class TheSlug(unittest.TestCase):
    def test_it_yields_a_folder_name_the_lint_can_parse(self):
        self.assertEqual(sd_plan.slugify("held bumps watch their blockers"),
                         "held-bumps-watch-their-blockers")
        # Punctuation a real row carries. `CLAUDE.md` is the one that would
        # otherwise leave a dot in a folder name the lint's ITEM_DIR_RE
        # rejects -- and an unparsed folder is a folder nothing checks.
        self.assertEqual(sd_plan.slugify("system: CLAUDE.md overstates rule 6"),
                         "system-claude-md-overstates-rule-6")

    def test_a_long_title_is_cut_on_a_hyphen_and_never_mid_word(self):
        title = "nothing notices a test suite that never gets wired into system-native"
        slug = sd_plan.slugify(title)
        self.assertLessEqual(len(slug), sd_plan.SLUG_LIMIT)
        self.assertTrue(sd_plan.SLUG_RE.match(slug), slug)
        self.assertFalse(slug.endswith("-"))
        # The cut falls between words: every piece of the slug is a whole word
        # of the title, never a prefix of one. `...gets-wir` reads as a typo.
        title_words = set(re.findall(r"[a-z0-9]+", title.lower()))
        self.assertTrue(
            set(slug.split("-")) <= title_words,
            f"{slug} contains a fragment that is not a word of the title",
        )
        # And it really did have to cut: a case that fits teaches nothing.
        self.assertGreater(len(re.sub(r"[^a-z0-9]+", "-", title.lower())),
                           sd_plan.SLUG_LIMIT)

    def test_a_title_with_nothing_to_slugify_is_refused(self):
        with self.assertRaises(sd_plan.Refused):
            sd_plan.slugify("!!! ???")


class WhatItRefuses(ItemCase):
    def test_a_row_belonging_to_another_checkout(self):
        other = self.home / "elsewhere"
        other.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=other, check=True,
                       capture_output=True)
        upsert_repo(self.connection, paths.key(str(other.resolve())), status_source="row")
        identifier = self.task()
        # R10-D6: standing in `elsewhere`, planning a row that belongs to
        # `repo` would write the folder into the wrong repository and record
        # a path nobody standing there can read.
        done = self.plan(str(identifier), expect=1, cwd=other)
        self.assertIn("plan it from its own checkout", done.stderr)
        self.assertEqual(self.rows(), [])

    def test_a_row_with_no_repository_at_all(self):
        identifier = self.task(repo=False)
        done = self.plan(str(identifier), expect=1)
        self.assertIn("has no repository", done.stderr)

    def test_a_row_that_is_already_finished(self):
        identifier = self.task()
        self.connection.execute("UPDATE item SET status = 'done' WHERE id = ?", (identifier,))
        self.connection.commit()
        done = self.plan(str(identifier), expect=1)
        self.assertIn("nothing left to plan", done.stderr)

    def test_a_row_that_does_not_exist(self):
        done = self.plan("9999", expect=1)
        self.assertIn("no item 9999", done.stderr)

    def test_a_checkout_with_uncommitted_work(self):
        # The run commits whatever it finds under docs/work. A dirty tree
        # means it could commit somebody else's edits, and in a shared
        # checkout that is exactly the accident CLAUDE.md's "Three sessions"
        # section exists to prevent.
        identifier = self.task()
        (self.repo / "README.md").write_text("edited\n", encoding="utf-8")
        done = self.plan(str(identifier), expect=1)
        self.assertIn("uncommitted changes", done.stderr)
        self.assertEqual(self.rows(), [])

    def test_a_run_that_left_a_document_unwritten(self):
        # Half a plan is not a plan: nothing is registered and nothing is
        # pushed, so the failure is visible rather than a folder that looks
        # planned.
        identifier = self.task()
        done = self.plan(str(identifier), expect=1, agent=self.agent("prd.md"))
        self.assertIn("design.md, implement.md", done.stderr)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.pushed(), ["main"])

    def test_the_prompt_asks_for_every_document_the_refusal_above_demands(self):
        """The refusal above and the prompt must agree on what a run is.

        `/sd-plan` writes `design.md` and `implement.md` only when asked, and
        the prompt as shipped asked for neither -- so the first unattended
        run passed only because the agent read this dispatcher's source and
        inferred the refusal (sd:438, exec note 709 on sd:442). The prompt
        names all three by file name, and says the run is unattended, so
        the skill's own "record routine choices and continue" clause applies
        without the agent having to work out that nobody will answer.
        """
        seen = []
        with mock.patch.dict(os.environ, {"SD_PLAN_CLAUDE": "/opt/agent"}, clear=False):
            os.environ.pop("SD_PLAN_AGENT", None)
            with mock.patch.object(sd_plan.subprocess, "run",
                                   lambda *a, **k: seen.append(a[0]) or
                                   subprocess.CompletedProcess(a[0], 0)):
                sd_plan.agent(self.repo, "a-slug", {"id": 7})
        prompt = seen[0][2]
        self.assertTrue(prompt.startswith("/sd-plan a-slug --from sd:7"), prompt)
        for name in sd_plan.DOCUMENTS:
            self.assertIn(name, prompt, f"the prompt does not ask for {name}")
        self.assertIn("unattended", prompt)

    def test_a_run_whose_agent_fails(self):
        failing = self.home / "failing.sh"
        failing.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
        failing.chmod(0o755)
        identifier = self.task()
        done = self.plan(str(identifier), expect=1, agent=failing)
        self.assertIn("exited 7", done.stderr)
        self.assertEqual(self.rows(), [])


class WhatItWrites(ItemCase):
    def test_a_row_becomes_a_folder_a_branch_a_commit_a_push_and_a_row(self):
        identifier = self.task(title="held bumps watch their blockers")
        done = self.plan(str(identifier))
        slug = None
        for candidate in (self.repo / "docs" / "work").iterdir():
            slug = candidate.name
        self.assertTrue(slug.endswith("-held-bumps-watch-their-blockers"), slug)

        for name in sd_plan.DOCUMENTS:
            self.assertTrue((self.repo / "docs/work" / slug / name).is_file(), name)

        # On a branch of its own, not on main.
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD"), f"plan/{slug}")
        self.assertIn(f"plan/{slug}", self.pushed())

        # Committed, and the commit says which row it advances.
        self.assertIn(f"Work: sd:{identifier}", self.git("log", "-1", "--format=%B"))
        self.assertEqual(self.git("status", "--porcelain=v1"), "")

        # And registered, which is the half that has no second chance: a
        # folder without a row has no readable status at all.
        row, = self.rows()
        self.assertEqual(row["status"], "planning")
        self.assertEqual(row["path"], f"docs/work/{slug}/prd.md")
        # Keyed by the repository's `~/...` key, as the pack stores it (sd:1439).
        self.assertEqual(row["external_id"],
                         f"{paths.key(str(self.repo.resolve()))}::docs/work/{slug}/prd.md")
        # The branch the documents were written on, not the remote default
        # the pack's verb wrote until sd:621.
        self.assertEqual(row["branch"], f"plan/{slug}")
        # What the pack's `sd work register` prints, passed through.
        self.assertIn(f"#{row['id']}  planning", done.stdout)

    def test_what_the_agent_staged_does_not_ride_the_documents_commit(self):
        """The agent runs after the dirty-tree check, so its index is its own.

        A commit of the whole index would carry a file the agent staged and
        push it with the documents. The commit takes the item's folder and
        nothing else, and the stray stays staged, so the clone is kept dirty.
        """
        stager = self.home / "stager.sh"
        stager.write_text(AGENT % " ".join(sd_plan.DOCUMENTS)
                          + "printf 'stray\\n' > stray-staged.txt\ngit add stray-staged.txt\n",
                          encoding="utf-8")
        stager.chmod(0o755)
        identifier = self.task(title="an agent that stages too much")
        self.plan(str(identifier), agent=stager)
        slug, = [path.name for path in (self.repo / "docs" / "work").iterdir()]
        committed = self.git("diff-tree", "--no-commit-id", "--name-only", "-r",
                             f"plan/{slug}", cwd=self.origin)
        self.assertEqual(sorted(committed.splitlines()),
                         sorted(f"docs/work/{slug}/{name}" for name in sd_plan.DOCUMENTS))
        self.assertNotIn("stray-staged.txt",
                         self.git("ls-tree", "-r", "--name-only", f"plan/{slug}", cwd=self.origin))
        self.assertIn("A  stray-staged.txt", self.git("status", "--porcelain=v1").splitlines())

    def test_it_uses_the_branch_the_row_already_names(self):
        """In a runner clone the row always has one, and it is the only one
        the clone's pre-push hook will accept."""
        identifier = self.task(title="a leased shape")
        self.connection.execute("UPDATE item SET branch = 'exec/leased' WHERE id = ?",
                                (identifier,))
        self.connection.commit()
        self.plan(str(identifier))
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD"), "exec/leased")
        self.assertIn("exec/leased", self.pushed())

    def test_planning_the_same_row_twice_changes_nothing(self):
        identifier = self.task()
        self.plan(str(identifier))
        before = self.git("rev-parse", "HEAD")
        done = self.plan(str(identifier))
        self.assertIn("already planned", done.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(len(self.rows()), 1)

    def test_dry_run_says_what_it_would_do_and_writes_nothing(self):
        identifier = self.task()
        done = self.plan(str(identifier), "--dry-run")
        self.assertIn("would plan item", done.stdout)
        self.assertFalse((self.repo / "docs").exists())
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.pushed(), ["main"])
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD"), "main")


class WhereItRegisters(ItemCase):
    """The row comes from the pack's `sd work register`, and nowhere else.

    These cases stand a double in for the pack, because what they pin is the
    call -- the argv, the interpreter, the environment, the exit -- and not
    what the verb writes. `WhatItWrites` runs the real verb at CI's pin.
    """

    def fake_pack(self, body):
        pack = self.home / "fake-pack"
        (pack / "bin").mkdir(parents=True)
        (pack / "bin" / "sd").write_text(body, encoding="utf-8")
        return pack

    def test_it_calls_the_pack_verb_under_the_pinned_interpreter(self):
        record = self.home / "sd-call.json"
        pack = self.fake_pack(
            "import json, os, sys\n"
            f"with open({str(record)!r}, 'w', encoding='utf-8') as handle:\n"
            "    json.dump({'argv': sys.argv[1:], 'cwd': os.getcwd(),\n"
            "               'executable': sys.executable, 'home': os.environ.get('HOME'),\n"
            "               'pythonpath': os.environ.get('PYTHONPATH', '')}, handle)\n"
        )
        identifier = self.task(title="held bumps watch their blockers")
        done = self.plan(str(identifier), env={"SD_PACK_ROOT": str(pack),
                                               "PYTHON": sys.executable})
        slug, = [path.name for path in (self.repo / "docs" / "work").iterdir()]
        self.assertTrue(record.is_file(), "the pack's sd was never run: " + done.stdout + done.stderr)
        seen = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(seen["argv"], ["work", "register", f"docs/work/{slug}/prd.md"])
        self.assertEqual(Path(seen["cwd"]).resolve(), self.repo.resolve())
        # Run by the interpreter `sd-plan.sh` pinned, not by `bin/sd`'s
        # shebang, which on the runner's PATH is Xcode's 3.9.
        self.assertEqual(os.path.realpath(seen["executable"]), os.path.realpath(sys.executable))
        # The store is the caller's: HOME reaches the verb unchanged.
        self.assertEqual(seen["home"], str(self.home))
        # And no source tree stands in front of the installed library.
        self.assertNotIn("local-sd-db", seen["pythonpath"])
        self.assertIn(f"plan/{slug}", self.pushed())

    def test_a_missing_pack_refuses_before_the_planning_run(self):
        absent = self.home / "no-pack-here"
        identifier = self.task()
        done = self.plan(str(identifier), expect=1, env={"SD_PACK_ROOT": str(absent)})
        self.assertIn(f"no pack at {absent}", done.stderr)
        self.assertIn("SD_PACK_ROOT", done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        # Asked first, so no agent ran, nothing was committed and nothing pushed.
        self.assertFalse((self.repo / "docs").exists())
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.pushed(), ["main"])

    def test_without_sd_pack_root_it_looks_where_the_fleet_keeps_the_pack(self):
        """Unset is the runner's case: `process_plan` passes no SD_PACK_ROOT."""
        identifier = self.task()
        for value in (None, ""):
            with self.subTest(SD_PACK_ROOT=value):
                done = self.plan(str(identifier), expect=1, env={"SD_PACK_ROOT": value})
                self.assertIn(f"no pack at {self.home}/repos/platypeeps/sd-ai-command-pack",
                              done.stderr)

    def test_a_refusal_from_the_pack_fails_the_run_and_pushes_nothing(self):
        pack = self.fake_pack(
            "import sys\n"
            "print('sd: the store would not take this row', file=sys.stderr)\n"
            "sys.exit(3)\n"
        )
        identifier = self.task()
        done = self.plan(str(identifier), expect=1, env={"SD_PACK_ROOT": str(pack)})
        self.assertIn("sd exited 3", done.stderr)
        self.assertIn("the store would not take this row", done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        self.assertEqual(self.pushed(), ["main"])

    def refusing_pack(self):
        return self.fake_pack("import sys\nprint('sd: store busy', file=sys.stderr)\nsys.exit(1)\n")

    def test_a_refused_registration_leaves_no_commit_to_publish(self):
        """The runner pushes a clean clone's branch even when the run failed.

        `runtime.py:_finish` calls `gitops.durable` on any clean clone, and
        that pushes. So a documents commit made before a refused registration
        was published anyway, with no row. Uncommitted, the clone is dirty,
        and the runner keeps it instead.
        """
        identifier = self.task(title="transient register failure")
        base = self.git("rev-parse", "HEAD")
        self.plan(str(identifier), expect=1, env={"SD_PACK_ROOT": str(self.refusing_pack())})
        self.assertEqual(self.git("rev-parse", "HEAD"), base, "a commit was made for a folder without a row")
        self.assertNotIn("Work: sd:", self.git("log", "--all", "--format=%B"))
        self.assertTrue(self.git("status", "--porcelain=v1", "--untracked-files=all"),
                        "the documents should still be on disk, uncommitted")
        self.assertEqual(self.pushed(), ["main"])

    def test_a_retry_after_a_refused_registration_registers_the_row(self):
        identifier = self.task(title="transient register failure")
        self.plan(str(identifier), expect=1, env={"SD_PACK_ROOT": str(self.refusing_pack())})
        # The retry must not plan again: an agent that fails proves it is not run.
        failing = self.home / "failing.sh"
        failing.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
        failing.chmod(0o755)
        done = self.plan(str(identifier), agent=failing)
        rows = self.rows()
        self.assertEqual(len(rows), 1, "the retry made no row: " + done.stdout + done.stderr)
        row, = rows
        slug = Path(row["path"]).parent.name
        self.assertTrue(slug.endswith("-transient-register-failure"), slug)
        self.assertEqual(row["branch"], f"plan/{slug}")
        self.assertIn(f"Work: sd:{identifier}", self.git("log", "-1", "--format=%B"))
        self.assertEqual(self.git("status", "--porcelain=v1", "--untracked-files=all"), "")
        self.assertIn(f"plan/{slug}", self.pushed())
        self.assertIn("planned item", done.stdout)

    def test_a_committed_folder_without_a_row_is_registered_and_not_skipped(self):
        """What the old order left behind, and what the runner then pushed."""
        identifier = self.task(title="committed but never registered")
        created = self.connection.execute(
            "SELECT created_at FROM item WHERE id = ?", (identifier,)).fetchone()[0][:10]
        slug = f"{created}-committed-but-never-registered"
        self.git("checkout", "-q", "-b", f"plan/{slug}")
        subprocess.run(["/bin/sh", str(self.agent()), slug, str(identifier)],
                       cwd=self.repo, check=True, capture_output=True)
        self.git("add", "docs")
        self.git("commit", "-qm", f"docs(work): {slug}\n\nWork: sd:{identifier}")
        failing = self.home / "failing.sh"
        failing.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
        failing.chmod(0o755)
        done = self.plan(str(identifier), agent=failing)
        self.assertNotIn("already planned", done.stdout)
        rows = self.rows()
        self.assertEqual(len(rows), 1, "no row was made: " + done.stdout + done.stderr)
        row, = rows
        self.assertEqual(row["path"], f"docs/work/{slug}/prd.md")
        self.assertIn(f"plan/{slug}", self.pushed())


class WhereItWillRun(ItemCase):
    """R10-D6 asks which repository, and a path answers a narrower question.

    The runner clones from the repo's remote into
    `/Volumes/sd-work/worktrees/<item>/<run>`, so a path comparison refuses
    the runner every time. The first real end-to-end run failed exactly
    there, after the command had been queued, dispatched and cloned.
    """

    def clone(self, name="clone"):
        """A second checkout of the same origin, the way the runner makes one."""
        target = self.home / name
        subprocess.run(["git", "clone", "-q", str(self.origin), str(target)],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email",
                        "fixture@example.invalid"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "Fixture"],
                       check=True, capture_output=True)
        return target

    def test_a_clone_of_the_rows_repository_is_the_rows_repository(self):
        identifier = self.task(title="planned from a clone")
        done = self.plan(str(identifier), cwd=self.clone())
        self.assertIn("planned item", done.stdout)
        self.assertEqual([row["repo"] for row in self.rows()],
                         [paths.key(str(self.repo.resolve()))],
                         "the row records the repository, not the clone it was written in")

    def test_a_checkout_with_a_different_origin_is_still_refused(self):
        """The narrowing must not become a hole: a different repository stays out."""
        elsewhere = self.home / "elsewhere.git"
        subprocess.run(["git", "init", "-q", "--bare", str(elsewhere)],
                       check=True, capture_output=True)
        other = self.home / "other"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(other)],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(other), "remote", "set-url", "origin",
                        str(elsewhere)], check=True, capture_output=True)
        identifier = self.task(title="not for that checkout")
        done = self.plan(str(identifier), expect=1, cwd=other)
        self.assertIn("not a clone of it", done.stderr)
        self.assertEqual(self.rows(), [])

    def test_the_git_suffix_alone_does_not_make_it_a_different_repository(self):
        self.assertTrue(sd_plan.same_remote(
            "git@github.com:platypeeps/system.git", "git@github.com:platypeeps/system"))
        self.assertFalse(sd_plan.same_remote(
            "git@github.com:platypeeps/system.git", "git@github.com:platypeeps/other"))

    def test_an_absent_remote_on_both_sides_is_not_a_match(self):
        """Two repositories with no remote are not thereby the same one."""
        self.assertFalse(sd_plan.same_remote(None, None))
        self.assertFalse(sd_plan.same_remote("", ""))


class WhichInterpreterItRuns(ItemCase):
    """A defect that shipped: the runner's PATH gives Python 3.9.

    The first real end-to-end run through the dashboard button died on
    `ImportError: cannot import name 'UTC' from 'datetime'` before reaching
    any of this script's own logic. Nothing in the suite caught it because the
    suite runs under whatever modern `python3` the developer has; the runner
    executes with a bare PATH, where `python3` is Xcode's 3.9.
    """

    def old_python(self):
        """A stand-in that fails the version probe, on any machine.

        Fabricated rather than reaching for `/usr/bin/python3`, so the case
        runs identically in CI and never turns into a skip -- the CI wrapper
        fails on skips.
        """
        path = self.home / "old-python"
        path.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def entrypoint(self, *args, env=None, expect=0, unset=()):
        environment = dict(os.environ)
        environment["HOME"] = str(self.home)
        environment.pop("PYTHONPATH", None)
        for name in unset:
            environment.pop(name, None)
        environment.update(env or {})
        done = subprocess.run(
            ["/bin/sh", str(ENTRYPOINT), *args],
            capture_output=True, text=True, input="",
            cwd=str(self.repo), env=environment,
        )
        self.assertEqual(done.returncode, expect, done.stdout + done.stderr)
        return done

    def test_an_interpreter_that_is_too_old_refuses_in_a_sentence(self):
        done = self.entrypoint("item", "1", "--dry-run", expect=1,
                               env={"PYTHON": str(self.old_python())})
        self.assertIn("too old", done.stderr)
        self.assertIn("3.11", done.stderr)
        self.assertNotIn("Traceback", done.stderr)

    def test_an_interpreter_without_sd_db_refuses_in_a_sentence(self):
        """No `PYTHONPATH` supplies the library now; PYTHON has it or says so.

        `-S` leaves the standard library and drops every site-packages, which
        is an interpreter new enough to pass the version probe and without
        `sd_db` -- on any machine, CI's venv included.
        """
        bare = self.home / "python-without-site"
        bare.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -S "$@"\n',
                        encoding="utf-8")
        bare.chmod(0o755)
        done = self.entrypoint("item", "1", "--dry-run", expect=1, env={"PYTHON": str(bare)})
        self.assertIn("cannot import sd_db", done.stderr)
        self.assertNotIn("Traceback", done.stderr)

    def test_the_pack_named_by_sd_pack_root_supplies_the_interpreter(self):
        """`sd_db` and `bin/sd` come from one pack, not from two checkouts."""
        pack = self.home / "elsewhere-pack"
        marker = self.home / "picked"
        python = pack / ".venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text(f'#!/bin/sh\ntouch {shlex.quote(str(marker))}\n'
                          f'exec {shlex.quote(sys.executable)} "$@"\n', encoding="utf-8")
        python.chmod(0o755)
        identifier = self.task(title="a shape worth agreeing on")
        environment = {"SD_PACK_ROOT": str(pack)}
        done = self.entrypoint("item", str(identifier), "--dry-run", env=environment,
                               unset=("PYTHON", "SD_PLAN_PYTHON"))
        self.assertIn("would plan item", done.stdout)
        self.assertTrue(marker.exists(), "the interpreter did not come from SD_PACK_ROOT")

    def test_a_bare_path_still_finds_a_usable_interpreter(self):
        """The runner's environment, reproduced: PATH without Homebrew.

        `SD_PLAN_PYTHON` names the interpreter running this suite, which is
        the mechanism the fix relies on; PATH is stripped to what the runner
        actually provides so a regression cannot hide behind the developer's
        own shell.
        """
        identifier = self.task(title="a shape worth agreeing on")
        marker = self.home / "fallback-reached"
        python = self.home / "sd-plan-python"
        python.write_text(f'#!/bin/sh\ntouch {shlex.quote(str(marker))}\n'
                          f'exec {shlex.quote(sys.executable)} "$@"\n', encoding="utf-8")
        python.chmod(0o755)
        # CI exports PYTHON in the shell that runs this suite; inherited, it
        # takes the early branch and the fallback is never reached (sd:1360).
        done = self.entrypoint(
            "item", str(identifier), "--dry-run",
            env={"PATH": "/usr/bin:/bin", "SD_PLAN_PYTHON": str(python)}, unset=("PYTHON",),
        )
        self.assertIn("would plan item", done.stdout)
        self.assertTrue(marker.exists(), "the interpreter did not come from the fallback loop")

    def test_the_minimum_version_is_stated_once_and_is_three_eleven(self):
        text = ENTRYPOINT.read_text(encoding="utf-8")
        self.assertEqual(text.count("(3, 11)"), 1)
        self.assertIn("datetime.UTC", text,
                      "the comment must say which import forced the floor")


class WhichAgentItRuns(ItemCase):
    """The second defect the same end-to-end run found, one layer further in.

    With the interpreter pinned and the clone recognised, the run reached
    `agent()` and died on `FileNotFoundError: 'claude'`. `claude` installs to
    `~/.local/bin`, which a login shell puts on PATH and the runner's exec'd
    environment does not carry -- so the bare name resolved from a terminal
    and resolved to nothing from the queue.
    """

    def bare_path(self):
        """The runner's PATH: no `~/.local/bin`, no Homebrew."""
        return {"PATH": "/usr/bin:/bin"}

    def test_an_explicit_binary_wins_over_everything(self):
        with mock.patch.dict(os.environ, {"SD_PLAN_CLAUDE": "/opt/agent"}):
            self.assertEqual(sd_plan.claude_binary(), "/opt/agent")

    def test_a_bare_path_falls_back_to_the_install_location(self):
        """Not on PATH, but installed where the installer puts it."""
        installed = self.home / ".local" / "bin" / "claude"
        installed.parent.mkdir(parents=True, exist_ok=True)
        installed.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        installed.chmod(0o755)
        with mock.patch.dict(os.environ, self.bare_path(), clear=False):
            os.environ.pop("SD_PLAN_CLAUDE", None)
            with mock.patch.object(sd_plan.Path, "home", lambda: self.home):
                self.assertEqual(sd_plan.claude_binary(), str(installed))

    def test_no_agent_anywhere_refuses_in_a_sentence(self):
        """A missing binary is a refusal, not a traceback.

        What shipped raised `FileNotFoundError` out of `subprocess.run`, so
        the durable receipt held a twenty-line traceback whose last line was
        the only part worth reading.
        """
        with mock.patch.dict(os.environ, self.bare_path(), clear=False):
            os.environ.pop("SD_PLAN_CLAUDE", None)
            with mock.patch.object(sd_plan.Path, "home", lambda: self.home):
                with self.assertRaises(sd_plan.Refused) as refusal:
                    sd_plan.claude_binary()
        self.assertIn("SD_PLAN_CLAUDE", str(refusal.exception))
        self.assertIn(".local/bin/claude", str(refusal.exception))

    def test_the_planning_argv_names_an_absolute_binary(self):
        """The whole point: what reaches `subprocess.run` is a path.

        Pinning the argv rather than only the helper, because the defect was
        that `agent()` used the name and not that the name was unresolvable.
        """
        installed = self.home / ".local" / "bin" / "claude"
        installed.parent.mkdir(parents=True, exist_ok=True)
        installed.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        installed.chmod(0o755)
        seen = []
        with mock.patch.dict(os.environ, self.bare_path(), clear=False):
            os.environ.pop("SD_PLAN_CLAUDE", None)
            os.environ.pop("SD_PLAN_AGENT", None)
            with mock.patch.object(sd_plan.Path, "home", lambda: self.home):
                with mock.patch.object(sd_plan.subprocess, "run",
                                       lambda *a, **k: seen.append(a[0]) or
                                       subprocess.CompletedProcess(a[0], 0)):
                    sd_plan.agent(self.repo, "a-slug", {"id": 1})
        self.assertTrue(os.path.isabs(seen[0][0]), seen[0])


class WhichUserItPlansAs(ItemCase):
    """The fourth defect of the same end-to-end run, and the quietest.

    With the interpreter, the clone and the binary all settled, the agent
    started, ran for three and a half minutes, printed

        Not logged in - Please run /login

    and exited 0 -- which reached `plan()` as a planning run that produced no
    documents rather than as a broken environment. The credential is a
    keychain generic password read under the name in `USER`, and the runner's
    minimal environment carries no `USER` at all.
    """

    def test_the_user_is_the_uid_and_not_the_ambient_name(self):
        """Derived, so a wrong ambient value cannot survive into the keychain."""
        with mock.patch.dict(os.environ, {"USER": "somebody-else"}):
            self.assertEqual(sd_plan.planning_environment()["USER"],
                             pwd.getpwuid(os.getuid()).pw_name)

    def test_an_environment_without_a_user_still_gets_one(self):
        """The runner's case exactly: HOME and PATH, and nothing else."""
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ["HOME"] = str(self.home)
            os.environ["PATH"] = "/usr/bin:/bin"
            self.assertEqual(sd_plan.planning_environment()["USER"],
                             pwd.getpwuid(os.getuid()).pw_name)

    def test_the_rest_of_the_environment_is_carried_through(self):
        """Adding `USER` must not become replacing everything else."""
        with mock.patch.dict(os.environ, {"HOME": "/somewhere", "LANG": "en_US.UTF-8"}):
            environment = sd_plan.planning_environment()
        self.assertEqual(environment["HOME"], "/somewhere")
        self.assertEqual(environment["LANG"], "en_US.UTF-8")

    def test_the_planning_run_is_given_the_environment(self):
        """Pinning the call site: the helper existing is not the fix."""
        installed = self.home / ".local" / "bin" / "claude"
        installed.parent.mkdir(parents=True, exist_ok=True)
        installed.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        installed.chmod(0o755)
        seen = {}
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=False):
            os.environ.pop("SD_PLAN_CLAUDE", None)
            os.environ.pop("SD_PLAN_AGENT", None)
            with mock.patch.object(sd_plan.Path, "home", lambda: self.home):
                with mock.patch.object(
                        sd_plan.subprocess, "run",
                        lambda *a, **k: seen.update(k) or
                        subprocess.CompletedProcess(a[0], 0)):
                    sd_plan.agent(self.repo, "a-slug", {"id": 1})
        self.assertIn("USER", seen.get("env", {}))


if __name__ == "__main__":
    unittest.main()
