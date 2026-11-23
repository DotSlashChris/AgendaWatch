# Database scripts

These scripts describe the SQLite database the Agenda Watch app uses (`data/agenda_watch.db`). They contain **no customer data**.

## What lives where

| File | Purpose |
| --- | --- |
| `schema.sql` | Current full schema: every application table, index, and check the code creates. Apply this to build a fresh empty database. |
| `migrations/` | Numbered deltas that reconstruct the same schema from the code’s original `CREATE` statements plus later `ALTER` / `_ensure_column` steps. |
| `seed/catalog.sql` | Reference catalog (17 cities, metros, public bodies, default keywords, PMN window settings) and the official Cottonwood Heights planning-commission city-page source. |
| `apply.py` | Create, migrate, seed, or verify. |
| `durable_object.sql` | Durable Object SQLite tables used by the Worker for snapshots and the daily-run lock. **Not** part of `agenda_watch.db`. |

There are no triggers. There is no D1 database. The current `cloudflare/wrangler.jsonc` has no Workers KV namespace and no R2 bucket binding. Snapshot bytes and a small key/value map live in Durable Object SQLite (see `durable_object.sql`).

The copy of `data/agenda_watch.db` on the operator box can lag the live container. `schema.sql` follows the **current code**, not that stale file.

## Apply

From the repository root, with a **new** file (do not point this at a live customer database):

```bash
# Path A — current schema in one step
python3 db/apply.py create  --db /tmp/agenda-watch.db
python3 db/apply.py seed    --db /tmp/agenda-watch.db

# Path B — replay migrations in order, then the same seed
python3 db/apply.py migrate --db /tmp/agenda-watch-from-migrations.db
python3 db/apply.py seed    --db /tmp/agenda-watch-from-migrations.db

# Confirm both paths (and the app's init_db) agree
python3 db/apply.py verify
```

`migrate` records each file in `schema_migrations` using the filename stem (`0001_core_accounts`, …) and `applied_at` in America/Denver. Already-applied ids are skipped. The running app also inserts its own markers (`stripe_live_v1`, `admin_swap_20261022`, `city_lock_v1`, `feedback_handling_20261112`) when `init_db()` runs; those names are reserved.

`seed` does **not** insert users, watches, emails, feedback, Stripe ids, or alert history. Starting the web app still may create admin rows from `config.json`; that is application startup, not this seed.

## App tables in schema.sql

`users`, `watches`, `watch_cities`, `magic_nonces`, `alerts_sent`, `schema_migrations`, `city_sources`, `city_source_docs`, `fetch_runs`, `notices`, `documents`, `missing_pmn_pdfs`, `user_alerts`, `settings`, `guard_usage`, `guard_trips`, `guard_lock`, `feedback`, `feedback_hits`, `feedback_spam`.

Seed-only tables (created by `seed/catalog.sql`, not by the app): `ref_metros`, `ref_cities`, `ref_city_bodies`, `ref_keywords`, `ref_settings`.
