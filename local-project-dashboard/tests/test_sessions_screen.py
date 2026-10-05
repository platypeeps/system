"""Operations > Sessions: registered worktrees and running sd-* commands (sd:719 step 5).

The rows come from `fleet.py` run as a child under the collectors' `Budget`:
the worktree registrations git keeps under each checkout's `.git/worktrees/`,
read from their files, and one `ps` for the process table. Nothing here opens
a database of its own; the criterion 2 grep in `tests/test_markup.py` is the
check.
"""

import re
from unittest.mock import patch

from sd_dashboard import fleet, operations_screen
from sd_dashboard.sessions_screen import sessions_panel

from fleet_support import FleetCase, SlowStart

PS = ("  4242 01:02:03 /Users/pat/repos/pack/bin/sd-review --json\n"
      "  4243    00:07 sd store item 719 --json\n"
      "  4244 02:00:00 vim /Users/pat/notes/sd-review.md\n"
      "  4245    00:01 python3 -m unittest\n")


class SessionsArea(FleetCase):

    def sessions(self, **parameters):
        return self.render("/operations", {"area": ["sessions"], **{key: [value] for key, value in parameters.items()}})

    def cells(self, page, css):
        return [re.sub(r"<[^>]+>", "", cell).strip()
                for cell in re.findall(rf'<td class="{css}">(.*?)</td>', page, re.DOTALL)]

    def test_worktrees_are_listed_abandoned_first_with_their_branches(self):
        pack = self.checkout("pack", "acme")
        live = self.worktree(pack, "lane-a", "fix/sd-1-lane-a")
        gone = self.abandoned(pack, "lane-b", "fix/sd-2-lane-b")
        self.shim("ps", "exit 0")
        page = self.sessions()
        self.assertEqual(self.cells(page, "worktree-repo"), ["acme/pack", "acme/pack"])
        self.assertEqual(self.cells(page, "worktree-name"), ["lane-b", "lane-a"])
        self.assertEqual(self.cells(page, "worktree-branch"), ["fix/sd-2-lane-b", "fix/sd-1-lane-a"])
        self.assertEqual(self.cells(page, "worktree-state"), ["Abandoned", "Live"])
        # git records the worktree by its resolved path: /private/var on macOS.
        self.assertEqual(self.cells(page, "worktree-path"), [str(gone), str(live.resolve())])
        self.assertIn("2 registered worktrees · 1 abandoned", page)

    def test_an_unreadable_registration_is_unknown_and_not_abandoned(self):
        pack = self.checkout("pack")
        live = self.worktree(pack, "lane-c", "fix/sd-3-lane-c")
        self.abandoned(pack, "lane-a", "fix/sd-1-lane-a")
        blind = self.abandoned(pack, "lane-b", "fix/sd-2-lane-b")
        gitdir = pack / ".git" / "worktrees" / "lane-b" / "gitdir"
        gitdir.chmod(0)
        self.addCleanup(gitdir.chmod, 0o644)
        self.shim("ps", "exit 0")
        page = self.sessions()
        self.assertEqual(self.cells(page, "worktree-name"), ["lane-a", "lane-b", "lane-c"])
        self.assertEqual(self.cells(page, "worktree-state"), ["Abandoned", "Unknown", "Live"])
        self.assertEqual(self.cells(page, "worktree-path"), [str(blind.parent / "lane-a"), "(unreadable registration)", str(live.resolve())])
        self.assertIn("3 registered worktrees · 1 abandoned · 1 unreadable", page)

    def test_a_filter_that_matches_no_process_says_so_as_a_filter(self):
        self.shim("ps", f"printf '{PS}'")
        page = self.sessions(**{"processes-q": "nothing-like-this"})
        self.assertIn("No sd-* command matches this filter.", page)
        self.assertNotIn("No sd-* command is running.", page)

    def test_each_listing_filters_and_pages_on_its_own_key(self):
        # The two lists are independent row sets. Sharing `q` and `page` meant
        # a process term also hid matching worktrees, and paging one table
        # paged the other (PR #427 review).
        pack = self.checkout("pack")
        self.worktree(pack, "lane-a", "fix/sd-1-lane-a")
        self.shim("ps", f"printf '{PS}'")
        page = self.sessions(**{"processes-q": "sd-review"})
        self.assertEqual(self.cells(page, "process-pid"), ["4242"])
        self.assertEqual(self.cells(page, "worktree-name"), ["lane-a"])
        page = self.sessions(**{"worktrees-q": "lane-a"})
        self.assertEqual(self.cells(page, "worktree-name"), ["lane-a"])
        self.assertEqual(self.cells(page, "process-pid"), ["4242", "4243"])

    def test_one_listings_filter_carries_the_others_state_past_it(self):
        # Each list's form and links carry the sibling's key, so filtering one
        # table does not drop the filter already on the other.
        pack = self.checkout("pack")
        self.worktree(pack, "lane-a", "fix/sd-1-lane-a")
        self.shim("ps", f"printf '{PS}'")
        page = self.sessions(**{"processes-q": "sd-review"})
        forms = re.findall(r'<form method="get" action="/operations"[^>]*>(.*?)</form>', page, re.DOTALL)
        carrying = [form for form in forms if 'name="worktrees-q"' in form]
        self.assertEqual(len(carrying), 1, forms)
        self.assertIn('<input type="hidden" name="processes-q" value="sd-review">', carrying[0])

    def test_running_sd_commands_are_listed_by_their_basename(self):
        self.shim("ps", f"printf '{PS}'")
        page = self.sessions()
        self.assertEqual(self.cells(page, "process-pid"), ["4242", "4243"])
        self.assertEqual(self.cells(page, "process-elapsed"), ["01:02:03", "00:07"])
        self.assertIn("/Users/pat/repos/pack/bin/sd-review --json", page)
        self.assertNotIn("vim", page)
        self.assertNotIn("unittest", page)
        self.assertIn(" · 2 sd-* commands running", page)

    def test_the_area_is_in_the_navigation_and_the_subtitle_names_it(self):
        self.shim("ps", "exit 0")
        page = self.sessions()
        navigation = re.search(r'<nav[^>]*aria-label="Operations areas"[^>]*>(.*?)</nav>', page).group(1)
        self.assertIn('href="/operations?area=sessions"', navigation)
        self.assertIn(">Sessions</a>", navigation)
        self.assertIn(("sessions", "Sessions"), operations_screen.AREAS)
        subtitle = re.search(r'<p class="subtitle">([^<]*)</p>', page).group(1)
        self.assertIn("sessions", subtitle)

    def test_a_missing_root_is_said_rather_than_shown_as_no_worktrees(self):
        self.shim("ps", f"printf '{PS}'")
        with patch.dict("os.environ", {"REPO_ROOT": str(self.root / "elsewhere")}):
            page = self.sessions()
        self.assertIn("does not exist", page)
        self.assertNotIn("0 registered worktrees", page)
        self.assertNotIn('<td class="worktree-name">', page)
        # The process table does not depend on the root and is still read.
        self.assertEqual(self.cells(page, "process-pid"), ["4242", "4243"])

    def test_a_hung_process_table_is_cut_at_the_budget_and_the_worktrees_still_render(self):
        pack = self.checkout("pack")
        self.worktree(pack, "lane-a", "fix/sd-1-lane-a")
        self.hanging("ps")
        # sd:2667. The child gets 4 s, under ps's own 5, so its deadline is
        # what cuts ps, and counts them from its own start: the page's start
        # is read as a moment in the future. A 3 s page with a 2 s child
        # counted from the page's start left a loaded machine no time to
        # start ps at all; here the child has the page's other 8 s to start.
        with patch.object(fleet, "FLEET_MARGIN", 8.0), patch.object(fleet, "time", SlowStart(-60)):
            page = self.sessions()
        # The half that answered is on the page; the half that did not says so.
        self.assertEqual(self.cells(page, "worktree-name"), ["lane-a"])
        self.assertIn("The process table could not be read: ps ran past the budget of 4 seconds and was stopped", page)
        self.assertNotIn('<td class="process-pid">', page)
        # Cut by the child at its own deadline, so the hung ps is gone with
        # it rather than left holding a pipe (the `collectors.run` docstring).
        self.assertGone("ps")

    def test_a_registration_that_is_not_utf8_is_unknown_and_not_a_failed_collection(self):
        # `UnicodeDecodeError` is not an `OSError`. It escaped the `except`
        # around these two reads and aborted the whole Sessions collection,
        # where the surrounding code keeps such a registration as `unknown`.
        pack = self.checkout("pack")
        self.worktree(pack, "lane-a", "fix/sd-1-lane-a")
        entry = pack / ".git" / "worktrees" / "lane-b"
        entry.mkdir(parents=True)
        (entry / "gitdir").write_bytes(b"/tmp/\xff\xfe/.git\n")
        (entry / "HEAD").write_bytes(b"ref: refs/heads/\xff\n")
        self.shim("ps", "exit 0")
        page = self.sessions()
        self.assertNotIn("Sessions could not be observed", page)
        self.assertEqual(self.cells(page, "worktree-name"), ["lane-b", "lane-a"])
        self.assertEqual(self.cells(page, "worktree-state"), ["Unknown", "Live"])

    def test_a_process_table_past_the_ceiling_is_cut_and_the_count_is_a_floor(self):
        # `collectors.run` held the whole `ps` table in the child; only the
        # JSON that left it was ever bounded. Read through `read_output` the
        # ceiling applies to both. It applies to the `sd-*` rows, so 64 KB of
        # them is what reaches it: read here rather than through the page,
        # because a document that large is refused at the child's own budget.
        self.shim("ps", "i=0; while [ $i -lt 900 ]; do "
                        "printf '  %d 00:01 sd-review --json --%060d\\n' $i $i; i=$((i+1)); done")
        collectors = fleet._collectors()
        rows, error, cut = fleet.running(collectors)
        self.assertEqual(error, "")
        self.assertTrue(cut)
        self.assertLess(len(rows), 900)
        self.assertGreater(len(rows), 0)
        # Whole rows only: the row the ceiling fell inside is dropped rather
        # than kept as the part that arrived (Codex review of this branch).
        self.assertTrue(all(row["command"] == f"sd-review --json --{int(row['pid']):060d}" for row in rows))
        self.assertLessEqual(sum(len(row["command"]) for row in rows), collectors.BUDGET_BYTES)

    def test_one_record_past_the_ceiling_is_not_held_whole(self):
        # Under `keep` the ceiling applies to the lines kept, but the record
        # still being assembled is held too, and it had no bound at all: one
        # `ps` record whose command line outgrows the ceiling before its
        # newline arrives grew that buffer without end, against the
        # docstring's "at most `collectors.BUDGET_BYTES` of stdout is ever
        # held" (Copilot review of this branch). 400 KB on one line, not
        # matching the filter, was read and held in full and then discarded.
        # Such a record cannot be kept in any case -- `take` refuses a line
        # the ceiling cannot hold -- so it is cut where the ceiling is, and
        # `cut` says what came back is a floor, which it is: the record was
        # dropped before `keep` ever saw it whole.
        self.shim("ps", "printf 'vim '; i=0; "
                        "while [ $i -lt 400 ]; do printf '%01000d' $i; i=$((i+1)); done; "
                        "printf '\\n  4242 00:01 sd-review --json\\n'")
        collectors = fleet._collectors()
        real = fleet.os.read
        read = []

        def counted(fd, size):
            chunk = real(fd, size)
            read.append(len(chunk))
            return chunk

        with patch.object(fleet.os, "read", counted):
            rows, error, cut = fleet.running(collectors)
        self.assertEqual(error, "")
        self.assertTrue(cut)
        # One chunk past the ceiling is the whole of the overshoot.
        self.assertLessEqual(sum(read), collectors.BUDGET_BYTES + 65536)

    def test_a_cut_table_is_drawn_as_a_floor_and_says_where_it_was_cut(self):
        # The panel's half of the finding above. A document with the cut flag
        # set is drawn here, because a real one large enough to be cut is
        # refused at the child's budget before any page sees it.
        panel = str(sessions_panel({}, backend=lambda: {
            "root": str(self.root), "rootExists": True, "worktrees": [],
            "processes": [{"pid": "4242", "elapsed": "01:02", "command": "sd-review --json"}],
            "processes_error": "", "processes_truncated": True}))
        self.assertIn(" · ≥ 1 sd-* command running", panel)
        self.assertIn("The table was cut at 64 KB, so these are a floor.", panel)

    def test_other_processes_do_not_crowd_the_sd_commands_out_of_the_table(self):
        # 70 KB of unrelated processes ahead of the sd rows spent the whole
        # budget, so the rows this reads for were cut away before anything
        # looked at them and Sessions said no sd command was running (Codex
        # review of this branch, reproduced against the committed code).
        self.shim("ps", "i=0; while [ $i -lt 1200 ]; do "
                        "printf '  %d 00:01 other-%050d\\n' $i $i; i=$((i+1)); done; "
                        f"printf '{PS}'")
        page = self.sessions()
        self.assertEqual(self.cells(page, "process-pid"), ["4242", "4243"])
        self.assertIn(" · 2 sd-* commands running", page)
        self.assertNotIn("≥ 2 sd-* commands", page)
        self.assertNotIn("No sd-* command is running", page)

    def test_a_record_the_ceiling_cuts_in_half_is_not_a_running_command(self):
        # `sdiff /tmp/a /tmp/b` cut after `sd` passed the basename test and
        # became a running `sd` command, with a pid and an elapsed time read
        # off the halves around the cut (Codex review of this branch). The
        # table below is arithmetic, not luck: 655 lines of 100 bytes and one
        # of 21 put the ceiling 15 bytes into the last line, which is where
        # `  9999 00:02 sdiff ...` reads as `  9999 00:02 sd`.
        self.shim("ps", "i=0; while [ $i -lt 655 ]; do "
                        "printf '  %04d 00:01 other-%080d\\n' $i $i; i=$((i+1)); done; "
                        "printf '  1 00:01 other-1234\\n'; "
                        "printf '  9999 00:02 sdiff /tmp/a /tmp/b\\n'")
        rows, error, cut = fleet.running(fleet._collectors())
        self.assertEqual(error, "")
        self.assertEqual(rows, [])
        # Nothing was kept, so nothing was cut: the ceiling is on the rows
        # this reads for, and this table has none.
        self.assertFalse(cut)

    def test_a_table_that_stopped_after_a_row_is_a_failure_and_not_a_quiet_machine(self):
        # Two rows and then a hang: the rows already read said nothing about
        # the rest of the table, but the count of lines seen was taken as the
        # read having happened, so Sessions drew an empty Running list with
        # no error at all (Codex review of PR #500). The deadline here is the
        # `ps` timeout rather than the budget, which is the uncapped path.
        self.shim("ps", f"printf '{PS}'; exec sleep 60")
        with patch.object(fleet, "PS_SECONDS", 1.0):
            rows, error, cut = fleet.running(fleet._collectors())
        self.assertEqual(rows, [])
        self.assertFalse(cut)
        self.assertEqual(error, "ps gave no answer within 1.0 seconds")

    def test_a_ps_that_fails_after_printing_is_a_failure_too(self):
        # The same hole with an exit status instead of a hang.
        self.shim("ps", f"printf '{PS}'; exit 1")
        rows, error, cut = fleet.running(fleet._collectors())
        self.assertEqual(rows, [])
        self.assertIn("gave no answer", error)

    def test_a_cut_table_with_nothing_above_the_cut_does_not_claim_absence(self):
        # A cut table holds an unknown number of rows past the ceiling, so
        # "No sd-* command is running" is a claim the read cannot support,
        # and a filter over it is narrower still (Codex review of PR #500).
        def panel(**parameters):
            return str(sessions_panel({key: [value] for key, value in parameters.items()}, backend=lambda: {
                "root": str(self.root), "rootExists": True, "worktrees": [], "processes": [],
                "processes_error": "", "processes_truncated": True}))

        self.assertIn("The table was cut at 64 KB, so no command below the cut was read.", panel())
        self.assertNotIn("No sd-* command is running.", panel())
        filtered = panel(**{"processes-q": "sd-review"})
        self.assertIn("Nothing above it matches this filter.", filtered)
        self.assertNotIn("No sd-* command matches this filter.", filtered)

    def test_an_empty_process_table_is_a_failure_and_not_a_quiet_machine(self):
        self.shim("ps", "exit 0")
        page = self.sessions()
        self.assertIn("The process table could not be read: ps gave no answer", page)
        self.assertNotIn("0 sd-* commands running", page)
