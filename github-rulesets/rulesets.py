"""GitHub rulesets kept as files, compared with and applied to a repository.

Each ruleset is one JSON file under `.github/rulesets/`, holding only the
fields a create or update takes: name, target, enforcement, conditions, rules
and bypass_actors. Everything GitHub adds on a read (ids, node ids,
timestamps, `_links`, `source`, `current_user_can_bypass`) is dropped, so a
file says what the rule is and not when or where it was read.

A live ruleset is matched to a file by name. `diff` and `export` only read.
`apply` prints the calls it would make unless given `--apply`; it creates a
missing ruleset and updates a drifted one, and never deletes one: a live
ruleset with no file is reported and left alone.

All calls go through `gh api`, so authentication is whatever `gh` holds.
"""

import argparse
import difflib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

KEPT_FIELDS = ("name", "target", "enforcement", "conditions", "rules", "bypass_actors")

# Bypass actors that name a person or a group of people. Roles, apps and
# deploy keys stay; these are dropped on export and listed on stderr, because
# a public file must not carry an account id.
PERSONAL_ACTOR_TYPES = ("User", "Team")

DEFAULT_DIR = pathlib.Path(__file__).resolve().parent.parent / ".github" / "rulesets"


class GhError(Exception):
    pass


def gh(*args, stdin=None):
    """Run `gh api ...` and return its parsed JSON output."""
    proc = subprocess.run(
        ["gh", "api", *args],
        input=stdin,
        # No body means no stdin at all: an inherited one could be an open
        # pipe that `gh` would wait on.
        stdin=subprocess.DEVNULL if stdin is None else None,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise GhError("gh api %s: %s" % (" ".join(args), proc.stderr.strip() or "exit %d" % proc.returncode))
    out = proc.stdout.strip()
    return json.loads(out) if out else None


def normalise(ruleset):
    """The fields a create or update takes, in a stable order."""
    return {key: ruleset[key] for key in KEPT_FIELDS if key in ruleset}


def canonical(ruleset):
    """A form two equal rulesets share whatever order GitHub lists them in."""
    data = json.loads(json.dumps(normalise(ruleset)))
    data.setdefault("bypass_actors", [])
    for rule in data.get("rules", []):
        checks = rule.get("parameters", {}).get("required_status_checks")
        if checks is not None:
            checks.sort(key=lambda c: (c.get("context", ""), c.get("integration_id") or 0))
    data.get("rules", []).sort(key=lambda r: r.get("type", ""))
    data["bypass_actors"].sort(key=lambda a: (a.get("actor_type", ""), a.get("actor_id") or 0))
    return data


def render(data):
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def load_files(directory):
    files = {}
    for path in sorted(pathlib.Path(directory).glob("*.json")):
        data = json.loads(path.read_text())
        name = data.get("name")
        if not name:
            raise GhError("%s: no name" % path)
        if name in files:
            raise GhError("%s: ruleset name %r is also in %s" % (path, name, files[name][0]))
        files[name] = (path, data)
    return files


def live_rulesets(repo):
    """Repository-level rulesets of `repo`, by name, each read in full."""
    listed = gh("repos/%s/rulesets?per_page=100&includes_parents=false" % repo) or []
    live = {}
    for entry in listed:
        if entry.get("source_type", "Repository") != "Repository":
            continue
        live[entry["name"]] = gh("repos/%s/rulesets/%s" % (repo, entry["id"]))
    return live


def plan(repo, directory):
    """[(action, name, path_or_None, live_or_None)] for every file and live ruleset."""
    files = load_files(directory)
    live = live_rulesets(repo)
    steps = []
    for name, (path, data) in files.items():
        if name not in live:
            steps.append(("create", name, path, None))
        elif canonical(live[name]) != canonical(data):
            steps.append(("update", name, path, live[name]))
        else:
            steps.append(("same", name, path, live[name]))
    for name in live:
        if name not in files:
            steps.append(("extra", name, None, live[name]))
    return steps


def cmd_diff(args):
    drift = False
    for action, name, path, live in plan(args.repo, args.dir):
        if action == "same":
            print("same: %s" % name)
            continue
        drift = True
        if action == "extra":
            print("extra: %s (live only, no file)" % name)
            continue
        if action == "create":
            print("missing: %s (file only, not on %s)" % (name, args.repo))
            continue
        want = render(canonical(json.loads(path.read_text())))
        have = render(canonical(live))
        print("differs: %s" % name)
        sys.stdout.writelines(
            difflib.unified_diff(
                have.splitlines(True),
                want.splitlines(True),
                fromfile="%s (live)" % args.repo,
                tofile=str(path.name),
            )
        )
    return 1 if drift else 0


def cmd_apply(args):
    for action, name, path, live in plan(args.repo, args.dir):
        if action == "same":
            print("same: %s" % name)
            continue
        if action == "extra":
            print("extra: %s (live only; not deleted)" % name)
            continue
        if action == "create":
            call = ("POST", "repos/%s/rulesets" % args.repo)
        else:
            call = ("PUT", "repos/%s/rulesets/%s" % (args.repo, live["id"]))
        print("%s: gh api --method %s %s --input %s" % (action, call[0], call[1], path.name))
        if args.apply:
            body = json.dumps(normalise(json.loads(path.read_text())))
            gh("--method", call[0], call[1], "--input", "-", stdin=body)
            print("  done")
    if not args.apply:
        print("dry run: nothing changed; pass --apply to make these calls")
    return 0


def cmd_export(args):
    out = pathlib.Path(args.dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, ruleset in live_rulesets(args.repo).items():
        data = normalise(ruleset)
        kept = []
        for actor in data.get("bypass_actors", []):
            if actor.get("actor_type") in PERSONAL_ACTOR_TYPES:
                print(
                    "dropped bypass actor from %s: %s %s" % (name, actor.get("actor_type"), actor.get("actor_id")),
                    file=sys.stderr,
                )
            else:
                kept.append(actor)
        data["bypass_actors"] = kept
        target = out / ("%s.json" % slug(name))
        target.write_text(json.dumps(data, indent=2) + "\n")
        print("wrote %s" % target)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="github-rulesets.sh")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("diff", "apply", "export"):
        p = sub.add_parser(name)
        p.add_argument("repo", metavar="OWNER/REPO")
        p.add_argument("--dir", default=os.environ.get("GITHUB_RULESETS_DIR", str(DEFAULT_DIR)))
        if name == "apply":
            mode = p.add_mutually_exclusive_group()
            mode.add_argument("--dry-run", dest="apply", action="store_false")
            mode.add_argument("--apply", dest="apply", action="store_true")
            p.set_defaults(apply=False)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        parser.error("repository must be OWNER/REPO, not %r" % args.repo)
    try:
        return {"diff": cmd_diff, "apply": cmd_apply, "export": cmd_export}[args.command](args)
    except (GhError, OSError, ValueError) as exc:
        print("github-rulesets: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
