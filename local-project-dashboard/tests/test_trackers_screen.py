"""Operations > Trackers: PRs and Issues out of `sd_db.shadow` (sd:719 step 4).

The rows are the cache `sd shadow sync` keeps; the page reads them through
`progress.tracker_items` and reports each tracker's health through
`progress.tracker_freshness`. Nothing here opens a database of its own;
the criterion 2 grep in `tests/test_markup.py` is the check.
"""

import re

from sd_db import upsert_shadow
from sd_db.shadow_sync import write_watermark
from sd_db.writes import record_state

from support import ScreenCase

PULL = "https://github.com/acme/widgets/pull/4321"
JIRA = "https://example.atlassian.net/browse/LOG-1234"


class TrackersArea(ScreenCase):

    def setUp(self):
        super().setUp()
        upsert_shadow(self.connection, tracker="github", repo="acme/widgets", url=PULL,
                      number=4321, kind="pull", title="Widen the widget", state="open", author="pat")
        upsert_shadow(self.connection, tracker="jira", repo="LOG", url=JIRA,
                      kind="issue", title="Log lines arrive twice", state="open", author="Pat Example")

    def trackers(self, **parameters):
        return self.render("/operations", {"area": ["trackers"], **{key: [value] for key, value in parameters.items()}})

    def references(self, page):
        """The text of every reference cell, in table order."""
        return re.findall(r'<td class="tracker-reference">(?:<a [^>]*>)?([^<]+)', page)

    def test_a_github_row_shows_its_number_and_a_jira_row_its_key(self):
        prs = self.trackers(view="prs")
        self.assertEqual(self.references(prs), ["acme/widgets#4321"])
        self.assertIn(f'href="{PULL}"', prs)
        self.assertNotIn("LOG-1234", prs)
        issues = self.trackers(view="issues")
        self.assertIn(f'href="{JIRA}"', issues)
        self.assertNotIn("acme/widgets#4321", issues)

    def test_a_jira_row_shows_its_ticket_and_not_its_project(self):
        # The key is the URL's last path segment, never the `repo` column,
        # which for Jira is the project: `LOG` where the ticket is `LOG-1234`.
        issues = self.trackers(view="issues")
        self.assertEqual(self.references(issues), ["LOG-1234"])
        self.assertNotIn(">LOG</a>", issues)

    def test_the_area_is_in_the_navigation_and_prs_is_the_default_view(self):
        page = self.trackers()
        navigation = re.search(r'<nav[^>]*aria-label="Operations areas"[^>]*>(.*?)</nav>', page).group(1)
        self.assertIn('href="/operations?area=trackers"', navigation)
        self.assertIn(">Trackers</a>", navigation)
        self.assertEqual(self.references(page), ["acme/widgets#4321"])
        self.assertEqual(self.references(self.trackers(view="nonsense")), ["acme/widgets#4321"])

    def test_only_open_rows_are_listed_and_a_github_issue_lists_with_jira(self):
        upsert_shadow(self.connection, tracker="github", repo="acme/widgets",
                      url="https://github.com/acme/widgets/issues/7", number=7, kind="issue",
                      title="Widget wobbles", state="open", author="pat")
        upsert_shadow(self.connection, tracker="github", repo="acme/widgets",
                      url="https://github.com/acme/widgets/pull/9", number=9, kind="pull",
                      title="Merged already", state="merged", author="pat")
        self.assertEqual(self.references(self.trackers(view="prs")), ["acme/widgets#4321"])
        self.assertEqual(sorted(self.references(self.trackers(view="issues"))), ["LOG-1234", "acme/widgets#7"])

    def test_each_tracker_reports_its_sync_health(self):
        page = self.trackers(view="issues")
        self.assertIn("github: never synced", page)
        self.assertIn("jira: never synced", page)
        write_watermark(self.connection, "jira", "2026-09-06T11:00:00+00:00", "2026-09-05T11:00:00+00:00")
        page = self.trackers(view="issues")
        self.assertIn("jira: fresh", page)
        self.assertIn("github: never synced", page)
        self.assertNotIn("jira: never synced", page)

    def test_a_stale_tracker_names_its_last_success_and_a_degraded_one_its_reason(self):
        # `_health` has a `stale` path and a `degraded` path beyond `never`
        # and `fresh`, and neither was rendered by a test (PR #411 review).
        # The clock is the fixture's `NOW`, 2026-09-06T12:00Z; a success
        # more than a day before it is stale.
        write_watermark(self.connection, "jira", "2026-09-01T11:00:00+00:00", "2026-08-31T11:00:00+00:00")
        page = self.trackers(view="issues")
        self.assertRegex(page, r"jira: stale · last successful sync <time [^>]*datetime=\"2026-09-01T11:00:00\+00:00\"")
        self.assertNotIn("jira: never synced", page)
        # A failed attempt after the last success degrades the tracker, and
        # the page says why in the collector's words.
        record_state(self.connection, "heartbeat", key="tracker-sync:jira",
                     timestamp="2026-09-06T11:30:00+00:00",
                     body={"ok": False, "reason": "Jira answered 503"})
        page = self.trackers(view="issues")
        self.assertRegex(page, r"jira: degraded · last successful sync <time [^>]*>[^<]*</time> · Jira answered 503")
        self.assertNotIn("jira: stale", page)
        # A failed attempt with no success behind it is degraded with no time.
        record_state(self.connection, "heartbeat", key="tracker-sync:github",
                     timestamp="2026-09-06T11:30:00+00:00",
                     body={"ok": False, "reason": ""})
        page = self.trackers(view="issues")
        self.assertIn("github: degraded · Latest tracker refresh failed", page)
        self.assertNotIn("github: never synced", page)

    def test_the_view_toggle_carries_the_filter_and_the_page(self):
        # The toggle was a bare `?area=trackers&view=...`, so switching views
        # from a filtered or paged list reset both (PR #411 review).
        page = self.trackers(view="prs", q="widget", page="2")
        navigation = re.search(r'<nav[^>]*aria-label="Trackers"[^>]*>(.*?)</nav>', page).group(1)
        self.assertIn('href="/operations?area=trackers&amp;page=2&amp;q=widget&amp;view=issues"', navigation)
        self.assertIn('href="/operations?area=trackers&amp;page=2&amp;q=widget&amp;view=prs"', navigation)
        bare = re.search(r'<nav[^>]*aria-label="Trackers"[^>]*>(.*?)</nav>', self.trackers(view="prs")).group(1)
        self.assertIn('href="/operations?area=trackers&amp;view=issues"', bare)

    def test_an_unsafe_url_is_text_and_its_tail_is_escaped(self):
        upsert_shadow(self.connection, tracker="jira", repo="LOG", url="javascript:alert(1)/<b>x",
                      kind="issue", title="odd", state="open")
        # An authority with no host: `netloc` is `@`, `hostname` is None.
        upsert_shadow(self.connection, tracker="jira", repo="LOG", url="https://@/browse/LOG-1",
                      kind="issue", title="hostless", state="open")
        page = self.trackers(view="issues")
        self.assertNotIn('href="javascript:', page)
        self.assertNotIn('href="https://@/', page)
        self.assertIn("<span>LOG-1</span>", page)
        self.assertNotIn("<b>x", page)
        self.assertIn("&lt;b&gt;x", page)
