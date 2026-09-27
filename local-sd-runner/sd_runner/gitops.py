"""Every mutation is in an independent clone, including local-only publication."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from pathlib import Path

paths = None  # bound at the end of this module (sd:1439)
from sd_db.runner import RunnerRefused

#: The pack's untracked per-checkout block, which holds a repository's check overrides.
LOCAL_OVERRIDES = "CLAUDE.local.md"


def git(root: Path, *args: str, check=True) -> str:
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=120, check=False)
    if check and done.returncode:
        raise RunnerRefused(f"git {' '.join(args[:2])}: {done.stderr.strip()}")
    return done.stdout.strip() if done.returncode == 0 else ""


def head(root: Path, ref: str) -> str:
    return git(root, "rev-parse", "--verify", ref, check=False)


def tree(root: Path, ref: str = "HEAD") -> str:
    """The tree a commit points at: what a check ran against, whatever the commit's message or parents."""
    return git(root, "rev-parse", "--verify", f"{ref}^{{tree}}")


def ancestor(root: Path, old: str, new: str) -> bool:
    return subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", old, new], capture_output=True, check=False).returncode == 0


def remote_head(root: Path, branch: str) -> str:
    result = git(root, "ls-remote", "--heads", "origin", "refs/heads/" + branch)
    return result.split()[0] if result else ""


def default_branch(root: Path) -> str:
    result = git(root, "ls-remote", "--symref", "origin", "HEAD")
    for line in result.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "ref:" and parts[2] == "HEAD" and parts[1].startswith("refs/heads/"):
            return parts[1][11:]
    raise RunnerRefused("remote HEAD does not name a default branch")


def clone(request: dict) -> dict:
    run, item = request["run"], request["item_record"]
    target, checkout = Path(run["work_path"]), paths.disk(run["repo"])
    if target.exists():
        raise RunnerRefused(f"fresh clone path already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(["git", "clone", "--no-checkout", "--reference", str(checkout), "--dissociate", "--", item["remote"], str(target)],
                          capture_output=True, text=True, timeout=300, check=False)
    if done.returncode:
        raise RunnerRefused(f"clone failed; partial clone retained: {done.stderr.strip()}")
    if (target / ".git/objects/info/alternates").exists():
        raise RunnerRefused("clone still depends on another object store")
    for key in ("user.name", "user.email", "commit.gpgsign", "user.signingkey"):
        value = git(checkout, "config", "--get", key, check=False)
        if value:
            git(target, "config", key, value)
    forward_hooks(checkout, target, parallel=request["lane"] == "parallel", branch=run["branch"])
    return {"cloned": True}


def branch(request: dict) -> dict:
    run = request["run"]
    root, checkout, name = Path(run["work_path"]), paths.disk(run["repo"]), run["branch"]
    git(root, "check-ref-format", "--branch", name)
    default = default_branch(root)
    if name == default:
        raise RunnerRefused("author work must use an item branch, not the remote default branch")
    local = head(checkout, "refs/heads/" + name)
    remote = remote_head(root, name)
    binding = json.loads(request["item_record"]["fields"] or "{}").get("runner_branch")
    if binding and request["scope"] != "reconcile":
        seed = binding.get("source_commit")
        if binding.get("remote_absent") is not True or binding.get("source_branch") != default:
            raise RunnerRefused("new branch source binding no longer matches the remote default")
        if local or remote:
            raise RunnerRefused(f"new branch {name} appeared after preparation; refresh before running")
        if remote_head(root, default) != seed:
            raise RunnerRefused("new branch base changed after preparation; refresh before running")
        git(root, "fetch", "--no-tags", "origin", f"refs/heads/{default}:refs/remotes/origin/{default}")
        if head(root, "refs/remotes/origin/" + default) != seed:
            raise RunnerRefused("new branch base changed during fetch; refresh before running")
        local = seed
    if request["scope"] == "reconcile":
        # A squash merge can delete the source branch. Reconciliation proves
        # GitHub's merge commit on default and must never recreate that branch.
        git(root, "fetch", "--no-tags", "origin", f"refs/heads/{default}:refs/remotes/origin/{default}")
        chosen = head(root, "refs/remotes/origin/" + default)
        git(root, "checkout", "-b", name, chosen)
        return {"base_head": chosen, "default": default, "local_overrides": carry_local_overrides(checkout, root)}
    if not local and not remote:
        seed = request["item_record"].get("source_commit")
        if request["scope"] not in {"skill-review", "skill-apply"} or not seed or head(checkout, seed + "^{commit}") != seed:
            raise RunnerRefused(f"branch {name} exists in neither checkout nor remote")
        git(root, "fetch", "--no-tags", str(checkout), seed)
        local = seed
    if local and head(checkout, "refs/heads/" + name):
        git(root, "fetch", "--no-tags", str(checkout), f"refs/heads/{name}:refs/sd/source")
    if remote:
        git(root, "fetch", "--no-tags", "origin", f"refs/heads/{name}:refs/remotes/origin/{name}")
    if local and remote and not ancestor(root, local, remote) and not ancestor(root, remote, local):
        raise RunnerRefused(f"branch {name} diverged: checkout={local}, remote={remote}")
    chosen = local if local and (not remote or ancestor(root, remote, local)) else remote
    git(root, "checkout", "-b", name, chosen)
    if chosen != remote and request["role"] != "reviewer":
        git(root, "push", f"--force-with-lease=refs/heads/{name}:{remote}", "origin", f"HEAD:refs/heads/{name}")
    # Do not set upstream in the user's checkout; the clone owns all writes.
    git(root, "config", f"branch.{name}.remote", "origin")
    git(root, "config", f"branch.{name}.merge", "refs/heads/" + name)
    return {"base_head": chosen, "default": default, "prepared_branch": binding,
            "local_overrides": carry_local_overrides(checkout, root)}


def carry_local_overrides(checkout: Path, clone_path: Path) -> str | None:
    """Copy the checkout's untracked `CLAUDE.local.md` into the clone; the source copied, or None (sd:1752).

    The pack's `sd-check` reads a repository's test and lint overrides from
    that file's block before any Makefile or package.json, and a clone never
    has it: it is untracked. Three fleet rows failed on the clone's defaults.
    The clone's `info/exclude` names the copy, so it is never committed and
    the clone does not read as dirty. A file the branch tracks is its own, and
    a checkout without one leaves the clone as it was.
    """
    source, target = Path(checkout) / LOCAL_OVERRIDES, Path(clone_path) / LOCAL_OVERRIDES
    if not source.is_file() or target.exists() or target.is_symlink():
        return None
    exclude = Path(git(clone_path, "rev-parse", "--git-path", "info/exclude"))
    if not exclude.is_absolute():
        exclude = Path(clone_path) / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text() if exclude.exists() else ""
    if "/" + LOCAL_OVERRIDES not in text.splitlines():
        exclude.write_text(text + ("\n" if text and not text.endswith("\n") else "") + "/" + LOCAL_OVERRIDES + "\n")
    shutil.copyfile(source, target)
    return str(source)


def merge_default(request: dict) -> dict:
    root = Path(request["run"]["work_path"])
    default = default_branch(root)
    git(root, "fetch", "--no-tags", "origin", f"refs/heads/{default}:refs/remotes/origin/{default}")
    try:
        git(root, "merge", "--no-edit", "refs/remotes/origin/" + default)
    except RunnerRefused as error:
        conflicts = git(root, "diff", "--name-only", "--diff-filter=U")
        raise RunnerRefused(f"default-branch merge blocked: {conflicts or error}") from None
    return {"base_head": head(root, "HEAD")}


def forward_hooks(checkout: Path, clone_path: Path, *, parallel: bool, branch: str) -> None:
    effective = git(checkout, "rev-parse", "--git-path", "hooks")
    source = Path(effective)
    if not source.is_absolute():
        source = checkout / source
    try:
        relative = source.resolve().relative_to(checkout.resolve())
    except ValueError:
        target = source  # Absolute external hook paths retain their intended layout.
    else:
        target = clone_path / relative
        if source.exists():
            shutil.copytree(source, target, dirs_exist_ok=True, copy_function=shutil.copy2,
                            ignore=shutil.ignore_patterns("*.sample"), symlinks=True)
    git(clone_path, "config", "sd.hooksForward", str(target))
    if not parallel:
        git(clone_path, "config", "core.hooksPath", str(target))
        return
    guard = clone_path / ".git/sd-hooks"
    guard.mkdir()
    names = {"pre-push", "pre-commit", "commit-msg", "prepare-commit-msg", "post-commit", "post-checkout",
             "post-merge", "pre-rebase", "post-rewrite", "pre-applypatch", "applypatch-msg", "post-applypatch",
             "pre-auto-gc", "post-index-change", "reference-transaction", "push-to-checkout", "sendemail-validate"}
    if target.is_dir():
        names.update(path.name for path in target.iterdir() if path.is_file() and not path.name.endswith(".sample"))
    for name in names:
        forward = shlex.quote(str(target / name))
        body = "#!/bin/sh\nset -e\n"
        if name == "pre-push":
            body += f"branch={shlex.quote('refs/heads/' + branch)}\n"
            body += """input=$(mktemp "${TMPDIR:-/tmp}/sd-push.XXXXXX")
trap 'rm -f "$input"' EXIT HUP INT TERM
cat > "$input"
while read -r local_ref local_sha remote_ref remote_sha; do
  [ "$remote_ref" = "$branch" ] || { echo 'runner: push outside leased branch refused' >&2; exit 1; }
  [ "$local_sha" != 0000000000000000000000000000000000000000 ] || exit 1
  if [ "$remote_sha" != 0000000000000000000000000000000000000000 ]; then
    git merge-base --is-ancestor "$remote_sha" "$local_sha" || { echo 'runner: non-fast-forward push refused' >&2; exit 1; }
  fi
done < "$input"
"""
            body += f"if [ -x {forward} ]; then {forward} \"$@\" < \"$input\"; fi\n"
        else:
            body += f"if [ -x {forward} ]; then exec {forward} \"$@\"; fi\n"
        (guard / name).write_text(body)
        (guard / name).chmod(0o755)
    git(clone_path, "config", "core.hooksPath", str(guard))


def durable(run: dict) -> str:
    root, branch_name = Path(run["work_path"]), run["branch"]
    current = head(root, "HEAD")
    if not current:
        raise RunnerRefused("branch is absent; retain the partial setup without a Git durability probe")
    remote = remote_head(root, branch_name)
    if remote:
        git(root, "fetch", "--no-tags", "origin", f"refs/heads/{branch_name}")
        if ancestor(root, current, remote):
            return current
        if not ancestor(root, remote, current):
            raise RunnerRefused(f"remote diverged during run: {remote}; local={current}")
    git(root, "push", f"--force-with-lease=refs/heads/{branch_name}:{remote}", "origin", f"HEAD:refs/heads/{branch_name}")
    if remote_head(root, branch_name) != current:
        raise RunnerRefused("push readback does not match authored head")
    return current


def dirty(root: Path) -> bool:
    return bool(git(root, "status", "--porcelain=v1", "--untracked-files=all"))


def check_attribution(run: dict, assignment_base: str | None = None) -> str:
    """The authored head, once every commit the assignment authored carries the provider's trailer.

    A later attempt of the same assignment can find its work already
    committed: an earlier attempt pushed it, then timed out (sd:1802). With
    no commit of its own, the attempt is judged on the commits the branch
    carries beyond `assignment_base`, the first attempt's base, less the
    default branch its merge step brought in. Nothing carried still refuses.
    """
    root = Path(run["work_path"])
    current = head(root, "HEAD")
    expected = f"Authored-with: {run['provider']}/{run['vendor']}"
    commits = git(root, "rev-list", "--no-merges", f"{run['base_head']}..{current}").splitlines()
    if not commits and assignment_base and assignment_base != run["base_head"] and ancestor(root, assignment_base, current):
        default = "refs/remotes/origin/" + default_branch(root)
        commits = git(root, "rev-list", "--no-merges", current, "^" + assignment_base, "^" + default).splitlines()
    if not commits:
        raise RunnerRefused("provider exited successfully but authored no commits")
    for commit in commits:
        trailers = git(root, "show", "-s", "--format=%(trailers:only)", commit)
        if expected not in trailers.splitlines():
            raise RunnerRefused(f"commit {commit} lacks actual-provider attribution {expected}")
    return current


# A library before schema 14 (sd:1439) has no `sd_db.paths` and stores every
# repository path absolute. The services run from this checkout, so a pull can
# reach them before the library moves; against that library a key is the path.
try:
    from sd_db import paths
except ImportError:
    from types import SimpleNamespace as _Namespace
    paths = _Namespace(
        key=lambda value: value,
        disk=lambda value: Path(value).expanduser(),
        same=lambda left, right: left is not None and right is not None and (
            Path(left).expanduser().resolve() == Path(right).expanduser().resolve()),
    )
