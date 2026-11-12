#!/bin/sh
set -eu

display="${DISPLAY:-:1}"
timeout_seconds="${WAIT_FOR_X_TIMEOUT_SECONDS:-30}"

case "$timeout_seconds" in
  ''|*[!0-9]*|0)
    echo "WAIT_FOR_X_TIMEOUT_SECONDS must be a positive integer" >&2
    exit 64
    ;;
esac

if [ "$#" -eq 0 ]; then
  echo "wait-for-x.sh requires a command" >&2
  exit 64
fi

while ! xdpyinfo -display "$display" >/dev/null 2>&1; do
  if [ "$timeout_seconds" -le 1 ]; then
    echo "Display $display did not become ready" >&2
    exit 1
  fi
  timeout_seconds=$((timeout_seconds - 1))
  sleep 1
done

exec "$@"
