/**
 * #2806 — a chain-depth refusal is a RESULT the calling model stops on, not an
 * `API error (403)` it cannot tell apart from an access denial.
 *
 * Drives a real `TrinityClient` against a stubbed `fetch` through the real tool
 * `execute()` bodies, so both halves are exercised: the client's 403 parsing
 * and `runAgentChat` / `fan_out` surfacing it. Every dispatch branch is covered
 * — sequential `/chat`, parallel `/task`, the #946 pull-routed `/task`, and
 * `fan_out`. Controls: a 403 WITHOUT the code (access denial) and a 422 still
 * throw, exactly as before.
 */
import { describe, it, beforeEach, afterEach } from "node:test";
import { strict as assert } from "node:assert";

import { TrinityClient, parseDepthRefusal } from "./client.js";
import { createChatTools } from "./tools/chat.js";

const realFetch = globalThis.fetch;

const REFUSAL = {
  detail: {
    error: "inter_agent_depth_exceeded",
    depth: 9,
    max_depth: 8,
    caller: "caller",
    target: "target",
    message:
      "Inter-agent chain depth limit reached (9 > 8). Do not retry this call or route it " +
      "through another agent; finish your turn with what you have and report back to your caller.",
  },
};

function json(body: unknown, status: number) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** Answers every dispatch route with `respond()`; records the paths hit. */
function stubDispatch(respond: () => Response) {
  const hits: string[] = [];
  globalThis.fetch = (async (input: unknown) => {
    const url = String(input);
    const route = ["/chat", "/task", "/fan-out"].find((r) => url.includes(r));
    if (!route) throw new Error(`unstubbed fetch: ${url}`);
    hits.push(route);
    return respond();
  }) as typeof fetch;
  return hits;
}

/** A real client with the two access-gate lookups answered locally, so the
 *  dispatch branch is reached without stubbing the permissions endpoints. */
function realClient(): TrinityClient {
  const client = new TrinityClient("http://backend:8000", "tok");
  (client as any).isAgentPermitted = async () => true;
  (client as any).getAgentAccessInfo = async () => ({ owner: "u1", is_shared: true });
  return client;
}

const agentSession = {
  session: { scope: "agent", agentName: "caller", userId: "u1", keyId: "k1", keyName: "kn" },
};

function tools(pullEnabled = false) {
  return createChatTools(realClient(), false, pullEnabled);
}

function assertRefusal(raw: string) {
  const out = JSON.parse(raw);
  assert.equal(out.status, "inter_agent_depth_exceeded");
  assert.equal(out.agent, "target");
  assert.equal(out.depth, 9);
  assert.equal(out.max_depth, 8);
  assert.equal(out.retryable, false);
  assert.match(out.message, /Do not retry/);
}

describe("#2806 chain-depth refusal surfaces as a structured, non-retryable result", () => {
  beforeEach(() => { globalThis.fetch = realFetch; });
  afterEach(() => { globalThis.fetch = realFetch; });

  it("sequential chat_with_agent (/chat)", async () => {
    const hits = stubDispatch(() => json(REFUSAL, 403));
    const raw = await tools().chatWithAgent.execute(
      { agent_name: "target", message: "hi" }, agentSession,
    );
    assert.deepEqual(hits, ["/chat"]);
    assertRefusal(raw);
  });

  it("parallel chat_with_agent (/task)", async () => {
    const hits = stubDispatch(() => json(REFUSAL, 403));
    const raw = await tools().chatWithAgent.execute(
      { agent_name: "target", message: "hi", parallel: true }, agentSession,
    );
    assert.deepEqual(hits, ["/task"]);
    assertRefusal(raw);
  });

  it("pull-routed sequential chat_with_agent (#946, async /task)", async () => {
    const hits = stubDispatch(() => json(REFUSAL, 403));
    const raw = await tools(true).chatWithAgent.execute(
      { agent_name: "target", message: "hi" }, agentSession,
    );
    assert.deepEqual(hits, ["/task"]);
    assertRefusal(raw);
  });

  it("fan_out (/fan-out)", async () => {
    const hits = stubDispatch(() => json(REFUSAL, 403));
    const raw = await tools().fanOut.execute(
      { agent_name: "target", tasks: [{ id: "t1", message: "m" }] }, agentSession,
    );
    assert.deepEqual(hits, ["/fan-out"]);
    assertRefusal(raw);
  });
});

describe("#2806 controls: other errors still throw", () => {
  beforeEach(() => { globalThis.fetch = realFetch; });
  afterEach(() => { globalThis.fetch = realFetch; });

  const denial = () => json({ detail: "Source agent header 'x' doesn't match API key scope 'y'" }, 403);
  const invalid = () => json({ detail: "model: invalid" }, 422);

  for (const [label, respond, status] of [
    ["a 403 access denial", denial, 403],
    ["a 422", invalid, 422],
  ] as const) {
    it(`${label} on /chat throws API error (${status})`, async () => {
      stubDispatch(respond);
      await assert.rejects(
        tools().chatWithAgent.execute({ agent_name: "target", message: "hi" }, agentSession),
        new RegExp(`API error \\(${status}\\)`),
      );
    });

    it(`${label} on /task throws API error (${status})`, async () => {
      stubDispatch(respond);
      await assert.rejects(
        tools().chatWithAgent.execute(
          { agent_name: "target", message: "hi", parallel: true }, agentSession,
        ),
        new RegExp(`API error \\(${status}\\)`),
      );
    });

    it(`${label} on /fan-out throws API error (${status})`, async () => {
      stubDispatch(respond);
      await assert.rejects(
        tools().fanOut.execute(
          { agent_name: "target", tasks: [{ id: "t1", message: "m" }] }, agentSession,
        ),
        new RegExp(`API error \\(${status}\\)`),
      );
    });
  }

  it("parseDepthRefusal only accepts a 403 carrying the named code", () => {
    const body = JSON.stringify(REFUSAL);
    assert.equal(parseDepthRefusal(403, body, "target")?.depth, 9);
    assert.equal(parseDepthRefusal(422, body, "target"), undefined);
    assert.equal(parseDepthRefusal(403, "not json", "target"), undefined);
    assert.equal(parseDepthRefusal(403, JSON.stringify({ detail: "Agent not found" }), "target"), undefined);
  });
});
