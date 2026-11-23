# Decision log

Oldest first. Dates are America/Denver. Build labels and versions are included when they were recorded; some earlier deploys were not written down with a full version id, and those ids are not invented here.

## 2026-10-05 — Start Agenda Watch

Build a self-serve email watch on public city agendas, first for Cottonwood Heights and Salt Lake City via Utah Public Notice, under the owner's existing company. Address matches count as hits even with weak keywords. The product does not give legal advice, file comments, pull permits, or watch HOA boards. Other product work was shelved so this could be the focus.

## 2026-10-06 — First unattended fetch; Stripe work begins in test mode

The first unattended daily run succeeded silently around 8:32 AM. Stripe Checkout and Customer Portal work started in test mode (an early $15 idea was later replaced). The watch form was rewritten so each optional street belongs to its city row.

## 2026-10-07 — Domain is agendawatch.app; never use quazar16.com

Serve the app at https://agendawatch.app over HTTPS on a domain the owner already controls. Do not buy a domain. Do not deploy to quazar16.com or any of its subdomains. The owner confirmed the site loaded and said not to invite anyone.

## 2026-10-08 — Ten-city catalog, $5 extra-city price, matcher tests, report format

Supported cities became exactly ten Utah cities, grouped by metro. Price: free is one city plus one street; paid is $5 per extra city, street included, never per street, cap ten. Daily fetch scans every supported city at 8:21 AM. Morning chat reports run time, per-city notice counts, and hit, silence, or failure. Bare "STR" must not match "STREET" or site-plan fragments. Never test-save on the owner's watch. False matches are bugs.

## 2026-10-09 — Billing on its own page; watch form stays flat

Plan & billing moves off My watch onto its own Billing page with a top-right nav link. Checkout success and cancel copy must match the current plan and must not mention test mode. The watch screen stays a flat city list with an optional street on each row.

## 2026-10-12 — Move to Cloudflare Containers; standing brief; admin and feedback

The current app (not a rewrite) moves onto Cloudflare Containers on the Workers Paid plan, with the daily fetch running there. Keep the domain, keep invitations off. Standing operating rules were recorded: never send email, DMs, posts, payments, or live publishes without an explicit yes; stop before spending, legal exposure, or customer data; never invent a fetch result; do the task, then stop. An admin page and a public feedback form were approved (screenshots first). Stripe was still test mode on this date (superseded on 2026-10-14). The first Cloudflare fetch was set for 2026-10-13 at 8:21 AM, with a chat report originally at 8:41 AM.

## 2026-10-13 — Fetch efficiency

Notices already seen should not be re-downloaded or re-analyzed, but amended or changed notices must still be detected and not skipped by ID alone.

## 2026-10-14 — First Cloudflare report accepted; Herald; Stripe live; Resend chosen

The owner accepted the first Cloudflare morning report. The old tunnel DNS is gone; the site is served only by Cloudflare Containers. The product bot was named Herald. The owner explicitly ordered a switch to live Stripe (live keys only, no test-mode wording, no invitations). Adding an extra city mid-month is prorated and charged immediately; cancels take effect at period end so customers keep the city through the month they paid for. Resend was chosen so any address can receive sign-in links and alerts. Keep the domain, container, and Email Routing intact.

## 2026-10-16 — Resend From alerts@agendawatch.app; per-user alerts

Outbound mail uses the verified domain agendawatch.app (not a send. subdomain) and From `alerts@agendawatch.app`. When the morning fetch finds an upcoming hit, each matching user is emailed at their own address, and only if their watch includes that city. The same hit is never emailed twice.

## 2026-10-17 — Street rule, email title/excerpt, form validation, lock wording

A user's street adds matches and must never hide a city-wide keyword hit. Alert emails lead with the real agenda item title; the matched sentence goes under "Excerpt"; packet definitions are not titles and are dropped from excerpts. Form errors appear only after Save, in one place, and Save stays clickable. Lock wording and the locked indicator appear only when they are true.

## 2026-10-17 (also) — Draft a fix if a city site changes

If a city site changes and the fetch fails, draft a fix and stop; never silently skip. (Later missing-PDF and known-blocked rules are the documented exceptions.)

## 2026-10-19 — Tighten Provo-style noise

Build `20261019-matcher-provo-noise`. Transient tax becomes a weak term; short-term rental used only as a tax base is dropped; land-use table rows are dropped; certificates of occupancy do not count as occupancy; bare STR needs rental or housing context and never DNA/lab text. Provo counted hits fell sharply.

## 2026-10-21 — Five more cities; new-city alerts rule; price copy; cost guards

South Jordan, Midvale, Farmington, Vineyard, and Daniel (Heber metro) were added, bringing the catalog to 15. A new city stays alerts-off until its first scan; a junk flood keeps alerts off. Approved home-page price copy: first city free, $5/month per extra city, up to 10 total, cancel anytime with extra cities active through the paid month. Worker bot filter plus admin cost guards (fetch kill switch, run lock, daily caps of about twice normal use, snapshot interval). Build includes `20261021` guard work. The projected Cloudflare cost after filtering was about $5–5.30 a month; that figure has not been recently re-verified.

## 2026-10-22 — Admin account swap

Build `20261022-admin-swap`. `admin@agendawatch.app` is the admin account and has unlimited free cities and no city lock. The owner's personal account is a standard user. Owner and admin identity later moved out of `config.json` into secrets (see 2026-11-23).

## 2026-10-23 — Secrets never in argv

If a command would put a secret on a command line, don't. Tell the owner immediately if it happens. Secrets go through a header file or stdin.

## 2026-10-25 — West Jordan and Layton alerts on; 1,000-user target

Build `20261025-wj-layton-alerts-on`. After a first-scan review (Layton's only hit was a past meeting), the owner turned those cities' alerts on. The owner also set a 1,000 combined target by 2027-01-01 across Agenda Watch, another product, and a separate store's orders. It is a target only; take no growth actions.

## 2026-10-26 to 2026-10-28 — Minutes labeled; run-record; titles

Don't drop minutes; label them. Minutes email once, when first posted, as a past "discussed" item, never as upcoming, and are not emailed again when later packets include them for approval. A draft-minutes heading counts as minutes. If an alert email went out, the run must not be recorded as silence. If a city posts a new packet for a meeting that was already alerted, a new alert is expected. The stored and emailed title must be the agenda line that actually matched. Builds in this window include `20261027-minutes-labeled`.

## 2026-10-30 — Cleaner Provo email bodies; source-link dedupe

Build `20261030-provo-email-tidy`. Do not repeat the city name in the meeting body name. The alert title stops at the matched item. Never repeat a URL, and drop a Source URL that equals the PMN notice link (dedupe shipped with the next day's city-sources deploy).

## 2026-10-31 — Official city pages as sources

Build `20261031-city-sources` (recorded version `99b9a36a`; this may be a shortened id). Add official city agendas/packets/minutes pages on the city's own domain, including automatically from qualifying PMN links. Cottonwood Heights Planning Commission is the first source. Blocked listing page: log only, no email. Listing ok but PDF blocked: email the link with no excerpts. Never disguise requests or use a proxy. The container receives 403 from that Cottonwood Heights page (Akamai / egress block).

## 2026-11-01 — Morning report contents and time

The chat report runs at 8:51 AM. It reads every account's watch, including the admin account, and never calls a city unwatched while any account watches it. For every match it says why email did or did not go out, or "not verified." It covers the whole catalog and shows blocked city pages.

## 2026-11-03 — Park City and St. George; lodging-tax noise

The owner added Park City and St. George (catalog now 17; per-user cap still 10). Scan first; if the city floods with junk, leave alerts off. Both were turned on after the first scan showed no flood. A short-term rental mention that is only market data in a lodging or tax report is not a hit; a real rental-rule item still is.

## 2026-11-04 — Stream PDFs; city-source cap (no backfill)

Build `20261104-stream-pdfs` (out-of-memory fix), then `20261104-city-cap`. A city-page document emails only if the meeting is upcoming and the document was first seen after the page was added as a source. The first successful scan is the baseline. No backfill.

## 2026-11-06 — User count in the morning report

The morning report includes the current number of users from live data, as a separate visible line. `/__aw/watch-list` is the source of truth for that count.

## 2026-11-12 — Feedback handling and standing reply permission

Herald manages feedback in the existing app. Routine replies may be sent From and Reply-To `hello@agendawatch.app`. Everything else goes to the owner as a draft. Statuses: new, needs you (stored as `needs-owner`), replied, reviewed. Canonical URL work also landed around this date (`20261112`).

## 2026-11-13 — Signed-in feedback; Sandy Historic Commission removed; watch-list

Only signed-in users can submit feedback; the nav link shows only when signed in; there is no reply-to field; replies go to the account's email (confirmed 2026-11-13/14). Sandy City Historic Commission was removed from the catalog bodies. `/__aw/watch-list` is the account list used for reporting (do not use `/__aw/user-check` to count watched cities).

## 2026-11-14 — Missing-PDF rule

A Utah Public Notice PDF that returns 404 is logged and skipped. It does not fail the city or withhold that city's email. A 403, a timeout, or a connection error still fails the city and withholds email.

## 2026-11-19 to 2026-11-20 — Professional alert email layout

Build `20261119-email-format`, version `230f7ae3-7d01-4ae5-9135-9805e5781698`, live 2026-11-20 at 12:02 AM. Subject of the form `Agenda Watch: {City} {Body} — {date}`. Meeting name and date are the heading. Missing titles become a short neutral phrase such as "Matching item." Excerpts use real bullets or short paragraphs. Disclaimer and manage-watch link stay. Matching logic was not changed. The owner approved the before/after.

## 2026-11-21 — Stop repeating daily 404/403 noise; home-server fetch on the roadmap

Build `20261121-fetch-errors`, version `bb0ffc0f-49d7-4446-b1b5-fea3bbe365aa`, confirmed 10:39 AM. The twelve recurring PMN 404s are real missing files, not a bad URL builder. After two distinct dates they are logged once as permanently missing, with a weekly recheck. The Cottonwood Heights Planning Commission page is logged once as known-blocked after two 403 dates (PMN coverage continues). A brand-new hard failure is still reported the first day.

The owner also added a roadmap item: a small fetch app on a home Linux server so Agenda Watch can pull results from a network that is not blocked, tested first on that Cottonwood Heights page. On hold until the owner sets up the server.

## 2026-11-23 — Public repository; identity secrets; pull-request process

The owner asked for the full application, including DevOps and database scripts, in a public GitHub repository, with every later change going through a commit and a pull request. Personal information was scrubbed (including two screenshots the owner deleted). Owner and admin addresses came out of `config.json`, and `OWNER_EMAIL` / `NOTIFY_TO` came out of public wrangler vars.

Build `20261123-secret-emails`, version `96dbfea6-ab81-4de8-bca9-610ba4e2ea99`. New secret names: `AW_OWNER_EMAIL`, `AW_ADMIN_EMAILS` (fail closed), and `NOTIFY_TO` as a secret. Admin access was confirmed after deploy. A fine-grained GitHub token limited to this repository, stored as `GH_TOKEN`, is what the operator machine uses for `gh` and git. This `ai-context/` folder was then added so a new assistant can continue from here.
