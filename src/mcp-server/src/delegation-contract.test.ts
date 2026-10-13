/**
 * abilityai/trinity-enterprise#568 — the delegation contract reaches the model.
 *
 * The contract (`delegation_contract.ts`) is the same text the platform prompt
 * carries (tests/unit/test_ent568_delegation_contract.py pins the parity). This
 * file pins the MCP half on what a model actually reads:
 *
 *   - what `tools/list` PUBLISHES for `chat_with_agent`, a dynamic
 *     `chat_with_<agent>` tool (registered through the real reconciler and the
 *     real `addToolWithAudit`), `fan_out` and `send_message` — over a real
 *     transport, because a description built and then dropped by the
 *     registration wrapper reads fine in a unit test;
 *   - that each stays inside Claude Code's 2,048-char description cap
 *     (`CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH`): the model never sees text past
 *     it, which is how the old description's async advice went unread;
 *   - the receipt `message` every route answers with — the text the model reads
 *     at the moment it decides whether to re-send.
 *
 * Runner: node:test → `node --import tsx --test src/*.test.ts`.
 */
import { strict as assert } from "node:assert";
import { after, afterEach, before, beforeEach, describe, it } from "node:test";
import { createServer as createHttpServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

import { createServer } from "./server.js";
import { TrinityClient } from "./client.js";
import { createChatTools } from "./tools/chat.js";
import { makeDedicatedChatTool, startExposedToolsReconciler } from "./tools/dynamic-agents.js";
import {
  DELEGATION_CONTRACT,
  DELEGATION_CONTRACT_LINES,
  DELEGATION_RULE,
} from "./delegation_contract.js";

/** Claude Code's default cap on an MCP tool description; the rest is cut. */
const CLAUDE_CODE_DESCRIPTION_CAP = 2048;

const CALLER = "alpha";
const AGENT_KEY = "trinity_mcp_test_agent_key";
/** What the backend's poll hands the reconciler — distinct from the fallback, so a
 *  factory that ignores it cannot pass. */
const BACKEND_LINE = 'Chat directly with the "helper-bot" agent (backend line).';
/** The factory's own line when the backend's is blank. */
const FALLBACK_LINE = 'Chat directly with the "helper-bot" agent.';
/** Every tool the contract tells a caller to use — each must be published to it. */
const CONTRACT_TOOLS = [
  "chat_with_agent", "fan_out", "get_execution_result", "get_fan_out_result",
  "list_recent_executions", "set_reminder", "subscribe_to_event",
];

describe("ent#568 the shared text", () => {
  it("is the array joined, long enough to be the contract", () => {
    assert.equal(DELEGATION_CONTRACT, DELEGATION_CONTRACT_LINES.join("\n"));
    assert.ok(DELEGATION_CONTRACT.length > 1000, `contract is only ${DELEGATION_CONTRACT.length} chars`);
    assert.ok(DELEGATION_CONTRACT.includes(DELEGATION_RULE), "DELEGATION_RULE must be the contract's own sentence");
  });
});

describe("ent#568 what tools/list publishes (real server, real transport)", () => {
  let backend: Server;
  let mcpServer: { stop: () => Promise<void> };
  let reconciler: { stop: () => void } | undefined;
  let mcp: Client;
  const tools = new Map<string, { description?: string; inputSchema: any }>();

  before(async () => {
    backend = createHttpServer((req, res) => {
      req.resume();
      req.on("end", () => {
        res.writeHead(200, { "Content-Type": "application/json" });
        if ((req.url ?? "") === "/api/mcp/validate") {
          return res.end(JSON.stringify({
            valid: true,
            key_id: "key-agent-1",
            user_id: "owner",
            user_email: "owner@example.com",
            key_name: "agent-key",
            scope: "agent",
            agent_name: CALLER,
          }));
        }
        res.end("{}");
      });
    });
    await new Promise<void>((r) => backend.listen(0, "127.0.0.1", () => r()));
    const backendUrl = `http://127.0.0.1:${(backend.address() as AddressInfo).port}`;

    const probe = createHttpServer();
    await new Promise<void>((r) => probe.listen(0, "127.0.0.1", () => r()));
    const mcpPort = (probe.address() as AddressInfo).port;
    await new Promise<void>((r) => probe.close(() => r()));

    const built = await createServer({
      trinityApiUrl: backendUrl,
      requireApiKey: true,
      port: mcpPort,
      internalApiSecret: "test-internal-secret",
    });
    await built.server.start({
      transportType: "httpStream",
      httpStream: { port: mcpPort, host: "127.0.0.1" },
    });
    mcpServer = built.server;

    // The real reconciler, wired to the real registration handles — the path
    // index.ts runs — with the backend's poll stubbed to one exposed agent.
    const handle = startExposedToolsReconciler({
      trinityApiUrl: backendUrl,
      internalSecret: "test-internal-secret",
      client: built.client,
      requireApiKey: built.requireApiKey,
      agentChatPullEnabled: built.agentChatPullEnabled,
      reportBackEnabled: built.reportBackEnabled,
      registerDynamicTool: built.registerDynamicTool,
      unregisterDynamicTool: built.unregisterDynamicTool,
      operatorOnly: built.operatorOnly,
      builtinToolNames: built.builtinToolNames,
      intervalMs: 1_000_000,
      runImmediately: false,
      fetchImpl: (async () => ({
        ok: true,
        status: 200,
        json: async () => ({
          agents: [{ agent_name: "helper-bot", tool_name: "chat_with_helper_bot", description: BACKEND_LINE }],
        }),
      })) as unknown as typeof fetch,
    });
    reconciler = handle;
    await handle.syncOnce();

    mcp = new Client({ name: CALLER, version: "1.0.0" });
    await mcp.connect(new StreamableHTTPClientTransport(new URL(`http://127.0.0.1:${mcpPort}/mcp`), {
      requestInit: { headers: { Authorization: `Bearer ${AGENT_KEY}` } },
    }));
    for (const t of (await mcp.listTools()).tools) tools.set(t.name, t as any);
  });

  after(async () => {
    reconciler?.stop();
    await mcp?.close();
    await mcpServer?.stop();
    await new Promise<void>((r) => backend.close(() => r()));
  });

  const published = (name: string): string => {
    const t = tools.get(name);
    assert.ok(t, `${name} is not in tools/list (have ${tools.size} tools)`);
    return t.description ?? "";
  };

  it("chat_with_agent carries the whole contract, inside the cap", () => {
    const d = published("chat_with_agent");
    assert.ok(d.includes(DELEGATION_CONTRACT), "chat_with_agent's description lacks the contract");
    assert.ok(d.length <= CLAUDE_CODE_DESCRIPTION_CAP, `chat_with_agent is ${d.length} chars — Claude Code cuts it`);
  });

  it("a dynamic chat_with_<agent> tool carries the backend's line and the whole contract, inside the cap", () => {
    const d = published("chat_with_helper_bot");
    assert.ok(d.startsWith(`${BACKEND_LINE}\n\n`), "the backend's line must lead");
    assert.ok(d.includes(DELEGATION_CONTRACT), "the dedicated tool's description lacks the contract");
    assert.ok(d.length <= CLAUDE_CODE_DESCRIPTION_CAP, `chat_with_helper_bot is ${d.length} chars`);
  });

  it("every chat_with_* tool takes the parallel and async arguments the contract teaches", () => {
    const chatTools = [...tools.keys()].filter((n) => n.startsWith("chat_with_"));
    assert.deepEqual(chatTools.sort(), ["chat_with_agent", "chat_with_helper_bot"]);
    for (const name of chatTools) {
      const props = tools.get(name)!.inputSchema.properties ?? {};
      for (const arg of ["parallel", "async"]) {
        assert.ok(arg in props, `${name} does not take \`${arg}\`, yet its description teaches \`parallel=true, async=true\``);
      }
    }
  });

  it("every tool the contract names is published to the caller", () => {
    for (const name of CONTRACT_TOOLS) assert.ok(tools.has(name), `the contract names ${name}, which this caller cannot see`);
  });

  it("fan_out and send_message carry the rule and point at the contract, inside the cap", () => {
    for (const name of ["fan_out", "send_message"]) {
      const d = published(name);
      assert.ok(d.includes(DELEGATION_RULE), `${name} lacks the rule sentence`);
      assert.ok(d.includes("chat_with_agent"), `${name} does not point at the contract`);
      assert.ok(d.length <= CLAUDE_CODE_DESCRIPTION_CAP, `${name} is ${d.length} chars`);
    }
    assert.ok(published("fan_out").includes("get_fan_out_result"));
    assert.ok(published("send_message").includes("dedup_label"));
  });

  it("the mode details moved into parameter descriptions, which are not cut, and the REST poll line is gone", () => {
    const props = tools.get("chat_with_agent")!.inputSchema.properties;
    assert.match(props.parallel.description, /compact_metadata/);
    assert.match(props.async.description, /get_execution_result/);
    assert.match(props.timeout_seconds.description, /agent-side run/);
    const everything = JSON.stringify(tools.get("chat_with_agent"));
    assert.ok(!everything.includes("GET /api/agents"), "chat_with_agent still tells the model to call REST");
  });
});

describe("ent#568 the dedicated-tool factory", () => {
  for (const blank of ["", "   "]) {
    it(`a ${JSON.stringify(blank)} owner description falls back to the default line`, () => {
      const tool = makeDedicatedChatTool({} as TrinityClient, false, false, "helper-bot", "chat_with_helper_bot", blank);
      assert.equal(tool.description, `${FALLBACK_LINE}\n\n${DELEGATION_CONTRACT}`);
    });
  }
});

// ---------------------------------------------------------------------------
// Receipt messages — every route
// ---------------------------------------------------------------------------

const realFetch = globalThis.fetch;
const realTimeout = process.env.MCP_CHAT_TIMEOUT_MS;

type Handler = (url: string, init: RequestInit) => Promise<Response> | Response;

function stubFetch(routes: Array<[string, Handler]>) {
  globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
    const url = String(input);
    const hit = routes.find(([prefix]) => url.includes(prefix));
    if (!hit) throw new Error(`unstubbed fetch: ${url}`);
    return hit[1](url, init ?? {});
  }) as typeof fetch;
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function hangUntilAbort(_url: string, init: RequestInit): Promise<Response> {
  return new Promise((_resolve, reject) => {
    (init.signal as AbortSignal).addEventListener("abort", () => {
      const err = new Error("This operation was aborted");
      err.name = "AbortError";
      reject(err);
    });
  });
}

/** A live row, stamped when the stub serves it — the recovery window is ~10 s. */
function liveRow() {
  return {
    id: "ex-live",
    status: "running",
    triggered_by: "mcp",
    source_mcp_key_id: "key-1",
    message: "do the thing",
    started_at: new Date().toISOString(),
  };
}

const READ_NOT_RESEND = 'Do not re-send: read the outcome with get_execution_result(agent_name="agent-a", execution_id=';
const readNotResendFor = (id: string) => `${READ_NOT_RESEND}"${id}").`;

function assertTeaches(message: string, executionId: string) {
  assert.ok(
    message.includes(`${READ_NOT_RESEND}"${executionId}")`),
    `receipt message does not say how to read it instead of re-sending: ${message}`,
  );
  assert.ok(!message.includes("concurrent-duplicate"), `receipt message names a guard that does not exist: ${message}`);
  assert.ok(!message.includes("GET /api"), `receipt message tells an MCP caller to call REST: ${message}`);
}

describe("ent#568 the receipt message a timed-out or replayed call answers with", () => {
  beforeEach(() => { process.env.MCP_CHAT_TIMEOUT_MS = "20"; });
  afterEach(() => {
    globalThis.fetch = realFetch;
    if (realTimeout === undefined) delete process.env.MCP_CHAT_TIMEOUT_MS;
    else process.env.MCP_CHAT_TIMEOUT_MS = realTimeout;
  });

  it("chat() and task() timeouts answer with the SAME receipt, which teaches the read", async () => {
    stubFetch([
      ["/chat", hangUntilAbort],
      ["/task", hangUntilAbort],
      ["/executions?limit=50", () => json([liveRow()])],
    ]);
    const client = new TrinityClient("http://backend:8000", "tok");
    const viaChat = (await client.chat("agent-a", "do the thing", undefined, { keyId: "key-1" })) as any;
    const viaTask = (await client.task("agent-a", "do the thing", undefined, undefined, { keyId: "key-1" })) as any;
    assert.equal(viaChat.status, "queued_timeout");
    assert.deepEqual(viaChat, viaTask);
    assertTeaches(viaChat.message, "ex-live");
    assert.equal(
      viaChat.message,
      `MCP-server timeout (20ms) on chat_with_agent — the task is still running on 'agent-a'. ` +
        `${readNotResendFor("ex-live")} A timeout is not a failure (#914).`,
    );
  });

  it("a 409 replay of an in-flight call says it was already dispatched and how to read it", async () => {
    stubFetch([["/task", () => json({ detail: { execution_id: "ex-409" } }, 409)]]);
    const out = (await new TrinityClient("http://backend:8000", "tok").task("agent-a", "m")) as any;
    assert.equal(out.status, "queued_timeout");
    assertTeaches(out.message, "ex-409");
    assert.equal(
      out.message,
      `This exact call was already dispatched to 'agent-a' and has no result to replay yet. ` +
        `${readNotResendFor("ex-409")} A reworded re-send would dispatch a SECOND execution (#2661).`,
    );
  });
});

describe("ent#568 the async receipt chat_with_agent answers with", () => {
  type Seen = { method: "chat" | "task"; options?: any };

  /** What the backend writes for each receipt it authors — REST wording and all. */
  const BACKEND: Record<string, Record<string, unknown>> = {
    accepted: { status: "accepted", execution_id: "ex_9", agent_name: "agent-a", async_mode: true,
      message: "Task accepted. Poll GET /api/agents/{name}/executions/{execution_id} for results." },
    queued: { status: "queued", execution_id: "ex_9", agent_name: "agent-a", async_mode: true,
      message: "Agent at capacity; task queued. Poll GET /api/agents/agent-a/executions/ex_9 for results." },
    // The snapshot a sync /task stores when its backlog long-poll gives up
    // (chat_execution_service) — replayed verbatim for 24 h on an exact repeat.
    queued_timeout: { status: "queued_timeout", execution_id: "ex_9", task_execution_id: "ex_9",
      agent_name: "agent-a", async_mode: true,
      message: "Sync task on agent 'agent-a' did not complete within 600s. Execution ex_9 may still be " +
        "running; poll GET /api/agents/agent-a/executions/ex_9." },
  };

  /** The lead each one is rewritten to: liveness-neutral, because a replay can come after the end. */
  const LEAD: Record<string, string> = {
    accepted: "Accepted by 'agent-a' as ex_9 — it may still be running or already done.",
    queued: "Queued on 'agent-a' as ex_9 while it was at capacity — it may have run since.",
    queued_timeout: "The platform stopped waiting for ex_9 on 'agent-a' — it may still be running.",
  };

  /** A client whose /task answers with `answer`; /chat answers a plain reply. */
  function toolAnswering(answer: Record<string, unknown>, pullEnabled: boolean, seen: Seen[]) {
    const fake: Partial<TrinityClient> = {
      getBaseUrl: () => "http://localhost:8000",
      isAgentPermitted: async () => true,
      getAgentAccessInfo: async () => ({ owner: "u1", is_shared: true }) as any,
      chat: async () => {
        seen.push({ method: "chat" });
        return { response: "a plain reply" } as any;
      },
      task: async (_name: string, _m: string, options?: any) => {
        seen.push({ method: "task", options });
        return { ...answer } as any;
      },
    };
    return createChatTools(fake as unknown as TrinityClient, false, pullEnabled).chatWithAgent;
  }

  const agentSession = { session: { scope: "agent", agentName: "caller", userId: "u1", keyId: "k1", keyName: "kn" } };
  const SESSIONS: Record<string, unknown> = {
    "an agent": agentSession,
    "a person (user key)": { session: { scope: "user", userId: "u1", keyId: "k2", keyName: "kn" } },
    "a self-task": { session: { scope: "agent", agentName: "agent-a", userId: "u1", keyId: "k3", keyName: "kn" } },
  };

  for (const status of ["accepted", "queued"] as const) {
    for (const [who, session] of Object.entries(SESSIONS)) {
      it(`parallel async, ${who}: the backend's '${status}' receipt is re-worded for an MCP caller`, async () => {
        const seen: Seen[] = [];
        const out = JSON.parse(await toolAnswering(BACKEND[status], false, seen).execute(
          { agent_name: "agent-a", message: "m", parallel: true, async: true }, session,
        ));
        assert.equal(seen[0]?.method, "task");
        assert.deepEqual({ ...out, message: undefined }, { ...BACKEND[status], message: undefined }, "only `message` may change");
        assert.equal(out.message, `${LEAD[status]} ${readNotResendFor("ex_9")}`);
        assertTeaches(out.message, "ex_9");
      });
    }
  }

  it("a replay of the backend's own sync-/task queued_timeout snapshot is re-worded too", async () => {
    const seen: Seen[] = [];
    const out = JSON.parse(await toolAnswering(BACKEND.queued_timeout, false, seen).execute(
      { agent_name: "agent-a", message: "m", parallel: true }, agentSession,
    ));
    assert.equal(out.status, "queued_timeout");
    assert.equal(out.message, `${LEAD.queued_timeout} ${readNotResendFor("ex_9")}`);
  });

  it("the MCP server's own queued_timeout (already carrying the read line) is left as it is", async () => {
    const own = { status: "queued_timeout", agent: "agent-a", execution_id: "ex_9",
      message: `MCP-server timeout (20ms) on chat_with_agent — the task is still running on 'agent-a'. ` +
        `${readNotResendFor("ex_9")} A timeout is not a failure (#914).` };
    const out = JSON.parse(await toolAnswering(own, false, []).execute(
      { agent_name: "agent-a", message: "m", parallel: true }, agentSession,
    ));
    assert.deepEqual(out, own);
  });

  it("the #946 pull-routed sequential call (flag ON) gets the same wording", async () => {
    const seen: Seen[] = [];
    const out = JSON.parse(await toolAnswering(BACKEND.accepted, true, seen).execute(
      { agent_name: "agent-a", message: "m" }, agentSession,
    ));
    assert.equal(seen[0]?.method, "task");
    assert.equal(seen[0]?.options?.async_mode, true);
    assert.equal(out.message, `${LEAD.accepted} ${readNotResendFor("ex_9")}`);
  });

  it("#3245: a replayed receipt keeps its idempotent_replay marker through the re-wording", async () => {
    const out = JSON.parse(await toolAnswering({ ...BACKEND.accepted, idempotent_replay: true }, false, [])
      .execute({ agent_name: "agent-a", message: "m", parallel: true, async: true }, agentSession));
    assert.equal(out.idempotent_replay, true);
    assert.equal(out.message, `${LEAD.accepted} ${readNotResendFor("ex_9")}`);
  });

  it("a completed reply is passed through untouched", async () => {
    const seen: Seen[] = [];
    const out = JSON.parse(await toolAnswering(BACKEND.accepted, false, seen).execute(
      { agent_name: "agent-a", message: "m" }, agentSession,
    ));
    assert.equal(seen[0]?.method, "chat");
    assert.deepEqual(out, { response: "a plain reply" });
  });

  it("a completed sync parallel reply is passed through untouched too", async () => {
    const completed = { response: "done", execution_id: "ex_2", status: "success" };
    const fake: Partial<TrinityClient> = {
      getBaseUrl: () => "http://localhost:8000",
      isAgentPermitted: async () => true,
      getAgentAccessInfo: async () => ({ owner: "u1", is_shared: true }) as any,
      task: async () => completed as any,
    };
    const tool = createChatTools(fake as unknown as TrinityClient, false, false).chatWithAgent;
    const out = JSON.parse(await tool.execute({ agent_name: "agent-a", message: "m", parallel: true }, agentSession));
    assert.deepEqual(out, completed);
  });
});
