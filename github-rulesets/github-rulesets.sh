#!/bin/sh
# GitHub rulesets as code: the files under .github/rulesets/, compared with
# and applied to a repository through `gh api`. Only `apply --apply` writes.
# Usage: github-rulesets.sh diff|apply|export OWNER/REPO [options]|test|help
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${PYTHON:-python3}"

usage() {
    cat >&2 <<'USAGE'
github-rulesets.sh — GitHub rulesets kept as files.

Usage: github-rulesets.sh <command> [OWNER/REPO] [options]

  diff OWNER/REPO     Compare .github/rulesets/*.json with the repository's
                      live rulesets, matched by name (GET only). Prints
                      same/differs/missing/extra per ruleset and a unified
                      diff for each one that differs. Exits 0 when all
                      match, 1 on any drift, 2 when it cannot read.
  apply OWNER/REPO    Create missing rulesets and update drifted ones.
                      Prints the planned `gh api` calls and changes
                      nothing unless given --apply (--dry-run is the
                      default). Never deletes a live ruleset.
                        --dry-run   print the calls only (default)
                        --apply     make the calls
  export OWNER/REPO   Write the live rulesets as normalised files (GET
                      only). Drops ids, timestamps and links, and drops
                      bypass actors that are users or teams, naming each
                      on stderr.
  test                Run this folder's tests.
  help                This text.

  --dir DIR           Ruleset files to use (default: .github/rulesets at the
                      repository root, or $GITHUB_RULESETS_DIR).
USAGE
}

case "${1:-}" in
    diff|apply|export)
        exec "$PYTHON" "$DIR/rulesets.py" "$@"
        ;;
    test)
        shift
        exec "$PYTHON" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
        ;;
    -h|--help|help)
        usage
        exit 0
        ;;
    *)
        usage
        exit 1
        ;;
esac
