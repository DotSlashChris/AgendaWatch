# Roadmap

## On hold until the owner is ready

**Home-server fetch app (added 2026-11-21).** Build a small app that runs on a local server at the owner's house (likely Linux) and performs daily fetches from a network that is not Cloudflare's egress. Agenda Watch then pulls results from that server. The first test is the Cottonwood Heights Planning Commission agendas page, which returns 403 to the Cloudflare container and 200 from other networks. The owner will set up the server first, and that will not happen for a while. Do not start this work before then.

## Open items

- Confirm, beginning with the 2026-11-24 morning report, that the recurring twelve PMN PDF 404s and the Cottonwood Heights 403 no longer appear as daily errors (they should already have been logged as permanently missing or known-blocked).
- Note the first real user alert that goes out in the `20261119-email-format` layout. The December 2, 2026 Cottonwood Heights Planning Commission hearing was emailed on 2026-11-18, before that layout was live.
- Optional, only if the owner asks: fix the three date-dependent billing tests in `tests/test_city_lock.py` and `tests/test_period_end_webhooks.py` (they currently expect a November lock date and compute a December date).
- Optional, only if the owner asks: compare current Cloudflare usage with the roughly $5–5.30 per month projection made after the 2026-10-21 bot-filter and cost-guard work. That comparison has not been re-run recently.
- The owner's own hand test of signed-in feedback, if not already done.

## Not now

Do not start these unless the owner asks:

- Invitations, a sales bot, paid ads, or other growth actions
- Adding cities beyond the current 17, or changing the $5 per extra city price
- A separate private feedback API for another bot (feedback is already handled in the app; see [RULES.md](RULES.md))
- The home-server fetch app, until the owner's server exists
- Work on shelved products outside Agenda Watch (AgentForge Media, Patch Notes, and similar)
- Reopening Stripe configuration, Email Routing, or the custom domain
