"""PreToolUse hook: agents do not write in the system and pack checkouts (sd:6001).

The lane and repo-sync own those two checkouts and keep them on a pinned
detached HEAD; agents write in a worktree under ~/worktrees. An untracked
docs/work folder an agent left in the system checkout blocked a repo-sync
refresh (2026-10-23). The lane and repo-sync run as programs, not agent tool
calls, so this hook never sees them.

The protected roots are the checkout this file sits in and the pack checkout
(SD_PACK_ROOT, else ~/repos/platypeeps/sd-ai-command-pack), each only while
its `.git` is a folder: a linked worktree, whose `.git` is a file, is where
agents write, so a copy of this file run from one protects nothing there.

- Write, Edit, MultiEdit and NotebookEdit on a path inside a root: denied,
  unless git ignores the path (docs/dashboard/ takes generated HTML).
- Bash: a git verb that moves the work tree or HEAD, aimed at a root by
  `git -C` or the working folder; and mkdir, touch, rm, rmdir, mv, tee,
  truncate on a path inside a root, the last argument of cp, ln, install
  and rsync, and a `>` or `>>` target, each judged like a file tool's path.
  `cd` inside the command moves the working folder. Fetch, worktree, branch
  and every read pass.

Malformed input or a command shlex cannot split fails open: no decision, a
warning on stderr, exit 0. A broken hook must not stop every session.
"""

import json
import os
import re
import shlex
import subprocess
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
PACK = "~/repos/platypeeps/sd-ai-command-pack"

FILE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path",
              "NotebookEdit": "notebook_path"}
GIT_WRITES = {"add", "am", "apply", "checkout", "cherry-pick", "clean", "commit", "merge", "mv",
              "pull", "rebase", "reset", "restore", "revert", "rm", "stash", "switch"}
PATH_WRITERS = {"mkdir", "touch", "rm", "rmdir", "mv", "tee", "truncate"}
LAST_ARG_WRITERS = {"cp", "ln", "install", "rsync"}
WRAPPERS = {"rtk", "proxy", "command", "nohup", "time", "env", "exec"}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
OPERATOR = set("();<>|&")


def roots():
    pack = os.environ.get("SD_PACK_ROOT") or os.path.expanduser(PACK)
    found = []
    for root in (os.path.dirname(HERE), pack):
        root = os.path.realpath(root)
        if os.path.isdir(os.path.join(root, ".git")) and root not in found:
            found.append(root)
    return found


def resolve(path, cwd):
    return os.path.realpath(os.path.join(cwd, os.path.expanduser(os.path.expandvars(path))))


def inside(full, protected):
    for root in protected:
        if full == root or full.startswith(root + os.sep):
            return root
    return None


def ignored(root, full):
    """True only when git says it ignores the path; any failure is not ignored."""
    try:
        result = subprocess.run(["git", "-C", root, "check-ignore", "-q", "--",
                                 os.path.relpath(full, root)],
                                capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def written(path, cwd, protected):
    """The root a write to PATH lands in, or None."""
    full = resolve(path, cwd)
    root = inside(full, protected)
    if root is None or ignored(root, full):
        return None
    return root


def commands(command):
    """Each simple command as (words, redirect targets)."""
    lexer = shlex.shlex(command.replace("\n", ";"), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    tokens = list(lexer)
    words, targets, i = [], [], 0
    while i < len(tokens):
        token = tokens[i]
        if token and set(token) <= OPERATOR:
            if ">" in token:
                target = tokens[i + 1] if i + 1 < len(tokens) else ""
                if words and words[-1].isdigit():
                    words.pop()
                if not (token.endswith("&") and (target.isdigit() or target == "-")):
                    targets.append(target)
                i += 2
                continue
            if "<" in token:
                i += 2
                continue
            yield words, targets
            words, targets = [], []
        else:
            words.append(token)
        i += 1
    yield words, targets


def unwrap(words):
    while words and (ASSIGNMENT.match(words[0]) or words[0] in WRAPPERS):
        words = words[1:]
    return words


def git_target(words, cwd):
    """(folder, verb) for a git command; -C folders stack as git stacks them."""
    i = 1
    while i < len(words) and words[i].startswith("-"):
        if words[i] == "-C" and i + 1 < len(words):
            cwd = resolve(words[i + 1], cwd)
            i += 2
        elif words[i] == "-c" and i + 1 < len(words):
            i += 2
        else:
            i += 1
    return cwd, words[i] if i < len(words) else ""


def bash_root(command, cwd, protected):
    for words, targets in commands(command):
        for target in targets:
            root = written(target, cwd, protected)
            if root:
                return root
        words = unwrap(words)
        if not words:
            continue
        name = os.path.basename(words[0])
        args = [word for word in words[1:] if not word.startswith("-")]
        if name == "cd":
            cwd = resolve(args[0] if args else "~", cwd)
        elif name == "git":
            folder, verb = git_target(words, cwd)
            root = inside(folder, protected) if verb in GIT_WRITES else None
            if root:
                return root
        elif name in PATH_WRITERS or (name in LAST_ARG_WRITERS and args):
            for path in args if name in PATH_WRITERS else args[-1:]:
                root = written(path, cwd, protected)
                if root:
                    return root
    return None


def denied_root(payload):
    """The protected checkout this call writes in, or None to let it pass."""
    protected = roots()
    if not protected:
        return None
    name = payload["tool_name"]
    tool_input = payload["tool_input"]
    cwd = payload.get("cwd") or os.getcwd()
    if name in FILE_TOOLS:
        return written(tool_input[FILE_TOOLS[name]], cwd, protected)
    if name == "Bash":
        return bash_root(tool_input["command"], cwd, protected)
    return None


def main():
    try:
        root = denied_root(json.load(sys.stdin))
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        print(f"checkout_guard: unreadable input, allowing: {error!r}", file=sys.stderr)
        return 0
    if root is not None:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"{root} is a lane checkout: the lane and repo-sync own it, and agents do "
                "not write there (sd:6001). Work in a worktree: git -C "
                f"{root} worktree add ~/worktrees/<repo>-<branch> -b <branch> origin/main"),
        }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
