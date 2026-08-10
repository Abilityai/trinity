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

import {
  ANON_SESSION_IDLE_MS,
  ANON_SESSION_MAX_MS,
  createAnonymousSessionStore,
  readHeader,
} from "./server.js";

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

// ---------------------------------------------------------------------------
// TTL ratchet (#2035 condition 2)
// ---------------------------------------------------------------------------
//
// Pins the DIRECTION, not the value — the #1644 `MAX_ROWS_PER_SWEEP` shape.
// Tightening either window is always safe and needs no test edit; loosening one
// weakens every keyless session at once and has to be a deliberate change to
// this file with a reason attached. The numbers below are the ceilings in force
// when condition 2 was signed off; nothing else ends a keyless session (see
// constraint 2 in server.ts — no logout tool, no usable disconnect signal), so
// these ARE the exposure window for a leaked `Mcp-Session-Id`.

describe("#2035 anonymous session TTL", () => {
  const CEILING_IDLE_MS = 30 * 60 * 1000;
  const CEILING_MAX_MS = 4 * 60 * 60 * 1000;

  it("keeps the idle window at or below its agreed ceiling", () => {
    assert.ok(
      ANON_SESSION_IDLE_MS <= CEILING_IDLE_MS,
      `idle TTL loosened to ${ANON_SESSION_IDLE_MS}ms (ceiling ${CEILING_IDLE_MS}ms)`
    );
  });

  it("keeps the absolute cap at or below its agreed ceiling", () => {
    assert.ok(
      ANON_SESSION_MAX_MS <= CEILING_MAX_MS,
      `absolute TTL loosened to ${ANON_SESSION_MAX_MS}ms (ceiling ${CEILING_MAX_MS}ms)`
    );
  });

  it("keeps the absolute cap above the idle window", () => {
    // An absolute cap at or under the idle window would make idle expiry dead
    // code — the session would always die on the absolute clock first, and the
    // sliding refresh the tests above pin would never be observable.
    assert.ok(ANON_SESSION_MAX_MS > ANON_SESSION_IDLE_MS);
  });
});

describe("#2035 readHeader", () => {
  it("takes the first value of a repeated header", () => {
    assert.equal(readHeader(["a", "b"]), "a");
    assert.equal(readHeader("a"), "a");
    assert.equal(readHeader(undefined), undefined);
  });
});
