// Cloud runs LIVE Stripe only. Test keys (or anything else) are not passed to the container:
// the app then shows billing as unavailable instead of the whole Worker/DO failing.
export interface StripeSecrets {
  STRIPE_SECRET_KEY?: string;
  STRIPE_PUBLISHABLE_KEY?: string;
  STRIPE_WEBHOOK_SECRET?: string;
}

export function liveStripeEnv(env: StripeSecrets): { vars: Record<string, string>; ok: boolean; reason: string } {
  const sk = (env.STRIPE_SECRET_KEY ?? "").trim();
  const pk = (env.STRIPE_PUBLISHABLE_KEY ?? "").trim();
  const wh = (env.STRIPE_WEBHOOK_SECRET ?? "").trim();
  const empty = { STRIPE_SECRET_KEY: "", STRIPE_PUBLISHABLE_KEY: "", STRIPE_WEBHOOK_SECRET: "" };
  if (!(sk.startsWith("sk_live_") || sk.startsWith("rk_live_")))
    return { vars: empty, ok: false, reason: sk ? "secret key is not a live key (refused)" : "secret key missing" };
  if (!pk.startsWith("pk_live_"))
    return { vars: empty, ok: false, reason: pk ? "publishable key is not a live key (refused)" : "publishable key missing" };
  return { vars: { STRIPE_SECRET_KEY: sk, STRIPE_PUBLISHABLE_KEY: pk, STRIPE_WEBHOOK_SECRET: wh }, ok: true, reason: "live" };
}

// Non-secret diagnostics: only the key TYPE (known prefix) and formatting issues, never any key material.
export function stripeKeyKinds(env: StripeSecrets): Record<string, string> {
  const kind = (v: string | undefined, prefixes: string[]) => {
    const raw = v ?? "";
    if (!raw) return "missing";
    const t = raw.trim().replace(/^["']|["']$/g, "");
    const p = prefixes.find((x) => t.startsWith(x)) ?? "unrecognized";
    const notes = [raw !== raw.trim() ? "whitespace" : "", /^["']/.test(raw.trim()) ? "quoted" : ""].filter(Boolean);
    return notes.length ? `${p} (${notes.join(",")})` : p;
  };
  return {
    secret: kind(env.STRIPE_SECRET_KEY, ["sk_live_", "rk_live_", "sk_test_", "rk_test_", "pk_live_", "pk_test_", "whsec_"]),
    publishable: kind(env.STRIPE_PUBLISHABLE_KEY, ["pk_live_", "pk_test_", "sk_live_", "sk_test_", "rk_live_"]),
    webhook: kind(env.STRIPE_WEBHOOK_SECRET, ["whsec_"]),
  };
}
