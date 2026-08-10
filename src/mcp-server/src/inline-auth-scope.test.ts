/**
 * #2035 condition 3 — the memo is structurally unreachable from the keyed path.
 *
 * "Memoization strictly scoped to the anonymous tier — never consulted when an
 * Authorization header is present. This line is load-bearing and needs its own
 * test." (@vybe, #2035.)
 *
 * The transport suite tests the CONSEQUENCE (a keyed session still re-validates
 * its credential on every request). This file tests the CAUSE, which is the
 * stronger statement: the store is called from exactly one place, and that place
 * is inside the branch taken when no `Authorization` header was sent. A
 * behavioural test can only sample the paths it happens to drive; a source guard
 * fails the moment a second call site appears anywhere, including on a path no
 * test exercises.
 *
 * Source-shape guards are used elsewhere in Trinity for the same reason — see
 * `tests/unit/test_ent109_git_env_seam.py`, which pins the writer SET rather
 * than any single writer, because the bug it prevents was two writers with one
 * of them wrong.
 *
 * If this file fails after a refactor, do not delete the assertion to make it
 * pass. Either keep the single call site inside the no-credential branch, or
 * re-derive the guarantee and rewrite the guard to match the new shape.
 */
import { strict as assert } from "node:assert";
import { describe, it } from "node:test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const SERVER_SOURCE = readFileSync(
  fileURLToPath(new URL("./server.ts", import.meta.url)),
  "utf8"
);

/** The store call — the single point where a memoized context can be handed out. */
const RESOLVE_CALL = "anonymousSessions.resolve(";

/** The guard on the branch that runs only when no credential was presented. */
const NO_CREDENTIAL_GUARD = 'if (!authHeader || !authHeader.startsWith("Bearer "))';

/** Text of the block opened by the first `{` at or after `from`, by brace balance. */
function blockAfter(source: string, from: number): string {
  const open = source.indexOf("{", from);
  assert.notEqual(open, -1, "no block found");
  let depth = 0;
  for (let i = open; i < source.length; i++) {
    const c = source[i];
    if (c === "{") depth++;
    else if (c === "}") {
      depth--;
      if (depth === 0) return source.slice(open, i + 1);
    }
  }
  assert.fail("unbalanced block");
}

describe("#2035 the anonymous memo is scoped to the anonymous tier", () => {
  it("is consulted from exactly one place", () => {
    const occurrences = SERVER_SOURCE.split(RESOLVE_CALL).length - 1;
    assert.equal(
      occurrences,
      1,
      `expected exactly one \`${RESOLVE_CALL}\` call site, found ${occurrences}. ` +
        `A second one is how a keyed request starts being served from the memo.`
    );
  });

  it("is consulted only inside the no-Authorization-header branch", () => {
    const guardAt = SERVER_SOURCE.indexOf(NO_CREDENTIAL_GUARD);
    assert.notEqual(
      guardAt,
      -1,
      "could not find the no-credential branch guard — if the header check was " +
        "reworded, re-derive this guard against the new shape rather than deleting it"
    );
    const branch = blockAfter(SERVER_SOURCE, guardAt);
    assert.ok(
      branch.includes(RESOLVE_CALL),
      "the memo is no longer resolved inside the no-Authorization-header branch"
    );
  });

  it("is not consulted after the key is read", () => {
    // Belt for the brace scan: whatever the block structure, the single call
    // site must sit ahead of the point where a presented credential is taken
    // off the header. Anything after that line has, by definition, a key.
    const resolveAt = SERVER_SOURCE.indexOf(RESOLVE_CALL);
    const apiKeyAt = SERVER_SOURCE.indexOf("const apiKey = authHeader.substring(7)");
    assert.notEqual(apiKeyAt, -1, "could not find where the API key is read");
    assert.ok(
      resolveAt < apiKeyAt,
      "the memo is resolved on a path that has already accepted a credential"
    );
  });

  it("does not memoize the keyed context object", () => {
    // The keyed branch must build its context inline and hand it straight back.
    // If it were ever routed through a store, `Mcp-Session-Id` alone would
    // stand in for a key that was never presented.
    const keyedContextAt = SERVER_SOURCE.indexOf("const authContext: McpAuthContext = {");
    assert.notEqual(keyedContextAt, -1, "could not find the keyed context construction");
    const afterKeyed = SERVER_SOURCE.slice(keyedContextAt);
    assert.ok(
      !afterKeyed.includes(RESOLVE_CALL),
      "the keyed branch reaches the anonymous store"
    );
  });
});
