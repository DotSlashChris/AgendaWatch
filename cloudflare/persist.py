"""Durability for the ephemeral container disk (stdlib only, no app changes).

Snapshot = tar.gz of data/agenda_watch.db (consistent copy via sqlite backup API), data/secret.key, state/*.json.
Stored in R2 by the Worker (R2 binding) at  PUT/GET $AW_PERSIST_URL/latest  (+ daily copy kept by the Worker).
  restore : on boot, download latest and unpack (404 = first boot -> start empty, any other error -> exit 1)
  loop    : frugal change detection (2026-10-21, replaces the 20 s build+hash loop):
            * every 5 s a cheap stat() of the snapshot files (DB, secret.key, state/*.json): no reads, no hashing;
            * a change -> snapshot ~30 s after the first unsaved write (debounce; settings saves, webhooks);
            * state/.persist-now -> snapshot now (daily run end, alert sends, webhooks);
            * every `interval` s (admin setting guard_snapshot_interval_s, default 600, 60-3600) a backstop
              check, which still only builds when the files changed since the last snapshot;
            * SIGTERM (container sleep/shutdown) -> final snapshot.
            Over the daily upload budget the loop slows to one upload per 30 min (never stops: durability).
"""
import datetime as _dt, hashlib, io, json, os, signal, sqlite3, sys, tarfile, tempfile, time, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URL = os.environ.get("AW_PERSIST_URL", "").rstrip("/")
HDR = {"X-AW-Internal-Secret": os.environ.get("AW_INTERNAL_SECRET", ""), "User-Agent": "AgendaWatch-Container/1.0"}
DB = ROOT / "data" / "agenda_watch.db"
FLAG = ROOT / "state" / ".persist-now"
STATUS = ROOT / "cache" / "persist_status.json"   # not part of the snapshot (cache/ is never uploaded)
TICK_S = 5
DEBOUNCE_S = 30
THROTTLE_S = 30 * 60
DEFAULT_INTERVAL = 600


def build() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        if DB.exists():
            with tempfile.TemporaryDirectory() as td:
                cp = Path(td) / "agenda_watch.db"
                src, dst = sqlite3.connect(DB), sqlite3.connect(cp)
                src.backup(dst); dst.close(); src.close()
                tf.add(cp, "data/agenda_watch.db")
        for p in [ROOT / "data" / "secret.key", *sorted((ROOT / "state").glob("*.json"))]:
            if p.exists():
                tf.add(p, str(p.relative_to(ROOT)))
    return buf.getvalue()


def digest(blob: bytes) -> str:  # hash of member contents (tar headers carry mtimes)
    h = hashlib.sha256()
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        for m in sorted(tf.getmembers(), key=lambda m: m.name):
            h.update(m.name.encode()); h.update(tf.extractfile(m).read())
    return h.hexdigest()


def restore():
    try:
        with urllib.request.urlopen(urllib.request.Request(URL + "/latest", headers=HDR), timeout=60) as r:
            blob = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404 and e.headers.get("x-aw-persist") == "none":
            print("persist: no snapshot yet (first boot) — starting empty", flush=True); return
        raise
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        tf.extractall(ROOT, filter="data")
    print(f"persist: restored {len(blob)} bytes", flush=True)
    owner_check("restore")


def owner_check(tag="restore"):
    """Read-only integrity line for the owner's watch + subscription (no email, ids shortened, no secrets)."""
    try:
        owner = (os.environ.get("AW_OWNER_EMAIL") or "").strip().lower()
    except Exception:
        owner = ""
    if not owner or not DB.exists():
        return
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        u = con.execute("SELECT subscription_status, extra_cities, period_end, cancel_at, stripe_customer_id, "
                        "stripe_subscription_id FROM users WHERE lower(email)=?", (owner,)).fetchone()
        w = con.execute("SELECT alerts_on, keywords FROM watches WHERE lower(email)=?", (owner,)).fetchone()
        cities = [(r["city_key"], bool(r["street"]), bool(r["locked_at"])) for r in con.execute(
            "SELECT city_key, street, locked_at FROM watch_cities WHERE lower(email)=? AND enabled=1 ORDER BY city_key",
            (owner,))]
        con.close()
        tail = lambda v: ("…" + str(v)[-6:]) if v else None
        info = {"user": bool(u), "watch": bool(w),
                "cities": [f"{k}{'+street' if st else ''}{'+locked' if lk else ''}" for k, st, lk in cities],
                "alerts_on": w["alerts_on"] if w else None,
                "keywords": len([x for x in (w["keywords"] or "").splitlines() if x.strip()]) if w else None,
                **({"status": u["subscription_status"], "extra": u["extra_cities"], "period_end": u["period_end"],
                    "cancel_at": u["cancel_at"], "cust": tail(u["stripe_customer_id"]),
                    "sub": tail(u["stripe_subscription_id"])} if u else {})}
        print(f"persist: owner-check[{tag}] " + json.dumps(info, sort_keys=True), flush=True)
    except Exception as e:  # never block startup
        print("persist: owner-check failed", type(e).__name__, flush=True)


def upload(blob: bytes, final=False) -> dict:
    """PUT the snapshot. The Worker answers {ok, uploads_today, cap, throttled}; 429 = throttled."""
    h = {**HDR, "Content-Type": "application/gzip"}
    if final:
        h["X-AW-Final"] = "1"
    req = urllib.request.Request(URL + "/latest", data=blob, method="PUT", headers=h)
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read()
    try:
        return json.loads(body or b"{}")
    except ValueError:
        return {"ok": True}


def signature():
    """Cheap change signal: (name, mtime_ns, size) of every snapshot file. stat() only."""
    out = []
    for p in [DB, DB.with_name(DB.name + "-wal"), ROOT / "data" / "secret.key", *sorted((ROOT / "state").glob("*.json"))]:
        try:
            st = p.stat()
            out.append((p.name, st.st_mtime_ns, st.st_size))
        except FileNotFoundError:
            pass
    return tuple(out)


def read_interval(default=None):
    """Admin setting guard_snapshot_interval_s (read-only DB access), clamped to 60..3600."""
    d = int(default or os.environ.get("AW_PERSIST_INTERVAL") or DEFAULT_INTERVAL)
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
        try:
            r = con.execute("SELECT value FROM settings WHERE key='guard_snapshot_interval_s'").fetchone()
        finally:
            con.close()
        v = int(r[0]) if r else d
    except Exception:
        v = d
    return max(60, min(3600, v))


def write_status(st):
    try:
        STATUS.parent.mkdir(parents=True, exist_ok=True)
        STATUS.write_text(json.dumps(st, indent=1, default=str))
    except Exception:
        pass


def _now_iso():
    return _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec="seconds")


class Snapshotter:
    """Decision logic for the loop (unit-tested with a fake clock)."""

    def __init__(self, interval, clock=time.monotonic):
        self.clock = clock
        self.interval = interval
        self.saved_sig = None        # signature at the last successful snapshot/restore
        self.last_digest = None
        self.dirty_since = None
        self.last_check = clock()
        self.throttled_until = 0.0
        self.st = {"interval_s": interval, "debounce_s": DEBOUNCE_S, "uploads_since_boot": 0,
                   "builds_since_boot": 0, "last_upload_at": None, "last_upload_bytes": None,
                   "last_check_at": None, "last_error": None, "throttled": False}

    def due(self, sig, flag, stopping):
        """-> reason string when a snapshot attempt is due now, else None."""
        now = self.clock()
        if sig != self.saved_sig and self.dirty_since is None:
            self.dirty_since = now
        if sig == self.saved_sig:
            self.dirty_since = None
        if stopping:
            return "final"
        if now < self.throttled_until and not flag:
            return None
        if flag:
            return "flag"
        if self.dirty_since is not None and now - self.dirty_since >= DEBOUNCE_S:
            return "write"
        if now - self.last_check >= self.interval:
            self.last_check = now
            return "interval" if sig != self.saved_sig else None
        return None

    def attempt(self, reason, sig, build_fn, digest_fn, upload_fn):
        now = self.clock()
        self.last_check = now
        self.st["last_check_at"] = _now_iso()
        blob = build_fn(); self.st["builds_since_boot"] += 1
        d = digest_fn(blob)
        if d == self.last_digest:
            self.saved_sig, self.dirty_since = sig, None
            return "unchanged"
        try:
            r = upload_fn(blob, final=(reason == "final")) or {}
        except urllib.error.HTTPError as e:
            if e.code == 429:
                self.throttled_until = now + THROTTLE_S
                self.st.update(throttled=True, last_error="upload budget reached: next upload in 30 min")
                return "throttled"
            raise
        self.last_digest, self.saved_sig, self.dirty_since = d, sig, None
        self.st.update(uploads_since_boot=self.st["uploads_since_boot"] + 1, last_upload_at=_now_iso(),
                       last_upload_bytes=len(blob), last_error=None,
                       uploads_today=r.get("uploads_today"), upload_cap=r.get("cap"))
        if r.get("cap") and r.get("uploads_today", 0) >= r["cap"]:
            self.throttled_until = now + THROTTLE_S   # over budget: at most one upload per 30 min
            self.st["throttled"] = True
        else:
            self.st["throttled"] = False
        return "uploaded"


def loop():
    sn = Snapshotter(read_interval())
    try:
        sn.last_digest = digest(build())  # what we just restored
    except Exception as e:
        print("persist: initial digest failed", e, flush=True)
    sn.saved_sig = signature()
    stop = {"v": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(v=True))
    checked = False
    print(f"persist: loop started (stat every {TICK_S}s, debounce {DEBOUNCE_S}s, interval {sn.interval}s)", flush=True)
    write_status(sn.st)
    while True:
        for _ in range(TICK_S):
            if stop["v"] or FLAG.exists():
                break
            time.sleep(1)
        if not checked:  # once, after the app has started and run its migrations
            owner_check("running"); checked = True
        flag = FLAG.exists()
        sig = signature()
        reason = sn.due(sig, flag, stop["v"])
        if reason:
            FLAG.unlink(missing_ok=True)
            try:
                out = sn.attempt(reason, sig, build, digest, upload)
                if out == "uploaded":
                    print(f"persist: snapshot uploaded ({sn.st['last_upload_bytes']} bytes, {reason})", flush=True)
                elif out == "throttled":
                    print("persist: upload budget reached; throttled to one upload per 30 min", flush=True)
            except Exception as e:
                sn.st["last_error"] = repr(e)[:300]
                print("persist: snapshot FAILED", repr(e), flush=True)
            sn.interval = read_interval(sn.interval)
            sn.st["interval_s"] = sn.interval
            write_status(sn.st)
        if stop["v"]:
            return


if __name__ == "__main__":
    {"restore": restore, "loop": loop}[sys.argv[1]]()
