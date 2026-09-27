"""An `npm view` that answers from the same state file as the `gh` double.

`state["npm"][package]` holds `version` and `dependencies` the way the
registry's latest manifest does; `state["npm_errors"][package]` is a message
to fail with, the way `npm view` fails on E404 or a network error. The one
shape answered is `npm view <pkg>@latest version dependencies --json`, and it
answers it the way npm does: an object with both keys, or the bare version
string when the package declares no dependencies (recorded from `ms` and
`@payloadcms/email-nodemailer` on 2026-09-11).
"""

import json
import os
import sys


def main(argv):
    with open(os.environ["SD_HOLDS_DOUBLE_STATE"], encoding="utf-8") as handle:
        state = json.load(handle)
    if len(argv) != 5 or argv[0] != "view" or argv[2:] != ["version", "dependencies", "--json"] \
            or not argv[1].endswith("@latest"):
        print(f"npm double: unexpected invocation {argv!r}", file=sys.stderr)
        return 2
    package = argv[1][: -len("@latest")]
    if package in state.get("npm_errors", {}):
        print(state["npm_errors"][package], file=sys.stderr)
        return 1
    manifest = state.get("npm", {}).get(package)
    if manifest is None:
        print(f"npm error code E404\nnpm error 404  '{package}@latest' is not in this registry.",
              file=sys.stderr)
        return 1
    if manifest.get("dependencies"):
        print(json.dumps({"version": manifest["version"],
                          "dependencies": manifest["dependencies"]}, indent=2))
    else:
        print(json.dumps(manifest["version"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
