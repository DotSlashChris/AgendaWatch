"""Offline re-evaluation of the stored run (read-only DB + last_run.json). Not a pytest module.
usage: python3 tests/offline_rescan.py <snapshot_dir> [--history pre-run|post-run] [--run-id N]

Merged counts are computed the way the run did: merge_hits() drops every hit whose key is already in the sent
history.  The snapshot's state/sent_history.json is written AFTER the run (the run itself appends what it emailed),
so by default (--history pre-run) the history is rewound to what it was when the run started; --history post-run
keeps the old behaviour (history as found in the snapshot)."""
import bisect, json, sys, sqlite3, datetime as dt, collections
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
TS_FIELDS = ("created_at", "ts", "sent_at", "first_at", "time")
import agenda_watch as aw


def evaluate(snap, noise=True, only=None):
    cfg = json.load(open(Path(__file__).resolve().parents[1] / "config.json"))
    lr = json.load(open(f"{snap}/state/last_run.json"))
    hist = json.load(open(f"{snap}/state/sent_history.json"))
    now = dt.datetime.fromisoformat(lr["now_denver"])
    names = {h["link"]: h["document"] for h in lr["hits"]}
    c = sqlite3.connect(f"file:{snap}/data/agenda_watch.db?mode=ro", uri=True)
    m = aw.build_keyword_matchers(cfg)
    orig = aw.noise_reason
    if not noise:
        aw.noise_reason = lambda *a, **k: None
    drops, out = [], []
    try:
        cities = {k: v for k, v in cfg["cities"].items()} if "cities" in cfg else {}
        for nu, ck, md, body, title in c.execute("select notice_url,city_key,meeting_date,body,title from notices order by id"):
            if only and ck not in only:
                continue
            ev = dt.datetime.fromisoformat(md)
            if not (dt.datetime.fromisoformat(lr["window"][0]) <= ev <= dt.datetime.fromisoformat(lr["window"][1])):
                continue
            seen = set()
            for url, txt in c.execute("select url,text from documents where notice_url=?", (nu,)):
                if not txt:
                    continue
                dn = names.get(url) or url.rsplit("/", 1)[-1]
                dr = []
                for h in aw.find_hits(txt, m, dn, drops=dr):
                    k = (h["item_text"].lower(), h["matched"])
                    if k in seen:
                        continue
                    seen.add(k)
                    city = next((x["city"] for x in lr["hits"] if x["city_key"] == ck), None) or ck
                    h.update({"city": city, "city_key": ck, "meeting_body": body, "notice_title": title,
                              "meeting_datetime": md, "event_iso": md, "upcoming": ev >= now, "document": dn,
                              "link": url, "notice_url": nu})
                    h["key"] = aw.hit_key(city, url, h["item_text"])
                    out.append(h)
                for d in dr:
                    d.update({"city_key": ck, "meeting": md[:10], "url": url})
                    drops.append(d)
    finally:
        aw.noise_reason = orig
    return out, drops, hist


def run_started_at(snap, now_denver, run_id=None):
    """started_at (ISO, with offset) of the stored run from fetch_runs: --run-id N, else the run that started at
    or just before last_run.json's now_denver (the run stamps now_denver at start, so they agree to the second)."""
    c = sqlite3.connect(f"file:{snap}/data/agenda_watch.db?mode=ro", uri=True)
    if run_id is not None:
        r = c.execute("select id, started_at from fetch_runs where id=?", (run_id,)).fetchone()
    else:
        now = dt.datetime.fromisoformat(now_denver)
        rows = c.execute("select id, started_at from fetch_runs order by id desc").fetchall()
        r = next((x for x in rows if dt.datetime.fromisoformat(x[1]) <= now + dt.timedelta(seconds=1)), None)
    if not r:
        raise SystemExit("no matching fetch_runs row for the stored run")
    return r


def pre_run_history(hist, run_hit_keys, started_at):
    """Sent history as it stood before the run that started at `started_at` (ISO).

    sent_history.json entries only carry a DATE (plus key / duplicate_of), no time, so the rule is:
      1. an entry with a real timestamp field (created_at/ts/sent_at/first_at/time) is excluded iff ts >= started_at;
      2. otherwise an entry is excluded iff its date is on/after the run's local date AND its key (or the key it
         is duplicate_of) is one of THIS run's own recorded hits - i.e. it was appended by this run's e-mail step.
    Everything else is kept - in particular every entry dated before the run's day, and same-day entries for items
    the run did not produce.  Safe because the run only ever appends entries for hits it produced, and merge_hits
    only consults entries by key; a hit key is never in history before the run first reports it (otherwise merge
    would have filtered it and it could not have been a counted/emailed hit).  Known limit: a hit that an EARLIER
    run on the same calendar day had already sent would also be excluded; the daily routine runs once a day.
    Returns (kept_history, excluded_entries)."""
    start = dt.datetime.fromisoformat(started_at)
    keep, out = [], []
    for e in hist:
        ts = next((e[f] for f in TS_FIELDS if e.get(f)), None)
        if ts:
            try:
                ex = dt.datetime.fromisoformat(str(ts)) >= start
            except ValueError:
                ex = False
        else:
            ex = (str(e.get("date", "")) >= start.date().isoformat()
                  and (e.get("key") in run_hit_keys or e.get("duplicate_of") in run_hit_keys))
        (out if ex else keep).append(e)
    return keep, out


def merged_counts(hits, hist):
    by = collections.defaultdict(list)
    for h in hits:
        by[h["city_key"]].append(h)
    return {ck: aw.merge_hits(v, hist) for ck, v in by.items()}


def recorded_check(snap):
    """Apply the noise rules to every hit recorded in the stored run, at its stored offsets."""
    lr = json.load(open(f"{snap}/state/last_run.json"))
    c = sqlite3.connect(f"file:{snap}/data/agenda_watch.db?mode=ro", uri=True)
    dropped = []
    for h in lr["hits"]:
        if h["document"] == "PMN notice text":
            r = c.execute("select text from notices where notice_url=?", (h["notice_url"],)).fetchone()
        else:
            r = c.execute("select text from documents where url=?", (h["link"],)).fetchone()
        txt = r[0] if r else None
        if not txt or h["match_type"] != "keyword":
            continue
        lines = txt.splitlines(); offs = []; p = 0
        for l in lines:
            offs.append(p); p += len(l) + 1
        li = bisect.bisect_right(offs, h["start"]) - 1
        why = aw.noise_reason(txt, lines, li, h["start"], h["end"], h["matched"])
        if why:
            dropped.append((h["city_key"], h["matched"], why, h["key"]))
    return len(lr["hits"]), dropped


def main(argv):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("snap")
    ap.add_argument("--history", choices=("pre-run", "post-run"), default="pre-run")
    ap.add_argument("--run-id", type=int)
    ap.add_argument("--city", action="append")
    a = ap.parse_args(argv)
    snap = a.snap
    lr = json.load(open(f"{snap}/state/last_run.json"))
    summ = json.load(open(f"{snap}/state/last_run_summary.json"))
    post = json.load(open(f"{snap}/state/sent_history.json"))
    rid, started = run_started_at(snap, lr["now_denver"], a.run_id)
    run_keys = {h["key"] for h in lr["hits"]}
    pre, excl = pre_run_history(post, run_keys, started)
    hist = pre if a.history == "pre-run" else post
    print(f"run id {rid} started {started}; history basis: {a.history} "
          f"({len(post)} entries in snapshot, {len(excl) if a.history == 'pre-run' else 0} excluded, {len(hist)} used)")
    if a.history == "pre-run":
        for e in excl:
            print(f"  excluded (added by this run): {e['date']} {e['city']} key {e['key'][:8]} "
                  f"{'dup of ' + e['duplicate_of'][:8] if e.get('duplicate_of') else ''}")
    # (1) the run's own recorded hits: old rules (as recorded) and new rules
    n, dropped = recorded_check(snap)
    dkeys = {k for *_, k in dropped}
    rec_by = collections.defaultdict(list); new_by = collections.defaultdict(list)
    for h in lr["hits"]:
        rec_by[h["city_key"]].append(h)
        if h["key"] not in dkeys:
            new_by[h["city_key"]].append(h)
    # (2) full offline re-scan of stored documents under the current rules
    scan, drops, _ = evaluate(snap, noise=True)
    scan_m = merged_counts(scan, hist)
    sm = {p["key"]: p for p in summ["per_city"]}
    print(f"\n{'city':11s} {'run summary':>11s} {'recorded,old':>12s} {'recorded,new':>12s} {'rescan,new':>10s}   post-run history (recorded,old/new)")
    bad = []
    for ck in sm:
        if a.city and ck not in a.city:
            continue
        ro = len(aw.merge_hits(rec_by.get(ck, []), hist)); rn = len(aw.merge_hits(new_by.get(ck, []), hist))
        po = len(aw.merge_hits(rec_by.get(ck, []), post)); pn = len(aw.merge_hits(new_by.get(ck, []), post))
        flag = "" if ro == sm[ck]["hits"] else "  <-- recorded,old != run summary"
        if flag: bad.append(ck)
        print(f"{ck:11s} {sm[ck]['hits']:11d} {ro:12d} {rn:12d} {len(scan_m.get(ck, [])):10d}   {po}/{pn}{flag}")
    print("\nmerged items (recorded hits, new rules, %s history):" % a.history)
    for ck in sm:
        if a.city and ck not in a.city:
            continue
        for h in aw.merge_hits(new_by.get(ck, []), hist):
            print(f"  {ck}: {h['notice_title']} | {h['meeting_body']} | {h['meeting_datetime']} | "
                  f"{'upcoming' if h['upcoming'] else 'past'} | terms {h['matched_terms']} | {h['link']}")
    print("\ndropped by the new rules (recorded hits):", collections.Counter((c, r) for c, _, r, _ in dropped))
    if bad:
        print("cities whose old-rules recount differs from the run summary:", bad)


if __name__ == "__main__":
    main(sys.argv[1:])
