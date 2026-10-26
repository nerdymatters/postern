#!/bin/sh
# ENG-D8 acceptance. On a FileVault Mac "starts at boot" really means "starts once the disk is
# unlocked". This host also needs the operator login to start its Tailscale LaunchAgent.
# Usage: ops/postern/reboot-check.sh [config.json] [installed-label]
set -u
CONFIG="${1:-$HOME/.config/postern/config.json}"
LABEL_PREFIX="${2:-org.nerdymatters.postern}"
case "$LABEL_PREFIX" in
  ''|*[!A-Za-z0-9.-]*|-*) echo "Invalid launchd label" >&2; exit 1 ;;
esac
PORT=$(/usr/bin/sed -n 's/.*"port"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$CONFIG" 2>/dev/null | head -1)
[ -n "$PORT" ] || PORT=8790
FAIL=0

echo "booted   $(sysctl -n kern.boottime | sed 's/.*} //')"
echo "logins   $(who | wc -l | tr -d ' ') console/ssh sessions right now (the public tunnel starts at operator login)"

for LABEL in "$LABEL_PREFIX" "$LABEL_PREFIX.watchdog" "$LABEL_PREFIX.proxy"; do
  OUT=$(launchctl print "system/$LABEL" 2>&1) || { echo "FAIL $LABEL is not loaded as a LaunchDaemon"; FAIL=1; continue; }
  PID=$(printf '%s\n' "$OUT" | awk -F'= ' '/^[[:space:]]*pid =/{print $2; exit}')
  STATE=$(printf '%s\n' "$OUT" | awk -F'= ' '/^[[:space:]]*state =/{print $2; exit}')
  echo "ok       $LABEL state=$STATE pid=${PID:-none}"
  # Record process start times alongside the boot and login times.
  [ -n "${PID:-}" ] && echo "         started $(ps -o lstart= -p "$PID" 2>/dev/null | sed 's/^ *//')"
  [ -n "${PID:-}" ] || FAIL=1
done

if curl -fsS -m 5 "http://127.0.0.1:$PORT/health"; then
  echo ""
  echo "ok       http://127.0.0.1:$PORT/health answered"
else
  echo "FAIL     http://127.0.0.1:$PORT/health did not answer"; FAIL=1
fi

[ "$FAIL" = 0 ] && echo "ok       local services healthy — also verify the public URL after operator login"
exit "$FAIL"
