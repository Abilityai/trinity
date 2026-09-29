/**
 * Tests for #1101 — operator-queue agent-scope post-filter.
 *
 * Pins `filterQueueItemsForAgentScope` so a future edit can't silently widen
 * what an agent-scoped key sees in a broad `list_operator_queue` call (the
 * load-bearing gate: the backend filters to the KEY OWNER's accessible agents,
 * so the MCP layer is the only place agent_permissions are enforced).
 *
 * Runner: built-in `node:test`. No new devDependency. Run via:
 *   node --import tsx --test src/*.test.ts
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import {
  filterQueueItemsForAgentScope,
  createOperatorQueueTools,
} from "./tools/operator_queue.js";
import { ApiError, type TrinityClient } from "./client.js";
import type { OperatorAskCreate } from "./types.js";

type Item = { id: string; agent_name: string };

const items: Item[] = [
  { id: "a", agent_name: "self" },
  { id: "b", agent_name: "friend" },
  { id: "c", agent_name: "stranger" },
  { id: "d", agent_name: "self" },
];

describe("#1101 filterQueueItemsForAgentScope", () => {
  it("keeps only items whose agent is in the allowed set (self + permitted)", () => {
    const out = filterQueueItemsForAgentScope(items, new Set(["self", "friend"]));
    assert.deepEqual(out.map((i) => i.id), ["a", "b", "d"]);
  });

  it("a self-only allowed set keeps just the caller's own items", () => {
    const out = filterQueueItemsForAgentScope(items, new Set(["self"]));
    assert.deepEqual(out.map((i) => i.id), ["a", "d"]);
  });

  it("drops every item whose agent is not permitted", () => {
    const out = filterQueueItemsForAgentScope(items, new Set(["nobody"]));
    assert.deepEqual(out, []);
  });

  it("an empty allowed set drops everything", () => {
    assert.deepEqual(filterQueueItemsForAgentScope(items, new Set<string>()), []);
  });

  it("an empty item list returns empty", () => {
    assert.deepEqual(filterQueueItemsForAgentScope([], new Set(["self"])), []);
  });

  it("is a pure filter — does not mutate the input array", () => {
    const snapshot = items.map((i) => ({ ...i }));
    filterQueueItemsForAgentScope(items, new Set(["self"]));
    assert.deepEqual(items, snapshot);
  });

  it("is order-preserving", () => {
    const out = filterQueueItemsForAgentScope(items, new Set(["self", "stranger"]));
    assert.deepEqual(out.map((i) => i.id), ["a", "c", "d"]);
  });
});

// ---------------------------------------------------------------------------
// #1104 — respond_to_operator_queue access gate + proxy behavior.
// Builds the tool with requireApiKey=false so getClient() returns our fake
// client directly. The crux being pinned: an agent-scoped key may NOT resolve
// a non-permitted agent's item, and on denial the write is never attempted.
// ---------------------------------------------------------------------------

function makeRespondTool(fake: Partial<TrinityClient>) {
  const tools = createOperatorQueueTools(fake as unknown as TrinityClient, false);
  return tools.respondToOperatorQueue;
}

const agentCtx = (agentName: string) => ({
  session: { scope: "agent", agentName } as any,
});

describe("#1104 respond_to_operator_queue", () => {
  it("denies an agent-scoped key resolving a non-permitted agent's item — and never writes", async () => {
    let responded = false;
    const tool = makeRespondTool({
      getOperatorQueueItem: async () => ({ agent_name: "stranger" }) as any,
      getPermittedAgents: async () => [],
      respondToOperatorQueueItem: async () => {
        responded = true;
        return {} as any;
      },
    });

    const out = JSON.parse(
      await tool.execute(
        { item_id: "x", response: "approve" },
        agentCtx("self"),
      ),
    );

    assert.equal(out.error, "Access denied");
    assert.equal(responded, false, "respond must not be called when access is denied");
  });

  it("allows an agent to resolve its own item and proxies the response", async () => {
    const calls: Array<{ id: string; body: any }> = [];
    const tool = makeRespondTool({
      getOperatorQueueItem: async () => ({ agent_name: "self" }) as any,
      getPermittedAgents: async () => [],
      respondToOperatorQueueItem: async (id: string, body: any) => {
        calls.push({ id, body });
        return { id, status: "responded", agent_name: "self" } as any;
      },
    });

    const out = JSON.parse(
      await tool.execute(
        { item_id: "item1", response: "approve", response_text: "ok" },
        agentCtx("self"),
      ),
    );

    assert.equal(out.status, "responded");
    assert.deepEqual(calls, [
      { id: "item1", body: { response: "approve", response_text: "ok" } },
    ]);
  });

  it("allows resolving a permitted (non-self) agent's item", async () => {
    let responded = false;
    const tool = makeRespondTool({
      getOperatorQueueItem: async () => ({ agent_name: "friend" }) as any,
      getPermittedAgents: async () => ["friend"],
      respondToOperatorQueueItem: async () => {
        responded = true;
        return { status: "responded" } as any;
      },
    });

    const out = JSON.parse(
      await tool.execute({ item_id: "y", response: "deny" }, agentCtx("self")),
    );

    assert.equal(out.status, "responded");
    assert.equal(responded, true);
  });

  it("surfaces a backend 400 (non-pending item) as a structured error, not a throw", async () => {
    const tool = makeRespondTool({
      getOperatorQueueItem: async () => ({ agent_name: "self" }) as any,
      getPermittedAgents: async () => [],
      respondToOperatorQueueItem: async () => {
        throw new Error("API error (400): Cannot respond to item with status 'responded'");
      },
    });

    const out = JSON.parse(
      await tool.execute({ item_id: "z", response: "approve" }, agentCtx("self")),
    );

    assert.match(out.error, /400/);
    assert.match(out.error, /Cannot respond/);
  });
});

// ---------------------------------------------------------------------------
// trinity-enterprise#611 — get_my_ask: an agent reads back its OWN ask by the
// request_id it chose. Self-acting: the identity comes from the key
// (`resolveActingAgent`), so there is no agent parameter to spoof.
// ---------------------------------------------------------------------------

function makeGetMyAsk(fake: Partial<TrinityClient>) {
  return createOperatorQueueTools(fake as unknown as TrinityClient, false).getMyAsk;
}

function recordingClient(result: unknown = { request_id: "r-1", disposition: "cancelled" }) {
  const calls: Array<[string, string]> = [];
  const fake = {
    getMyAsk: async (agentName: string, requestId: string) => {
      calls.push([agentName, requestId]);
      return result;
    },
  } as Partial<TrinityClient>;
  return { fake, calls };
}

describe("trinity-enterprise#611 get_my_ask", () => {
  it("reads the calling agent's own ask — the agent comes from the key", async () => {
    const { fake, calls } = recordingClient();
    const out = JSON.parse(await makeGetMyAsk(fake).execute({ request_id: "r-1" }, agentCtx("self")));
    assert.deepEqual(calls, [["self", "r-1"]]);
    assert.equal(out.disposition, "cancelled");
  });

  it("declares no agent-target parameter — there is nothing to spoof", () => {
    const tool = makeGetMyAsk(recordingClient().fake);
    assert.deepEqual(Object.keys((tool.parameters as any).shape), ["request_id"]);
  });

  it("the system key reads as the agent it was minted for", async () => {
    const { fake, calls } = recordingClient();
    await makeGetMyAsk(fake).execute(
      { request_id: "r-1" },
      { session: { scope: "system", agentName: "trinity-system" } as any },
    );
    assert.deepEqual(calls, [["trinity-system", "r-1"]]);
  });

  for (const session of [
    { scope: "user" },
    { scope: "connector", agentName: "self" },
    { scope: "system" },
  ]) {
    it(`a ${session.scope}-scoped key${"agentName" in session ? " naming an agent" : ""} is refused before any backend call`, async () => {
      const { fake, calls } = recordingClient();
      const out = JSON.parse(await makeGetMyAsk(fake).execute({ request_id: "r-1" }, { session: session as any }));
      assert.equal(out.success, false);
      assert.match(out.error, /agent identity/);
      assert.deepEqual(calls, []);
    });
  }

  it("a backend refusal or a missing ask comes back as a structured error, not a throw", async () => {
    const fake = {
      getMyAsk: async () => {
        throw new Error("API error (404): Ask not found");
      },
    } as Partial<TrinityClient>;
    const out = JSON.parse(await makeGetMyAsk(fake).execute({ request_id: "nope" }, agentCtx("self")));
    assert.match(out.error, /Ask not found/);
  });

  it("the description teaches the rider and the readback's reach", () => {
    const { description } = makeGetMyAsk(recordingClient().fake);
    assert.match(description, /request_id/);
    assert.match(description, /do not re-ask the same action without new information/);
    assert.match(description, /disposition/);
  });
});

describe("trinity-enterprise#611 respond_to_operator_queue is person-only", () => {
  it("the description says an agent's key cannot end an ask", () => {
    const tool = createOperatorQueueTools({} as unknown as TrinityClient, false).respondToOperatorQueue;
    assert.match(tool.description, /user-scoped/);
    assert.match(tool.description, /agent- and system-scoped keys are refused/);
  });
});

// ---------------------------------------------------------------------------
// trinity-enterprise#611 — ask_operator: an agent raises an ask as ITSELF.
// Self-acting like get_my_ask: the agent comes from the key, there is no agent
// parameter, and a backend refusal comes back as its named code, never a throw.
// ---------------------------------------------------------------------------

function makeAskOperator(fake: Partial<TrinityClient>) {
  return createOperatorQueueTools(fake as unknown as TrinityClient, false).askOperator;
}

function raisingClient(result: unknown = { status: "created", request_id: "deploy-1", to_role: "primary" }) {
  const calls: Array<[string, OperatorAskCreate]> = [];
  const fake = {
    raiseAsk: async (agentName: string, body: OperatorAskCreate) => {
      calls.push([agentName, body]);
      return result;
    },
  } as Partial<TrinityClient>;
  return { fake, calls };
}

const ASK = {
  request_id: "deploy-1",
  title: "Deploy the release?",
  type: "approval",
  options: ["approve", "reject"],
  proposal: { release: "v2" },
};

describe("trinity-enterprise#611 ask_operator", () => {
  it("raises the ask as the calling agent — the agent comes from the key", async () => {
    const { fake, calls } = raisingClient();
    const out = JSON.parse(await makeAskOperator(fake).execute(ASK as any, agentCtx("self")));
    assert.deepEqual(calls, [["self", ASK]]);
    assert.equal(out.status, "created");
  });

  it("forwards only the declared fields, whatever else the arguments carry", async () => {
    const { fake, calls } = raisingClient();
    const stray = { ...ASK, agent_name: "sibling", channel: "file", raised_by: "gate" };
    await makeAskOperator(fake).execute(stray as any, agentCtx("self"));
    assert.deepEqual(calls, [["self", ASK]]);
  });

  it("declares no agent-target parameter — there is nothing to spoof", () => {
    const tool = makeAskOperator(raisingClient().fake);
    const keys = Object.keys((tool.parameters as any).shape);
    assert.ok(!keys.some((k) => /agent/.test(k)), `unexpected agent-shaped parameter: ${keys}`);
    assert.deepEqual(keys.sort(), [
      "context", "expires_at", "options", "priority", "proposal", "question",
      "request_id", "supersedes_expired", "title", "to", "type",
    ]);
  });

  it("the system key raises as the agent it was minted for", async () => {
    const { fake, calls } = raisingClient();
    await makeAskOperator(fake).execute(
      ASK as any,
      { session: { scope: "system", agentName: "trinity-system" } as any },
    );
    assert.deepEqual(calls.map((c) => c[0]), ["trinity-system"]);
  });

  for (const session of [
    { scope: "user" },
    { scope: "connector", agentName: "self" },
    { scope: "system" },
  ]) {
    it(`a ${session.scope}-scoped key${"agentName" in session ? " naming an agent" : ""} is refused before any backend call`, async () => {
      const { fake, calls } = raisingClient();
      const out = JSON.parse(await makeAskOperator(fake).execute(ASK as any, { session: session as any }));
      assert.equal(out.success, false);
      assert.match(out.error, /agent identity/);
      assert.deepEqual(calls, []);
    });
  }

  for (const [status, detail] of [
    [422, { code: "reask_requires_link", message: "Link the expired ask.", expired_request_id: "deploy-0" }],
    [429, { code: "queue_full", message: "Too many open asks." }],
  ] as const) {
    it(`a ${status} refusal comes back as its named code and extras, not a throw`, async () => {
      const fake = {
        raiseAsk: async () => {
          throw new ApiError(status, JSON.stringify({ detail }));
        },
      } as Partial<TrinityClient>;
      const out = JSON.parse(await makeAskOperator(fake).execute(ASK as any, agentCtx("self")));
      assert.deepEqual(out, { success: false, status, ...detail });
    });
  }

  it("a validation refusal without a code, or a non-API failure, still comes back structured", async () => {
    const unnamed = {
      raiseAsk: async () => {
        throw new ApiError(422, JSON.stringify({ detail: [{ loc: ["body", "title"], msg: "Field required" }] }));
      },
    } as Partial<TrinityClient>;
    const a = JSON.parse(await makeAskOperator(unnamed).execute(ASK as any, agentCtx("self")));
    assert.equal(a.success, false);
    assert.equal(a.status, 422);
    assert.equal(a.code, "invalid_ask");
    assert.match(a.message, /Field required/);

    const down = { raiseAsk: async () => { throw new Error("socket hang up"); } } as Partial<TrinityClient>;
    const b = JSON.parse(await makeAskOperator(down).execute(ASK as any, agentCtx("self")));
    assert.deepEqual(b, { success: false, error: "socket hang up" });
  });

  it("the description teaches idempotency, the re-ask link and how to learn the outcome", () => {
    const tool = makeAskOperator(raisingClient().fake);
    assert.match(tool.description, /request_id/);
    assert.match(tool.description, /replayed/);
    assert.match(tool.description, /supersedes_expired/);
    assert.match(tool.description, /get_my_ask/);
    assert.match(tool.description, /wakes_on_ending/);
    assert.doesNotMatch(tool.description, /mcp__trinity__/);
  });
});
