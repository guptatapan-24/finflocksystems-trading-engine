#!/bin/sh
set -eu
if [ "${BROKER_ADAPTER:-ib_async}" = "mock" ]; then
  exec sleep infinity
fi
installed_major="$(/usr/local/bin/ibgateway-version verify --expected "${TWS_MAJOR_VRSN:-}")" || exit $?
exec /opt/ibc/scripts/ibcstart.sh "$installed_major" \
  --gateway \
  --tws-path=/opt/Jts \
  --tws-settings-path=/home/ibgateway/Jts \
  --ibc-path=/opt/ibc \
  --ibc-ini=/home/ibgateway/ibc/config.ini \
  "--mode=${IBC_TRADING_MODE:-paper}" \
  --on2fatimeout=restart
