"""`sd-plan.sh item` — plan one row, standing in its checkout.

Run through the entrypoint, never directly: it is what pins an interpreter new
enough for `sd_db`, and on the runner's bare PATH the one a shebang would find
is not. `sd_db` is whatever that interpreter has installed -- the pack's
provisioned virtualenv on a machine, CI's venv in CI -- and the row that owns
the folder is made by the pack's `sd work register`, run under the same one.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import system_tools_config  # noqa: E402

try:
    from sd_db import connect, reads, repos, runner_controls, runner_exec
    from sd_db.repos import same_remote
    from sd_db.runner import RunnerRefused
    from sd_db.database import default_path
    from sd_db.workflow import MissingItem, WorkflowError, item_state
except ImportError as missing:
    # Nothing puts a source tree on `PYTHONPATH` in front of this any more, so
    # an interpreter without the library is a machine to fix -- said in a
    # sentence, not three imports deep.
    raise SystemExit(
        f"sd-plan: {sys.executable} cannot import sd_db ({missing}); set PYTHON to "
        f"an interpreter the pack's installer provisioned") from None

#: What `/sd-plan` writes. All three, or the run did not do its job.
DOCUMENTS = ("prd.md", "design.md", "implement.md")

#: `sd-docs-lint`'s own ITEM_DIR_RE: a date, then lowercase alphanumeric
#: groups joined by single hyphens. A folder it cannot parse is a folder it
#: does not check, so the slug is built to satisfy this rather than trimmed
#: until it happens to.
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: Long enough to stay readable, short enough to live in a branch name. The
#: three folders written by hand run 24 to 31 characters.
SLUG_LIMIT = 48


class Refused(Exception):
    """A reason to stop that is the caller's to fix, printed without a traceback."""


def slugify(title: str) -> str:
    """A title as a folder name the lint will parse.

    Truncation is on a hyphen boundary rather than mid-word: `...rule-6-che`
    reads as a typo, and the cut is arbitrary either way, so it may as well
    fall where a reader expects one. The exception is a first word longer
    than `SLUG_LIMIT`: no boundary falls inside the limit, so that word is
    cut (sd:1181). It is kept, not refused, because a changed rule renames
    the folder and branch of every row it reaches, and the nightly then
    plans those rows again.
    """
    flattened = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if len(flattened) > SLUG_LIMIT:
        head = flattened[:SLUG_LIMIT]
        flattened = head.rsplit("-", 1)[0] if "-" in head else head
    flattened = flattened.strip("-")
    if not SLUG_RE.match(flattened):
        raise Refused(f"{title!r} yields no folder name the lint can parse")
    return flattened


def created_day(value) -> str | None:
    """The `YYYY-MM-DD` a row was created on, or `None` when it names no day.

    Parsed, not shape-matched: `2026-02-30` has the shape and is no date, and
    a folder named after it would sort and read as one (sd:1181).
    """
    text = str(value or "")[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def git(root: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False,
    )
    if check and done.returncode != 0:
        raise Refused(f"git {' '.join(args)}: {(done.stderr or done.stdout).strip()}")
    return done.stdout.strip()


def repository_root() -> Path:
    done = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False,
    )
    if done.returncode != 0:
        raise Refused(f"{Path.cwd()} is not inside a git repository")
    return Path(done.stdout.strip()).resolve()


def read_row(connection, identifier: int) -> dict:
    try:
        row = dict(item_state(connection, identifier)["item"])
    except MissingItem:
        raise Refused(f"no item {identifier}") from None
    found = connection.execute(
        "SELECT remote FROM repo WHERE path = ?", (row["repo"],)).fetchone()
    row["remote"] = found["remote"] if found else None
    return row


def is_the_rows_repository(root: Path, row: dict) -> bool:
    """True standing in the row's own checkout, or in a clone of it.

    R10-D6 is about *which repository*, and a path comparison answers a
    narrower question than that. The runner clones from the repo's remote and
    works at `/Volumes/sd-work/worktrees/<item>/<run>`, so a path check
    refuses the runner every time -- which is exactly how the first real
    end-to-end run failed, after the command had already been queued,
    dispatched and cloned. The remote is the identity; the path is where a
    copy happens to sit.
    """
    if paths.same(row["repo"], root):  # a `~/...` key, not a path (sd:1439)
        return True
    return same_remote(row.get("remote"), git(root, "remote", "get-url", "origin", check=False))


def plan(arguments) -> int:
    root = repository_root()
    connection = connect(default_path())
    try:
        row = read_row(connection, arguments.item)
    finally:
        connection.close()

    # R10-D6, checked rather than trusted. A row planned from a checkout it
    # does not belong to writes its folder into the wrong repository, and the
    # `path` the registration records then names a file nobody standing here
    # can read.
    if not row["repo"]:
        raise Refused(f"item {row['id']} has no repository; `sd task edit` one on first")
    if not is_the_rows_repository(root, row):
        raise Refused(
            f"item {row['id']} belongs to {row['repo']}, and this is {root}, "
            f"which is not it and is not a clone of it; plan it from its own checkout"
        )
    if row["status"] in {"done", "ready_to_send"}:
        raise Refused(f"item {row['id']} is {row['status']}; there is nothing left to plan")

    created = created_day(row["created_at"])
    if created is None:
        raise Refused(f"item {row['id']} has no usable created date ({row['created_at']!r})")
    slug = f"{created}-{slugify(str(row['title']))}"
    folder = root / "docs" / "work" / slug
    relative = f"docs/work/{slug}/prd.md"

    # The branch is the row's when it has one, and in a runner clone it always
    # does: `runner.py:_item` refuses to enqueue an item without a valid
    # branch, and the clone's pre-push hook accepts pushes to that branch
    # alone. The derive below therefore only ever fires for a run by hand.
    branch = row["branch"] or f"plan/{slug}"

    # Planned means all of it: the folder written, its row made, and nothing
    # under it left uncommitted. A folder without a row is what a refused
    # registration leaves, and answering "nothing to do" to it would leave it
    # without a status for good -- so it is finished instead, without a
    # second planning run.
    #
    # Pushed is part of it too. The push comes last, after the row and the
    # commit, so a push that failed leaves both behind; a retry standing on
    # the branch finishes that push rather than calling the folder done
    # (sd:1181). Only that push: the one commit origin lacks must be this
    # run's planning commit, and the tree must be clean. Anything else on the
    # branch -- implementation work, say -- is not this verb's to publish, and
    # the retry stays the no-op it always was.
    written = (folder / "prd.md").is_file()
    pending = git(root, "status", "--porcelain=v1", "--untracked-files=all",
                  "--", f"docs/work/{slug}")
    if written and not pending and has_row(row["repo"], relative):
        commit = unpushed_plan(root, branch, slug, row["id"])
        if commit is None:
            print(f"sd-plan: {slug} is already planned; nothing to do")
            return 0
        if git(root, "status", "--porcelain=v1", "--untracked-files=all"):
            raise Refused(f"{slug} is planned and its commit {commit[:12]} is not pushed, "
                          "and the checkout has uncommitted changes; pushing it needs a clean tree")
        if arguments.dry_run:
            print(f"sd-plan: would push item {row['id']} as {slug} to {branch}")
            return 0
        push(root, branch)
        print(f"sd-plan: {slug} was planned and not pushed; pushed to {branch}")
        return 0

    if arguments.dry_run:
        verb = "finish" if written else "plan"
        print(f"sd-plan: would {verb} item {row['id']} as {slug} on {branch}")
        return 0

    pack = pack_entrypoint()

    # The item's own folder is the one exception: it is what a refused
    # registration leaves uncommitted, and the retry is what commits it.
    if git(root, "status", "--porcelain=v1", "--untracked-files=all",
           "--", ".", f":(exclude)docs/work/{slug}"):
        raise Refused("the checkout has uncommitted changes; a planning run needs a clean tree")

    current = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if current != branch:
        exists = git(root, "rev-parse", "--verify", "--quiet", branch, check=False)
        git(root, "checkout", "-q", *(([branch]) if exists else ["-b", branch]))

    # The planning run, and the optional judgment that can narrow it, below.
    required, probabilities = run_planning(root, folder, slug, row, written)

    missing = [name for name in required if not (folder / name).is_file()]
    if missing:
        raise Refused(
            f"the planning run left {slug} without {', '.join(missing)}; "
            f"nothing is registered and nothing is pushed"
        )

    # Registered before it is committed, so a refusal leaves no commit to
    # publish. `sd_plan` then pushes nothing, and the runner keeps a clone
    # with uncommitted work rather than pushing it
    # (`gitops.dirty(clone)`, in `_finish` of `local-sd-runner/sd_runner/runtime.py`).
    register(root, relative, pack)

    # The pathspec, not the index: the agent ran after the dirty-tree check,
    # and anything it staged would otherwise ride this commit and be pushed.
    git(root, "add", "--", f"docs/work/{slug}")
    if git(root, "status", "--porcelain=v1", "--", f"docs/work/{slug}"):
        # The judgment rides the commit when there was one: a folder with no
        # design.md is a claim somebody will want the reason for, and this
        # run's stdout is a log nobody keeps.
        message = f"docs(work): {slug}\n\nWork: sd:{row['id']}"
        if probabilities:
            message += f"\n\n{judgment_note(probabilities)}"
        git(root, "commit", "-q", "-m", message, "--", f"docs/work/{slug}")

    push(root, branch)
    print(f"sd-plan: planned item {row['id']} as {slug}, pushed to {branch}")
    return 0


def push(root: Path, branch: str) -> None:
    git(root, "push", "-q", "--set-upstream", "origin", f"HEAD:refs/heads/{branch}")


def unpushed_plan(root: Path, branch: str, slug: str, identifier: int) -> str | None:
    """The planning commit a failed push left behind, or `None`.

    `branch` must be checked out, and `HEAD` must be the one commit no
    `origin` ref reaches. That commit must be the one `plan` writes: its
    subject `docs(work): <slug>`, its `Work: sd:<id>` line, and no path
    outside the item's folder. A second unpushed commit, or one that is
    anything else, is somebody's work and `None`.
    """
    if git(root, "rev-parse", "--abbrev-ref", "HEAD", check=False) != branch:
        return None
    commits = git(root, "rev-list", "HEAD", "--not", "--remotes=origin", check=False).split()
    if len(commits) != 1:
        return None
    commit = commits[0]
    message = git(root, "log", "-1", "--format=%B", commit, check=False).splitlines()
    if not message or message[0] != f"docs(work): {slug}" or f"Work: sd:{identifier}" not in message:
        return None
    changed = git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", commit,
                  check=False).splitlines()
    if not changed or any(not name.startswith(f"docs/work/{slug}/") for name in changed):
        return None
    return commit


# --- the optional Jev judgment ----------------------------------------------
#
# All of it below `plan`, including the one import this section adds.

import json

#: The one document a judgment may never drop, and the state every question
#: below is asked about.
PRD = DOCUMENTS[0]

#: This stage's own switch, read by `jev enabled JEV_SD_PLAN` rather than here.
#: Unset means on: a run asks Jev which of the other two passes this item
#: needs. Set it to `0`, `off`, `false`, `no` or `disabled` (any case) and the
#: run writes all three, exactly as this tool always did. It only ever
#: subtracts -- it cannot switch this stage on against `jev off` or a machine
#: with no key.
JEV_STAGE = "JEV_SD_PLAN"

#: Who this is in the judgment ledger. The folder name, which the ledger's
#: identifier grammar accepts as it stands; a name it refuses is dropped to
#: `unknown`, and a stage whose rows are all `unknown` compares nothing.
JEV_CALLER = "local-sd-plan"

#: Plan the pass unless Jev is fairly sure it is unnecessary. Deliberately
#: under a half: today's answer is all three, an unwanted document costs a
#: read, and a missing one costs the pass nobody made.
JEV_GATE = 0.35

#: One request, two questions. A design pass and an implement pass are two
#: decisions -- an item can plainly need the second without the first -- and
#: `ask` runs the questions of one request in parallel, which is what makes
#: two of them cost about what one costs. They cannot see each other's
#: answers, and neither needs to: both are about the same prd.
JEV_QUESTIONS = {
    "design": {
        "type": "noul",
        "instructions": (
            "The state is the product requirements document for one unit of work "
            "in a small personal infrastructure repository. A design pass is a "
            "separate document written before anything is built, settling the "
            "shape: the interfaces, the data, the failure modes, and the "
            "alternatives that were rejected and why. Does this item need one? "
            "It does when the document leaves a real choice of shape open, when "
            "more than one component has to agree on something, or when getting "
            "the shape wrong would be expensive to undo. It does not when the "
            "requirements already fix the shape closely enough to build from."
        ),
    },
    "implement": {
        "type": "noul",
        "instructions": (
            "The state is the product requirements document for one unit of work "
            "in a small personal infrastructure repository. An implementation "
            "pass is a separate document written before the work starts, "
            "sequencing the build: the steps and their order, the files each one "
            "touches, and the check that says each step landed. Does this item "
            "need one? It does when the work lands in several steps, touches "
            "several places, has to stay working between the steps, or needs a "
            "migration or a rollout order. It does not when the change is one "
            "edit in one place with an obvious check."
        ),
    },
}

#: Which document each question decides.
JEV_DOCUMENT = {"design": "design.md", "implement": "implement.md"}

#: Long enough for `ask`'s own retries (`JEV_TIMEOUT` x `JEV_RETRIES`), short
#: enough that an endpoint which has stopped answering cannot hold a nightly
#: planning run open. Expiring here is a declined judgment, not a failure.
JEV_SECONDS = 150


def run_planning(root: Path, folder: Path, slug: str, row: dict, written: bool):
    """Write the documents, and answer which ones this item was to get.

    Returns `(required, probabilities)`: what the folder is held to, and the
    judgment behind it, `None` whenever Jev was not asked or could not answer.
    `None` means `DOCUMENTS`, which is what this did before there was a
    judgment and what it still does wherever Jev cannot answer or the stage
    is switched off.

    Two agent runs when the judgment is taken and one otherwise, because the
    question is about the prd's own text: it cannot be asked before a prd
    exists, so the prd is written alone first and whatever the judgment keeps
    is asked for second.
    """
    jev_run()
    consulted = jev_consulted()
    if not written:
        agent(root, slug, row, (PRD,) if consulted else DOCUMENTS)
    if not consulted:
        return DOCUMENTS, None

    probabilities = (consult_jev(folder / PRD, jev_subject(row))
                     if (folder / PRD).is_file() else None)
    required = judged_documents(probabilities)
    note = f" -- {judgment_note(probabilities)}" if probabilities else ""
    print(f"sd-plan: {slug} plans {', '.join(required)}{note}")
    # The second run writes what the judgment kept. Reached only when a
    # judgment was taken: a run without one asks the agent once, and a document
    # that run did not write is a refusal, exactly as it was before.
    if not written and [name for name in required if not (folder / name).is_file()]:
        agent(root, slug, row, required)
    return required, probabilities


def jev_argv(*arguments: str) -> list[str]:
    """Jev by path, resolved from this folder, never by name.

    `local-bin-links` puts `jev` on a login shell's PATH, and this runs under
    no shell at all: the runner executes it with a bare PATH, the same
    environment that made `python3` resolve to Xcode's 3.9 and `claude`
    resolve to nothing at all (`claude_binary`). The sibling entrypoint is
    where it actually is. `SD_PLAN_JEV` replaces the argv so the suite can
    answer for Jev without a network or a key.
    """
    override = os.environ.get("SD_PLAN_JEV")
    if override:
        return [*shlex.split(override), *arguments]
    return [str(Path(__file__).resolve().parent.parent / "local-jev" / "jev.sh"),
            *arguments]


def jev_subject(row: dict) -> str:
    """The ledger's name for this item's judgment: `sd-plan:sd-<id>` (sd:2953).

    The tracker's own number, so an outcome -- which passes the item really
    needed -- joins the row by the item id alone, with nothing to recompute.
    """
    return f"sd-plan:sd-{row['id']}"


def jev_run() -> str:
    """This run's `JEV_RUN`, inherited by every `jev` call it makes (sd:2953).

    The one the run inherited, else `sd-plan-<UTC yyyymmddThhmmss>-<4 hex>`,
    set in this process's environment so every child sees the same one.
    """
    if not os.environ.get("JEV_RUN"):
        os.environ["JEV_RUN"] = (f"sd-plan-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}"
                                 f"-{secrets.token_hex(2)}")
    return os.environ["JEV_RUN"]


def declined(reason: str) -> None:
    """Say why the judgment did not happen, and carry on with all three.

    Loud on purpose, and never fatal: `local-jev/README.md` makes the point
    that a lane which quietly stops running is the bug this shape exists to
    avoid. A planning run that lost its judgment still plans.
    """
    print(f"sd-plan: jev {reason}; planning {', '.join(DOCUMENTS)}", file=sys.stderr)
    return None


def record_baseline(cause: str | None = None, subject: str | None = None) -> None:
    """Say all three documents -- the control arm -- are what got planned.

    `--outcome ok` always: the old path completed, and that this row exists at
    all is the fact nothing else carries.

    `cause` is the word only when `jev` never wrote its own row: the caller
    killed it on a deadline, or it could not be started. `jev` flushes its
    measurement after the answer is printed and installs no signal handler, so
    a call ended by SIGTERM leaves nothing behind and this row is the only
    record there will be. When `jev` did return -- a non-zero exit, an answer
    this cannot read -- it has already written the cause, and repeating it
    here counts one decision as two: `judgment.py`'s DECLINES groups by stage
    and cause across both arms with no deduplication.

    Bookkeeping only: `jev record` sends nothing, needs no key, prints
    nothing and always exits 0. Every failure is swallowed, because a
    planning run must never fail over its own measurement.
    """
    try:
        subprocess.run(
            jev_argv("record", "--caller", JEV_CALLER, "--stage", JEV_STAGE,
                     "--arm", "baseline", "--outcome", "ok",
                     *(("--decline", cause) if cause else ()),
                     *(("--subject", subject) if subject else ())),
            capture_output=True, stdin=subprocess.DEVNULL, check=False,
            timeout=30,
        )
    except Exception:  # noqa: BLE001 - bookkeeping may never fail the caller
        pass


def jev_consulted() -> bool:
    """Whether this run asks Jev at all -- on here, and able to answer here.

    Both halves are settled before the prd is written, because the judgment
    splits the planning run in two and a machine where Jev cannot answer must
    be found out before the first run rather than between them. `jev enabled`
    costs nothing and calls nothing, so asking it here is free, and a machine
    with no `TYPESAFE_API_KEY` answers it exactly like one with the switch
    off.
    """
    try:
        # Both halves in one call: Jev can answer on this machine, and
        # JEV_SD_PLAN has not been used to switch this stage off. Unset means
        # on -- a per-caller switch defaulting to off makes every integration
        # added after it silently never run.
        # stdin closed: `enabled` reads none, but a command that inherits an
        # open stdin and happens to read it waits out the timeout instead of
        # answering, and this runs inside other people's pipelines.
        # `--record` so the decline is counted: a planning run that never
        # asked still planned all three documents, and that is the control
        # arm's only fact.
        done = subprocess.run(jev_argv("enabled", JEV_STAGE, "--record",
                                       "--caller", JEV_CALLER),
                              capture_output=True,
                              stdin=subprocess.DEVNULL, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError) as failure:
        declined(f"could not be asked whether it is enabled ({failure})")
        return False
    if done.returncode != 0:
        declined("is switched off, unkeyed or unreachable on this machine")
        return False
    return True


def consult_jev(prd: Path, subject: str | None = None) -> dict | None:
    """The two probabilities for one prd, or `None` for every way to miss.

    Off, unkeyed, failing, timed out, and answering something this cannot
    read all come back the same way, and `judged_documents` turns `None` into
    today's answer. The prd's text is the whole state: it is what the
    questions are about, and it is the only thing this sends.
    """
    try:
        done = subprocess.run(
            jev_argv("ask", "--questions", "-", "--state", str(prd),
                     "--caller", JEV_CALLER, "--stage", JEV_STAGE,
                     *(("--subject", subject) if subject else ())),
            input=json.dumps(JEV_QUESTIONS), capture_output=True, text=True,
            check=False, timeout=JEV_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as failure:
        # `timeout=JEV_SECONDS` above kills it, and a killed `jev` never
        # reached its flush, so this row is the only record the run will have.
        record_baseline("timeout"
                        if isinstance(failure, subprocess.TimeoutExpired)
                        else "unavailable", subject)
        return declined(f"could not be run ({failure})")
    if done.returncode != 0:
        record_baseline(subject=subject)
        return declined(f"exited {done.returncode}: {done.stderr.strip()[:200]}")
    try:
        answers = json.loads(done.stdout)["answers"]
        return {key: float(answers[key]["noul"]) for key in JEV_QUESTIONS}
    except (ValueError, KeyError, TypeError) as unreadable:
        record_baseline(subject=subject)
        return declined(f"answered something this cannot read ({unreadable})")


def judged_documents(probabilities: dict | None) -> tuple[str, ...]:
    """What the run is held to. `None` is today's answer, and so is no judgment."""
    if probabilities is None:
        return DOCUMENTS
    return (PRD, *(JEV_DOCUMENT[key] for key in ("design", "implement")
                   if probabilities[key] >= JEV_GATE))


def judgment_note(probabilities: dict) -> str:
    """The judgment in one line, for the run's output and for its commit.

    The numbers are kept and not only the verdict: a skipped pass is a claim
    about the item, and a reader who disagrees with it can see how close the
    call was without asking Jev the same question again.
    """
    return "Jev: " + ", ".join(
        f"{key} {probabilities[key]:.2f} "
        f"({'planned' if probabilities[key] >= JEV_GATE else 'skipped'})"
        for key in ("design", "implement"))


def has_row(repo: str, relative: str, connection=None) -> bool:
    """Whether the folder already has the row `register_work_item` makes.

    Keyed the way that function keys it, `(source, external_id)`, with the
    repository the row belongs to rather than the clone this runs in.
    """
    if connection is not None:
        return connection.execute(
            "SELECT 1 FROM item WHERE source = 'docs/work' AND external_id = ?",
            (f"{repo}::{relative}",)).fetchone() is not None
    connection = connect(default_path())
    try:
        return has_row(repo, relative, connection)
    finally:
        connection.close()


def override_hint() -> str:
    """Where an override reaches a queued run, said after "set SD_...".

    `runner_exec.process_plan` hands a queued run a fixed environment, so an
    exported value never arrives there. `sd-plan.sh item` sources the tool's
    `.env` from the config folder, which it finds under the `HOME` that
    environment keeps (sd:1181).
    """
    return (f" (export it, or set it in {system_tools_config.config_dir('sd-plan')}/.env, "
            "the one place a queued run reads it)")


def claude_binary() -> str:
    """Where the agent is, not what it is called.

    The runner executes this with a bare `PATH`, the same environment that
    made `python3` resolve to Xcode's 3.9 and cost #249. `claude` is not on
    that `PATH` at all: the install puts it in `~/.local/bin`, which a login
    shell adds and an exec'd command does not inherit. Resolving the bare name
    worked from a terminal and raised `FileNotFoundError` from the queue --
    a traceback, because nothing here was expecting a name to go missing.

    `local-cron-jobs/cron-jobs.sh` (`claude_binary`) resolved the same binary
    the same way and fell back to the bare name; it survived because the
    plist there spells `~/.local/bin` into PATH. This runs under no shell at
    all, so the fallback has to be the install path rather than the name --
    and since sd:456 the shell resolves in this order too, with `CLAUDE_BIN`
    as its explicit override, so the two do not drift apart again.

    The override and the install path are held to what `shutil.which` holds
    a PATH entry to: an executable file, not a directory and not a file
    without its `x` bit. Either would otherwise fail inside `subprocess.run`
    with the traceback this function exists to prevent (sd:1181).
    """
    explicit = os.environ.get("SD_PLAN_CLAUDE")
    if explicit:
        resolved = shutil.which(explicit)
        if resolved is None:
            raise Refused(
                f"SD_PLAN_CLAUDE={explicit} is not an executable file; "
                f"set it to the binary that should do the planning{override_hint()}")
        return resolved
    found = shutil.which("claude")
    if found:
        return found
    installed = Path.home() / ".local" / "bin" / "claude"
    if installed.is_file() and os.access(installed, os.X_OK):
        return str(installed)
    raise Refused(
        "no `claude` on PATH and none at ~/.local/bin/claude; "
        f"set SD_PLAN_CLAUDE to the binary that should do the planning{override_hint()}")


#: What the pages must survive once pushed (sd:990). The first unattended run
#: pushed pages that passed `sd-docs-lint` and failed the pack's citation gate:
#: four `path:line` citations into code and one `source:` locator naming a
#: symbol declared twice. Its log also blamed the gate's other failures on the
#: registered checkout, where the gate passed. One sentence per rule.
CITATION_RULES = (
    "Cite code by anchor only, never as path:line: `source:<path>::<symbol>`, "
    "or a backticked snippet followed by `in <path>`. Cite a symbol that is "
    "declared more than once in its file by the file alone.",
    "Before you finish, run this checkout's citation gate as a whole, from this "
    "checkout: `python3 -m unittest tests.test_doc_citations` in the command "
    "pack. Fix every failure it names on the pages you wrote.",
    "Claim that a failure also happens on the registered checkout only after "
    "reproducing it there, with the command and its output in your log; "
    "otherwise do not claim it.",
)


def agent(root: Path, slug: str, row: dict, documents=DOCUMENTS) -> None:
    """Invoke the planning skill headlessly.

    `SD_PLAN_AGENT` replaces the whole argv so a test can answer for it. The
    real one is the single production pattern this repository has for headless
    invocation: `"$claude_bin" -p "$JOB_PROMPT"`, in `cmd_exec` of
    `local-cron-jobs/cron-jobs.sh`.

    Cited by that snippet and not by a line number. The number this named was
    correct the day it was written and pointed into an unrelated comment by
    the time anyone re-read it: insertions above the target walked the call
    from 418 to 521 while the citation stood still (sd:825, #365 N3). A
    snippet cannot drift that way.

    `/sd-plan` carries `disable-model-invocation: true`, so it must be named
    explicitly -- an agent asked to "write planning documents" will not find
    it by description.

    `documents` is what this run asks for, and it is `DOCUMENTS` unless a Jev
    judgment narrowed it -- in which case the sentence changes, rather than a
    list quietly shrinking inside one that says "all three". The same list
    reaches an `SD_PLAN_AGENT` double as `SD_PLAN_DOCUMENTS`, which is how the
    suite drives a two-run plan without a model.

    The prompt names every file in `DOCUMENTS`, because the skill writes
    `design.md` and `implement.md` only when asked and `plan()` refuses the
    run without them. The one unattended run before this sentence existed
    passed only because the agent read this file and inferred the refusal
    (sd:438; exec note 709 on sd:442). It also says the run is unattended,
    which is the condition under which the skill records routine choices
    on the row and continues instead of asking, and it carries
    `CITATION_RULES`, because nobody reads the pages before they are pushed.
    """
    override = os.environ.get("SD_PLAN_AGENT")
    if tuple(documents) == DOCUMENTS:
        leave = (f"Leave all three documents in docs/work/{slug}/: "
                 f"{', '.join(DOCUMENTS)}.")
    else:
        leave = (f"Leave exactly these documents in docs/work/{slug}/: "
                 f"{', '.join(documents)}. Write no others.")
    prompt = (
        f"/sd-plan {slug} --from sd:{row['id']}\n\n"
        f"This run is unattended: nobody will answer a question, so record "
        f"routine choices on the row and continue. {' '.join(CITATION_RULES)} "
        f"{leave}"
    )
    argv = ([*shlex.split(override), slug, str(row["id"])] if override else
            [claude_binary(), "-p", prompt, "--dangerously-skip-permissions"])
    # The agent reads its keychain credential under `USER`. The runner's
    # `process_plan` sets it from the uid (sd:458), so this passes the
    # environment through and sets none of its own (sd:2557).
    environment = {**os.environ, "SD_PLAN_DOCUMENTS": " ".join(documents)}
    done = subprocess.run(argv, cwd=str(root), check=False, env=environment)
    if done.returncode != 0:
        raise Refused(f"the planning run exited {done.returncode}")


def pack_entrypoint() -> Path:
    """The pack's `sd`, or a refusal naming where it looked.

    `SD_PACK_ROOT` when set, else the checkout path the rest of this
    repository assumes (`local-bin-links/bin-links.sh`,
    `local-cron-jobs/examples/shadow-sync-nightly.job`). An environment variable
    rather than a config file, because those callers already answer to one;
    a queued run, which the runner gives no `SD_PACK_ROOT`, reads it from the
    tool's `.env`, which `sd-plan.sh item` sources (sd:1181).

    Asked before the planning run, not at registration: a pack that is not
    there would otherwise surface only after the agent had spent minutes
    writing documents nothing can register.
    """
    root = Path(os.environ.get("SD_PACK_ROOT")
                or Path.home() / "repos" / "platypeeps" / "sd-ai-command-pack")
    entrypoint = root / "bin" / "sd"
    if not entrypoint.is_file():
        raise Refused(
            f"no pack at {root}: {entrypoint} is not a file, and the folder's row is "
            f"made by its `sd work register`; set SD_PACK_ROOT to the "
            f"sd-ai-command-pack checkout{override_hint()}")
    return entrypoint


def register(root: Path, relative: str, entrypoint: Path) -> None:
    """Give the folder its row, through the pack's `sd work register`.

    Under this interpreter, the one `sd-plan.sh` pinned, and not under the
    `#!/usr/bin/env python3` that `bin/sd` carries: on the runner's bare PATH
    that is Xcode's 3.9. Standing on the branch `plan` checked out -- the
    row's own when it has one, `plan/<slug>` when not -- the verb records that
    as the row's branch (pack sd:621).

    Called before the documents are committed: the verb reads the file on
    disk, and an uncommitted folder is the ordinary case for it, which is why
    `source_commit` is nullable (`sd_db.jobs.cli._last_commit`).

    `sd`'s stdout is passed through. A non-zero exit is this run's refusal,
    naming the code and quoting what `sd` said. It leaves the documents
    uncommitted, so no commit carries a folder without a row: `plan` pushes
    nothing, and the runner keeps a dirty clone instead of pushing it. A
    retry finds the folder without its row and registers it without
    planning again. `cwd` is the checkout `sd` resolves the path against;
    named, so that does not rest on the caller's working directory.

    Registering is not optional: since retirement a folder without a row has
    no readable status at all, which is what `sd-status` calls
    `status-unreadable`.
    """
    done = subprocess.run(
        [sys.executable, str(entrypoint), "work", "register", relative],
        cwd=str(root), capture_output=True, text=True, check=False,
    )
    sys.stdout.write(done.stdout)
    if done.returncode != 0:
        raise Refused(
            f"registering {relative} failed: sd exited {done.returncode}: "
            f"{(done.stderr or done.stdout).strip()}")


#: One item per repository per night. The original ask was two; with twelve
#: registered repositories that is twenty-four unattended planning runs a
#: night against an assumption nobody has measured once. The number lives
#: here and nowhere else.
PER_REPOSITORY = 1


#: An assignment in one of these holds its row: queued or running is work
#: under way, `ending` is where a kept run stays until an operator acts, and
#: `blocked` is a run that failed and is not the nightly's to retry
#: unattended. Nor can it be requeued: `runner.requeue` refuses every exec
#: assignment, because an execution authorization is single-use. The operator
#: saves the item's run setup again and plans it again from the item's
#: button. Until a newer run is on the row, the nightly names it on stderr --
#: newer by assignment, not by status: a recovery run that has finished is in
#: none of these, and the row is then nobody's to plan again.
HOLDING = ("queued", "running", "ending", "blocked")


def planning_run(assignment) -> bool:
    """Whether an assignment is a `plan-item` run: an `exec` whose palette request names it."""
    scope = assignment["scope"] or ""
    if assignment["role"] != "exec" or not scope.startswith("palette:"):
        return False
    try:
        request = json.loads(scope.removeprefix("palette:"))
    except ValueError:
        return False
    return isinstance(request, dict) and request.get("command") == "plan-item"


def candidates(connection, repo: str):
    """The rows in one repository this job would plan, best first, one at a time.

    `backlog_items` is already ordered by priority, then due date, then id
    (source:local-sd-db/sd_db/reads.py::backlog_items), so "best" needs no
    opinion here.

    A planned row is still `planning`: a mutating worktree command leaves the
    status alone, and the folder lands on `plan/<slug>` in a runner clone, so
    it is not in this checkout until that branch merges and the checkout
    pulls. A row is therefore passed over when its folder exists here, when
    the folder already has its work row, when an assignment holds the row
    (`HOLDING`), or when the branch it would run on -- the one it has, or
    `plan/<slug>` when it has none -- is already on the remote. Without the
    last three the same top row was queued every night and the row behind it
    never was.

    A generator, so the remote is asked only about a row that passed every
    question the database can answer, and only until the caller has enough.
    """
    # `repo` is a disk path or a key; the store holds the key (sd:1439), and
    # the folder check below needs the disk path.
    key = paths.key(repo)
    root = paths.disk(repo)
    found = connection.execute("SELECT remote FROM repo WHERE path = ?", (key,)).fetchone()
    remote = found["remote"] if found else None
    for row in reads.backlog_items(connection, kind="task", repo=key, status="planning"):
        created = created_day(row["created_at"])
        if created is None:
            continue
        try:
            slug = f"{created}-{slugify(str(row['title']))}"
        except Refused:
            continue
        if (root / "docs" / "work" / slug).exists():
            continue
        if has_row(key, f"docs/work/{slug}/prd.md", connection):
            continue
        held = connection.execute(
            f"SELECT id, role, scope, status FROM assignment WHERE item = ? AND status IN "
            f"({','.join('?' * len(HOLDING))})", (row["id"], *HOLDING)).fetchall()
        if held:
            # Work under way is its own news. A row held only by a blocked
            # run is held on every night after, so a silent skip loses it.
            # The newest planning run and not `held`: a recovery run that has
            # finished is no longer a `HOLDING` status, so the blocked run it
            # replaced is all `held` holds, and the row was told to plan again
            # what it had already planned again (sd:793). A planning run and
            # not any run: planning again settles neither a blocked review nor
            # a newer one of another role, so that run is named instead
            # (sd:1181).
            newest = next((found for found in connection.execute(
                "SELECT role, scope, status FROM assignment WHERE item = ? ORDER BY id DESC",
                (row["id"],)) if planning_run(found)), None)
            other = next((found for found in held
                          if found["status"] == "blocked" and not planning_run(found)), None)
            if newest is not None and newest["status"] == "blocked":
                print(f"sd-plan: item {row['id']} in {repo} is held by a blocked run; "
                      "plan it again from the item's button", file=sys.stderr)
            elif other is not None:
                print(f"sd-plan: item {row['id']} in {repo} is held by a blocked {other['role']} "
                      f"run (assignment {other['id']}); the nightly plans it once that run is settled",
                      file=sys.stderr)
            continue
        # The branch the row has, and `plan/<slug>` only when it has none: a
        # row renamed after its setup keeps the branch it was set up on, so
        # asking about the slug the new title yields queued it again every
        # night, which is the sd:774 class the check exists to close (sd:793).
        if published(remote, row["branch"] or f"plan/{slug}"):
            continue
        yield {"id": row["id"], "slug": slug, "branch": row["branch"]}


def selectable(connection, repo: str, limit: int) -> list[dict]:
    """The first `limit` of `candidates`."""
    return list(itertools.islice(candidates(connection, repo), limit))


def published(remote: str | None, branch: str) -> bool:
    """Whether `branch` is on `remote`, asked of the remote and not a checkout.

    An answer that cannot be had is "no", said on stderr, and the check lets
    the row through. Setup then asks the remote again and refuses a row with
    no branch or a `plan/` branch when it cannot, so that row is named on
    stderr and not queued. Only a row on a branch set by hand outside `plan/`
    is queued anyway, and the worst that costs is a run that finds the folder
    and reports it already planned.
    """
    if not remote:
        return False
    try:
        done = subprocess.run(
            ["git", "ls-remote", "--heads", "--", remote, f"refs/heads/{branch}"],
            capture_output=True, text=True, timeout=60, check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError) as error:
        print(f"sd-plan: could not ask {remote} for {branch}: {error}", file=sys.stderr)
        return False
    if done.returncode != 0:
        print(f"sd-plan: could not ask {remote} for {branch}: {(done.stderr or done.stdout).strip()}",
              file=sys.stderr)
        return False
    return bool(done.stdout.strip())


def nightly(arguments) -> int:
    """Select one row per participating repository and enqueue each.

    Participation arrives on stdin, one repository path per line, because the
    entrypoint already resolves the profile and parses the conf for `status`.
    Parsing it a second time here is how the two answers drift apart. The
    entrypoint has likewise already refused a runner that is not dispatching.

    Exits non-zero when a standing refusal skipped a repository, and that
    exit is the whole signal (sd:842). Until then the skip was one stderr
    line, which under launchd is a line in
    `local-cron-jobs/logs/sd-plan-nightly.log` that nobody opens: the night
    still exited 0 and still printed "nothing to plan tonight", so a nightly
    that had stopped planning a repository read exactly like one that ran and
    found nothing -- the shape sd:766 already filed once for the nightly
    never enqueuing at all. Nothing here writes a signal anywhere: all three
    destinations the item weighed hang off the rc `cron-jobs.sh`'s `cmd_exec`
    already branches on. `notify_failure` pages on it the same night
    (failures.log, a macOS notification, an ntfy push, and the daily
    watchdog). `record_run_report` hands the rc to `sd reports ingest`, where
    `reporting.ingest_log` reads `attention = exit_code != 0` and opens a
    report item carrying a `followup` note, which is what `reads.open_followups`
    puts on `/today` until a person resolves it -- and the report itself is
    on `/operations` -> Reports with the night's stderr as its body. And
    `operations.LaunchdBackend._parse` maps a non-zero `last exit code` to
    `failed`, so `sd-plan-nightly` is red with Retry on `/operations` -> Jobs
    until a night succeeds. Writing a followup from here instead would have
    been new state on a night whose whole design is to write nothing when it
    refuses (sd:786, sd:805, sd:820), and it would have had to hang on an
    item -- reading as that row's fault when the refusal is the repository's.

    Only a standing refusal does this. Every one of them is machine state
    somebody has to fix, and none of them is "nothing to plan". A row the
    queue refuses on its own account -- an assignment already open, the
    ordinary case -- is still not the night's failure, and neither is a
    machine with no participation list.

    A repository that offers no candidate is never asked, and the night is
    green (sd:877). The question is put on the first row a real night tries,
    so a repository whose every row is passed over -- already planned, held
    by an assignment, its branch already on the remote -- puts no question
    at all, says "nothing to plan tonight" and exits 0 even while a refusal
    stands. That is deliberate and not the sd:842 hole it resembles. The
    refusal cannot have caused it: the `break` below is reached before the
    first `enqueue`, so a refused night writes nothing and passes no row
    over, and every row this repository passed over was passed over for a
    reason of its own. Nothing is waiting on the refusal. The first night a
    row *is* a candidate, a real night puts the question and goes red --
    which is the first night the refusal costs anything at all.

    A real night, because `--dry-run` asks nothing at all, for any
    repository, and is green however many candidates it reports: it takes
    the branch below before `catalog()` and never reaches the question. That
    is not an exception to the rule so much as the rule's premise. A dry run
    enqueues nothing, so there is no setup for the standing answer to guard,
    and it is an operator reading what a night would select rather than the
    run launchd watches. Every sentence here about what the night exits
    describes a real one.

    Asking ahead of the loop instead was rejected twice over. It needs the
    catalog, because `_standing` reads the palette before every refusal but
    the pending restore, and reading the catalog on a night that would queue
    nothing fails every quiet night on a machine with no palette configured
    -- a machine doing exactly what it should. That false red recurs nightly
    and forever; the red it buys is one night, once, on the transition from
    an empty backlog to a full one. And it needs a row: `standing_refusal`
    type-checks the `item` placeholder through `_shape`, so a night with no
    candidate would have to invent an id and ask a question no `prepare`
    will ever be asked. Asking only the pending restore, the one refusal
    that needs neither a catalog nor a row, was rejected as well: it is a
    second copy of a check `_standing` holds in one place precisely so a
    caller cannot drift from what `prepare` will do (sd:820), and "the cheap
    half of the set" is no rule a reader can carry to the next refusal.
    """
    wanted = {line.strip() for line in sys.stdin.read().splitlines() if line.strip()}
    if not wanted:
        print("sd-plan: no repository participates; nothing to plan")
        return 0

    connection = connect(default_path())
    try:
        # The conf names disk paths and the store holds keys (sd:1439), so a
        # participant is matched by its key and named as the conf spells it.
        known = {str(row["path"]) for row in repos.registered(connection)}
        unknown = sorted(path for path in wanted if paths.key(path) not in known)
        for path in unknown:
            print(f"sd-plan: {path} participates but is not a registered repository", file=sys.stderr)

        # Read once, and only if something is actually going to be queued:
        # every enqueue must quote the same catalog, and a night with nothing
        # to plan must not fail on a machine that has no palette configured.
        book: dict | None = None

        queued = 0
        # The repositories a standing refusal stopped, in the order they were
        # tried. A list and not a count: the summary below names them, because
        # the reader the exit status fetched needs to know which repository
        # stopped being planned, and a fleet of twelve makes "one of them" no
        # answer at all.
        skipped: list[str] = []
        for repo in sorted(path for path in wanted if paths.key(path) in known):
            # Counted as queued, not as tried: a row the queue refuses is
            # named and the next row in the same repository gets its turn.
            here = 0
            # Asked once for the repository, because the answer is the same
            # for every one of its rows: `runner_exec.standing_refusal`
            # enumerates every refusal `prepare` makes alike for every
            # row, reading the store, the palette file for all but the first,
            # and never the row itself (sd:820) -- a restore still to finish
            # (sd:786), which reads only the store, a palette that
            # cannot be read or that changed since this night read it,
            # `plan-item` registered on some other screen (sd:805) or
            # rejected from the catalog, typed values that are not the
            # names or the kinds its entry declares, and, because this night
            # only queues, a `plan-item` that is not a mutating worktree
            # command and so would run at once rather than be queued
            # (sd:814). The setup below writes a
            # decision note and bumps the revision on every row whose base
            # moved, so a refusal that stands for three nights wrote that
            # three times over for nothing. Asked on the first candidate this
            # night tries, which its own per-row refusal may still turn away,
            # rather than ahead of the loop: the answer is stale from the
            # moment it is read, and this is the latest point that still
            # precedes the first setup it guards.
            #
            # A refusal ends this repository's night where it is read
            # (sd:826). Until then the answer was carried down the loop
            # instead: the setup was skipped and `prepare` was called for
            # every remaining row anyway. That is the window sd:806 measured,
            # and skipping is what closes it. The answer is read, not held,
            # and every refusal above is machine state, so it can change
            # inside a night. One that ends after the answer -- a restore
            # finishing, or a palette unreadable when asked and readable
            # again, with the same bytes, at `prepare` (sd:806 note 2003) --
            # used to queue a row that no setup had refreshed. Its saved base
            # is then wherever its last setup left it, and a default branch
            # that moved since reaches the branch step stale: the step
            # refuses, the run blocks, and `HOLDING` passes the row over for
            # good (sd:778). Stopping costs those rows one night; carrying on
            # cost them for good.
            #
            # The other direction stays open, and a skip cannot close it: a
            # refusal that *starts* after the answer still leaves every row
            # tried after it whose branch is unset or on `plan/` set up and
            # then refused, a decision note and a revision each, for nothing;
            # `enqueue` sets up no other row. That half needs a second question
            # rather than a skip, and the late asking above is what keeps it
            # as narrow as one question can.
            #
            # Per repository and not per night: the next repository is asked
            # its own question, about its own first row, and may get a
            # different answer. And only if it has a first row: a repository
            # that offers no candidate is never asked and the night stays
            # green (sd:877). The docstring says why moving the question
            # ahead of this loop costs more than it buys, and
            # `WhatANightWithNoCandidateDoesNotAsk` pins both halves.
            asked = False
            for candidate in candidates(connection, repo):
                if here >= PER_REPOSITORY:
                    break
                if arguments.dry_run:
                    print(f"sd-plan: would plan item {candidate['id']} in {repo} as {candidate['slug']}")
                    here += 1
                    continue
                if book is None:
                    # Not inside the per-repository guard below: a palette
                    # that cannot be read is "could not ask", which fails the
                    # night, not one repository's ordinary refusal.
                    book = runner_exec.catalog()
                if not asked:
                    asked = True
                    standing = runner_exec.standing_refusal(
                        connection, "plan-item", {"item": candidate["id"]},
                        expected_catalog=book["sha256"], screen="item", require_queue=True)
                    if standing is not None:
                        # The repository and not the row: the refusal is the
                        # same for every one of its rows, so naming the row
                        # the question was put about would read as that row's
                        # fault.
                        print(f"sd-plan: {repo} is not planned tonight: {standing}", file=sys.stderr)
                        skipped.append(repo)
                        break
                try:
                    enqueue(connection, repo, candidate, book["sha256"])
                except (WorkflowError, RunnerRefused) as refusal:
                    # One repository's refusal is not the night's failure: a
                    # row with an assignment already open is the ordinary
                    # case, and the rest of the fleet still wants planning.
                    # That one comes from the queue as `RunnerRefused`, which
                    # is an `SdDbError` and not a `WorkflowError`.
                    print(f"sd-plan: item {candidate['id']} in {repo} was not enqueued: {refusal}",
                          file=sys.stderr)
                    continue
                here += 1
            queued += here
        if skipped:
            # Said before anything else and instead of "nothing to plan
            # tonight": that sentence is what made a stopped nightly read as
            # a quiet one, and a night that skipped a repository did not find
            # nothing -- it never looked. Non-zero even when another
            # repository queued a row, because the failure is one repository
            # of the fleet silently ceasing to be planned; a night that only
            # failed when it queued nothing at all would stay green forever
            # while that one was never planned again.
            print(f"sd-plan: {len(skipped)} repository(ies) not planned tonight: "
                  f"{', '.join(skipped)}", file=sys.stderr)
            return 1
        if not queued:
            print("sd-plan: nothing to plan tonight")
        return 0
    finally:
        connection.close()


def enqueue(connection, repo: str, candidate: dict, book: str) -> None:
    """Give the row a branch, then queue the command the button queues.

    Both halves are required and in this order: `prepare` refuses an item
    whose `branch` is NULL for every role, `exec` among them
    (`needs a valid branch`, in `_item` of `local-sd-db/sd_db/runner.py`).
    `configure_item` is what
    `sd runner prepare` calls, and it is a database write alone -- it creates
    no branch in any checkout.

    The setup is not optional. It once was: a night whose standing answer
    refused queued its rows with the setup skipped, so that a refusal
    standing for a hundred nights cost no notes and no revisions (sd:786,
    sd:805). A row queued that way carries whatever base its last setup
    saved, which is the sd:778 shape, so `nightly` now skips the rest of the
    repository instead and nothing reaches here without its setup (sd:826).

    `require_queue` makes `prepare` refuse an entry it would not queue, in
    the same transaction and before it writes the exec note, so the line
    below is printed only for a row that was queued (sd:814).
    """
    identifier = candidate["id"]
    # A row already on a `plan/` branch is set up again too, on the branch it
    # has. `configure_item` also saves the default branch's head, and the
    # runner refuses a new branch whose saved base has moved. A night whose
    # `prepare` refused leaves the branch and that head behind, so without
    # this the next run after the default branch moved ended blocked, and
    # `HOLDING` then passed the row over for good (sd:778). The row's own
    # branch and not `plan/<slug>`: a title edited since then yields another
    # slug, and a `plan/` branch set by hand keeps its name. A base that has
    # not moved writes nothing. Any other branch is left alone.
    branch = candidate["branch"]
    if not branch or branch.startswith("plan/"):
        runner_controls.configure_item(
            connection, identifier, repo=repo, branch=branch or f"plan/{candidate['slug']}",
            expected_revision=item_state(connection, identifier)["revision"], who="sd-plan",
        )
    runner_exec.prepare(
        # An int: `runner_exec._shape` refuses any other type for an item.
        connection, identifier, "plan-item", {"item": identifier},
        expected_revision=item_state(connection, identifier)["revision"],
        expected_catalog=book, screen="item", who="sd-plan", require_queue=True,
    )
    print(f"sd-plan: queued item {identifier} in {repo} as {candidate['slug']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sd-plan.sh", add_help=True)
    verbs = parser.add_subparsers(dest="verb", required=True)

    one = verbs.add_parser("item", help="plan one row, standing in its checkout")
    one.add_argument("item", type=int)
    one.add_argument("--dry-run", action="store_true",
                     help="say what would be planned, and write nothing")
    one.set_defaults(run=plan)

    every = verbs.add_parser("nightly", help="select one row per participating repository")
    every.add_argument("--dry-run", action="store_true",
                       help="say what would be enqueued, and enqueue nothing")
    every.set_defaults(run=nightly)

    arguments = parser.parse_args(argv)
    try:
        return arguments.run(arguments)
    except (Refused, WorkflowError) as refusal:
        print(f"sd-plan: {refusal}", file=sys.stderr)
        return 1


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


if __name__ == "__main__":
    sys.exit(main())
