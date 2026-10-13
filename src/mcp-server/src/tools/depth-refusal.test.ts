/**
 * #2973 — run_agent_loop, trigger_agent_schedule and emit_event render the
 * backend's chain-depth refusal (403 inter_agent_depth_exceeded, #2806) as a
 * result the calling model stops on, the way chat_with_agent does. A 403
 * WITHOUT the code (an access denial) must keep surfacing as an error.
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createLoopTools } from "./loops.js";
import { createScheduleTools } from "./schedules.js";
import { createEventTools } from "./events.js";
import { ApiError, type TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

const DEPTH_BODY = JSON.stringify({
  detail: {
    error: "inter_agent_depth_exceeded",
    depth: 4,
    max_depth: 3,
    caller: "alpha",
    target: "alpha",
    message: "Inter-agent chain depth limit reached (4 > 3). Do not retry this call.",
  },
});
const DENIED_BODY = JSON.stringify({ detail: "Access denied" });

function fakeThrowing(body: string): TrinityClient {
  const boom = async () => {
    throw new ApiError(403, body);
  };
  return {
    getBaseUrl: () => "http://localhost:8000",
    startAgentLoop: boom,
    triggerAgentSchedule: boom,
    emitEvent: boom,
  } as unknown as TrinityClient;
}

const ctx = {
  session: { scope: "agent", agentName: "alpha", userId: "owner", keyId: "k", keyName: "kn" } as unknown as McpAuthContext,
};

type Exec = (p: Record<string, unknown>, c?: { session?: McpAuthContext }) => Promise<string>;

const CASES: Array<[string, (c: TrinityClient) => Exec, Record<string, unknown>]> = [
  ["run_agent_loop", (c) => createLoopTools(c, false).runAgentLoop.execute as Exec, { message: "go", max_runs: 2 }],
  [
    "trigger_agent_schedule",
    (c) => createScheduleTools(c, false).triggerAgentSchedule.execute as Exec,
    { agent_name: "alpha", schedule_id: "s1" },
  ],
  ["emit_event", (c) => createEventTools(c, false).emitEvent.execute as Exec, { event_type: "work.done" }],
];

describe("#2973 depth refusal on loop / schedule-trigger / emit tools", () => {
  for (const [name, make, params] of CASES) {
    it(`${name}: a depth refusal is a non-retryable result`, async () => {
      const out = JSON.parse(await make(fakeThrowing(DEPTH_BODY))(params, ctx));
      assert.equal(out.status, "inter_agent_depth_exceeded");
      assert.equal(out.retryable, false);
      assert.equal(out.depth, 4);
      assert.equal(out.max_depth, 3);
    });

    it(`${name}: a plain 403 is not mistaken for a depth refusal`, async () => {
      let text: string;
      try {
        text = await make(fakeThrowing(DENIED_BODY))(params, ctx);
      } catch (e) {
        text = String(e);
      }
      assert.ok(!text.includes("inter_agent_depth_exceeded"));
      assert.ok(text.includes("403"));
    });
  }
});
