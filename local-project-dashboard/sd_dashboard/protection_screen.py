"""The fleet's branch protection, one row per registered repository.

Read from `repo_protection` as the nightly collector left it; nothing here
calls GitHub. The gap ids and sentences are the ones `sd-status` prints for
one repository, so this table is that report across the fleet. The one rule
the rendering exists to keep: a repository whose protection could not be read
is `unknown`, has its own marker, and shows no gap cells -- an unread gap is
not a passed one.
"""

from sd_db import protection

from .listing import Column, Listing
from .markup import tag
from .pages import page

GAPS = (
    ("enforce_admins", "Admins"),
    ("required_checks", "Checks"),
    ("strict", "Strict"),
    ("required_not_produced", "Req. not produced"),
    ("produced_not_required", "Prod. not required"),
    ("reviews", "Reviews"),
    ("bypass", "Bypass"),
)
#: Read from the row's `merge_settings` list. The last two are the fleet
#: baseline (sd:1741): a repository another owner holds carries neither, so
#: its two cells read as not applicable.
FLAGS = (("squash_message", "Squash message"), ("rebase_merge", "Rebase merge"),
         ("protection_source", "Rulesets only"), ("required_check", "Requires ci"))

#: Which gap columns a status can answer. `unprotected` has no admins rule to
#: be exempt from, no checks to be strict about, no contexts to compare and
#: no ruleset to bypass; those cells read as not applicable rather than as
#: passing.
APPLICABLE = {
    "protected": frozenset(gap for gap, _ in GAPS),
    "unprotected": frozenset({"required_checks", "reviews"}),
    "unknown": frozenset(),
}
ORDER = {"unprotected": 0, "unknown": 1, "protected": 2}
NOT_APPLICABLE = "—"


def _link(row):
    """The settings page, only for a github.com remote; a plain name otherwise.

    `slug` is set by `protection.github_slug` and only for a github.com
    remote, ssh or https, so its presence is the hostname check.
    """
    slug = row.get("slug")
    if slug:
        return tag("a", slug, href=f"https://github.com/{slug}/settings/branches", rel="noopener noreferrer")
    return tag("span", row["repo"])


def _status(row):
    status = row["status"]
    marker = tag("span", status, class_=f"status protection-{status}")
    sentence = [gap for gap in row["gaps"] if gap.get("id") == "unprotected"]
    if status == "unprotected" and sentence:
        return tag("details", tag("summary", marker), tag("p", sentence[0].get("gap") or "", class_="protection-sentence"),
                   class_="protection-detail")
    return marker


def _gap_cell(row, gap_id):
    if gap_id not in APPLICABLE[row["status"]]:
        return tag("span", NOT_APPLICABLE, class_="protection-na")
    found = [gap for gap in row["gaps"] if gap.get("id") == gap_id]
    if not found:
        return tag("span", "ok", class_="protection-ok")
    return tag("details", tag("summary", "GAP", class_="protection-gap"),
               tag("p", found[0].get("gap") or "", class_="protection-sentence"), class_="protection-detail")


def _gap_text(row, gap_id):
    if gap_id not in APPLICABLE[row["status"]]:
        return ""
    found = [gap for gap in row["gaps"] if gap.get("id") == gap_id]
    return f"gap {found[0].get('gap') or ''}" if found else "ok"


def _flag_cell(row, flag_id):
    if row["status"] == "unknown":
        return tag("span", NOT_APPLICABLE, class_="protection-na")
    found = [flag for flag in row["merge_settings"] if flag.get("id") == flag_id]
    if not found:
        return tag("span", NOT_APPLICABLE, class_="protection-na")
    flag = found[0]
    if not flag.get("flagged"):
        return tag("span", "ok", class_="protection-ok")
    return tag("details", tag("summary", "GAP", class_="protection-gap"),
               tag("p", tag("code", str(flag.get("value") or "")), " ", flag.get("gap") or "",
                   class_="protection-sentence"), class_="protection-detail")


def _flag_text(row, flag_id):
    found = [flag for flag in row["merge_settings"] if flag.get("id") == flag_id]
    if row["status"] == "unknown" or not found:
        return ""
    return f"gap {found[0].get('value') or ''}" if found[0].get("flagged") else "ok"


def _reason(row):
    if row["status"] != "unknown":
        return ""
    return tag("span", row.get("reason") or "unknown", class_="protection-reason")


def _summary(rows):
    counts = {status: sum(1 for row in rows if row["status"] == status) for status in ORDER}
    observed = [row["observed_at"] for row in rows if row.get("observed_at")]
    last = max(observed) if observed else "never"
    return tag("p", f"{counts['protected']} protected · {counts['unprotected']} unprotected · "
               f"{counts['unknown']} unknown · last observed {last}", class_="protection-summary")


def render(connection, *, parameters):
    rows = sorted(protection.rows(connection), key=lambda row: (ORDER.get(row["status"], 1), row.get("slug") or row["repo"]))
    query, number, selected = Listing.read_query(parameters)
    columns = [
        Column("repo", "Repository", _link, text=lambda row: f"{row.get('slug') or ''} {row['repo']}"),
        Column("status", "Status", _status, text=lambda row: row["status"]),
        Column("branch", "Branch", lambda row: row.get("default_branch") or tag("span", NOT_APPLICABLE, class_="protection-na"),
               text=lambda row: row.get("default_branch") or ""),
    ]
    for gap_id, label in GAPS:
        columns.append(Column(gap_id, label, lambda row, gap_id=gap_id: _gap_cell(row, gap_id),
                              text=lambda row, gap_id=gap_id: _gap_text(row, gap_id), css="column-meta"))
    for flag_id, label in FLAGS:
        columns.append(Column(flag_id, label, lambda row, flag_id=flag_id: _flag_cell(row, flag_id),
                              text=lambda row, flag_id=flag_id: _flag_text(row, flag_id), css="column-meta"))
    columns.append(Column("observed_at", "Observed", lambda row: row.get("observed_at") or "never", css="column-meta"))
    columns.append(Column("reason", "Reason", _reason, text=lambda row: row.get("reason") or "" if row["status"] == "unknown" else ""))
    listing = Listing("protection", columns, rows, path="/protection", query=query, page_number=number,
                      selected=selected,
                      empty="No repositories registered. Register one with sd-db.sh repo add PATH, then run sd shadow sync.")
    return page("Protection", "protection",
        tag("p", "What each registered repository's default branch enforces, as the nightly collector last read it. "
                 "Unprotected first, then unknown, then protected.", class_="lead"),
        tag("p", "Unknown is not protected: a repository whose protection could not be read shows no gap cells. "
                 "Protection that exempts admins is prose, not authority.", class_="hint"),
        _summary(rows), listing.render(), cli_equivalents=False)
