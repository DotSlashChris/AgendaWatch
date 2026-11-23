# Handoff guide

This is the starting brief for a new assistant taking over Agenda Watch. Read [RULES.md](RULES.md) first, then this file.

You are the product bot for Agenda Watch only (https://agendawatch.app). The bot that wrote this package is Herald. Stay on Agenda Watch unless the owner explicitly switches you.

## What Agenda Watch is

Agenda Watch emails a person when an upcoming public city agenda item matches the keywords or street on their watch. It reads Utah Public Notice (https://www.utah.gov/pmn/) and official city agenda pages. It is aimed at rental-license holders who want to know when a city might change rules that affect them.

It is not legal advice, and it does not file comments, pull permits, or watch HOA boards. Past meetings may appear as labeled examples on the site; they are never alert emails.

The live site is https://agendawatch.app (and www). The public source repository is this repo, under the owner's GitHub account, at the AgendaWatch project on github.com.

## Architecture

- **Cloudflare Worker** `agenda-watch` on custom domains `agendawatch.app` and `www.agendawatch.app`. It routes public pages, rate-limits, serves stored anonymous HTML, and forwards signed-in and API traffic to one container.
- **Cloudflare Container** (Durable Object class `AgendaWatchContainer`, basic instance, max 1). A Python 3.12 FastAPI app (`web/`) plus the daily fetcher (`agenda_watch.py`) run here. SQLite in the container holds users, watches, run logs, and related app data. Snapshots of that database (and a few state files) are stored in the Durable Object's own SQLite, not in a separate R2 binding in the current `wrangler.jsonc`.
- **Bindings:** `AW_CONTAINER` (the container / Durable Object), `EMAIL` (send_email fallback to the verified Email Routing destination), `AW_RL` (per-IP rate limit), `CF_VERSION_METADATA` (keys the stored anonymous home page to the Worker version).
- **Cron:** `21 14 * * *` and `21 15 * * *` UTC, so the job starts at 8:21 AM America/Denver in both MDT and MST.
- **Email:** Resend sends alerts From `alerts@agendawatch.app` and feedback replies From `hello@agendawatch.app`. `NOTIFY_TO` is the verified Email Routing destination (the project inbox). In this public repo it is a placeholder; the live secret is set with Wrangler.
- **Billing:** Stripe is live. Local `./run_web.sh` accepts test keys only and refuses live keys.
- **Identity:** `AW_OWNER_EMAIL` is the owner watch identity (the cloud mail path does not send to this address). `AW_ADMIN_EMAILS` is a comma-separated admin list; `admin@agendawatch.app` is the admin account and has every catalog city free. If `AW_ADMIN_EMAILS` is missing or empty, no one is admin.

The Herald bot profile still describes an older product (Stripe in test mode, ten cities, West Jordan and Layton alerts off, public copy that says "No charge yet"). Those lines are outdated. Trust this folder, `config.json`, and `/healthz` for current truth.

## How to deploy

Changes go through a pull request first. After the PR is merged, deploy from that revision. This package itself is docs only and is not a deploy.

1. Set `ENV AW_BUILD=YYYYMMDD-short-label` in `cloudflare/Dockerfile`. That label is what `/healthz` reports.
2. Use Node 22 and Wrangler 4.x (recent production deploys used 4.149.0):

   ```bash
   export PATH="$HOME/.local/node22/bin:$PATH"
   export WRANGLER_DOCKER_BIN="$PWD/scripts/docker-hostnet"
   cd cloudflare
   npx wrangler deploy
   ```

   `scripts/docker-hostnet` wraps Docker so image builds use `--network=host` (needed when dockerd is started with `--iptables=false`).
3. Wait until `https://agendawatch.app/healthz` shows the new `build` label. First boot after a deploy can take a minute or two.
4. Do not send email as part of a deploy, and do not `POST /__aw/run` unless the owner asked for a fetch.
5. Keep the custom domain, the container, and Email Routing intact.

On the operator machine, the live working tree has been `/workspace/agenda-watch`. Keep it in sync with this repository after merges. Dated `*.bak-*` copies may exist there; they are not in the repo.

## Internal endpoints

All of these require the header `X-AW-Internal-Secret` (the value of secret `AW_INTERNAL_SECRET`):

- `GET /__aw/status` — run and host status
- `GET /__aw/watch-list` — source of truth for accounts and the user count
- `GET /__aw/user-check` — read-only check for one email (app rows and Stripe summary). Do not use it to count watched cities or to produce the user total
- `GET /__aw/billing-check` — billing check
- `GET /__aw/feedback` — feedback queue

Call them with a mode-600 header file and an absolute path:

```bash
curl --header @/absolute/path/to/internal_secret.hdr https://agendawatch.app/__aw/status
```

Never put the secret on the command line, and never use a tilde inside `-H @…`. Do not `POST /__aw/run` unless the owner explicitly asked. Do not test-save on the owner's watch.

## Secret names only

Set these with `wrangler secret put NAME` (stdin only), plus `GH_TOKEN` in the operator secret store for GitHub:

| Name | Used for |
| --- | --- |
| `AW_INTERNAL_SECRET` | Internal `/__aw/*` header and container persist/email relay |
| `RESEND_API_KEY` | Alert and feedback email |
| `TURNSTILE_SECRET` | Feedback form Siteverify |
| `STRIPE_SECRET_KEY` | Live Stripe |
| `STRIPE_PUBLISHABLE_KEY` | Live Stripe |
| `STRIPE_WEBHOOK_SECRET` | Live Stripe webhook |
| `AW_OWNER_EMAIL` | Owner watch identity |
| `AW_ADMIN_EMAILS` | Comma-separated admin accounts (fail closed if missing) |
| `NOTIFY_TO` | Verified Email Routing destination |
| `GH_TOKEN` | Fine-grained GitHub token limited to this Agenda Watch repository (Contents and Pull requests, read/write), used by `gh` and git on the operator machine |

`STRIPE_PORTAL_CONFIG_ID` is legacy and ignored. Public vars (`AW_BASE_URL`, `MAIL_FROM`, `TURNSTILE_SITE_KEY`) stay in `wrangler.jsonc`. Do not commit a personal inbox; the checked-in `send_email.allowed_destination_addresses` uses a placeholder that must be replaced in the live config only.

## Daily morning report

A chat report is due at 8:51 AM America/Denver, after the 8:21 AM fetch. The site's own fetch still runs if the assistant is offline; only the chat report is missed in that case. Do not invent a fetch result.

Each report must include:

- The current number of users, as its own clearly visible line, taken from `GET /__aw/watch-list` (not from `user-check`)
- Fetch start and end times in America/Denver, with a short zone label
- How many notices were scanned, that all catalog cities were fetched, and whether the host stayed up
- Whether any alert emails went out, or silence
- For every official match, why an email did or did not go out (already sent, past meeting, or minutes already sent), or "not verified" if that cannot be confirmed
- Never call a city unwatched while any account, including `admin@agendawatch.app`, watches it
- Recurring permanently missing PDFs and known-blocked pages should not be re-listed as fresh daily errors once they have been logged that way; do report a brand-new 404, timeout, or hard failure
- Whether watches changed, and any new feedback or replies sent under the standing feedback permission

Silence is a result. A late or missing run is a result.

## Tests

From the repo root, so a sandbox is used and the real database is never touched:

```bash
python3 -m pytest -q tests
```

Expect three known date-dependent billing failures in `tests/test_city_lock.py` (`test_paid_lock_date_is_billing_date`) and `tests/test_period_end_webhooks.py` (`test_new_subscriber_any_event_order_keeps_period_end`, two cases). They pin a lock date that drifts as the calendar moves. Do not "fix" them as part of an unrelated change, and do not treat them as a reason to change Stripe. Any new failure beyond those three should be investigated before a deploy.

Worker tests (Node 22):

```bash
export PATH="$HOME/.local/node22/bin:$PATH"
cd cloudflare
npm install
npx tsc --noEmit
node --test test/*.test.ts
```

## Change process

Every Agenda Watch code, config, or deployment change goes through a branch, a commit, and a pull request to this repository. Do not edit in place on the live tree and ship from there. Do not put personal information in the public repo. Do not merge a pull request unless the owner asked for the merge.

`gh` on the operator machine authenticates with the `GH_TOKEN` environment secret. Use that. Do not store a GitHub token in a repo file, a shell history line, or a chat message.

## Current state (as of 2026-11-23)

- Live build: `20261123-secret-emails`, version `96dbfea6-ab81-4de8-bca9-610ba4e2ea99`, confirmed on `/healthz` (ok, Cloudflare Containers, Resend, Stripe live). This build moved owner and admin identity and `NOTIFY_TO` into Cloudflare secrets.
- Previous notable builds still in effect as product behavior: `20261119-email-format` (alert layout), `20261121-fetch-errors` (permanent-missing PDFs and known-blocked city pages).
- Public repo is in place, scrubbed of personal information, with a fine-grained GitHub token limited to this project.
- As of the 2026-11-23 morning report there were four accounts. Watches were unchanged. No alert email went out that morning. The December 2, 2026 Cottonwood Heights Planning Commission hearing had already been emailed on 2026-11-18 (old layout).
- The 2026-11-23 morning run logged the recurring dead PMN PDFs as permanently missing and the Cottonwood Heights Planning Commission page as known-blocked. From 2026-11-24 those lines should be off the daily error list.
- Stripe is live. Catalog is 17 cities, alerts on for all of them. Admin is `admin@agendawatch.app`.

## Pending

See [ROADMAP.md](ROADMAP.md). In short: relay each morning report; confirm the recurring 404/403 lines stay off the daily list; note the first alert in the new email layout; leave the home-server fetch app alone until the owner's server exists; fix the three billing tests only if asked; take no growth actions.
