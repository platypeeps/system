"""Your mail on Today (sd:4191): the mail threads waiting on the operator, under the repo whose alias they came to.

A repo names its aliases in its channels (`email`, pack sd:4191, `repos.mail_aliases`); each is a label of
`local-mail-intake`'s conf. Today reads mail-intake's state files and fetches nothing. The pin in `.sd-pack-rev` predates
`mail_aliases`, so these stub it: what is under test is the page's half, the rows, every reason none could be read and the
drawn table. The pack's own suite tests the setting.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
from pathlib import Path
from unittest import mock

from sd_db import repos
from sd_db.channels import ChannelRefusal

from sd_dashboard import now_screen, today_mail, v2

from support import NOW, ScreenCase
from test_now_screen import JobsBackend, fleet_document
from test_v2_today import run_js

V2 = Path(v2.__file__).resolve().parent
MAIL_JS = (V2 / "static" / "mail.js").read_text(encoding="utf-8") if (V2 / "static" / "mail.js").exists() else ""

ANVIL, KILN = "/home/example/repos/acme/anvil", "/home/example/repos/other/kiln"
# The columns in the order mail-intake writes them today; a file from before this change reads the same.
ARRIVAL_COLUMNS = ("detected_at", "thread_id", "message_id", "alias", "subject", "who", "direction", "waiting_on",
                   "sent_at", "reported_at")
CONF = "account|me@example.test\nme|me@example.test\nalias|acme|acme@example.test\nalias|kiln|kiln@example.test\n"


def arrival(thread, alias, detected, *, waiting="you", subject="", who="Pat Doe", direction="received", reported=""):
    return {"detected_at": detected, "thread_id": thread, "message_id": f"m-{thread}-{detected}", "alias": alias,
            "subject": subject or f"About {thread}", "who": who, "direction": direction, "waiting_on": waiting,
            "sent_at": detected, "reported_at": reported}


class MailCase(ScreenCase):
    """mail-intake's conf and state folder in the test's temp folder, and two repos, each with one alias."""

    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name)
        self.state, self.conf = root / "mail-state", root / "mail-intake.conf"
        self.state.mkdir()
        self.conf.write_text(CONF, encoding="utf-8")
        self.environ = {"MAIL_INTAKE_STATE": str(self.state), "MAIL_INTAKE_CONFIG": str(self.conf),
                        "SYSTEM_TOOLS_CONFIG": str(root / "config"), "HOME": str(root)}
        self.fetched("2026-09-06T05:00:00-06:00")
        for path in (ANVIL, KILN):
            self.repo(path)
        self.aliases = {ANVIL: ["acme"], KILN: ["kiln"]}

    def fetched(self, seen_at):
        with (self.state / "threads.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(("thread_id", "last_message_id", "last_at", "seen_at"))
            writer.writerow(("t-old", "m-1", "2026-09-01T00:00:00-06:00", "2026-09-01T00:00:00-06:00"))
            writer.writerow(("t-new", "m-2", "2026-09-06T04:00:00-06:00", seen_at))

    def arrivals(self, *rows):
        with (self.state / "mail-arrivals.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=ARRIVAL_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)

    def stub(self, **kwargs):
        kwargs.setdefault("side_effect", lambda connection, path: list(self.aliases.get(path, [])))
        return mock.patch.object(repos, "mail_aliases", create=True, **kwargs)

    def read(self, **kwargs):
        with self.stub(**kwargs):
            return today_mail.document(self.connection, now=NOW, environ=self.environ)

    def unread(self, document):
        return {row["repo"]: row["reason"] for row in document["mail_unread"]}


class TheRows(MailCase):
    def test_each_threads_newest_arrival_shows_when_it_waits_on_you_under_a_repo_label_in_48_hours(self):
        self.arrivals(
            arrival("t1", "acme", "2026-09-06T05:00:00-06:00", subject="Invoice <b>due</b>"),
            # Answered: the operator's reply is the newest arrival, so the older one waiting on them is not shown.
            arrival("t2", "acme", "2026-09-05T05:00:00-06:00"),
            arrival("t2", "acme", "2026-09-06T05:00:00-06:00", waiting="them", direction="sent", who="Me"),
            arrival("t3", "acme", "2026-09-04T05:00:00-06:00", subject="Old"),          # 2 days and 1 hour before NOW
            arrival("t4", "acme", "2026-09-06T05:00:00-06:00", waiting="them"),
            arrival("t5", "kiln", "2026-09-05T05:00:00-06:00", who="Lee Roe"),
            arrival("t6", "other", "2026-09-06T05:00:00-06:00"),                        # no repo names this label
            # A reported row still waits: the digest's stamp is not an answer.
            arrival("t7", "kiln", "2026-09-04T13:00:00-06:00", reported="2026-09-05T07:00:00-06:00"),
        )
        document = self.read()
        self.assertEqual((document["mail_error"], document["mail_unread"], document["mail_more"]), ("", [], 0))
        self.assertEqual([(row["repo"], row["thread"], row["subject"], row["who"]) for row in document["mail"]], [
            ("anvil", "t1", "Invoice <b>due</b>", "Pat Doe"),
            ("kiln", "t5", "About t5", "Lee Roe"),
            ("kiln", "t7", "About t7", "Pat Doe"),
        ])
        self.assertEqual(document["mail"][0]["at"], "2026-09-06T05:00:00-06:00")
        self.assertEqual(document["mail"][0]["url"], "https://mail.google.com/mail/?authuser=me%40example.test#all/t1")

    def test_ten_rows_show_and_the_rest_are_counted(self):
        self.arrivals(*(arrival(f"t{n:02}", "acme", f"2026-09-06T{n:02}:00:00Z") for n in range(12)))
        document = self.read()
        self.assertEqual([row["thread"] for row in document["mail"]], [f"t{n:02}" for n in range(11, 1, -1)])
        self.assertEqual(document["mail_more"], 2)

    def test_a_thread_id_is_quoted_into_the_link(self):
        self.arrivals(arrival("a/b#c", "acme", "2026-09-06T05:00:00-06:00"))
        self.assertEqual(self.read()["mail"][0]["url"], "https://mail.google.com/mail/?authuser=me%40example.test#all/a%2Fb%23c")

    def test_repos_without_aliases_read_no_mail_state_and_say_nothing(self):
        (self.state / "threads.csv").unlink()
        self.state.rmdir()
        self.aliases = {}
        self.assertEqual(self.read(), {"mail": [], "mail_more": 0, "mail_unread": [], "mail_error": ""})


class TheUnknowns(MailCase):
    """Unknown is not none: each cause is a reason under each repo it hides, never an empty list."""

    def test_a_missing_state_folder_is_said_under_each_repo_with_aliases(self):
        (self.state / "threads.csv").unlink()
        self.state.rmdir()
        self.aliases = {ANVIL: ["acme"]}
        document = self.read()
        self.assertEqual(document["mail"], [])
        self.assertEqual(self.unread(document), {"anvil": f"{self.state} does not exist: mail-intake has not fetched on this machine"})

    def test_an_unreadable_arrivals_file_is_said(self):
        (self.state / "mail-arrivals.csv").mkdir()
        reasons = self.unread(self.read())
        self.assertEqual(set(reasons), {"anvil", "kiln"})
        self.assertTrue(reasons["anvil"].startswith(f"cannot read {self.state / 'mail-arrivals.csv'}: "), reasons["anvil"])

    def test_a_label_the_conf_does_not_define_is_said_under_its_repo_and_the_others_still_show(self):
        self.aliases = {ANVIL: ["acme", "gone"], KILN: ["kiln"]}
        self.arrivals(arrival("t1", "acme", "2026-09-06T05:00:00-06:00"), arrival("t5", "kiln", "2026-09-06T05:00:00-06:00"))
        document = self.read()
        self.assertEqual(self.unread(document), {"anvil": f"alias gone is not in {self.conf}"})
        self.assertEqual([row["thread"] for row in document["mail"]], ["t5"])

    def test_a_missing_or_broken_conf_is_said(self):
        self.conf.unlink()
        self.assertEqual(self.unread(self.read())["anvil"], f"{self.conf} does not exist")
        self.conf.write_text("account|me@example.test\n", encoding="utf-8")
        self.assertEqual(self.unread(self.read())["kiln"],
                         f"{self.conf}: no alias rows: nothing to watch; no me rows: every message would look like it needs a reply")

    def test_a_last_fetch_over_36_hours_old_is_said(self):
        self.fetched("2026-09-04T23:59:00Z")
        self.arrivals(arrival("t1", "acme", "2026-09-06T05:00:00-06:00"))
        document = self.read()
        self.assertEqual(document["mail"], [])
        self.assertEqual(self.unread(document)["anvil"],
                         "the last fetch was at 2026-09-04T23:59:00Z, over 36 hours ago: check the mail-intake fetch job")
        self.fetched("2026-09-05T00:01:00Z")
        self.assertEqual(self.unread(self.read()), {})

    def test_no_fetch_recorded_is_said(self):
        (self.state / "threads.csv").unlink()
        self.assertEqual(self.unread(self.read())["kiln"],
                         f"{self.state / 'threads.csv'} records no fetch: mail-intake has not fetched on this machine")

    def test_a_repo_whose_channels_cannot_be_read_is_said_and_the_others_still_show(self):
        def aliases(connection, path):
            if path == ANVIL:
                raise ChannelRefusal(f"{ANVIL} channels: 'site' is not a channel")
            return ["kiln"]
        self.arrivals(arrival("t5", "kiln", "2026-09-06T05:00:00-06:00"))
        document = self.read(side_effect=aliases)
        self.assertEqual(self.unread(document), {"anvil": f"{ANVIL} channels: 'site' is not a channel"})
        self.assertEqual([row["thread"] for row in document["mail"]], ["t5"])

    def test_a_cut_short_or_headless_arrivals_file_is_said_not_read_as_no_mail(self):
        """A fetch stopped mid-append leaves a short last line; read as a row, it would hide the thread's real arrival."""
        self.arrivals(arrival("t1", "acme", "2026-09-06T05:00:00-06:00"))
        with (self.state / "mail-arrivals.csv").open("a", encoding="utf-8") as handle:
            handle.write("2026-09-06T06:00:00-06:00,t1,m-9,acme\n")
        document = self.read()
        self.assertEqual(document["mail"], [])
        self.assertEqual(self.unread(document)["anvil"],
                         f"{self.state / 'mail-arrivals.csv'} has a row it cannot read (thread t1): fix or remove that line")
        self.arrivals(arrival("t2", "acme", "2026-09-06T05:00:00-06:00", waiting=""))
        self.assertEqual(self.unread(self.read())["anvil"],
                         f"{self.state / 'mail-arrivals.csv'} has a row it cannot read (thread t2): fix or remove that line")
        (self.state / "mail-arrivals.csv").write_text("id,alias\nt1,acme\n", encoding="utf-8")
        self.assertEqual(self.unread(self.read())["kiln"],
                         f"{self.state / 'mail-arrivals.csv'} has no detected_at, thread_id, waiting_on column: fix or move the file")
        (self.state / "mail-arrivals.csv").unlink()
        self.assertEqual(self.read()["mail_unread"], [], "fetched, and nothing has arrived yet")
        self.arrivals(arrival("t2", "acme", "yesterday"))
        self.assertEqual(self.unread(self.read())["anvil"],
                         f"{self.state / 'mail-arrivals.csv'} has a row it cannot read (thread t2): fix or remove that line")

    def test_a_mail_intake_module_that_cannot_load_is_said_and_today_still_reads(self):
        with mock.patch("sd_dashboard.notes_screen._mail_intake", side_effect=PermissionError("cannot read mail_intake.py")):
            document = self.read()
            with self.stub():
                today = now_screen.document(self.connection, now=NOW, jobs=JobsBackend(self.tmp.name),
                                            fleet=lambda area: fleet_document(area))
        self.assertEqual(self.unread(document)["anvil"], "cannot load local-mail-intake: cannot read mail_intake.py")
        self.assertEqual((today["work_error"], today["mail_error"]), ("", ""))

    def test_a_pack_without_the_read_is_said_with_its_remedy(self):
        with mock.patch.object(repos, "mail_aliases", None, create=True):
            document = today_mail.document(self.connection, now=NOW, environ=self.environ)
        self.assertEqual(document, {"mail": [], "mail_more": 0, "mail_unread": [],
                                    "mail_error": "this sd_db has no repos.mail_aliases; update the command pack"})

    def test_a_failed_repo_list_is_said_not_empty(self):
        with self.stub(), mock.patch.object(repos, "registered", side_effect=sqlite3.OperationalError("database is locked")):
            document = today_mail.document(self.connection, now=NOW, environ=self.environ)
        self.assertEqual(document["mail_error"], "database is locked")


class ThePage(MailCase):
    def test_today_carries_the_mail_and_draws_it_from_its_read(self):
        self.arrivals(arrival("t1", "acme", "2026-09-06T05:00:00-06:00"))
        with self.stub(), mock.patch.dict(os.environ, self.environ):
            today = now_screen.document(self.connection, now=NOW, jobs=JobsBackend(self.tmp.name),
                                        fleet=lambda area: fleet_document(area))
        self.assertEqual(([row["thread"] for row in today["mail"]], today["mail_error"], today["work_error"]), (["t1"], "", ""))
        html = (V2 / "today.html").read_text(encoding="utf-8")
        self.assertIn('<tbody id="mail">', html)
        self.assertIn('id="mail-tally"', html)
        scripts = re.findall(r'<script src="/ui/([a-z-]+\.js)"></script>', html)
        self.assertLess(scripts.index("mail.js"), scripts.index("today.js"))
        self.assertIn("mail:read", (V2 / "static" / "today.js").read_text(encoding="utf-8"))


class TheTable(MailCase):
    """mail.js under JavaScriptCore: it draws what Today's read hands it, escaped, and says each reason."""

    PAGE = """
var els = {}, listeners = {};
document.getElementById = id => (els[id] = els[id] || { id, replaceChildren(f) { sunk.push([id, f.parsed]); } });
document.addEventListener = (t, f) => { listeners[t] = f; };
"""

    def drawn(self, detail):
        script = self.PAGE + MAIL_JS + f"listeners['mail:read']({{ detail: {json.dumps(detail)} }}); JSON.stringify(sunk)"
        return dict(run_js(self, script))

    def test_rows_link_the_subject_escape_it_and_count_the_rest(self):
        self.arrivals(arrival("t1", "acme", "2026-09-06T05:00:00-06:00", subject="Invoice <b>due</b>"))
        document = self.read()
        drawn = self.drawn({**document, "mail_more": 3, "mail_unread": [{"repo": "kiln", "reason": "alias kiln is not in x.conf"}]})
        self.assertIn('<a href="https://mail.google.com/mail/?authuser=me%40example.test#all/t1">Invoice &lt;b&gt;due&lt;/b&gt;</a>',
                      drawn["mail"])
        self.assertIn(">Pat Doe<", drawn["mail"])
        self.assertIn(">anvil<", drawn["mail"])
        self.assertIn("3 more wait in Gmail.", drawn["mail"])
        self.assertIn("kiln: mail not read: alias kiln is not in x.conf", drawn["mail"])
        self.assertIn("4 waiting", drawn["mail-tally"])

    def test_a_failure_is_a_row_naming_it_and_none_is_said(self):
        failed = self.drawn({"mail_error": "database is locked"})
        self.assertIn("Mail could not be read: database is locked. Refresh tries again.", failed["mail"])
        self.assertNotIn("waiting", failed["mail-tally"])
        empty = self.drawn({"mail": [], "mail_more": 0, "mail_unread": [], "mail_error": ""})
        self.assertIn("No thread waits on you in a repo with mail aliases.", empty["mail"])
