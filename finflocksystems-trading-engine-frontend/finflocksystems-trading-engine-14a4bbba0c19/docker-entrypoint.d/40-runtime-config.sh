#!/bin/sh
set -eu

BACKEND_API_URL="${BACKEND_API_URL:-https://finflockqfsplatform.cloud/qfsplatform/ai_trading_engine_backend1/api/v1}"
line_count="$(printf '%s' "$BACKEND_API_URL" | wc -l | tr -d ' ')"
if [ "$line_count" -ne 0 ] || ! printf '%s' "$BACKEND_API_URL" | grep -Eq '^https?://[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?(/[A-Za-z0-9._~:/?#@!$&()*+,;=%-]*)?$'; then
  echo "BACKEND_API_URL must be a single-line HTTP or HTTPS URL" >&2
  exit 64
fi
export BACKEND_API_URL
envsubst '${BACKEND_API_URL}' < /etc/trading-engine/runtime-config.template.js > /usr/share/nginx/html/runtime-config.js
