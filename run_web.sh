#!/usr/bin/env bash
# Agenda Watch step 3 — local web app. Binds to localhost only.
cd "$(dirname "$0")"
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
export AW_BASE_URL="${AW_BASE_URL:-http://localhost:8080}"
# Refuse to start if someone pointed live keys at us
if [[ "${STRIPE_SECRET_KEY:-}" == sk_live_* ]] || [[ "${STRIPE_PUBLISHABLE_KEY:-}" == pk_live_* ]]; then
  echo "REFUSING live Stripe keys. Use sk_test_ / pk_test_ only." >&2
  exit 1
fi
exec .venv/bin/uvicorn web.app:app --host 127.0.0.1 --port 8080 "$@"
