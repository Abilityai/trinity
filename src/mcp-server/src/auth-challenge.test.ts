/**
 * Tests for #2322 — a login that issued no session must fail at the login call.
 *
 * `authenticate()` checked only `response.ok`, so a deferred login stored
 * `undefined` and reported success; every later request then 401'd with
 * nothing pointing back at the login. A deferred login is now a **403**
 * carrying `mfa_required`, so the body is parsed BEFORE the status check —
 * otherwise the one failure with a specific, actionable cause would report a
 * bare "Forbidden".
 *
 * Runner: built-in `node:test`. No new devDependency. Run via:
 *   node --import tsx --test src/auth-challenge.test.ts
 */
import { describe, it, beforeEach, afterEach } from "node:test";
import { strict as assert } from "node:assert";

import { TrinityClient } from "./client.js";

const realFetch = globalThis.fetch;

function stubFetch(body: unknown, status = 200) {
  globalThis.fetch = (async () =>
    new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    })) as typeof fetch;
}

describe("#2322 authenticate() rejects a session-less 200", () => {
  beforeEach(() => {
    globalThis.fetch = realFetch;
  });
  afterEach(() => {
    globalThis.fetch = realFetch;
  });

  it("throws a specific error on the 403 challenge", async () => {
    stubFetch(
      {
        detail: "mfa_required",
        mfa_required: true,
        mfa_enrolled: true,
        enrollment_required: false,
        challenge_token: "eyJ-challenge",
      },
      403,
    );
    const client = new TrinityClient("http://backend:8000");

    await assert.rejects(
      () => client.authenticate("admin", "pw"),
      /second factor/i,
      "a pending challenge must fail at the login call, not silently later",
    );
  });

  it("does not report the challenge as a bare 'Forbidden'", async () => {
    // The regression this ordering exists to prevent: checking `response.ok`
    // first collapses the one actionable failure into the generic status text.
    stubFetch({ detail: "mfa_required", mfa_required: true }, 403);
    const client = new TrinityClient("http://backend:8000");

    await assert.rejects(() => client.authenticate("admin", "pw"), (e: Error) => {
      assert.doesNotMatch(e.message, /Forbidden/i);
      assert.match(e.message, /MCP API key/i, "must point at the workaround");
      return true;
    });
  });

  it("still reports an ordinary auth failure by status", async () => {
    stubFetch({ detail: "Incorrect username or password" }, 401);
    const client = new TrinityClient("http://backend:8000");

    await assert.rejects(() => client.authenticate("admin", "pw"), /Authentication failed/i);
  });

  it("throws when a 2xx carries no token (the belt)", async () => {
    stubFetch({ token_type: "bearer" });
    const client = new TrinityClient("http://backend:8000");

    await assert.rejects(
      () => client.authenticate("admin", "pw"),
      /no access token/i,
    );
  });

  it("still accepts a real grant", async () => {
    stubFetch({ access_token: "eyJ-real", token_type: "bearer" });
    const client = new TrinityClient("http://backend:8000");

    await client.authenticate("admin", "pw");
  });
});
