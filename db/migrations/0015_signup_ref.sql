-- Signup attribution, 2026-11-27: first-touch ?ref=<code> (lowercase a-z0-9-, max 40 chars).
-- users.ref is set once when a NEW account is created; NULL = direct. magic_nonces.ref carries the code
-- from the sign-in request to the email link. No IP address or other visitor data is stored.
ALTER TABLE users ADD COLUMN ref TEXT;
ALTER TABLE magic_nonces ADD COLUMN ref TEXT;
