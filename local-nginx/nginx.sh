#!/bin/sh
# nginx via Homebrew services (docker variant kept below, commented).
# Usage: nginx.sh start|stop|update
set -e

case "$1" in
  start)
    brew services start nginx
    # docker run -d --rm --name nginx -p 8083:80 nginx
    ;;
  stop)
    brew services stop nginx
    # docker stop nginx
    ;;
  update)
    brew upgrade nginx
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: nginx.sh start|stop|update

  start        brew services start nginx
  stop         brew services stop nginx
  update       brew upgrade nginx
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") start|stop|update" >&2
    exit 1
    ;;
esac
