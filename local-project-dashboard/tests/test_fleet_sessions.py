"""The fleet's sessions read: registered worktrees and running sd-* commands (sd:719 step 5).

The rows come from `fleet.py` run as a child under the collectors' `Budget`:
the worktree registrations git keeps under each checkout's `.git/worktrees/`,
read from their files, and one `ps` for the process table. Health reads the
worktrees and Management the processes. These tests read the document itself:
the classic Operations > Sessions area that drew it was retired in sd:3402.
"""

from unittest.mock import patch

from sd_dashboard import fleet

from fleet_support import FleetCase, SlowStart

PS = ("  4242 01:02:03 /Users/pat/repos/pack/bin/sd-review --json\n"
      "  4243    00:07 sd store item 719 --json\n"
      "  4244 02:00:00 vim /Users/pat/notes/sd-review.md\n"
      "  4245    00:01 python3 -m unittest\n")


class SessionsRead(FleetCase):

    def sessions(self):
        return fleet.collect("sessions")

    def column(self, document, key, field):
        return [row[field] for row in document[key]]

    def test_worktrees_are_listed_abandoned_first_with_their_branches(self):
        pack = self.checkout("pack", "acme")
        live = self.worktree(pack, "lane-a", "fix/sd-1-lane-a")
        gone = self.abandoned(pack, "lane-b", "fix/sd-2-lane-b")
        self.shim("ps", "exit 0")
        document = self.sessions()
        self.assertEqual(self.column(document, "worktrees", "repo"), ["acme/pack", "acme/pack"])
        self.assertEqual(self.column(document, "worktrees", "name"), ["lane-b", "lane-a"])
        self.assertEqual(self.column(document, "worktrees", "branch"), ["fix/sd-2-lane-b", "fix/sd-1-lane-a"])
        self.assertEqual(self.column(document, "worktrees", "state"), ["abandoned", "live"])
        # git records the worktree by its resolved path: /private/var on macOS.
        self.assertEqual(self.column(document, "worktrees", "path"), [str(gone), str(live.resolve())])

    def test_an_unreadable_registration_is_unknown_and_not_abandoned(self):
        pack = self.checkout("pack")
        live = self.worktree(pack, "lane-c", "fix/sd-3-lane-c")
        self.abandoned(pack, "lane-a", "fix/sd-1-lane-a")
        blind = self.abandoned(pack, "lane-b", "fix/sd-2-lane-b")
        gitdir = pack / ".git" / "worktrees" / "lane-b" / "gitdir"
        gitdir.chmod(0)
        self.addCleanup(gitdir.chmod, 0o644)
        self.shim("ps", "exit 0")
        document = self.sessions()
        self.assertEqual(self.column(document, "worktrees", "name"), ["lane-a", "lane-b", "lane-c"])
        self.assertEqual(self.column(document, "worktrees", "state"), ["abandoned", "unknown", "live"])
        self.assertEqual(self.column(document, "worktrees", "path"), [str(blind.parent / "lane-a"), "", str(live.resolve())])

    def test_running_sd_commands_are_listed_by_their_basename(self):
        self.shim("ps", f"printf '{PS}'")
        document = self.sessions()
        self.assertEqual(self.column(document, "processes", "pid"), ["4242", "4243"])
        self.assertEqual(self.column(document, "processes", "elapsed"), ["01:02:03", "00:07"])
        self.assertEqual(self.column(document, "processes", "command"),
                         ["/Users/pat/repos/pack/bin/sd-review --json", "sd store item 719 --json"])
        self.assertEqual((document["processes_error"], document["processes_truncated"]), ("", False))

    def test_a_missing_root_is_said_rather_than_shown_as_no_worktrees(self):
        self.shim("ps", f"printf '{PS}'")
        with patch.dict("os.environ", {"REPO_ROOT": str(self.root / "elsewhere")}):
            document = self.sessions()
        self.assertFalse(document["rootExists"])
        self.assertEqual(document["worktrees"], [])
        # The process table does not depend on the root and is still read.
        self.assertEqual(self.column(document, "processes", "pid"), ["4242", "4243"])

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
            document = self.sessions()
        # The half that answered is in the document; the half that did not says so.
        self.assertEqual(self.column(document, "worktrees", "name"), ["lane-a"])
        self.assertIn("ps ran past the budget of 4 seconds and was stopped", document["processes_error"])
        self.assertEqual(document["processes"], [])
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
        document = self.sessions()
        self.assertEqual(self.column(document, "worktrees", "name"), ["lane-b", "lane-a"])
        self.assertEqual(self.column(document, "worktrees", "state"), ["unknown", "live"])

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

    def test_other_processes_do_not_crowd_the_sd_commands_out_of_the_table(self):
        # 70 KB of unrelated processes ahead of the sd rows spent the whole
        # budget, so the rows this reads for were cut away before anything
        # looked at them and Sessions said no sd command was running (Codex
        # review of this branch, reproduced against the committed code).
        self.shim("ps", "i=0; while [ $i -lt 1200 ]; do "
                        "printf '  %d 00:01 other-%050d\\n' $i $i; i=$((i+1)); done; "
                        f"printf '{PS}'")
        document = self.sessions()
        self.assertEqual(self.column(document, "processes", "pid"), ["4242", "4243"])
        self.assertFalse(document["processes_truncated"])

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

    def test_an_empty_process_table_is_a_failure_and_not_a_quiet_machine(self):
        self.shim("ps", "exit 0")
        document = self.sessions()
        self.assertIn("ps gave no answer", document["processes_error"])
        self.assertEqual(document["processes"], [])
