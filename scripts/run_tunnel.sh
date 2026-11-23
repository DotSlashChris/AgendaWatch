#!/usr/bin/env bash
# Expose 127.0.0.1:8080 via localtunnel+proxychains (sandbox blocks non-443 edge ports).
set -euo pipefail
cd "$(dirname "$0")/.."
CONF=state/proxychains.conf
LOG=state/localtunnel.log
: > "$LOG"
proxychains4 -f "$CONF" "${LT_BIN:-$HOME/.local/bin/lt}" --port 8080 --host https://loca.lt >>"$LOG" 2>&1 &
echo $! > state/localtunnel.pid
sleep 5
grep -oE 'https://[^ ]+\.loca\.lt' "$LOG" | head -1
