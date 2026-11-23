#!/usr/bin/env bash
# Keep localtunnel up; rewrite AW_BASE_URL in .env when URL changes.
set -euo pipefail
cd "$(dirname "$0")/.."
CONF=state/proxychains.conf
LOG=state/localtunnel.log
PIDF=state/localtunnel.pid
while true; do
  alive=0
  if [[ -f "$PIDF" ]] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
    alive=1
  fi
  if [[ "$alive" -eq 0 ]]; then
    printf '' > "$LOG"
    proxychains4 -f "$CONF" "${LT_BIN:-$HOME/.local/bin/lt}" --port 8080 --host https://loca.lt >>"$LOG" 2>&1 &
    echo $! > "$PIDF"
    sleep 6
    URL=$(grep -oE 'https://[^ ]+\.loca\.lt' "$LOG" | head -1 || true)
    if [[ -n "$URL" ]]; then
      sed -i "s|^AW_BASE_URL=.*|AW_BASE_URL=$URL|" .env
      echo "$(date -Iseconds) restarted tunnel $URL" >> state/tunnel-watchdog.log
    fi
  else
    # light keepalive through tunnel
    URL=$(grep '^AW_BASE_URL=' .env | cut -d= -f2-)
    curl -sS -o /dev/null --max-time 15 -H "bypass-tunnel-reminder: true" "$URL/healthz" || true
  fi
  sleep 60
done
