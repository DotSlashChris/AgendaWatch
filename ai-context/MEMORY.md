# Project notes

Herald's working notes, arranged by topic and scrubbed for a public repository. Prefer [PRODUCT_RULES.md](PRODUCT_RULES.md) and [DECISIONS.md](DECISIONS.md) when a note and a later decision disagree. Current live truth beats older profile text.

## Scope

Herald is the product bot for Agenda Watch only. The owner is the only approver. Refer to the owner as "the owner." In this public repo, use `owner@example.com` if an owner-address example is required, `inbox@example.invalid` for the project inbox, and `100 Example St` for a sample street. Business addresses `hello@agendawatch.app`, `alerts@agendawatch.app`, and `admin@agendawatch.app` are fine to name.

Work on AgentForge Media, Patch Notes, fleet installs, operating kits, landing-page ads, and other products is out of scope unless the owner explicitly switches the bot.

## Hosting and runtime

The site runs on Cloudflare Containers (Workers Paid), Worker name `agenda-watch`, one basic container. Cron is 8:21 AM America/Denver. Data snapshots live in Durable Object SQLite, not in an R2 binding in the current wrangler file. Older comments in `cloudflare/persist.py` and `cloudflare/DEPLOY.md` may still mention R2 or a pre-cutover checklist; trust current `wrangler.jsonc` and the root README.

There are no tunnels serving traffic. The 2026-10-14 cutover removed tunnel DNS. Helper scripts under `scripts/` (`run_tunnel.sh` and similar) are leftovers and are not used in production.

Operator habits that have worked: Node 22 at `~/.local/node22/bin`, Wrangler 4.149.0, Docker 26.1.4 with `dockerd --iptables=false`, `WRANGLER_DOCKER_BIN` pointed at `scripts/docker-hostnet`. Confirm every deploy on `/healthz`. The project is not required to be a git checkout on the build machine, but deploys should follow a merged pull request.

The live working tree on the operator box has been `/workspace/agenda-watch`. The public clone used for pull requests has been `/workspace/aw-export/repo`. Keep them aligned after merges.

## Email and identity

Alerts: Resend, From `alerts@agendawatch.app`. Feedback replies: From and Reply-To `hello@agendawatch.app`. `NOTIFY_TO` is the verified Email Routing destination (a wrangler secret, placeholder in the checked-in config). The Cloudflare `EMAIL` binding is a fallback for owner-bound notices, not the path for per-user alerts.

`AW_OWNER_EMAIL` identifies the owner's watch row. It is not the cloud mail destination. `AW_ADMIN_EMAILS` lists admin accounts (`admin@agendawatch.app` today). Missing admin secret means no admins. Those values used to live in `config.json` and in public wrangler vars; they must not be committed.

Internal calls use a mode-600 header file and `curl --header @/absolute/path/to/internal_secret.hdr`. A tilde inside `-H @…` can fail.

## Catalog quirks

Park City is grouped in the Heber metro in `config.json` because it is in the Heber micropolitan area (OMB 2023), even though people often think of it as its own place. Daniel is the Town of Daniel in Wasatch County. St. George has its own metro group. The home page city count should follow the catalog (17), not a hardcoded "15" or "10." The per-user cap stays 10.

`config.json` `_product_rules` is stale (it still says Stripe test and ten supported cities). Do not copy that string into new user-facing text.

## Reporting pitfalls

`/__aw/watch-list` is the source of truth for the account list and the user total. `/__aw/user-check` is a per-email read-only probe. An earlier report mistake counted watched cities the wrong way; do not repeat that. Always include the admin account when deciding whether a city is watched. If you cannot confirm why a match did or did not email, say "not verified."

The site fetch is independent of the assistant. If the assistant cannot send the 8:51 AM report (for example because usage on the assistant platform ran out, as happened on 2026-11-18), still do not invent a fetch result; the container may have run anyway.

Times in reports are America/Denver, with a short zone label. Convert timestamps that arrive in UTC.

## Cottonwood Heights and PMN files

The official Planning Commission agendas page on cottonwoodheights.utah.gov is the first city-page source. The Cloudflare container gets HTTP 403 there; other networks can get 200. There is no official off-domain alternate. Coverage for that city still comes through Utah Public Notice. The long-term idea is the home-server fetch app, which is on hold.

A set of Utah Public Notice PDF links (twelve, as of the mid-November runs) are still listed on notice pages but return a real "Not Found." Only one Draper notice in that set had a working replacement file, which the fetcher already reads. After two dates those dead links are "permanently missing" and should not be daily-error noise. A weekly recheck remains.

## Tests and deploys

Run `python3 -m pytest -q tests` from the repo root (not a broader path that would pick up backup trees). The three known billing-date failures are expected until someone is asked to fix them. Worker tests are `node --test test/*.test.ts` inside `cloudflare/`.

Never `POST /__aw/run` as a shortcut to "see if a deploy worked." Never send a test alert to a real user as part of a deploy. The December 2, 2026 Cottonwood Heights Planning Commission ZTA-26-006 notice was the example used to redesign the alert email; that email had already gone out on 2026-11-18, before the new layout.

## GitHub

The repository is public. After 2026-11-23, every code, config, or deploy change is a branch and a pull request. The operator machine uses a fine-grained token, stored as `GH_TOKEN`, limited to this repository, with Contents and Pull requests set to read and write. Do not put tokens in the repo or in chat. Do not merge unless the owner asked.

## Cost and safety

Daily caps (admin page, "Cost and safety"): fetch kill switch default on; run lock; 3,000 PMN fetch requests; 50 alert emails; 100 Resend sends; 60 snapshot uploads; 60 container wakes. Hitting a cap stops that work for the day and sends one notice to the project inbox. The Worker returns 404 for junk paths so bots do not keep the container awake. The owner handles Cloudflare billing alerts.

## What is outdated on purpose

Treat these as historical, not current:

- Stripe in test mode, "No charge yet," and "do not turn Stripe live"
- A ten-city catalog, or West Jordan and Layton permanently alerts-off
- A $15 extra-city price, or a charge per street
- Alerts that go only to the owner, or From a send.agendawatch.app address
- Morning report at 8:41 AM (it is 8:51 AM)
- Owner and admin addresses stored in `config.json`
- The old computer tunnel as the live host
- A separate unbuilt "feedback bot API" as the way feedback is handled (the in-app admin queue plus the 2026-11-12 standing permission are what exist)
