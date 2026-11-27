// Agenda Watch on Cloudflare Containers: Worker + 1 Durable Object-backed container.
import { liveStripeEnv, stripeKeyKinds } from "./stripe_env";
import { Container, getContainer } from "@cloudflare/containers";
import { cronDecision, denverParts } from "./schedule";
import { handlePublic } from "./edge";
import { CITY_SLUGS } from "./catalog.gen";

interface Env {
  AW_CONTAINER: DurableObjectNamespace<AgendaWatchContainer>;
  EMAIL: SendEmail;
  AW_RL?: RateLimit;                         // per-IP limit for requests that may reach the container
  CF_VERSION_METADATA?: WorkerVersionMetadata; // stored home page is keyed by Worker version
  // secrets (wrangler secret put)
  AW_INTERNAL_SECRET: string;
  STRIPE_SECRET_KEY: string;
  STRIPE_PUBLISHABLE_KEY: string;
  STRIPE_WEBHOOK_SECRET: string;
  STRIPE_PORTAL_CONFIG_ID?: string; // legacy test id, ignored
  // vars
  AW_BASE_URL: string;
  MAIL_FROM: string;
  // secrets (identity + project inbox; wrangler secret put — not public vars)
  AW_OWNER_EMAIL: string;
  AW_ADMIN_EMAILS: string; // comma-separated
  NOTIFY_TO: string;
  RESEND_API_KEY?: string;
  // optional Cloudflare Turnstile for the feedback form (both set -> widget + server check; else off)
  TURNSTILE_SITE_KEY?: string;   // var (public)
  TURNSTILE_SECRET?: string;     // secret (wrangler secret put TURNSTILE_SECRET)
}

const INSTANCE = "main";
const STALE_MS = 90 * 60 * 1000; // a "running" claim older than this is considered dead
const CHUNK = 1_000_000; // bytes per SQLite row (limit 2 MB)
const RPC_PART = 8; // chunks per RPC read (8 MB, well under the 32 MiB RPC limit)
const MAX_SNAPSHOT = 90_000_000;
const UPLOAD_THROTTLE_MS = 30 * 60 * 1000; // over the daily upload budget: at most one upload per 30 min
// Guard defaults; the container pushes the admin's values to /__aw/guard at boot and on every admin save.
const GUARD_DEFAULTS: Record<string, number> = { fetch_enabled: 1, cap_snapshot_uploads: 60, cap_container_wakes: 60 };
const denverDay = () => denverParts(Date.now()).date; // Worker request body limit is 100 MB // exactly one instance: SQLite must have a single writer

export class AgendaWatchContainer extends Container<Env> {
  defaultPort = 8080;
  sleepAfter = "15m"; // idle -> sleep (billing stops); next request cold-starts (~seconds) + restore from R2
  enableInternet = true; // PMN fetches, Stripe API, calls back to the Worker for persistence/email

  constructor(ctx: DurableObjectState<{}>, env: Env) {
    super(ctx, env);
    const stripe = liveStripeEnv(env);
    if (!stripe.ok) console.warn(`Stripe disabled in container: ${stripe.reason}`);
    this.envVars = {
      AW_CLOUD: "1",
      AW_RUNTIME: "cloudflare-containers",
      AW_BASE_URL: env.AW_BASE_URL,
      AW_INTERNAL_SECRET: env.AW_INTERNAL_SECRET,
      AW_PERSIST_URL: `${env.AW_BASE_URL}/__aw/persist`,
      AW_EMAIL_RELAY_URL: `${env.AW_BASE_URL}/__aw/email`,
      RESEND_API_KEY: env.RESEND_API_KEY ?? "", // optional: when set, the container sends via Resend
      NOTIFY_TO: env.NOTIFY_TO ?? "", // owner-bound mail goes to the project inbox (secret)
      AW_OWNER_EMAIL: (env.AW_OWNER_EMAIL ?? "").trim(),
      AW_ADMIN_EMAILS: (env.AW_ADMIN_EMAILS ?? "").trim(), // comma-separated; empty = no admins
      TURNSTILE_SITE_KEY: (env.TURNSTILE_SITE_KEY ?? "").trim(), // empty = Turnstile off
      TURNSTILE_SECRET: (env.TURNSTILE_SECRET ?? "").trim(),
      ...stripe.vars, // live keys only; STRIPE_PORTAL_CONFIG_ID (old test id) is intentionally not passed
    };
    ctx.blockConcurrencyWhile(async () => {
      ctx.storage.sql.exec("CREATE TABLE IF NOT EXISTS snap (name TEXT, idx INTEGER, data BLOB, PRIMARY KEY(name, idx))");
      // once-per-day guard, keyed by Denver date
      ctx.storage.sql.exec("CREATE TABLE IF NOT EXISTS daily_run (date TEXT PRIMARY KEY, source TEXT, started_at TEXT, finished_at TEXT, status TEXT, http INTEGER, result TEXT)");
      // cost guardrails (2026-10-21): small key/value store + per-Denver-day counters
      ctx.storage.sql.exec("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)");
      ctx.storage.sql.exec("CREATE TABLE IF NOT EXISTS usage (day TEXT, k TEXT, n INTEGER, PRIMARY KEY(day, k))");
    });
  }

  // ---- guardrail state ----
  kvGet(k: string): string | null {
    return this.ctx.storage.sql.exec<{ v: string }>("SELECT v FROM kv WHERE k = ?", k).toArray()[0]?.v ?? null;
  }
  kvSet(k: string, v: string): void {
    this.ctx.storage.sql.exec("INSERT OR REPLACE INTO kv VALUES (?, ?)", k, v);
  }
  bump(k: string, n = 1): number {
    const day = denverDay();
    this.ctx.storage.sql.exec("INSERT INTO usage VALUES (?, ?, ?) ON CONFLICT(day, k) DO UPDATE SET n = n + excluded.n", day, k, n);
    this.ctx.storage.sql.exec("DELETE FROM usage WHERE day < ?", new Date(Date.now() - 40 * 864e5).toISOString().slice(0, 10));
    return this.used(k, day);
  }
  used(k: string, day = denverDay()): number {
    return this.ctx.storage.sql.exec<{ n: number }>("SELECT n FROM usage WHERE day = ? AND k = ?", day, k).toArray()[0]?.n ?? 0;
  }
  guardConfig(): Record<string, number> {
    const raw = this.kvGet("guard_config");
    const cfg = { ...GUARD_DEFAULTS };
    try { Object.assign(cfg, raw ? JSON.parse(raw) : {}); } catch { /* defaults */ }
    return cfg;
  }
  async setGuardConfig(c: Record<string, unknown>): Promise<Record<string, number>> {
    const cur = this.guardConfig();
    const lim: Record<string, [number, number]> = { fetch_enabled: [0, 1], cap_snapshot_uploads: [24, 2000], cap_container_wakes: [4, 500] };
    for (const [k, [lo, hi]] of Object.entries(lim)) {
      const v = Number(c[k]);
      if (c[k] !== undefined && Number.isInteger(v) && v >= lo && v <= hi) cur[k] = v;
    }
    this.kvSet("guard_config", JSON.stringify(cur));
    return cur;
  }
  /** Signup attribution: +1 first-touch landing for a ?ref= code (per-day counter in `usage`, kept 40 days).
   *  Stores only the code and a count. At most 100 distinct codes, so junk codes cannot grow the table. */
  async refLanding(code: string): Promise<boolean> {
    if (!/^[a-z0-9-]{1,40}$/.test(code)) return false;
    const k = `ref:${code}`;
    const sql = this.ctx.storage.sql;
    const known = sql.exec<{ n: number }>("SELECT COUNT(*) AS n FROM usage WHERE k = ?", k).one().n > 0;
    if (!known && sql.exec<{ n: number }>("SELECT COUNT(DISTINCT k) AS n FROM usage WHERE k LIKE 'ref:%'").one().n >= 100) return false;
    this.bump(k);
    return true;
  }
  /** {code: landings} over the retained window (about 40 days). */
  async refLandings(): Promise<Record<string, number>> {
    const out: Record<string, number> = {};
    for (const r of this.ctx.storage.sql.exec<{ k: string; n: number }>(
      "SELECT k, SUM(n) AS n FROM usage WHERE k LIKE 'ref:%' GROUP BY k ORDER BY n DESC, k").toArray()) out[r.k.slice(4)] = r.n;
    return out;
  }
  /** First time today for `key` -> true (caller sends the one owner notice). */
  async tripOnce(key: string): Promise<boolean> {
    const k = `trip:${denverDay()}:${key}`;
    if (this.kvGet(k)) return false;
    this.kvSet(k, new Date().toISOString());
    return true;
  }
  async guardView(): Promise<Record<string, unknown>> {
    const day = denverDay();
    const trips = this.ctx.storage.sql.exec<{ k: string }>("SELECT k FROM kv WHERE k LIKE ?", `trip:${day}:%`).toArray()
      .map((r) => r.k.split(":").slice(2).join(":"));
    return { day, config: this.guardConfig(), usage: { container_wakes: this.used("container_wakes"), snapshot_uploads: this.used("snapshot_uploads"),
      snapshot_throttled: this.used("snapshot_throttled") }, trips, last_snapshot_at: this.kvGet("last_snapshot_at"),
      last_snapshot_bytes: Number(this.kvGet("last_snapshot_bytes") ?? 0) || null, running: this.ctx.container?.running ?? null,
      disabled_run: this.kvGet(`disabled_run:${day}`) };
  }
  /** One DO round trip for the edge: seeded? running? wakes vs budget. Never starts the container. */
  async gate(): Promise<{ seeded: boolean; running: boolean; wakes: number; wakeCap: number }> {
    return { seeded: await this.hasSnapshot(), running: this.ctx.container?.running ?? false,
      wakes: this.used("container_wakes"), wakeCap: this.guardConfig().cap_container_wakes };
  }
  async getHome(version: string): Promise<{ html: string; at: number } | null> {
    const raw = this.kvGet(`home:${version}`);
    return raw ? JSON.parse(raw) : null;
  }
  async putHome(version: string, html: string): Promise<void> {
    this.ctx.storage.sql.exec("DELETE FROM kv WHERE k LIKE 'home:%'");
    this.kvSet(`home:${version}`, JSON.stringify({ html, at: Date.now() }));
  }
  /** Public city pages: one stored anonymous copy per path for the current Worker version. */
  async getPage(version: string, path: string): Promise<{ html: string; at: number } | null> {
    const raw = this.kvGet(`page:${version}:${path}`);
    return raw ? JSON.parse(raw) : null;
  }
  async putPage(version: string, path: string, html: string): Promise<void> {
    this.ctx.storage.sql.exec("DELETE FROM kv WHERE k LIKE 'page:%' AND k NOT LIKE ?", `page:${version}:%`);
    this.kvSet(`page:${version}:${path}`, JSON.stringify({ html, at: Date.now() }));
  }
  /** Snapshot upload budget: under cap -> ok; over cap -> ok at most once per 30 min; final (SIGTERM) always ok. */
  async uploadAllowed(final: boolean): Promise<{ ok: boolean; uploads: number; cap: number; firstTrip: boolean }> {
    const cap = this.guardConfig().cap_snapshot_uploads;
    const uploads = this.used("snapshot_uploads");
    if (final || uploads < cap) return { ok: true, uploads, cap, firstTrip: false };
    const firstTrip = await this.tripOnce("cap_snapshot_uploads");
    const last = Date.parse(this.kvGet("last_snapshot_at") ?? "") || 0;
    if (Date.now() - last >= UPLOAD_THROTTLE_MS) return { ok: true, uploads, cap, firstTrip };
    this.bump("snapshot_throttled");
    return { ok: false, uploads, cap, firstTrip };
  }

  override onStart(): void {
    const n = this.bump("container_wakes");
    console.log(`container started (wake ${n} today)`);
  }

  // Snapshot storage in this DO's SQLite (limits: 2 MB per row/BLOB, 10 GB per object; RPC args 32 MiB).
  // Rows are CHUNK bytes (< 2 MB). Uploads are staged part-by-part (each RPC <= RPC_PART bytes), then
  // commit() atomically swaps the staged upload in as "latest" (+ a daily copy) in one transaction.
  async putPart(upload: string, idx: number, buf: ArrayBuffer): Promise<void> {
    if (buf.byteLength > CHUNK) throw new Error("part too large");
    this.ctx.storage.sql.exec("INSERT OR REPLACE INTO snap VALUES (?, ?, ?)", `staging/${upload}`, idx, buf);
  }

  async commit(upload: string, parts: number, bytes: number): Promise<void> {
    const sql = this.ctx.storage.sql;
    const day = new Date().toISOString().slice(0, 10);
    const stage = `staging/${upload}`;
    this.ctx.storage.transactionSync(() => {
      const got = sql.exec<{ n: number; b: number }>(
        "SELECT COUNT(*) AS n, COALESCE(SUM(length(data)),0) AS b FROM snap WHERE name = ?", stage).one();
      if (got.n !== parts || got.b !== bytes) throw new Error(`incomplete upload ${got.n}/${parts} ${got.b}/${bytes}`);
      for (const name of ["latest", `daily/${day}`]) {
        sql.exec("DELETE FROM snap WHERE name = ?", name);
        sql.exec("INSERT INTO snap SELECT ?, idx, data FROM snap WHERE name = ?", name, stage);
      }
      sql.exec("DELETE FROM snap WHERE name = ?", stage);
      sql.exec("DELETE FROM snap WHERE name LIKE 'daily/%' AND name NOT IN (SELECT DISTINCT name FROM snap WHERE name LIKE 'daily/%' ORDER BY name DESC LIMIT 14)");
    });
    // abandoned staging uploads (crashed mid-upload) are cleared on the next commit
    sql.exec("DELETE FROM snap WHERE name LIKE 'staging/%'");
    this.kvSet("last_snapshot_at", new Date().toISOString());
    this.kvSet("last_snapshot_bytes", String(bytes));
    this.bump("snapshot_uploads");
  }

  async snapshotParts(name = "latest"): Promise<number> {
    return this.ctx.storage.sql.exec<{ n: number }>("SELECT COUNT(*) AS n FROM snap WHERE name = ?", name).one().n;
  }

  async getParts(name: string, from: number, to: number): Promise<ArrayBuffer[]> {
    return this.ctx.storage.sql
      .exec<{ data: ArrayBuffer }>("SELECT data FROM snap WHERE name = ? AND idx >= ? AND idx < ? ORDER BY idx", name, from, to)
      .toArray().map((r) => r.data);
  }

  /** Atomically claim today's run. false = already claimed (running or done). */
  async claimRun(date: string, source: string): Promise<boolean> {
    const sql = this.ctx.storage.sql;
    const row = sql.exec<{ status: string }>("SELECT status FROM daily_run WHERE date = ?", date).toArray()[0];
    const stale = row?.status === "running" && Date.now() - Date.parse(String((row as any).started_at)) > STALE_MS;
    if (row && row.status !== "failed" && !stale) return false;
    sql.exec("INSERT OR REPLACE INTO daily_run(date, source, started_at, status) VALUES (?, ?, ?, 'running')",
      date, source, new Date().toISOString());
    return true;
  }

  async finishRun(date: string, ok: boolean, http: number, result: string): Promise<void> {
    this.ctx.storage.sql.exec("INSERT OR IGNORE INTO daily_run(date, source, started_at, status) VALUES (?, 'reconcile', ?, 'running')",
      date, new Date().toISOString());
    this.ctx.storage.sql.exec("UPDATE daily_run SET finished_at = ?, status = ?, http = ?, result = ? WHERE date = ?",
      new Date().toISOString(), ok ? "done" : "failed", http, result.slice(0, 20000), date);
  }

  async dailyRun(date: string): Promise<Record<string, unknown> | null> {
    return (this.ctx.storage.sql.exec("SELECT * FROM daily_run WHERE date = ?", date).toArray()[0] as Record<string, unknown>) ?? null;
  }

  /** A run is in flight (claimed, not finished, not stale). */
  runInFlight(): boolean {
    const r = this.ctx.storage.sql.exec<{ started_at: string }>(
      "SELECT started_at FROM daily_run WHERE status = 'running' ORDER BY started_at DESC LIMIT 1").toArray()[0];
    return !!r && Date.now() - Date.parse(r.started_at) < STALE_MS;
  }

  // Keep the container awake for the whole background run (sleepAfter would otherwise stop it).
  override async onActivityExpired(): Promise<void> {
    if (this.runInFlight()) {
      console.log("activity expired during daily run; keeping container awake");
      this.renewActivityTimeout();
      return;
    }
    await super.onActivityExpired();
  }

  async hasSnapshot(): Promise<boolean> {
    return this.ctx.storage.sql.exec("SELECT 1 FROM snap WHERE name = 'latest' LIMIT 1").toArray().length > 0;
  }


}

function authed(req: Request, env: Env): boolean {
  const got = req.headers.get("x-aw-internal-secret") ?? "";
  if (!env.AW_INTERNAL_SECRET || got.length !== env.AW_INTERNAL_SECRET.length) return false;
  let diff = 0;
  for (let i = 0; i < got.length; i++) diff |= got.charCodeAt(i) ^ env.AW_INTERNAL_SECRET.charCodeAt(i);
  return diff === 0;
}

/** One owner notice per cap per day (dedupe by the DO's tripOnce) via the send_email binding (project inbox). */
async function ownerNotice(env: Env, label: string, detail: string): Promise<void> {
  try {
    await env.EMAIL.send({ to: env.NOTIFY_TO, from: env.MAIL_FROM, subject: `Agenda Watch: daily ${label} limit reached`,
      text: `Agenda Watch reached its daily safety limit for ${label}.\n\n${detail}\n\nIt resets at midnight Denver time. Change the limits on the admin page: ${env.AW_BASE_URL}/admin#cost-safety\n` } as any);
    console.warn(`guard notice sent: ${label}`);
  } catch (e) {
    console.error(`guard notice failed: ${label}: ${String(e)}`);
  }
}

async function internal(req: Request, env: Env, url: URL, ctx: ExecutionContext): Promise<Response> {
  if (!authed(req, env)) return new Response("forbidden", { status: 403 });
  // Cost and safety: container pushes admin settings (POST); admin page / reports read the Worker's view (GET).
  if (url.pathname === "/__aw/guard") {
    const stub = getContainer(env.AW_CONTAINER, INSTANCE);
    if (req.method === "POST") return Response.json({ ok: true, config: await stub.setGuardConfig((await req.json()) as Record<string, unknown>) });
    if (req.method === "GET") return Response.json(await stub.guardView());
  }
  if (url.pathname === "/__aw/persist/latest") {
    if (req.method === "GET") {
      const stub = getContainer(env.AW_CONTAINER, INSTANCE);
      const n = await stub.snapshotParts();
      if (!n) return new Response("none", { status: 404, headers: { "x-aw-persist": "none" } });
      const { readable, writable } = new TransformStream();
      (async () => {
        const w = writable.getWriter();
        for (let i = 0; i < n; i += RPC_PART) for (const part of await stub.getParts("latest", i, i + RPC_PART)) await w.write(new Uint8Array(part));
        await w.close();
      })();
      return new Response(readable);
    }
    if (req.method === "PUT") {
      const body = await req.arrayBuffer();
      if (body.byteLength < 100) return new Response("too small", { status: 400 });
      if (body.byteLength > MAX_SNAPSHOT) return new Response("too large", { status: 413 });
      const stub = getContainer(env.AW_CONTAINER, INSTANCE);
      const final = req.headers.get("x-aw-final") === "1";
      const gate = await stub.uploadAllowed(final);
      if (gate.firstTrip) ctx.waitUntil(ownerNotice(env, "snapshot uploads", `${gate.uploads} of ${gate.cap} snapshot uploads used today; uploads are now limited to one per 30 minutes (the final save before sleep always goes through).`));
      if (!gate.ok) return Response.json({ ok: false, throttled: true, uploads_today: gate.uploads, cap: gate.cap }, { status: 429 });
      const upload = crypto.randomUUID();
      let parts = 0;
      for (let i = 0; i < body.byteLength; i += CHUNK) await stub.putPart(upload, parts++, body.slice(i, i + CHUNK));
      await stub.commit(upload, parts, body.byteLength);
      return Response.json({ ok: true, uploads_today: gate.uploads + 1, cap: gate.cap, final });
    }
  }
  // Private status for the morning report: GET https://agendawatch.app/__aw/status (secret header)
  if (url.pathname === "/__aw/status" && req.method === "GET") {
    const stub = getContainer(env.AW_CONTAINER, INSTANCE);
    if (!(await stub.hasSnapshot())) return new Response("not seeded", { status: 503 });
    const today = denverParts(Date.now()).date;
    const { guard, status } = await reconcile(env, today);
    const st = liveStripeEnv(env);
    const worker_guard: any = await stub.guardView();
    const ref_landings = await stub.refLandings(); // first-touch landings per ?ref= code, last ~40 days (counts only)
    return Response.json({ ...status, ref_landings, ref_landings_window_days: 40, today, guard, worker_guard, fetch_enabled: !!(worker_guard.config as any).fetch_enabled, stripe_keys: { ok: st.ok, reason: st.reason, kinds: stripeKeyKinds(env) }, email_provider: (env.RESEND_API_KEY ?? "").trim() ? "resend" : "cloudflare" });
  }
  if (url.pathname === "/__aw/email-check" && req.method === "GET") {
    return getContainer(env.AW_CONTAINER, INSTANCE).fetch(new Request("http://container/internal/email-check", {
      headers: { "x-aw-internal-secret": env.AW_INTERNAL_SECRET },
    }));
  }
  // City agendas sources: read-only reachability + alert dry run from the container (writes and sends nothing)
  if (url.pathname === "/__aw/city-sources-check" && req.method === "GET") {
    return getContainer(env.AW_CONTAINER, INSTANCE).fetch(new Request("http://container/internal/city-sources-check" + url.search, {
      headers: { "x-aw-internal-secret": env.AW_INTERNAL_SECRET },
    }));
  }
  if (url.pathname === "/__aw/billing-check" && req.method === "GET") {
    return getContainer(env.AW_CONTAINER, INSTANCE).fetch(new Request("http://container/internal/billing-check", {
      headers: { "x-aw-internal-secret": env.AW_INTERNAL_SECRET },
    }));
  }
  if (url.pathname === "/__aw/stripe-descriptor" && req.method === "POST") {
    return getContainer(env.AW_CONTAINER, INSTANCE).fetch(new Request("http://container/internal/stripe-descriptor", {
      method: "POST", headers: { "x-aw-internal-secret": env.AW_INTERNAL_SECRET },
    }));
  }
  if ((url.pathname === "/__aw/seed-alerts" || url.pathname === "/__aw/test-alert") && req.method === "POST") {
    const path = url.pathname === "/__aw/seed-alerts" ? "seed-alerts" : "test-alert";
    return getContainer(env.AW_CONTAINER, INSTANCE).fetch(new Request(`http://container/internal/${path}`, {
      method: "POST", headers: { "x-aw-internal-secret": env.AW_INTERNAL_SECRET, "content-type": "application/json" },
      body: await req.text(),
    }));
  }
  if (url.pathname === "/__aw/resend-lookup" && req.method === "GET") {
    return getContainer(env.AW_CONTAINER, INSTANCE).fetch(new Request(`http://container/internal/resend-lookup?id=${encodeURIComponent(url.searchParams.get("id") ?? "")}`, {
      headers: { "x-aw-internal-secret": env.AW_INTERNAL_SECRET },
    }));
  }
  if (url.pathname === "/__aw/user-check" && req.method === "GET") {
    return getContainer(env.AW_CONTAINER, INSTANCE).fetch(new Request(`http://container/internal/user-check?email=${encodeURIComponent(url.searchParams.get("email") ?? "")}`, {
      headers: { "x-aw-internal-secret": env.AW_INTERNAL_SECRET },
    }));
  }
  if (url.pathname === "/__aw/delete-user" && req.method === "POST") {
    return getContainer(env.AW_CONTAINER, INSTANCE).fetch(new Request("http://container/internal/delete-user", {
      method: "POST", headers: { "x-aw-internal-secret": env.AW_INTERNAL_SECRET, "content-type": "application/json" },
      body: await req.text(),
    }));
  }
  // Owner order 2026-11-03: switch on listed cities in an ADMIN's watch only (sends nothing, no run); read-only read-back
  if (url.pathname === "/__aw/admin-watch-add" && req.method === "POST") {
    return getContainer(env.AW_CONTAINER, INSTANCE).fetch(new Request("http://container/internal/admin-watch-add", {
      method: "POST", headers: { "x-aw-internal-secret": env.AW_INTERNAL_SECRET, "content-type": "application/json" },
      body: await req.text(),
    }));
  }
  if (url.pathname === "/__aw/admin-watch" && req.method === "GET") {
    return getContainer(env.AW_CONTAINER, INSTANCE).fetch(new Request(`http://container/internal/admin-watch?email=${encodeURIComponent(url.searchParams.get("email") ?? "")}`, {
      headers: { "x-aw-internal-secret": env.AW_INTERNAL_SECRET },
    }));
  }
  if (url.pathname === "/__aw/watch-list" && req.method === "GET") {
    const q = url.searchParams.toString();
    return getContainer(env.AW_CONTAINER, INSTANCE).fetch(new Request(`http://container/internal/watch-list${q ? `?${q}` : ""}`, {
      headers: { "x-aw-internal-secret": env.AW_INTERNAL_SECRET },
    }));
  }
  if (url.pathname === "/__aw/feedback" && req.method === "GET") {
    const q = url.searchParams.toString();
    return getContainer(env.AW_CONTAINER, INSTANCE).fetch(new Request(`http://container/internal/feedback${q ? `?${q}` : ""}`, {
      headers: { "x-aw-internal-secret": env.AW_INTERNAL_SECRET },
    }));
  }
  const fbStatus = /^\/__aw\/feedback\/(\d{1,12})\/status$/.exec(url.pathname);
  if (fbStatus && req.method === "POST") {
    return getContainer(env.AW_CONTAINER, INSTANCE).fetch(new Request(`http://container/internal/feedback/${fbStatus[1]}/status`, {
      method: "POST", headers: { "x-aw-internal-secret": env.AW_INTERNAL_SECRET, "content-type": "application/json" },
      body: await req.text(),
    }));
  }
  const fbReply = /^\/__aw\/feedback\/(\d{1,12})\/reply$/.exec(url.pathname);
  if (fbReply && req.method === "POST") {
    return getContainer(env.AW_CONTAINER, INSTANCE).fetch(new Request(`http://container/internal/feedback/${fbReply[1]}/reply`, {
      method: "POST", headers: { "x-aw-internal-secret": env.AW_INTERNAL_SECRET, "content-type": "application/json" },
      body: await req.text(),
    }));
  }
  if (url.pathname === "/__aw/test-signin" && req.method === "POST") {
    return getContainer(env.AW_CONTAINER, INSTANCE).fetch(new Request("http://container/internal/test-signin", {
      method: "POST", headers: { "x-aw-internal-secret": env.AW_INTERNAL_SECRET, "content-type": "application/json" },
      body: await req.text(),
    }));
  }
  // Owner-only diagnostic: create (not complete) a live Checkout Session through the app code path.
  if (url.pathname === "/__aw/checkout-probe" && req.method === "POST") {
    return getContainer(env.AW_CONTAINER, INSTANCE).fetch(new Request("http://container/internal/checkout-probe", {
      method: "POST", headers: { "x-aw-internal-secret": env.AW_INTERNAL_SECRET },
    }));
  }
  // Container -> Worker callback when the background run finishes
  if (url.pathname === "/__aw/run-done" && req.method === "POST") {
    const r = (await req.json()) as { date?: string; ok?: boolean };
    if (!r.date || !/^\d{4}-\d{2}-\d{2}$/.test(r.date)) return new Response("bad date", { status: 400 });
    await getContainer(env.AW_CONTAINER, INSTANCE).finishRun(r.date, !!r.ok, 200, JSON.stringify(r));
    console.log(`run-done ${r.date}: ${r.ok ? "done" : "FAILED"} ${JSON.stringify(r).slice(0, 2000)}`);
    return Response.json({ ok: true });
  }
  // Manual trigger of the same run path (respects the once-per-day guard). Waits for completion.
  if (url.pathname === "/__aw/run" && req.method === "POST") {
    if (!(await fetchEnabled(env))) return Response.json({ ok: false, disabled: true, error: "daily fetch is turned off (admin kill switch)" }, { status: 423 });
    const r = await runOwner(env, denverParts(Date.now()).date, "manual");
    return Response.json(r.body, { status: r.status });
  }
  if (url.pathname === "/__aw/email" && req.method === "POST") {
    const m = (await req.json()) as { to: string; subject: string; text: string };
    const owner = (env.AW_OWNER_EMAIL || "").trim().toLowerCase();
    if (!owner || m.to.trim().toLowerCase() !== owner) return new Response("owner only", { status: 403 });
    const r = await env.EMAIL.send({ to: env.NOTIFY_TO, from: env.MAIL_FROM, subject: m.subject, text: m.text });
    return Response.json({ messageId: r.messageId });
  }
  return new Response("not found", { status: 404 });
}

async function fetchEnabled(env: Env): Promise<boolean> {
  const g: any = await getContainer(env.AW_CONTAINER, INSTANCE).guardView();
  return !!(g.config as any).fetch_enabled;
}

async function containerStatus(env: Env): Promise<any> {
  const res = await getContainer(env.AW_CONTAINER, INSTANCE).fetch(new Request("http://container/internal/status", {
    headers: { "x-aw-internal-secret": env.AW_INTERNAL_SECRET },
  }));
  if (!res.ok) throw new Error(`container status ${res.status}`);
  return res.json();
}

/** Guard row for `date`, reconciled against the container's fetch_runs: a "running" claim whose run
 *  finished (container idle, latest fetch_runs row started that Denver day and has finished_at) -> done. */
async function reconcile(env: Env, date: string): Promise<{ guard: any; status: any }> {
  const stub = getContainer(env.AW_CONTAINER, INSTANCE);
  const status = await containerStatus(env);
  let guard: any = await stub.dailyRun(date);
  const lr = status.latest_run;
  const ranToday = !!lr && String(lr.started_at).slice(0, 10) === date && !!lr.finished_at;
  if ((guard?.status === "running" || guard?.status === "failed" || !guard) && !status.in_progress && ranToday) {
    await stub.finishRun(date, true, 200, JSON.stringify({ reconciled_from_fetch_run: lr.id, outcome: lr.outcome }));
    console.log(`guard ${date}: reconciled running -> done from fetch_runs id=${lr.id}`);
    guard = await stub.dailyRun(date);
  }
  return { guard, status };
}

/** Claim the day and kick off the background run in the container (returns immediately). */
async function runOwner(env: Env, date: string, source: string): Promise<{ status: number; body: Record<string, unknown> }> {
  const stub = getContainer(env.AW_CONTAINER, INSTANCE);
  if (!(await stub.hasSnapshot())) return { status: 503, body: { ok: false, error: "no snapshot seeded" } };
  await reconcile(env, date).catch((e) => console.log("reconcile failed", String(e)));
  if (!(await stub.claimRun(date, source))) {
    console.log(`run-owner ${date}: already claimed; skipping (${source})`);
    return { status: 409, body: { ok: false, error: `already ran or running for ${date}`, run: await stub.dailyRun(date) } };
  }
  let http = 0, text = "";
  try {
    const res = await stub.fetch(new Request(`http://container/internal/run-owner?date=${date}`, {
      method: "POST", headers: { "x-aw-internal-secret": env.AW_INTERNAL_SECRET },
    }));
    http = res.status; text = await res.text();
  } catch (e) {
    text = `exception: ${String(e)}`;
  }
  console.log(`run-owner ${date}: kick-off http=${http} (${source}) ${text.slice(0, 500)}`);
  if (http === 202 || http === 409) return { status: 202, body: { ok: true, date, source, container: http, detail: safeJson(text) } };
  await stub.finishRun(date, false, http, text);
  return { status: 502, body: { ok: false, date, source, http, detail: safeJson(text) } };
}

function safeJson(t: string): unknown {
  try { return JSON.parse(t); } catch { return t.slice(0, 2000); }
}

export default {
  async fetch(req: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(req.url);
    if (url.hostname === "www.agendawatch.app") {
      url.hostname = "agendawatch.app";
      return Response.redirect(url.toString(), 301);
    }
    if (url.pathname.startsWith("/__aw/")) return internal(req, env, url, ctx);
    const stub = getContainer(env.AW_CONTAINER, INSTANCE);
    const version = env.CF_VERSION_METADATA?.id ?? "dev";
    // Bot filter (src/edge.ts): only real app routes reach the container; /internal/* is never public (404).
    return handlePublic(req, {
      forward: (r) => stub.fetch(r),
      gate: () => stub.gate(),
      getHome: () => stub.getHome(version),
      putHome: (html) => stub.putHome(version, html),
      citySlugs: CITY_SLUGS,
      getPage: (path) => stub.getPage(version, path),
      putPage: (path, html) => stub.putPage(version, path, html),
      countLanding: (code) => stub.refLanding(code),
      rateLimit: env.AW_RL ? async (ip) => (await env.AW_RL!.limit({ key: ip })).success : undefined,
      onWakeCap: async () => {
        if (await stub.tripOnce("cap_container_wakes")) ctx.waitUntil(ownerNotice(env, "container wakes",
          "The app has started the maximum number of times today. Signed-in users, sign-in links and Stripe still get through; other visits see a short 'try later' page until midnight Denver time."));
      },
      waitUntil: (p) => ctx.waitUntil(p),
    });
  },

  // Two UTC crons (14:21 = 8:21 MDT, 15:21 = 8:21 MST); run only the one that is 08:21 in Denver.
  async scheduled(ev: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    const d = cronDecision(ev.scheduledTime);
    console.log(`cron ${ev.cron}: ${d.action} (${d.reason})`);
    if (d.action === "skip") return;
    const stub = getContainer(env.AW_CONTAINER, INSTANCE);
    // Kill switch (admin "Daily fetch"): when off, do nothing (no container wake) and record it for the report.
    if (!(await fetchEnabled(env))) {
      await stub.kvSet(`disabled_run:${d.date}`, new Date().toISOString());
      console.warn(`cron ${ev.cron}: daily fetch is turned OFF (admin kill switch); ${d.action} skipped for ${d.date}`);
      return;
    }
    if (d.action === "check") {
      const { guard, status } = await reconcile(env, d.date);
      const lr = status.latest_run;
      const ranToday = !!lr && String(lr.started_at).slice(0, 10) === d.date && !!lr.finished_at;
      if (status.in_progress) console.log(`daily run ${d.date} still in progress since ${status.in_progress.started_at}`);
      else if (!ranToday) console.error(`DAILY RUN MISSING for ${d.date}: latest fetch_run=${JSON.stringify(lr)?.slice(0, 500)} guard=${JSON.stringify(guard)?.slice(0, 500)}`);
      else console.log(`daily run ok for ${d.date}: fetch_run ${lr.id} ${lr.outcome} exit=${lr.exit_code}`);
      return;
    }
    const r = await runOwner(env, d.date, `cron ${ev.cron}`);
    if (r.status >= 500) throw new Error(`run-owner failed: ${JSON.stringify(r.body).slice(0, 2000)}`);
  },
} satisfies ExportedHandler<Env>;
