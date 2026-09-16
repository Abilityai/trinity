/**
 * abilityai/trinity-enterprise#628 — the three loop tools honour the permission edge.
 *
 * `run_agent_loop` is gated at registration (its `TOOL_ACCESS_POLICY` row is `enforce`), so it
 * is driven here through the REAL wrapper with the REAL row — never through the bare tool,
 * which would prove nothing. `get_loop_status` / `stop_loop` are addressed by loop id, so they
 * resolve the loop and gate on its agent inside the tool. Every denial test asserts BOTH the
 * envelope and that the side-effecting client method was never called: an envelope-only
 * assertion passes on a gate that denies after starting the loop.
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createLoopTools } from "./loops.js";
import { createChatTools } from "./chat.js";
import { TOOL_ACCESS_POLICY, withAgentAccess, type ToolAccessPolicy } from "../access.js";
import type { TrinityClient } from "../client.js";
import type { LoopStatus, McpAuthContext } from "../types.js";

type FakeOpts = {
  permitted?: boolean;
  loop?: Partial<LoopStatus> | null;
  statusThrows?: string;
  stopThrows?: string;
};

function makeFake(opts: FakeOpts = {}) {
  const calls: string[] = [];
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    isAgentPermitted: async (a: string, b: string) => {
      calls.push(`perm:${a}->${b}`);
      return opts.permitted ?? false;
    },
    getAgentAccessInfo: async () => ({ name: "x", owner: "owner", is_shared: false }),
    startAgentLoop: async (name: string, data: { max_runs: number }) => {
      calls.push(`start:${name}`);
      return { loop_id: "loop_1", agent_name: name, status: "queued", max_runs: data.max_runs, on_failure: "abort" } as any;
    },
    getLoopStatus: async (id: string) => {
      calls.push(`status:${id}`);
      if (opts.statusThrows) throw new Error(opts.statusThrows);
      const loop =
        opts.loop === undefined
          ? { loop_id: id, agent_name: "bravo-agent", status: "running", runs: [{ run_number: 1 }], last_response: "SECRET-REPLY", total_cost: 0.5 }
          : opts.loop;
      return loop as LoopStatus;
    },
    stopAgentLoop: async (id: string) => {
      calls.push(`stop:${id}`);
      if (opts.stopThrows) throw new Error(opts.stopThrows);
      return { loop_id: id, status: "stopping" };
    },
    chat: async () => ({ response: "ok" }) as any,
    task: async () => ({ status: "accepted", execution_id: "ex_1" }) as any,
  };
  return { fake: fake as unknown as TrinityClient, calls };
}

const session = (scope: string, agentName?: string): { session: McpAuthContext } => ({
  session: { scope, agentName, userId: "owner", keyId: "k1", keyName: "kn" } as unknown as McpAuthContext,
});

/** run_agent_loop exactly as server.ts registers it: the real row, the real wrapper, requireApiKey=false. */
function wrappedRunLoop(fake: TrinityClient) {
  const tools = createLoopTools(fake, false);
  const row = TOOL_ACCESS_POLICY.run_agent_loop;
  assert.equal(row.kind, "enforce");
  return withAgentAccess(
    "run_agent_loop",
    tools.runAgentLoop.execute as (p: Record<string, unknown>, c?: { session?: McpAuthContext }) => Promise<string>,
    row as Extract<ToolAccessPolicy, { kind: "enforce" }>,
    () => fake
  );
}

const START = { message: "Reply with pong", max_runs: 1 };

describe("ent#628 run_agent_loop", () => {
  it("an agent key without an edge is refused and no loop starts — with requireApiKey=false the gate still runs", async () => {
    const { fake, calls } = makeFake({ permitted: false });
    const out = JSON.parse(await wrappedRunLoop(fake)({ agent_name: "bravo-agent", ...START }, session("agent", "alpha")));
    assert.equal(out.success, false);
    assert.equal(out.error, "Access denied");
    assert.match(out.reason, /Permission denied: Agent 'alpha' is not permitted to communicate with 'bravo-agent'/);
    assert.equal(out.loop_id, undefined);
    assert.deepEqual(calls, ["perm:alpha->bravo-agent"]);
  });

  it("an agent key with an edge starts the loop on the target", async () => {
    const { fake, calls } = makeFake({ permitted: true });
    const out = JSON.parse(await wrappedRunLoop(fake)({ agent_name: "bravo-agent", ...START }, session("agent", "alpha")));
    assert.equal(out.success, true);
    assert.equal(out.loop_id, "loop_1");
    assert.deepEqual(calls, ["perm:alpha->bravo-agent", "start:bravo-agent"]);
  });

  it("self — by omission or by name — starts without a permission read", async () => {
    const { fake, calls } = makeFake();
    const run = wrappedRunLoop(fake);
    assert.equal(JSON.parse(await run({ ...START }, session("agent", "alpha"))).success, true);
    assert.equal(JSON.parse(await run({ agent_name: "alpha", ...START }, session("agent", "alpha"))).success, true);
    assert.deepEqual(calls, ["start:alpha", "start:alpha"]);
  });

  it("an unreadable permitted list denies (fail-closed) and starts nothing", async () => {
    // `client.getPermittedAgents` swallows a failed read to [] — modelled here as "not permitted".
    const { fake, calls } = makeFake({ permitted: false });
    const out = JSON.parse(await wrappedRunLoop(fake)({ agent_name: "bravo-agent", ...START }, session("agent", "alpha")));
    assert.equal(out.success, false);
    assert.ok(!calls.some((c) => c.startsWith("start:")));
  });

  it("a user key passes through to the backend, which decides by role and grant", async () => {
    const { fake, calls } = makeFake({ permitted: false });
    const out = JSON.parse(await wrappedRunLoop(fake)({ agent_name: "bravo-agent", ...START }, session("user")));
    assert.equal(out.success, true);
    assert.deepEqual(calls, ["start:bravo-agent"]);
  });

  it("system scope and no session both pass through", async () => {
    const { fake, calls } = makeFake({ permitted: false });
    const run = wrappedRunLoop(fake);
    assert.equal(JSON.parse(await run({ agent_name: "bravo-agent", ...START }, session("system", "trinity-system"))).success, true);
    assert.equal(JSON.parse(await run({ agent_name: "bravo-agent", ...START }, undefined)).success, true);
    assert.deepEqual(calls, ["start:bravo-agent", "start:bravo-agent"]);
  });

  it("the refusal reason is byte-identical to chat_with_agent's for the same caller and target", async () => {
    const { fake } = makeFake({ permitted: false });
    const loopOut = JSON.parse(await wrappedRunLoop(fake)({ agent_name: "bravo-agent", ...START }, session("agent", "alpha")));
    const chat = createChatTools(fake, false, false);
    const chatOut = JSON.parse(
      await chat.chatWithAgent.execute({ agent_name: "bravo-agent", message: "hi" } as any, session("agent", "alpha"))
    );
    assert.equal(chatOut.error, "Access denied");
    assert.equal(loopOut.reason, chatOut.reason);
  });
});

describe("ent#628 get_loop_status resolves, then gates", () => {
  it("a loop on an agent the caller has no edge to is withheld behind a compound uniform reason", async () => {
    const { fake, calls } = makeFake({ permitted: false });
    const tools = createLoopTools(fake, false);
    const text = await tools.getLoopStatus.execute({ loop_id: "loop_1" }, session("agent", "alpha"));
    const out = JSON.parse(text);
    assert.equal(out.success, false);
    assert.equal(out.error, "Access denied");
    assert.equal(out.reason, "Loop 'loop_1' not found or not accessible");
    assert.ok(out.hint, "the denial tells a revoked initiator what to do");
    for (const leak of ["bravo-agent", "SECRET-REPLY", "runs", "total_cost"]) {
      assert.ok(!text.includes(leak), `denial leaks '${leak}'`);
    }
    assert.deepEqual(calls, ["status:loop_1", "perm:alpha->bravo-agent"]);
  });

  it("a loop on the caller itself is returned intact", async () => {
    const { fake } = makeFake({ loop: { loop_id: "loop_1", agent_name: "alpha", status: "running", runs: [{ run_number: 1 }] } });
    const tools = createLoopTools(fake, false);
    const out = JSON.parse(await tools.getLoopStatus.execute({ loop_id: "loop_1" }, session("agent", "alpha")));
    assert.equal(out.success, true);
    assert.equal(out.agent_name, "alpha");
    assert.equal(out.runs.length, 1);
  });

  it("a payload without an agent name is denied — fail-closed, not lucky", async () => {
    const { fake } = makeFake({ loop: { loop_id: "loop_1", status: "running" } });
    const tools = createLoopTools(fake, false);
    const out = JSON.parse(await tools.getLoopStatus.execute({ loop_id: "loop_1" }, session("agent", "alpha")));
    assert.equal(out.success, false);
    assert.equal(out.reason, "Loop 'loop_1' not found or not accessible");
  });

  it("an unknown loop keeps the backend's own error shape", async () => {
    const { fake } = makeFake({ statusThrows: "API error (404): Loop not found" });
    const tools = createLoopTools(fake, false);
    const out = JSON.parse(await tools.getLoopStatus.execute({ loop_id: "loop_x" }, session("agent", "alpha")));
    assert.equal(out.success, false);
    assert.match(out.error, /API error \(404\)/);
  });

  it("a user key reads through — the backend already authorised the fetch", async () => {
    const { fake, calls } = makeFake({ permitted: false });
    const tools = createLoopTools(fake, false);
    const out = JSON.parse(await tools.getLoopStatus.execute({ loop_id: "loop_1" }, session("user")));
    assert.equal(out.success, true);
    assert.deepEqual(calls, ["status:loop_1"]);
  });
});

describe("ent#628 stop_loop resolves, then gates, then stops", () => {
  it("an agent key without an edge is refused and the stop is never sent", async () => {
    const { fake, calls } = makeFake({ permitted: false });
    const tools = createLoopTools(fake, false);
    const out = JSON.parse(await tools.stopLoop.execute({ loop_id: "loop_1" }, session("agent", "alpha")));
    assert.equal(out.success, false);
    assert.equal(out.reason, "Loop 'loop_1' not found or not accessible");
    assert.deepEqual(calls, ["status:loop_1", "perm:alpha->bravo-agent"]);
  });

  it("a permitted caller stops the loop once", async () => {
    const { fake, calls } = makeFake({ permitted: true });
    const tools = createLoopTools(fake, false);
    const out = JSON.parse(await tools.stopLoop.execute({ loop_id: "loop_1" }, session("agent", "alpha")));
    assert.equal(out.success, true);
    assert.equal(out.status, "stopping");
    assert.deepEqual(calls, ["status:loop_1", "perm:alpha->bravo-agent", "stop:loop_1"]);
  });

  it("a failed resolve sends no stop and names the escape hatch", async () => {
    const { fake, calls } = makeFake({ statusThrows: "API error (502): Bad Gateway" });
    const tools = createLoopTools(fake, false);
    const out = JSON.parse(await tools.stopLoop.execute({ loop_id: "loop_1" }, session("agent", "alpha")));
    assert.equal(out.success, false);
    assert.match(out.error, /NOT sent/);
    assert.match(out.error, /POST \/api\/loops\/loop_1\/stop/);
    assert.deepEqual(calls, ["status:loop_1"]);
  });

  it("a user key stops through — the backend decides", async () => {
    const { fake, calls } = makeFake({ permitted: false });
    const tools = createLoopTools(fake, false);
    const out = JSON.parse(await tools.stopLoop.execute({ loop_id: "loop_1" }, session("user")));
    assert.equal(out.success, true);
    assert.deepEqual(calls, ["status:loop_1", "stop:loop_1"]);
  });
});
