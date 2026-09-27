#!/bin/sh
set -e

case "${1:-}" in
  -h|--help|help)
    cat <<'HELPEOF'
usage: opentelemetry-traces.sh

emit 3 test OTLP traces with telemetrygen against localhost. No options.
HELPEOF
    exit 0
    ;;
  "")
    ;;
  *)
    echo "usage: $(basename "$0") [-h|--help]  (takes no arguments)" >&2
    exit 1
    ;;
esac

GOBIN="${GOBIN:-$(go env GOBIN 2>/dev/null)}"
GOBIN="${GOBIN:-$HOME/go/bin}"
if [ ! -x "$GOBIN/telemetrygen" ]; then
  echo "opentelemetry-traces.sh: telemetrygen not found at $GOBIN — set GOBIN or run:" >&2
  echo "  go install github.com/open-telemetry/opentelemetry-collector-contrib/cmd/telemetrygen@latest" >&2
  exit 1
fi
"$GOBIN/telemetrygen" traces --otlp-insecure \
  --traces 3 2>&1 | grep -E 'start|traces|stop'

