#!/bin/sh
# Jaeger all-in-one (UI on :16686, OTLP on :4327/:4328) plus optional
# HotROD demo app (linked to jaeger, UI on :8080).
# Usage: jaeger.sh start|stop|update [hotrod]
set -e

TARGET="${2:-jaeger}"

# 4327/4328, not the standard 4317/4318: local-opentelemetry-collector owns
# those, since being the OTLP receiver is its entire job. Both can now run,
# which is what the usual app -> collector -> jaeger pipeline needs. Old note,
# now obsolete: the standalone collector also publishes this pair; list
# one of the two per profile. The four legacy receiver ports below are
# jaeger-internal and left alone.
UI_PORT="${JAEGER_UI_PORT:-16686}"
OTLP_GRPC_PORT="${JAEGER_OTLP_GRPC_PORT:-4327}"
OTLP_HTTP_PORT="${JAEGER_OTLP_HTTP_PORT:-4328}"
ZIPKIN_PORT="${JAEGER_ZIPKIN_PORT:-9411}"

case "$1" in
  start)
    # Both containers share a user-defined bridge network (name-based DNS)
    # instead of the deprecated --link flag.
    docker network inspect jaeger-net >/dev/null 2>&1 || docker network create jaeger-net
    if [ "$TARGET" = "hotrod" ]; then
      docker run -d --rm --name jaeger-hotrod -it --network jaeger-net \
        -p8080-8083:8080-8083 \
        -e OTEL_EXPORTER_OTLP_ENDPOINT="http://jaeger:4318" \
        jaegertracing/example-hotrod:1.76.0 \
        all --otel-exporter=otlp
    else
      # UI is loopback-only like every other local service. The ingest ports
      # below deliberately stay on all interfaces: accepting spans from
      # another machine is what a collector endpoint is for, and they carry
      # no credential. Bind them down if that changes.
      docker run -d --rm --name jaeger --network jaeger-net \
        -e COLLECTOR_ZIPKIN_HOST_PORT=:9411 \
        -p 127.0.0.1:"$UI_PORT":16686 \
        -p "$OTLP_GRPC_PORT":4317 \
        -p "$OTLP_HTTP_PORT":4318 \
        -p 14250:14250 \
        -p 14268:14268 \
        -p 14269:14269 \
        -p "$ZIPKIN_PORT":9411 \
        jaegertracing/all-in-one:1.76.0
    fi
    ;;
  stop)
    if [ "$TARGET" = "hotrod" ]; then docker stop jaeger-hotrod 2>/dev/null || true; else docker stop jaeger 2>/dev/null || true; fi
    ;;
  update)
    if [ "$TARGET" = "hotrod" ]; then
      docker stop jaeger-hotrod 2>/dev/null || true
      docker images -a | grep "jaegertracing/example-hotrod" | awk '{print $3}' | xargs docker rmi
    else
      docker stop jaeger 2>/dev/null || true
      docker images -a | grep "jaegertracing/all-in-one" | awk '{print $3}' | xargs docker rmi
    fi
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: jaeger.sh start|stop|update [hotrod]

  start        run the container (docker, --rm, named)
  stop         stop and remove the container
  update       stop the container and delete its local images by image ID
               (next start pulls the latest)
  [hotrod]     optional second argument: apply the action to the HotROD
               demo app container instead of jaeger itself

environment: JAEGER_UI_PORT (16686), JAEGER_OTLP_GRPC_PORT (4327),
             JAEGER_OTLP_HTTP_PORT (4328), JAEGER_ZIPKIN_PORT (9411)
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") start|stop|update [hotrod]" >&2
    exit 1
    ;;
esac
