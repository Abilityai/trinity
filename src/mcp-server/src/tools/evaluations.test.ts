/**
 * `get_evaluation_summary` (abilityai/trinity-enterprise#937)
 *
 * Driven the way an agent drives it: a real `createServer` in key mode, a real
 * MCP client over streamable HTTP, and a stub backend that records every
 * `GET /api/agents/<name>/evaluations/summary` it receives. What the proxy
 * decides by itself is WHICH route it calls for WHOM — an agent key with no
 * `agent_name` reads itself, a sibling without a permission edge is refused
 * before the backend is asked, a person's key must name the agent. The
 * projection (own rows only, no judge scores for a machine) is the backend's
 * and is pinned in tests/unit/test_937_routes.py.
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { strict as assert } from "node:assert";
import { after, before, describe, it } from "node:test";
import { createServer as createHttpServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

import { createServer } from "../server.js";
import { TOOL_ACCESS_POLICY } from "../access.js";
import { classifyEvaluationError, createEvaluationTools } from "./evaluations.js";
import { TrinityClient } from "../client.js";

const CALLER = "alpha";
const SIBLING = "bravo";
const KEYS: Record<string, { scope: string; agent_name?: string }> = {
  trinity_mcp_eval_agent: { scope: "agent", agent_name: CALLER },
  trinity_mcp_eval_user: { scope: "user" },
};

describe("ent#937 get_evaluation_summary over the real transport", () => {
  let backend: Server;
  let mcpServer: { stop: () => Promise<void> };
  let mcpUrl: URL;
  let permitted: string[] = [];
  const summaryGets: string[] = [];

  before(async () => {
    backend = createHttpServer((req, res) => {
      req.resume();
      req.on("end", () => {
        const send = (status: number, payload: unknown) => {
          res.writeHead(status, { "Content-Type": "application/json" });
          res.end(JSON.stringify(payload));
        };
        const url = req.url ?? "";
        if (url === "/api/mcp/validate") {
          const bearer = String(req.headers.authorization ?? "").replace(/^Bearer /, "");
          const key = KEYS[bearer] ?? KEYS.trinity_mcp_eval_user;
          return send(200, {
            valid: true, key_id: `key-${key.scope}`, user_id: "owner",
            user_email: "owner@example.com", key_name: bearer, ...key,
          });
        }
        const reach = url.match(new RegExp(`^/api/agents/${CALLER}/reach/([^/?]+)$`));
        if (reach) return send(200, { reachable: permitted.includes(decodeURIComponent(reach[1])) });
        const summary = url.match(/^\/api\/agents\/([^/]+)\/evaluations\/summary(\?.*)?$/);
        if (summary && req.method === "GET") {
          summaryGets.push(url);
          return send(200, {
            agent_name: decodeURIComponent(summary[1]), since: "s", until: "u", truncated: false,
            summary: { evaluated: 2, completed: 2, graded: 2, passed: 1, judged: 0, per_check_fail: { c1: 1 } },
            failed: [], failed_truncated: false,
          });
        }
        return send(200, {});
      });
    });
    await new Promise<void>((r) => backend.listen(0, "127.0.0.1", () => r()));
    const backendPort = (backend.address() as AddressInfo).port;
    const probe = createHttpServer();
    await new Promise<void>((r) => probe.listen(0, "127.0.0.1", () => r()));
    const mcpPort = (probe.address() as AddressInfo).port;
    await new Promise<void>((r) => probe.close(() => r()));
    const { server } = await createServer({
      trinityApiUrl: `http://127.0.0.1:${backendPort}`, requireApiKey: true, port: mcpPort,
    });
    await server.start({ transportType: "httpStream", httpStream: { port: mcpPort, host: "127.0.0.1" } });
    mcpServer = server;
    mcpUrl = new URL(`http://127.0.0.1:${mcpPort}/mcp`);
  });

  after(async () => {
    await mcpServer?.stop();
    await new Promise<void>((r) => backend.close(() => r()));
  });

  async function call(key: string, args: Record<string, unknown>) {
    const client = new Client({ name: "eval-937", version: "1.0.0" });
    await client.connect(new StreamableHTTPClientTransport(mcpUrl, {
      requestInit: { headers: { Authorization: `Bearer ${key}` } },
    }));
    try {
      const listed = await client.listTools();
      const tool = listed.tools.find((t) => t.name === "get_evaluation_summary");
      const result: any = await client.callTool({ name: "get_evaluation_summary", arguments: args });
      return { tool, body: JSON.parse(result.content[0].text) };
    } finally {
      await client.close();
    }
  }

  it("an agent key with no agent_name reads its OWN summary route", async () => {
    const before = summaryGets.length;
    const { tool, body } = await call("trinity_mcp_eval_agent", {});
    assert.ok(tool, "get_evaluation_summary is not advertised to an agent session");
    assert.equal(body.success, true);
    assert.equal(body.agent_name, CALLER);
    assert.deepEqual(summaryGets.slice(before), [`/api/agents/${CALLER}/evaluations/summary?period=week`]);
  });

  it("forwards period / since / until / caller to the route", async () => {
    const before = summaryGets.length;
    await call("trinity_mcp_eval_agent", {
      period: "custom", since: "2026-10-01T00:00:00Z", until: "2026-10-08T00:00:00Z", caller: SIBLING,
    });
    const [url] = summaryGets.slice(before);
    const q = new URL(url, "http://x").searchParams;
    assert.equal(new URL(url, "http://x").pathname, `/api/agents/${CALLER}/evaluations/summary`);
    assert.equal(q.get("period"), "custom");
    assert.equal(q.get("since"), "2026-10-01T00:00:00Z");
    assert.equal(q.get("until"), "2026-10-08T00:00:00Z");
    assert.equal(q.get("caller"), SIBLING);
  });

  it("an agent key naming a sibling with no edge is refused before the backend is asked", async () => {
    permitted = [];
    const before = summaryGets.length;
    const { body } = await call("trinity_mcp_eval_agent", { agent_name: SIBLING });
    assert.equal(body.success, false);
    assert.equal(body.error, "Access denied");
    assert.equal(summaryGets.length, before, "the sibling's summary was fetched without an edge");
  });

  it("with an edge, the sibling's route is called (the backend then narrows to the pair)", async () => {
    permitted = [SIBLING];
    const before = summaryGets.length;
    const { body } = await call("trinity_mcp_eval_agent", { agent_name: SIBLING });
    assert.equal(body.success, true);
    assert.deepEqual(summaryGets.slice(before), [`/api/agents/${SIBLING}/evaluations/summary?period=week`]);
    permitted = [];
  });

  it("a person's key must name the agent — no backend call without one", async () => {
    const before = summaryGets.length;
    const { body } = await call("trinity_mcp_eval_user", {});
    assert.equal(body.success, false);
    assert.match(body.error, /agent identity/);
    assert.equal(summaryGets.length, before);
    const named = await call("trinity_mcp_eval_user", { agent_name: SIBLING });
    assert.equal(named.body.success, true);
  });
});

describe("ent#937 get_evaluation_summary contract", () => {
  const tool = createEvaluationTools(new TrinityClient("http://backend:8000"), true).getEvaluationSummary;

  it("is gated on agent_name at registration", () => {
    assert.deepEqual(TOOL_ACCESS_POLICY.get_evaluation_summary, { kind: "enforce", param: "agent_name" });
  });

  it("states the self/pair scope and that agents never see judge scores, inside the cap", () => {
    assert.ok(tool.description.length <= 2048, `${tool.description.length} chars`);
    assert.match(tool.description, /ITS OWN runs/);
    assert.match(tool.description, /NEVER sees judge scores/);
    for (const key of ["quality_mean", "judge_human_agreement", "agreement_n"]) {
      assert.ok(tool.description.includes(key), key);
    }
  });

  it("maps backend failures to flags", () => {
    assert.deepEqual(classifyEvaluationError("HTTP 404: not found"), { not_authorized: true });
    assert.deepEqual(classifyEvaluationError("HTTP 422: invalid_period"), { invalid: true });
    assert.deepEqual(classifyEvaluationError("HTTP 503"), { retryable: true });
  });
});
