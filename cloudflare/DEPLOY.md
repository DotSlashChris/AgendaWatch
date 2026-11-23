# Agenda Watch — Cloudflare Containers deploy notes

The live site is already on Cloudflare Containers (Worker `agenda-watch`, custom domain `agendawatch.app`). This file is a short operator checklist, not a first-time cutover plan.

Node 22: `export PATH=~/.local/node22/bin:$PATH`. Docker builds use the `docker-hostnet` wrapper (see the root README).

## Secrets

Set production secrets with Wrangler. Type or pipe the value on stdin; never pass it as a command-line argument:

```
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

`AW_OWNER_EMAIL` is the owner watch identity (the cloud mail path does not send to this address). `AW_ADMIN_EMAILS` is a comma-separated list of admin accounts; if it is missing or empty, no one has admin access. `NOTIFY_TO` must be the verified Email Routing destination (the project inbox). Before a live deploy, set `send_email.allowed_destination_addresses` in `wrangler.jsonc` to that same address. The checked-in file uses a placeholder; do not commit a personal inbox.

`STRIPE_PORTAL_CONFIG_ID` is legacy and ignored.

## Build and confirm

Label the image in `cloudflare/Dockerfile` (`ENV AW_BUILD=YYYYMMDD-label`), deploy, and confirm `https://agendawatch.app/healthz` shows that build.

Email Routing on `agendawatch.app` must stay enabled (MX/SPF/DKIM) with the verified destination configured in the dashboard. Keep the domain, container, and Email Routing intact.
