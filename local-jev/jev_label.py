"""`jev label RULE`: write whether a recorded judgment was right, from what
happened afterwards (sd:2107).

Reported confidence is not accuracy, and a per-call human label does not
scale. For most stages a later event says whether the judgment held; this is
where that event is read and written down as the row's `override`.

**It asks no model.** It reads the ledger through `sd-db.sh`, the pull request
through `gh`, and the default branch through `git` in the local checkout. A
machine with Jev switched off labels exactly the same rows.

**It never opens the database.** `sd-db.sh judgments unlabelled` is the read
and `sd-db.sh judgments label` is the write, so the ledger's refusals are the
only ones, and `jev.py` stays standalone: nothing here is imported by it.

**It writes only a final outcome.** A change not yet merged, merged inside the
window, in a repository with no checkout here, or whose outcome `gh` or a
stale checkout could not settle is skipped with its reason and read again on
the next run. Without `--apply` it writes nothing and says what it would.

The one rule today is `sd-review`: the review tier `sd-review` chose, and
whether a fix followed. The subject is `sd-review-tier:<owner>.<repo>:<sha12>`,
which the pack records with `--subject`.

* The pull request containing the commit is found with
  `gh api repos/<owner>/<repo>/commits/<sha>/pulls`.
* Merged less than `WINDOW_DAYS` ago: the window is open.
* A fix is a default-branch commit in the `WINDOW_DAYS` after the merge,
  touching a file of the pull request, whose subject starts with the word
  `fix` or says `revert`.
* A fix means the tier was one too shallow, so the label is the next deeper
  position; at the deepest tier a fix is not the tier's fault and the label
  equals the answer. No fix: the label equals the answer. The rule sees a
  missed problem, never wasted depth, and the report says so.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent

STAGE = "JEV_SD_REVIEW"
PREFIX = "sd-review-tier:"
SOURCE = "outcome.sd-review.14d"

#: How long after the merge a fix still counts against the review.
WINDOW_DAYS = 14

#: How long an unmerged change is waited for before it is called abandoned.
ABANDONED_DAYS = 30

#: The deepest position of the default tier order (`skip`, `cheap`,
#: `standard`, `deep`). The ledger holds a position, not the list, so a
#: repository with its own tier order names its deepest with `--deepest`.
DEEPEST = 4

#: `<owner>.<repo>:<sha>`. A GitHub owner cannot contain `.`, so the first `.`
#: splits owner from repository.
SUBJECT = re.compile(r"^sd-review-tier:([A-Za-z0-9-]+)\.([A-Za-z0-9._-]+):([0-9a-f]{7,40})$")

#: A fix: the subject starts with the word `fix` (`fix:`, `fix(scope):`,
#: `Fixes ...`) or says `revert` anywhere. `Fixture data` is not a fix.
FIX = re.compile(r"^\s*fix(es|ed)?\b|revert", re.IGNORECASE)

#: A position: a whole number of one or more.
POSITION = re.compile(r"^[1-9][0-9]*$")

#: `github.com/<owner>/<repo>` in any transport's spelling.
GITHUB = re.compile(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")


class Skip(Exception):
    """This row's outcome is not final or cannot be read here; say why."""


def when(stamp: str) -> datetime:
    moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def sd_db_command(env) -> list[str]:
    script = env.get("JEV_SD_DB") or str(HERE.parent / "local-sd-db" / "sd-db.sh")
    return ["sh", script]


def run(argv, env, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, env=env, cwd=cwd, capture_output=True,
                          text=True, timeout=120)


def gh_json(path: str, env, *, paginate: bool = False):
    """`gh api PATH`, parsed. `--paginate` prints one JSON document per page,
    so the pages are decoded one after another and joined."""
    argv = ["gh", "api", *(["--paginate"] if paginate else []), path]
    try:
        done = run(argv, env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Skip(f"gh failed ({exc.__class__.__name__})") from None
    if done.returncode != 0:
        last = (done.stderr.strip().splitlines() or ["no message"])[-1]
        raise Skip(f"gh failed ({last})")
    decoder, text, index, pages = json.JSONDecoder(), done.stdout, 0, []
    try:
        while index < len(text.rstrip()):
            while text[index].isspace():
                index += 1
            page, index = decoder.raw_decode(text, index)
            pages.append(page)
    except (ValueError, IndexError):
        raise Skip("gh failed (the answer was not JSON)") from None
    if not paginate:
        return pages[0] if pages else None
    return [item for page in pages for item in (page if isinstance(page, list) else [page])]


def git(checkout: Path, *args, env) -> subprocess.CompletedProcess:
    return run(["git", "-C", str(checkout), *args], env)


def checkouts(env) -> dict[str, Path]:
    """`owner/repo` (lower case) to the registered checkout, from
    `sd-db.sh repo list`. A row whose remote is not GitHub is left out."""
    done = run([*sd_db_command(env), "repo", "list"], env)
    if done.returncode != 0:
        return {}
    home = Path(env.get("HOME") or "~").expanduser()
    found = {}
    for line in done.stdout.splitlines():
        if not line.startswith("sd-db: "):
            continue
        # `path  remote  source  managed  ci  runner_merge`; the path may hold
        # spaces, so the five single-token fields are split off the right.
        fields = line[len("sd-db: "):].rsplit("  ", 5)
        if len(fields) != 6:
            continue
        path, remote = fields[0], fields[1]
        match = GITHUB.search(remote)
        if not match:
            continue
        where = home / path[2:] if path.startswith("~/") else Path(path)
        found[f"{match.group(1)}/{match.group(2)}".lower()] = where
    return found


def fixes_after(checkout: Path, merge: str, base: str, merged: datetime,
                files: list[str], env) -> list[tuple[str, str]]:
    """The default-branch commits in the window after the merge that touch a
    file of the pull request and read as a fix, oldest first."""
    until = merged + timedelta(days=WINDOW_DAYS)
    done = git(checkout, "log", "--reverse", "--format=%H%x09%s",
               f"--since={merged.isoformat()}", f"--until={until.isoformat()}",
               f"{merge}..origin/{base}", "--", *files, env=env)
    if done.returncode != 0:
        raise Skip("git log failed in the checkout")
    found = []
    for line in done.stdout.splitlines():
        sha, _, subject = line.partition("\t")
        if FIX.search(subject):
            found.append((sha, subject))
    return found


class SdReview:
    """The `sd-review` rule. One instance per run, so the checkouts and the
    freshness of each default branch are read once."""

    def __init__(self, env, now: datetime, deepest: int):
        self.env = env
        self.now = now
        self.deepest = deepest
        self._checkouts = None
        self._fresh: dict[tuple[str, str], bool] = {}

    def checkout(self, slug: str) -> Path:
        if self._checkouts is None:
            self._checkouts = checkouts(self.env)
        found = self._checkouts.get(slug.lower())
        if found is None or not found.exists():
            raise Skip("no checkout")
        return found

    def fresh(self, slug: str, checkout: Path, base: str) -> bool:
        """Whether the checkout's `origin/<base>` holds GitHub's head of it.
        A stale one would read a fix it has not fetched as no fix."""
        key = (slug, base)
        if key not in self._fresh:
            branch = gh_json(f"repos/{slug}/branches/{base}", self.env) or {}
            head = (branch.get("commit") or {}).get("sha") or ""
            self._fresh[key] = bool(head) and git(
                checkout, "merge-base", "--is-ancestor", head, f"origin/{base}",
                env=self.env).returncode == 0
        return self._fresh[key]

    def label(self, row: dict) -> tuple[str, str]:
        """The label and a sentence saying why, or `Skip`."""
        match = SUBJECT.match(row.get("question_id") or "")
        if not match:
            raise Skip("no subject")
        answer = row.get("answer") or ""
        if not POSITION.match(answer):
            raise Skip("no answer")
        owner, repo, sha = match.groups()
        slug = f"{owner}/{repo}"
        checkout = self.checkout(slug)
        # The full sha when the checkout has the commit, which it does when the
        # review ran in a worktree of it; GitHub reads the short one otherwise.
        resolved = git(checkout, "rev-parse", "--verify", "--quiet",
                       f"{sha}^{{commit}}", env=self.env)
        ref = resolved.stdout.strip() if resolved.returncode == 0 else sha
        pulls = gh_json(f"repos/{slug}/commits/{ref}/pulls", self.env) or []
        merged = [pull for pull in pulls if pull.get("merged_at")]
        if not merged:
            age = self.now - when(row["timestamp"])
            raise Skip("abandoned" if age > timedelta(days=ABANDONED_DAYS)
                       else "not merged")
        pull = merged[0]
        merged_at = when(pull["merged_at"])
        if self.now - merged_at < timedelta(days=WINDOW_DAYS):
            raise Skip("window open")
        base = (pull.get("base") or {}).get("ref") or "main"
        merge = pull.get("merge_commit_sha") or ""
        if not merge:
            raise Skip("no merge commit")
        if not self.fresh(slug, checkout, base):
            raise Skip("checkout behind")
        if git(checkout, "cat-file", "-e", f"{merge}^{{commit}}",
               env=self.env).returncode != 0:
            raise Skip("checkout behind")
        files = [item.get("filename") for item in gh_json(
            f"repos/{slug}/pulls/{pull['number']}/files", self.env,
            paginate=True) if item.get("filename")]
        if not files:
            raise Skip("no files")
        fixes = fixes_after(checkout, merge, base, merged_at, files, self.env)
        position = int(answer)
        if not fixes:
            return answer, f"right, label {answer} (answer {answer}; no fix in {WINDOW_DAYS} days)"
        first = f'{fixes[0][0][:7]} "{fixes[0][1]}"'
        if position >= self.deepest:
            return answer, (f"right, label {answer} (answer {answer} is the "
                            f"deepest tier; fix {first})")
        deeper = str(position + 1)
        return deeper, f"wrong, label {deeper} (answer {answer}; fix {first})"


RULES = {"sd-review": SdReview}


def unlabelled(env) -> list[dict]:
    done = run([*sd_db_command(env), "judgments", "unlabelled", "--stage", STAGE,
                "--prefix", PREFIX, "--json"], env)
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip() or "sd-db.sh judgments unlabelled failed")
    return json.loads(done.stdout)


def write(row_id: int, value: str, env) -> None:
    done = run([*sd_db_command(env), "judgments", "label", "--row", str(row_id),
                "--override", value, "--source", SOURCE], env)
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip() or f"sd-db.sh judgments label failed for row {row_id}")


def main(argv=None, out=None, env=None) -> int:
    out = sys.stdout if out is None else out
    env = dict(os.environ if env is None else env)
    parser = argparse.ArgumentParser(prog="jev label", add_help=True)
    parser.add_argument("rule", help="which labelling rule to run: " + ", ".join(RULES))
    parser.add_argument("--apply", action="store_true",
                        help="write the labels; without it nothing is written")
    parser.add_argument("--deepest", type=int, default=DEEPEST, metavar="N",
                        help=f"the deepest tier's position (default {DEEPEST})")
    parser.add_argument("--now", default=None, metavar="STAMP",
                        help="the moment to measure the windows from (default: now)")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.rule not in RULES:
        sys.stderr.write(f"jev label: no rule {args.rule!r}; the rules are "
                         f"{', '.join(RULES)}\n")
        return 1
    if args.deepest < 1:
        sys.stderr.write("jev label: --deepest must be 1 or more\n")
        return 1
    now = when(args.now) if args.now else datetime.now(timezone.utc)
    rule = RULES[args.rule](env, now, args.deepest)
    try:
        rows = unlabelled(env)
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        sys.stderr.write(f"jev label: could not read the ledger: {exc}\n")
        return 1
    labelled = skipped = 0
    for row in rows:
        head = f"row {row['id']} {row.get('question_id')}"
        try:
            value, why = rule.label(row)
        except Skip as skip:
            skipped += 1
            out.write(f"{head}: skip, {skip}\n")
            continue
        if args.apply:
            try:
                write(row["id"], value, env)
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                sys.stderr.write(f"jev label: {exc}\n")
                return 1
            out.write(f"{head}: {why}\n")
        else:
            out.write(f"{head}: would label; {why}\n")
        labelled += 1
    if args.apply:
        out.write(f"jev label {args.rule}: {labelled} labelled, {skipped} skipped\n")
    else:
        out.write(f"jev label {args.rule}: {labelled} to label, {skipped} skipped; "
                  f"dry run, --apply writes\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
