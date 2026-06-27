/**
 * #905 — audit-wrapper target_id + request_id capture.
 *
 * Pins the centralized behavior added to `withAudit`/`logToolCall`:
 *   - target_id resolves from params.agent_name, falls back to params.name,
 *     and is undefined for non-agent tools (the 60-tool guard — every wrapped
 *     tool now attributes its target, none crash on a param-less call).
 *   - request_id is read from the shared context AFTER execute, so a tool that
 *     stamps context.requestId (git.ts) gets it onto its own mcp_operation row.
 *
 * No backend is touched: postAudit no-ops without INTERNAL_API_SECRET, so we
 * assert on `resolveTargetId` (pure) and drive `withAudit` to prove it reads
 * the post-execute context without throwing.
 *
 * Runner: node:test → `node --import tsx --test src/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { resolveTargetId, withAudit, type ToolCallContext } from "./audit.js";

describe("#905 resolveTargetId", () => {
  it("prefers agent_name", () => {
    assert.equal(resolveTargetId({ agent_name: "alpha", name: "beta" }), "alpha");
  });

  it("falls back to name", () => {
    assert.equal(resolveTargetId({ name: "beta" }), "beta");
  });

  it("is undefined for non-agent params (60-tool guard)", () => {
    assert.equal(resolveTargetId({ limit: 10 }), undefined);
    assert.equal(resolveTargetId(undefined), undefined);
    assert.equal(resolveTargetId("not-an-object"), undefined);
    assert.equal(resolveTargetId({ agent_name: 5 }), undefined);
  });
});

describe("#905 withAudit request_id read-after-execute", () => {
  it("does not throw and returns the tool result when a tool stamps context.requestId", async () => {
    const wrapped = withAudit<{ agent_name: string }>("git_sync", async (_params, context) => {
      // Mirror git.ts: stamp the shared context before doing work.
      if (context) context.requestId = "rid-123";
      return JSON.stringify({ ok: true });
    });

    const context: ToolCallContext = {};
    const out = await wrapped({ agent_name: "alpha" }, context);
    assert.equal(out, JSON.stringify({ ok: true }));
    // The wrapper reads requestId from the same object the tool mutated.
    assert.equal(context.requestId, "rid-123");
  });

  it("propagates the original error (audit on failure path never swallows)", async () => {
    const wrapped = withAudit<{ agent_name: string }>("git_sync", async () => {
      throw new Error("boom");
    });
    await assert.rejects(() => wrapped({ agent_name: "alpha" }, {}), /boom/);
  });
});
