#!/bin/sh
#
# opentelemetry-demo.sh - manage the local opentelemetry-demo kind setup.
#
# Everything is pinned to cluster "otel-demo" / context "kind-otel-demo".
# No subcommand ever acts on the ambient kubectl context.
#
# Run "./opentelemetry-demo.sh help" for the subcommand list.

set -e

CLUSTER_NAME=otel-demo
CONTEXT="kind-$CLUSTER_NAME"
NAMESPACE=otel-demo
RELEASE=otel-demo
MCP_NAMESPACE=default
CHART_VERSION=0.40.10

BASE="$(cd "$(dirname "$0")" && pwd)"
. "$BASE/../lib/config.sh"
VALUES_FILE="$BASE/collector-values.yaml"
# Shell -> Secret -> collector env. $SYSTEM_TOOLS_CONFIG/opentelemetry-demo/.env
# is sourced when present, and it wins over OTLP_EXPORT_* already exported in
# the calling shell; optional when they are exported.
st_source_env opentelemetry-demo
# The upstream opentelemetry-demo checkout for repo-start / repo-stop.
DEMO_REPO="${OTEL_DEMO_REPO:-}"

# frontend-proxy -> localhost:8080
PID_FILE="$BASE/.port-forward.pid"
LOG_FILE="$BASE/port-forward.log"

# prometheus-mcp-server -> localhost:18081 (matches the "prom" MCP server config)
MCP_PID_FILE="$BASE/.mcp-port-forward.pid"
MCP_LOG_FILE="$BASE/mcp-port-forward.log"
MCP_LOCAL_PORT=18081

# kubernetes-mcp-server -> localhost:18080 (matches the "k8s" MCP server config)
K8S_PID_FILE="$BASE/.k8s-port-forward.pid"
K8S_LOG_FILE="$BASE/k8s-port-forward.log"
K8S_LOCAL_PORT=18080

# Names used when registering these servers with Claude Code (user scope).
PROM_MCP_NAME=prom
K8S_MCP_NAME=k8s

DEMO_URL="http://localhost:8080"

# End-user UIs behind frontend-proxy, as "name|path|description".
# Paths verified against a running frontend-proxy, not the envoy template: the
# Jaeger UI answers on /jaeger/ui/ (/jaeger/ itself 404s).
UI_ROUTES='store|/|web store frontend
jaeger|/jaeger/ui/|distributed traces
grafana|/grafana/|dashboards
loadgen|/loadgen/|load generator
flags|/feature|feature flag UI (flagd-ui)'

# ---------------------------------------------------------------- helpers ---

die() { echo "error: $*" >&2; exit 1; }

cluster_exists() {
  kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"
}

require_cluster() {
  cluster_exists || die "cluster '$CLUSTER_NAME' not found - run '$0 start' first"
}

# pid_running <pid-file>
pid_running() {
  [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null
}

# stop_forward <pid-file> <label>
stop_forward() {
  if pid_running "$1"; then
    kill "$(cat "$1")" 2>/dev/null || true
    echo "$2 port-forward stopped"
  else
    echo "$2 port-forward not running"
  fi
  rm -f "$1"
}

# svc_exists <namespace> <service>
svc_exists() {
  kubectl --context "$CONTEXT" -n "$1" get "svc/$2" >/dev/null 2>&1
}

# start_forward <pid-file> <log-file> <namespace> <svc> <ports> <label> <url>
start_forward() {
  _pid=$1; _log=$2; _ns=$3; _svc=$4; _ports=$5; _label=$6; _url=$7
  if pid_running "$_pid"; then
    kill "$(cat "$_pid")" 2>/dev/null || true
  fi
  kubectl --context "$CONTEXT" -n "$_ns" port-forward "svc/$_svc" "$_ports" > "$_log" 2>&1 &
  echo $! > "$_pid"
  echo "$_label port-forward: $_url (log: $_log)"
}

# Start the MCP forwards, but only for servers that are actually installed.
start_mcp_forwards() {
  if svc_exists "$MCP_NAMESPACE" prometheus-mcp-server; then
    start_forward "$MCP_PID_FILE" "$MCP_LOG_FILE" "$MCP_NAMESPACE" prometheus-mcp-server \
      "$MCP_LOCAL_PORT:8080" "prometheus-mcp-server" "http://127.0.0.1:$MCP_LOCAL_PORT/mcp"
  fi
  if svc_exists "$MCP_NAMESPACE" kubernetes-mcp-server; then
    start_forward "$K8S_PID_FILE" "$K8S_LOG_FILE" "$MCP_NAMESPACE" kubernetes-mcp-server \
      "$K8S_LOCAL_PORT:8080" "kubernetes-mcp-server" "http://127.0.0.1:$K8S_LOCAL_PORT/mcp"
  fi
}

stop_all_forwards() {
  stop_forward "$PID_FILE" "frontend-proxy"
  stop_forward "$MCP_PID_FILE" "prometheus-mcp-server"
  stop_forward "$K8S_PID_FILE" "kubernetes-mcp-server"
}

browser_open() {
  if command -v open >/dev/null 2>&1; then
    open "$1"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$1"
  else
    echo "no browser opener found - visit $1"
  fi
}

ui_names() {
  echo "$UI_ROUTES" | cut -d'|' -f1
}

# Emit "worker<TAB>tool" for every mcp_filter entry aura logged at startup.
# The filter lists live in the aura values file, but reading them from the logs
# reports what the running aura actually applied.
aura_filters() {
  _sq="'"
  kubectl --context "$CONTEXT" -n "$MCP_NAMESPACE" logs \
    -l app.kubernetes.io/name=aura --tail=-1 2>/dev/null \
  | while IFS= read -r _line; do
      case "$_line" in
        *"Creating worker"*)
          _w=${_line#*worker "$_sq"}
          _w=${_w%%"$_sq"*}
          ;;
        *"MCP filter:"*)
          _pats=${_line#*[}
          _pats=${_pats%]*}
          echo "$_pats" | tr ',' '\n' | tr -d ' "' | while IFS= read -r _t; do
            [ -n "$_t" ] && printf '%s\t%s\n' "$_w" "$_t"
          done
          ;;
      esac
    done | sort -u
}

# List the tool names a streamable-HTTP MCP server advertises, via a temporary
# port-forward. Ports here are deliberately not 18080/18081 so this never
# collides with the long-lived forwards.
mcp_tools_list() {
  _svc=$1; _port=$2
  kubectl --context "$CONTEXT" -n "$MCP_NAMESPACE" port-forward "svc/$_svc" "$_port:8080" \
    >/dev/null 2>&1 &
  _pf=$!
  sleep 3

  _hdr=$(curl -s -D - -o /dev/null -X POST "http://127.0.0.1:$_port/mcp" \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"opentelemetry-demo.sh","version":"1"}}}' 2>/dev/null)
  _sid=$(echo "$_hdr" | grep -i '^mcp-session-id:' | tr -d '\r' | awk '{print $2}')

  curl -s -X POST "http://127.0.0.1:$_port/mcp" \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -H "Mcp-Session-Id: $_sid" \
    -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1

  curl -s -X POST "http://127.0.0.1:$_port/mcp" \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -H "Mcp-Session-Id: $_sid" \
    -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' 2>/dev/null \
    | sed 's/^data: //' \
    | grep -o '"name":"[a-zA-Z_]*"' | sed 's/.*:"//;s/"$//' | sort -u

  kill "$_pf" 2>/dev/null || true
}

require_claude_cli() {
  command -v claude >/dev/null 2>&1 || die "the 'claude' CLI is not on PATH"
}

# register_mcp <name> <url>
# "claude mcp add" fails if the name is already taken, so drop it first to keep
# this idempotent.
register_mcp() {
  claude mcp remove -s user "$1" >/dev/null 2>&1 || true
  claude mcp add -s user -t http "$1" "$2"
}

# unregister_mcp <name>
unregister_mcp() {
  if claude mcp remove -s user "$1" >/dev/null 2>&1; then
    echo "$1: removed from user scope"
  else
    echo "$1: not registered in user scope"
  fi
}

# require_var <name> [file]: fail naming the variable and both remedies.
require_var() {
  eval "_val=\${$1:-}"
  [ -n "$_val" ] || { st_missing "$1" opentelemetry-demo "${2:-.env}"; exit 1; }
}

# The header that carries OTLP_EXPORT_AUTH. The collector expands ${env:...}
# in values only, never in map keys, so the header name reaches the exporter as
# a helm --set-string key instead of through the secret.
EXPORT_AUTH_HEADER="${OTLP_EXPORT_AUTH_HEADER:-Authorization}"

# check_export_header: a header name is an HTTP token; a dot or comma would
# also split the helm --set path.
check_export_header() {
  case "$EXPORT_AUTH_HEADER" in
    ''|*[!A-Za-z0-9-]*) die "OTLP_EXPORT_AUTH_HEADER must be a header name (letters, digits, '-'), got '$EXPORT_AUTH_HEADER'" ;;
  esac
}

# export_header_set: the helm --set-string value that puts the auth header on
# the otlphttp/export exporter.
export_header_set() {
  printf 'opentelemetry-collector.config.exporters.otlphttp/export.headers.%s=%s\n' \
    "$EXPORT_AUTH_HEADER" '${env:OTLP_EXPORT_AUTH}'
}

# Create/refresh the otlp-export secret the collector's extraEnvs read.
# Values come from the config .env when it exists, otherwise from the calling
# shell, so a plain "./opentelemetry-demo.sh start" works without exporting
# anything first.
ensure_export_secret() {
  require_var OTLP_EXPORT_URL
  require_var OTLP_EXPORT_AUTH
  case "$OTLP_EXPORT_URL$OTLP_EXPORT_AUTH" in
    *change-me*) die "OTLP_EXPORT_URL / OTLP_EXPORT_AUTH are still at their .env.example placeholders" ;;
  esac
  check_export_header

  kubectl --context "$CONTEXT" create namespace "$NAMESPACE" \
    --dry-run=client -o yaml | kubectl --context "$CONTEXT" apply -f - >/dev/null

  kubectl --context "$CONTEXT" create secret generic otlp-export \
    --namespace "$NAMESPACE" \
    --from-literal=url="$OTLP_EXPORT_URL" \
    --from-literal=auth="$OTLP_EXPORT_AUTH" \
    --from-literal=auth-header="$EXPORT_AUTH_HEADER" \
    --dry-run=client -o yaml | kubectl --context "$CONTEXT" apply -f - >/dev/null
}

# ------------------------------------------------------------- subcommands ---

cmd_start() {
  if ! cluster_exists; then
    kind create cluster --name "$CLUSTER_NAME"
  fi
  kubectl config use-context "$CONTEXT"

  helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts >/dev/null 2>&1 || true
  helm repo update open-telemetry >/dev/null

  # Must precede helm: the collector pod crashloops without these env vars and
  # "--wait" then fails on DaemonSet/otel-collector-agent.
  ensure_export_secret

  helm --kube-context "$CONTEXT" upgrade --install "$RELEASE" open-telemetry/opentelemetry-demo \
    --version "$CHART_VERSION" \
    --namespace "$NAMESPACE" \
    --create-namespace \
    -f "$VALUES_FILE" \
    --set-string "$(export_header_set)" \
    --wait --timeout 10m

  start_forward "$PID_FILE" "$LOG_FILE" "$NAMESPACE" frontend-proxy 8080:8080 \
    "frontend-proxy" "http://localhost:8080"
  start_mcp_forwards
  echo "opentelemetry-demo (helm) up: http://localhost:8080"
}

cmd_stop() {
  stop_all_forwards
  if cluster_exists; then
    helm --kube-context "$CONTEXT" uninstall "$RELEASE" -n "$NAMESPACE" 2>/dev/null || true
    kind delete cluster --name "$CLUSTER_NAME"
  else
    echo "cluster '$CLUSTER_NAME' already gone"
  fi
}

cmd_upgrade() {
  require_cluster
  kubectl config use-context "$CONTEXT"
  helm repo update open-telemetry >/dev/null
  ensure_export_secret
  helm --kube-context "$CONTEXT" upgrade "$RELEASE" open-telemetry/opentelemetry-demo \
    --version "$CHART_VERSION" \
    --namespace "$NAMESPACE" \
    -f "$VALUES_FILE" \
    --set-string "$(export_header_set)" \
    --wait --timeout 5m
}

cmd_status() {
  echo "=== kind cluster ==="
  if cluster_exists; then
    echo "cluster '$CLUSTER_NAME': running"
  else
    echo "cluster '$CLUSTER_NAME': not found"
    exit 0
  fi

  echo
  echo "=== helm releases ==="
  helm --kube-context "$CONTEXT" list -A 2>/dev/null || echo "none"

  echo
  echo "=== pods ($NAMESPACE) ==="
  kubectl --context "$CONTEXT" -n "$NAMESPACE" get pods -o wide 2>/dev/null || echo "no pods (namespace missing?)"

  echo
  echo "=== pods ($MCP_NAMESPACE) ==="
  kubectl --context "$CONTEXT" -n "$MCP_NAMESPACE" get pods -o wide 2>/dev/null || echo "no pods"

  echo
  echo "=== services ($NAMESPACE) ==="
  kubectl --context "$CONTEXT" -n "$NAMESPACE" get svc 2>/dev/null

  echo
  echo "=== port-forwards ==="
  if pid_running "$PID_FILE"; then
    echo "frontend-proxy: running, pid $(cat "$PID_FILE"), http://localhost:8080"
  else
    echo "frontend-proxy: not running"
  fi
  if pid_running "$MCP_PID_FILE"; then
    echo "prometheus-mcp-server: running, pid $(cat "$MCP_PID_FILE"), http://127.0.0.1:$MCP_LOCAL_PORT/mcp"
  else
    echo "prometheus-mcp-server: not running"
  fi
  if pid_running "$K8S_PID_FILE"; then
    echo "kubernetes-mcp-server: running, pid $(cat "$K8S_PID_FILE"), http://127.0.0.1:$K8S_LOCAL_PORT/mcp"
  else
    echo "kubernetes-mcp-server: not running"
  fi
}

cmd_mcp_install() {
  require_cluster
  kubectl config use-context "$CONTEXT"

  helm --kube-context "$CONTEXT" upgrade --install kubernetes-mcp-server \
    oci://ghcr.io/containers/charts/kubernetes-mcp-server \
    --namespace "$MCP_NAMESPACE" \
    --set ingress.enabled=false \
    --set config.read_only=true \
    --set 'rbac.extraClusterRoleBindings[0].name=view' \
    --set 'rbac.extraClusterRoleBindings[0].roleRef.name=view' \
    --set 'rbac.extraClusterRoleBindings[0].roleRef.external=true'
  kubectl --context "$CONTEXT" -n "$MCP_NAMESPACE" wait --for=condition=ready \
    pod -l app.kubernetes.io/name=kubernetes-mcp-server --timeout=120s
  kubectl --context "$CONTEXT" -n "$MCP_NAMESPACE" logs \
    -l app.kubernetes.io/name=kubernetes-mcp-server --tail=5

  # prometheus lives in $NAMESPACE while the MCP server runs in $MCP_NAMESPACE,
  # so the bare name "prometheus" will not resolve. Use the cross-namespace FQDN.
  helm --kube-context "$CONTEXT" upgrade --install prometheus-mcp-server \
    oci://ghcr.io/pab1it0/charts/prometheus-mcp-server \
    --namespace "$MCP_NAMESPACE" \
    --set prometheus.url="http://prometheus.$NAMESPACE.svc.cluster.local:9090" \
    --set livenessProbe.httpGet=null \
    --set 'livenessProbe.tcpSocket.port=http' \
    --set readinessProbe.httpGet=null \
    --set 'readinessProbe.tcpSocket.port=http'
  kubectl --context "$CONTEXT" -n "$MCP_NAMESPACE" wait --for=condition=ready \
    pod -l app.kubernetes.io/name=prometheus-mcp-server --timeout=120s
  kubectl --context "$CONTEXT" -n "$MCP_NAMESPACE" logs \
    -l app.kubernetes.io/name=prometheus-mcp-server --tail=5

  [ -n "$OPENAI_API_KEY" ] || die "OPENAI_API_KEY is not set (needed by the aura chart)"
  require_var AURA_CHART_DIR
  require_var AURA_VALUES_FILE
  helm --kube-context "$CONTEXT" upgrade --install aura "$AURA_CHART_DIR" \
    --namespace "$MCP_NAMESPACE" \
    -f "$AURA_VALUES_FILE" \
    --set secrets.openaiApiKey="$OPENAI_API_KEY"
  kubectl --context "$CONTEXT" -n "$MCP_NAMESPACE" wait --for=condition=ready \
    pod -l app.kubernetes.io/name=aura --timeout=120s

  echo
  start_mcp_forwards
}

cmd_open() {
  list_only=0
  case "${1:-}" in
    -l|--list) list_only=1; shift ;;
  esac

  for t in "$@"; do
    ui_names | grep -qx "$t" || die "unknown UI '$t' - see '$0 open --list'"
  done

  if [ "$list_only" -eq 0 ] && ! pid_running "$PID_FILE"; then
    die "frontend-proxy port-forward is not running - run '$0 start' first"
  fi

  wanted=" $* "
  echo "$UI_ROUTES" | while IFS='|' read -r name path desc; do
    [ "$wanted" = "  " ] || case "$wanted" in *" $name "*) ;; *) continue ;; esac
    url="$DEMO_URL$path"
    if [ "$list_only" -eq 1 ]; then
      printf '  %-10s %-34s %s\n' "$name" "$url" "$desc"
    else
      echo "opening $name: $url"
      browser_open "$url"
    fi
  done
}

cmd_register() {
  require_claude_cli
  register_mcp "$PROM_MCP_NAME" "http://127.0.0.1:$MCP_LOCAL_PORT/mcp"
  register_mcp "$K8S_MCP_NAME" "http://127.0.0.1:$K8S_LOCAL_PORT/mcp"

  echo
  pid_running "$MCP_PID_FILE" || echo "note: prometheus-mcp-server port-forward is not running"
  pid_running "$K8S_PID_FILE" || echo "note: kubernetes-mcp-server port-forward is not running"
  echo "registered in user scope - restart Claude Code, or run /mcp, to pick them up"
}

cmd_unregister() {
  require_claude_cli
  unregister_mcp "$PROM_MCP_NAME"
  unregister_mcp "$K8S_MCP_NAME"
}

cmd_aura_tools() {
  require_cluster
  command -v curl >/dev/null 2>&1 || die "curl is required by aura-tools"

  filters=$(aura_filters)
  [ -n "$filters" ] || echo "warning: no MCP filter lines in the aura logs - is aura running?" >&2

  printf '%-11s %-24s %s\n' "SERVER" "TOOL" "ALLOWED FOR"
  printf '%-11s %-24s %s\n' "-----------" "------------------------" "-----------"

  found=0
  for _spec in "kubernetes kubernetes-mcp-server 18190" "prometheus prometheus-mcp-server 18191"; do
    # shellcheck disable=SC2086
    set -- $_spec
    _label=$1; _svc=$2; _port=$3
    svc_exists "$MCP_NAMESPACE" "$_svc" || continue
    for _tool in $(mcp_tools_list "$_svc" "$_port"); do
      found=1
      _workers=$(echo "$filters" | awk -F'\t' -v t="$_tool" \
        '$2==t{printf "%s%s", (n++?",":""), $1}')
      [ -n "$_workers" ] || _workers="- (not in any filter)"
      printf '%-11s %-24s %s\n' "$_label" "$_tool" "$_workers"
    done
  done

  [ "$found" -eq 1 ] || echo "no MCP servers found in namespace $MCP_NAMESPACE - run '$0 mcp-install'"
}

cmd_aura() {
  require_cluster
  kubectl --context "$CONTEXT" -n "$MCP_NAMESPACE" exec -it deploy/aura -- \
    ./aura --api-url http://localhost:8080
}

cmd_cleanup() {
  assume_yes=0
  keep_cluster=0
  for arg in "$@"; do
    case "$arg" in
      -y|--yes) assume_yes=1 ;;
      --keep-cluster) keep_cluster=1 ;;
      *) die "cleanup: unknown option '$arg'" ;;
    esac
  done

  if [ "$keep_cluster" -eq 1 ]; then
    echo "About to delete from cluster '$CLUSTER_NAME':"
  else
    echo "About to PERMANENTLY DELETE the kind cluster '$CLUSTER_NAME' and everything in it:"
  fi
  echo "  - port-forwards: frontend-proxy (8080), prometheus-mcp-server ($MCP_LOCAL_PORT), kubernetes-mcp-server ($K8S_LOCAL_PORT)"
  echo "  - helm releases: $RELEASE (ns $NAMESPACE); aura, kubernetes-mcp-server, prometheus-mcp-server (ns $MCP_NAMESPACE)"
  echo "  - namespace: $NAMESPACE"
  if [ "$keep_cluster" -eq 0 ]; then
    echo "  - kind cluster '$CLUSTER_NAME' and kubeconfig context '$CONTEXT'"
  fi
  echo "This cannot be undone."

  if [ "$assume_yes" -eq 0 ]; then
    printf "Continue? [y/N] "
    read -r reply
    case "$reply" in
      y|Y|yes|YES) ;;
      *) echo "aborted"; exit 1 ;;
    esac
  fi

  echo
  echo "=== port-forwards ==="
  stop_all_forwards

  if ! cluster_exists; then
    echo
    echo "cluster '$CLUSTER_NAME' not found - nothing left to remove"
    kubectl config delete-context "$CONTEXT" 2>/dev/null || true
    kubectl config delete-cluster "$CONTEXT" 2>/dev/null || true
    kubectl config delete-user "$CONTEXT" 2>/dev/null || true
    echo "done"
    return 0
  fi

  echo
  echo "=== helm releases ==="
  helm --kube-context "$CONTEXT" uninstall "$RELEASE" -n "$NAMESPACE" 2>/dev/null || echo "$RELEASE: not installed"
  for r in aura kubernetes-mcp-server prometheus-mcp-server; do
    helm --kube-context "$CONTEXT" uninstall "$r" -n "$MCP_NAMESPACE" 2>/dev/null || echo "$r: not installed"
  done

  echo
  echo "=== namespaces ==="
  kubectl --context "$CONTEXT" delete namespace "$NAMESPACE" --ignore-not-found --timeout=120s

  if [ "$keep_cluster" -eq 1 ]; then
    echo
    echo "cluster '$CLUSTER_NAME' kept (--keep-cluster)"
    return 0
  fi

  echo
  echo "=== kind cluster ==="
  kind delete cluster --name "$CLUSTER_NAME"

  echo
  echo "=== kubeconfig ==="
  kubectl config delete-context "$CONTEXT" 2>/dev/null || true
  kubectl config delete-cluster "$CONTEXT" 2>/dev/null || true
  kubectl config delete-user "$CONTEXT" 2>/dev/null || true

  echo
  echo "teardown complete"
}

cmd_repo_start() {
  require_var OTEL_DEMO_REPO
  [ -d "$DEMO_REPO" ] || die "demo repo not found at $DEMO_REPO"
  cd "$DEMO_REPO"
  make start 2>&1 | tee "$BASE/opentelemetry-demo-output.txt"
}

cmd_repo_stop() {
  require_var OTEL_DEMO_REPO
  [ -d "$DEMO_REPO" ] || die "demo repo not found at $DEMO_REPO"
  cd "$DEMO_REPO"
  make stop
}

cmd_help() {
  cat <<EOF
opentelemetry-demo.sh - manage the local opentelemetry-demo kind setup
cluster: $CLUSTER_NAME   context: $CONTEXT   namespace: $NAMESPACE

Cluster and demo chart
  start              create the kind cluster if needed, install/upgrade the demo
                     chart, and start every port-forward (see below)
  stop               stop all port-forwards, uninstall the demo release, delete the cluster
  upgrade            helm upgrade the demo release to chart $CHART_VERSION
  status             cluster, releases, pods, services, and port-forward state
  open [--list] [name...]
                     open the end-user demo UIs in a browser (all of them by
                     default). --list prints them without opening. Requires the
                     frontend-proxy forward, i.e. a running "start".
                     Names: store jaeger grafana loadgen flags

MCP servers (kubernetes-mcp-server, prometheus-mcp-server, aura)
  mcp-install        install/upgrade all three into namespace $MCP_NAMESPACE
                     (requires OPENAI_API_KEY, AURA_CHART_DIR and
                     AURA_VALUES_FILE for the aura chart), then start their
                     port-forwards
  aura               attach to the aura CLI in the cluster
  aura-tools         table of every tool each MCP server advertises, and which
                     aura worker filter allows it

Claude Code registration (user scope, i.e. global)
  register           register the two MCP servers with Claude Code as
                       $PROM_MCP_NAME -> http://127.0.0.1:$MCP_LOCAL_PORT/mcp
                       $K8S_MCP_NAME  -> http://127.0.0.1:$K8S_LOCAL_PORT/mcp
                     Overwrites entries of the same name. Idempotent.
  unregister         remove both from user scope

Port-forwards (managed by start / stop, no separate subcommand)
  frontend-proxy        -> http://localhost:8080
  prometheus-mcp-server -> http://127.0.0.1:$MCP_LOCAL_PORT/mcp   ("prom" MCP config)
  kubernetes-mcp-server -> http://127.0.0.1:$K8S_LOCAL_PORT/mcp   ("k8s" MCP config)
  The two MCP forwards are started only if those servers are installed in the
  cluster. Aura does not need them; it reaches both via in-cluster DNS.

Teardown
  cleanup [-y] [--keep-cluster]
                     full teardown: forwards, all helm releases, the $NAMESPACE
                     namespace, the cluster, and the kubeconfig entries.
                     -y skips the prompt; --keep-cluster leaves an empty cluster.

Makefile-based variant (uses OTEL_DEMO_REPO, not this kind cluster)
  repo-start         run "make start" in the demo repo, teeing output here
  repo-stop          run "make stop" in the demo repo

  test               run this folder's tests (extra args go to unittest)
  help               this text

Configuration: \$SYSTEM_TOOLS_CONFIG/opentelemetry-demo/.env or exported
(copy .env.example). start/upgrade need OTLP_EXPORT_URL (OTLP/HTTP base URL)
and OTLP_EXPORT_AUTH (auth header value). OTLP_EXPORT_AUTH_HEADER names that
header (default Authorization; apikey for Mezmo-style ingestion).
EOF
}

# -------------------------------------------------------------- dispatch ---

if [ $# -eq 0 ]; then
  cmd_help >&2
  exit 1
fi
cmd=$1
shift

case "$cmd" in
  start)                cmd_start "$@" ;;
  stop)                 cmd_stop "$@" ;;
  upgrade)              cmd_upgrade "$@" ;;
  status)               cmd_status "$@" ;;
  mcp-install|mcp)      cmd_mcp_install "$@" ;;
  open|urls)            cmd_open "$@" ;;
  register)             cmd_register "$@" ;;
  unregister)           cmd_unregister "$@" ;;
  aura)                 cmd_aura "$@" ;;
  aura-tools)           cmd_aura_tools "$@" ;;
  cleanup)              cmd_cleanup "$@" ;;
  repo-start)           cmd_repo_start "$@" ;;
  repo-stop)            cmd_repo_stop "$@" ;;
  test)                 exec "${PYTHON:-python3}" -m unittest discover -s "$BASE/tests" -t "$BASE" "$@" ;;
  help|-h|--help)       cmd_help ;;
  *)                    echo "unknown subcommand: $cmd" >&2; echo >&2; cmd_help >&2; exit 2 ;;
esac
