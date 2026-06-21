/**
 * #946 — agent→agent chat pull-routing fork.
 *
 * Pins the MCP-layer routing decision in `chat_with_agent`:
 *   - scope='user' (any flag)            → apiClient.chat()  (CRITICAL regression)
 *   - flag OFF, agent→agent              → apiClient.chat()  (CRITICAL fallback)
 *   - flag ON, agent→agent, non-self     → apiClient.task(async_mode=true)
 *   - flag ON, self-task                 → apiClient.chat()  (interactive contract)
 *   - parallel=true                      → apiClient.task()  (unchanged)
 * and the D8 dispatch-mode idempotency key:
 *   - transport retry WITHIN one mode    → identical key (dedupes)
 *   - flag flip across modes (same call) → DISTINCT keys (no stale-shape replay)
 *
 * Drives the real tool `execute()` with a fake TrinityClient (requireApiKey=false
 * → getClient() returns the base/fake client directly, same seam as
 * operator_queue.test.ts). The fake records which method ran and the
 * Idempotency-Key it was handed.
 *
 * Runner: built-in node:test → `node --import tsx --test src/*.test.ts`.
 */
import { describe, it, beforeEach } from "node:test";
import { strict as assert } from "node:assert";

import { createChatTools } from "./tools/chat.js";
import type { TrinityClient } from "./client.js";

type Recorded = { method: "chat" | "task"; idempotencyKey?: string; options?: any };

function makeTool(pullEnabled: boolean, calls: Recorded[]) {
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    // Agent→agent permission gate: allow any non-self target so routing is reached.
    isAgentPermitted: async () => true,
    // User-scope access gate: same owner ("u1") → allowed.
    getAgentAccessInfo: async () => ({ owner: "u1", is_shared: true }) as any,
    chat: async (
      _name: string,
      _message: string,
      _sourceAgent?: string,
      _mcpKeyInfo?: any,
      idempotencyKey?: string,
    ) => {
      calls.push({ method: "chat", idempotencyKey });
      return { response: "sync chat reply" } as any;
    },
    task: async (
      _name: string,
      _message: string,
      options?: any,
      _sourceAgent?: string,
      _mcpKeyInfo?: any,
      idempotencyKey?: string,
    ) => {
      calls.push({ method: "task", idempotencyKey, options });
      return { status: "accepted", execution_id: "ex_1", agent_name: _name, async_mode: true } as any;
    },
  };
  const tools = createChatTools(fake as unknown as TrinityClient, false, pullEnabled);
  return tools.chatWithAgent;
}

const agentSession = (agentName: string) => ({
  session: { scope: "agent", agentName, userId: "u1", keyId: "k1", keyName: "kn" } as any,
});
const userSession = () => ({
  session: { scope: "user", userId: "u1", keyId: "k1", keyName: "kn" } as any,
});

async function run(tool: any, args: Record<string, unknown>, ctx: any) {
  return JSON.parse(await tool.execute(args, ctx));
}

describe("#946 chat_with_agent pull routing", () => {
  let calls: Recorded[];
  beforeEach(() => {
    calls = [];
  });

  it("scope=user keeps synchronous chat() even with the flag ON (CRITICAL regression)", async () => {
    const tool = makeTool(true, calls);
    await run(tool, { agent_name: "target", message: "hi", parallel: false }, userSession());
    assert.equal(calls.length, 1);
    assert.equal(calls[0].method, "chat");
  });

  it("flag OFF keeps agent→agent on synchronous chat() (CRITICAL fallback)", async () => {
    const tool = makeTool(false, calls);
    await run(tool, { agent_name: "target", message: "hi", parallel: false }, agentSession("caller"));
    assert.equal(calls.length, 1);
    assert.equal(calls[0].method, "chat");
  });

  it("flag ON + agent→agent + non-self routes to async task()", async () => {
    const tool = makeTool(true, calls);
    const out = await run(tool, { agent_name: "target", message: "hi", parallel: false }, agentSession("caller"));
    assert.equal(calls.length, 1);
    assert.equal(calls[0].method, "task");
    assert.equal(calls[0].options?.async_mode, true);
    // Receipt is returned verbatim for the caller to poll.
    assert.equal(out.status, "accepted");
    assert.equal(out.execution_id, "ex_1");
  });

  it("flag ON self-task stays on synchronous chat() (interactive inject_result contract)", async () => {
    const tool = makeTool(true, calls);
    // caller === agent_name → self-task
    await run(tool, { agent_name: "caller", message: "hi", parallel: false }, agentSession("caller"));
    assert.equal(calls.length, 1);
    assert.equal(calls[0].method, "chat");
  });

  it("parallel=true is unchanged (always task) regardless of the flag", async () => {
    const tool = makeTool(true, calls);
    await run(tool, { agent_name: "target", message: "hi", parallel: true }, agentSession("caller"));
    assert.equal(calls.length, 1);
    assert.equal(calls[0].method, "task");
  });
});

describe("#946 D8 dispatch-mode idempotency key", () => {
  it("a transport retry within ONE mode reuses the same key (dedupes)", async () => {
    const calls: Recorded[] = [];
    const tool = makeTool(true, calls);
    const args = { agent_name: "target", message: "same task", parallel: false };
    await run(tool, args, agentSession("caller"));
    await run(tool, args, agentSession("caller"));
    assert.equal(calls.length, 2);
    assert.equal(calls[0].method, "task");
    assert.equal(calls[1].method, "task");
    assert.ok(calls[0].idempotencyKey);
    assert.equal(calls[0].idempotencyKey, calls[1].idempotencyKey, "same mode → same key");
  });

  it("the SAME call under flag OFF vs ON yields DISTINCT keys (no stale-shape replay)", async () => {
    const offCalls: Recorded[] = [];
    const onCalls: Recorded[] = [];
    const args = { agent_name: "target", message: "same task", parallel: false };
    await run(makeTool(false, offCalls), args, agentSession("caller")); // → chat (sync)
    await run(makeTool(true, onCalls), args, agentSession("caller"));    // → task (pull)
    assert.equal(offCalls[0].method, "chat");
    assert.equal(onCalls[0].method, "task");
    assert.ok(offCalls[0].idempotencyKey && onCalls[0].idempotencyKey);
    assert.notEqual(
      offCalls[0].idempotencyKey,
      onCalls[0].idempotencyKey,
      "flag flip must change the key so the backend can't replay a wrong-shape snapshot",
    );
  });

  it("scope=user key is unchanged across flag states (no in-flight key invalidation)", async () => {
    const offCalls: Recorded[] = [];
    const onCalls: Recorded[] = [];
    const args = { agent_name: "target", message: "same task", parallel: false };
    await run(makeTool(false, offCalls), args, userSession());
    await run(makeTool(true, onCalls), args, userSession());
    assert.equal(offCalls[0].method, "chat");
    assert.equal(onCalls[0].method, "chat");
    assert.equal(
      offCalls[0].idempotencyKey,
      onCalls[0].idempotencyKey,
      "scope=user never pull-routes, so its key must be identical across flag states",
    );
  });
});
