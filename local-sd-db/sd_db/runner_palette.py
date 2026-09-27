"""CLI parity for the finite command palette; no caller argv or shell fallback."""

import argparse
import getpass
import json
import sys
from pathlib import Path

from . import runner_exec, workflow
from .database import connect
from .errors import SdDbError


def _values(raw, entry):
    values = {}
    for argument in raw:
        name, separator, value = argument.partition("=")
        if not separator or name in values or name not in entry["placeholders"]:
            raise workflow.WorkflowError("each --value must name one registered placeholder exactly once")
        kind = entry["placeholders"][name]
        values[name] = int(value) if kind in {"item", "assignment"} else value
    return values


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sd runner commands", description="Registered command requests and execution evidence")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--home", type=Path, default=Path.home())
    commands = parser.add_subparsers(dest="verb", required=True)
    catalog = commands.add_parser("catalog", help="read the configured commands and current item guards")
    catalog.add_argument("--screen", choices=sorted(runner_exec.SCREENS), default="item")
    catalog.add_argument("--item", type=int)
    prepare = commands.add_parser("prepare", help="record a request; queue a mutating worktree command")
    prepare.add_argument("--item", type=int, required=True)
    prepare.add_argument("--command", required=True)
    prepare.add_argument("--if-revision", required=True)
    prepare.add_argument("--catalog", required=True)
    prepare.add_argument("--screen", choices=sorted(runner_exec.SCREENS), default="item")
    prepare.add_argument("--value", action="append", default=[])
    prepare.add_argument("--target-revision")
    prepare.add_argument("--target-run")
    for name in ("execute", "reconcile", "output"):
        command = commands.add_parser(name)
        command.add_argument("note", type=int)
        if name == "output":
            command.add_argument("--offset", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        connection = connect(args.database, home=args.home, write=args.verb not in {"catalog", "output"})
        try:
            if args.verb == "catalog":
                result = runner_exec.inventory(connection, screen=args.screen, item=args.item, home=args.home)
                # stdout is the JSON callers parse; the refusals go beside it,
                # one line each, so a person checking a hand edit sees the
                # entry and the rule without counting entries.
                for rejection in result["rejected"]:
                    print(f"rejected: {rejection['name']}: {rejection['reason']}", file=sys.stderr)
            elif args.verb == "prepare":
                current = runner_exec.catalog(home=args.home, screen=args.screen)
                entry = runner_exec.registered(current, args.command, missing="this command is not registered on the current screen")
                values = _values(args.value, entry)
                target = None
                if entry["scope"] != "worktree":
                    if not args.target_revision or not args.target_run:
                        raise workflow.WorkflowError("native controls need --target-revision and --target-run from catalog")
                    target = {"assignment": values.get("assignment"), "revision": args.target_revision,
                              "run": None if args.target_run == "none" else args.target_run}
                elif args.target_revision is not None or args.target_run is not None:
                    raise workflow.WorkflowError("worktree commands do not accept target guards")
                result = runner_exec.prepare(connection, args.item, args.command, values,
                    expected_revision=args.if_revision, expected_catalog=args.catalog, screen=args.screen,
                    target=target, home=args.home, who=getpass.getuser())
            elif args.verb == "output":
                result = runner_exec.read_execution(connection, args.note, offset=args.offset)
            else:
                operation = runner_exec.execute_immediate if args.verb == "execute" else runner_exec.reconcile
                result = operation(connection, args.note, home=args.home)
        finally:
            connection.close()
        print(json.dumps(result, indent=2, default=str))
        return 0
    except (OSError, ValueError, SdDbError) as error:
        print(f"commands: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
