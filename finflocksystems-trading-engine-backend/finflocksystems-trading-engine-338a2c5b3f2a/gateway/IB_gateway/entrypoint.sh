#!/bin/sh
set -eu
umask 077

# Validation emits only non-secret shell assignments and writes the normalized
# VNC password to a root-only temporary file for x11vnc.
normalized_vnc_password_file="/tmp/normalized-novnc-password"
runtime_exports="$(python /app/runtime_config.py --write-normalized-vnc-password "$normalized_vnc_password_file")" || exit $?
eval "$runtime_exports"
unset runtime_exports
export FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-*}"
export GATEWAY_DB_PATH="${GATEWAY_DB_PATH:-/data/gateway.sqlite3}"

if [ "${1:-}" = "--validate-only" ]; then
  rm -f "$normalized_vnc_password_file"
  unset NOVNC_PASSWORD
  exit 0
fi

mkdir -p /data /home/ibgateway/.vnc /home/ibgateway/ibc /home/ibgateway/Jts /tmp/.X11-unix
chown root:root /tmp/.X11-unix
chmod 1777 /tmp/.X11-unix
rm -f /tmp/.X1-lock /tmp/.X11-unix/X1
chown -R ibgateway:ibgateway /data /home/ibgateway

IFS= read -r novnc_password < "$normalized_vnc_password_file"
rm -f "$normalized_vnc_password_file"
x11vnc -storepasswd "$novnc_password" /home/ibgateway/.vnc/passwd >/dev/null 2>&1
unset novnc_password
unset NOVNC_PASSWORD
chown -R ibgateway:ibgateway /home/ibgateway/.vnc

# The noVNC viewer pair (x11vnc + websockify) exists so a human can watch the
# IB Gateway window in a browser. It measured ~64MB resident and is pure
# debugging overhead in an unattended run, so it stays off unless asked for.
#
# Xvfb and fluxbox are NOT gated: IB Gateway is a Java Swing application that
# needs a display, and IBC drives its login dialog through the window manager.
# Normalized here because supervisord's autostart needs a literal boolean and
# fails to start at all if the variable is unset.
case "$(printf '%s' "${GATEWAY_REMOTE_DESKTOP_ENABLED:-false}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on) GATEWAY_REMOTE_DESKTOP=true ;;
  *) GATEWAY_REMOTE_DESKTOP=false ;;
esac
export GATEWAY_REMOTE_DESKTOP
echo "Remote desktop (x11vnc + noVNC): ${GATEWAY_REMOTE_DESKTOP}"

envsubst '${PORT}' < /app/nginx.conf.template > /etc/nginx/nginx.conf
# Creates the PostgreSQL database on first start (no-op without POSTGRES_HOST),
# then migrates whichever database settings selected.
su -s /bin/sh ibgateway -c 'cd /app && python ensure_database.py && python manage.py migrate --noinput'

if [ "$BROKER_ADAPTER" = "ib_async" ]; then
  python manage.py configure_ibc
else
  rm -f /home/ibgateway/ibc/config.ini
fi
unset IB_USERNAME IB_PASSWORD

exec /usr/bin/supervisord -c /app/supervisord.conf
