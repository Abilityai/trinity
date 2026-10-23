/**
 * #3296 — an identical delegation from a LATER turn is a new delegation.
 *
 * `chat_with_agent` derives its Idempotency-Key from caller, target, route,
 * model, mode and message. The caller's turn was not part of it, so the same
 * text sent from a different turn within 24 h replayed the FIRST run's receipt:
 * the second turn's request never ran, and its report went to the first turn's
 * conversation while the receipt said `report_back: "requested"`.
 *
 * Now the key carries the turn — the platform-stamped one (#2392), never the
 * model-typed `execution_id` (Invariant #18: a re-send that only changes
 * report-back must keep its key, or it runs the work twice) — so:
 *   - same text, same turn  → same key (a transport retry still replays);
 *   - same text, other turn → a different key (a new run that reports here);
 *   - no turn at all        → the key is byte-identical to before (nothing in
 *     flight is invalidated for user keys and manual sessions);
 * and a replayed receipt says it repeats an earlier run instead of reading as
 * a fresh report-back request.
 *
 * Runner: built-in node:test → `node --import tsx --test src/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createChatTools, deriveMcpIdempotencyKey, REPORT_BACK_DEFAULT_NOTE } from "./tools/chat.js";
import type { TrinityClient } from "./client.js";

type Call = { idempotencyKey?: string; options?: any };

function makeTool(calls: Call[], receipt: Record<string, unknown> = {}) {
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    isAgentPermitted: async () => true,
    getAgentAccessInfo: async () => ({ owner: "u1", is_shared: true }) as any,
    chat: async (_n: string, _m: string, _s?: string, _k?: any, idempotencyKey?: string) => {
      calls.push({ idempotencyKey });
      return { response: "reply" } as any;
    },
    task: async (name: string, _m: string, options?: any, _s?: string, _k?: any, idempotencyKey?: string) => {
      calls.push({ idempotencyKey, options });
      return { status: "accepted", execution_id: "ex_1", agent_name: name, async_mode: true, ...receipt } as any;
    },
  };
  return createChatTools(fake as unknown as TrinityClient, false, false).chatWithAgent;
}

const agentTurn = (executionId?: string) => ({
  session: { scope: "agent", agentName: "caller", userId: "u1", keyId: "k1", keyName: "kn", executionId } as any,
});

async function run(tool: any, args: Record<string, unknown>, ctx: any) {
  return JSON.parse(await tool.execute(args, ctx));
}

// The Slack-thread example from the issue: an async parallel delegation.
const DELEGATION = { agent_name: "target", message: "summarise yesterday's incidents", parallel: true, async: true };

describe("#3296 the delegation key is scoped to the caller's turn", () => {
  it("same text from the SAME turn keeps one key (a retry still replays)", async () => {
    const calls: Call[] = [];
    const tool = makeTool(calls);
    await run(tool, DELEGATION, agentTurn("exec-turn-a"));
    await run(tool, DELEGATION, agentTurn("exec-turn-a"));
    assert.ok(calls[0].idempotencyKey);
    assert.equal(calls[0].idempotencyKey, calls[1].idempotencyKey);
  });

  it("same text from a DIFFERENT turn gets a different key (a new run)", async () => {
    const calls: Call[] = [];
    const tool = makeTool(calls);
    await run(tool, DELEGATION, agentTurn("exec-turn-a"));
    await run(tool, DELEGATION, agentTurn("exec-turn-b"));
    assert.notEqual(calls[0].idempotencyKey, calls[1].idempotencyKey);
    // ...and each run is asked to report into its own turn.
    assert.equal(calls[0].options?.parent_execution_id, "exec-turn-a");
    assert.equal(calls[1].options?.parent_execution_id, "exec-turn-b");
  });

  it("the sequential /chat route is scoped the same way", async () => {
    const calls: Call[] = [];
    const tool = makeTool(calls);
    const args = { agent_name: "target", message: "same", parallel: false };
    await run(tool, args, agentTurn("exec-turn-a"));
    await run(tool, args, agentTurn("exec-turn-b"));
    assert.notEqual(calls[0].idempotencyKey, calls[1].idempotencyKey);
  });

  it("with no turn the key is byte-identical to the pre-#3296 derivation", async () => {
    for (const executionId of [undefined, "manual"]) {
      const calls: Call[] = [];
      await run(makeTool(calls), DELEGATION, agentTurn(executionId));
      assert.equal(
        calls[0].idempotencyKey,
        deriveMcpIdempotencyKey(["caller", "target", "task", undefined, "async", DELEGATION.message]),
        `executionId=${executionId}`,
      );
    }
  });

  it("a typed id never changes the key (Invariant #18: report-back alone is not a new delegation)", async () => {
    for (const executionId of [undefined, "exec-turn-a"]) {
      const calls: Call[] = [];
      const tool = makeTool(calls);
      await run(tool, { ...DELEGATION, execution_id: "exec-typed-a" }, agentTurn(executionId));
      await run(tool, { ...DELEGATION, execution_id: "exec-typed-b" }, agentTurn(executionId));
      await run(tool, DELEGATION, agentTurn(executionId));
      assert.equal(calls[0].idempotencyKey, calls[1].idempotencyKey, `header=${executionId}`);
      assert.equal(calls[0].idempotencyKey, calls[2].idempotencyKey, `header=${executionId}`);
    }
  });

  it("the header turn wins over a typed id (a stale typed id cannot split one turn)", async () => {
    const calls: Call[] = [];
    const tool = makeTool(calls);
    await run(tool, { ...DELEGATION, execution_id: "exec-stale-1" }, agentTurn("exec-turn-a"));
    await run(tool, { ...DELEGATION, execution_id: "exec-stale-2" }, agentTurn("exec-turn-a"));
    assert.equal(calls[0].idempotencyKey, calls[1].idempotencyKey);
  });
});

describe("#3296 a replayed receipt says it is a replay", () => {
  it("keeps idempotent_replay and does not read as a fresh report-back request", async () => {
    const out = await run(makeTool([], { idempotent_replay: true }), DELEGATION, agentTurn("exec-turn-a"));
    assert.equal(out.idempotent_replay, true);
    assert.notEqual(out.report_back_note, REPORT_BACK_DEFAULT_NOTE);
    assert.match(out.report_back_note, /earlier/i);
    assert.match(out.report_back_note, /no new run/i);
  });

  it("a fresh receipt keeps the default report-back note", async () => {
    const fresh = await run(makeTool([]), DELEGATION, agentTurn("exec-turn-a"));
    const replay = await run(makeTool([], { idempotent_replay: true }), DELEGATION, agentTurn("exec-turn-a"));
    assert.equal(fresh.report_back, "requested");
    assert.equal(fresh.idempotent_replay, undefined);
    assert.notEqual(fresh.report_back_note, replay.report_back_note);
  });
});
