import { test } from "node:test";
import assert from "node:assert/strict";
import { classify, handlePublic, isSignedIn, isBot, type Deps, type Gate } from "../src/edge.ts";

const BROWSER = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36";
const HOME = '<!doctype html><html><body><a class="btn" href="/signin">Get started free</a></body></html>';
const cookie = (payload: object) => `aw_session=${btoa(JSON.stringify(payload))}.ZxYwVu.sig123`;

function fake(over: Partial<Gate> = {}, home: { html: string; at: number } | null = { html: HOME, at: Date.now() }) {
  const seen: string[] = [];
  let gates = 0, wakeNotices = 0;
  let stored = home;
  const deps: Deps = {
    forward: async (r) => { seen.push(`${r.method} ${new URL(r.url).pathname}`); return new Response(HOME, { status: 200, headers: { "set-cookie": "aw_session=x" } }); },
    gate: async () => { gates++; return { seeded: true, running: true, wakes: 0, wakeCap: 60, ...over }; },
    getHome: async () => stored,
    putHome: async (html) => { stored = { html, at: Date.now() }; },
    rateLimit: async () => true,
    onWakeCap: async () => { wakeNotices++; },
  };
  return { deps, seen, gates: () => gates, wakeNotices: () => wakeNotices, stored: () => stored };
}
const req = (method: string, path: string, headers: Record<string, string> = {}, body?: string) =>
  new Request(`https://agendawatch.app${path}`, { method, headers: { "user-agent": BROWSER, ...headers }, body });

test("classify: allowlist, 404 for junk, 405 for wrong method", () => {
  for (const p of ["/signin", "/auth/verify", "/watch", "/billing", "/billing/success", "/billing/cancel", "/billing/return", "/feedback", "/admin", "/healthz"])
    assert.equal(classify("GET", p).kind, "forward", p);
  for (const p of ["/signin", "/signout", "/watch", "/billing/checkout", "/billing/portal", "/stripe/webhook", "/feedback", "/admin/cost-safety", "/admin/feedback/12/reviewed"])
    assert.equal(classify("POST", p).kind, "forward", p);
  assert.equal(classify("GET", "/").kind, "home");
  assert.equal(classify("HEAD", "/").kind, "home");
  assert.equal(classify("HEAD", "/signin").kind, "head");
  for (const p of ["/wp-admin/install.php", "/login", "/register", "/.env", "/internal/status", "/admin/feedback/x/reviewed", "/signin/", "/xmlrpc.php"])
    assert.deepEqual(classify("GET", p), { kind: "status", status: 404 }, p);
  assert.deepEqual(classify("PUT", "/watch"), { kind: "status", status: 405, allow: "GET, HEAD, POST" });
  assert.deepEqual(classify("GET", "/stripe/webhook"), { kind: "status", status: 405, allow: "POST" });
  assert.deepEqual(classify("DELETE", "/"), { kind: "status", status: 405, allow: "GET, HEAD" });
});

test("junk paths get a 404 without touching the container or the DO", async () => {
  const f = fake();
  for (const p of ["/wp-admin/install.php", "/login", "/register", "/.git/config", "/internal/run-owner", "/cgi-bin/x"]) {
    const r = await handlePublic(req("GET", p), f.deps);
    assert.equal(r.status, 404, p);
    assert.equal(r.headers.get("x-aw-edge"), "1");
  }
  const r = await handlePublic(req("POST", "/wp-login.php", {}, "x"), f.deps);
  assert.equal(r.status, 404);
  const m = await handlePublic(req("PATCH", "/watch", {}, "x"), f.deps);
  assert.equal(m.status, 405);
  assert.deepEqual(f.seen, []);
  assert.equal(f.gates(), 0);
});

test("allowed routes are forwarded to the container", async () => {
  const f = fake();
  for (const p of ["/signin", "/watch", "/billing", "/feedback", "/admin", "/healthz"]) assert.equal((await handlePublic(req("GET", p), f.deps)).status, 200);
  await handlePublic(req("POST", "/signin", { "content-type": "application/x-www-form-urlencoded" }, "email=a%40b.c&csrf=x"), f.deps);
  await handlePublic(req("POST", "/admin/feedback/7/reviewed", {}, "csrf=x"), f.deps);
  assert.deepEqual(f.seen, ["GET /signin", "GET /watch", "GET /billing", "GET /feedback", "GET /admin", "GET /healthz", "POST /signin", "POST /admin/feedback/7/reviewed"]);
});

test("Stripe webhook is forwarded (not rate limited, not blocked by the wake budget)", async () => {
  const f = fake({ running: false, wakes: 999 });
  f.deps.rateLimit = async () => false;
  const r = await handlePublic(new Request("https://agendawatch.app/stripe/webhook", { method: "POST", headers: { "user-agent": "Stripe/1.0 (+https://stripe.com/docs/webhooks)", "stripe-signature": "t=1,v1=bad" }, body: "{}" }), f.deps);
  assert.equal(r.status, 200);
  assert.deepEqual(f.seen, ["POST /stripe/webhook"]);
});

test("sign-in link routes are forwarded, even past the wake budget", async () => {
  const f = fake({ running: false, wakes: 60, wakeCap: 60 });
  assert.equal((await handlePublic(req("GET", "/auth/verify?token=abc"), f.deps)).status, 200);
  assert.equal((await handlePublic(req("POST", "/signin", {}, "email=a%40b.c"), f.deps)).status, 200);
  assert.deepEqual(f.seen, ["GET /auth/verify", "POST /signin"]);
});

test("anonymous GET and HEAD / come from the stored copy, no container", async () => {
  const f = fake({ running: false });
  const r = await handlePublic(req("GET", "/"), f.deps);
  assert.equal(r.status, 200);
  assert.equal(await r.text(), HOME);
  assert.equal(r.headers.get("set-cookie"), null);
  const h = await handlePublic(req("HEAD", "/"), f.deps);
  assert.equal(h.status, 200);
  const anonCookie = await handlePublic(req("GET", "/", { cookie: cookie({ csrf: "abc" }) }), f.deps);
  assert.equal(anonCookie.headers.get("x-aw-edge"), "home");
  assert.deepEqual(f.seen, []);
  assert.equal(f.gates(), 0);
});

test("signed-in GET / passes through to the container", async () => {
  const f = fake();
  await handlePublic(req("GET", "/", { cookie: cookie({ csrf: "abc", email: "someone@example.invalid" }) }), f.deps);
  assert.deepEqual(f.seen, ["GET /"]);
});

test("home copy missing: rendered once anonymously, stored, then served from the copy", async () => {
  const f = fake({ running: false }, null);
  const r = await handlePublic(req("GET", "/", { cookie: cookie({ email: "x" }) /* no @ -> not signed in */ }), f.deps);
  assert.equal(r.status, 200);
  assert.equal(r.headers.get("set-cookie"), null);
  assert.ok(f.stored());
  await handlePublic(req("GET", "/"), f.deps);
  assert.deepEqual(f.seen, ["GET /"]);
});

test("link-preview bots never wake the container", async () => {
  const f = fake({ running: false });
  for (const ua of ["facebookexternalhit/1.1", "Slackbot-LinkExpanding 1.0", "Twitterbot/1.0", "Mozilla/5.0 (compatible; Discordbot/2.0)", "LinkedInBot/1.0", "WhatsApp/2.23", "TelegramBot (like TwitterBot)"]) {
    for (const p of ["/", "/signin", "/auth/verify?token=abc"]) {
      const r = await handlePublic(req("GET", p, { "user-agent": ua }), f.deps);
      assert.equal(r.status, 200, `${ua} ${p}`);
    }
    assert.equal((await handlePublic(req("HEAD", "/", { "user-agent": ua }), f.deps)).status, 200);
  }
  assert.deepEqual(f.seen, []);
  const none = fake({ running: false }, null); // no copy yet: bot on a non-home page gets 404, still no wake
  assert.equal((await handlePublic(req("GET", "/signin", { "user-agent": "Twitterbot/1.0" }), none.deps)).status, 404);
  assert.deepEqual(none.seen, []);
});

test("wake budget: sleeping container + budget used -> 503 for anonymous pages, one notice hook", async () => {
  const f = fake({ running: false, wakes: 60, wakeCap: 60 });
  const r = await handlePublic(req("GET", "/signin"), f.deps);
  assert.equal(r.status, 503);
  assert.equal(f.wakeNotices(), 1);
  // signed-in users still get through
  await handlePublic(req("GET", "/watch", { cookie: cookie({ email: "a@b.c" }) }), f.deps);
  assert.deepEqual(f.seen, ["GET /watch"]);
  // awake container: no budget check needed
  const g = fake({ running: true, wakes: 999, wakeCap: 60 });
  assert.equal((await handlePublic(req("GET", "/signin"), g.deps)).status, 200);
});

test("rate limit returns 429 before reaching the container; not seeded -> 503", async () => {
  const f = fake();
  f.deps.rateLimit = async () => false;
  assert.equal((await handlePublic(req("GET", "/signin"), f.deps)).status, 429);
  const n = fake({ seeded: false });
  assert.equal((await handlePublic(req("GET", "/signin"), n.deps)).status, 503);
  assert.deepEqual([...f.seen, ...n.seen], []);
});

test("isSignedIn / isBot", () => {
  assert.equal(isSignedIn(null), false);
  assert.equal(isSignedIn("other=1"), false);
  assert.equal(isSignedIn(cookie({ csrf: "x" })), false);
  assert.equal(isSignedIn("a=1; " + cookie({ email: "u@x.io", csrf: "y" })), true);
  assert.equal(isSignedIn("aw_session=%%%"), false);
  assert.equal(isBot(BROWSER), false);
  assert.equal(isBot(""), true);
  assert.equal(isBot("Googlebot/2.1"), true);
});

test("robots.txt and sitemap.xml are static, served to crawlers, never touch the container or the DO", async () => {
  const f = fake();
  const GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)";
  const robots = await handlePublic(req("GET", "/robots.txt", { "user-agent": GOOGLEBOT }), f.deps);
  assert.equal(robots.status, 200);
  const rt = await robots.text();
  for (const p of ["/admin", "/watch", "/billing", "/auth", "/feedback"]) assert.match(rt, new RegExp(`Disallow: ${p}\\n`), p);
  assert.match(rt, /Allow: \/\$/);
  assert.match(rt, /Sitemap: https:\/\/agendawatch\.app\/sitemap\.xml/);
  const sm = await handlePublic(req("GET", "/sitemap.xml", { "user-agent": GOOGLEBOT }), f.deps);
  assert.equal(sm.status, 200);
  assert.match(sm.headers.get("content-type") ?? "", /xml/);
  const xml = await sm.text();
  assert.deepEqual(xml.match(/<loc>[^<]+<\/loc>/g), ["<loc>https://agendawatch.app/</loc>"]); // no catalog passed -> home only
  assert.equal((await handlePublic(req("HEAD", "/sitemap.xml"), f.deps)).status, 200);
  assert.equal((await handlePublic(req("POST", "/sitemap.xml", {}, "x"), f.deps)).status, 405);
  for (const p of ["/sitemap.xml.gz", "/sitemap_index.xml", "/robots.txt/", "/sitemap"]) assert.equal((await handlePublic(req("GET", p), f.deps)).status, 404, p);
  assert.deepEqual(f.seen, []);
  assert.equal(f.gates(), 0);
});
