#!/bin/sh
# 1) restore DB + state from R2 (via Worker) — refuses to start on failure so an empty DB never overwrites the snapshot
# 2) start the snapshot loop  3) run the unchanged FastAPI app
set -eu
cd /app
if [ -n "${AW_PERSIST_URL:-}" ]; then
  python3 cloudflare/persist.py restore
  python3 cloudflare/persist.py loop &
  PERSIST_PID=$!
  uvicorn web.app:app --host 0.0.0.0 --port 8080 --proxy-headers --forwarded-allow-ips='*' &
  APP_PID=$!
  trap 'kill -TERM $APP_PID 2>/dev/null; wait $APP_PID; kill -TERM $PERSIST_PID; wait $PERSIST_PID; exit 0' TERM INT
  wait $APP_PID
else
  exec uvicorn web.app:app --host 0.0.0.0 --port 8080 --proxy-headers --forwarded-allow-ips='*'
fi
