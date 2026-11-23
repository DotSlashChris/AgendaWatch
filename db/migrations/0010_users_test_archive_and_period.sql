-- Archived test-mode Stripe ids, then cancel-at-period-end columns.
ALTER TABLE users ADD COLUMN test_stripe_customer_id TEXT;
ALTER TABLE users ADD COLUMN test_stripe_subscription_id TEXT;
ALTER TABLE users ADD COLUMN test_subscription_status TEXT;
ALTER TABLE users ADD COLUMN test_extra_cities INTEGER;
ALTER TABLE users ADD COLUMN cancel_at REAL;
ALTER TABLE users ADD COLUMN period_end REAL;
