#!/bin/sh
# The system owns the workflow dashboard: Today, Backlog, Writing, Operations,
# and item controls share sd_db. The <prefix>.sd-dashboard LaunchAgent
# (prefix from SYSTEM_TOOLS_LABEL_PREFIX, default local.system-tools) manages its loopback
# server and optional authenticated Tailscale IP listener.
# The legacy collectors back two Operations areas: Resources runs sd_tile.py
# for Toolbox, Briefs, Vault, Research and Queues, and Ports calls one of
# them, `collectors.collect_ports`, in-process -- not the whole tile set.
# `tile` runs one view from a shell; `queue-open` opens a queue.
# Usage: dashboard.sh tile <tab> | queue-open <queue> | serve | preflight | install | health | pages | test
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

# DASHBOARD_PYTHON (.env) is the interpreter for `tile` and `queue-open`, the
# two subcommands a person runs from a shell, and for nothing else. `serve`
# runs under SD_DASHBOARD_PYTHON (below), and a Resources view runs the tile as
# a child of that server under its own sys.executable — so the same tile has
# two interpreters, today Homebrew python@3.14 on this path and Homebrew
# python@3.13 on that one. That matters because the vault is under ~/Documents
# behind Full Disk Access, macOS grants FDA to a binary, and each path needs
# its own grant. A tile under an ungranted interpreter does not report an empty
# vault: `collectors.vault_blocked` probes the read and the tab fails naming
# the interpreter to grant. Homebrew python is the pin here because the tidier
# one — /usr/bin/python3, whose path survives brew upgrades — does not work:
# user FDA grants for Xcode's python are silently ignored (tested 2026-08-29;
# bundle, inner binary, toggled, TCC-reset — every variant fails). The file is
# gitignored for a reason that has outlived its cause: it still carries the
# Jira credentials the deleted server's Issues tab read, which nothing here
# reads any more. `.env.example` no longer documents them.
# The file lives in the config directory: <config>/project-dashboard/.env,
# where <config> is $SYSTEM_TOOLS_CONFIG (default ~/.config/system).
. "$DIR/../lib/config.sh"
st_source_env project-dashboard
# Personal paths, the label prefix and the JEV_JOB_TRIAGE stage switch may come from .env. Export the ones set
# so collectors.py and `install` (which copies them into the LaunchAgent) see
# the same values this script does.
for _name in VAULT REPO_ROOT SYSTEM_TOOLS_LABEL_PREFIX SYSTEM_TOOLS_CONFIG JEV_JOB_TRIAGE; do
  if eval "[ -n \"\${$_name:-}\" ]"; then export "$_name"; fi
done

cmd_tile() {
  # One tab per invocation, JSON on stdout. The five seconds and 64KB are the
  # *outer* ceilings a caller applies to the whole child: the pack's loader
  # applied them, a Resources view applies them now
  # (`reports_screen.VIEW_SECONDS`), and a run from here has neither. The
  # inner ones still hold on this path -- `sd_tile.main` sets a deadline of
  # `TILE_SECONDS - TILE_MARGIN`, and each collector reads inside
  # `collectors.BUDGET_BYTES`. A subcommand rather than a second script because
  # .env is sourced above and names the interpreter this path's vault reads
  # need.
  #
  # Exit codes follow the retired plugin contract, not this script: 1 when a
  # collector failed, with the reason on stderr, and 2 when the tab name is
  # wrong. A Resources view shows that reason for any nonzero exit. Nothing
  # but the payload may reach stdout, so this execs.
  exec "${DASHBOARD_PYTHON:-python3}" "$DIR/sd_tile.py" "$@"
}

cmd_queue_open() {
  # The command behind the four `sys/queue-*` actions in `sd-plugin.json`, one
  # per decision queue. No dashboard renders those actions now (sd:719 step 3
  # removed them from the pack dashboard, which is no longer served, and the
  # system dashboard does not read the manifest). `sd plugin list` lists them
  # but runs nothing, so this subcommand is the way to run one. Each
  # declaration names its queue in its own command line, and a person running
  # this subcommand names the queue as its argument.
  # No `local`: this script is `#!/bin/sh` and every other function here keeps
  # to POSIX, where `local` is not defined -- it happens to work under the
  # macOS `/bin/sh` and would fail under `dash`. Found in review.
  queue_key="${1:-}"
  if [ -z "$queue_key" ]; then
    echo "usage: $(basename "$0") queue-open <queue>" >&2
    exit 2
  fi
  queue_url=$("${DASHBOARD_PYTHON:-python3}" "$DIR/sd_tile.py" --url "$queue_key") || exit $?
  open "$queue_url"
  echo "opened $queue_key in Obsidian"
}

# Production uses the command pack's installed sd_db build. -I ignores the
# caller's PYTHONPATH and working directory; the bootstrap adds only this
# dashboard package. The source checkout is available only to the test verb.
# The server refuses to start when that installed build lacks the last commit
# touching local-sd-db/sd_db in this checkout (sd:962, the #395 class).
SD_DB="$DIR/../local-sd-db"
# The server's interpreter, and so the one the tiles a Resources view runs
# inherit. One default, read by `serve`, `preflight`, `install`, `health` and
# `grants`, so the path `grants` probes is the path `serve` would run.
RUNTIME_PYTHON_DEFAULT="$HOME/repos/platypeeps/sd-ai-command-pack/.venv/bin/python"

cmd_grants() {
  # Both interpreters the vault is read under (the comment at the top), each
  # probed as the binary whose grant is in question, one line per path. This
  # is the report `collectors.vault_blocked` cannot give: it names the
  # interpreter a tile runs under, on the tile that was asked, and a grant the
  # other path lost -- `brew upgrade python` moves that Cellar and drops it --
  # had nowhere to appear but as an empty tab (sd:831). Exit 0 when both list
  # the vault and the answers are their own -- the control was refused, or it
  # was still waiting when its timeout ran out, which is what a read nobody
  # can grant looks like, so it counts as one kept out (sd:845) -- 1 when one
  # cannot (the line says what to grant), 3 when they are not the binaries'
  # answers: /bin/ls, which holds no grant of its own, listed the vault too,
  # so this shell's own Documents access reached both and an ok proves
  # nothing -- or the control gave no conclusive result, which is every other
  # end it can come to: it never started, the call on it failed, or it came
  # back nonzero saying something that is not an access refusal. Coming back
  # unsettled and running out the clock are not one answer: only the second
  # carries the lines above it. From a Terminal the leak is the usual case,
  # and the reason .claude/rules/macos-tcc.md says to test a grant from launchd, not a terminal.
  # Driven by the server's interpreter, as `preflight` and `health` are; the
  # suspect paths are arguments, so a broken one is a line and not a crash.
  if [ "$#" -gt 0 ]; then
    echo "usage: $(basename "$0") grants (no arguments; the interpreters come from .env and the environment)" >&2
    exit 2
  fi
  runtime_python="${SD_DASHBOARD_PYTHON:-$RUNTIME_PYTHON_DEFAULT}"
  if [ ! -x "$runtime_python" ]; then
    echo "dashboard: missing installed runtime $runtime_python; set SD_DASHBOARD_PYTHON to the server's interpreter" >&2
    exit 1
  fi
  # -I, as `serve` and `runtime` below: this answers which binary holds a
  # grant, and a caller's PYTHONPATH or user site-packages could change what
  # the probe runs. sd_tile.py imports the standard library only and loads
  # collectors.py by path, so isolation costs it no import.
  exec "$runtime_python" -I "$DIR/sd_tile.py" --grants \
    "DASHBOARD_PYTHON=${DASHBOARD_PYTHON:-python3}" \
    "SD_DASHBOARD_PYTHON=$runtime_python"
}

cmd_serve() {
  runtime_python="${SD_DASHBOARD_PYTHON:-$RUNTIME_PYTHON_DEFAULT}"
  if [ ! -x "$runtime_python" ]; then
    echo "dashboard: missing installed runtime $runtime_python; run the pack's setup or set SD_DASHBOARD_PYTHON to its provisioned interpreter" >&2
    exit 1
  fi
  exec "$runtime_python" -I "$DIR/sd_dashboard/bootstrap.py" server "$@"
}

cmd_runtime() {
  runtime_python="${SD_DASHBOARD_PYTHON:-$RUNTIME_PYTHON_DEFAULT}"
  if [ ! -x "$runtime_python" ]; then
    echo "dashboard: missing installed runtime $runtime_python" >&2
    exit 1
  fi
  exec "$runtime_python" -I "$DIR/sd_dashboard/bootstrap.py" runtime "$@"
}

cmd_docs() {
  # The design documents: docs/design/*.md rendered into docs/design's sibling
  # docs/dashboard/, which `documents.py` already serves for any checkout that
  # holds one -- so this repository needs no registration step, only a build.
  # Plain python3: the renderer carries its own markdown subset rather than a
  # dependency, because no interpreter on this machine ships python-markdown
  # and the CI job installs no extras.
  exec "${PYTHON:-python3}" "$DIR/design_docs.py" "$@"
}

cmd_pages() {
  # The rail and palette map, built from the page registry in sd_dashboard/v2/pages/ (sd:2418): the one list of which
  # section opens a new page, which opens its old screen, and which old screens the palette offers. The README points
  # here instead of keeping a table that each port had to edit.
  if [ "$#" -gt 0 ]; then
    echo "usage: $(basename "$0") pages (no arguments)" >&2
    exit 2
  fi
  PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}" exec "${PYTHON:-python3}" -m sd_dashboard.v2
}

cmd_test() {
  PYTHONPATH="$DIR:$SD_DB:$DIR/tests${PYTHONPATH:+:$PYTHONPATH}" \
    exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
}

case "${1:-}" in
  tile) shift; cmd_tile "$@" ;;
  queue-open) shift; cmd_queue_open "${1:-}" ;;
  serve) shift; cmd_serve "$@" ;;
  preflight|install|health) cmd_runtime "$@" ;;
  grants) shift; cmd_grants "$@" ;;
  docs) shift; cmd_docs "$@" ;;
  pages) shift; cmd_pages "$@" ;;
  test|check) shift; cmd_test "$@" ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: dashboard.sh tile <tab> | queue-open <queue> | serve | preflight | install | health | grants | docs | pages | test

  tile <tab>       one tab as JSON on stdout — toolbox, briefs,
                   briefs-rows, vault, research, ports or queues. The
                   Briefs page reads briefs-rows. The Resources views are
                   toolbox, briefs, vault, research and queues, and each
                   reads its tab this way; Ports has no view and reads
                   collect_ports in-process. Exits 1 with the reason on
                   stderr when a collector fails and 2 when the tab name is
                   not one of those
  queue-open <q>   open one decision queue in Obsidian, where a status is
                   still written. sd-plugin.json declares one action per
                   queue that runs this; no dashboard renders those actions
  serve            Today, Backlog, Writing, Operations and item controls.
                   All reads/writes use `sd_db`. Binds loopback; optional ip_origin
                   adds a listener on this node's Tailscale IP with peer authentication.
                   --config, --port and --database select explicit settings
  preflight        read config, installed library, database, LaunchAgent and
                   Tailscale Serve; print a reviewable plan and fingerprint
  install          preview by default. --apply --expected-fingerprint SHA
                   replaces the LaunchAgent with backups and health checks;
                   local configuration makes no Tailscale calls. Private :8443
                   routing requires an explicit HTTPS origin and operator login;
                   ip_origin optionally enables HTTP access on the Tailscale IP :8768
  health           check the configured front door and backend /health
                   once; --wait SECONDS retries until it answers healthy
  grants           probe the vault under DASHBOARD_PYTHON and under
                   SD_DASHBOARD_PYTHON, one line per path, naming the one
                   that lacks Full Disk Access. Takes no arguments. Exits 1
                   when a path cannot read, 3 when the answers were not
                   measured as the binaries' own -- this shell's Documents
                   access reached them, or the control gave no conclusive
                   result -- and 0 when both hold the grant, or when the
                   control was still waiting at its timeout, which is what a
                   read nobody can grant looks like. Run it from launchd,
                   where a child's access is its own, for a conclusive 0 when
                   both hold the grant
  docs             render docs/design/*.md into docs/dashboard/, diagrams
                   included, where the Documents tab already serves them.
                   --check lists stale outputs and exits 1 if any; --hashes
                   prints the CSP digests the diagram pages need, which
                   documents.py pins, and exits 1 unless the pages carry
                   exactly the pinned scripts, each on every page. Exits 3
                   when this checkout has no docs/design/
  pages            the rail and palette map from the page registry, one
                   tab-separated line per target: section, address and
                   `new` with its sd item for a registered page; `classic`
                   for a section that opens its old screen; `palette` for
                   an old screen the palette offers. Takes no arguments
  test             the dashboard's own tests. `check` is the same thing

The workflow server replaces the earlier pack dashboard on :8767. Progress
controls save to the shared database without status commits; the legacy plugin
collector commands remain available.

environment:
  DASHBOARD_PYTHON            interpreter for `tile` and `queue-open` only
                              (.env; Homebrew python, not /usr/bin/python3).
                              It does not select the tiles a Resources view
                              renders -- those run under the server's
                              interpreter, below
  VAULT                       vault path, read by collectors.py itself
                              (default ~/Documents/Vault; set it in
                              <config>/project-dashboard/.env)
  REPO_ROOT                   checkout root, likewise (default ~/repos)
  SYSTEM_TOOLS_CONFIG         config root (default ~/.config/system);
                              .env and documents.conf are read from its
                              project-dashboard/, cron job files from
                              cron-jobs/jobs
  SD_DASHBOARD_PYTHON         interpreter containing installed sd_db, for
                              serve, preflight, install and health (default
                              command-pack .venv/bin/python). A Resources view
                              runs sd_tile.py as a child of the server, so this
                              is the interpreter its vault reads use. Full Disk
                              Access is granted to a binary, so this one and
                              DASHBOARD_PYTHON each need their own grant;
                              `grants` probes both
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") tile <tab> | queue-open <queue> | serve | preflight | install | health | grants | docs | pages | test" >&2
    exit 1
    ;;
esac
