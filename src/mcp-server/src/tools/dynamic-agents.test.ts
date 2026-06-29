/**
 * #846 — dynamic dedicated-agent chat tools.
 *
 * Pins:
 *   - makeDedicatedChatTool delegates to runAgentChat (bound agent, no agent_name
 *     param) and propagates a 403 access denial.
 *   - the dedicated tool preserves the audit target (bound agent name).
 *   - startExposedToolsReconciler add/remove/no-op diffing, the built-in
 *     collision guard, fail-open on transient errors, and the in-flight mutex.
 *
 * Runner: built-in node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it, beforeEach } from "node:test";
import { strict as assert } from "node:assert";

import { makeDedicatedChatTool, startExposedToolsReconciler } from "./dynamic-agents.js";
import { withAudit } from "../audit.js";
import type { TrinityClient } from "../client.js";

type Recorded = { method: "chat" | "task"; name: string; idempotencyKey?: string };

function makeFakeClient(calls: Recorded[], opts: { permitted?: boolean } = {}): TrinityClient {
  const permitted = opts.permitted ?? true;
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    isAgentPermitted: async () => permitted,
    getAgentAccessInfo: async () => ({ owner: "u1", is_shared: true }) as any,
    chat: async (name: string, _m: string, _s?: string, _k?: any, idempotencyKey?: string) => {
      calls.push({ method: "chat", name, idempotencyKey });
      return { response: "ok" } as any;
    },
    task: async (name: string, _m: string, _o?: any, _s?: string, _k?: any, idempotencyKey?: string) => {
      calls.push({ method: "task", name, idempotencyKey });
      return { status: "accepted", execution_id: "ex_1" } as any;
    },
  };
  return fake as unknown as TrinityClient;
}

const agentSession = (agentName: string) => ({
  session: { scope: "agent", agentName, userId: "u1", keyId: "k1", keyName: "kn" } as any,
});

describe("#846 makeDedicatedChatTool", () => {
  it("delegates to runAgentChat with the bound agent (no agent_name param)", async () => {
    const calls: Recorded[] = [];
    const tool = makeDedicatedChatTool(
      makeFakeClient(calls),
      false,
      false,
      "support-bot",
      "chat_with_support_bot",
      "Chat directly with the \"support-bot\" agent."
    );
    assert.equal(tool.name, "chat_with_support_bot");
    const out = JSON.parse(await tool.execute({ message: "hi" }, agentSession("caller")));
    assert.equal(calls.length, 1);
    assert.equal(calls[0].method, "chat");
    assert.equal(calls[0].name, "support-bot"); // bound, never from caller input
    assert.equal(out.response, "ok");
  });

  it("propagates a 403 access denial from checkAgentAccess", async () => {
    const calls: Recorded[] = [];
    const tool = makeDedicatedChatTool(
      makeFakeClient(calls, { permitted: false }),
      false,
      false,
      "secret-bot",
      "chat_with_secret_bot",
      "desc"
    );
    const out = JSON.parse(await tool.execute({ message: "hi" }, agentSession("caller")));
    assert.equal(out.error, "Access denied");
    assert.equal(calls.length, 0); // never dispatched
  });

  it("preserves the audit target_id (bound agent) when wrapped by withAudit", async () => {
    const calls: Recorded[] = [];
    const tool = makeDedicatedChatTool(
      makeFakeClient(calls),
      false,
      false,
      "audit-bot",
      "chat_with_audit_bot",
      "desc"
    );
    // The dedicated tool's params carry no agent_name — withAudit must fall back
    // to the bound target. We can't intercept the fire-and-forget POST here, so
    // assert the binding wiring: withAudit accepts a boundTargetId and the
    // wrapped execute still runs.
    const wrapped = withAudit(tool.name, tool.execute, "audit-bot");
    const out = JSON.parse(await wrapped({ message: "hi" } as any, agentSession("caller")));
    assert.equal(out.response, "ok");
    assert.equal(calls[0].name, "audit-bot");
  });
});

// ---------------------------------------------------------------------------
// Reconciler
// ---------------------------------------------------------------------------

interface FakeServer {
  registered: Map<string, string>; // agentName -> toolName via auditTargetId
  registerDynamicTool: (tool: any, canAccess: any, auditTargetId: string) => void;
  unregisterDynamicTool: (name: string) => void;
}

function makeFakeServer(): FakeServer {
  const registered = new Map<string, string>();
  // reverse: toolName -> agentName so unregister-by-name can clean up
  const byTool = new Map<string, string>();
  return {
    registered,
    registerDynamicTool: (tool: any, _canAccess: any, auditTargetId: string) => {
      registered.set(auditTargetId, tool.name);
      byTool.set(tool.name, auditTargetId);
    },
    unregisterDynamicTool: (name: string) => {
      const agent = byTool.get(name);
      if (agent) {
        registered.delete(agent);
        byTool.delete(name);
      }
    },
  };
}

function makeFetch(responses: Array<{ status: number; body?: any; throwIt?: boolean }>) {
  let i = 0;
  const fn = async (_url: any, _init?: any) => {
    const r = responses[Math.min(i, responses.length - 1)];
    i++;
    if (r.throwIt) throw new Error("network down");
    return {
      ok: r.status >= 200 && r.status < 300,
      status: r.status,
      json: async () => r.body,
    } as any;
  };
  (fn as any).calls = () => i;
  return fn as unknown as typeof fetch & { calls: () => number };
}

function baseOpts(server: FakeServer, fetchImpl: any) {
  return {
    trinityApiUrl: "http://localhost:8000",
    internalSecret: "s3cr3t",
    client: makeFakeClient([]),
    requireApiKey: false,
    agentChatPullEnabled: false,
    registerDynamicTool: server.registerDynamicTool,
    unregisterDynamicTool: server.unregisterDynamicTool,
    connectorDenied: (_auth: any) => true,
    builtinToolNames: new Set<string>(["chat_with_agent", "list_agents"]),
    intervalMs: 1_000_000, // effectively disable the timer in tests
    runImmediately: false, // tests drive syncOnce() deterministically
    fetchImpl,
  };
}

describe("#846 startExposedToolsReconciler", () => {
  let server: FakeServer;
  beforeEach(() => {
    server = makeFakeServer();
  });

  it("registers tools from a 200 response", async () => {
    const fetchImpl = makeFetch([
      { status: 200, body: { agents: [
        { agent_name: "a1", tool_name: "chat_with_a1", description: "d1" },
        { agent_name: "a2", tool_name: "chat_with_a2", description: "d2" },
      ] } },
    ]);
    const h = startExposedToolsReconciler(baseOpts(server, fetchImpl));
    await h.syncOnce();
    h.stop();
    assert.equal(server.registered.get("a1"), "chat_with_a1");
    assert.equal(server.registered.get("a2"), "chat_with_a2");
  });

  it("removes tools that are no longer exposed", async () => {
    const fetchImpl = makeFetch([
      { status: 200, body: { agents: [{ agent_name: "a1", tool_name: "chat_with_a1", description: "d" }] } },
      { status: 200, body: { agents: [] } },
    ]);
    const h = startExposedToolsReconciler(baseOpts(server, fetchImpl));
    await h.syncOnce();
    assert.equal(server.registered.size, 1);
    await h.syncOnce();
    h.stop();
    assert.equal(server.registered.size, 0);
  });

  it("is a no-op on an unchanged set (no churn)", async () => {
    const body = { agents: [{ agent_name: "a1", tool_name: "chat_with_a1", description: "d" }] };
    const fetchImpl = makeFetch([{ status: 200, body }, { status: 200, body }]);
    const h = startExposedToolsReconciler(baseOpts(server, fetchImpl));
    await h.syncOnce();
    const first = server.registered.get("a1");
    await h.syncOnce();
    h.stop();
    assert.equal(server.registered.get("a1"), first);
    assert.equal(server.registered.size, 1);
  });

  it("skips a tool_name that collides with a built-in tool", async () => {
    const fetchImpl = makeFetch([
      { status: 200, body: { agents: [
        { agent_name: "evil", tool_name: "chat_with_agent", description: "d" }, // collides
        { agent_name: "ok", tool_name: "chat_with_ok", description: "d" },
      ] } },
    ]);
    const h = startExposedToolsReconciler(baseOpts(server, fetchImpl));
    await h.syncOnce();
    h.stop();
    assert.equal(server.registered.has("evil"), false);
    assert.equal(server.registered.get("ok"), "chat_with_ok");
  });

  it("fail-open: a transient 500 keeps the last-known set (0 unregisters)", async () => {
    const fetchImpl = makeFetch([
      { status: 200, body: { agents: [{ agent_name: "a1", tool_name: "chat_with_a1", description: "d" }] } },
      { status: 500 },
    ]);
    const h = startExposedToolsReconciler(baseOpts(server, fetchImpl));
    await h.syncOnce();
    assert.equal(server.registered.size, 1);
    await h.syncOnce(); // 500 → must NOT mutate
    h.stop();
    assert.equal(server.registered.get("a1"), "chat_with_a1");
  });

  it("fail-open: a network throw keeps the last-known set", async () => {
    const fetchImpl = makeFetch([
      { status: 200, body: { agents: [{ agent_name: "a1", tool_name: "chat_with_a1", description: "d" }] } },
      { throwIt: true, status: 0 },
    ]);
    const h = startExposedToolsReconciler(baseOpts(server, fetchImpl));
    await h.syncOnce();
    await h.syncOnce();
    h.stop();
    assert.equal(server.registered.get("a1"), "chat_with_a1");
  });

  it("fail-open: a malformed body (no agents array) keeps the last-known set", async () => {
    const fetchImpl = makeFetch([
      { status: 200, body: { agents: [{ agent_name: "a1", tool_name: "chat_with_a1", description: "d" }] } },
      { status: 200, body: { oops: true } },
    ]);
    const h = startExposedToolsReconciler(baseOpts(server, fetchImpl));
    await h.syncOnce();
    await h.syncOnce();
    h.stop();
    assert.equal(server.registered.get("a1"), "chat_with_a1");
  });

  it("re-registers on a re-slug (tool_name change for the same agent)", async () => {
    const fetchImpl = makeFetch([
      { status: 200, body: { agents: [{ agent_name: "a1", tool_name: "chat_with_a1", description: "d" }] } },
      { status: 200, body: { agents: [{ agent_name: "a1", tool_name: "chat_with_a1_ab12", description: "d" }] } },
    ]);
    const h = startExposedToolsReconciler(baseOpts(server, fetchImpl));
    await h.syncOnce();
    assert.equal(server.registered.get("a1"), "chat_with_a1");
    await h.syncOnce();
    h.stop();
    assert.equal(server.registered.get("a1"), "chat_with_a1_ab12");
  });

  it("in-flight mutex: an overlapping syncOnce is a no-op and doesn't corrupt the map", async () => {
    // A fetch that resolves on a microtask lets us start two syncs back-to-back.
    let resolveFetch: (v: any) => void;
    const gate = new Promise((res) => { resolveFetch = res; });
    let served = false;
    const fetchImpl = (async () => {
      if (!served) {
        served = true;
        await gate; // first call hangs until released
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({ agents: [{ agent_name: "a1", tool_name: "chat_with_a1", description: "d" }] }),
      } as any;
    }) as unknown as typeof fetch;

    const h = startExposedToolsReconciler(baseOpts(server, fetchImpl));
    const p1 = h.syncOnce();           // enters, awaits gate, inFlight=true
    const p2 = h.syncOnce();           // sees inFlight → returns immediately
    await p2;                          // the second one resolved without mutating
    assert.equal(server.registered.size, 0, "second sync must no-op while first is in flight");
    resolveFetch!(undefined);
    await p1;
    h.stop();
    assert.equal(server.registered.get("a1"), "chat_with_a1");
  });
});
