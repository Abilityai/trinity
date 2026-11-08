/**
 * #4281 slice E: `get_execution_result` passes the backend's turn-tier
 * refusal reading through, so a caller polling a scheduled run can tell a
 * clean `success` from one the turn-tier hook refused.
 *
 * The values are the backend's (`services/execution_refusals.py`); this tool
 * only forwards them. A `0` must stay `0` (not collapse to null through a
 * truthiness check), and a null count must keep its marker.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createExecutionTools } from "./executions.js";
import type { TrinityClient } from "../client.js";

function tool(fields: Record<string, unknown>) {
  const fake: Partial<TrinityClient> = {
    getExecution: async () =>
      ({
        id: "ex_4281",
        schedule_id: "sched-1",
        agent_name: "target",
        status: "success",
        message: "brief",
        started_at: "t",
        triggered_by: "schedule",
        ...fields,
      }) as any,
  };
  return createExecutionTools(fake as unknown as TrinityClient, false).getExecutionResult;
}

async function run(fields: Record<string, unknown>) {
  const t: any = tool(fields);
  return JSON.parse(await t.execute({ agent_name: "target", execution_id: "ex_4281" }, undefined));
}

describe("#4281 get_execution_result carries the refusal reading", () => {
  it("passes a refused run's count through", async () => {
    const out = await run({ guardrail_refusals: 7, guardrail_reporting: null });
    assert.equal(out.guardrail_refusals, 7);
    assert.equal(out.guardrail_reporting, null);
  });

  it("keeps a complete zero as 0, not null", async () => {
    const out = await run({ guardrail_refusals: 0, guardrail_reporting: null });
    assert.equal(out.guardrail_refusals, 0);
  });

  it("keeps an unknown count's reason", async () => {
    const out = await run({ guardrail_refusals: null, guardrail_reporting: "not_claude_code" });
    assert.equal(out.guardrail_refusals, null);
    assert.equal(out.guardrail_reporting, "not_claude_code");
  });

  it("reads null for both from a backend that predates the fields", async () => {
    const out = await run({});
    assert.equal(out.guardrail_refusals, null);
    assert.equal(out.guardrail_reporting, null);
  });
});
