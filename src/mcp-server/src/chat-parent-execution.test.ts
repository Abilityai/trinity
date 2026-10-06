/**
 * #3232 — `chat_with_agent` (and every dedicated `chat_with_<agent>`) carries the
 * caller's turn as `parent_execution_id`, so delegated work reports back into the
 * conversation it came from (ent#224 / ent#265 do the rest on the backend).
 *
 * What this file measures is what actually LEAVES the MCP server: a real
 * `createServer`, a real MCP `Client` over streamable HTTP (so FastMCP's zod
 * parse runs — it is what silently dropped the dedicated tool's undeclared
 * `execution_id`), the real reconciler registering `chat_with_helper_bot`, and
 * the real `TrinityClient` body, recorded by a fake backend. A fake `task()`
 * could not see a `client.ts` body regression.
 *
 * The rule (the route table in docs/memory/feature-flows/channel-completion-report.md,
 * "How an MCP delegation carries the parent"):
 *   - async dispatches (parallel+async, #946 pull-routed) send the platform
 *     header turn by DEFAULT; a typed `manual` opts out; a self-task with
 *     `inject_result` is excluded;
 *   - sync parallel is opt-in by a typed id; sequential `/chat` never carries one;
 *   - the header turn wins over a typed id; a malformed or `manual` typed id is
 *     never forwarded;
 *   - `MCP_REPORT_BACK_ENABLED=false` stops all of it.
 *
 * Contrast pairs: dev forwards nothing, so an "absent" assertion alone is green
 * on dev and proves nothing. Every absent assertion sits in the same `it` as its
 * nearest forwarding sibling (same route, one input changed).
 *
 * Non-ambient values: the typed id and the header turn differ, so "forwarded the
 * argument" and "fell back to the header" never look the same.
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
// A namespace import, not named imports: on a tree without these exports the
// file still loads and each test fails on the missing BEHAVIOUR, not on a link
// error that takes the whole file down.
import * as chatMod from "./tools/chat.js";
import { startExposedToolsReconciler } from "./tools/dynamic-agents.js";

const CALLER = "alpha";
const TARGET = "target-bot";
const HELPER = "helper-bot";
const DEDICATED = "chat_with_helper_bot";
const AGENT_KEY = "trinity_mcp_test_agent_key";

const TYPED = "exec-typed-parent";
const HEADER = "exec-header-turn";
const HEADER_2 = "exec-header-turn-2";
/** Fails the header's own format check (> 128 chars), so the server parses it away. */
const MALFORMED_HEADER = "x".repeat(200);
/** What a model copies out of a template instead of its own id. */
const PLACEHOLDER_ID = "<your execution_id>";

const FIELDS = ["report_back", "report_back_reason", "report_back_note"] as const;

type Mode = "ok" | "queued" | "gate" | "depth" | "error" | "busy" | "replay409";

interface Dispatch {
  route: "task" | "chat";
  agent: string;
  body: Record<string, unknown>;
  turn: string | undefined;
  key: string | undefined;
}

interface CallResult {
  out: any;
  text: string;
  isError: boolean;
  rec: Dispatch | undefined;
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
  const unexpected: string[] = [];

  const backend: Server = createHttpServer((req, res) => {
    let raw = "";
    req.on("data", (c) => (raw += c));
    req.on("end", () => {
      const url = req.url ?? "";
      // The server's own startup probe of the backend.
      if (url === "/health" && req.method === "GET") return json(res, 200, { status: "healthy" });
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
      switch (mode) {
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
          return json(res, 429, { retry_after: 12 });
        case "replay409":
          return json(res, 409, { detail: { execution_id: "exec-child-replay" } });
        case "queued":
          return json(res, 200, {
            status: "queued",
            execution_id: "exec-child-queued",
            agent_name: agent,
            message: `Queued. Poll GET /api/agents/${agent}/executions/exec-child-queued`,
          });
        default:
          if (route === "task" && body.async_mode === true) {
            return json(res, 200, {
              status: "accepted",
              execution_id: "exec-child-1",
              agent_name: agent,
              message: `Accepted. Poll GET /api/agents/${agent}/executions/exec-child-1`,
              async_mode: true,
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
        return { out, text, isError: res.isError === true, rec: dispatches[0] };
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

const hasParent = (r: CallResult) => r.rec !== undefined && "parent_execution_id" in r.rec.body;
const parentOf = (r: CallResult) => r.rec?.body.parent_execution_id;
function assertNoFields(out: any, label: string) {
  for (const f of FIELDS) assert.equal(out && f in out, false, `${label}: unexpected ${f}`);
}
function withoutFields(out: any) {
  const copy = { ...out };
  for (const f of FIELDS) delete copy[f];
  return copy;
}

const PA = { parallel: true, async: true };

describe("#3232 parent_execution_id leaves the MCP server (real transport)", () => {
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

  it("T1: parallel async, typed id, no header → the typed id is the parent", async () => {
    const r = await off.call("chat_with_agent", { agent_name: TARGET, message: "t1", ...PA, execution_id: TYPED });
    assert.equal(r.rec?.route, "task");
    assert.equal(parentOf(r), TYPED);
    assert.equal(r.out.report_back, "requested");
  });

  it("T2: parallel SYNC, typed id, no header → the typed id is the parent (D2)", async () => {
    const r = await off.call("chat_with_agent", { agent_name: TARGET, message: "t2", parallel: true, execution_id: TYPED });
    assert.equal(r.rec?.route, "task");
    assert.equal(r.rec?.body.async_mode, false);
    assert.equal(parentOf(r), TYPED);
  });

  it("T3: parallel async, typed id + header → the header turn wins; the forwarded turn header is unchanged", async () => {
    const r = await off.call("chat_with_agent", { agent_name: TARGET, message: "t3", ...PA, execution_id: TYPED }, HEADER);
    assert.equal(parentOf(r), HEADER);
    assert.equal(r.rec?.turn, HEADER);
  });

  it("T4: pull-routed sequential, typed id → header when present, else the typed id", async () => {
    const withHeader = await pull.call("chat_with_agent", { agent_name: TARGET, message: "t4", execution_id: TYPED }, HEADER);
    assert.equal(withHeader.rec?.route, "task");
    assert.equal(withHeader.rec?.body.async_mode, true);
    assert.equal(parentOf(withHeader), HEADER);
    const noHeader = await pull.call("chat_with_agent", { agent_name: TARGET, message: "t4", execution_id: TYPED });
    assert.equal(parentOf(noHeader), TYPED);
  });

  it("T5: no typed id + header turn → async routes default to the header; sync parallel and /chat carry nothing", async () => {
    const asyncPar = await off.call("chat_with_agent", { agent_name: TARGET, message: "t5", ...PA }, HEADER);
    assert.equal(parentOf(asyncPar), HEADER);
    const pulled = await pull.call("chat_with_agent", { agent_name: TARGET, message: "t5" }, HEADER);
    assert.equal(pulled.rec?.route, "task");
    assert.equal(parentOf(pulled), HEADER);

    const syncPar = await off.call("chat_with_agent", { agent_name: TARGET, message: "t5", parallel: true }, HEADER);
    assert.equal(syncPar.rec?.route, "task");
    assert.equal(hasParent(syncPar), false, "sync parallel stays opt-in");
    assertNoFields(syncPar.out, "sync parallel, not armed");
    const seq = await off.call("chat_with_agent", { agent_name: TARGET, message: "t5" }, HEADER);
    assert.equal(seq.rec?.route, "chat");
    assert.deepEqual(seq.rec?.body, { message: "t5" });
  });

  it("T16: no typed id, NO header → nothing (an old agent image keeps opt-in); the same call typed forwards", async () => {
    const plain = await off.call("chat_with_agent", { agent_name: TARGET, message: "t16", ...PA });
    assert.equal(hasParent(plain), false);
    assertNoFields(plain.out, "no header, no typed id");
    const typed = await off.call("chat_with_agent", { agent_name: TARGET, message: "t16", ...PA, execution_id: TYPED });
    assert.equal(parentOf(typed), TYPED);
  });

  it("T17: no typed id, header `manual` → nothing and no fields; header turn → the header", async () => {
    const manual = await off.call("chat_with_agent", { agent_name: TARGET, message: "t17", ...PA }, "manual");
    assert.equal(hasParent(manual), false);
    assertNoFields(manual.out, "manual session, default");
    const turn = await off.call("chat_with_agent", { agent_name: TARGET, message: "t17", ...PA }, HEADER);
    assert.equal(parentOf(turn), HEADER);
  });

  it("T18: typed `manual` opts out on parallel async and pull — but the forwarded turn header survives", async () => {
    for (const [h, args] of [
      [off, { agent_name: TARGET, message: "t18", ...PA }],
      [pull, { agent_name: TARGET, message: "t18" }],
    ] as const) {
      const out = await h.call("chat_with_agent", { ...args, execution_id: "manual" }, HEADER);
      assert.equal(out.rec?.route, "task");
      assert.equal(hasParent(out), false, "a typed manual is the opt-out");
      assertNoFields(out.out, "opted out");
      assert.equal(out.rec?.turn, HEADER, "the opt-out must not clear the gate's view of the turn");
      const armed = await h.call("chat_with_agent", args, HEADER);
      assert.equal(parentOf(armed), HEADER);
    }
  });

  it("T19: a self-task with inject_result is excluded from the default; without it, it defaults", async () => {
    const injected = await off.call(
      "chat_with_agent",
      { agent_name: CALLER, message: "t19", ...PA, inject_result: true, chat_session_id: "sess-1" },
      HEADER,
    );
    assert.equal(injected.rec?.route, "task");
    assert.equal(injected.rec?.body.inject_result, true);
    assert.equal(hasParent(injected), false);
    assertNoFields(injected.out, "self-task with inject_result");
    const plain = await off.call("chat_with_agent", { agent_name: CALLER, message: "t19", ...PA }, HEADER);
    assert.equal(parentOf(plain), HEADER);
  });

  it("T6: sequential /chat (pull OFF) never carries a parent; the same call parallel does", async () => {
    const seq = await off.call("chat_with_agent", { agent_name: TARGET, message: "t6", execution_id: TYPED }, HEADER);
    assert.equal(seq.rec?.route, "chat");
    assert.deepEqual(seq.rec?.body, { message: "t6" });
    const par = await off.call("chat_with_agent", { agent_name: TARGET, message: "t6", ...PA, execution_id: TYPED }, HEADER);
    assert.equal(parentOf(par), HEADER);
  });

  it("T7/T7b/T7c: `manual` from either side never forwards", async () => {
    // T7: typed id + header `manual` → nothing; typed id + no header → typed.
    const t7 = await off.call("chat_with_agent", { agent_name: TARGET, message: "t7", ...PA, execution_id: TYPED }, "manual");
    assert.equal(hasParent(t7), false);
    assert.equal(t7.out.report_back, "off");
    assert.equal(t7.out.report_back_reason, "manual_session");
    const t7pair = await off.call("chat_with_agent", { agent_name: TARGET, message: "t7", ...PA, execution_id: TYPED });
    assert.equal(parentOf(t7pair), TYPED);

    // T7b: typed `manual`, no header → nothing (never forwarded raw).
    const t7b = await off.call("chat_with_agent", { agent_name: TARGET, message: "t7b", ...PA, execution_id: "manual" });
    assert.equal(hasParent(t7b), false);
    assertNoFields(t7b.out, "typed manual, no header");
    const t7bPair = await off.call("chat_with_agent", { agent_name: TARGET, message: "t7b", ...PA, execution_id: TYPED });
    assert.equal(parentOf(t7bPair), TYPED);

    // T7c: typed `manual` + header turn → nothing, no fields; no typed → header.
    const t7c = await off.call("chat_with_agent", { agent_name: TARGET, message: "t7c", ...PA, execution_id: "manual" }, HEADER);
    assert.equal(hasParent(t7c), false);
    assertNoFields(t7c.out, "typed manual + header");
    const t7cPair = await off.call("chat_with_agent", { agent_name: TARGET, message: "t7c", ...PA }, HEADER);
    assert.equal(parentOf(t7cPair), HEADER);
  });

  it("T7d: typed id + malformed header (parsed away) → the typed id", async () => {
    const r = await off.call("chat_with_agent", { agent_name: TARGET, message: "t7d", ...PA, execution_id: TYPED }, MALFORMED_HEADER);
    assert.equal(parentOf(r), TYPED);
    assert.equal(r.rec?.turn, undefined, "a malformed header is ignored, never forwarded");
  });

  it("T7e: a malformed typed id is never forwarded; with a header the header goes", async () => {
    const bare = await off.call("chat_with_agent", { agent_name: TARGET, message: "t7e", ...PA, execution_id: PLACEHOLDER_ID });
    assert.equal(hasParent(bare), false);
    assert.equal(bare.out.report_back, "off");
    assert.equal(bare.out.report_back_reason, "invalid_execution_id");
    const withHeader = await off.call("chat_with_agent", { agent_name: TARGET, message: "t7e", ...PA, execution_id: PLACEHOLDER_ID }, HEADER);
    assert.equal(parentOf(withHeader), HEADER);
    assert.equal(withHeader.out.report_back, "requested");
  });

  it("T8: the dedicated tool declares execution_id and forwards it, default included", async () => {
    const t = off.tools.get(DEDICATED);
    assert.ok(t, `${DEDICATED} is not listed — the registry is empty, so nothing below would mean anything`);
    assert.ok(t.inputSchema?.properties?.execution_id, "the dedicated tool must declare execution_id (zod drops undeclared keys)");
    const typed = await off.call(DEDICATED, { message: "t8", ...PA, execution_id: TYPED });
    assert.equal(typed.rec?.agent, HELPER);
    assert.equal(parentOf(typed), TYPED);
    const dflt = await off.call(DEDICATED, { message: "t8", ...PA }, HEADER);
    assert.equal(parentOf(dflt), HEADER);
  });

  it("T9: the parent never enters the Idempotency-Key (Invariant #18)", async () => {
    const msg = { agent_name: TARGET, message: "t9", ...PA };
    const pairs: Array<[CallResult, CallResult]> = [
      [await off.call("chat_with_agent", { ...msg, execution_id: TYPED }), await off.call("chat_with_agent", msg)],
      [await off.call("chat_with_agent", msg, HEADER), await off.call("chat_with_agent", msg)],
      [await off.call("chat_with_agent", msg, HEADER), await off.call("chat_with_agent", { ...msg, execution_id: "manual" }, HEADER)],
    ];
    for (const [a, b] of pairs) {
      assert.ok(a.rec?.key, "an Idempotency-Key is sent");
      assert.equal(a.rec?.key, b.rec?.key);
      assert.notEqual(parentOf(a), parentOf(b), "the pair must differ in the parent, or it proves nothing");
    }
  });

  it("T10: a self-task keeps inject_result and chat_session_id next to the parent", async () => {
    const r = await off.call("chat_with_agent", {
      agent_name: CALLER, message: "t10", parallel: true, inject_result: true, chat_session_id: "sess-1", execution_id: TYPED,
    });
    assert.equal(r.rec?.body.inject_result, true);
    assert.equal(r.rec?.body.chat_session_id, "sess-1");
    assert.equal(parentOf(r), TYPED);
  });

  it("T11: both tools publish the one shared execution_id description", () => {
    const shared = chatMod.EXECUTION_ID_PARAM_DESCRIPTION;
    assert.equal(typeof shared, "string", "EXECUTION_ID_PARAM_DESCRIPTION must be exported from chat.ts");
    for (const name of ["chat_with_agent", DEDICATED]) {
      assert.equal(off.tools.get(name)?.inputSchema?.properties?.execution_id?.description, shared, name);
    }
    for (const phrase of [
      "parallel=true, async=true",
      "on by default",
      "routes as a task",
      "\"manual\"",
      "not a guarantee",
      "does not report back",
      "do not re-send",
      // An async call that errors after the backend accepted the work still
      // reports back: the default is tied to the dispatch, not to the receipt.
      // Scoped to async or opted-in calls: a sync call with no execution_id
      // sends no parent, so its error really does mean nothing will post.
      "An error from an async call, or from one you opted in, does not mean nothing will post",
      "the run may have started",
    ]) {
      assert.ok(shared.includes(phrase), `the description must say: ${phrase}`);
    }
    assert.ok(
      !shared.includes("answers with an async receipt"),
      "the default must not be scoped to receiving a receipt (an errored call may still report back)"
    );
  });

  it("T12: the same text from two turns shares one key but names two parents (Q4 characterization)", async () => {
    const a = await off.call("chat_with_agent", { agent_name: TARGET, message: "t12", ...PA }, HEADER);
    const b = await off.call("chat_with_agent", { agent_name: TARGET, message: "t12", ...PA }, HEADER_2);
    assert.equal(a.rec?.key, b.rec?.key);
    assert.equal(parentOf(a), HEADER);
    assert.equal(parentOf(b), HEADER_2);
  });

  it("T13a: typed id on sequential /chat → off/sequential_chat with the future-only note", async () => {
    const r = await off.call("chat_with_agent", { agent_name: TARGET, message: "t13a", execution_id: TYPED }, HEADER);
    assert.equal(r.out.response, "done");
    assert.equal(r.out.report_back, "off");
    assert.equal(r.out.report_back_reason, "sequential_chat");
    assert.equal(r.out.report_back_note, chatMod.REPORT_BACK_OFF_NOTES?.sequential_chat);
    assert.match(r.out.report_back_note, /parallel=true, async=true/);
    assert.match(r.out.report_back_note, /Do not re-send this one/);
  });

  it("T13b: queued_timeout and agent_busy keep the reason but never the note", async () => {
    off.setMode("replay409");
    const timeout = await off.call("chat_with_agent", { agent_name: TARGET, message: "t13b", execution_id: TYPED }, HEADER);
    assert.equal(timeout.out.status, "queued_timeout");
    assert.equal(timeout.out.report_back, "off");
    assert.equal(timeout.out.report_back_reason, "sequential_chat");
    assert.equal("report_back_note" in timeout.out, false);

    off.setMode("busy");
    const busy = await off.call("chat_with_agent", { agent_name: TARGET, message: "t13b", execution_id: TYPED }, HEADER);
    assert.equal(busy.out.status, "agent_busy");
    assert.equal(busy.out.report_back_reason, "sequential_chat");
    assert.equal("report_back_note" in busy.out, false);
  });

  it("T13c: a typed id in a manual session → off/manual_session", async () => {
    const r = await off.call("chat_with_agent", { agent_name: TARGET, message: "t13c", execution_id: TYPED }, "manual");
    assert.equal(r.out.report_back, "off");
    assert.equal(r.out.report_back_reason, "manual_session");
  });

  it("T13d: typed id on parallel and pull → requested, no note", async () => {
    const par = await off.call("chat_with_agent", { agent_name: TARGET, message: "t13d", ...PA, execution_id: TYPED }, HEADER);
    assert.equal(par.out.report_back, "requested");
    assert.equal("report_back_note" in par.out, false);
    assert.equal("report_back_reason" in par.out, false);
    const pulled = await pull.call("chat_with_agent", { agent_name: TARGET, message: "t13d", execution_id: TYPED }, HEADER);
    assert.equal(pulled.out.report_back, "requested");
    assert.equal("report_back_note" in pulled.out, false);
  });

  it("T13e: default-armed receipts (both tools, accepted and queued) → requested + the default note, nothing else changed", async () => {
    const cases: Array<[Harness, string, Record<string, unknown>, Mode]> = [
      [off, "chat_with_agent", { agent_name: TARGET, message: "t13e", ...PA }, "ok"],
      [off, DEDICATED, { message: "t13e", ...PA }, "ok"],
      [pull, "chat_with_agent", { agent_name: TARGET, message: "t13e" }, "ok"],
      [pull, DEDICATED, { message: "t13e" }, "ok"],
      [off, "chat_with_agent", { agent_name: TARGET, message: "t13e", ...PA }, "queued"],
    ];
    for (const [h, tool, args, mode] of cases) {
      const label = `${tool} ${JSON.stringify(args)} ${mode}`;
      h.setMode(mode);
      const armed = await h.call(tool, args, HEADER);
      assert.equal(armed.out.report_back, "requested", label);
      assert.equal(armed.out.report_back_note, chatMod.REPORT_BACK_DEFAULT_NOTE, label);
      assert.ok(String(armed.out.report_back_note).includes("never re-send or reword"), label);
      assert.equal("report_back_reason" in armed.out, false, label);
      h.setMode(mode);
      const optedOut = await h.call(tool, { ...args, execution_id: "manual" }, HEADER);
      assert.deepEqual(withoutFields(armed.out), optedOut.out, `${label}: every other key is today's receipt`);
    }
  });

  it("T13f: gate results, depth refusals and thrown errors never carry fields — default-armed or typed", async () => {
    for (const extra of [{}, { execution_id: TYPED }]) {
      const args = { agent_name: TARGET, message: "t13f", ...PA, ...extra };
      const armed = await off.call("chat_with_agent", args, HEADER);
      assert.equal(armed.out.report_back, "requested", "the same call, answered normally, is armed");

      off.setMode("gate");
      const gate = await off.call("chat_with_agent", args, HEADER);
      assert.equal(gate.out.status, "pending_approval");
      assertNoFields(gate.out, "gate");

      off.setMode("depth");
      const depth = await off.call("chat_with_agent", args, HEADER);
      assert.equal(depth.out.status, "inter_agent_depth_exceeded");
      assertNoFields(depth.out, "depth refusal");

      off.setMode("error");
      const thrown = await off.call("chat_with_agent", args, HEADER);
      assert.equal(thrown.isError, true);
      assert.equal(thrown.text.includes("report_back"), false);
    }
  });
});

describe("#3232 kill switch MCP_REPORT_BACK_ENABLED", () => {
  it("T14: reportBackEnabled:false sends no parent anywhere; typed → off/disabled, default → no fields", async () => {
    const h = await boot({ pull: false, reportBackEnabled: false });
    try {
      for (const [tool, base] of [
        ["chat_with_agent", { agent_name: TARGET, message: "t14", ...PA }],
        [DEDICATED, { message: "t14", ...PA }],
      ] as const) {
        const typed = await h.call(tool, { ...base, execution_id: TYPED }, HEADER);
        assert.equal(hasParent(typed), false, tool);
        assert.equal(typed.out.report_back, "off", tool);
        assert.equal(typed.out.report_back_reason, "disabled", tool);
        const dflt = await h.call(tool, base, HEADER);
        assert.equal(hasParent(dflt), false, tool);
        assertNoFields(dflt.out, `${tool} default, switched off`);
      }
    } finally {
      await h.stop();
    }
  });

  it("T20: createServer() with no explicit option reads the env", async () => {
    const saved = process.env.MCP_REPORT_BACK_ENABLED;
    try {
      process.env.MCP_REPORT_BACK_ENABLED = "false";
      const disabled = await boot({ pull: false });
      try {
        const r = await disabled.call("chat_with_agent", { agent_name: TARGET, message: "t20", ...PA }, HEADER);
        assert.equal(hasParent(r), false, "env false → no parent");
      } finally {
        await disabled.stop();
      }
      delete process.env.MCP_REPORT_BACK_ENABLED;
      const enabled = await boot({ pull: false });
      try {
        const r = await enabled.call("chat_with_agent", { agent_name: TARGET, message: "t20", ...PA }, HEADER);
        assert.equal(parentOf(r), HEADER, "env unset → the default sends the header");
      } finally {
        await enabled.stop();
      }
      // The switch is an operator's off-word, not one exact spelling: any case,
      // surrounding whitespace trimmed. Anything else (including empty) leaves it on.
      const rows: Array<[string, boolean]> = [
        ["False", false],
        [" OFF ", false],
        ["0", false],
        ["no", false],
        ["NO", false],
        ["true", true],
        ["", true],
        ["1", true],
        ["yes", true],
      ];
      const mismatches: string[] = [];
      for (const [raw, on] of rows) {
        process.env.MCP_REPORT_BACK_ENABLED = raw;
        const h = await boot({ pull: false });
        try {
          const r = await h.call("chat_with_agent", { agent_name: TARGET, message: "t20", ...PA }, HEADER);
          const got = parentOf(r);
          if (on ? got !== HEADER : hasParent(r)) {
            mismatches.push(`${JSON.stringify(raw)}: expected ${on ? "header sent" : "no parent"}, got parent=${JSON.stringify(got)}`);
          }
        } finally {
          await h.stop();
        }
      }
      assert.deepEqual(mismatches, [], "env value → switch state");
    } finally {
      if (saved === undefined) delete process.env.MCP_REPORT_BACK_ENABLED;
      else process.env.MCP_REPORT_BACK_ENABLED = saved;
    }
  });
});

describe("#3232 resolveReportBack (the rule, table-tested)", () => {
  type Row = {
    name: string;
    in: {
      route: "task-async" | "task-sync" | "pull" | "chat";
      typed?: string;
      header?: string;
      isSelfTask?: boolean;
      injectResult?: boolean;
      enabled?: boolean;
    };
    arm: "default" | "typed" | "opt_out" | "none";
    parent?: string;
    report?: "requested" | "off";
    reason?: string;
    note?: "default" | "off" | "none";
    log: string[];
  };
  const rows: Row[] = [
    { name: "async default", in: { route: "task-async", header: HEADER }, arm: "default", parent: HEADER, report: "requested", note: "default",
      log: [`caller_turn=${HEADER}`, "route=task-async", "arm=default", `parent=${HEADER}`, "source=header", "overridden=false", "report_back=requested"] },
    { name: "pull default", in: { route: "pull", header: HEADER }, arm: "default", parent: HEADER, report: "requested", note: "default",
      log: ["route=pull", "arm=default"] },
    { name: "sync never defaults", in: { route: "task-sync", header: HEADER }, arm: "none",
      log: ["route=task-sync", "arm=none", "parent=none", "source=none", "report_back=n/a"] },
    { name: "chat never defaults", in: { route: "chat", header: HEADER }, arm: "none", log: ["route=chat", "arm=none"] },
    { name: "no header, no default", in: { route: "task-async" }, arm: "none", log: ["caller_turn=none", "arm=none"] },
    { name: "manual header, no default", in: { route: "task-async", header: "manual" }, arm: "none", log: ["caller_turn=manual", "arm=none"] },
    { name: "typed manual opts out", in: { route: "task-async", typed: "manual", header: HEADER }, arm: "opt_out", log: ["arm=opt_out", "parent=none", "report_back=n/a"] },
    { name: "typed manual with whitespace opts out", in: { route: "pull", typed: "  manual ", header: HEADER }, arm: "opt_out", log: ["arm=opt_out"] },
    { name: "whitespace-only typed is nothing typed", in: { route: "task-async", typed: "   ", header: HEADER }, arm: "default", parent: HEADER, report: "requested", note: "default", log: ["arm=default"] },
    { name: "self-task + inject_result excluded", in: { route: "task-async", header: HEADER, isSelfTask: true, injectResult: true }, arm: "none", log: ["arm=none"] },
    { name: "self-task without inject_result defaults", in: { route: "task-async", header: HEADER, isSelfTask: true }, arm: "default", parent: HEADER, report: "requested", note: "default", log: ["arm=default"] },
    { name: "self-task + inject_result + typed opts in", in: { route: "task-async", header: HEADER, isSelfTask: true, injectResult: true, typed: TYPED }, arm: "typed", parent: HEADER, report: "requested", note: "none", log: ["arm=typed"] },
    { name: "typed, no header", in: { route: "task-async", typed: TYPED }, arm: "typed", parent: TYPED, report: "requested", note: "none",
      log: [`parent=${TYPED}`, "source=typed", "overridden=false", "report_back=requested"] },
    { name: "typed is trimmed", in: { route: "task-async", typed: `  ${TYPED} ` }, arm: "typed", parent: TYPED, report: "requested", note: "none", log: ["source=typed"] },
    { name: "header wins over typed", in: { route: "task-async", typed: TYPED, header: HEADER }, arm: "typed", parent: HEADER, report: "requested", note: "none",
      log: [`parent=${HEADER}`, "source=header", "overridden=true"] },
    { name: "typed equal to header is not overridden", in: { route: "task-async", typed: HEADER, header: HEADER }, arm: "typed", parent: HEADER, report: "requested", note: "none", log: ["overridden=false"] },
    { name: "sync typed", in: { route: "task-sync", typed: TYPED }, arm: "typed", parent: TYPED, report: "requested", note: "none", log: ["route=task-sync", "arm=typed"] },
    { name: "pull typed", in: { route: "pull", typed: TYPED }, arm: "typed", parent: TYPED, report: "requested", note: "none", log: ["route=pull"] },
    { name: "chat typed", in: { route: "chat", typed: TYPED, header: HEADER }, arm: "typed", report: "off", reason: "sequential_chat", note: "off",
      log: ["parent=none", "report_back=off:sequential_chat"] },
    { name: "manual_session beats sequential_chat", in: { route: "chat", typed: TYPED, header: "manual" }, arm: "typed", report: "off", reason: "manual_session", note: "off", log: ["report_back=off:manual_session"] },
    { name: "manual header drops a typed id", in: { route: "task-async", typed: TYPED, header: "manual" }, arm: "typed", report: "off", reason: "manual_session", note: "off", log: ["parent=none"] },
    { name: "malformed typed, no header", in: { route: "task-async", typed: PLACEHOLDER_ID }, arm: "typed", report: "off", reason: "invalid_execution_id", note: "off", log: ["report_back=off:invalid_execution_id"] },
    { name: "malformed typed, header supplies the turn", in: { route: "task-async", typed: PLACEHOLDER_ID, header: HEADER }, arm: "typed", parent: HEADER, report: "requested", note: "none", log: ["source=header"] },
    { name: "invalid_execution_id beats sequential_chat", in: { route: "chat", typed: PLACEHOLDER_ID }, arm: "typed", report: "off", reason: "invalid_execution_id", note: "off", log: [] },
    { name: "disabled typed", in: { route: "task-async", typed: TYPED, header: HEADER, enabled: false }, arm: "typed", report: "off", reason: "disabled", note: "off", log: ["parent=none", "report_back=off:disabled"] },
    { name: "disabled beats manual_session", in: { route: "chat", typed: TYPED, header: "manual", enabled: false }, arm: "typed", report: "off", reason: "disabled", note: "off", log: [] },
    { name: "disabled default: no parent, no fields", in: { route: "task-async", header: HEADER, enabled: false }, arm: "default", log: ["arm=default", "parent=none", "report_back=n/a"] },
  ];

  for (const row of rows) {
    it(row.name, () => {
      assert.equal(typeof chatMod.resolveReportBack, "function", "resolveReportBack must be exported from chat.ts");
      const d = chatMod.resolveReportBack({
        route: row.in.route,
        typed: row.in.typed,
        header: row.in.header,
        isSelfTask: row.in.isSelfTask ?? false,
        injectResult: row.in.injectResult ?? false,
        enabled: row.in.enabled ?? true,
        caller: CALLER,
        target: TARGET,
      });
      assert.equal(d.arm, row.arm);
      assert.equal(d.parentExecutionId, row.parent);
      if (row.report === undefined) {
        assert.equal(d.fields, undefined);
      } else {
        assert.equal(d.fields?.report_back, row.report);
        assert.equal(d.fields?.report_back_reason, row.reason);
        const note = d.fields?.report_back_note;
        if (row.note === "default") assert.equal(note, chatMod.REPORT_BACK_DEFAULT_NOTE);
        else if (row.note === "off") assert.equal(note, chatMod.REPORT_BACK_OFF_NOTES[row.reason as keyof typeof chatMod.REPORT_BACK_OFF_NOTES]);
        else assert.equal(note, undefined);
      }
      assert.ok(d.logLine.startsWith(`[Report-Back #3232] ${CALLER} -> ${TARGET} `), d.logLine);
      for (const part of row.log) assert.ok(d.logLine.includes(part), `log line lacks ${part}: ${d.logLine}`);
    });
  }

  it("every off note is future-only and never asks for this call again", () => {
    const notes = chatMod.REPORT_BACK_OFF_NOTES;
    assert.ok(notes, "REPORT_BACK_OFF_NOTES must be exported from chat.ts");
    for (const reason of ["disabled", "manual_session", "invalid_execution_id", "sequential_chat"] as const) {
      assert.match(notes[reason], /Do not re-send this one\.$/, reason);
    }
  });
});

describe("#3232 resolveReportBack over the full input product (independent oracle)", () => {
  // The picked rows above stay as the readable spec; this block walks every
  // combination and checks each cell against an oracle written from the route
  // table in channel-completion-report.md and the resolveReportBack doc comment,
  // not from the implementation's control flow: classify the inputs, decide the
  // parent per route, then the fields, then the reason by precedence.
  type Route = "task-async" | "task-sync" | "pull" | "chat";
  type In = {
    route: Route;
    typed: string | undefined;
    header: string | undefined;
    isSelfTask: boolean;
    injectResult: boolean;
    enabled: boolean;
  };
  type Expect = {
    arm: "default" | "typed" | "opt_out" | "none";
    parent: string | undefined;
    fields:
      | { report_back: "requested" | "off"; report_back_reason?: string; report_back_note?: string }
      | undefined;
    log: Record<string, string>;
  };

  /** Fixture facts: these ids have the shape of a real execution id; PLACEHOLDER_ID does not. */
  const WELL_FORMED_FIXTURES = new Set([TYPED, HEADER]);

  function oracle(i: In): Expect {
    // 1. Classify what was typed (trimmed) and what the platform header says.
    const t = (i.typed ?? "").trim();
    const typedKind =
      t === "" ? "nothing" : t === "manual" ? "manual" : WELL_FORMED_FIXTURES.has(t) ? "id" : "malformed";
    const headerKind = i.header === undefined ? "absent" : i.header === "manual" ? "manual" : "turn";

    // Route-table columns: typed `manual` is the opt-out; any other non-empty typed value
    // is an opt-in; with nothing typed, only an async dispatch (parallel+async or
    // pull-routed) from a real turn defaults on, unless a self-task already routes
    // its result into its own chat (inject_result).
    const asyncDispatch = i.route === "task-async" || i.route === "pull";
    const ownChatDestination = i.isSelfTask && i.injectResult;
    let arm: Expect["arm"];
    if (typedKind === "manual") arm = "opt_out";
    else if (typedKind !== "nothing") arm = "typed";
    else if (asyncDispatch && headerKind === "turn" && !ownChatDestination) arm = "default";
    else arm = "none";

    // 2. Parent per route. Sequential /chat carries none; the kill switch sends
    // none anywhere. A typed opt-in follows the header (Q3: a real turn wins; a
    // `manual` header means no execution); only with no header does the typed id
    // itself go, and only if it is well-formed.
    let parent: string | undefined;
    let source: "header" | "typed" | "none" = "none";
    const routeCarriesParent = i.route !== "chat";
    if (i.enabled && routeCarriesParent && arm === "default") {
      parent = i.header;
      source = "header";
    } else if (i.enabled && routeCarriesParent && arm === "typed") {
      if (headerKind === "turn") {
        parent = i.header;
        source = "header";
      } else if (headerKind === "absent" && typedKind === "id") {
        parent = t;
        source = "typed";
      }
    }

    // 3. Fields. Opt-out / none: nothing. Default: `requested` + the default note,
    // and nothing at all with the switch off (byte-identical receipts). Typed:
    // `requested` (no note) when a parent goes, else `off` + reason + its note.
    let fields: Expect["fields"];
    if (arm === "default" && i.enabled) {
      fields = { report_back: "requested", report_back_note: chatMod.REPORT_BACK_DEFAULT_NOTE };
    } else if (arm === "typed" && parent !== undefined) {
      fields = { report_back: "requested" };
    } else if (arm === "typed") {
      // 4. Reason precedence: disabled > manual_session > invalid_execution_id > sequential_chat.
      const candidates: Array<[boolean, string]> = [
        [!i.enabled, "disabled"],
        [headerKind === "manual", "manual_session"],
        [headerKind === "absent" && typedKind === "malformed", "invalid_execution_id"],
        [i.route === "chat", "sequential_chat"],
      ];
      const reason = candidates.find(([applies]) => applies)?.[1];
      if (reason === undefined) throw new Error(`oracle: typed call with no parent and no reason: ${JSON.stringify(i)}`);
      fields = {
        report_back: "off",
        report_back_reason: reason,
        report_back_note: chatMod.REPORT_BACK_OFF_NOTES[reason as keyof typeof chatMod.REPORT_BACK_OFF_NOTES],
      };
    }

    // 5. The log line: overridden = the agent typed a different id than the turn it is serving.
    const overridden = arm === "typed" && source === "header" && t !== i.header;
    const verdict = fields === undefined ? "n/a" : fields.report_back === "off" ? `off:${fields.report_back_reason}` : "requested";
    return {
      arm,
      parent,
      fields,
      log: {
        caller_turn: i.header ?? "none",
        route: i.route,
        arm,
        parent: parent ?? "none",
        source,
        overridden: String(overridden),
        report_back: verdict,
      },
    };
  }

  function parseLog(line: string): Record<string, string> | string {
    const prefix = `[Report-Back #3232] ${CALLER} -> ${TARGET} `;
    if (!line.startsWith(prefix)) return `bad prefix: ${line}`;
    const out: Record<string, string> = {};
    for (const tok of line.slice(prefix.length).split(" ")) {
      const eq = tok.indexOf("=");
      if (eq <= 0) return `bad token ${JSON.stringify(tok)}: ${line}`;
      out[tok.slice(0, eq)] = tok.slice(eq + 1);
    }
    return out;
  }

  it("every route × typed × header × self/inject × enabled cell matches the oracle", () => {
    const routes: Route[] = ["task-async", "task-sync", "pull", "chat"];
    const typeds = [undefined, "", "   ", "manual", "  manual ", TYPED, `  ${TYPED} `, PLACEHOLDER_ID, HEADER];
    const headers = [undefined, HEADER, "manual"];
    const selfInject: Array<[boolean, boolean]> = [[false, false], [false, true], [true, false], [true, true]];
    const enableds = [true, false];

    const mismatches: string[] = [];
    let cells = 0;
    for (const route of routes)
      for (const typed of typeds)
        for (const header of headers)
          for (const [isSelfTask, injectResult] of selfInject)
            for (const enabled of enableds) {
              cells++;
              const i: In = { route, typed, header, isSelfTask, injectResult, enabled };
              const label = JSON.stringify(i);
              let want: Expect;
              try {
                want = oracle(i);
              } catch (e) {
                mismatches.push(String(e));
                continue;
              }
              const got = chatMod.resolveReportBack({ ...i, caller: CALLER, target: TARGET });
              const diffs: string[] = [];
              if (got.arm !== want.arm) diffs.push(`arm ${got.arm} != ${want.arm}`);
              if (got.parentExecutionId !== want.parent) diffs.push(`parent ${got.parentExecutionId} != ${want.parent}`);
              for (const f of FIELDS) {
                const g = got.fields?.[f];
                const w = want.fields?.[f];
                // Identity with the exported constants, not a copy of their text.
                if (g !== w) diffs.push(`${f} ${JSON.stringify(g)} != ${JSON.stringify(w)}`);
              }
              if ((got.fields === undefined) !== (want.fields === undefined)) {
                diffs.push(`fields ${got.fields === undefined ? "absent" : "present"} != ${want.fields === undefined ? "absent" : "present"}`);
              }
              const log = parseLog(got.logLine);
              if (typeof log === "string") diffs.push(log);
              else
                for (const [k, v] of Object.entries(want.log)) {
                  if (log[k] !== v) diffs.push(`log ${k}=${log[k]} != ${v}`);
                }
              if (diffs.length) mismatches.push(`${label}: ${diffs.join("; ")}`);
            }
    assert.equal(cells, 4 * 9 * 3 * 4 * 2, "the product was walked in full");
    assert.deepEqual(mismatches, [], `${mismatches.length} of ${cells} cells disagree with the oracle`);
  });
});
