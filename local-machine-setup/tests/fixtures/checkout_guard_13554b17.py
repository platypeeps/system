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
  `cd` inside the command moves the working folder until its subshell ends,
  and `NAME=value`, alone or after `export` or `declare`, sets `$NAME` for
  later commands. A quoted or escaped operator is plain text. Fetch,
  worktree, branch and every read pass.

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
DECLARERS = {"export", "readonly", "declare", "local", "typeset"}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
VARIABLE = re.compile(r"\$(?:\{(\w+)\}|(\w+))")
OPERATOR = set("();<>|&")
HIDDEN = 0xE000  # a quoted operator character moves to the private-use plane
UNHIDE = {HIDDEN + ord(char): char for char in OPERATOR}


def roots():
    pack = os.environ.get("SD_PACK_ROOT") or os.path.expanduser(PACK)
    found = []
    for root in (os.path.dirname(HERE), pack):
        root = os.path.realpath(root)
        if os.path.isdir(os.path.join(root, ".git")) and root not in found:
            found.append(root)
    return found


def expand(text, variables):
    return VARIABLE.sub(lambda m: variables.get(m.group(1) or m.group(2), m.group(0)), text)


def resolve(path, cwd, variables=os.environ):
    return os.path.realpath(os.path.join(cwd, os.path.expanduser(expand(path, variables))))


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


def written(path, cwd, protected, variables=os.environ):
    """The root a write to PATH lands in, or None."""
    full = resolve(path, cwd, variables)
    root = inside(full, protected)
    if root is None or ignored(root, full):
        return None
    return root


def shield(command):
    """Hide quoted and escaped operators from the lexer; drop comments.

    An unquoted newline ends a command, and a `#` that starts a word starts a
    comment to the end of its line, as the shell reads them.
    """
    out, quote, i = [], None, 0
    while i < len(command):
        char = command[i]
        if char == "\\" and quote != "'" and i + 1 < len(command):
            if quote is None and command[i + 1] == "\n":
                i += 2
                continue
            out += [char, hide(command[i + 1])]
            i += 2
            continue
        if quote:
            quote = None if char == quote else quote
            out.append(hide(char))
        elif char in "'\"":
            quote = char
            out.append(char)
        elif char == "#" and (not out or out[-1].isspace() or out[-1] in ";&|("):
            end = command.find("\n", i)
            i = len(command) if end < 0 else end
            continue
        else:
            out.append(";" if char == "\n" else char)
        i += 1
    return "".join(out)


def hide(char):
    return chr(HIDDEN + ord(char)) if char in OPERATOR else char


def is_operator(token):
    return bool(token) and set(token) <= OPERATOR


def commands(command):
    """Each simple command as (words, redirect targets, the operator after it).

    A `(` or `)` is an operator of its own, so a caller can open and close a
    subshell; a quoted operator stays a word.
    """
    lexer = shlex.shlex(shield(command), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens = []
    for token in lexer:
        tokens += re.findall(r"[()]|[^()]+", token) if is_operator(token) else [token]
    words, targets, i = [], [], 0
    while i < len(tokens):
        token = tokens[i]
        if is_operator(token) and ("<" in token or ">" in token):
            target = tokens[i + 1] if i + 1 < len(tokens) else ""
            if is_operator(target):
                target = ""
            else:
                i += 1
            if ">" in token:
                if words and words[-1].isdigit():
                    words.pop()
                if target and not (token.endswith("&") and (target.isdigit() or target == "-")):
                    targets.append(target.translate(UNHIDE))
        elif is_operator(token):
            yield words, targets, token
            words, targets = [], []
        else:
            words.append(token.translate(UNHIDE))
        i += 1
    yield words, targets, ""


def unwrap(words):
    while words and (ASSIGNMENT.match(words[0]) or words[0] in WRAPPERS):
        words = words[1:]
    return words


def git_target(words, cwd, variables):
    """(folder, verb) for a git command; -C folders stack as git stacks them."""
    i = 1
    while i < len(words) and words[i].startswith("-"):
        if words[i] == "-C" and i + 1 < len(words):
            cwd = resolve(words[i + 1], cwd, variables)
            i += 2
        elif words[i] == "-c" and i + 1 < len(words):
            i += 2
        else:
            i += 1
    return cwd, words[i] if i < len(words) else ""


def assign(words, variables):
    """Set the variables a bare `NAME=value`, `export` or `declare` sets."""
    if words[:1] and words[0] in DECLARERS:
        words = words[1:]
    elif not all(ASSIGNMENT.match(word) for word in words):
        return
    for word in words:
        match = ASSIGNMENT.match(word)
        if match:
            value = word[match.end():]
            variables[match.group()[:-1]] = os.path.expanduser(expand(value, variables))


def bash_root(command, cwd, protected):
    variables, outer = dict(os.environ), []
    for words, targets, operator in commands(command):
        for target in targets:
            root = written(target, cwd, protected, variables)
            if root:
                return root
        assign(words, variables)
        words = unwrap(words)
        name = os.path.basename(words[0]) if words else ""
        args = [word for word in words[1:] if not word.startswith("-")]
        if name == "cd":
            cwd = resolve(args[0] if args else "~", cwd, variables)
        elif name == "git":
            folder, verb = git_target(words, cwd, variables)
            root = inside(folder, protected) if verb in GIT_WRITES else None
            if root:
                return root
        elif name in PATH_WRITERS or (name in LAST_ARG_WRITERS and args):
            for path in args if name in PATH_WRITERS else args[-1:]:
                root = written(path, cwd, protected, variables)
                if root:
                    return root
        if operator == "(":
            outer.append((cwd, dict(variables)))
        elif operator == ")" and outer:
            cwd, variables = outer.pop()
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
