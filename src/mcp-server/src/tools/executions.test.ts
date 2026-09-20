/**
 * abilityai/trinity-enterprise#653 — `search_executions` is a system-agent / human capability.
 *
 * The backend route refuses agent-scoped keys (`reject_agent_principal`) and is
 * entitlement-gated; this file pins the MCP half: the per-tool `canAccess` is an
 * ALLOW-LIST (#848 — an unknown or absent principal fails closed), an agent-scoped
 * session is refused in-tool with the audited denial envelope and never reaches
 * the client, permitted scopes are proxied with their parameters intact, and an
 * absent (OSS) or unentitled module answers `available:false` rather than
 * "no results". The real client's query-string building is driven through the
 * real `TrinityClient` with `request` stubbed.
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createExecutionTools } from "./executions.js";
import { ApiError, TrinityClient } from "../client.js";
import { TOOL_ACCESS_POLICY, AGENT_TARGET_PARAMS } from "../access.js";
import type { ExecutionSearchParams, ExecutionSearchResult, McpAuthContext } from "../types.js";

const EMPTY: ExecutionSearchResult = { query: "x", mode: "substring", fields: ["message", "response", "error"], hours: 24, count: 0, hits: [] };

function makeFake(opts: { throws?: ApiError; result?: ExecutionSearchResult } = {}) {
  const calls: ExecutionSearchParams[] = [];
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    searchExecutions: async (params: ExecutionSearchParams) => {
      calls.push(params);
      if (opts.throws) throw opts.throws;
      return opts.result ?? EMPTY;
    },
  };
  return { fake: fake as TrinityClient, calls };
}

const session = (scope: string, agentName?: string): { session: McpAuthContext; outcome?: unknown } => ({
  session: { scope, agentName, userId: "owner", keyId: "k1", keyName: "kn" } as unknown as McpAuthContext,
});

describe("search_executions — canAccess is an allow-list of system and user scope", () => {
  const { fake } = makeFake();
  const tool = createExecutionTools(fake, true).searchExecutions as any;

  it("admits system and user scope", () => {
    assert.equal(tool.canAccess({ scope: "system" }), true);
    assert.equal(tool.canAccess({ scope: "user" }), true);
  });

  it("hides the tool from agent-scoped and connector sessions", () => {
    assert.equal(tool.canAccess({ scope: "agent", agentName: "a" }), false);
    assert.equal(tool.canAccess({ scope: "connector" }), false);
  });

  it("fails CLOSED on an unknown, missing or null principal (#848, #2323)", () => {
    assert.equal(tool.canAccess({ scope: "ops" }), false);
    assert.equal(tool.canAccess({}), false);
    assert.equal(tool.canAccess(undefined), false);
    assert.equal(tool.canAccess(null), false);
  });
});

describe("search_executions — in-tool gate and proxying", () => {
  it("refuses an agent-scoped session before touching the client, with the audited envelope", async () => {
    const { fake, calls } = makeFake();
    const tool = createExecutionTools(fake, false).searchExecutions;
    const ctx = session("agent", "some-agent");
    const out = JSON.parse(await tool.execute({ query: "deploy" }, ctx));
    assert.equal(out.error, "Access denied");
    assert.match(out.reason, /system agent and user-scoped keys only/);
    assert.equal(calls.length, 0, "the client must never be called for a refused principal");
    assert.deepEqual((ctx as any).outcome?.kind, "denied");
  });

  it("refuses an unknown scope (fails closed)", async () => {
    const { fake, calls } = makeFake();
    const tool = createExecutionTools(fake, false).searchExecutions;
    const out = JSON.parse(await tool.execute({ query: "deploy" }, session("ops")));
    assert.equal(out.error, "Access denied");
    assert.equal(calls.length, 0);
  });

  it("proxies a system-scoped session with the parameters intact and explicit defaults", async () => {
    const { fake, calls } = makeFake();
    const tool = createExecutionTools(fake, false).searchExecutions;
    const out = JSON.parse(await tool.execute(
      { query: "deploy", agents: ["alpha", "beta"], fields: ["response"], hours: 168, limit: 5 },
      session("system")
    ));
    assert.equal(out.available, true);
    assert.equal(calls.length, 1);
    assert.deepEqual(calls[0], {
      query: "deploy", mode: "substring", agents: ["alpha", "beta"], fields: ["response"], status: undefined,
      triggered_by: undefined, hours: 168, limit: 5, offset: 0, context: 120,
    });
  });

  it("passes mode=regex through untouched", async () => {
    const { fake, calls } = makeFake();
    const tool = createExecutionTools(fake, false).searchExecutions;
    await tool.execute({ query: "deploy.*prod", mode: "regex" }, session("system"));
    assert.equal(calls[0].mode, "regex");
  });

  it("proxies a user-scoped session", async () => {
    const { fake, calls } = makeFake();
    const tool = createExecutionTools(fake, false).searchExecutions;
    const out = JSON.parse(await tool.execute({ query: "deploy" }, session("user")));
    assert.equal(out.available, true);
    assert.equal(calls.length, 1);
  });

  it("admits an absent session only in dev mode (no API key required)", async () => {
    const dev = makeFake();
    const devOut = JSON.parse(await createExecutionTools(dev.fake, false).searchExecutions.execute({ query: "q" }, undefined));
    assert.equal(devOut.available, true);
    assert.equal(dev.calls.length, 1);

    const prod = makeFake();
    const prodOut = JSON.parse(await createExecutionTools(prod.fake, true).searchExecutions.execute({ query: "q" }, undefined));
    assert.equal(prodOut.error, "Access denied");
    assert.equal(prod.calls.length, 0);
  });

  it("answers available:false (not 'no results') when the module is absent — OSS build 404", async () => {
    const { fake } = makeFake({ throws: new ApiError(404, "Not Found") });
    const tool = createExecutionTools(fake, false).searchExecutions;
    const out = JSON.parse(await tool.execute({ query: "deploy" }, session("system")));
    assert.equal(out.available, false);
    assert.equal(out.count, 0);
    assert.match(out.message, /not available on this platform/);
    assert.equal(out.detail, "Not Found");
  });

  it("answers available:false with the gate's sentence when unentitled — 403", async () => {
    const { fake } = makeFake({ throws: new ApiError(403, '{"detail":"license required: execution_search"}') });
    const tool = createExecutionTools(fake, false).searchExecutions;
    const out = JSON.parse(await tool.execute({ query: "deploy" }, session("user")));
    assert.equal(out.available, false);
    assert.match(out.message, /not enabled for this caller/);
    assert.match(out.detail, /execution_search/);
  });

  it("lets any other failure propagate (a 500 is not 'unavailable')", async () => {
    const { fake } = makeFake({ throws: new ApiError(500, "boom") });
    const tool = createExecutionTools(fake, false).searchExecutions;
    await assert.rejects(() => tool.execute({ query: "deploy" }, session("system")), /boom/);
  });
});

describe("search_executions — wiring", () => {
  it("has a TOOL_ACCESS_POLICY row naming the backend fence", () => {
    const row = TOOL_ACCESS_POLICY.search_executions as any;
    assert.equal(row.kind, "baselined");
    assert.match(row.owner, /reject_agent_principal/);
  });

  it("`agents` is an agent-target parameter name (a future none-row tool cannot slip it past the checker)", () => {
    assert.ok(AGENT_TARGET_PARAMS.has("agents"));
  });

  it("the real client builds the enterprise route's query string", async () => {
    const client = new TrinityClient("http://localhost:8000");
    let seen: { method: string; path: string } | undefined;
    (client as any).request = async (method: string, path: string) => {
      seen = { method, path };
      return EMPTY;
    };
    await client.searchExecutions({
      query: "50% done_now", mode: "regex", agents: ["alpha", "beta"], fields: ["message", "error"],
      status: "failed", triggered_by: "mcp", hours: 720, limit: 5, offset: 10, context: 60,
    });
    assert.ok(seen);
    assert.equal(seen!.method, "GET");
    const url = new URL(seen!.path, "http://x");
    assert.equal(url.pathname, "/api/enterprise/execution-search");
    assert.equal(url.searchParams.get("q"), "50% done_now");
    assert.equal(url.searchParams.get("mode"), "regex");
    assert.equal(url.searchParams.get("agents"), "alpha,beta");
    assert.equal(url.searchParams.get("fields"), "message,error");
    assert.equal(url.searchParams.get("status"), "failed");
    assert.equal(url.searchParams.get("triggered_by"), "mcp");
    assert.equal(url.searchParams.get("hours"), "720");
    assert.equal(url.searchParams.get("limit"), "5");
    assert.equal(url.searchParams.get("offset"), "10");
    assert.equal(url.searchParams.get("context"), "60");
  });

  it("omits unset optionals from the query string", async () => {
    const client = new TrinityClient("http://localhost:8000");
    let path = "";
    (client as any).request = async (_m: string, p: string) => { path = p; return EMPTY; };
    await client.searchExecutions({ query: "q" });
    const url = new URL(path, "http://x");
    assert.deepEqual([...url.searchParams.keys()], ["q"]);
  });
});
