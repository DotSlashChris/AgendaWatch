-- _ensure_column calls in init_db for Stripe fields.
ALTER TABLE users ADD COLUMN stripe_customer_id TEXT;
ALTER TABLE users ADD COLUMN stripe_subscription_id TEXT;
ALTER TABLE users ADD COLUMN subscription_status TEXT;
ALTER TABLE users ADD COLUMN paid_updated_at TEXT;
ALTER TABLE users ADD COLUMN extra_cities INTEGER;
ALTER TABLE users ADD COLUMN billing_synced_at REAL;
