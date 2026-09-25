/**
 * #2958 AC3/AC4: `get_execution_result` passes the execution's
 * `compact_metadata` through, parsed, so a caller that polls a long chat turn
 * can see it was a one-off auto-compaction rather than a degraded agent.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createExecutionTools } from "./executions.js";
import type { TrinityClient } from "../client.js";

const EVENTS = [
  { trigger: "auto", pre_tokens: 173771, post_tokens: 5600, duration_ms: 162585, timestamp: "t" },
];

function tool(compact_metadata: string | null | undefined) {
  const fake: Partial<TrinityClient> = {
    getExecution: async () =>
      ({
        id: "ex_2958",
        agent_name: "target",
        status: "success",
        message: "hi",
        started_at: "t",
        triggered_by: "mcp",
        duration_ms: 173000,
        compact_metadata,
      }) as any,
  };
  return createExecutionTools(fake as unknown as TrinityClient, false).getExecutionResult;
}

async function run(compact_metadata: string | null | undefined) {
  const t: any = tool(compact_metadata);
  return JSON.parse(await t.execute({ agent_name: "target", execution_id: "ex_2958" }, undefined));
}

describe("#2958 get_execution_result carries compact_metadata", () => {
  it("parses the stored JSON string", async () => {
    const out = await run(JSON.stringify(EVENTS));
    assert.deepEqual(out.compact_metadata, EVENTS);
  });

  it("is null when the turn did not compact", async () => {
    assert.equal((await run(null)).compact_metadata, null);
    assert.equal((await run(undefined)).compact_metadata, null);
  });

  it("degrades a malformed value to null instead of failing the read", async () => {
    const out = await run("{not json");
    assert.equal(out.compact_metadata, null);
    assert.equal(out.status, "success");
  });
});
