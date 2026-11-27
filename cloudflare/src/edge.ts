// Edge filter for Agenda Watch (2026-10-21). Pure logic, no Cloudflare imports: unit-tested in node.
// Only real app routes reach the container. Everything else is answered here, cheaply, without
// touching the container or the Durable Object. Anonymous GET/HEAD / is served from a stored copy.

/** GET (and HEAD) routes of the FastAPI app (web/app.py). */
export const GET_ROUTES = new Set([
  "/", "/signin", "/auth/verify", "/watch", "/billing", "/billing/success", "/billing/cancel",
  "/billing/return", "/feedback", "/admin", "/healthz",
]);
/** POST routes of the FastAPI app. */
export const POST_ROUTES = new Set([
  "/signin", "/signout", "/watch", "/billing/checkout", "/billing/portal", "/stripe/webhook", "/feedback",
  "/admin/cost-safety",
]);
const POST_PATTERNS = [/^\/admin\/feedback\/\d{1,9}\/reviewed$/];

/** Always forwarded (when seeded), even past the wake budget: money, sign-in links, signed-in users. */
export const ESSENTIAL = new Set(["/stripe/webhook", "/auth/verify", "/signout", "/billing/success",
  "/billing/return", "/billing/cancel"]);

export type Verdict =
  | { kind: "forward" }
  | { kind: "home" }          // anonymous GET/HEAD /
  | { kind: "head" }          // HEAD on another allowed GET route: 200, empty, no container
  | { kind: "robots" }
  | { kind: "sitemap" }
  | { kind: "page" }          // public /cities/<slug> page (catalog city): stored copy per version
  | { kind: "redirect"; location: string }  // /cities/<slug>/ -> /cities/<slug> (query string kept by handlePublic)
  | { kind: "status"; status: number; allow?: string };

/** /cities/<slug>/ (one trailing slash, any slug). Query string is not part of path. */
export const CITY_TRAILING_SLASH = /^\/cities\/[^/]+\/$/;

export function isPostRoute(path: string): boolean {
  return POST_ROUTES.has(path) || POST_PATTERNS.some((re) => re.test(path));
}

/** Public city page paths from the catalog slugs (generated from config.json at deploy). */
export function cityPaths(slugs: readonly string[]): Set<string> {
  return new Set(slugs.map((s) => `/cities/${s}`));
}

export function classify(method: string, path: string, pages: ReadonlySet<string> = new Set()): Verdict {
  const m = method.toUpperCase();
  if (CITY_TRAILING_SLASH.test(path))
    return m === "GET" || m === "HEAD"
      ? { kind: "redirect", location: path.slice(0, -1) }
      : { kind: "status", status: 405, allow: "GET, HEAD" };
  if (pages.has(path)) return m === "GET" || m === "HEAD" ? { kind: "page" } : { kind: "status", status: 405, allow: "GET, HEAD" };
  if (path === "/robots.txt") return m === "GET" || m === "HEAD" ? { kind: "robots" } : { kind: "status", status: 405, allow: "GET, HEAD" };
  if (path === "/sitemap.xml") return m === "GET" || m === "HEAD" ? { kind: "sitemap" } : { kind: "status", status: 405, allow: "GET, HEAD" };
  const isGet = GET_ROUTES.has(path), isPost = isPostRoute(path);
  if (!isGet && !isPost) return { kind: "status", status: 404 };
  if ((m === "GET" || m === "HEAD") && isGet) {
    if (path === "/") return { kind: "home" };
    return m === "HEAD" ? { kind: "head" } : { kind: "forward" };
  }
  if (m === "POST" && isPost) return { kind: "forward" };
  const allow = [...(isGet ? ["GET", "HEAD"] : []), ...(isPost ? ["POST"] : [])].join(", ");
  return { kind: "status", status: 405, allow };
}

/** Starlette SessionMiddleware cookie: base64(JSON).timestamp.signature. We only peek at the payload
 *  (the container still verifies the signature); a forged cookie merely gets forwarded. */
export function isSignedIn(cookieHeader: string | null): boolean {
  if (!cookieHeader) return false;
  const m = /(?:^|;\s*)aw_session=([^;]+)/.exec(cookieHeader);
  if (!m) return false;
  let v = m[1];
  if (v.startsWith('"')) v = v.slice(1, -1);
  const payload = v.split(".")[0];
  try {
    const b64 = payload.replace(/-/g, "+").replace(/_/g, "/");
    const json = JSON.parse(atob(b64 + "=".repeat((4 - (b64.length % 4)) % 4)));
    return typeof json?.email === "string" && json.email.includes("@");
  } catch {
    return false;
  }
}

/** Link-preview bots and crawlers: never wake the container for them. */
export const BOT_UA = /bot\b|bot\/|crawler|spider|slurp|facebookexternalhit|facebot|embedly|preview|whatsapp|telegram|skype|discord|slack|linkedin|pinterest|vkshare|iframely|outbrain|quora link|bitlybot|nuzzel|w3c_validator|headless|python-requests|curl\/|wget|go-http-client|okhttp|scrapy|httpclient|zgrab|masscan|nmap|censys|expanse/i;
export function isBot(ua: string | null): boolean {
  return !ua || BOT_UA.test(ua);
}

export const ROBOTS = "User-agent: *\nAllow: /$\nDisallow: /signin\nDisallow: /auth\nDisallow: /watch\nDisallow: /billing\nDisallow: /admin\nDisallow: /feedback\n\nSitemap: https://agendawatch.app/sitemap.xml\n";
/** Public pages only: the home page plus one page per catalog city (same output as aw_cities.sitemap_xml). */
export function sitemapXml(slugs: readonly string[], site = "https://agendawatch.app"): string {
  const urls = [`${site}/`, ...slugs.map((s) => `${site}/cities/${s}`)];
  return '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    + urls.map((u) => `  <url><loc>${u}</loc></url>\n`).join("") + "</urlset>\n";
}
/** Home-only sitemap (no catalog passed). */
export const SITEMAP = sitemapXml([]);

// ---- Signup attribution (2026-11-27): ?ref=<code> on a public page -> first-touch cookie ----
export const REF_COOKIE = "aw_ref";
export const REF_MAX_AGE = 30 * 24 * 3600;
export const REF_MAX_LEN = 40;
/** Lowercase [a-z0-9-], at most 40 chars; null when nothing usable is left. Mirrors clean_ref() in web/app.py. */
export function cleanRef(v: string | null | undefined): string | null {
  if (typeof v !== "string") return null;
  const c = v.trim().toLowerCase().replace(/[_\s]+/g, "-").replace(/[^a-z0-9-]/g, "")
    .replace(/^-+|-+$/g, "").slice(0, REF_MAX_LEN).replace(/-+$/g, "");
  return c || null;
}
export function refFromCookie(cookieHeader: string | null): string | null {
  const m = cookieHeader ? /(?:^|;\s*)aw_ref=([^;]+)/.exec(cookieHeader) : null;
  return m ? cleanRef(m[1]) : null;
}
export function refCookie(code: string): string {
  return `${REF_COOKIE}=${code}; Max-Age=${REF_MAX_AGE}; Path=/; SameSite=Lax; Secure; HttpOnly`;
}
const REF_PATHS = (p: string) => p === "/" || p === "/signin" || p.startsWith("/cities/");

/** Add the first-touch cookie to a successful public GET/HEAD that carried ?ref=. Never for bots or for a
 *  visitor who already has the cookie (first touch wins). Counts one landing per first touch (a bare
 *  per-code counter in the Durable Object: no IP, no user agent, nothing about the visitor). */
async function withRef(req: Request, res: Response, deps: Deps): Promise<Response> {
  const m = req.method.toUpperCase();
  if (m !== "GET" && m !== "HEAD") return res;
  const url = new URL(req.url);
  if (!REF_PATHS(url.pathname) || res.status >= 400) return res;
  const code = cleanRef(url.searchParams.get("ref"));
  if (!code || isBot(req.headers.get("user-agent")) || refFromCookie(req.headers.get("cookie"))) return res;
  const out = new Response(res.body, res);
  if (!/(?:^|,\s*)aw_ref=/.test(res.headers.get("set-cookie") ?? "")) out.headers.append("set-cookie", refCookie(code));
  out.headers.set("cache-control", "private, no-store");
  if (deps.countLanding) {
    const job = deps.countLanding(code).catch(() => undefined);
    if (deps.waitUntil) deps.waitUntil(job);
  }
  return out;
}

export interface Gate { seeded: boolean; running: boolean; wakes: number; wakeCap: number }
export interface HomeCopy { html: string; at: number }
export interface Deps {
  forward(req: Request): Promise<Response>;                 // proxy to the container (may wake it)
  gate(): Promise<Gate>;                                    // DO RPC (does not wake the container)
  getHome(): Promise<HomeCopy | null>;                      // DO RPC
  putHome(html: string): Promise<void>;                     // DO RPC
  rateLimit?(key: string): Promise<boolean>;                // true = allowed
  citySlugs?: readonly string[];                            // catalog city slugs -> /cities/<slug> + sitemap
  getPage?(path: string): Promise<HomeCopy | null>;         // DO RPC: stored anonymous copy of a public page
  putPage?(path: string, html: string): Promise<void>;      // DO RPC
  countLanding?(code: string): Promise<unknown>;            // DO RPC: +1 first-touch landing for this ref code
  onWakeCap?(): Promise<void>;                              // owner notice (once per day, deduped by the caller)
  waitUntil?(p: Promise<unknown>): void;
  now?(): number;
}

const HOME_REFRESH_MS = 6 * 3600 * 1000;   // refresh the copy only while the container is awake anyway

const text = (body: string, status: number, extra: Record<string, string> = {}) =>
  new Response(body, { status, headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store", "x-aw-edge": "1", ...extra } });

export function moving(): Response {
  return text("Agenda Watch is moving — back in a minute.", 503, { "retry-after": "60" });
}

function busy(): Response {
  return new Response("<!doctype html><meta charset=utf-8><title>Agenda Watch</title><body style=\"font-family:system-ui;max-width:36rem;margin:3rem auto;padding:0 1rem\"><h1>Agenda Watch is resting</h1><p>We've hit today's safety limit for starting the app. Please try again later. If you have a sign-in link, it still works.</p><p><a href=\"/\">Home</a></p>",
    { status: 503, headers: { "content-type": "text/html; charset=utf-8", "retry-after": "1800", "cache-control": "no-store", "x-aw-edge": "wake-cap" } });
}

/** Self-referencing canonical (sitemap form, fixed apex). Used when serving stored or freshly fetched public HTML. */
export const SITE = "https://agendawatch.app";
export function withCanonical(html: string, path: string): string {
  const href = path === "/" ? `${SITE}/` : `${SITE}${path}`;
  const tag = `<link rel="canonical" href="${href}">`;
  if (/<link rel="canonical" href="[^"]*">/.test(html)) return html.replace(/<link rel="canonical" href="[^"]*">/, tag);
  if (html.includes("</title>")) return html.replace("</title>", `</title>\n${tag}`);
  return html;
}

function homeResponse(html: string, method: string, src: string): Response {
  return new Response(method === "HEAD" ? null : withCanonical(html, "/"), {
    status: 200,
    headers: { "content-type": "text/html; charset=utf-8", "cache-control": "public, max-age=300", "x-aw-edge": src },
  });
}

/** Render / from the container anonymously (no cookie) and keep it if it is the signed-out page. */
async function fetchHome(req: Request, deps: Deps): Promise<string | null> {
  const r = await deps.forward(new Request(new URL("/", req.url).toString(), {
    method: "GET", headers: { accept: "text/html", "user-agent": "AgendaWatch-Edge/1.0" },
  }));
  if (r.status !== 200) return null;
  const html = await r.text();
  if (html.includes('action="/signout"') || !html.includes("</html>")) return null; // never cache a signed-in page
  await deps.putHome(html);
  return html;
}

function pageResponse(html: string, method: string, src: string, path: string): Response {
  return new Response(method === "HEAD" ? null : withCanonical(html, path), {
    status: 200,
    headers: { "content-type": "text/html; charset=utf-8", "cache-control": "public, max-age=3600", "x-aw-edge": src },
  });
}

/** Anonymous (people and crawlers) GET/HEAD of a public city page: the stored copy for this Worker
 *  version; on a miss, render it once from the container anonymously (wake budget applies) and keep it. */
async function publicPage(req: Request, path: string, method: string, deps: Deps): Promise<Response> {
  const copy = deps.getPage ? await deps.getPage(path) : null;
  if (copy) return pageResponse(copy.html, method, "page", path);
  const g = await deps.gate();
  if (!g.seeded) return moving();
  if (!g.running && g.wakes >= g.wakeCap) { await deps.onWakeCap?.(); return busy(); }
  const r = await deps.forward(new Request(new URL(path, req.url).toString(), {
    method: "GET", headers: { accept: "text/html", "user-agent": "AgendaWatch-Edge/1.0" },
  }));
  if (r.status !== 200) return text(r.status === 404 ? "Not found" : "Try again later", r.status === 404 ? 404 : 503);
  const html = await r.text();
  if (html.includes('action="/signout"') || !html.includes("</html>")) return text("Try again later", 503);
  if (deps.putPage) await deps.putPage(path, html);
  return pageResponse(html, method, "page-miss", path);
}

export async function handlePublic(req: Request, deps: Deps): Promise<Response> {
  return withRef(req, await handlePublicInner(req, deps), deps);
}

async function handlePublicInner(req: Request, deps: Deps): Promise<Response> {
  const url = new URL(req.url);
  const path = url.pathname, method = req.method.toUpperCase();
  const slugs = deps.citySlugs ?? [];
  const v = classify(method, path, cityPaths(slugs));
  if (v.kind === "status") {
    return text(v.status === 404 ? "Not found" : "Method not allowed", v.status, v.allow ? { allow: v.allow } : {});
  }
  if (v.kind === "redirect") {
    // Relative Location: /cities/<slug> plus the original query string. Host is not taken from the request.
    const loc = v.location + url.search;
    return new Response(null, { status: 301, headers: { location: loc, "cache-control": "public, max-age=86400", "x-aw-edge": "slash" } });
  }
  if (v.kind === "robots") return new Response(method === "HEAD" ? null : ROBOTS, { headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "public, max-age=86400", "x-aw-edge": "robots" } });
  if (v.kind === "sitemap") return new Response(method === "HEAD" ? null : sitemapXml(slugs), { headers: { "content-type": "application/xml; charset=utf-8", "cache-control": "public, max-age=86400", "x-aw-edge": "sitemap" } });
  if (v.kind === "head") return new Response(null, { status: 200, headers: { "content-type": "text/html; charset=utf-8", "x-aw-edge": "head" } });

  const signed = isSignedIn(req.headers.get("cookie"));
  if (v.kind === "page" && (!signed || method === "HEAD")) return publicPage(req, path, method, deps);
  const essential = signed || ESSENTIAL.has(path) || (method === "POST" && path === "/signin");
  const bot = isBot(req.headers.get("user-agent"));

  // Anonymous home, and any GET from a bot without a session (link previews, crawlers, mail scanners
  // that pre-open sign-in links): the stored home page, no container.
  if ((v.kind === "home" && !signed) || (bot && !signed && method === "GET" && path !== "/healthz" && path !== "/stripe/webhook")) {
    const copy = await deps.getHome();
    if (copy) {
      const now = (deps.now ?? Date.now)();
      if (now - copy.at > HOME_REFRESH_MS && deps.waitUntil) {
        deps.waitUntil((async () => { const g = await deps.gate(); if (g.running) await fetchHome(req, deps); })().catch(() => undefined));
      }
      return homeResponse(copy.html, method, path === "/" ? "home" : "bot-home");
    }
    if (bot && path !== "/") return text("Not found", 404); // no copy yet: still never wake for a bot
    const g = await deps.gate();
    if (!g.seeded) return moving();
    if (!g.running && g.wakes >= g.wakeCap) { await deps.onWakeCap?.(); return busy(); }
    const html = await fetchHome(req, deps);
    if (html) return homeResponse(html, method, "home-miss");
    return deps.forward(new Request(new URL("/", req.url).toString(), { method: "GET", headers: { "user-agent": "AgendaWatch-Edge/1.0" } }));
  }

  if (path !== "/stripe/webhook" && deps.rateLimit) {
    const ip = req.headers.get("cf-connecting-ip") ?? "unknown";
    if (!(await deps.rateLimit(ip))) return text("Too many requests", 429, { "retry-after": "60" });
  }
  const g = await deps.gate();
  if (!g.seeded) return moving(); // never boot the app before data is seeded
  if (!g.running && !essential && g.wakes >= g.wakeCap) { await deps.onWakeCap?.(); return busy(); }
  return deps.forward(req);
}
