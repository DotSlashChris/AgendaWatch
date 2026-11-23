"""Shared fixtures for Agenda Watch tests.

Everything here is a sandbox:
  * agenda_watch's STATE / OUTBOX / HISTORY / DB / CACHE / ROOT are redirected to tmp_path,
    so tests never touch the real state/, outbox/, data/agenda_watch.db or config.json.
  * All network is blocked (socket connect, requests session, SMTP). The only "PMN" the
    code can reach is FakePMN, an in-memory stand-in for https://www.utah.gov/pmn/.
  * time.sleep is a no-op so get()'s retry back-off doesn't slow the suite.
"""

import os

# Identity secrets: set before agenda_watch / web.app are imported so OWNER/ADMIN
# match the test fixtures. Production reads these from wrangler secrets.
os.environ.setdefault("AW_OWNER_EMAIL", "owner@example.com")
os.environ.setdefault("AW_ADMIN_EMAILS", "admin@agendawatch.app")
os.environ.setdefault("NOTIFY_TO", "inbox@example.invalid")

import copy
import datetime as dt
import json
import smtplib
import socket
import sqlite3
import sys
import types
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

# Never let any test import of web.app (which runs init_db/migrations) touch data/agenda_watch.db.
import os as _os, tempfile as _tempfile  # noqa: E402
_os.environ.setdefault("AW_DB", str(Path(_tempfile.mkdtemp(prefix="aw-test-")) / "import.db"))

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import agenda_watch as aw  # noqa: E402
import aw_cities  # noqa: E402

TZ = aw.TZ
OWNER = "owner@example.com"
CH = "Cottonwood Heights"
SLC = "Salt Lake City"
CH_BODY = "City Council"
SLC_BODY = "Salt Lake City Council"
# Fixed "now" for deterministic upcoming/past logic: Wed Oct 7 2026, 3:00 PM America/Denver.
FROZEN_NOW = dt.datetime(2026, 10, 7, 15, 0, tzinfo=TZ)


def grant_admin(email, db=None):
    """List email in AW_ADMIN_EMAILS (and optionally set users.is_admin=1) for a test."""
    email = (email or "").strip().lower()
    cur = [e.strip() for e in (os.environ.get("AW_ADMIN_EMAILS") or "").split(",") if e.strip()]
    if email and email not in cur:
        os.environ["AW_ADMIN_EMAILS"] = ",".join(cur + [email])
    if db is not None and email:
        with sqlite3.connect(db) as c:
            c.execute("UPDATE users SET is_admin=1 WHERE email=?", (email,))


@pytest.fixture(autouse=True)
def _reset_identity_env():
    """Keep identity secrets at test defaults so one test cannot leak admin grants."""
    os.environ["AW_OWNER_EMAIL"] = "owner@example.com"
    os.environ["AW_ADMIN_EMAILS"] = "admin@agendawatch.app"
    os.environ.setdefault("NOTIFY_TO", "inbox@example.invalid")
    yield
    os.environ["AW_OWNER_EMAIL"] = "owner@example.com"
    os.environ["AW_ADMIN_EMAILS"] = "admin@agendawatch.app"
    os.environ.setdefault("NOTIFY_TO", "inbox@example.invalid")



class NetworkBlocked(AssertionError):
    pass


def _blocked(*a, **k):
    raise NetworkBlocked(f"live network/email attempted in a test: {a!r} {k!r}")


def real_config():
    """The project's real config.json (rules, keywords, windows, owner, all fifteen catalog cities) expanded
    to cfg['cities'], with public bodies narrowed to the first one per city so the fake PMN
    doesn't report 'body not found' failures."""
    cfg = aw_cities.load_config(PROJECT / "config.json")
    cfg = copy.deepcopy(cfg)
    for c in cfg["cities"].values():
        c["public_bodies"] = c["public_bodies"][:1]
    assert cfg["cities"][CH]["public_bodies"] == [CH_BODY]
    assert cfg["cities"][SLC]["public_bodies"] == [SLC_BODY]
    return cfg


ALL_CITIES = list(real_config()["cities"])            # fifteen names, display order
CITY_KEY = {n: c["key"] for n, c in real_config()["cities"].items()}
CITY_BODY = {n: c["public_bodies"][0] for n, c in real_config()["cities"].items()}


# ------------------------------------------------------------------ fake PMN
class FakeResponse:
    def __init__(self, status_code, text, content=None, headers=None):
        self.status_code = status_code
        self.text = text
        self.content = text.encode() if content is None else content
        self.headers = headers or {}


class FakePMN:
    """Serves the 4 PMN HTML shapes agenda_watch parses. No real HTTP."""

    BASE = "https://www.utah.gov"

    def __init__(self):
        cfg = real_config()
        self.entities, self.bodies, self.notices, self.body_name = {}, {}, {}, {}
        for i, (name, c) in enumerate(cfg["cities"].items()):
            eid, bid = str(1001 + i), str(2001 + i)
            self.entities[c["pmn_entity_name"]] = eid
            self.bodies[eid] = {c["public_bodies"][0]: bid}
            self.notices[bid] = []
            self.body_name[name] = c["public_bodies"][0]
        self.details = {}
        self.fail = {}  # url fragment -> int status code or Exception instance
        self.requests = []
        self._next = 9000
        self.files = {}   # file id -> {"text", "etag", "lm"}; served as fake "%PDF" bytes

    def set_file(self, fid, text, etag=None):
        self.files[str(fid)] = {"text": text, "etag": etag or f'"{fid}-{abs(hash(text))}"',
                                "lm": "Mon, 12 Oct 2026 12:00:00 GMT"}
        return f"{self.BASE}/pmn/files/{fid}.pdf"

    def add_notice(self, city, when, agenda, title="Regular Meeting", files=(), edited=""):
        """when: aware datetime in America/Denver. agenda: notice text."""
        self._next += 1
        nid = str(self._next)
        eid = self.entities[city]
        body = self.body_name[city]
        bid = self.bodies[eid][body]
        when_local = when.astimezone(TZ)
        self.notices[bid].append((nid, title, when_local.strftime("%Y/%m/%d %I:%M %p")))
        self.details[nid] = dict(
            entity=city, body=body, title=title,
            start=when_local.strftime("%B %d, %Y %I:%M %p"), agenda=agenda,
            files=list(files), edited=edited,
        )
        return f"{self.BASE}/pmn/sitemap/notice/{nid}.html"

    # --- request handler (replaces agenda_watch.S.get)
    def get(self, url, timeout=None, headers=None, **kw):
        self.requests.append(url)
        self.cond = getattr(self, "cond", [])
        if headers:
            self.cond.append((url, dict(headers)))
        assert url.startswith(self.BASE), f"unexpected host: {url}"
        for frag, f in self.fail.items():
            if frag in url:
                if isinstance(f, BaseException):
                    raise f
                return FakeResponse(f, f"<html>HTTP {f}</html>")
        u = urlparse(url)
        q = parse_qs(u.query)
        if u.path == "/pmn/list/entities.html":
            lis = "".join(f'<li><a id="{i}">{n}</a></li>' for n, i in self.entities.items())
            return FakeResponse(200, f'<div id="entities"><ul>{lis}</ul></div>')
        if u.path == "/pmn/list/publicBodies.html":
            b = self.bodies.get(q["id"][0], {})
            lis = "".join(f'<li><a id="{i}">{n}</a></li>' for n, i in b.items())
            return FakeResponse(200, f'<div id="publicBodies"><ul>{lis}</ul></div>')
        if u.path == "/pmn/list/notices.html":
            rows = "".join(
                f'<tr><td><a href="/pmn/sitemap/notice/{nid}.html">{t}</a></td><td>{w}</td></tr>'
                for nid, t, w in self.notices.get(q["id"][0], [])
            )
            if not rows:
                rows = "<tr><td>No notices</td></tr>"
            return FakeResponse(200, f'<div id="browseResults"><table><tbody>{rows}</tbody></table></div>')
        if u.path.startswith("/pmn/files/"):
            fid = u.path.rsplit("/", 1)[-1].split(".")[0]
            f = self.files.get(fid)
            if not f:
                return FakeResponse(404, "not found")
            h = {"ETag": f["etag"], "Last-Modified": f["lm"]}
            if headers and headers.get("If-None-Match") == f["etag"]:
                return FakeResponse(304, "", b"", h)
            body = b"%PDF-FAKE\n" + f["text"].encode()
            return FakeResponse(200, "", body, h)
        if u.path.startswith("/pmn/sitemap/notice/"):
            nid = u.path.rsplit("/", 1)[-1].split(".")[0]
            d = self.details[nid]
            return FakeResponse(200, (
                "<article><dl>"
                f"<dt>Entity</dt><dd>{d['entity']}</dd>"
                f"<dt>Public Body</dt><dd>{d['body']}</dd>"
                f"<dt>Notice Title</dt><dd>{d['title']}</dd>"
                f"<dt>Event Start Date &amp; Time</dt><dd>{d['start']}</dd>"
                f"<dt>Description/Agenda</dt><dd class=\"agenda\">{d['agenda']}</dd>"
                + (f"<dt>Notice Last Edited On</dt><dd>{d['edited']}</dd>" if d.get("edited") else "")
                + "</dl>"
                + "".join(f'<a href="/pmn/files/{fid}.pdf">Attachment {fid}</a>' for fid in d.get("files", []))
                + "</article>"
            ))
        return FakeResponse(404, "not found")


# ------------------------------------------------------------------ fixtures
@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    root = tmp_path / "aw"
    for d in ("state", "outbox", "cache", "data"):
        (root / d).mkdir(parents=True)
    (root / "config.json").write_text(json.dumps(real_config(), indent=2))
    monkeypatch.setattr(aw, "ROOT", root)
    monkeypatch.setattr(aw, "STATE", root / "state")
    monkeypatch.setattr(aw, "OUTBOX", root / "outbox")
    monkeypatch.setattr(aw, "CACHE", root / "cache")
    monkeypatch.setattr(aw, "HISTORY", root / "state" / "sent_history.json")
    monkeypatch.setattr(aw, "DB", root / "data" / "agenda_watch.db")
    monkeypatch.setattr(aw.time, "sleep", lambda *_a, **_k: None)
    # Hard network / email guards.
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(smtplib, "SMTP", _blocked)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _blocked)
    monkeypatch.setattr(aw.S, "get", _blocked)
    monkeypatch.setattr(aw.S, "post", _blocked)
    monkeypatch.setattr(aw.requests, "post", _blocked)
    monkeypatch.setattr(aw.requests, "get", _blocked)
    return types.SimpleNamespace(
        root=root, state=root / "state", outbox=root / "outbox", db=root / "data" / "agenda_watch.db"
    )


@pytest.fixture
def frozen_now(monkeypatch):
    """Freeze agenda_watch's clock at FROZEN_NOW (America/Denver)."""

    class FrozenDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return FROZEN_NOW.astimezone(tz) if tz else FROZEN_NOW.replace(tzinfo=None)

    monkeypatch.setattr(aw, "dt", types.SimpleNamespace(datetime=FrozenDateTime, timedelta=dt.timedelta))
    return FROZEN_NOW


@pytest.fixture
def pmn(monkeypatch, frozen_now):
    fake = FakePMN()
    monkeypatch.setattr(aw.S, "get", fake.get)
    return fake


@pytest.fixture
def cfg():
    return real_config()


@pytest.fixture
def owner_watch(sandbox):
    """Create the owner's watch row in the sandbox SQLite DB. Returns a setter."""

    def make(cities=None, **fields):
        """cities: {city_key: (enabled, street)}. Legacy kwargs city_ch/city_slc/street_ch/
        street_slc are accepted and mapped onto watch_cities rows (ch / slc)."""
        legacy = {k: fields.pop(k) for k in ("city_ch", "city_slc", "street_ch", "street_slc") if k in fields}
        if cities is None:
            cities = {
                "ch": (legacy.get("city_ch", 1), legacy.get("street_ch", "")),
                "slc": (legacy.get("city_slc", 0), legacy.get("street_slc", "")),
            }
        row = dict(
            email=OWNER,
            keywords="short-term rental\nSTR\nrental license\nbusiness license\nzoning\n"
                     "conditional use\ntransient tax\noccupancy",
            alerts_on=1, updated_at="2026-10-07T00:00:00",
        )
        row.update(fields)
        con = sqlite3.connect(sandbox.db)
        con.executescript(
            "CREATE TABLE IF NOT EXISTS watches(email TEXT PRIMARY KEY, keywords TEXT NOT NULL,"
            " alerts_on INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS watch_cities(email TEXT NOT NULL, city_key TEXT NOT NULL,"
            " enabled INTEGER NOT NULL DEFAULT 0, street TEXT NOT NULL DEFAULT '', updated_at TEXT,"
            " PRIMARY KEY(email, city_key));"
            "DELETE FROM watches; DELETE FROM watch_cities;"
        )
        cols = ",".join(row)
        con.execute(f"INSERT INTO watches({cols}) VALUES ({','.join('?' * len(row))})", list(row.values()))
        for key, (on, street) in cities.items():
            con.execute("INSERT INTO watch_cities(email,city_key,enabled,street) VALUES(?,?,?,?)",
                        (OWNER, key, int(bool(on)), street or ""))
        con.commit()
        con.close()
        return dict(row, cities={k: {"enabled": bool(on), "street": st or ""} for k, (on, st) in cities.items()})

    return make


@pytest.fixture
def run_main(monkeypatch, capsys):
    """Invoke agenda_watch.main() like the CLI; returns (exit_code, stdout, stderr)."""

    def _run(*argv):
        monkeypatch.setattr(sys, "argv", ["agenda_watch.py", *argv])
        code = 0
        try:
            aw.main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        out = capsys.readouterr()
        return code, out.out, out.err

    return _run


def outbox_files(sandbox):
    """Every file the run queued for sending (email-*.json, magic links, anything)."""
    return sorted(p for p in sandbox.outbox.rglob("*") if p.is_file())
