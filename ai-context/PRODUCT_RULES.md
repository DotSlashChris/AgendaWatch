# Product rules

These are the current product rules. Some older notes and the `_product_rules` string in `config.json` still say "Stripe test" and "ten supported cities." That text is stale. Stripe is live, the catalog has 17 cities, and the per-user cap remains 10 cities.

Agenda Watch is not legal advice. It does not file comments, pull permits, or speak for any city. It does not watch HOA boards.

## Catalog and plans

Seventeen Utah cities, modeled as state → metro → city, with stable city keys in `config.json`:

- Salt Lake metro: Cottonwood Heights, Salt Lake City, Sandy, Draper, West Jordan, Murray, South Jordan, Midvale
- Ogden-Clearfield metro: Ogden, Layton, Farmington
- Provo-Orem metro: Provo, Orem, Vineyard
- Heber metro: Daniel (Town of Daniel, Wasatch County), Park City (Summit County; grouped here because it is in the Heber micropolitan area)
- St. George metro: St. George

The per-user plan cap is 10 cities (`aw_cities.MAX_CITIES`). Adding a city to the catalog is an owner decision and is not the same as raising that cap.

- Free: one city and one optional street on that city. A free account cannot turn on a second city.
- Paid: $5 per extra city per month, billed through Stripe. The street is included; there is no per-street charge. Allowed cities = 1 + paid extra cities, capped at 10.
- Admin accounts (`AW_ADMIN_EMAILS`) get every catalog city and street free, with no city lock and no billing requirement.
- Public price copy (approved 2026-10-21): "Your first city is free. Watch one city and one street at no cost, with no card needed. Want more cities? Add them for $5/month each, up to 10 cities total. Cancel anytime; extra cities stay active through the month you paid for."
- Adding a paid extra city mid-month is prorated and charged immediately. Cancels and decreases take effect at the end of the billing period. Streets are kept when a city is turned off.
- The public Stripe statement descriptor suffix is `AGWATCH`. Do not change the public business name.

### Alert-on for a city

`config.json` `alerts_off_cities` is currently empty: alerts are on for all 17 cities. A newly added city stays alerts-off until its first scan. If that scan shows a flood of junk hits, alerts stay off and the owner is told. Otherwise alerts are turned on. West Jordan and Layton were later turned on by the owner (2026-10-25). Park City and St. George were turned on after their first scan showed no flood (2026-11-03).

Default keywords: short-term rental, STR, rental license, business license, zoning, conditional use, transient tax, occupancy. Users can edit their list and reset it to this default.

## Matcher

- Past meetings are labeled examples only, never alert emails. The home page may show a labeled past example.
- Upcoming meetings only produce alerts.
- A street matches only in its own city's agendas, never across cities.
- A user's street adds matches and must never hide a city-wide hit. An item is sent if it names the user's street. An item that matches the keywords and names no street is also sent.
- Weak terms (zoning, conditional use, business license, occupancy, and, since 2026-10-19, transient tax) match only with a rental term in the same item.
- Bare "STR" matches only as a whole token (word boundaries) or as "short-term rental." It must not match "STREET," garbled site-plan text, or DNA/lab text, and it needs rental or housing context.
- Short-term rental used only as a tax base is dropped. Land-use table rows are dropped. Certificates of occupancy do not count as occupancy.
- A short-term rental mention that is only market data (demand, revenue, bookings) inside a lodging, transient-room, or sales-tax revenue report is not a rental-rule hit. A real rental-rule item still matches.
- False matches are bugs.
- Address matches count as hits even with weak keywords, for that city's street only.

## Minutes and titles

- Do not drop minutes; label them. A minutes match emails once, when first posted, as a past "discussed" item. The subject and heading must say the discussion already happened, never "upcoming agenda item." The same minutes are never emailed again when a later packet includes them for approval. Real upcoming agenda lines still email as upcoming items.
- A heading such as "DRAFT MINUTES FOR JULY 15, 2026" counts as minutes and is sent as a past "discussed" item.
- The stored agenda title and the alert title must be the agenda line that actually matched, not another item or a section heading.
- The alert title stops at the matched agenda item, with no closed-meeting line or other text appended.
- The city name is never repeated in the meeting body name (for example, "Provo Municipal Council," not "Provo Provo Municipal Council").

## City sources

- Official city agenda pages may be added as sources, proactively, without waiting for a missed alert.
- A source is legitimate only if it is on that city's own domain and the page is agendas, packets, or minutes. No other URL may be added.
- When a Utah Public Notice hit for a catalog city includes a link that passes this rule, that page is added as a source for that city.
- The first Cottonwood Heights source is the Planning Commission agendas, packets, and minutes page on cottonwoodheights.utah.gov.
- A packet found on an added city page that is not yet on Utah Public Notice is matched like a PMN packet. A new upcoming hit emails through the normal alert path, never as a hand-written email.
- If a listing page loads and a new packet link appears but the PDF itself is blocked, the alert emails the link with no excerpts and says the packet could not be read.
- If the listing page itself is blocked, write that in the run log only: no guessing and no email from a blocked page.

### City-source cap (no backfill)

A document from a city page emails only if the meeting is upcoming and only if that document was first seen after the page was added as a source. The source's first successful scan marks everything already present as baseline. Do not backfill past packets.

## Missing PDFs and blocked pages (fetch errors)

- A Utah Public Notice PDF that returns 404 is logged and skipped. It does not fail the city and does not withhold that city's email.
- After a dead PMN PDF URL misses on two different America/Denver dates, the run logs it once as permanently missing and stops retrying it every day. A weekly recheck picks it up if the file returns. A new file on the same notice is still fetched. The URL builder is not the cause of the known dead links; Utah Public Notice still lists files that are gone.
- A 403, a timeout, or a connection error on a notice or PDF is still a hard failure: it fails the city and withholds that city's email.
- A city listing page that keeps returning 403 (the Cottonwood Heights Planning Commission page does this from Cloudflare's egress, while other networks may get 200) is, after two dates, logged once as known-blocked. It is not counted as a fresh daily error after that. The city stays covered through Utah Public Notice. A weekly recheck runs; the first successful scan is baselined under the city-source cap.
- A brand-new 404, a timeout, or a PDF 403 is still reported the first day it happens.
- Never disguise requests or use a proxy to work around a block.

## Alerts and email

- When the morning fetch finds an upcoming hit, it emails each matching user at their own address. It emails only users whose watch includes that city. The same hit is never emailed twice to the same user.
- If a city later posts a new packet or document for a meeting that was already alerted on, a new alert is expected from the new document. If no new alert is sent, treat that as a bug.
- A failed fetch reports in chat and does not email the owner from the failure. Do not send a "no change" email.
- Alerts send From `alerts@agendawatch.app` via Resend. Feedback replies send From `hello@agendawatch.app`. The Cloudflare `EMAIL` binding is a fallback for owner-bound notices to the verified Email Routing destination (`NOTIFY_TO`).
- If a title is missing or "not identified," omit that line or use a short neutral phrase such as "Matching item."
- The current professional layout (build `20261119-email-format`) uses a clear subject of the form `Agenda Watch: {City} {Body} — {Month Day, Year}`. The meeting name and date are the heading. Excerpts use real bullet points or short paragraphs, not raw bullet characters jammed on one line. The email keeps matched terms, the meeting name and date, the source PDF link, the PMN notice link, the legal disclaimer, and the manage-watch link.
- Lead with the actual agenda item title. The matched sentence goes under "Excerpt." Definition text quoted from inside a packet must never be presented as the agenda item, and should be dropped from the excerpt; keep the actual rental sentence.
- Counts use correct singular/plural ("item," not "item(s)" for one).
- Never repeat a URL in the email, and drop a "Source URL" that equals the PMN notice link.

## Run records

- If an alert email went out, the run outcome must not be recorded as "silence."
- The daily fetch scans every catalog city every morning, including cities no current user is watching.
- Notices already seen should not be re-downloaded or re-analyzed, but amended or changed notices must still be detected and not skipped by ID alone.
- Daily fetch deletes notices and run records older than 365 days, never users, watches, or alert history.

## City lock

- Enabled cities lock until the next unlock date after a save. For free accounts this is the 1st of next month. For paid accounts it is the Stripe billing date. Streets stay saved.
- When the extra-city count drops, the most recently added cities are turned off and stay off until the next billing date.
- Admin accounts are exempt from the lock and can change enabled cities at any time.
- The "locked" indicator next to a city appears only after Save, on saved cities only, never on a box that has just been ticked.
- The "Saving locks your selected cities until [date]" text appears only when the user can actually change and save their city selection, not while cities are already locked.

## Watch form

- The watch screen is a flat city list, grouped by metro, not a three-level picker. Each city row has an optional "Street to watch" for that city only, with no zip, state, or mailing block.
- No validation errors are shown before the user clicks Save. Errors appear only after Save, in one place. The Save button stays clickable.
- Save validates that the correct number of cities is selected for the plan.
- A partial settings save keeps existing cities, streets, and alerts. A missing checkbox is not an implicit off.
- Users can reset keywords to the default list.

## Feedback

- Only signed-in users can submit feedback. The Feedback nav link shows only when signed in. There is no reply-to field; replies go to the account's email.
- Statuses are new, needs you (stored as `needs-owner`), replied, and reviewed.
- See [RULES.md](RULES.md) for who may send a reply.
- Optional Cloudflare Turnstile is used on the feedback form when both the public site key and the secret are set.
