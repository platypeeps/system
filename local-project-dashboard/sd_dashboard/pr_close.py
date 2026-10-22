"""Close original (sd:5669): once the lane merged Merge's copy of a pull request (sd:5668), Today > GitHub closes the
original with one comment that links the lane's pull request and its merge commit.

The operator's ruling (sd:5668): "close the original with a link. Commits keep their authors." Closing someone else's
pull request is an outward act, so nothing closes on its own: the operator presses Close original and confirms the exact
comment. The original is closed, never merged.

The copy is merged when the item filed under the ref `github:<owner>/<name>#<n>` (Merge's step 4) carries a `Code
delivery <url> at <commit>` note, which `sd-ship merge` writes once the lane's pull request merged (`ship.note_merge`).
Its URL must name another pull request of the same repository: a note about the original itself, or about another
repository's, is no merge of this copy. The newest such note counts (`landed`); Today's rows carry it, so the command is
off, with the reason, until the lane merged.

The page reads `GET /api/today/github/close/preview?repo=<owner/name>&number=<n>` (`preview`): the lane's pull request,
its merge commit and the exact comment, from the database; nothing calls GitHub on that read. The confirm posts
`/api/today/github/close` `{repo, number, revision}` (`request`); the write reads the merge again, and a revision that
differs moves nothing. Then GitHub, through gh as Copilot review calls it (`issue_filing.GH`):

1. Read. `gh api repos/<repo>/pulls/<n>`: the answer must name this pull request, open and not merged; then
   `gh api --paginate repos/<repo>/pulls/<lane>/commits` must hold its head, or the author pushed after the copy.
2. Find. `gh api --paginate repos/<repo>/issues/<n>/comments`, for a comment carrying `MARKER` with this merge's link
   and commit: the one an earlier run posted.
3. Comment, unless step 2 found one: `POST repos/<repo>/issues/<n>/comments` with the text the preview showed.
4. Close. `PATCH repos/<repo>/pulls/<n>` `{"state": "closed"}`.

Failure table (step, state moved, failure, recovery, test in `test_v2_today_github_close.py`):

- Read (database): nothing. No merge recorded, or a delivery note naming the original or another repository: 409
  (`test_a_copy_not_merged_is_refused_before_gh_runs`,
  `test_a_delivery_of_another_pull_request_is_no_merge_of_the_copy`).
- Revision: nothing. Another merge recorded since the preview: `StaleItem`
  (`test_a_revision_that_differs_moves_nothing`).
- 1 Read: nothing. Closed or merged on GitHub: 409 (`test_a_pull_request_github_shows_closed_or_merged_is_refused`);
  a head the lane's pull request does not hold: 409 (`test_a_head_pushed_after_the_copy_is_refused_and_posts_nothing`);
  gh failed, or answered about another pull request: 502, not read
  (`test_a_read_that_fails_is_not_known_and_posts_nothing`).
- 2 Find: nothing. gh failed: 502, not read (`test_a_read_that_fails_is_not_known_and_posts_nothing`).
- 3 Comment: one comment. GitHub or gh refused: 409, nothing posted (`test_a_comment_github_refused_is_not_posted`). Any
  other failure: 502 `outcome: unknown`; the next Close finds the comment by its marker and does not post again
  (`test_a_comment_with_no_answer_is_found_by_the_next_close_and_not_posted_again`).
- 4 Close: the pull request's state. Refused or failed: 502 with the comment posted; the next Close finds the comment
  and only closes (`test_a_close_that_failed_after_the_comment_closes_on_the_next_press_with_one_comment`). No answer:
  `outcome: unknown`; the next Close reads it closed, a 409
  (`test_a_close_with_no_answer_reads_closed_on_the_next_press`).
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess

from .land import Unknown

__all__ = ["landed", "preview", "request"]

GH_SECONDS = 60
#: The hidden line that names a comment this view posted: the lane's pull request and its merge commit.
MARKER = "<!-- close-original {url} {commit} -->"
DELIVERY = re.compile(r"Code delivery (\S+) at ([0-9a-f]{40,64})\b")


class Refused(Exception):
    """gh or GitHub refused the call, so it changed nothing."""


def landed(connection, repo: str, number: int) -> dict | None:
    """`{item, url, commit}`: the lane's merge of the copy filed under the pull request's ref, or None."""
    from sd_db import contributions, reads, writes

    from .pr_merge import _ref_names
    from .today_github import REF_SOURCE

    for ref in _ref_names(repo, number):
        held = writes.item_by_external(connection, REF_SOURCE, ref)
        if held is None:
            continue
        found = None
        for note in reads.item_notes(connection, held["id"]):
            delivery = DELIVERY.match(note["body"] or "") if note["kind"] == "comment" else None
            pull = delivery and contributions.PULL.fullmatch(delivery[1])
            # Only another pull request of the same repository is the copy's: never the original, nor another repo's.
            if pull and f"{pull[1]}/{pull[2]}".lower() == repo.lower() and int(pull[3]) != number:
                found = {"item": held["id"], "url": delivery[1], "commit": delivery[2]}
        return found
    return None


def _text(merged: dict) -> str:
    return (f"Thank you. This change landed through {merged['url']}, merge commit {merged['commit']}, so this pull "
            f"request closes in its favor.\n\n" + MARKER.format(url=merged["url"], commit=merged["commit"]))


def _read(connection, repo: str, number: int) -> dict:
    merged = landed(connection, repo, number)
    if merged is None:
        raise ValueError(f"No merge of {repo}#{number}'s copy is recorded: Merge copies it, and Close original opens "
                         "once the lane merged that copy. Nothing moved.")
    text = _text(merged)
    subject = [repo.lower(), number, merged["item"], merged["url"], merged["commit"], text]
    return {"repo": repo, "number": number, "url": f"https://github.com/{repo}/pull/{number}", "merged": merged["url"],
            "commit": merged["commit"], "item": merged["item"], "text": text,
            "revision": hashlib.sha256(json.dumps(subject).encode()).hexdigest()}


def _cli(facts: dict) -> str:
    target = f"repos/{facts['repo']}"
    return (f"gh api {target}/issues/{facts['number']}/comments --method POST -f body={shlex.quote(facts['text'])} && "
            f"gh api {target}/pulls/{facts['number']} --method PATCH -f state=closed")


def preview(connection, parameters: dict) -> dict | tuple[int, dict]:
    """`GET /api/today/github/close/preview?repo=<owner/name>&number=<n>`: the comment Close posts, and its revision."""
    from .pr_merge import _values

    first = {name: (value[0] if isinstance(value, list) and value else value) for name, value in parameters.items()}
    try:
        facts = _read(connection, *_values(first.get("repo"), first.get("number"), "Close original"))
    except ValueError as error:
        return 409, {"error": str(error)}
    return {**facts, "cli": _cli(facts)}


def _api(run, path: str, *options: str, body: dict | None = None) -> str:
    """gh's answer to one `gh api` call; `Refused` when gh or GitHub refused it, `Unknown` on any other failure."""
    from .issue_filing import GH, GH_REFUSED

    sent = ["--input", "-"] if body is not None else []
    try:
        done = run(["sh", "-c", GH, "sh", "api", path, *options, *sent], input=json.dumps(body) if sent else None,
                   capture_output=True, text=True, timeout=GH_SECONDS, start_new_session=True, check=False)
    except subprocess.TimeoutExpired:
        raise Unknown(f"gh gave no answer in {GH_SECONDS} s") from None
    except OSError as problem:
        raise Refused(f"gh did not run ({type(problem).__name__})") from None
    if done.returncode != 0:
        said = [line for line in done.stderr.splitlines() if line.strip()]
        why = f"gh exited {done.returncode}: {said[-1] if said else 'no message'}"
        # A nonzero exit is no proof: GitHub can take the call and the answer still fail on the way back. Exit 127 is
        # the shell's own: gh was not found, so nothing was sent.
        if done.returncode == 127 or GH_REFUSED.search(done.stderr):
            raise Refused(why)
        raise Unknown(why)
    return done.stdout


def _json(text: str) -> dict:
    try:
        answer = json.loads(text)
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        raise Unknown("gh exited 0 but its answer was no JSON object")
    return answer


def close(connection, repo: str, number: int, expected: str, *, run=None):
    """`(status, document)`: 200 once GitHub answered the pull request closed, 409 refused, 502 failed part way."""
    run = run or subprocess.run
    try:
        facts = _read(connection, repo, number)
    except ValueError as error:
        return 409, {"error": str(error)}
    if facts["revision"] != expected:
        from sd_db import workflow

        raise workflow.StaleItem("The recorded merge changed since the preview. Nothing moved. "
                                 "Open Close original again.")
    ref, pulls = f"{repo}#{number}", f"repos/{repo}/pulls/{number}"
    marker = MARKER.format(url=facts["merged"], commit=facts["commit"])
    try:
        pull = _json(_api(run, pulls))
        base = (pull.get("base") or {}).get("repo") or {}
        if pull.get("number") != number or str(base.get("full_name", "")).lower() != repo.lower():
            raise Unknown(f"GitHub's answer did not name {ref}")
        if pull.get("merged") or pull.get("state") != "open":
            return 409, {"error": f"GitHub shows {ref} {'merged' if pull.get('merged') else pull.get('state')}; "
                                  "nothing to close. Nothing moved."}
        # The lane's pull request was built from the head Merge copied, so the original's head now must be one of its
        # commits: a push after the copy landed nowhere, and closing would drop it.
        head, lane = str((pull.get("head") or {}).get("sha") or ""), facts["merged"].rstrip("/").rsplit("/", 1)[-1]
        copied = _api(run, f"repos/{repo}/pulls/{lane}/commits", "--paginate", "--jq", ".[].sha").split()
        if not head or head not in copied:
            return 409, {"error": f"{ref}'s head {head[:12] or '(none)'} is not in {facts['merged']}: its author pushed "
                                  "after the copy. Merge it again, or close it by hand. Nothing moved."}
        # jq's string is the marker as JSON writes it, so a quote or a backslash in it stays a literal.
        posted = _api(run, f"repos/{repo}/issues/{number}/comments", "--paginate", "--jq",
                      f".[] | select((.body // \"\") | contains({json.dumps(marker)})) | .html_url").split()
    except (Refused, Unknown) as error:
        return 502, {"error": f"{ref} was not read: {error}. Nothing moved."}
    done = {"repo": repo, "number": number, "merged": facts["merged"], "commit": facts["commit"],
            "comment": posted[0] if posted else None, "commented": False, "closed": False}
    if not posted:
        try:
            answer = _json(_api(run, f"repos/{repo}/issues/{number}/comments", "--method", "POST",
                                body={"body": facts["text"]}))
        except Refused as error:
            return 409, {"error": f"{error}; the comment was not posted, and {ref} stays open."}
        except Unknown as error:
            return 502, {"error": f"Whether the comment was posted is not known ({error}); {ref} stays open. Close "
                                  "original again finds the comment before it posts one.", "outcome": "unknown", **done}
        url = str(answer.get("html_url") or "")
        if not url.lower().startswith(f"https://github.com/{repo}/pull/{number}#issuecomment-".lower()):
            return 502, {"error": f"gh exited 0 but its answer named no comment on {ref}; Close original again finds "
                                  "the comment before it posts one.", "outcome": "unknown", **done}
        done.update(comment=url, commented=True)
    try:
        answer = _json(_api(run, pulls, "--method", "PATCH", body={"state": "closed"}))
    except Refused as error:
        return 502, {"error": f"Commented, but GitHub refused the close: {error}. Close original again closes without "
                              "a second comment.", **done}
    except Unknown as error:
        return 502, {"error": f"Commented; whether {ref} closed is not known ({error}). Close original again reads it.",
                     "outcome": "unknown", **done}
    if answer.get("number") != number or answer.get("state") != "closed":
        return 502, {"error": f"Commented; gh exited 0 but its answer did not show {ref} closed. Close original again "
                              "reads it.", "outcome": "unknown", **done}
    return {**done, "closed": True}


def request(payload: dict, principal: str):
    """`POST /api/today/github/close`: the page's checks before the server opens a connection; the write as
    `action_route` takes it."""
    from .pr_merge import _values

    values = dict(payload)
    if set(values) != {"repo", "number", "revision"} or not isinstance(values["revision"], str) \
            or not re.fullmatch(r"[0-9a-f]{64}", values["revision"]) or isinstance(values["number"], str):
        raise ValueError("Close original takes the pull request's repo, its number and the preview's revision.")
    repo, number = _values(values["repo"], values["number"], "Close original")
    return lambda connection: close(connection, repo, number, values["revision"])
