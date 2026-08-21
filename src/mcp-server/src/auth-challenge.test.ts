/**
 * Tests for #2322 — a 2xx from /token is not proof a session was issued.
 *
 * When enterprise 2FA defers a login, /token answers HTTP 200 carrying the
 * challenge and no `access_token`. `authenticate()` checked only
 * `response.ok`, so it stored `undefined` and reported success; every later
 * request then 401'd with nothing pointing back at the login.
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

  it("throws when a second factor is pending", async () => {
    stubFetch({
      mfa_required: true,
      mfa_enrolled: true,
      enrollment_required: false,
      challenge_token: "eyJ-challenge",
    });
    const client = new TrinityClient("http://backend:8000");

    await assert.rejects(
      () => client.authenticate("admin", "pw"),
      /second factor/i,
      "a pending challenge must fail at the login call, not silently later",
    );
  });

  it("throws when the token is absent for any other reason", async () => {
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
