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
- A change that may not happen keeps the old state beside the new one: a
  command after `&&` or `||`, in an `if`, loop or `case` body, a pipeline
  stage, a `&` job, and a `cd` to a folder that does not exist yet. The
  reader carries every (folder, variables, status) world and denies a write
  in any of them (sd:6083).

Malformed input or a command shlex cannot split fails open: no decision, a
warning on stderr, exit 0. A broken hook must not stop every session. A
command whose worlds pass BUDGET is denied: its writes are unknown, and a
shorter command reads.
"""

import collections
import functools
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
PREFIXES = {"command", "builtin", "time"}  # run the command in this shell
FORKS = {"rtk", "proxy", "nohup", "env", "exec", "nice", "timeout", "xargs", "sudo", "stdbuf"}
VALUED = {"env": {"-u", "-P", "-S", "-C"}, "nice": {"-n"}, "timeout": {"-s", "-k"},
          "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-U", "-r", "-t"},
          "xargs": {"-n", "-I", "-J", "-L", "-P", "-R", "-S", "-s", "-E", "-d", "-a"},
          "stdbuf": {"-i", "-o", "-e"}}  # options that take the next word
DECLARERS = {"export", "readonly", "declare", "local", "typeset"}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
VARIABLE = re.compile(r"\$(?:\{(\w+)\}|(\w+))")
OPERATOR = set("();<>|&")
HIDDEN = 0xE000  # a quoted operator character moves to the private-use plane
UNHIDE = {HIDDEN + ord(char): char for char in OPERATOR}
LITERAL = {HIDDEN + ord(char): char for char in "$~"}  # a quoted `$` or `~` expands to nothing
CONTROL = re.compile(r"&>>|&>|>>|>&|>\||<<<|<<-|<<|<&|<>|[<>]|;;&|;;|;&|&&|\|\||\|&|[;&|()]")
KEYWORDS = {"if", "then", "elif", "else", "fi", "while", "until", "for", "select", "do", "done",
            "case", "esac", "{", "}", "!"}
BUDGET = 1024  # commands times worlds the reader may run; more reads as unsplittable


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
    path = os.path.expanduser(expand(path, variables)).translate(LITERAL)
    return os.path.realpath(os.path.join(cwd, path))


def inside(full, protected):
    for root in protected:
        if full == root or full.startswith(root + os.sep):
            return root
    return None


@functools.lru_cache(maxsize=None)
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

    A `$` in single quotes, after a backslash or before a quote (`$'..'`),
    and a quoted or escaped `~`, hide too: the shell expands neither.
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
            out += [char, hide(command[i + 1], "$~")]
            i += 2
            continue
        if quote:
            quote = None if char == quote else quote
            out.append(hide(char, "$~" if quote == "'" else "~"))
        elif char == "$" and command[i + 1:i + 2] in ("'", '"'):
            out.append(hide(char, "$"))
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


def hide(char, literal=""):
    return chr(HIDDEN + ord(char)) if char in OPERATOR or char in literal else char


def is_operator(token):
    return bool(token) and set(token) <= OPERATOR


def items(command):
    """The command as ("cmd", words, redirect targets) and ("op", operator) items.

    A quoted operator stays a word; a run such as `&&;` (`&&` then a newline)
    splits into the shell's operators.
    """
    lexer = shlex.shlex(shield(command), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens = []
    for token in lexer:
        tokens += CONTROL.findall(token) if is_operator(token) else [token]
    out, words, targets, i = [], [], [], 0
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
            if words or targets:
                out.append(("cmd", words, targets))
            out.append(("op", token))
            words, targets = [], []
        else:
            words.append(token.translate(UNHIDE))
        i += 1
    if words or targets:
        out.append(("cmd", words, targets))
    return out


def unwrap(words):
    """(the command, forked): FORKED when a wrapper runs it in a child process.

    A child's cd, exit, assignment or loop jump leaves this shell as it was.
    """
    forked = False
    while words:
        wrapper = os.path.basename(words[0])
        if ASSIGNMENT.match(words[0]):
            words = words[1:]
            continue
        if wrapper not in PREFIXES and wrapper not in FORKS:
            break
        forked, words = forked or wrapper in FORKS, words[1:]
        while words and words[0].startswith("-") and words[0] != "--":
            if wrapper == "command" and words[0] in ("-v", "-V"):
                return [], forked  # prints what would run; runs nothing
            words = words[2:] if words[0] in VALUED.get(wrapper, ()) else words[1:]
        words = words[1 if words[:1] == ["--"] else 0:]
        words = words[1:] if wrapper == "timeout" else words  # the duration
    return words, forked


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


def assigned(words, variables):
    """The variables a bare `NAME=value`, `export` or `declare` sets."""
    if words[:1] and words[0] in DECLARERS:
        words = words[1:]
    elif not all(ASSIGNMENT.match(word) for word in words):
        return {}
    variables, changes = dict(variables), {}
    for word in words:
        match = ASSIGNMENT.match(word)
        if match:
            name = match.group()[:-1]
            variables[name] = changes[name] = os.path.expanduser(expand(word[match.end():],
                                                                        variables))
    return changes


class World(collections.namedtuple("World", "cwd changes ok")):
    """One state the shell may be in: folder, variables set so far, last status."""

    def variables(self, inherited):
        return {**inherited, **dict(self.changes)}

    def set(self, changes):
        return self._replace(changes=tuple(sorted({**dict(self.changes), **changes}.items())))


class Denied(Exception):
    pass


class TooMany(Exception):
    pass


def passed(worlds, ok):
    return {world for world in worlds if world.ok == ok}


def statuses(worlds):
    """The exit statuses WORLDS end with; none left (all exited) reads as either."""
    return {world.ok for world in worlds} or {True, False}


def with_status(worlds, oks):
    return {world._replace(ok=ok) for world in worlds for ok in oks}


class Reader:
    """Reads the command once, carrying every world the shell may be in.

    A command that may not run (after `&&`, `||`, `if`, `while`, `for`, `case`)
    leaves its world beside the one it changes; a pipeline stage and a `&` job
    run in a subshell, so their changes may not last; a `cd` to a folder that
    does not exist yet may fail. A write in any world is a write.
    """

    def __init__(self, command, protected):
        self.items, self.protected, self.i, self.skip = items(command), protected, 0, 0
        self.budget, self.inherited, self.loops = BUDGET, dict(os.environ), []

    def peek(self):
        if self.i >= len(self.items):
            return ("end",)
        item = self.items[self.i]
        return ("cmd", item[1][self.skip:], item[2]) if item[0] == "cmd" else item

    def advance(self):
        self.i, self.skip = self.i + 1, 0

    def op(self, *ops):
        item = self.peek()
        if item[0] == "op" and item[1] in ops:
            self.advance()
            return item[1]
        return None

    def newlines(self):
        while self.op(";"):
            pass

    def keyword(self, *names):
        """Consume a reserved word at the start of the next command."""
        item = self.peek()
        if item[0] != "cmd" or not item[1] or item[1][0] not in names:
            return None
        self.skip += 1
        if len(item[1]) == 1 and not item[2]:
            self.advance()
        return item[1][0]

    def at(self, stops):
        item = self.peek()
        return (item[0] == "op" and item[1] in stops) or (
            item[0] == "cmd" and bool(item[1]) and item[1][0] in stops)

    def text(self, worlds):
        """Read past a word list (case word, patterns, for words), running each `$( )`."""
        words = []
        while self.peek()[0] == "cmd":
            words += self.peek()[1]
            self.advance()
            if not (words and words[-1].endswith("$") and self.op("(")):
                break
            self.run(worlds, (")",))
            self.op(")")
        return words

    def check(self, path, world):
        root = written(path, world.cwd, self.protected, world.variables(self.inherited))
        if root:
            raise Denied(root)

    def run(self, worlds, stops=()):
        """A list: and-or lists split by `;` or `&`, up to a word in STOPS."""
        while True:
            self.newlines()
            if self.peek()[0] == "end" or self.at(stops):
                return worlds
            start = worlds
            worlds = self.and_or(worlds)
            if self.op("&"):
                worlds = with_status(start, {True})

    def and_or(self, worlds):
        worlds = self.pipeline(worlds)
        while True:
            op = self.op("&&", "||")
            if not op:
                return worlds
            self.newlines()
            go = passed(worlds, op == "&&")
            worlds = (worlds - go) | self.pipeline(go)

    def pipeline(self, worlds):
        negate = self.keyword("!")
        out, stages = self.command(worlds), 1
        while self.op("|", "|&"):
            self.newlines()
            out, stages = self.command(worlds), stages + 1
        if stages > 1:
            # bash runs every stage in a subshell; zsh keeps the last one
            out |= with_status(worlds, statuses(out))
        return {world._replace(ok=not world.ok) for world in out} if negate else out

    def command(self, worlds):
        self.budget -= len(worlds) + 1
        if self.budget < 0:
            raise TooMany(BUDGET)
        item = self.peek()
        if item[0] == "end":
            return worlds
        if item[0] == "op":
            self.advance()
            if item[1] != "(":
                return worlds
            inner = self.run(worlds, (")",))
            self.op(")")
            return self.trailing(with_status(worlds, statuses(inner)), worlds)
        if self.keyword("{"):
            out = self.run(worlds, ("}",))
            self.keyword("}")
            return self.trailing(out, worlds)
        if self.keyword("if"):
            return self.trailing(self.branches(worlds), worlds)
        loop = self.keyword("while", "until", "for", "select")
        if loop:
            return self.trailing(self.loop(loop, worlds), worlds)
        if self.keyword("case"):
            return self.trailing(self.arms(worlds), worlds)
        if item[1] and item[1][0] in KEYWORDS:
            self.keyword(item[1][0])  # a reserved word out of place: read past it
            return worlds
        return self.simple(worlds)

    def branches(self, worlds):
        out, rest = set(), self.run(worlds, ("then",))
        while self.keyword("then"):
            out |= self.run(passed(rest, True), ("elif", "else", "fi"))
            rest = passed(rest, False)
            if self.keyword("elif"):
                rest = self.run(rest, ("then",))
        if self.keyword("else"):
            out |= self.run(rest, ("fi",))
            rest = set()
        self.keyword("fi")
        return out | with_status(rest, {True})

    def loop(self, kind, worlds):
        """A loop body runs zero, one or two times; a third pass reads like the second."""
        out, header = set(worlds), []
        if kind in ("for", "select"):
            header = self.text(worlds)
            if not (header[2:] and header[1] == "in" and re.fullmatch(r"[A-Za-z_]\w*", header[0])):
                header = []
            worlds = self.bind(header, worlds)
        start = (self.i, self.skip)
        self.loops.append(set())  # the worlds a `break` or `continue` leaves at
        for _ in range(2):
            self.i, self.skip = start
            if kind in ("while", "until"):
                cond = self.run(worlds, ("do",))
                out |= cond
                worlds = passed(cond, kind == "while")
            self.newlines()
            self.keyword("do")
            worlds = self.run(worlds, ("done",)) | self.loops[-1]
            out |= worlds
            worlds |= self.bind(header, worlds)  # the next pass takes the next value
        self.loops.pop()
        self.keyword("done")
        return with_status(out, {True, False})

    def bind(self, header, worlds):
        """Each world once per value a `for NAME in WORDS` loop gives NAME."""
        if not header:
            return worlds
        return {world.set({header[0]: expand(value, world.variables(self.inherited))})
                for world in worlds for value in header[2:]}

    def arms(self, worlds):
        """`case WORD in PATTERN) LIST ;; ... esac`: any arm, or none, runs.

        An arm after `;&` or `;;&` may also start where the arm before it ended.
        """
        out, carry = set(worlds), set()
        self.text(worlds)  # WORD in, and the first pattern when it shares the line
        while self.peek()[0] != "end" and not self.keyword("esac"):
            if self.op(")"):
                arm = self.run(worlds | carry, (";;", ";&", ";;&", "esac"))
                out |= arm
                carry = arm if self.op(";;", ";&", ";;&") in (";&", ";;&") else set()
            elif self.peek()[0] == "cmd":
                self.text(worlds)  # pattern words
            else:
                self.advance()  # `|` and `(` in a pattern, and newlines
        return with_status(out, {True, False})

    def trailing(self, out, worlds):
        """A redirect after `)`, `}`, `fi`, `done` or `esac` opens before the body runs."""
        item = self.peek()
        if item[0] == "cmd" and not item[1]:
            self.advance()
            for world in worlds:
                for target in item[2]:
                    self.check(target, world)
        return out

    def nested(self, command, worlds):
        """Read the text `eval` runs in this shell; text that cannot split changes nothing."""
        try:
            reader = Reader(command, self.protected)
        except ValueError:
            return with_status(worlds, {True, False})
        reader.budget, reader.inherited, reader.loops = self.budget, self.inherited, self.loops
        out = reader.run(worlds)
        self.budget = reader.budget
        return out

    def simple(self, worlds):
        _, words, targets = self.peek()
        self.advance()
        while self.op("("):  # $( ), <( ) and >( ) run in a subshell inside this command
            self.run(worlds, (")",))
            self.op(")")
            item = self.peek()
            if item[0] == "cmd":
                self.advance()
                words, targets = words + item[1], targets + item[2]
        out = set()
        for world in worlds:
            for target in targets:
                self.check(target, world)
            command, forked = unwrap(words)
            name = os.path.basename(command[0]) if command else ""
            args = [word for word in command[1:] if not word.startswith("-")]
            here = not forked  # a builtin's effect reaches this shell
            if here:
                changes = assigned(command or words, world.variables(self.inherited))
                if name == "unset":
                    changes = {arg: "" for arg in args}
                world = world.set(changes) if changes else world
            variables = world.variables(self.inherited)
            if name == "cd" and here:
                folder = resolve(args[0] if args else "~", world.cwd, variables)
                out.add(world._replace(cwd=folder, ok=True))
                if not os.path.isdir(folder):
                    out.add(world._replace(ok=False))
                continue
            if name == "exit" and here:
                continue
            if name == "eval" and here:
                out |= self.nested(" ".join(command[1:]).translate(LITERAL), {world})
                continue
            if name in ("break", "continue") and here and self.loops:
                self.loops[-1] |= with_status({world}, {True, False})
            if name == "git":
                folder, verb = git_target(command, world.cwd, variables)
                root = inside(folder, self.protected) if verb in GIT_WRITES else None
                if root:
                    raise Denied(root)
            elif name in PATH_WRITERS or (name in LAST_ARG_WRITERS and args):
                for path in args if name in PATH_WRITERS else args[-1:]:
                    self.check(path, world)
            out |= with_status({world}, {True, False})
        return out


def bash_root(command, cwd, protected):
    try:
        Reader(command, protected).run({World(cwd, (), True)})
    except Denied as denied:
        return denied.args[0]
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


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))


def main():
    try:
        root = denied_root(json.load(sys.stdin))
    except TooMany:
        deny(f"checkout_guard cannot read this command within {BUDGET} steps, so it cannot "
             "tell whether it writes in a lane checkout (sd:6083). Split it into shorter "
             "commands.")
        return 0
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        print(f"checkout_guard: unreadable input, allowing: {error!r}", file=sys.stderr)
        return 0
    if root is not None:
        deny(f"{root} is a lane checkout: the lane and repo-sync own it, and agents do "
             "not write there (sd:6001). Work in a worktree: git -C "
             f"{root} worktree add ~/worktrees/<repo>-<branch> -b <branch> origin/main")
    return 0


if __name__ == "__main__":
    sys.exit(main())
