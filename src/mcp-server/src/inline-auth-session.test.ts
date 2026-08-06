/**
 * #2035 — a verified inline-auth session must survive to the NEXT request.
 *
 * The bug this file exists to prevent: #848 upgrades a session by mutating its
 * auth context in place, but `mcp-proxy` re-runs `authenticate` on every POST
 * and `FastMCPSession#updateAuth` REPLACES the context. A callback returning a
 * fresh object per request therefore discarded every login, and `verify_login`
 * could report success while the very next tool call answered `login_required`.
 *
 * These are the store's own semantics. They are NECESSARY BUT NOT SUFFICIENT:
 * the 30 tests shipped with #848 were all handler-level — they invoke a tool's
 * `execute` with a context object the test constructs, so the object is never
 * swapped and the bug is invisible by construction. That is why it shipped
 * green. The test that actually pins this bug class has to cross the transport;
 * it lives in `inline-auth-transport.test.ts`. Do not delete that file in
 * favour of this one.
 */
import { strict as assert } from "node:assert";
import { describe, it } from "node:test";

import { createAnonymousSessionStore, readHeader } from "./server.js";

// ---------------------------------------------------------------------------
// Store semantics
// ---------------------------------------------------------------------------

describe("#2035 createAnonymousSessionStore", () => {
  it("returns the SAME object for the same transport session", () => {
    const store = createAnonymousSessionStore();
    const a = store.resolve("session-1");
    const b = store.resolve("session-1");
    // Identity, not equality: the in-place upgrade only survives if the very
    // same object is handed back for `updateAuth` to reinstall.
    assert.equal(a, b);
  });

  it("carries an in-place upgrade across resolves", () => {
    const store = createAnonymousSessionStore();
    const first = store.resolve("session-1");
    first.verifiedEmail = "someone@example.com";
    first.agents = ["demo-agent"];

    const second = store.resolve("session-1");
    assert.equal(second.verifiedEmail, "someone@example.com");
    assert.deepEqual(second.agents, ["demo-agent"]);
  });

  it("isolates distinct transport sessions", () => {
    const store = createAnonymousSessionStore();
    const a = store.resolve("session-1");
    const b = store.resolve("session-2");
    assert.notEqual(a, b);
    a.verifiedEmail = "a@example.com";
    assert.equal(store.resolve("session-2").verifiedEmail, undefined);
  });

  it("never stores a context for an absent session id (the initialize request)", () => {
    const store = createAnonymousSessionStore();
    const a = store.resolve(undefined);
    const b = store.resolve(undefined);
    assert.notEqual(a, b);
    assert.equal(store.size(), 0);
  });

  it("keeps the correlation id independent of the transport id", () => {
    const store = createAnonymousSessionStore();
    const ctx = store.resolve("session-secret-value");
    // The logged/rate-limited id must NOT be the value that grants access —
    // under #2035 the transport id is bearer-equivalent, so leaking it via a
    // log line would hand over the session.
    assert.notEqual(ctx.sessionId, "session-secret-value");
    assert.equal(ctx.scope, "anonymous");
  });

  it("expires an idle session and issues a fresh, pre-login context", () => {
    const store = createAnonymousSessionStore({ idleMs: 1_000 });
    const first = store.resolve("session-1", 0);
    first.verifiedEmail = "someone@example.com";

    const later = store.resolve("session-1", 5_000);
    assert.notEqual(later, first);
    assert.equal(later.verifiedEmail, undefined);
  });

  it("refreshes idle expiry on use", () => {
    const store = createAnonymousSessionStore({ idleMs: 1_000 });
    const first = store.resolve("session-1", 0);
    assert.equal(store.resolve("session-1", 800), first);
    assert.equal(store.resolve("session-1", 1_500), first);
  });

  it("expires on the absolute cap even while in active use", () => {
    const store = createAnonymousSessionStore({ idleMs: 10_000, maxMs: 5_000 });
    const first = store.resolve("session-1", 0);
    assert.equal(store.resolve("session-1", 4_000), first);
    assert.notEqual(store.resolve("session-1", 6_000), first);
  });

  it("bounds retained entries, evicting oldest first", () => {
    const store = createAnonymousSessionStore({ maxEntries: 3 });
    for (let i = 0; i < 10; i++) store.resolve(`session-${i}`);
    assert.ok(store.size() <= 3, `expected <= 3 retained, got ${store.size()}`);
  });
});

describe("#2035 readHeader", () => {
  it("takes the first value of a repeated header", () => {
    assert.equal(readHeader(["a", "b"]), "a");
    assert.equal(readHeader("a"), "a");
    assert.equal(readHeader(undefined), undefined);
  });
});
