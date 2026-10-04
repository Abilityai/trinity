/**
 * trinity-enterprise#751 — a request naming a gated skill is a RESULT the
 * calling model stops on: `pending_approval` (an approval was raised, the
 * outcome arrives later) or `refused` (named, nothing raised). Never an empty
 * reply, never "agent busy, retry".
 *
 * Drives a real `TrinityClient` against a stubbed `fetch` through the real
 * `chat_with_agent` tool on every dispatch branch, and checks the caller's turn
 * (#2392) is forwarded from the PLATFORM header, never from the model-typed
 * `execution_id` param.
 */
import { describe, it, beforeEach, afterEach } from "node:test";
import { strict as assert } from "node:assert";

import { TrinityClient, parseGateResult } from "./client.js";
import { createChatTools } from "./tools/chat.js";

const realFetch = globalThis.fetch;

const PENDING = {
  status: "pending_approval",
  code: "approval_pending",
  request_id: "gate-abc",
  agent: "target",
  skills: ["pay-invoice"],
  approver_role: "primary",
  expires_at: "2026-10-03T10:00:00Z",
  message: "Not run: the skill pay-invoice on target needs approval from its primary.",
};

const QUEUE_FULL = {
  detail: { status: "refused", code: "approval_queue_full", message: "10 are already waiting." },
};

function json(body: unknown, status: number, errorCode?: string) {
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (errorCode) headers["x-trinity-error-code"] = errorCode;
  return new Response(JSON.stringify(body), { status, headers });
}

function stubDispatch(respond: () => Response) {
  const calls: Array<{ route: string; headers: Record<string, string> }> = [];
  globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
    const url = String(input);
    const route = ["/chat", "/task"].find((r) => url.includes(r));
    if (!route) throw new Error(`unstubbed fetch: ${url}`);
    calls.push({ route, headers: (init?.headers ?? {}) as Record<string, string> });
    return respond();
  }) as typeof fetch;
  return calls;
}

function realClient(): TrinityClient {
  const client = new TrinityClient("http://backend:8000", "tok");
  (client as any).isAgentPermitted = async () => true;
  (client as any).getAgentAccessInfo = async () => ({ owner: "u1", is_shared: true });
  return client;
}

const agentSession = {
  session: { scope: "agent", agentName: "caller", userId: "u1", keyId: "k1", keyName: "kn",
             executionId: "exec-turn-1" },
};

function tools(pullEnabled = false) {
  return createChatTools(realClient(), false, pullEnabled);
}

describe("#751 gated skill answers surface as structured, non-retryable results", () => {
  beforeEach(() => { globalThis.fetch = realFetch; });
  afterEach(() => { globalThis.fetch = realFetch; });

  for (const [label, args, pull, route] of [
    ["sequential /chat", { agent_name: "target", message: "/pay-invoice 1" }, false, "/chat"],
    ["parallel /task", { agent_name: "target", message: "/pay-invoice 1", parallel: true }, false, "/task"],
    ["pull-routed /task", { agent_name: "target", message: "/pay-invoice 1" }, true, "/task"],
  ] as const) {
    it(`${label}: 202 pending_approval`, async () => {
      const calls = stubDispatch(() => json(PENDING, 202, "approval_pending"));
      const out = JSON.parse(await tools(pull).chatWithAgent.execute(args as any, agentSession));
      assert.equal(calls[0].route, route);
      assert.equal(out.status, "pending_approval");
      assert.equal(out.request_id, "gate-abc");
      assert.equal(out.retryable, false);
      assert.match(out.message, /Do not retry/);
    });

    it(`${label}: a gate 429 is a refusal, not "agent busy"`, async () => {
      stubDispatch(() => json(QUEUE_FULL, 429, "approval_queue_full"));
      const out = JSON.parse(await tools(pull).chatWithAgent.execute(args as any, agentSession));
      assert.equal(out.status, "refused");
      assert.equal(out.code, "approval_queue_full");
      assert.notEqual(out.status, "agent_busy");
    });

    it(`${label}: forwards the platform turn, not the model-typed execution_id`, async () => {
      const calls = stubDispatch(() => json(PENDING, 202, "approval_pending"));
      await tools(pull).chatWithAgent.execute({ ...(args as any), execution_id: "typed-by-model" }, agentSession);
      assert.equal(calls[0].headers["X-Trinity-Execution-Id"], "exec-turn-1");
    });
  }

  it("an ordinary 429 on /chat is still agent_busy", async () => {
    stubDispatch(() => json({ retry_after: 12 }, 429));
    const out = JSON.parse(await tools().chatWithAgent.execute(
      { agent_name: "target", message: "hi" }, agentSession));
    assert.equal(out.status, "agent_busy");
  });

  it("a manual session forwards no turn", async () => {
    const calls = stubDispatch(() => json(PENDING, 202, "approval_pending"));
    await tools().chatWithAgent.execute({ agent_name: "target", message: "/pay-invoice 1" },
      { session: { ...agentSession.session, executionId: "manual" } });
    assert.equal(calls[0].headers["X-Trinity-Execution-Id"], undefined);
  });
});

describe("#751 parseGateResult", () => {
  it("ignores an accepted async receipt", () => {
    assert.equal(parseGateResult(202, null, JSON.stringify({ status: "accepted" }), "a"), undefined);
  });
  it("ignores a refusal whose header does not name the same code", () => {
    assert.equal(parseGateResult(429, null, JSON.stringify(QUEUE_FULL), "a"), undefined);
  });
  it("ignores the chain-depth refusal", () => {
    const depth = { detail: { error: "inter_agent_depth_exceeded" } };
    assert.equal(parseGateResult(403, "inter_agent_depth_exceeded", JSON.stringify(depth), "a"), undefined);
  });
  it("never throws on a non-JSON body", () => {
    assert.equal(parseGateResult(202, null, "<html>", "a"), undefined);
  });
});
