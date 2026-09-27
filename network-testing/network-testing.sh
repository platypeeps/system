#!/bin/sh
# Network throughput / latency testing between the machines here.
# All output lands in ./logs (gitignored).
#
# Host presets come from hosts.conf (gitignored; see hosts.conf.example):
# `client` records are iperf3 targets, `bridge` records are Thunderbolt bridge
# addresses, `ping` records are named ping targets.
#
# Usage:
#   network-testing.sh server [port]        iperf3 server (default port, or e.g. 5202)
#   network-testing.sh server-tb <name>     iperf3 server bound to a bridge address
#   network-testing.sh client <preset> [port]
#   network-testing.sh results              throughput column from last iperf3 run
#   network-testing.sh issues               iperf3 intervals below ~300 Mbit
#   network-testing.sh ping <name>|<ip>     named ping record, or a literal address
#   network-testing.sh ping-issues          non-clean lines from all ping logs
#   network-testing.sh test [-v]            thunderbolt-bridge.sh suite, stubbed tools
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
mkdir -p logs

IPERF_OPTS="--bidir --parallel 3 --time 85000"

# Host presets live in hosts.conf (gitignored; copy hosts.conf.example), or
# the file NETWORK_TESTING_HOSTS names. hosts_lookup <kind> <name> prints the
# record's remaining fields; hosts_names <kind> lists the names of one kind.
HOSTS_FILE="${NETWORK_TESTING_HOSTS:-$DIR/hosts.conf}"
require_hosts() {
  [ -r "$HOSTS_FILE" ] && return 0
  echo "missing host presets: $HOSTS_FILE" >&2
  echo "copy $DIR/hosts.conf.example to $DIR/hosts.conf and fill it in," >&2
  echo "or export NETWORK_TESTING_HOSTS=<file>" >&2
  exit 1
}
hosts_lookup() {
  require_hosts
  awk -v k="$1" -v n="$2" '{ sub(/#.*/, "") } $1 == k && $2 == n {
    $1 = ""; $2 = ""; sub(/^ +/, ""); print; found = 1; exit }
    END { exit !found }' "$HOSTS_FILE"
}
hosts_names() {
  require_hosts
  awk -v k="$1" '{ sub(/#.*/, "") } $1 == k { printf "%s%s", sep, $2; sep = " " }
    END { print "" }' "$HOSTS_FILE"
}

client_target() {
  require_hosts
  rec="$(hosts_lookup client "$1")" || return 1
  # shellcheck disable=SC2086
  set -- $rec
  IP="$1"; BIND="${2:-}"
}

case "$1" in
  server)
    LOG="logs/iperf3_server${2:+_$2}.log"
    rm -f "$LOG"
    # shellcheck disable=SC2086
    iperf3 -s -V ${2:+--port $2} --logfile "$LOG"
    ;;
  server-tb)
    require_hosts
    BIND="$(hosts_lookup bridge "${2:-}")" || {
      echo "usage: network-testing.sh server-tb <name> — one of: $(hosts_names bridge)" >&2
      exit 1
    }
    LOG="logs/iperf3_server_${2}_thunderbolt.log"
    rm -f "$LOG"
    iperf3 -B "$BIND" -s -V --logfile "$LOG"
    ;;
  client)
    client_target "$2" || {
      echo "unknown preset '$2' — one of: $(hosts_names client)" >&2
      exit 1
    }
    rm -f logs/iperf3.log
    # shellcheck disable=SC2086
    iperf3 -V ${BIND:+-B $BIND} --client "$IP" ${3:+--port $3} --logfile logs/iperf3.log $IPERF_OPTS
    ;;
  results)
    [ -f logs/iperf3.log ] || { echo "no logs/iperf3.log yet — run a client test first" >&2; exit 1; }
    grep SUM logs/iperf3.log | awk '{ print $6, $7 }' || true
    ;;
  issues)
    # Bitrate under ~300 Mbit: keep Mbits/sec rows below 300, drop Gbits/sec
    # rows entirely (the old grep flagged 1.05 Gbits as a sub-300 problem).
    [ -f logs/iperf3.log ] || { echo "no logs/iperf3.log yet — run a client test first" >&2; exit 1; }
    grep SUM logs/iperf3.log | awk '$7 ~ /^Gbits/ { next } $6 + 0 < 300 { print }' || true
    ;;
  ping)
    require_hosts
    case "${2:-}" in
      "") echo "usage: network-testing.sh ping <name>|<ip> — names: $(hosts_names ping)" >&2; exit 1 ;;
      *.*.*.*) IP="$2" ;;
      *) IP="$(hosts_lookup ping "$2")" || {
           echo "unknown ping target '$2' — one of: $(hosts_names ping), or an IP" >&2
           exit 1
         } ;;
    esac
    ping --apple-time "$IP" > "logs/ping_$IP.log" 2>&1
    ;;
  ping-issues)
    found=0
    for f in logs/ping*.log; do
      [ -f "$f" ] || continue
      found=1
      grep -v PING "$f" | grep -v "64 bytes" || true
    done
    [ "$found" -eq 1 ] || { echo "no logs/ping*.log yet — run a ping test first" >&2; exit 1; }
    ;;
  test)
    shift
    exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: network-testing.sh server [port]|server-tb <name>|client <preset> [port]|results|issues|ping <target>|ping-issues|test [-v]

  server       run iperf3 server (default port)
  server-tb    iperf3 server bound to a `bridge` record's address
  client       iperf3 against a `client` record
  results      summarize iperf3 logs
  issues       show anomalies in iperf3 logs
  ping         long-running ping logger (a `ping` record name, or an IP)
  ping-issues  show packet loss / anomalies in ping logs
  test         run the thunderbolt-bridge.sh suite against stubbed system tools

Host presets: hosts.conf beside this script (copy hosts.conf.example), or the
file NETWORK_TESTING_HOSTS names.
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") server [port]|server-tb <name>|client <preset> [port]|results|issues|ping <target>|ping-issues|test [-v]" >&2
    exit 1
    ;;
esac
