"""Identity secrets: AW_OWNER_EMAIL / AW_ADMIN_EMAILS are not in config.json; admin fails closed."""
import os

import aw_cities
import web.app as webapp
from conftest import OWNER

ADMIN = "admin@agendawatch.app"


def test_config_json_has_no_identity_keys():
    raw = (webapp.ROOT / "config.json").read_text()
    assert '"owner_email"' not in raw
    assert '"admin_emails"' not in raw
    cfg = aw_cities.load_config(webapp.ROOT / "config.json")
    assert cfg["owner_email"] == OWNER
    assert ADMIN in cfg["admin_emails"]


def test_is_admin_uses_env_list(monkeypatch, sandbox):
    webapp.init_db()
    assert webapp.is_admin(ADMIN) is True
    assert webapp.is_admin(OWNER) is False
    assert webapp.is_admin("stranger@example.invalid") is False


def test_is_admin_fails_closed_when_secret_missing(monkeypatch, sandbox):
    webapp.init_db()
    # Even if the database still has is_admin=1 from an earlier init, a missing
    # secret must deny access (never open admin to everyone).
    with webapp.db() as c:
        c.execute("UPDATE users SET is_admin=1 WHERE email=?", (ADMIN,))
        c.execute("UPDATE users SET is_admin=1 WHERE email=?", (OWNER,))
        # also try a leftover row
        c.execute("INSERT OR IGNORE INTO users(email, created_at, is_admin) VALUES(?,?,1)",
                  ("stranger@example.invalid", webapp.now_iso()))
        c.execute("UPDATE users SET is_admin=1 WHERE email=?", ("stranger@example.invalid",))
    monkeypatch.delenv("AW_ADMIN_EMAILS", raising=False)
    assert aw_cities.admin_emails() == []
    assert webapp.is_admin(ADMIN) is False
    assert webapp.is_admin(OWNER) is False
    assert webapp.is_admin("stranger@example.invalid") is False


def test_is_admin_fails_closed_when_secret_blank(monkeypatch, sandbox):
    monkeypatch.setenv("AW_ADMIN_EMAILS", "  ,  ")
    assert webapp.is_admin(ADMIN) is False
    assert webapp.is_admin(OWNER) is False


def test_load_config_injects_empty_identity_when_env_missing(monkeypatch):
    monkeypatch.delenv("AW_OWNER_EMAIL", raising=False)
    monkeypatch.delenv("AW_ADMIN_EMAILS", raising=False)
    cfg = aw_cities.load_config(webapp.ROOT / "config.json")
    assert cfg["owner_email"] == ""
    assert cfg["admin_emails"] == []
