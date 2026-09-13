/**
 * #2661 wiring — the `task()` route, executed rather than grepped.
 *
 * The pure helpers (`pickRecentMcpExecution`, `extractIdempotencyExecutionId`)
 * have their own cases in client.test.ts. Those do not gate the CHANGE, which
 * is what `task()` does with a 409 body and with its own abort: the #2675
 * review mutated both branches out of `sendTask` and nothing went red. Each
 * case here drives `TrinityClient.task()` against a stubbed `fetch` and fails
 * on exactly that mutation.
 */
import { describe, it, beforeEach, afterEach } from "node:test";
import { strict as assert } from "node:assert";

import { TrinityClient } from "./client.js";

const realFetch = globalThis.fetch;
const realTimeout = process.env.MCP_CHAT_TIMEOUT_MS;

type Handler = (url: string, init: RequestInit) => Promise<Response> | Response;

/** Route stub: first matching prefix wins; records every call. */
function stubFetch(routes: Array<[string, Handler]>) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, init: init ?? {} });
    const hit = routes.find(([prefix]) => url.includes(prefix));
    if (!hit) throw new Error(`unstubbed fetch: ${url}`);
    return hit[1](url, init ?? {});
  }) as typeof fetch;
  return calls;
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** A fetch that never answers until its signal aborts — the gateway-ceiling case. */
function hangUntilAbort(_url: string, init: RequestInit): Promise<Response> {
  return new Promise((_resolve, reject) => {
    const signal = init.signal as AbortSignal;
    signal.addEventListener("abort", () => {
      const err = new Error("This operation was aborted");
      err.name = "AbortError";
      reject(err);
    });
  });
}

function row(overrides: Record<string, unknown>) {
  return {
    id: "ex-live",
    status: "running",
    triggered_by: "mcp",
    source_mcp_key_id: "key-1",
    message: "do the thing",
    started_at: new Date().toISOString(),
    ...overrides,
  };
}

describe("#2661 task(): the 409 idempotency replay becomes a receipt", () => {
  beforeEach(() => { globalThis.fetch = realFetch; });
  afterEach(() => {
    globalThis.fetch = realFetch;
    if (realTimeout === undefined) delete process.env.MCP_CHAT_TIMEOUT_MS;
    else process.env.MCP_CHAT_TIMEOUT_MS = realTimeout;
  });

  it("a 409 carrying execution_id returns a queued_timeout receipt, not an API error", async () => {
    stubFetch([["/task", () => json({
      detail: {
        error: "request_in_progress",
        message: "A request with this Idempotency-Key is still being processed.",
        execution_id: "ex-409",
      },
    }, 409)]]);
    const client = new TrinityClient("http://backend:8000", "tok");

    const out = await client.task("agent-a", "do the thing");
    assert.equal((out as { status?: string }).status, "queued_timeout");
    assert.equal((out as { execution_id?: string }).execution_id, "ex-409");
    assert.match((out as { message: string }).message, /already dispatched/);
  });

  it("a 409 WITHOUT an execution_id is still an API error — no id, nothing to poll", async () => {
    stubFetch([["/task", () => json({ detail: "duplicate" }, 409)]]);
    const client = new TrinityClient("http://backend:8000", "tok");
    await assert.rejects(() => client.task("agent-a", "x"), /API error \(409\)/);
  });
});

describe("#2661 task(): the client's own abort recovers an execution_id", () => {
  beforeEach(() => {
    globalThis.fetch = realFetch;
    process.env.MCP_CHAT_TIMEOUT_MS = "20";
  });
  afterEach(() => {
    globalThis.fetch = realFetch;
    if (realTimeout === undefined) delete process.env.MCP_CHAT_TIMEOUT_MS;
    else process.env.MCP_CHAT_TIMEOUT_MS = realTimeout;
  });

  it("abort in sync mode → executions lookup → the one attributable row is the receipt", async () => {
    const calls = stubFetch([
      ["/task", hangUntilAbort],
      ["/executions?limit=50", () => json([
        row({ id: "ex-live" }),
        row({ id: "ex-done", status: "success" }),           // terminal: not ours to poll
        row({ id: "ex-other-msg", message: "something else" }), // another call's task
      ])],
    ]);
    const client = new TrinityClient("http://backend:8000", "tok");

    const out = await client.task("agent-a", "do the thing", undefined, undefined, { keyId: "key-1" });
    assert.equal((out as { status?: string }).status, "queued_timeout");
    assert.equal((out as { execution_id?: string }).execution_id, "ex-live");
    assert.equal(calls.filter((c) => c.url.includes("/executions")).length, 1);
  });

  it("abort with NO provable match throws the guidance error rather than guessing", async () => {
    stubFetch([
      ["/task", hangUntilAbort],
      ["/executions?limit=50", () => json([
        row({ id: "ex-1" }),
        row({ id: "ex-2" }), // two identical survivors: ambiguous by construction
      ])],
    ]);
    const client = new TrinityClient("http://backend:8000", "tok");
    await assert.rejects(
      () => client.task("agent-a", "do the thing", undefined, undefined, { keyId: "key-1" }),
      /no execution could be attributed/,
    );
  });

  it("async_mode never enters recovery — the abort propagates as-is", async () => {
    // async_mode has its own 30s; force the abort from the outside so the
    // case does not wait on it.
    const calls = stubFetch([
      ["/task", (_u, init) => {
        (init.signal as AbortSignal).dispatchEvent(new Event("abort"));
        const err = new Error("aborted");
        err.name = "AbortError";
        throw err;
      }],
      ["/executions", () => json([row({})])],
    ]);
    const client = new TrinityClient("http://backend:8000", "tok");
    await assert.rejects(
      () => client.task("agent-a", "do the thing", { async_mode: true }),
      /aborted/,
    );
    assert.equal(calls.filter((c) => c.url.includes("/executions")).length, 0);
  });

  it("a transport TypeError (request may never have arrived) is NOT recovered on this route", async () => {
    const calls = stubFetch([
      ["/task", () => { throw new TypeError("fetch failed"); }],
      ["/executions", () => json([row({})])],
    ]);
    const client = new TrinityClient("http://backend:8000", "tok");
    await assert.rejects(() => client.task("agent-a", "do the thing"), /fetch failed/);
    assert.equal(calls.filter((c) => c.url.includes("/executions")).length, 0);
  });
});

describe("#2661 chat(): the sequential route honours the same 409 rule", () => {
  beforeEach(() => { globalThis.fetch = realFetch; });
  afterEach(() => { globalThis.fetch = realFetch; });

  it("a 409 carrying execution_id returns the receipt (the tool description promises EVERY sync route)", async () => {
    stubFetch([["/chat", () => json({ detail: { execution_id: "ex-chat-409" } }, 409)]]);
    const client = new TrinityClient("http://backend:8000", "tok");
    const out = await client.chat("agent-a", "do the thing");
    assert.equal((out as { status?: string }).status, "queued_timeout");
    assert.equal((out as { execution_id?: string }).execution_id, "ex-chat-409");
  });

  it("chat() and task() emit the SAME receipt for the same 409 — one contract, two routes", async () => {
    const body = { detail: { execution_id: "ex-same" } };
    stubFetch([["/chat", () => json(body, 409)], ["/task", () => json(body, 409)]]);
    const client = new TrinityClient("http://backend:8000", "tok");
    const viaChat = await client.chat("agent-a", "m");
    const viaTask = await client.task("agent-a", "m");
    assert.deepEqual(viaChat, viaTask);
  });

  it("a 409 without an execution_id stays an API error on chat() too", async () => {
    stubFetch([["/chat", () => json({ detail: "nope" }, 409)]]);
    const client = new TrinityClient("http://backend:8000", "tok");
    await assert.rejects(() => client.chat("agent-a", "m"), /API error \(409\)/);
  });
});
