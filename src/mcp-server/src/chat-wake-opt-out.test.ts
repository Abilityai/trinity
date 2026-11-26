/**
 * #4538 — a caller that waits is not woken: what leaves the MCP server, and
 * what the receipt says.
 *
 * `wake: false` on `chat_with_agent` and on every dedicated `chat_with_<agent>`
 * declines the turn the platform starts on the caller when the run ends, and a
 * sync call tells the backend how long this server waits for the reply
 * (`caller_wait_ms`), so a run that answers inline wakes nobody while one that
 * outlives the wait keeps its wake.
 *
 * Measured on what actually LEAVES the server, the `chat-parent-execution.test.ts`
 * shape: a real `createServer`, a real MCP `Client` over streamable HTTP (so
 * FastMCP's zod parse runs — an undeclared parameter is stripped there), the
 * real reconciler registering `chat_with_helper_bot`, and the real
 * `TrinityClient` body, recorded by a fake backend that stamps the wake verdict
 * the real one does.
 *
 * Contrast pairs: every "absent" assertion sits beside the call that sends the
 * field, one input changed.
 *
 * Runner: node:test → `node --import tsx --test src/*.test.ts`.
 */
import { strict as assert } from "node:assert";
import { after, before, describe, it } from "node:test";
import { createServer as createHttpServer, type IncomingHttpHeaders, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

import { createServer } from "./server.js";
import { startExposedToolsReconciler } from "./tools/dynamic-agents.js";

const CALLER = "alpha";
const TARGET = "target-bot";
const HELPER = "helper-bot";
const DEDICATED = "chat_with_helper_bot";
const AGENT_KEY = "trinity_mcp_test_agent_key";
const TURN = "exec-header-turn";

type Mode = "ok" | "queued" | "gate" | "depth" | "error" | "busy" | "replay409" | "chat504"
  | "armRefused" | "slow" | "policyOnReply";

interface Dispatch {
  route: "task" | "chat";
  agent: string;
  body: Record<string, unknown>;
  turn: string | undefined;
  key: string | undefined;
}

interface Arm {
  agent: string;
  executionId: string;
  body: Record<string, unknown>;
  sourceAgent: string | undefined;
}

interface CallResult {
  out: any;
  text: string;
  isError: boolean;
  rec: Dispatch | undefined;
  arms: Arm[];
}

interface Harness {
  call: (tool: string, args: Record<string, unknown>, header?: string) => Promise<CallResult>;
  setMode: (m: Mode) => void;
  tools: Map<string, { description?: string; inputSchema: any }>;
  stop: () => Promise<void>;
}

const one = (h: IncomingHttpHeaders, name: string): string | undefined => {
  const v = h[name];
  return Array.isArray(v) ? v[0] : v;
};

function json(res: import("node:http").ServerResponse, status: number, payload: unknown, extra: Record<string, string> = {}) {
  res.writeHead(status, { "Content-Type": "application/json", ...extra });
  res.end(JSON.stringify(payload));
}

/**
 * Boot one real MCP server against a recording fake backend, with the real
 * reconciler registering the dedicated tool, and one MCP client whose `fetch`
 * stamps (or omits) `X-Trinity-Execution-Id` per call.
 */
async function boot(opts: { pull?: boolean; reportBackEnabled?: boolean } = {}): Promise<Harness> {
  let mode: Mode = "ok";
  const dispatches: Dispatch[] = [];
  const arms: Arm[] = [];
  const unexpected: string[] = [];
  /** The message of the last /chat dispatch — what the 504 recovery lookup must find. */
  let lastChatMessage = "";

  const backend: Server = createHttpServer((req, res) => {
    let raw = "";
    req.on("data", (c) => (raw += c));
    req.on("end", () => {
      const url = req.url ?? "";
      // The server's own startup probe of the backend.
      if (url === "/health" && req.method === "GET") return json(res, 200, { status: "healthy" });
      // #3295: the report-back arm.
      const arm = url.match(/^\/api\/agents\/([^/]+)\/executions\/([^/]+)\/report-back$/);
      if (arm && req.method === "POST") {
        const body = JSON.parse(raw) as Record<string, unknown>;
        arms.push({
          agent: decodeURIComponent(arm[1]),
          executionId: decodeURIComponent(arm[2]),
          body,
          sourceAgent: one(req.headers, "x-source-agent"),
        });
        if (mode === "armRefused") {
          return json(res, 200, { armed: false, execution_id: arm[2], reason: "no_inherited_context" });
        }
        return json(res, 200, { armed: true, execution_id: arm[2], status: "running" });
      }
      // #3295: the recovery lookup after a backend 504 (`findRecentMcpExecution`).
      const recovery = url.match(/^\/api\/agents\/([^/]+)\/executions\?limit=\d+$/);
      if (recovery && req.method === "GET") {
        return json(res, 200, [{
          id: "exec-child-504",
          schedule_id: "",
          agent_name: decodeURIComponent(recovery[1]),
          status: "running",
          started_at: new Date().toISOString(),
          message: lastChatMessage,
          triggered_by: "agent",
          source_mcp_key_id: "key-agent-1",
        }]);
      }
      // #3296 review: the caller's own turn row, read to scope a delegation's key
      // to its retry chain. None of these turns is a retry.
      const turnRow = url.match(/^\/api\/agents\/([^/]+)\/executions\/([^/?]+)$/);
      if (turnRow && req.method === "GET") {
        return json(res, 200, { id: decodeURIComponent(turnRow[2]), agent_name: decodeURIComponent(turnRow[1]) });
      }
      if (url === "/api/mcp/validate") {
        return json(res, 200, {
          valid: true,
          key_id: "key-agent-1",
          user_id: "owner",
          user_email: "owner@example.com",
          key_name: "agent-key",
          scope: "agent",
          agent_name: CALLER,
        });
      }
      const reach = url.match(new RegExp(`^/api/agents/${CALLER}/reach/([^/?]+)$`));
      if (reach) {
        return json(res, 200, { reachable: [TARGET, HELPER, CALLER].includes(decodeURIComponent(reach[1])) });
      }
      if (url === `/api/agents/${CALLER}/permissions`) {
        return json(res, 200, { permitted_agents: [{ name: TARGET }, { name: HELPER }, { name: CALLER }] });
      }
      const m = url.match(/^\/api\/agents\/([^/]+)\/(task|chat)$/);
      if (!m || req.method !== "POST") {
        unexpected.push(`${req.method} ${url}`);
        return json(res, 500, { detail: `unexpected request ${req.method} ${url}` });
      }
      const agent = decodeURIComponent(m[1]);
      const route = m[2] as "task" | "chat";
      const body = JSON.parse(raw) as Record<string, unknown>;
      dispatches.push({
        route,
        agent,
        body,
        turn: one(req.headers, "x-trinity-execution-id"),
        key: one(req.headers, "idempotency-key"),
      });
      lastChatMessage = String(body.message ?? "");
      // What `_wake_on_ending_receipt` stamps on a receipt (#4538: a declined
      // call is its own layer, `source: "call"`).
      const verdict = body.wake === false
        ? { effective: false, source: "call" }
        : { effective: true, source: "instance" };
      switch (mode) {
        case "chat504":
          if (route === "chat") {
            return json(res, 504, { detail: `Chat turn on agent '${agent}' did not complete in time. Execution exec-child-504 may still be running; poll GET /api/agents/${agent}/executions/exec-child-504.` });
          }
          break;
        case "armRefused":
          if (route === "chat") return json(res, 409, { detail: { execution_id: "exec-child-replay" } });
          break;
        case "gate":
          return json(res, 202, {
            status: "pending_approval",
            code: "approval_pending",
            request_id: "gate-abc",
            agent,
            skills: ["pay-invoice"],
            approver_role: "primary",
            expires_at: "2026-10-03T10:00:00Z",
            message: "Not run: the skill pay-invoice needs approval.",
          }, { "x-trinity-error-code": "approval_pending" });
        case "depth":
          return json(res, 403, { detail: { error: "inter_agent_depth_exceeded", depth: 4, max_depth: 3 } });
        case "error":
          return json(res, 500, { detail: "boom" });
        case "busy":
          return json(res, 429, { retry_after: 12 }, { "x-trinity-error-code": "capacity" });
        case "replay409":
          return json(res, 409, { detail: { execution_id: "exec-child-replay" } });
        case "slow":
          // The run outlives the MCP server's own wait.
          setTimeout(() => {
            if (!res.writableEnded) json(res, 200, { response: "late", execution_id: "exec-child-504" });
          }, 900);
          return;
        case "policyOnReply":
          // A sync answer that is the reply, carrying a policy all the same.
          return json(res, 200, {
            response: "done", execution_id: "exec-child-sync", wake_on_ending: verdict,
          });
        case "queued":
          return json(res, 200, {
            status: "queued",
            execution_id: "exec-child-queued",
            agent_name: agent,
            message: `Queued. Poll GET /api/agents/${agent}/executions/exec-child-queued`,
            wake_on_ending: verdict,
          });
        default:
          if (route === "task" && body.async_mode === true) {
            return json(res, 200, {
              status: "accepted",
              execution_id: "exec-child-1",
              agent_name: agent,
              message: `Accepted. Poll GET /api/agents/${agent}/executions/exec-child-1`,
              async_mode: true,
              wake_on_ending: verdict,
            });
          }
          return json(res, 200, { response: "done", execution_id: "exec-child-sync", timestamp: "2026-10-06T00:00:00Z" });
      }
    });
  });
  await new Promise<void>((r) => backend.listen(0, "127.0.0.1", () => r()));
  const backendUrl = `http://127.0.0.1:${(backend.address() as AddressInfo).port}`;

  // FastMCP takes a fixed port, so probe one, then retry on EADDRINUSE: the
  // node test runner runs test files as concurrent processes and the
  // probe-close-reuse window can race.
  let built: Awaited<ReturnType<typeof createServer>> | undefined;
  let mcpPort = 0;
  for (let attempt = 0; attempt < 3 && !built; attempt++) {
    const probe = createHttpServer();
    await new Promise<void>((r) => probe.listen(0, "127.0.0.1", () => r()));
    mcpPort = (probe.address() as AddressInfo).port;
    await new Promise<void>((r) => probe.close(() => r()));
    const candidate = await createServer({
      trinityApiUrl: backendUrl,
      requireApiKey: true,
      port: mcpPort,
      internalApiSecret: "",
      agentChatPullEnabled: opts.pull ?? false,
      ...(opts.reportBackEnabled === undefined ? {} : { reportBackEnabled: opts.reportBackEnabled }),
    });
    try {
      await candidate.server.start({ transportType: "httpStream", httpStream: { port: mcpPort, host: "127.0.0.1" } });
      built = candidate;
    } catch (e) {
      if ((e as NodeJS.ErrnoException)?.code !== "EADDRINUSE" || attempt === 2) throw e;
    }
  }
  const server = built!;

  // The real reconciler on the real registration handles — the path index.ts runs.
  const reconciler = startExposedToolsReconciler({
    trinityApiUrl: backendUrl,
    internalSecret: "test-internal-secret",
    client: server.client,
    requireApiKey: server.requireApiKey,
    agentChatPullEnabled: server.agentChatPullEnabled,
    reportBackEnabled: server.reportBackEnabled,
    registerDynamicTool: server.registerDynamicTool,
    unregisterDynamicTool: server.unregisterDynamicTool,
    operatorOnly: server.operatorOnly,
    builtinToolNames: server.builtinToolNames,
    intervalMs: 1_000_000,
    runImmediately: false,
    fetchImpl: (async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        agents: [{ agent_name: HELPER, tool_name: DEDICATED, description: `Chat directly with the "${HELPER}" agent.` }],
      }),
    })) as unknown as typeof fetch,
  });
  await reconciler.syncOnce();

  let header: string | undefined;
  const mcp = new Client({ name: CALLER, version: "1.0.0" });
  await mcp.connect(new StreamableHTTPClientTransport(new URL(`http://127.0.0.1:${mcpPort}/mcp`), {
    requestInit: { headers: { Authorization: `Bearer ${AGENT_KEY}` } },
    fetch: (url, init) => {
      const h = new Headers(init?.headers);
      if (header === undefined) h.delete("X-Trinity-Execution-Id");
      else h.set("X-Trinity-Execution-Id", header);
      return fetch(url, { ...init, headers: h });
    },
  }));
  const tools = new Map<string, { description?: string; inputSchema: any }>();
  for (const t of (await mcp.listTools()).tools) tools.set(t.name, t as any);

  return {
    tools,
    setMode: (m) => { mode = m; },
    call: async (tool, args, h) => {
      assert.ok(tools.has(tool), `${tool} is not in tools/list (have ${tools.size})`);
      header = h;
      dispatches.length = 0;
      arms.length = 0;
      try {
        const res = (await mcp.callTool({ name: tool, arguments: args })) as {
          content: Array<{ type: string; text: string }>;
          isError?: boolean;
        };
        const text = res.content?.[0]?.text ?? "";
        let out: any;
        try { out = JSON.parse(text); } catch { out = undefined; }
        assert.deepEqual(unexpected, [], "the fake backend got a request it does not serve");
        assert.ok(dispatches.length <= 1, `one call dispatched ${dispatches.length} times`);
        assert.ok(arms.length <= 1, `one call armed ${arms.length} times`);
        return { out, text, isError: res.isError === true, rec: dispatches[0], arms: [...arms] };
      } finally {
        header = undefined;
        mode = "ok";
      }
    },
    stop: async () => {
      reconciler.stop();
      await mcp.close();
      await server.server.stop();
      await new Promise<void>((r) => backend.close(() => r()));
    },
  };
}

const PA = { parallel: true, async: true };
/** The wait this server declares on a sync call: its own ceiling. */
const ceiling = () => Number(process.env.MCP_CHAT_TIMEOUT_MS || 25000);

describe("#4538 wake opt-out leaves the MCP server (real transport)", () => {
  let off: Harness; // pull flag OFF
  let pull: Harness; // pull flag ON

  before(async () => {
    off = await boot({ pull: false });
    pull = await boot({ pull: true });
  });
  after(async () => {
    await off?.stop();
    await pull?.stop();
  });

  it("both tools declare `wake`, so zod does not strip it", () => {
    for (const tool of ["chat_with_agent", DEDICATED]) {
      const props = off.tools.get(tool)?.inputSchema?.properties ?? {};
      assert.ok("wake" in props, `${tool} does not declare wake`);
      assert.match(String(props.wake.description), /caller_declined/);
    }
  });

  it("an async call with wake:false sends it, and the receipt says caller_declined", async () => {
    const declined = await off.call("chat_with_agent", { agent_name: TARGET, message: "ping", ...PA, wake: false }, TURN);
    assert.equal(declined.rec?.route, "task");
    assert.equal(declined.rec?.body.wake, false);
    assert.equal(declined.out.wake, "none");
    assert.equal(declined.out.wake_reason, "caller_declined");

    const plain = await off.call("chat_with_agent", { agent_name: TARGET, message: "ping", ...PA }, TURN);
    assert.equal("wake" in (plain.rec?.body ?? {}), false, "an undeclared wake was sent");
    assert.equal(plain.out.wake, "on_ending");
    assert.equal("wake_reason" in plain.out, false);
  });

  it("wake:true is the default, not a force: nothing is sent", async () => {
    const r = await off.call("chat_with_agent", { agent_name: TARGET, message: "ping", ...PA, wake: true }, TURN);
    assert.equal("wake" in (r.rec?.body ?? {}), false);
    assert.equal(r.out.wake, "on_ending");
  });

  it("wake is a delivery option: it does not change the idempotency key", async () => {
    const declined = await off.call("chat_with_agent", { agent_name: TARGET, message: "same", ...PA, wake: false }, TURN);
    const plain = await off.call("chat_with_agent", { agent_name: TARGET, message: "same", ...PA }, TURN);
    assert.ok(declined.rec?.key);
    assert.equal(declined.rec?.key, plain.rec?.key);
  });

  it("a replay reports what the first call had, not what this call asked", async () => {
    // The backend replays the first call's receipt: it was not declined.
    off.setMode("ok");
    const r = await off.call("chat_with_agent", { agent_name: TARGET, message: "ping", ...PA }, TURN);
    assert.equal(r.out.wake, "on_ending");
    // ...and a receipt from a backend that did not confirm the decline never
    // says `caller_declined` on the caller's word alone.
    off.setMode("replay409");
    const replay = await off.call("chat_with_agent", { agent_name: TARGET, message: "ping", ...PA, wake: false }, TURN);
    assert.equal(replay.out.wake_reason === "caller_declined", false);
  });

  it("the dedicated chat_with_<agent> tool passes wake through", async () => {
    const declined = await off.call(DEDICATED, { message: "ping", ...PA, wake: false }, TURN);
    assert.equal(declined.rec?.agent, HELPER);
    assert.equal(declined.rec?.body.wake, false);
    assert.equal(declined.out.wake_reason, "caller_declined");

    const plain = await off.call(DEDICATED, { message: "ping", ...PA }, TURN);
    assert.equal("wake" in (plain.rec?.body ?? {}), false);
    assert.equal(plain.out.wake, "on_ending");
  });

  it("a sync parallel call declares this server's wait; an async one declares none", async () => {
    const sync = await off.call("chat_with_agent", { agent_name: TARGET, message: "ping", parallel: true }, TURN);
    assert.equal(sync.rec?.route, "task");
    assert.equal(sync.rec?.body.caller_wait_ms, ceiling());
    // The reply is in hand: the result says nothing about a wake.
    assert.equal("wake" in sync.out, false);

    const async_ = await off.call("chat_with_agent", { agent_name: TARGET, message: "ping", ...PA }, TURN);
    assert.equal("caller_wait_ms" in (async_.rec?.body ?? {}), false);
  });

  it("a sync parallel answer that is the reply reads as inline, whatever it carries", async () => {
    off.setMode("policyOnReply");
    const reply = await off.call("chat_with_agent", { agent_name: TARGET, message: "ping", parallel: true }, TURN);
    assert.equal(reply.out.response, "done");
    assert.equal("wake" in reply.out, false, "an inline reply promised a wake");
  });

  it("a sequential /chat call declares the wait and carries wake:false", async () => {
    const declined = await off.call("chat_with_agent", { agent_name: TARGET, message: "ping", wake: false }, TURN);
    assert.equal(declined.rec?.route, "chat");
    assert.equal(declined.rec?.body.caller_wait_ms, ceiling());
    assert.equal(declined.rec?.body.wake, false);

    const plain = await off.call("chat_with_agent", { agent_name: TARGET, message: "ping" }, TURN);
    assert.equal(plain.rec?.body.caller_wait_ms, ceiling());
    assert.equal("wake" in (plain.rec?.body ?? {}), false);
  });

  it("a pull-routed sequential call is a receipt: wake:false goes out, no wait does", async () => {
    const declined = await pull.call("chat_with_agent", { agent_name: TARGET, message: "ping", wake: false }, TURN);
    assert.equal(declined.rec?.route, "task");
    assert.equal(declined.rec?.body.async_mode, true);
    assert.equal(declined.rec?.body.wake, false);
    assert.equal("caller_wait_ms" in (declined.rec?.body ?? {}), false);
    assert.equal(declined.out.wake_reason, "caller_declined");

    const plain = await pull.call("chat_with_agent", { agent_name: TARGET, message: "ping" }, TURN);
    assert.equal("wake" in (plain.rec?.body ?? {}), false);
    assert.equal(plain.out.wake, "on_ending");
  });

  it("a sync call that outlives this server's wait declares the shortened wait and its own receipt stays silent", async () => {
    const before_ = process.env.MCP_CHAT_TIMEOUT_MS;
    process.env.MCP_CHAT_TIMEOUT_MS = "250";
    try {
      off.setMode("slow");
      const r = await off.call("chat_with_agent", { agent_name: TARGET, message: "slow one", parallel: true, wake: false }, TURN);
      assert.equal(r.rec?.body.caller_wait_ms, 250);
      assert.equal(r.out.status, "queued_timeout");
      // No backend verdict reached this receipt, so it claims nothing.
      assert.equal("wake" in r.out, false);
    } finally {
      if (before_ === undefined) delete process.env.MCP_CHAT_TIMEOUT_MS;
      else process.env.MCP_CHAT_TIMEOUT_MS = before_;
    }
  });
});
