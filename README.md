# Agenda Watch

Public-agenda alerts for Utah cities. Live site: https://agendawatch.app

This repository is the full application and the Cloudflare Containers deploy. It does not contain secrets, the live SQLite database, or customer data.

## Architecture

Agenda Watch is a Python FastAPI app (`web/`, plus the daily fetcher in `agenda_watch.py`) that runs inside a single Cloudflare Container, fronted by a Cloudflare Worker (`cloudflare/src`).

- **Worker (`agenda-watch`)** — custom domains `agendawatch.app` and `www.agendawatch.app`. Routes public pages, rate-limits, serves stored anonymous HTML, and forwards signed-in / API traffic to the container. Cron triggers fire the daily fetch. Container state snapshots are stored in the Durable Object’s SQLite (not a separate R2 binding in the current `wrangler.jsonc`).
- **Container** — Python 3.12 image built from `cloudflare/Dockerfile` (build context is the repo root). On boot it restores a snapshot, starts a persist loop, and runs uvicorn. Daily job: fetch Utah Public Notice (PMN) plus official city agenda pages, match watches, send alert email via Resend (`alerts@agendawatch.app`; feedback replies from `hello@agendawatch.app`).
- **Cron** — `21 14 * * *` and `21 15 * * *` (UTC) so the job starts at 8:21 AM America/Denver in both MDT and MST. Do not trigger a fetch with `POST /__aw/run` unless the owner has explicitly asked for that.
- **Billing** — Stripe is live in production. Local `./run_web.sh` accepts test keys only and refuses live keys.
- **Internal status** — `GET /__aw/status`, `/__aw/watch-list`, and related `/__aw/*` routes require the `X-AW-Internal-Secret` header (value of secret `AW_INTERNAL_SECRET`).

Product rules worth knowing when you change fetch or alerts:

- A city-page document emails only if its meeting is upcoming and it was first seen after the page was added as a source (no backfill).
- A PMN PDF HTTP 404 is skipped and remembered; after two distinct dates it is logged once as permanently missing and not retried daily. HTTP 403, timeouts, and connection errors still fail that city and withhold email.
- A city listing page that keeps returning HTTP 403 is logged once as known-blocked (the city stays covered by PMN) and is not a fresh daily error.

## Layout

```
agenda_watch.py          Daily fetch + matcher
aw_alerts.py             Alert planning and email body
aw_cities.py             City catalog helpers
aw_city_sources.py       Official city agenda pages
aw_guard.py              Daily request / cost guards
aw_store.py              SQLite run log, document cache, missing-PDF memory
config.json              Cities, keywords, PMN windows (owner/admin identity is in secrets, not this file)
web/                     FastAPI app and HTML templates
tests/                   Python tests and fixtures
cloudflare/              Worker, Dockerfile, wrangler config, persist hook
  src/                   TypeScript Worker (edge, schedule, Stripe env checks)
  test/                  Worker tests
  Dockerfile             Container image (set AW_BUILD here before a deploy)
  wrangler.jsonc         Worker name, routes, cron, bindings, public vars
scripts/                 Ops helpers, including docker-hostnet
tools/gen_worker_catalog.py   Regenerates src/catalog.gen.ts from config.json
screens/                 UI screenshots from earlier product work
```

## Tests

Python (from the repo root; uses a sandbox so it never touches a real database). The `data/` and `state/` directories are in the tree (empty `.gitkeep` only) so a clean checkout can run tests and the web app. The app auto-creates `data/secret.key` on first import (gitignored; not a production secret) and may write `state/.persist-now`. Do not commit those files:

```bash
pip install -r requirements-web.txt -r requirements-run.txt -r requirements-dev.txt
python3 -m pytest -q tests
```

Expect three known date-dependent billing failures in `tests/test_city_lock.py` and `tests/test_period_end_webhooks.py` (they pin a lock date that drifts as the calendar moves). New failures beyond those three should be investigated before deploy.

Worker and TypeScript (Node 22):

```bash
export PATH="$HOME/.local/node22/bin:$PATH"   # or your Node 22 install
cd cloudflare
npm install
npx tsc --noEmit
node --test test/*.test.ts
```

## Local web app

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-web.txt -r requirements-run.txt
cp .env.example .env   # fill TEST Stripe keys only
./run_web.sh           # http://127.0.0.1:8080
```

`./run_web.sh` refuses to start if `STRIPE_SECRET_KEY` or `STRIPE_PUBLISHABLE_KEY` are live keys.

## Deploy

The project is not required to be a git checkout on the build machine, but going forward changes should land on `main` through a pull request, then deploy from that revision.

1. Set `ENV AW_BUILD=YYYYMMDD-short-label` in `cloudflare/Dockerfile` (this is the build label reported by `/healthz`; there is no git commit on the live host today).
2. Use Node 22 and Wrangler 4.x. On the operator box this has been:

   ```bash
   export PATH="$HOME/.local/node22/bin:$PATH"
   export WRANGLER_DOCKER_BIN="$PWD/scripts/docker-hostnet"
   cd cloudflare
   npx wrangler deploy
   ```

   `scripts/docker-hostnet` wraps Docker so image builds use `--network=host` (needed when dockerd is started with `--iptables=false`).
3. Wait until `https://agendawatch.app/healthz` shows the new `build` label before treating the deploy as live. First boot after a deploy can take a minute or two.
4. Do not send email as part of a deploy, and do not `POST /__aw/run` unless the owner has asked to run a fetch.
5. Keep the custom domain, the container, and Email Routing intact. Do not point anything at quazar16.com.

Docker on the operator box has been Docker Engine 26.1.4, with `dockerd --iptables=false`. Wrangler 4.149.0 is what recent production deploys used (`package.json` currently asks for `wrangler@latest`; the lockfile pins a concrete version after `npm install`).

### Secrets (stdin only)

Set production secrets with Wrangler. Type or pipe the value on stdin; never pass the secret as a command-line argument or leave it in shell history:

```bash
cd cloudflare
npx wrangler secret put AW_INTERNAL_SECRET
npx wrangler secret put RESEND_API_KEY
npx wrangler secret put TURNSTILE_SECRET
npx wrangler secret put STRIPE_SECRET_KEY
npx wrangler secret put STRIPE_PUBLISHABLE_KEY
npx wrangler secret put STRIPE_WEBHOOK_SECRET
npx wrangler secret put AW_OWNER_EMAIL
npx wrangler secret put AW_ADMIN_EMAILS
npx wrangler secret put NOTIFY_TO
```

On a trusted operator machine, a copy of `AW_INTERNAL_SECRET` may be kept in a mode-600 header file and passed to curl as `--header @/absolute/path/to/internal_secret.hdr` (never `~` inside `-H @…`, and never the raw secret on the command line).

### Cloudflare secrets, vars, and bindings (names only)

**Secrets** (wrangler secret put):

| Name | Used for |
| --- | --- |
| `AW_INTERNAL_SECRET` | Header `X-AW-Internal-Secret` for `/__aw/*` and container persist/email relay |
| `RESEND_API_KEY` | Alert and feedback email via Resend |
| `TURNSTILE_SECRET` | Feedback form Siteverify |
| `STRIPE_SECRET_KEY` | Live Stripe (`sk_live_` or `rk_live_`) |
| `STRIPE_PUBLISHABLE_KEY` | Live Stripe (`pk_live_`) |
| `STRIPE_WEBHOOK_SECRET` | Live Stripe webhook (`whsec_`) |
| `AW_OWNER_EMAIL` | Owner watch identity (not the cloud mail destination) |
| `AW_ADMIN_EMAILS` | Comma-separated admin accounts. Missing or empty: no one is admin (fail closed) |
| `NOTIFY_TO` | Verified Email Routing destination (project inbox for alerts and sign-in links) |

`STRIPE_PORTAL_CONFIG_ID` is legacy and ignored.

Set `send_email.allowed_destination_addresses` in `cloudflare/wrangler.jsonc` to that same verified destination before a live deploy. The checked-in file uses a placeholder; do not commit a personal inbox address.

**Public / non-secret vars** in `cloudflare/wrangler.jsonc`:

- `AW_BASE_URL` = `https://agendawatch.app`
- `MAIL_FROM` = `alerts@agendawatch.app`
- `TURNSTILE_SITE_KEY` = public Turnstile site key

The container also receives `AW_CLOUD=1`, `AW_RUNTIME=cloudflare-containers`, `AW_PERSIST_URL`, `AW_EMAIL_RELAY_URL` (derived from `AW_BASE_URL`), plus the identity secrets `AW_OWNER_EMAIL`, `AW_ADMIN_EMAILS`, and `NOTIFY_TO`.

**Bindings:**

| Binding | Kind | Purpose |
| --- | --- | --- |
| `AW_CONTAINER` | Durable Object (`AgendaWatchContainer`) | The container instance, snapshot storage, daily-run lock |
| `EMAIL` | send_email | Fallback / cap notices to the verified Email Routing destination |
| `AW_RL` | rate limit | Per-IP limit for requests that may reach the container |
| `CF_VERSION_METADATA` | version metadata | Keys the stored anonymous home page to the Worker version |

There is no R2 bucket binding in the current `wrangler.jsonc`. Older comments in `cloudflare/persist.py` and `cloudflare/DEPLOY.md` still mention R2; treat `DEPLOY.md` as a pre-cutover note (Workers Paid, Email Routing, wrangler login). Snapshot bytes live in Durable Object SQLite.


## Database scripts

Schema and seed files live in `db/`. They are the SQLite layout the app creates, plus reference data only (the 17-city catalog, metros, public bodies, default keywords, and the official Cottonwood Heights planning-commission city-page source). They do not include accounts, emails, watches, feedback, Stripe IDs, or alert history.

```bash
python3 db/apply.py create  --db /tmp/agenda-watch.db
python3 db/apply.py seed    --db /tmp/agenda-watch.db
python3 db/apply.py migrate --db /tmp/agenda-watch-from-migrations.db
python3 db/apply.py seed    --db /tmp/agenda-watch-from-migrations.db
python3 db/apply.py verify  # schema.sql, migrations, and init_db must agree
```

`db/schema.sql` is the current full schema. `db/migrations/` is the same schema as numbered deltas, recorded in `schema_migrations` by filename stem. `db/durable_object.sql` documents the Worker Durable Object tables (snapshots and the daily-run lock). There is no D1, and the current Wrangler config has no Workers KV namespace or R2 bucket binding. See `db/README.md`.

## What this repo does not include

Secrets, `.env`, magic-link files, SQLite databases, `data/secret.key`, customer records, dated `*.bak*` copies, `node_modules`, virtualenvs, caches, logs, and the `cloudflared` binary. Recreate secrets in the Cloudflare dashboard or with `wrangler secret put` if you deploy from a new checkout.

Tunnel helper scripts under `scripts/` (`run_tunnel.sh`, `named_tunnel_setup.sh`, `watch_tunnel.sh`) are leftovers from the pre-Containers path; the live site does not use them.
