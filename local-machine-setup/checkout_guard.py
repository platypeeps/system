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
  and `NAME=value`, alone or after `export`, sets `$NAME` for later
  commands; a name `readonly`, `declare`, `typeset`, `local` or
  `export -n` touches is unknown, and so is every name after a bare
  `readonly`. A quoted or escaped operator is plain text. Fetch,
  worktree, branch and every read pass.
- A change that may not happen keeps the old state beside the new one: a
  command after `&&` or `||`, in an `if`, loop or `case` body, a pipeline
  stage, a `&` job, and every change itself, since `unset`, `set --`,
  `shift`, `cd`, `pushd`, `eval` and an assignment to a name the shell may
  hold readonly may fail. The reader carries every (folder, folder stack,
  variables, status) world and denies a write in any of them (sd:6083).
  A loop body runs zero, one or two times; what the second pass still
  changes is unknown after it.
- A word expands as the shell expands it: braces, `~`, variables, the
  `${..}` forms in parameter(), `$'..'` escapes, splitting and globs. What
  the reader cannot know (input, xargs input, `$( )`, arrays, other `${..}`
  forms, a stack entry) reads as unknown.
- `sh -c` and `bash -c` read their script in a child shell (sd:6153). A
  function definition is unknown: the reader does not follow functions.

Malformed input or a command shlex cannot split fails open: no decision, a
warning on stderr, exit 0. A broken hook must not stop every session. A
command the reader cannot follow within BUDGET steps, that writes through
an unknown or after `source`, or that the reader fails on, is denied near a
root: when a world's folder is in one, or the text names one. Elsewhere it
passes.
"""

import collections
import fnmatch
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
FORKS = {"rtk", "proxy", "nohup", "env", "exec", "nice", "timeout", "xargs", "sudo", "stdbuf",
         "coproc"}
VALUED = {"env": {"-u", "-P", "-S", "-C"}, "nice": {"-n"}, "timeout": {"-s", "-k"},
          "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-U", "-r", "-t"},
          "xargs": {"-n", "-I", "-J", "-L", "-P", "-R", "-S", "-s", "-E", "-d", "-a"},
          "stdbuf": {"-i", "-o", "-e"}}  # options that take the next word
DECLARERS = {"export", "readonly", "declare", "local", "typeset"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}  # `-c SCRIPT` runs SCRIPT in a child
READONLY = {"BASHOPTS", "BASH_VERSINFO", "EUID", "PPID", "SHELLOPTS", "UID"}  # bash's own
READS = {"read": ("REPLY",), "mapfile": ("MAPFILE",), "readarray": ("MAPFILE",),
         "getopts": ("OPTARG", "OPTIND"), "printf": ()}  # set names from input
NAME = re.compile(r"[A-Za-z_]\w*", re.ASCII)
FUNCTION = re.compile(r"[^$`\\\0\uE07F]+")  # bash takes `my-f` and `a.b` too
ASSIGNMENT = re.compile(r"[A-Za-z_]\w*(\[[^]]*\])?\+?=", re.ASCII)
# $NAME, $1, ${NAME}, ${#NAME} and ${NAME<op>WORD} for the ops in parameter()
# (a `?` double quotes hid counts), WORD holding no brace, `$` or backquote
# but `$NAME` and `${NAME}`; `$$`, `$?`, `$#` and `$!` are numbers; any other
# `$` form (`${D/a/b}`, `$@`, `$(`) and a backquote read as unknown
REFERENCE = r"\$(?:\{(?P<braced>[A-Za-z_]\w*|[0-9])\}|(?P<name>[A-Za-z_]\w*|[0-9]))"
WORD_REFERENCE = re.compile(REFERENCE, re.ASCII)
DEFAULTED = re.compile(r"\$\{([A-Za-z_]\w*):?=", re.ASCII)  # `${D:=w}` and `${D=w}` set D
VARIABLE = re.compile(r"\$(?:\{(?P<length>#)?(?P<braced>[A-Za-z_]\w*|[0-9]+)"
                      r"(?:(?P<op>:?[-+=?\uE03F]|##?|%%?)(?P<word>(?:[^{}$`\\]|"
                      + re.sub(r"\?P<\w+>", "?:", REFERENCE) + r")*))?\}"
                      r"|(?P<name>[A-Za-z_]\w*|[0-9])|(?P<number>[$?#!])|[{(@*-])|`", re.ASCII)
TILDE = re.compile(r"^~([+-]?)(\d*)(?=/|$)", re.ASCII)
USER_TILDE = re.compile(r"~[A-Za-z_][\w.-]*(?=/|$)", re.ASCII)
SEQUENCE = re.compile(r"(-?\d+)\.\.(-?\d+)(?:\.\.(-?\d+))?"
                      r"|([A-Za-z])\.\.([A-Za-z])(?:\.\.(-?\d+))?", re.ASCII)
GLOB = re.compile(r"[*?[]")
CASE = re.compile(r"(case|esac)(?![^\s;&|()<>])")
CLOSE = {"(": ")", "a(": ")", "a[": "]", "{": "}"}
PATH_END = r"(?![^/\s'\"`;&|()<>{}\[\],:])"  # a `/` or the end of a word follows
PATH_START = r"(?<![^\s'\"`;&|()<>{}\[\],:=])"  # the start of a word or a value precedes
PARAMETER_WORD = re.compile(r"\$\{\w+:?[-=+?]")  # `${D:-` and kin start a value too
UNKNOWN = "\0"  # a value the reader cannot know; no shell word holds one
LONGEST = 4096  # a longer value reads as unknown, so `D=$D$D` stops growing
OPERATOR = set("();<>|&")
DOUBLE = "~{},*?["  # what double quotes keep from expanding
QUOTED = DOUBLE + "$`"  # what single quotes or a backslash keep from expanding
HIDDEN = 0xE000  # a quoted operator character moves to the private-use plane
MARK = chr(HIDDEN + 0x7F)  # a quote was here; a quoted reserved word keeps it
UNHIDE = {HIDDEN + ord(char): char for char in OPERATOR} | {ord(MARK): None}
LITERAL = {HIDDEN + ord(char): char for char in QUOTED} | {ord(MARK): None}
LIVE = {HIDDEN + ord(char): char for char in "*?["}  # an unquoted `$D` may glob its value
ESCAPED = {**LITERAL, **{HIDDEN + ord(char): glob.escape(char) for char in "*?["}}
ANSI = re.compile(r"\\(x[0-9A-Fa-f]{1,2}|u[0-9A-Fa-f]{1,4}|U[0-9A-Fa-f]{1,8}|[0-7]{1,3}|c.|.)",
                  re.DOTALL)
ESCAPES = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r",
           "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}
CONTROL = re.compile(r"&>>|&>|>>|>&|>\||<<<|<<-|<<|<&|<>|[<>]|;;&|;;|;&|&&|\|\||\|&|[;&|()]")
KEYWORDS = {"if", "then", "elif", "else", "fi", "while", "until", "for", "select", "do", "done",
            "case", "esac", "{", "}", "!"}
RESERVED = KEYWORDS | {"in", "function", "time", "[[", "]]"}  # quoted, each is a command
COMPOUND = {"{", "if", "while", "until", "for", "select", "case"}  # `time` or `coproc` may lead
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


class Unsplittable(ValueError):
    """shlex cannot split the command: it fails open, like malformed input."""


class Budget:
    """The steps left: each command, world and expanded word spends some.

    It also keeps, for the whole read, the first thing the reader could not
    know, and whether a world's folder was ever in a root (NEAR).
    """

    def __init__(self):
        self.left, self.unknown, self.near = BUDGET, None, False

    def note(self, reason):
        self.unknown = self.unknown or reason

    def __call__(self, steps):
        self.left -= steps
        if self.left < 0:
            raise Unreadable(f"it takes more than {BUDGET} steps")


def defaults(words, variables, spend):
    """The names a `${D:=w}` or `${D=w}` in WORDS sets, each with the values
    that form expands to; unknown where the reader cannot read the form."""
    out = {}
    for text in words:
        for match in DEFAULTED.finditer(text):
            form = VARIABLE.match(text, match.start())
            values = [UNKNOWN]
            if form and form.group("braced") == match.group(1):
                values = [UNKNOWN if value == form.group(0) else value
                          for value in expand(form.group(0), variables, spend)]
            out[match.group(1)] = out.get(match.group(1), frozenset()) | frozenset(values)
    return out


def expand(text, variables, spend):
    """Every way TEXT expands; VARIABLES maps a name to its possible values.

    A name with no known value (missing, or None among its values) stays as
    written, so its path stays under the folder. The product of the values
    is spent before it is made.
    """
    matches = list(VARIABLE.finditer(text))
    names = sorted({name for match in matches for name in referenced(match)})
    choices = [sorted(variables.get(name, variables.get("*", {None})), key=str) for name in names]
    spend(math.prod(map(len, choices)) - 1)
    out = []
    for values in itertools.product(*choices):
        chosen, parts, last = dict(zip(names, values)), [], 0
        for match in matches:
            parts += [[text[last:match.start()]], parameter(match, chosen)]
            last = match.end()
        parts.append([text[last:]])
        spend(math.prod(map(len, parts)) - 1)
        for pieces in itertools.product(*parts):
            expanded = "".join(pieces)
            out.append(UNKNOWN if len(expanded) > LONGEST else expanded)
    return out


def referenced(match):
    """The names a VARIABLE match reads: its own, and those in its WORD."""
    name = match.group("braced") or match.group("name")
    words = WORD_REFERENCE.finditer(match.group("word") or "")
    return ([name] if name else []) + [ref.group("braced") or ref.group("name") for ref in words]


def parameter(match, chosen):
    """Every value one `$` form takes, CHOSEN giving each name one value (sd:6153).

    `-` and `+` test for a set name, `:-` and `:+` for a non-empty one; an
    empty value may be an unset name, so it takes both readings. `=` and `:=`
    read as `-` and `:-`, and also set the name: Reader.simple. `#` and `%`
    cut the shortest prefix or suffix the pattern matches, `##` and `%%` the
    longest. A name with no known value stays as written, beside the WORD.
    A form the reader models only in part is unknown, never a known but
    wrong value: any form of a value holding an unknown, and a pattern with
    a quote, a `[` or a value that globs (lead review of cd702481). `$!` is
    empty with no job in the background.
    """
    name = match.group("braced") or match.group("name")
    if not name:
        number = match.group("number")
        return ["", "0"] if number == "!" else ["0"] if number else [UNKNOWN]
    found, op = chosen[name], (match.group("op") or "").translate(LITERAL).replace("=", "-")
    if found is not None and UNKNOWN in found and (op or match.group("length")):
        return [UNKNOWN]  # it may be empty, or any length
    if match.group("length"):
        return [UNKNOWN if op else "0" if found is None else str(len(found))]
    if found is not None:
        found = found.translate(LIVE).replace(MARK, "")
    if not op:
        return [match.group(0) if found is None else found]

    def value(ref):
        known = chosen[ref.group("braced") or ref.group("name")]
        return ref.group(0) if known is None else known.translate(LIVE)

    word = WORD_REFERENCE.sub(value, match.group("word"))
    if found is None:
        return [match.group(0)] + ([word, ""] if op[-1] == "+" else
                                   [word] if op[-1] == "-" else [])
    if op[-1] in "-+?":
        empty = found == ""
        if op == ":-":
            return [word if empty else found]
        if op == "-":
            return [found, word] if empty else [found]
        if op == ":+":
            return [""] if empty else [word]
        if op == "+":
            return ["", word] if empty else [word]
        return [found]
    raw = match.group("word")
    if UNKNOWN in word or "[" in word or any(HIDDEN <= ord(char) <= ord(MARK) for char in raw) \
            or any(GLOB.search(value(ref)) for ref in WORD_REFERENCE.finditer(raw)):
        return [UNKNOWN]  # a quote, a bracket fnmatch reads otherwise, a `"$P"` that globs
    pattern, text = word.translate(LITERAL), found.translate(LITERAL)
    cuts = range(len(text) + 1) if op in ("#", "%") else range(len(text), -1, -1)
    for cut in cuts:
        if op[0] == "#" and fnmatch.fnmatchcase(text[:cut], pattern):
            return [found[cut:]]
        if op[0] == "%" and fnmatch.fnmatchcase(text[len(text) - cut:], pattern):
            return [found[:len(text) - cut]]
    return [found]


def tilde(text, assignment=False):
    """`~`, `~+` and `~-` read HOME, PWD and OLDPWD; a stack entry `~N` is
    unknown; `~user` is that user's home. An ASSIGNMENT value expands each
    `~` after a `:` too."""
    if assignment:
        return ":".join(tilde(part) for part in text.split(":"))
    user = USER_TILDE.match(text)
    if user:
        return os.path.expanduser(user.group()) + text[user.end():]
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
    padded = any(re.match(r"-?0[0-9]", end) for end in (first, last))
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
        text = text.translate(LITERAL)  # fields() read `~`; a `~` a value holds is text
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
    """The root a write to PATH may land in, UNKNOWN for a path the reader cannot
    know, or None."""
    found = None
    for full in resolve(path, cwd, environment() if variables is None else variables,
                        spend or Budget()):
        if full is None:
            found = UNKNOWN
            continue
        root = inside(full, protected)
        if root is not None and not ignored(root, full):
            return root
    return found


def named(text, protected):
    """The root TEXT names as a whole path, also as `~/..` or `$HOME/..`, or None.

    A form counts only where the start of a word or value precedes it and a
    `/` or the end of a word follows it, so `/root-tools` and `/backup/root`
    name no root (sd:6236). The word of `${D:-w}` and its kin starts a
    value, at any depth (sd:6284).
    """
    home, text = os.path.expanduser("~"), PARAMETER_WORD.sub(" ", text)
    for root in protected:
        forms = {root}
        if root.startswith(home + os.sep):
            forms |= {prefix + root[len(home):] for prefix in ("~", "$HOME", "${HOME}")}
        if any(re.search(PATH_START + re.escape(form) + PATH_END, text, re.IGNORECASE)
               for form in forms):
            return root
    return None


def shield(command):
    """Hide quoted and escaped operators from the lexer; drop comments.

    A `$` in single quotes or after a backslash, and a quoted or escaped
    `~`, brace, comma or glob character, hide too: the shell expands none
    of them. A `$'..'` comes out single-quoted, its escapes spelled (`\x2f`
    is `/`), and a `$".."` double-quoted: its `$` asks for a translation.
    A quote or backslash leaves MARK in its word, so `'if'` reads as a
    command, not a reserved word (sd:6153).
    An unquoted newline ends a command, and a `#` that starts a word starts a
    comment to the end of its line, as the shell reads them. Every `$( )`
    and backquote the shell runs, in double quotes or an unquoted
    here-document body too, comes out as an unquoted `$( )`, so the reader
    reads its commands (sd:6083 round 5).
    """
    return scan(command, 0)[0]


def scan(command, i, nest=(), closer=False):
    """(shielded text, index) for COMMAND from I; with CLOSER, up to and past the
    `)` that closes a `$(`.

    NEST holds the open `(`, and the `((`, `$[` and `${` forms, in which `<<`
    is a shift or text, not a here-document. CASES holds the NEST depth of
    each open `case`, where a `)` ends a pattern.
    """
    out, quote, depth, opened, bodies = [], None, 0, 0, []
    nest, cases, boundary = list(nest), [], True
    while i < len(command):
        char = command[i]
        if quote != "'" and (command.startswith("$(", i) or char == "`"):
            text, i = substitution(command, i, quote)
            if quote == '"':  # end the quote around it, and open it again after
                if len(out) == opened:
                    out.pop()
                else:
                    out.append('"')
                out.append(text)
                if command[i:i + 1] == '"':
                    quote, depth, i = None, 0, i + 1
                else:
                    out.append('"')
                    opened = len(out)
            else:
                out.append(text)
            boundary = False
            continue
        if quote is None and command.startswith("<<<", i):  # a here-string is one word
            out.append("<<<")
            i, boundary = i + 3, True
            continue
        if quote is None and command.startswith("<<", i) and nest[-1:] in ([], ["("]):
            tabs = command.startswith("<<-", i)
            word, quoted, end = delimiter(command, i + 2 + tabs)
            if word or quoted:
                bodies.append((word, tabs, quoted))
                out.append("<< _ ")
                i, boundary = end, True
                continue
        if char == "\n" and quote is None and bodies:
            out.append(";")
            i += 1
            for word, tabs, quoted in bodies:
                lines = []
                while i < len(command):
                    end = command.find("\n", i)
                    end = len(command) if end < 0 else end
                    line, i = command[i:end], end + 1
                    if (line.lstrip("\t") if tabs else line) == word:
                        break
                    lines.append(line)
                if not quoted:  # an unquoted body runs its substitutions
                    out.append(expansions("\n".join(lines)))
            bodies, boundary = [], True
            continue
        if quote is None and command.startswith("$'", i):  # its escapes spell characters
            end = i + 2
            while end < len(command) and command[end] != "'":
                end += 2 if command[end] == "\\" else 1
            text = ANSI.sub(escape, command[i + 2:end])
            out += [MARK, "'", *("'\\''" if c == "'" else hide(c, QUOTED) for c in text), "'"]
            i, boundary = end + 1, False
            continue
        if char == "\\" and i + 1 < len(command) and quote != "'":
            if quote is None and command[i + 1] == "\n":
                i += 2
                continue
            out += [MARK, char, hide(command[i + 1], QUOTED)]
            i, boundary = i + 2, False
            continue
        if quote == '"' and depth and char in "'\"":
            out.append(UNKNOWN)  # a quote nests in `${..}`; the reader does not follow it
            i, boundary = i + 1, False
            continue
        if quote:
            quote = None if char == quote else quote
            depth = depth if quote else 0
            # `${..}`, `$*` and `$?` expand in double quotes, and a pattern
            # in `${..}` matches there as it does unquoted
            if quote == '"' and char not in OPERATOR and (out[-1:] == ["$"] or
                                                          depth and char in "{}"):
                depth += {"{": 1, "}": -1}.get(char, 0)
                out.append(char)
            else:
                out.append(hide(char, QUOTED if quote == "'" else "~" if depth else DOUBLE))
        elif char == "$" and command[i + 1:i + 2] == '"':
            i += 1  # `$".."` is its text, translated
            continue
        elif char in "'\"":
            quote = char
            out += [MARK, char]
            opened = len(out)
        elif char == "#" and boundary:
            end = command.find("\n", i)
            i = len(command) if end < 0 else end
            continue
        else:
            word = CASE.match(command, i) if boundary else None
            if word and word.group(1) == "case":
                cases.append(len(nest))
            elif word and cases:
                cases.pop()
            if char == ")" and cases and cases[-1] == len(nest):
                pass  # a `case` pattern ends
            elif char == ")" and not nest and closer:
                return "".join(out), i + 1
            elif nest and char == CLOSE[nest[-1]]:
                nest.pop()
            elif char == "(":
                nest.append("a(" if command[i + 1:i + 2] == "(" or nest[-1:] == ["a("] else "(")
            elif char in "[{" and (out[-1:] == ["$"] or nest[-1:] in (["a["], ["{"])):
                nest.append("a[" if char == "[" else "{")
            out.append(";" if char == "\n" else char)
        boundary = quote is None and (char.isspace() or char in ";&|(")
        i += 1
    return "".join(out), i


def escape(match):
    """The character one ANSI-C escape in `$'..'` spells; NUL is unknown."""
    code = match.group(1)
    if code[0] in "xuU" and len(code) > 1:
        number = int(code[1:], 16)
        return chr(number) if number <= 0x10FFFF else UNKNOWN
    if code[0] in "01234567":
        return chr(int(code, 8) & 0xFF)
    if code[0] == "c" and len(code) > 1:
        return chr(ord(code[1]) & 0x1F)
    return ESCAPES.get(code, "\\" + code)


def substitution(command, i, quote):
    """(`$( .. )` text, index past it) for the `$(`, `$((` or backquote at I."""
    if command[i] == "`":
        end = i + 1
        while end < len(command) and command[end] != "`":
            end += 2 if command[end] == "\\" else 1
        # a backslash before `\`, a backquote or `$` (and `"` in double quotes) goes
        body = re.sub(r'\\([\\`$"])' if quote == '"' else r"\\([\\`$])", r"\1",
                      command[i + 1:end])
        return "$(" + scan(body, 0)[0] + ")", end + 1
    if command.startswith("$((", i):
        text, end = scan(command, i + 3, ["a("], True)
        return "$((" + text + ")", end
    text, end = scan(command, i + 2, (), True)
    return "$(" + text + ")", end


def expansions(body):
    """`: $(..) ..;`, the substitutions an unquoted here-document BODY runs."""
    out, i = [":"], 0
    while i < len(body):
        if body.startswith("$(", i) or body[i] == "`":
            text, i = substitution(body, i, None)
            out.append(text)
        else:
            i += 2 if body[i] == "\\" else 1
    return " ".join(out) + ";"


def delimiter(command, i):
    """(word, quoted, end) for the here-document delimiter at I; a quote or
    backslash anywhere in it makes the body text."""
    while i < len(command) and command[i] in " \t":
        i += 1
    word, quoted = [], False
    while i < len(command) and not command[i].isspace() and command[i] not in ";&|<>()":
        if command[i] in "'\"":
            end = command.find(command[i], i + 1)
            end = len(command) if end < 0 else end
            word.append(command[i + 1:end])
            i, quoted = end + 1, True
        elif command[i] == "\\":
            word.append(command[i + 1:i + 2])
            i, quoted = i + 2, True
        else:
            word.append(command[i])
            i += 1
    return "".join(word), quoted, i


def digits(text):
    """True for a run of ASCII digits; bash reads no other digits as a number."""
    return text.isascii() and text.isdigit()


def hide(char, literal=""):
    return chr(HIDDEN + ord(char)) if char in OPERATOR or char in literal else char


def word(token):
    """TOKEN as a word: quoted operators come back, and MARK stays only on a
    quoted reserved word, which the shell runs as a command."""
    text = token.translate(UNHIDE)
    return text + MARK if MARK in token and text in RESERVED else text


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
    try:
        for token in lexer:
            tokens += CONTROL.findall(token) if is_operator(token) else [token]
    except ValueError as error:
        raise Unsplittable(error) from error
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
                if words and digits(words[-1]):
                    words.pop()
                if target and not (token.endswith("&") and (digits(target) or target == "-")):
                    targets.append(word(target))
        elif is_operator(token):
            if words or targets:
                out.append(("cmd", words, targets))
            out.append(("op", token))
            words, targets = [], []
        else:
            words.append(word(token))
        i += 1
    if words or targets:
        out.append(("cmd", words, targets))
    return out


def unwrap(words):
    """(the command, forked, handed): FORKED when a wrapper runs it in a child
    process; HANDED the `NAME=value` words before it, or after `env`, which
    the command sees set.

    A child's cd, exit, assignment or loop jump leaves this shell as it was.
    xargs adds words from its input, unknown, at the end or in place of its
    `-I`, `-J` or `-i` string (sd:6153).
    """
    forked, handed = False, []
    while words:
        wrapper = os.path.basename(words[0]).replace(MARK, "")
        if ASSIGNMENT.match(words[0]):
            handed, words = handed + words[:1], words[1:]
            continue
        if wrapper not in PREFIXES and wrapper not in FORKS:
            break
        # a path or a quoted `time` names a program, a child, not `command`,
        # `builtin` or `time`
        forked = forked or wrapper in FORKS or "/" in words[0] or MARK in words[0]
        words, replace = words[1:], None
        while words and words[0].startswith("-") and words[0] != "--":
            if wrapper == "command" and words[0] in ("-v", "-V"):
                return [], forked, handed  # prints what would run; runs nothing
            if wrapper == "xargs" and words[0][:2] in ("-I", "-J"):
                replace = words[0][2:] or (words[1:2] or [""])[0]
            elif wrapper == "xargs" and words[0][:2] == "-i":
                replace = words[0][2:] or "{}"
            words = words[2:] if words[0] in VALUED.get(wrapper, ()) else words[1:]
        words = words[1 if words[:1] == ["--"] else 0:]
        words = words[1:] if wrapper == "timeout" else words  # the duration
        if wrapper == "xargs" and words:
            words = [text.replace(replace, UNKNOWN) for text in words] if replace else \
                words + [UNKNOWN]
    return words, forked, handed


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
        return places(rest, variables, spend)
    if head == "shift":
        counts = {int(text) if digits(text) else None
                  for text in (expand(words[1], variables, spend) if words[1:] else ["1"])}
        known = max((int(name) for name in variables if digits(name) and name != "0"), default=0)
        return {str(i): frozenset().union(*(variables.get(str(i + count), frozenset([""]))
                                            if count is not None else unknown for count in counts))
                for i in range(1, known + 1)}
    if head in DECLARERS:
        options = "".join(word[1:] for word in words[1:] if word[:1] in "-+")
        words = [word for word in words[1:] if word[:1] not in "-+"]
        if head != "export" or "n" in options:
            # the reader does not follow what a declaration does to a name (a
            # readonly one makes a later assignment fail): each name it
            # touches is unknown, and every name after a bare `readonly`
            # (lead ruling in review of 86680cc0, as on sd:6083)
            names = [NAME.match(word).group() for word in words if NAME.match(word)]
            if not names and (head == "readonly" or "r" in options):
                return {name: unknown for name in [*variables, "*"]}
            return {name: unknown for name in names}
    elif not all(ASSIGNMENT.match(word) for word in words):
        return {}
    incoming, variables, changes = variables, dict(variables), {}
    for word in words:
        match = ASSIGNMENT.match(word)
        if not match:
            continue
        name = NAME.match(word).group()
        if match.group(1):  # an array element
            variables[name] = changes[name] = unknown
            continue
        text = (("${%s}" % name if match.group().endswith("+=") else "")
                + tilde(word[match.end():], assignment=True))
        values = expand(text, variables, spend)
        if head == "export":  # bash expands every argument first; zsh in turn (sd:6153)
            values += expand(text, incoming, spend)
        variables[name] = changes[name] = frozenset(os.path.expanduser(value) for value in values)
    return changes


def places(words, variables, spend, first=1):
    """`$1`.. (from FIRST) as WORDS set them. A word that may make more or
    fewer than one field (braces, a split, an empty or unknown value, a
    glob) moves every later place, so those are unknown (lead review of
    cd702481)."""
    out = {}
    for n, word in enumerate(words, first):
        values = expand(tilde(word), variables, spend)
        if len(braces(word, spend)) > 1 or any(
                len(value.split()) != 1 or GLOB.search(value) or UNKNOWN in value
                for value in values):
            return {**out, **{str(k): frozenset([UNKNOWN])
                              for k in range(n, first + len(words) + 9)}}
        out[str(n)] = frozenset(values)
    return out


def sticks(words, changes, variables):
    """True when the assignment WORDS make cannot fail (sd:6270).

    A plain assignment fails only on a readonly name. The reader cannot see
    which names the incoming shell holds readonly, so a name it may hold
    (its environment, set earlier, or anything after `source`) may fail; one
    it does not hold, set to a known value, sticks. Another declaration,
    an option, a read and the other builtins may fail.
    """
    head = words[0]
    if head != "export" and not ASSIGNMENT.match(head):
        return False
    if head == "export" and any(word[:1] in "-+" for word in words[1:]):
        return False
    return "*" not in variables and all(
        name not in variables and name not in READONLY and not any(UNKNOWN in value
                                                                   for value in values)
        for name, values in changes.items())


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

    def __init__(self, command, protected, spend=None):
        self.protected, self.i, self.skip = protected, 0, 0
        self.spend, self.inherited, self.loops = spend or Budget(), environment(), []
        try:
            self.items = items(command)
        except RecursionError:  # `$( )` nested past the stack: nothing reads
            self.items = []
            self.spend.note("it nests deeper than the reader goes")

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
        if any(DEFAULTED.search(word) for word in words):
            self.spend.note("a `${D:=w}` in a header sets D, and the reader does not follow that")
        return words

    def check(self, path, world):
        root = written(path, world.cwd, self.protected, world.variables(self.inherited),
                       self.spend)
        if root == UNKNOWN:
            self.spend.note("it writes to a path it cannot know")
        elif root:
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
        self.lead()
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
        if item[1][:1] == ["function"] or len(item[1]) == 1 and not item[2] and \
                FUNCTION.fullmatch(item[1][0]) and not ASSIGNMENT.match(item[1][0]) and \
                self.items[self.i + 1:self.i + 3] == [("op", "("), ("op", ")")]:
            return self.define(worlds)
        if item[1] and item[1][0] in KEYWORDS:
            self.keyword(item[1][0])  # a reserved word out of place: read past it
            return worlds
        return self.simple(worlds)

    def lead(self):
        """Read past `time [-p]` or `coproc [NAME]` before a compound command:
        the compound runs as bare, a coprocess in a child (sd:6153)."""
        item = self.peek()
        words = item[1] if item[0] == "cmd" else []
        if words[:1] == ["time"]:
            n = 2 if words[1:2] == ["-p"] else 1
        elif words[:1] == ["coproc"]:
            n = 2 if words[1:2] and words[1] not in COMPOUND else 1
        else:
            return
        if words[n:n + 1] and words[n] in COMPOUND:
            self.skip += n

    def define(self, worlds):
        """`NAME () BODY` or `function NAME [()] BODY` is unknown: the reader
        does not follow functions, so near a root it denies (lead ruling in
        review of 5a1f993f, as on sd:6083)."""
        self.spend.note("it defines a function, and the reader does not follow functions")
        if self.keyword("function"):
            item = self.peek()
            self.keyword(item[1][0] if item[0] == "cmd" and item[1] else "")
        else:
            self.advance()
        if self.op("("):
            self.op(")")
        self.newlines()
        self.command(set())  # read past the body, running it in no world
        return worlds

    def child(self, command, world, variables, handed=()):
        """Read the script of `sh -c SCRIPT [NAME ARGS..]` in a child shell
        (sd:6153): its writes count; its `cd` and variables stay there. The
        child holds a name only if it was exported, so each set name may also
        be unset there; a HANDED name is set."""
        words, i, script = command, 1, False
        while i < len(words) and words[i][:1] in "-+" and words[i] not in ("-", "--"):
            flag, i = words[i], i + 1
            if flag.startswith("--"):
                i += flag in ("--rcfile", "--init-file")
                continue
            script = script or "c" in flag
            i += sum(char in "oO" for char in flag[1:])  # -o NAME, -O NAME
        i += words[i:i + 1] == ["--"]
        if not script or i >= len(words):
            return
        exported = {NAME.match(word).group(0) for word in handed}
        changes = {name: values if name in exported else values | {None}
                   for name, values in world.changes if name != "*"}
        changes.update({str(n): frozenset([""]) for n in range(10)})
        changes.update(places(words[i + 1:], variables, self.spend, first=0))
        inner = world._replace(changes=tuple(sorted(changes.items(), key=lambda item: item[0])))
        if "*" in dict(world.changes):
            inner = inner.set({"*": dict(world.changes)["*"]})
        for text in expand(words[i], variables, self.spend):
            self.nested(text.translate(LITERAL), {inner}, child=True)

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
        """A loop body runs zero, one or two times; a third pass reads like the
        second, unless the second still changed something: that is unknown."""
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
        first = set()
        for _ in range(2):
            first, out = out, set(out)
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
        return with_status(out | self.widened(first, out), {True, False})

    def widened(self, first, out):
        """Worlds a third pass may reach (sd:6153 round 8): a variable the second
        pass still changed may also hold an unknown, and a folder only the
        second pass reached may lead to one the reader cannot know."""
        before = {(world.cwd, world.stack): dict(world.changes)
                  for world in merged(first, self.inherited)}
        extra = set()
        for world in merged(out, self.inherited):
            old = before.get((world.cwd, world.stack))
            if old is None:
                extra.add(world.moved(None, ()))
                continue
            grown = {name: values | {UNKNOWN} for name, values in world.changes
                     if old.get(name) != values}
            if grown:
                extra.add(world.set(grown))
        return extra

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

    def nested(self, command, worlds, child=False):
        """Read the text `eval` runs in this shell, or a CHILD shell runs; text
        that cannot split changes nothing."""
        try:
            reader = Reader(command, self.protected, self.spend)
        except ValueError:
            return with_status(worlds, {True, False})
        reader.inherited = self.inherited
        if not child:  # a child knows no loop of this shell
            reader.loops = self.loops
        return reader.run(worlds)

    def simple(self, worlds):
        _, words, targets = self.peek()
        self.advance()
        while self.op("("):  # $( ), <( ) and >( ) run in a subshell inside this command
            if words and ASSIGNMENT.fullmatch(words[-1]):  # NAME=( ) is an array
                while self.peek()[0] != "end" and not self.op(")"):
                    if self.op("("):  # an element's `$( )` or `<( )` runs
                        self.run(worlds, (")",))
                        self.op(")")
                    else:
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
            if "IFS" in dict(world.changes):  # fields split where the reader does not
                self.spend.note("IFS splits words in a way the reader does not follow")
            names = defaults(words + targets, world.variables(self.inherited), self.spend)
            world = world.set(names) if names else world  # the words expand first
            for target in targets:
                self.check(target, world)
            command, forked, handed = unwrap(words)
            head = command[0] if command else ""
            if VARIABLE.search(head) or UNKNOWN in head:
                # the name comes from an expansion: each value it may take, split
                for text in expand(head, world.variables(self.inherited), self.spend):
                    found, child, more = unwrap(text.split() + command[1:])
                    out |= self.effect(world, found, forked or child, words, handed + more)
            else:
                out |= self.effect(world, command, forked, words, handed)
        return out

    def effect(self, world, command, forked, words, handed=()):
        """The worlds COMMAND leaves WORLD in; FORKED when it runs in a child.
        HANDED are the `NAME=value` words before it, which a child shell and
        `eval` read set."""
        name = os.path.basename(command[0]) if command else ""
        args = [word for word in command[1:] if not word.startswith("-")]
        # a builtin's effect reaches this shell; a path names a program
        here, out = not forked and "/" not in (command or [""])[0], set()
        # every change may fail (a readonly variable, a missing folder, a bad
        # word): the world it started in stays beside the changed one
        if here and name in DECLARERS - {"export"} and any("n" in word[1:] for word in command[1:]
                                                  if word[:1] in "-+"):
            self.spend.note("a name reference moves its writes to another name")
        if here:
            variables = world.variables(self.inherited)
            changes = assigned(command or words, variables, self.spend)
            if changes and not sticks(command or words, changes, variables):
                out.add(world._replace(ok=False))
            world = world.set(changes) if changes else world
        variables = world.variables(self.inherited)
        seen = world.set(assigned(handed, variables, self.spend)) if handed else world
        if name in ("cd", "pushd", "popd") and here:
            moves = self.moves(name, command[1:], world, variables)
            self.spend.near |= any(folder and inside(folder, self.protected)
                                   for folder, _ in moves)
            return out | {world._replace(ok=False)} | {world.moved(folder, stack)
                                                       for folder, stack in moves}
        if name == "exit" and here:
            return out
        if name == "eval" and here:  # its words expand, then read again
            for text in expand(" ".join(command[1:]), variables, self.spend):
                out |= self.restored(self.nested(text.translate(LITERAL), {seen}), handed, world)
            return out | {world._replace(ok=False)}
        if name == "trap" and here and len(args) > 1:
            # the body runs later, in a folder and with values the reader
            # cannot know, and may run again after each command (DEBUG)
            later = world.moved(None, ())._replace(changes=(("*", frozenset([UNKNOWN])),))
            for text in expand(args[0], variables, self.spend):
                out |= self.nested(text.translate(LITERAL), {later})
        if name in SHELLS:
            self.child(command, seen, seen.variables(self.inherited), handed)
        if name in ("break", "continue") and here and self.loops:
            self.loops[-1] |= with_status({world}, {True, False})
        if UNKNOWN in name:  # any command: it may write each word
            for path in command[1:]:
                self.check(path, world)
        if (UNKNOWN in name or name in ("source", ".")) and here:  # it may change anything
            # every name, set before or not, may hold anything after it
            out.add(world.moved(None, ())._replace(changes=(("*", frozenset([UNKNOWN])),)))
        elif name == "git":
            folders, verb = git_target(command, world.cwd, variables, self.spend)
            for folder in folders if verb in GIT_WRITES else ():
                if folder is None:
                    self.spend.note("git writes in a folder it cannot know")
                    continue
                root = inside(folder, self.protected)
                if root:
                    raise Denied(root)
        elif name in PATH_WRITERS or (name in LAST_ARG_WRITERS and args):
            for path in args if name in PATH_WRITERS else args[-1:]:
                self.check(path, world)
        return out | with_status({world}, {True, False})

    def restored(self, worlds, handed, before):
        """WORLDS after a command that saw HANDED set: bash puts each name
        back as it was BEFORE, and POSIX mode keeps it, so it may hold both."""
        if not handed:
            return worlds
        old = before.variables(self.inherited)
        names = {NAME.match(word).group(0) for word in handed}
        out = set()
        for world in worlds:
            now = world.variables(self.inherited)
            out.add(world.set({name: frozenset(now.get(name, now.get("*", {None})))
                               | frozenset(old.get(name, old.get("*", {None}))) for name in names}))
        return out

    def moves(self, name, words, world, variables):
        """(folder, stack) pairs a `cd`, `pushd` or `popd` that works may leave.

        None is a folder the reader cannot know: past the known stack, a stack
        index (`+1`, `-1`, which zsh's `cd` takes too), or an unknown word.
        """
        options = [word for word in words if word.startswith("-") and word not in ("-", "--")]
        args = [word for word in words if word not in options and word != "--"]
        stack = world.stack
        if any(re.fullmatch(r"[+-][0-9]+", word) for word in words):
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
    """The root COMMAND writes in, or None.

    What the reader cannot know refuses only near a root: when a world's
    folder was in one, or the text names one. Elsewhere it passes (operator
    ruling on sd:6083).
    """
    spend = Budget()
    spend.near = inside(cwd, protected) is not None
    try:
        Reader(command, protected, spend).run(
            {World(cwd, (("OLDPWD", frozenset([UNKNOWN])), ("PWD", frozenset([cwd]))), True)})
    except Denied as denied:
        return denied.args[0]
    except Unsplittable:  # fails open, as malformed input does
        raise
    except Unreadable as error:  # past the budget: the rest goes unread
        spend.note(str(error))
    except RecursionError:
        spend.note("it nests deeper than the reader goes")
    except Exception as error:  # a reader fault is an unknown, not a pass (sd:6153)
        spend.note(f"the reader failed on it ({type(error).__name__})")
    if spend.unknown and (spend.near or named(command, protected)):
        raise Unreadable(spend.unknown)
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
        path = tool_input[FILE_TOOLS[name]]
        try:
            root = written(path, cwd, protected)
        except Exception:  # a reader fault is an unknown, not a pass (sd:6153)
            root = UNKNOWN
        return (inside(cwd, protected) or named(path, protected)) if root == UNKNOWN else root
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
