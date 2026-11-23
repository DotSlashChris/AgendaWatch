#!/usr/bin/env bash
# Serve Agenda Watch (127.0.0.1:8080) at https://agendawatch.app via a Cloudflare NAMED tunnel.
#
# Prereq: `cloudflared tunnel login` finished and wrote ~/.cloudflared/cert.pem
#         (authorize the agendawatch.app zone in the browser).
# Does NOT touch the quick tunnel (trycloudflare), Stripe config, rules, cities, matcher, or the daily fetch.
#
# Env knobs:
#   ROUTE_WWW=1        also route www.agendawatch.app (default 1; set 0 to skip)
#   OVERWRITE_DNS=0    pass --overwrite-dns to `route dns` (default 0: never replaces an existing record)
#   SKIP_BASE_URL=0    set 1 to leave .env AW_BASE_URL / uvicorn untouched
set -euo pipefail
cd "$(dirname "$0")/.."
APP_DIR="$(pwd)"

TUNNEL_NAME=agendawatch
APEX=agendawatch.app
WWW=www.agendawatch.app
ORIGIN=http://127.0.0.1:8080
CF="$APP_DIR/cloudflared"
CF_DIR="$HOME/.cloudflared"
CERT="${TUNNEL_ORIGIN_CERT:-$CF_DIR/cert.pem}"
CONFIG="$CF_DIR/config.yml"
LOG="$APP_DIR/state/named_tunnel.log"
PIDF="$APP_DIR/state/named_tunnel.pid"
ROUTE_WWW="${ROUTE_WWW:-1}"
OVERWRITE_DNS="${OVERWRITE_DNS:-0}"
SKIP_BASE_URL="${SKIP_BASE_URL:-0}"

say() { echo "[$(date '+%F %T %Z')] $*"; }

if [[ ! -s "$CERT" ]]; then
  echo "No origin cert at $CERT. Run '$CF tunnel login' and authorize agendawatch.app first." >&2
  exit 2
fi
export TUNNEL_ORIGIN_CERT="$CERT"

# Refuse live Stripe keys (same guard as run_web.sh)
if grep -Eq '^(STRIPE_SECRET_KEY=sk_live_|STRIPE_PUBLISHABLE_KEY=pk_live_)' .env 2>/dev/null; then
  echo "REFUSING: live Stripe keys found in .env" >&2; exit 1
fi

tunnel_id() {
  # Explicit override wins (e.g. TUNNEL_ID=a73b1e98-... ./scripts/named_tunnel_setup.sh)
  if [[ -n "${TUNNEL_ID:-}" ]]; then echo "$TUNNEL_ID"; return 0; fi
  # Note: live tunnels report deleted_at as Go zero time "0001-01-01T00:00:00Z", not null.
  "$CF" tunnel list --name "$TUNNEL_NAME" -o json 2>/dev/null \
    | python3 -c 'import json,sys
name=sys.argv[1]
try: d=json.load(sys.stdin) or []
except Exception: d=[]
def live(t):
    v=t.get("deleted_at") or ""
    return (not v) or v.startswith("0001-01-01")
d=[t for t in d if t.get("name")==name and live(t)]
print(d[0]["id"] if d else "")' "$TUNNEL_NAME"
}

# 1) Create tunnel if missing
TID="$(tunnel_id || true)"
if [[ -z "$TID" ]] && compgen -G "$CF_DIR/*-*-*-*-*.json" >/dev/null && [[ "${FORCE_CREATE:-0}" != 1 ]]; then
  echo "Tunnel lookup returned nothing but credentials exist in $CF_DIR:" >&2
  ls -1 "$CF_DIR"/*-*-*-*-*.json >&2
  echo "Refusing to create a second tunnel. Re-run with TUNNEL_ID=<id> (or FORCE_CREATE=1 if truly intended)." >&2
  exit 3
fi
if [[ -z "$TID" ]]; then
  say "Creating tunnel $TUNNEL_NAME"
  "$CF" tunnel create "$TUNNEL_NAME"
  TID="$(tunnel_id)"
else
  say "Tunnel $TUNNEL_NAME exists: $TID"
fi
[[ -n "$TID" ]] || { echo "Could not determine tunnel id" >&2; exit 3; }
CREDS="$CF_DIR/$TID.json"
if [[ ! -s "$CREDS" ]]; then
  echo "Credentials file $CREDS missing (tunnel created elsewhere?). Run: $CF tunnel token --cred-file $CREDS $TUNNEL_NAME" >&2
  exit 4
fi

# 2) DNS routes (CNAME -> <id>.cfargotunnel.com, proxied)
route() {
  local host="$1" args=(tunnel route dns)
  [[ "$OVERWRITE_DNS" == 1 ]] && args+=(--overwrite-dns)
  say "Routing DNS $host -> $TUNNEL_NAME"
  if ! "$CF" "${args[@]}" "$TUNNEL_NAME" "$host"; then
    echo "route dns failed for $host (existing record? rerun with OVERWRITE_DNS=1 only if replacing it is OK)" >&2
    return 1
  fi
}
route "$APEX"
if [[ "$ROUTE_WWW" == 1 ]]; then route "$WWW" || say "WARN: www route failed; apex continues"; fi

# 3) Config with ingress
mkdir -p "$CF_DIR"
[[ -f "$CONFIG" ]] && cp "$CONFIG" "$CONFIG.bak-$(date +%Y%m%d-%H%M%S)"
{
  echo "tunnel: $TID"
  echo "credentials-file: $CREDS"
  echo "protocol: http2"
  echo "no-autoupdate: true"
  echo "ingress:"
  echo "  - hostname: $APEX"
  echo "    service: $ORIGIN"
  if [[ "$ROUTE_WWW" == 1 ]]; then
    echo "  - hostname: $WWW"
    echo "    service: $ORIGIN"
  fi
  echo "  - service: http_status:404"
} > "$CONFIG"
"$CF" tunnel --config "$CONFIG" ingress validate

# 4) Run named tunnel in background (separate process; quick tunnel untouched)
if [[ -f "$PIDF" ]] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
  say "Stopping previous named tunnel PID $(cat "$PIDF")"
  kill "$(cat "$PIDF")" || true; sleep 2
fi
: > "$LOG"
# Separate metrics port so it never collides with the quick tunnel's 127.0.0.1:20241
nohup "$CF" tunnel --config "$CONFIG" --metrics 127.0.0.1:20251 run "$TUNNEL_NAME" >>"$LOG" 2>&1 &
echo $! > "$PIDF"
say "Named tunnel PID $(cat "$PIDF"), log $LOG"
for _ in $(seq 1 30); do
  grep -q "Registered tunnel connection" "$LOG" && break
  kill -0 "$(cat "$PIDF")" 2>/dev/null || { echo "cloudflared exited; see $LOG" >&2; tail -20 "$LOG" >&2; exit 5; }
  sleep 1
done
grep -q "Registered tunnel connection" "$LOG" || { echo "No edge connection yet; see $LOG" >&2; exit 5; }
say "Edge connection registered"

# 5) AW_BASE_URL + restart uvicorn (localhost only, via run_web.sh)
if [[ "$SKIP_BASE_URL" != 1 ]]; then
  cp .env "state/.env.bak-$(date +%Y%m%d-%H%M%S)"; chmod 600 state/.env.bak-*
  if grep -q '^AW_BASE_URL=' .env; then
    sed -i "s|^AW_BASE_URL=.*|AW_BASE_URL=https://$APEX|" .env
  else
    echo "AW_BASE_URL=https://$APEX" >> .env
  fi
  say "AW_BASE_URL=https://$APEX written to .env"
  OLD="$(pgrep -f 'uvicorn web.app:app' || true)"
  if [[ -n "$OLD" ]]; then say "Stopping uvicorn $OLD"; kill $OLD || true; fi
  for _ in $(seq 1 15); do pgrep -f 'uvicorn web.app:app' >/dev/null || break; sleep 1; done
  nohup ./run_web.sh >> web.log 2>&1 &
  echo $! > state/uvicorn.pid
  for _ in $(seq 1 20); do
    [[ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/healthz)" == 200 ]] && break; sleep 1
  done
  say "uvicorn PID $(cat state/uvicorn.pid), local healthz $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/healthz)"
fi

# 6) Verify public
for _ in $(seq 1 12); do
  code="$(curl -sS -o /tmp/aw_named.html -w '%{http_code}' --max-time 20 "https://$APEX/" || true)"
  if [[ "$code" == 200 ]] && grep -q 'price-block' /tmp/aw_named.html; then
    say "https://$APEX/ -> 200, landing page OK"; exit 0
  fi
  say "https://$APEX/ -> ${code:-error}; retrying (DNS/edge propagation)"; sleep 10
done
echo "https://$APEX not serving 200 yet; check $LOG and DNS in the Cloudflare dashboard" >&2
exit 6
