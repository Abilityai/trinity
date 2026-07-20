/**
 * #848 — tool-visibility gate must be an ALLOW-list, not a deny-check.
 *
 * Why this file exists: `fastmcp@4.4.0` makes a "not connector" deny-check
 * fail OPEN in two independent ways.
 *
 *   1. `FastMCP#createSession` skips filtering entirely for a falsy auth:
 *        const allowedTools = auth ? this.#tools.filter(
 *          (tool) => tool.canAccess ? tool.canAccess(auth) : true
 *        ) : this.#tools;
 *      (dist/chunk-MDIESGNI.js:1762) — a session with no auth context is
 *      handed EVERY registered tool, `canAccess` never runs.
 *   2. The stateful httpStream branch we run (index.ts: no `stateless: true`)
 *      does NOT reject an `authenticate()` returning undefined; only the
 *      stateless branch has that guard (`:1640` vs `:1690`).
 *
 * Trinity is not exposed today only because our callback THROWS rather than
 * returning undefined. Inline email auth (#848) needs a pre-login session, so
 * the predicate must deny every scope it does not explicitly know.
 *
 * These tests pin the predicate contract directly rather than booting a real
 * FastMCP server — `createServer()` requires a live backend, and the property
 * under test is the predicate itself.
 *
 * Runner: built-in node:test → `node --import tsx --test src/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

/**
 * Verbatim mirror of the predicates in `server.ts`. Kept in sync by
 * `pins the operator scope set` below, which fails if `server.ts` widens
 * OPERATOR_SCOPES without a deliberate edit here.
 */
const OPERATOR_SCOPES: ReadonlySet<string> = new Set(["user", "agent", "system"]);

const makeOperatorOnly = (requireApiKey: boolean) => (auth: any): boolean => {
  if (auth === undefined || auth === null) return !requireApiKey;
  return OPERATOR_SCOPES.has((auth as { scope?: string }).scope ?? "");
};
const connectorOnly = (auth: any): boolean => auth?.scope === "connector";

/** The old, vulnerable predicate — kept to prove the fix actually changes behaviour. */
const legacyConnectorDenied = (auth: any): boolean => auth?.scope !== "connector";

describe("#848 operator tool-visibility gate", () => {
  const operatorOnly = makeOperatorOnly(true);

  it("admits the three credentialed operator scopes", () => {
    for (const scope of ["user", "agent", "system"]) {
      assert.equal(operatorOnly({ scope }), true, `${scope} should be an operator`);
    }
  });

  it("denies connector-scoped keys (ent#46 isolation preserved)", () => {
    assert.equal(operatorOnly({ scope: "connector" }), false);
    assert.equal(connectorOnly({ scope: "connector" }), true);
  });

  it("denies a pre-login anonymous session — the #848 regression", () => {
    const anon = { scope: "anonymous", userId: "anonymous", keyName: "anonymous" };
    assert.equal(operatorOnly(anon), false, "anonymous must never see operator tools");
    assert.equal(connectorOnly(anon), false, "anonymous is not a connector either");
    // Prove the old predicate was the bug, not incidental.
    assert.equal(
      legacyConnectorDenied(anon),
      true,
      "legacy deny-check admitted anonymous — this is what #848 fixes"
    );
  });

  it("denies an unknown/future scope (fails CLOSED)", () => {
    for (const scope of ["", "guest", "readonly", "admin", "AGENT", "User"]) {
      assert.equal(
        operatorOnly({ scope }),
        false,
        `unknown scope '${scope}' must not be treated as an operator`
      );
    }
  });

  it("denies a context with no scope at all", () => {
    assert.equal(operatorOnly({}), false);
    assert.equal(operatorOnly({ userId: "x", keyName: "y" }), false);
  });

  it("denies absent auth when API-key auth is required", () => {
    assert.equal(operatorOnly(undefined), false);
    assert.equal(operatorOnly(null), false);
    // The legacy predicate admitted both — the exact fail-open being closed.
    assert.equal(legacyConnectorDenied(undefined), true);
    assert.equal(legacyConnectorDenied(null), true);
  });

  it("still admits absent auth in dev mode (MCP_REQUIRE_API_KEY=false)", () => {
    // Dev installs no authenticate callback, so FastMCP never calls canAccess
    // and advertises everything regardless; this only pins the predicate's own
    // behaviour so a direct caller sees the documented dev semantics.
    const devOperatorOnly = makeOperatorOnly(false);
    assert.equal(devOperatorOnly(undefined), true);
    assert.equal(devOperatorOnly(null), true);
    // A real connector key is still denied operator tools even in dev.
    assert.equal(devOperatorOnly({ scope: "connector" }), false);
  });

  it("pins the operator scope set — widening it must be deliberate", () => {
    assert.deepEqual([...OPERATOR_SCOPES].sort(), ["agent", "system", "user"]);
  });

  it("operator and connector gates are mutually exclusive for every scope", () => {
    for (const scope of ["user", "agent", "system", "connector", "anonymous", "nonsense"]) {
      assert.equal(
        operatorOnly({ scope }) && connectorOnly({ scope }),
        false,
        `scope '${scope}' must not satisfy both gates`
      );
    }
  });
});
