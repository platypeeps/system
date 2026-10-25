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
  stage, a `&` job, and every change itself, since an assignment, `export`,
  `unset`, `set --`, `shift`, `cd`, `pushd` or `eval` may fail. The reader
  carries every (folder, folder stack, variables, status) world and denies a
  write in any of them (sd:6083).
- A word expands as the shell expands it: braces, `~`, variables, splitting
  and globs. What the reader cannot know (input, `$( )`, arrays, `${..}`
  forms, a stack entry) reads as unknown, and a write through it is denied.

Malformed input or a command shlex cannot split fails open: no decision, a
warning on stderr, exit 0. A broken hook must not stop every session. A
command the reader cannot follow within BUDGET steps, or that writes through
an unknown, is denied: its writes are unknown, and a shorter command reads.
"""

import collections
import functools
import glob
import itertools
import json
import math
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
READS = {"read": ("REPLY",), "mapfile": ("MAPFILE",), "readarray": ("MAPFILE",),
         "getopts": ("OPTARG", "OPTIND"), "printf": ()}  # set names from input
NAME = re.compile(r"[A-Za-z_]\w*")
ASSIGNMENT = re.compile(r"[A-Za-z_]\w*(\[[^]]*\])?\+?=")
# $NAME, ${NAME}, $1; `$$`, `$?`, `$#` and `$!` are numbers; any other `$` form
# (`${D%/}`, `$@`, `$(`) and a backquote read as unknown
VARIABLE = re.compile(r"\$(?:\{(?P<braced>\w+)\}|(?P<name>[A-Za-z_]\w*|\d)|(?P<number>[$?#!])"
                      r"|[{(@*-])|`")
TILDE = re.compile(r"^~([+-]?)(\d*)(?=/|$)")
SEQUENCE = re.compile(r"(-?\d+)\.\.(-?\d+)(?:\.\.(-?\d+))?"
                      r"|([A-Za-z])\.\.([A-Za-z])(?:\.\.(-?\d+))?")
GLOB = re.compile(r"[*?[]")
HEREDOC = re.compile(r"<<(-?)[ \t]*(['\"]?)([^\s'\";&|<>()]+)\2")  # not `<<<`
UNKNOWN = "\0"  # a value the reader cannot know; no shell word holds one
LONGEST = 4096  # a longer value reads as unknown, so `D=$D$D` stops growing
OPERATOR = set("();<>|&")
DOUBLE = "~{},*?["  # what double quotes keep from expanding
QUOTED = DOUBLE + "$`"  # what single quotes or a backslash keep from expanding
HIDDEN = 0xE000  # a quoted operator character moves to the private-use plane
UNHIDE = {HIDDEN + ord(char): char for char in OPERATOR}
LITERAL = {HIDDEN + ord(char): char for char in QUOTED}  # a quoted one expands to nothing
LIVE = {HIDDEN + ord(char): char for char in "*?["}  # an unquoted `$D` may glob its value
ESCAPED = {**LITERAL, **{HIDDEN + ord(char): glob.escape(char) for char in "*?["}}
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


def environment():
    """The hook's own variables, each with its one value."""
    return {name: frozenset([value]) for name, value in os.environ.items()}


class Unreadable(Exception):
    """The reader cannot follow the command; the reason completes `cannot read this command`."""


class Budget:
    """The steps left: each command, world and expanded word spends some."""

    def __init__(self):
        self.left = BUDGET

    def __call__(self, steps):
        self.left -= steps
        if self.left < 0:
            raise Unreadable(f"it takes more than {BUDGET} steps")


def expand(text, variables, spend):
    """Every way TEXT expands; VARIABLES maps a name to its possible values.

    A name with no known value (missing, or None among its values) stays as
    written, so its path stays under the folder. The product of the values
    is spent before it is made.
    """
    matches = list(VARIABLE.finditer(text))
    names = sorted({m.group("braced") or m.group("name") for m in matches} - {None})
    choices = [sorted(variables.get(name, variables.get("*", {None})), key=str) for name in names]
    spend(math.prod(map(len, choices)) - 1)

    def value(match, chosen):
        name = match.group("braced") or match.group("name")
        if name:
            found = chosen[name]
            return match.group(0) if found is None else found.translate(LIVE)
        return "0" if match.group("number") else UNKNOWN

    out = []
    for values in itertools.product(*choices):
        chosen = dict(zip(names, values))
        expanded = VARIABLE.sub(lambda match: value(match, chosen), text) if matches else text
        out.append(UNKNOWN if len(expanded) > LONGEST else expanded)
    return out


def tilde(text):
    """`~`, `~+` and `~-` read HOME, PWD and OLDPWD; a stack entry `~N` is unknown."""
    return TILDE.sub(lambda m: UNKNOWN if m.group(2) else
                     "${%s}" % {"": "HOME", "+": "PWD", "-": "OLDPWD"}[m.group(1)], text)


def braces(word, spend):
    """Every word brace expansion makes of WORD: `{a,b}`, `{1..3}`; `${` opens no brace."""
    depth, start, commas = 0, None, []
    for i, char in enumerate(word):
        if char == "{":
            if not depth:
                start, commas = (None if word[i - 1:i] == "$" else i), []
            depth += 1
        elif char == "," and depth == 1:
            commas.append(i)
        elif char == "}" and depth:
            depth -= 1
            if depth or start is None:
                continue
            head, body, tail = word[:start], word[start + 1:i], word[i + 1:]
            if commas:
                cuts = [start] + commas + [i]
                parts = [word[a + 1:b] for a, b in zip(cuts, cuts[1:])]
                spend(len(parts))
            else:
                parts = sequence(body, spend)
            if parts is None:  # `{x}` stays; a brace inside it may still expand
                inner, rest = braces(body, spend), braces(tail, spend)
                spend(len(inner) * len(rest) - 1)
                return [head + "{" + one + "}" + two for one in inner for two in rest]
            return [out for part in parts for out in braces(head + part + tail, spend)]
    return [word]


def sequence(body, spend):
    """The words of `{1..9..2}` or `{a..e}`, or None; their count is spent before they are made."""
    match = SEQUENCE.fullmatch(body)
    if not match:
        return None
    letters = match.group(4) is not None
    first, last, step = match.group(4, 5, 6) if letters else match.group(1, 2, 3)
    low, high = (ord(first), ord(last)) if letters else (int(first), int(last))
    step = abs(int(step or 1)) or 1
    spend(abs(high - low) // step + 1)
    numbers = range(low, high + (1 if high >= low else -1), step if high >= low else -step)
    if letters:
        return [chr(number) for number in numbers]
    padded = any(re.match(r"-?0\d", end) for end in (first, last))
    return [str(number).zfill(max(map(len, (first, last))) if padded else 0) for number in numbers]


def fields(word, variables, cwd, spend):
    """Every field WORD may become: braces, `~`, variables, splitting, then globs.

    A field keeps the whole value beside its split pieces, and a glob keeps
    itself beside its matches, so the list holds what the shell makes with or
    without quotes. A match is relative when its pattern is.
    """
    out = []
    for text in braces(word, spend):
        for value in expand(tilde(text), variables, spend):
            pieces = value.split()
            for piece in [value] + (pieces if len(pieces) > 1 else []):
                out.append(piece)
                relative = not piece.startswith("/")
                if not GLOB.search(piece) or UNKNOWN in piece or (relative and cwd is None):
                    continue
                pattern = os.path.join(glob.escape(cwd or "/"), piece.translate(ESCAPED))
                for match in glob.iglob(pattern):
                    spend(1)
                    out.append(os.path.relpath(match, cwd) if relative else match)
    return out


def resolve(path, cwd, variables, spend):
    """Every folder or file PATH may name; None for one the reader cannot know."""
    out = []
    for text in fields(path, variables, cwd, spend):
        text = os.path.expanduser(text).translate(LITERAL)
        known = UNKNOWN not in text and (cwd is not None or os.path.isabs(text))
        out.append(os.path.realpath(os.path.join(cwd or "/", text)) if known else None)
    return out


def inside(full, protected):
    """The root FULL lies in, matched without case: macOS folders ignore it."""
    for root in protected:
        if full.casefold() == root.casefold() or full.casefold().startswith(
                root.casefold() + os.sep):
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


def written(path, cwd, protected, variables=None, spend=None):
    """The root a write to PATH may land in, or None."""
    for full in resolve(path, cwd, environment() if variables is None else variables,
                        spend or Budget()):
        if full is None:
            raise Unreadable("it writes to a path it cannot know")
        root = inside(full, protected)
        if root is not None and not ignored(root, full):
            return root
    return None


def shield(command):
    """Hide quoted and escaped operators from the lexer; drop comments.

    A `$` in single quotes, after a backslash or before a quote (`$'..'`),
    and a quoted or escaped `~`, brace, comma or glob character, hide too:
    the shell expands none of them.
    An unquoted newline ends a command, and a `#` that starts a word starts a
    comment to the end of its line, as the shell reads them. A here-document
    body is text, not commands: it drops at the newline that starts it.
    """
    out, quote, i, depth, bodies = [], None, 0, 0, []
    while i < len(command):
        char = command[i]
        heredoc = HEREDOC.match(command, i) if quote is None else None
        if heredoc:
            bodies.append((heredoc.group(3), heredoc.group(1) == "-"))
            out.append(f"<< {heredoc.group(3)} ")
            i = heredoc.end()
            continue
        if char == "\n" and quote is None and bodies:
            out.append(";")
            i += 1
            for delimiter, tabs in bodies:
                while i < len(command):
                    end = command.find("\n", i)
                    end = len(command) if end < 0 else end
                    line, i = command[i:end], end + 1
                    if (line.lstrip("\t") if tabs else line) == delimiter:
                        break
            bodies = []
            continue
        if char == "\\" and quote != "'" and i + 1 < len(command):
            if quote is None and command[i + 1] == "\n":
                i += 2
                continue
            out += [char, hide(command[i + 1], QUOTED)]
            i += 2
            continue
        if quote:
            quote = None if char == quote else quote
            depth = depth if quote else 0
            # `${..}`, `$*` and `$?` expand in double quotes
            if quote == '"' and char not in OPERATOR and (out[-1:] == ["$"] or
                                                          depth and char in "{}"):
                depth += {"{": 1, "}": -1}.get(char, 0)
                out.append(char)
            else:
                out.append(hide(char, QUOTED if quote == "'" else DOUBLE))
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


def git_target(words, cwd, variables, spend):
    """(folders, verb) for a git command; -C folders stack as git stacks them."""
    i, folders = 1, [cwd]
    while i < len(words) and words[i].startswith("-"):
        if words[i] == "-C" and i + 1 < len(words):
            folders = [full for folder in folders for full in resolve(words[i + 1], folder,
                                                                      variables, spend)]
            i += 2
        elif words[i] == "-c" and i + 1 < len(words):
            i += 2
        else:
            i += 1
    return folders, words[i] if i < len(words) else ""


def assigned(words, variables, spend):
    """The values a builtin or a bare `NAME=value` sets; a value read from input is unknown."""
    head, unknown = (words or [""])[0], frozenset([UNKNOWN])
    if head == "unset":
        return {word: frozenset([""]) for word in words[1:] if not word.startswith("-")}
    if head in READS:
        names = (words[words.index("-v") + 1:][:1] if "-v" in words else []) if head == "printf" \
            else words[1:]
        return {name: unknown for name in [*names, *READS[head]] if NAME.fullmatch(name)}
    if head == "set":
        rest = words[1:]
        if "--" in rest:
            rest = rest[rest.index("--") + 1:]
        elif not rest or rest[0].startswith(("-", "+")):
            return {}
        changes = {}
        for i, word in enumerate(rest, 1):
            values = expand(word, variables, spend)
            if len(braces(word, spend)) > 1 or any(
                    len(value.split()) != 1 or GLOB.search(value) or UNKNOWN in value
                    for value in values):  # it may make more words: later places are unknown
                return {**changes, **{str(n): unknown for n in range(i, len(rest) + 10)}}
            changes[str(i)] = frozenset(values)
        return changes
    if head == "shift":
        counts = {int(text) if text.isdigit() else None
                  for text in (expand(words[1], variables, spend) if words[1:] else ["1"])}
        known = max((int(name) for name in variables if name.isdigit() and name != "0"), default=0)
        return {str(i): frozenset().union(*(variables.get(str(i + count), frozenset([""]))
                                            if count is not None else unknown for count in counts))
                for i in range(1, known + 1)}
    options = ""
    if head in DECLARERS:
        options = "".join(word[1:] for word in words[1:] if word[:1] in "-+")
        if "n" in options and head != "export":
            raise Unreadable("a name reference moves its writes to another name")
        words = [word for word in words[1:] if word[:1] not in "-+"]
    elif not all(ASSIGNMENT.match(word) for word in words):
        return {}
    variables, changes = dict(variables), {}
    for word in words:
        match = ASSIGNMENT.match(word)
        if not match:
            continue
        name = NAME.match(word).group()
        if match.group(1) or set(options) & set("aA"):  # an array or one of its elements
            variables[name] = changes[name] = unknown
            continue
        text = ("${%s}" % name if match.group().endswith("+=") else "") + tilde(word[match.end():])
        variables[name] = changes[name] = frozenset(
            os.path.expanduser(value) for value in expand(text, variables, spend))
    return changes


def merged(worlds, inherited):
    """One world per folder and stack, each variable with every value it has in any of them.

    An and-or list starts in every world whatever its status, so a `;` may
    merge them: this keeps a run of assignments from doubling the worlds.
    """
    groups = {}
    for world in worlds:
        groups.setdefault((world.cwd, world.stack), []).append(dict(world.changes))
    out = set()
    for (cwd, stack), maps in groups.items():
        names = set().union(*maps)
        changes = {name: frozenset().union(*(found.get(name, inherited.get(name, {None}))
                                             for found in maps)) for name in names}
        out.add(World(cwd, tuple(sorted(changes.items(), key=lambda item: item[0])), True, stack))
    return out


class World(collections.namedtuple("World", "cwd changes ok stack", defaults=((),))):
    """One state the shell may be in: folder (None: unknown), each variable's possible
    values, last status, and the `pushd` stack below the folder, whose end is unknown."""

    def variables(self, inherited):
        """Each name's values; after `source`, "*" holds those of every name not set since."""
        changes = dict(self.changes)
        return changes if "*" in changes else {**inherited, **changes}

    def set(self, changes):
        changes = {**dict(self.changes), **changes}
        return self._replace(changes=tuple(sorted(changes.items(), key=lambda item: item[0])))

    def moved(self, folder, stack):
        """This world after a `cd`, `pushd` or `popd` to FOLDER that worked."""
        return self.set({"OLDPWD": frozenset([UNKNOWN if self.cwd is None else self.cwd]),
                         "PWD": frozenset([UNKNOWN if folder is None else folder])})._replace(
            cwd=folder, stack=stack, ok=True)


class Denied(Exception):
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
    run in a subshell, so their changes may not last; and any change may fail.
    A write in any world is a write.
    """

    def __init__(self, command, protected):
        self.items, self.protected, self.i, self.skip = items(command), protected, 0, 0
        self.spend, self.inherited, self.loops = Budget(), environment(), []

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
        """Read past a word list (case word, patterns, for words), running each `$( )`;
        what it prints is unknown."""
        words = []
        while self.peek()[0] == "cmd":
            words += self.peek()[1]
            self.advance()
            if not (words and words[-1].endswith("$") and self.op("(")):
                break
            words[-1] = words[-1][:-1] + UNKNOWN
            self.run(worlds, (")",))
            self.op(")")
        return words

    def check(self, path, world):
        root = written(path, world.cwd, self.protected, world.variables(self.inherited),
                       self.spend)
        if root:
            raise Denied(root)

    def run(self, worlds, stops=()):
        """A list: and-or lists split by `;` or `&`, up to a word in STOPS."""
        while True:
            self.newlines()
            if self.peek()[0] == "end" or self.at(stops):
                return worlds
            start = worlds = merged(worlds, self.inherited)
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
        self.spend(len(worlds) + 1)
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
        if item[1][:1] == ["function"] or len(item[1]) == 1 and NAME.fullmatch(item[1][0]) and \
                self.items[self.i + 1:self.i + 3] == [("op", "("), ("op", ")")]:
            raise Unreadable("it defines a function, and the reader does not follow calls")
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
            if header[:1] and NAME.fullmatch(header[0]) and header[1:2] in ([], ["do"]):
                header = [header[0], "in", "$@"]  # `for NAME; do` walks the arguments
            if not (header[2:] and header[1] == "in" and NAME.fullmatch(header[0])):
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
        return {world.set({header[0]: frozenset([text])})
                for world in worlds for value in header[2:]
                for text in fields(value, world.variables(self.inherited), world.cwd, self.spend)}

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
        reader.spend, reader.inherited, reader.loops = self.spend, self.inherited, self.loops
        return reader.run(worlds)

    def simple(self, worlds):
        _, words, targets = self.peek()
        self.advance()
        while self.op("("):  # $( ), <( ) and >( ) run in a subshell inside this command
            if words and ASSIGNMENT.fullmatch(words[-1]):  # NAME=( ) is an array
                while self.peek()[0] != "end" and not self.op(")"):
                    self.advance()
                words = words[:-1] + [words[-1] + UNKNOWN]
            else:
                if words and words[-1].endswith("$"):  # what `$( )` prints is unknown
                    words = words[:-1] + [words[-1][:-1] + UNKNOWN]
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
            head = command[0] if command else ""
            if VARIABLE.search(head) or UNKNOWN in head:
                # the name comes from an expansion: each value it may take, split
                for text in expand(head, world.variables(self.inherited), self.spend):
                    found, child = unwrap(text.split() + command[1:])
                    out |= self.effect(world, found, forked or child, words)
            else:
                out |= self.effect(world, command, forked, words)
        return out

    def effect(self, world, command, forked, words):
        """The worlds COMMAND leaves WORLD in; FORKED when it runs in a child."""
        name = os.path.basename(command[0]) if command else ""
        args = [word for word in command[1:] if not word.startswith("-")]
        here, out = not forked, set()  # a builtin's effect reaches this shell
        # every change may fail (a readonly variable, a missing folder, a bad
        # word): the world it started in stays beside the changed one
        if here:
            changes = assigned(command or words, world.variables(self.inherited), self.spend)
            if changes:
                out.add(world._replace(ok=False))
                world = world.set(changes)
        variables = world.variables(self.inherited)
        if name in ("cd", "pushd", "popd") and here:
            return out | {world._replace(ok=False)} | {
                world.moved(folder, stack)
                for folder, stack in self.moves(name, command[1:], world, variables)}
        if name == "exit" and here:
            return out
        if name == "eval" and here:  # its words expand, then read again
            for text in expand(" ".join(command[1:]), variables, self.spend):
                out |= self.nested(text.translate(LITERAL), {world})
            return out | {world._replace(ok=False)}
        if name in ("break", "continue") and here and self.loops:
            self.loops[-1] |= with_status({world}, {True, False})
        if UNKNOWN in name:  # any command: it may write each word
            for path in command[1:]:
                self.check(path, world)
        if (UNKNOWN in name or name in ("source", ".")) and here:  # it may change anything
            out.add(world.moved(None, ()).set({"*": frozenset([UNKNOWN])}))
        elif name == "git":
            folders, verb = git_target(command, world.cwd, variables, self.spend)
            for folder in folders if verb in GIT_WRITES else ():
                if folder is None:
                    raise Unreadable("git writes in a folder it cannot know")
                root = inside(folder, self.protected)
                if root:
                    raise Denied(root)
        elif name in PATH_WRITERS or (name in LAST_ARG_WRITERS and args):
            for path in args if name in PATH_WRITERS else args[-1:]:
                self.check(path, world)
        return out | with_status({world}, {True, False})

    def moves(self, name, words, world, variables):
        """(folder, stack) pairs a `cd`, `pushd` or `popd` that works may leave.

        None is a folder the reader cannot know: past the known stack, a stack
        index (`+1`, `-1`, which zsh's `cd` takes too), or an unknown word.
        """
        options = [word for word in words if word.startswith("-") and word not in ("-", "--")]
        args = [word for word in words if word not in options and word != "--"]
        stack = world.stack
        if any(re.fullmatch(r"[+-]\d+", word) for word in words):
            return [(None, ())]
        stays = "-n" in options  # the stack moves, the folder stays
        if name == "popd":
            if stays:
                return [(world.cwd, stack[1:])]
            return [(stack[0], stack[1:])] if stack else [(None, ())]
        if name == "pushd" and not args:  # swaps the top two
            if stays:
                return [(world.cwd, ())]
            return [(stack[0], (world.cwd,) + stack[1:])] if stack else [(None, (world.cwd,))]
        target = {"-": "$OLDPWD"}.get(args[0], args[0]) if args else "$HOME"
        folders = resolve(target, world.cwd, variables, self.spend)
        if not target.startswith(("/", ".")):  # a relative `cd` tries CDPATH first
            for value in variables.get("CDPATH", ()):
                for entry in (value or "").split(":"):
                    if entry:
                        folders += resolve(os.path.join(entry, target), world.cwd, variables,
                                           self.spend)
        if name == "cd":
            return [(folder, stack) for folder in folders]
        if stays:
            return [(world.cwd, (folder,) + stack) for folder in folders]
        return [(folder, (world.cwd,) + stack) for folder in folders]


def bash_root(command, cwd, protected):
    try:
        Reader(command, protected).run({World(cwd, (("OLDPWD", frozenset([UNKNOWN])),
                                                    ("PWD", frozenset([cwd]))), True)})
    except Denied as denied:
        return denied.args[0]
    except RecursionError:
        raise Unreadable("it nests deeper than the reader goes") from None
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
    except Unreadable as error:
        deny(f"checkout_guard cannot read this command: {error}, so it cannot tell whether "
             "it writes in a lane checkout (sd:6083). Split it into shorter commands, or "
             "write the path out.")
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
