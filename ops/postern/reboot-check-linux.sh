#!/bin/sh
# Run after a real reboot, before any manual service starts.
# Usage: ops/postern/reboot-check-linux.sh [config.json] [installed-label] [python]
set -eu
CONFIG="${1:-$HOME/.config/postern/config.json}"
LABEL_PREFIX="${2:-org.nerdymatters.postern}"
PYTHON="${3:-.venv/bin/python}"
case "$LABEL_PREFIX" in
  ''|*[!A-Za-z0-9.-]*|-*) echo "Invalid systemd label" >&2; exit 1 ;;
esac
PORT=$("$PYTHON" -c 'import json,sys; p=json.load(open(sys.argv[1])).get("port",8790); assert type(p) is int and 0<p<65536; print(p)' "$CONFIG")
FAIL=0
echo "booted   $(uptime -s)"
for LABEL in "$LABEL_PREFIX" "$LABEL_PREFIX.watchdog" "$LABEL_PREFIX.proxy"; do
  UNIT="$LABEL.service"
  ENABLED=$(systemctl --system is-enabled "$UNIT") || ENABLED=disabled
  if [ "$ENABLED" = enabled ] && systemctl --system is-active --quiet "$UNIT"; then
    echo "ok       $UNIT enabled and active"
    systemctl --system show "$UNIT" --property=MainPID --property=ActiveEnterTimestamp
  else
    echo "FAIL     $UNIT must be enabled and active"; FAIL=1
  fi
done
if curl -fsS -m 5 "http://127.0.0.1:$PORT/health"; then
  echo ""
  echo "ok       local API answered; also verify public HTTPS and ingress after reboot"
else
  echo "FAIL     local API did not answer"; FAIL=1
fi
exit "$FAIL"
