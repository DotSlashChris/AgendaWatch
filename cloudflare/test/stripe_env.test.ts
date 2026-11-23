import { test } from "node:test";
import assert from "node:assert/strict";
import { liveStripeEnv } from "../src/stripe_env.ts";

test("live keys accepted (sk_ and rk_)", () => {
  for (const sk of ["sk_live_abc", "rk_live_abc"]) {
    const r = liveStripeEnv({ STRIPE_SECRET_KEY: sk, STRIPE_PUBLISHABLE_KEY: "pk_live_x", STRIPE_WEBHOOK_SECRET: "whsec_y" });
    assert.equal(r.ok, true);
    assert.equal(r.vars.STRIPE_SECRET_KEY, sk);
    assert.equal(r.vars.STRIPE_WEBHOOK_SECRET, "whsec_y");
  }
});

test("test keys refused and never passed to the container", () => {
  for (const [sk, pk] of [["sk_test_a", "pk_test_b"], ["rk_test_a", "pk_live_b"], ["sk_live_a", "pk_test_b"], ["", ""]]) {
    const r = liveStripeEnv({ STRIPE_SECRET_KEY: sk, STRIPE_PUBLISHABLE_KEY: pk, STRIPE_WEBHOOK_SECRET: "whsec_t" });
    assert.equal(r.ok, false);
    assert.deepEqual(r.vars, { STRIPE_SECRET_KEY: "", STRIPE_PUBLISHABLE_KEY: "", STRIPE_WEBHOOK_SECRET: "" });
  }
});
