"""A GitHub remote made of a real bare repository and a table of answers.

The state is deliberately split from the front doors. `FixtureRemote` holds
what the remote knows -- the bare repository, its default branch, the branch
protection, the pull requests, the collaborators and each pull request's
checks -- and answers questions about it. `sd_db.testing.github` puts an HTTP
door and a `gh` door onto the same object, so a call arriving either way reads
and writes one truth and is recorded once.

The bare repository is real. A double that answers `merge` without moving a
ref lets a test pass while the branch it claims to have merged is untouched,
and the tests that matter here -- a merge refused because the head moved, a
default branch that must not be pushed to -- are exactly the ones a mocked
`git` would agree with wrongly.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class RemoteRefusal(Exception):
    """The remote refused, the way GitHub refuses: a status and a message.

    Carried rather than returned so a caller that ignores the result cannot
    treat a refusal as a merge. The HTTP door turns it back into a status
    code; the `gh` door turns it into a non-zero exit and a message on stderr.
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Call:
    """One question the remote was asked, in the order it was asked."""

    door: str
    method: str
    path: str
    body: dict | None = None
    status: int = 200


@dataclass
class PullRequest:
    number: int
    head: str
    base: str
    title: str = ""
    body: str = ""
    state: str = "OPEN"
    draft: bool = False
    mergeable: str = "MERGEABLE"
    merge_state_status: str = "CLEAN"
    review_decision: str = ""
    merge_commit_sha: str | None = None
    checks: list[dict] = field(default_factory=list)

    def head_sha(self, remote: FixtureRemote) -> str:
        return remote.rev_parse(self.head)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {repo}: {result.stderr.strip()}")
    return result.stdout.strip()


class FixtureRemote:
    """A bare repository with the state GitHub keeps beside one.

    `slug` is `owner/name`, the form every call the pack makes is addressed
    by. Nothing here reaches the network and nothing reads the operator's
    real git configuration: the bare repository is created with an explicit
    initial branch and every commit is made with fixture identity.
    """

    def __init__(self, root: Path, slug: str = "fixture/repo", default_branch: str = "main") -> None:
        self.root = Path(root)
        self.slug = slug
        self.default_branch = default_branch
        self.path = self.root / "remote.git"
        self.calls: list[Call] = []
        self.pull_requests: dict[int, PullRequest] = {}
        self.collaborators: list[dict] = []
        self.protection: dict | None = None
        self.delete_branch_on_merge = False
        self._next_number = 1
        self._make_bare()

    # ---------------------------------------------------------------- git

    def _make_bare(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        _git(self.path, "init", "--bare", f"--initial-branch={self.default_branch}", ".")
        seed = self.root / "seed"
        seed.mkdir(exist_ok=True)
        _git(seed, "init", f"--initial-branch={self.default_branch}", ".")
        _git(seed, "config", "user.email", "fixture@example.invalid")
        _git(seed, "config", "user.name", "Fixture")
        (seed / "README.md").write_text("fixture\n", encoding="utf-8")
        _git(seed, "add", "README.md")
        _git(seed, "commit", "-m", "seed")
        _git(seed, "remote", "add", "origin", str(self.path))
        _git(seed, "push", "-q", "origin", self.default_branch)
        self.seed = seed

    def rev_parse(self, ref: str) -> str:
        return _git(self.path, "rev-parse", ref)

    def branches(self) -> list[str]:
        listing = _git(self.path, "for-each-ref", "--format=%(refname:short)", "refs/heads")
        return [line for line in listing.split("\n") if line]

    def commit_on(self, branch: str, message: str, *, files: dict[str, str] | None = None) -> str:
        """Add one commit to `branch` and push it. Returns the new sha."""
        work = self.seed
        existing = self.branches()
        if branch in existing:
            _git(work, "fetch", "-q", "origin", branch)
            _git(work, "checkout", "-q", "-B", branch, "FETCH_HEAD")
        else:
            _git(work, "checkout", "-q", "-B", branch, self.default_branch)
        for name, text in (files or {"note.txt": message}).items():
            target = work / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            _git(work, "add", name)
        _git(work, "commit", "-m", message)
        _git(work, "push", "-q", "origin", branch)
        return _git(work, "rev-parse", "HEAD")

    # ------------------------------------------------------- the answers

    def record(self, door: str, method: str, path: str, body: dict | None = None, status: int = 200) -> None:
        self.calls.append(Call(door=door, method=method, path=path, body=body, status=status))

    def open_pull_request(self, head: str, *, base: str | None = None, title: str = "", body: str = "") -> PullRequest:
        pull = PullRequest(
            number=self._next_number,
            head=head,
            base=base or self.default_branch,
            title=title,
            body=body,
        )
        self._next_number += 1
        self.pull_requests[pull.number] = pull
        return pull

    def pull(self, number: int) -> PullRequest:
        try:
            return self.pull_requests[number]
        except KeyError:
            raise RemoteRefusal(404, f"no pull request {number}") from None

    def behind_by(self, base: str, head: str) -> int:
        counts = _git(self.path, "rev-list", "--left-right", "--count", f"{base}...{head}")
        behind, _ahead = counts.split()
        return int(behind)

    def merge(self, number: int, *, sha: str | None = None, method: str = "squash") -> str:
        """Merge, the way `PUT /repos/{slug}/pulls/{n}/merge` merges.

        `sha` names the head the caller reviewed. GitHub answers 405 when it
        is not the pull request's current head, which is the whole point of
        passing it: a head that moved after the review is refused rather than
        merged. The refusal is recorded like any other call.
        """
        pull = self.pull(number)
        if pull.state != "OPEN":
            raise RemoteRefusal(405, f"pull request {number} is {pull.state.lower()}")
        head_sha = pull.head_sha(self)
        if sha is not None and sha != head_sha:
            raise RemoteRefusal(
                405,
                f"Head branch was modified. Review and try the merge again. "
                f"(expected {sha}, head is {head_sha})",
            )
        if pull.mergeable != "MERGEABLE":
            raise RemoteRefusal(405, f"pull request {number} is not mergeable")
        work = self.seed
        _git(work, "fetch", "-q", "origin", pull.base)
        _git(work, "checkout", "-q", "-B", pull.base, "FETCH_HEAD")
        _git(work, "fetch", "-q", "origin", pull.head)
        _git(work, "merge", "-q", "--squash", "FETCH_HEAD")
        _git(work, "commit", "-m", pull.title or f"{pull.head} (#{number})")
        _git(work, "push", "-q", "origin", pull.base)
        pull.merge_commit_sha = _git(work, "rev-parse", "HEAD")
        pull.state = "MERGED"
        if self.delete_branch_on_merge:
            _git(self.path, "update-ref", "-d", f"refs/heads/{pull.head}")
        return pull.merge_commit_sha

    def can_administer(self, login: str) -> bool:
        for entry in self.collaborators:
            if entry.get("login") == login:
                return bool(entry.get("permissions", {}).get("admin"))
        return False

    def other_pushers(self, login: str) -> list[str]:
        """Everyone but `login` who can push. The collaborator question."""
        return [
            entry["login"]
            for entry in self.collaborators
            if entry.get("login") != login
            and (entry.get("permissions", {}).get("push") or entry.get("permissions", {}).get("admin"))
        ]
