import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { classify, cityPaths, handlePublic, sitemapXml, withCanonical, type Deps } from "../src/edge.ts";
import { CITY_SLUGS } from "../src/catalog.gen.ts";

const BROWSER = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36";
const GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)";
// Same rule as aw_cities.city_slug, applied to config.json's catalog in display order.
const cfg = JSON.parse(readFileSync(new URL("../../config.json", import.meta.url), "utf8"));
const slug = (n: string) => n.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
const PAGE = (n: string) => `<!doctype html><html><head><title>${n} short-term rental agendas</title></head><body><h1>${n} short-term rental agendas</h1><a href="/signin">Sign in</a></body></html>`;

function fake(store: Map<string, string> = new Map()) {
  const seen: string[] = [];
  const deps: Deps = {
    forward: async (r) => { const p = new URL(r.url).pathname; seen.push(`${r.method} ${p} ${r.headers.get("cookie") ?? ""}`);
      return p.startsWith("/cities/") ? new Response(PAGE(p), { status: 200 }) : new Response("Not found", { status: 404 }); },
    gate: async () => ({ seeded: true, running: true, wakes: 0, wakeCap: 60 }),
    getHome: async () => ({ html: "<html>HOME</html>", at: Date.now() }),
    putHome: async () => undefined,
    rateLimit: async () => true,
    citySlugs: CITY_SLUGS,
    getPage: async (p) => (store.has(p) ? { html: store.get(p)!, at: Date.now() } : null),
    putPage: async (p, h) => { store.set(p, h); },
  };
  return { deps, seen, store };
}
const req = (method: string, path: string, ua = BROWSER, headers: Record<string, string> = {}) =>
  new Request(`https://agendawatch.app${path}`, { method, headers: { "user-agent": ua, ...headers } });

test("generated catalog matches config.json (17 cities, kebab-case)", () => {
  const want: string[] = [];
  for (const st of Object.values<any>(cfg.states)) for (const m of Object.values<any>(st.metros)) for (const c of Object.values<any>(m.cities)) want.push(slug(c.name));
  assert.deepEqual([...CITY_SLUGS], want);
  assert.equal(CITY_SLUGS.length, 17);
  for (const s of ["park-city", "st-george", "salt-lake-city", "cottonwood-heights"]) assert.ok(CITY_SLUGS.includes(s), s);
});

test("classify: catalog city paths are pages; unknown slugs 404; wrong method 405", () => {
  const pages = cityPaths(CITY_SLUGS);
  for (const s of CITY_SLUGS) { assert.equal(classify("GET", `/cities/${s}`, pages).kind, "page"); assert.equal(classify("HEAD", `/cities/${s}`, pages).kind, "page"); }
  for (const p of ["/cities/moab", "/cities/", "/cities", "/cities/Park-City", "/cities/st.-george"])
    assert.deepEqual(classify("GET", p, pages), { kind: "status", status: 404 }, p);
  assert.deepEqual(classify("GET", "/cities/park-city/", pages), { kind: "redirect", location: "/cities/park-city" });
  assert.deepEqual(classify("HEAD", "/cities/moab/", pages), { kind: "redirect", location: "/cities/moab" });
  assert.deepEqual(classify("POST", "/cities/park-city/", pages), { kind: "status", status: 405, allow: "GET, HEAD" });
  assert.equal(classify("POST", "/cities/park-city", pages).kind, "status");
});

test("sitemap.xml lists the home page plus exactly the catalog cities", async () => {
  const { deps, seen } = fake();
  const r = await handlePublic(req("GET", "/sitemap.xml"), deps);
  assert.equal(r.status, 200);
  const locs = [...(await r.text()).matchAll(/<loc>([^<]+)<\/loc>/g)].map((m) => m[1]);
  assert.deepEqual(locs, ["https://agendawatch.app/", ...CITY_SLUGS.map((s) => `https://agendawatch.app/cities/${s}`)]);
  assert.equal(sitemapXml(CITY_SLUGS), sitemapXml([...CITY_SLUGS]));
  assert.deepEqual(seen, []); // never wakes the container
});

test("anonymous page: first view renders once without cookies and stores it; next views (people and bots) use the copy", async () => {
  const { deps, seen, store } = fake();
  const r1 = await handlePublic(req("GET", "/cities/park-city", BROWSER, { cookie: "aw_csrf=abc" }), deps);
  assert.equal(r1.status, 200);
  assert.match(await r1.text(), /Park City|\/cities\/park-city/);
  assert.equal(r1.headers.get("x-aw-edge"), "page-miss");
  assert.deepEqual(seen, ["GET /cities/park-city "]);
  assert.ok(store.has("/cities/park-city"));
  const r2 = await handlePublic(req("GET", "/cities/park-city", GOOGLEBOT), deps);
  assert.equal(r2.status, 200); assert.equal(r2.headers.get("x-aw-edge"), "page");
  const r3 = await handlePublic(req("HEAD", "/cities/park-city"), deps);
  assert.equal(r3.status, 200); assert.equal(await r3.text(), "");
  assert.equal(seen.length, 1);
});

test("bots get the city page (not the home copy) and unknown slugs 404 without waking", async () => {
  const { deps, seen } = fake();
  const r = await handlePublic(req("GET", "/cities/st-george", GOOGLEBOT), deps);
  assert.equal(r.status, 200);
  assert.doesNotMatch(await r.text(), /HOME/);
  const n = await handlePublic(req("GET", "/cities/moab", GOOGLEBOT), deps);
  assert.equal(n.status, 404);
  const n2 = await handlePublic(req("GET", "/cities/moab"), deps);
  assert.equal(n2.status, 404);
  assert.deepEqual(seen, ["GET /cities/st-george "]);
});

test("a page is not stored if the container did not render the anonymous page", async () => {
  const { deps, store } = fake();
  deps.forward = async () => new Response('<html><form action="/signout"></form></html>', { status: 200 });
  assert.equal((await handlePublic(req("GET", "/cities/sandy"), deps)).status, 503);
  deps.forward = async () => new Response("boom", { status: 500 });
  assert.equal((await handlePublic(req("GET", "/cities/sandy"), deps)).status, 503);
  assert.equal(store.size, 0);
});

test("no seed -> moving; wake cap reached while stopped -> busy, no forward", async () => {
  const { deps, seen } = fake();
  deps.gate = async () => ({ seeded: false, running: false, wakes: 0, wakeCap: 60 });
  assert.notEqual((await handlePublic(req("GET", "/cities/orem"), deps)).status, 200);
  deps.gate = async () => ({ seeded: true, running: false, wakes: 60, wakeCap: 60 });
  assert.notEqual((await handlePublic(req("GET", "/cities/orem"), deps)).status, 200);
  assert.deepEqual(seen, []);
});

test("signed-in GET of a city page is forwarded as-is", async () => {
  const { deps, seen } = fake();
  const c = `aw_session=${btoa(JSON.stringify({ email: "a@example.com" }))}.ZxYwVu.sig123`;
  const r = await handlePublic(req("GET", "/cities/provo", BROWSER, { cookie: c }), deps);
  assert.equal(r.status, 200);
  assert.equal(seen.length, 1, String(seen)); assert.ok(seen[0].startsWith("GET /cities/provo aw_session="), String(seen));
});

test("trailing slash on a city path 301s to the no-slash sitemap form and keeps the query string", async () => {
  const { deps, seen } = fake();
  const r = await handlePublic(req("GET", "/cities/salt-lake-city/?utm=1"), deps);
  assert.equal(r.status, 301);
  assert.equal(r.headers.get("location"), "/cities/salt-lake-city?utm=1");
  assert.equal(r.headers.get("x-aw-edge"), "slash");
  const h = await handlePublic(req("HEAD", "/cities/park-city/"), deps);
  assert.equal(h.status, 301);
  assert.equal(h.headers.get("location"), "/cities/park-city");
  assert.deepEqual(seen, []); // redirect is at the edge; no container
});

test("unknown slug with a trailing slash 301s; the no-slash target is still 404", async () => {
  const { deps, seen } = fake();
  const r = await handlePublic(req("GET", "/cities/moab/?x=1"), deps);
  assert.equal(r.status, 301);
  assert.equal(r.headers.get("location"), "/cities/moab?x=1");
  const n = await handlePublic(req("GET", "/cities/moab"), deps);
  assert.equal(n.status, 404);
  assert.deepEqual(seen, []);
});

test("withCanonical writes the sitemap-form apex URL and is a no-op when already correct", () => {
  const home = '<!doctype html><html><head><title>Agenda Watch</title>\n<style></style></head><body></body></html>';
  const h = withCanonical(home, "/");
  assert.equal(h.match(/<link rel="canonical" href="([^"]+)">/)?.[1], "https://agendawatch.app/");
  assert.equal(withCanonical(h, "/"), h);
  const city = withCanonical('<html><head><title>X</title></head></html>', "/cities/salt-lake-city");
  assert.equal(city.match(/<link rel="canonical" href="([^"]+)">/)?.[1], "https://agendawatch.app/cities/salt-lake-city");
});

test("stored home and city copies without a canonical still get one in the response", async () => {
  const { deps } = fake(new Map([["/cities/sandy", "<!doctype html><html><head><title>Sandy</title></head><body></body></html>"]]));
  deps.getHome = async () => ({ html: "<!doctype html><html><head><title>Agenda Watch</title></head><body>HOME</body></html>", at: Date.now() });
  const home = await handlePublic(req("GET", "/"), deps);
  assert.equal(home.status, 200);
  assert.match(await home.text(), /<link rel="canonical" href="https:\/\/agendawatch.app\/">/);
  const city = await handlePublic(req("GET", "/cities/sandy"), deps);
  assert.match(await city.text(), /<link rel="canonical" href="https:\/\/agendawatch.app\/cities\/sandy">/);
});
