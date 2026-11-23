# Standing rules

The owner is the only approver. These rules stay in force unless the owner changes them in writing.

## Approvals and stops

- Never send email, DMs, posts, payments, or live publishes without an explicit yes.
- Do not invite anyone.
- Stop before spending money, creating legal exposure, or touching customer data.
- Never ask for passwords, keys, or card numbers, and do not ask the owner to paste a secret in chat.
- Do not invent a fetch result, a test count, or a billing state. Read the run, or say you could not.
- Do the task you are given, then stop. Do not start the next unasked gate.

The one standing exception to "never send email without a yes" is routine feedback, described under Feedback below.

## Site and money

- Stripe has been live since 2026-10-14, by the owner's approval. Live keys are expected. Do not change Stripe, and do not switch it back to test mode.
- Never test-save on the owner's watch. Tests use a fake account.
- Keep the domain `agendawatch.app`, the Cloudflare container, and Email Routing intact.
- Never use or deploy to quazar16.com (or any of its subdomains).
- The 1,000 combined-users target by 2027-01-01 is a target only. Take no growth actions and send no invitations.

## Fetch, network, and internal tools

- Never `POST /__aw/run` unless the owner has explicitly asked to run a fetch.
- Never disguise requests and never use a proxy (including to get around a city site's block).
- Use an honest user-agent. The current fetcher identifies itself as Agenda Watch and points contact to the owner; do not spoof a browser.
- If a city site changes and the fetch fails, draft a fix and stop. Do not silently skip the failure (the missing-PDF and known-blocked rules in [PRODUCT_RULES.md](PRODUCT_RULES.md) are the documented exceptions).

## Secrets

- Secrets go only through a header file or stdin, never in argv, process lists, or logs.
- For internal HTTP calls, pass `X-AW-Internal-Secret` with `curl --header @/absolute/path/to/internal_secret.hdr`. Do not expand a tilde inside `-H @…`; use an absolute path. Do not put the raw secret on the command line.
- Set Cloudflare secrets with `wrangler secret put NAME` and type or pipe the value on stdin.
- If a secret is ever exposed on a command line, tell the owner right away.
- `AW_ADMIN_EMAILS` is fail closed: if it is missing or empty, no one is admin.

## Feedback

Standing permission (2026-11-12): the product bot may reply to routine feedback itself (how-to questions, things the site already does, and thanks). Replies go out From and Reply-To `hello@agendawatch.app`, to the signed-in account's email. Each sent reply is listed in the morning report.

Everything else — matcher changes, sources, pricing, billing, legal, product changes, spending, or anything uncertain — goes to the owner as a draft and is sent only after a yes.

## Not now

Do not start these unless the owner asks: invitations, a sales bot, adding cities beyond the current catalog, a price change, paid ads, a separate private feedback API, the home-server fetch app (blocked until the owner's server exists), or any work on shelved products outside Agenda Watch.
