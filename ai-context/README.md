# AI context for Agenda Watch

This folder is a handoff package so a new assistant can continue Agenda Watch without the prior conversation. It is documentation only: it does not change the running site.

Everything here is written for the public repository. It uses placeholders where a real inbox or personal address would otherwise appear (`owner@example.com`, `inbox@example.invalid`, `100 Example St`). It does not contain secrets, customer data, or the live database.

## Read order

1. [RULES.md](RULES.md) — standing owner rules and approval boundaries. Read this before doing any work.
2. [HANDOFF.md](HANDOFF.md) — what the product is, how it is hosted, how to deploy, how to test, the daily report, and what is current versus pending.
3. [PRODUCT_RULES.md](PRODUCT_RULES.md) — matcher, streets, sources, alerts, billing, catalog, and form rules.
4. [DECISIONS.md](DECISIONS.md) — dated decision log, oldest first.
5. [MEMORY.md](MEMORY.md) — project notes arranged by topic.
6. [ROADMAP.md](ROADMAP.md) — planned work, open items, and the not-now list.

The running application and deploy files live in the rest of this repository. The live operator tree on the build machine is kept in sync with this repo; going forward, every code, config, or deploy change goes through a branch and a pull request, then a deploy from that revision.

Other products (including AgentForge Media, Patch Notes, and any other work outside Agenda Watch) are out of scope here.
