/**
 * #4173 — the by-id execution reads carry the turn they are made from.
 *
 * In a turn for a shared person or a visitor the backend narrows what the
 * agent's key reads, and lets one thing through by id: the turn's own run and
 * the runs that turn started. It decides that from `X-Trinity-Execution-Id`,
 * so a read that reached the backend without the header would answer "not
 * found" for a run the caller holds a receipt for.
 *
 * Real fastmcp server + real StreamableHTTP client (the
 * `execution-id-transport.test.ts` shape). The backend stub records the header
 * on the three reads `get_execution_result` and `get_fan_out_result` make, and
 * answers them the way the backend answers a granted read: 200, still marked
 * `X-Trinity-Read-Narrowed`.
 *
 * This pins wiring that exists (`turnFetch` adds the header to every backend
 * call): it fails if one of these reads stops going through that door.
 *
 * Runner: node:test → `node --import tsx --test src/*.test.ts`.
 */
import { strict as assert } from "node:assert";
import { after, before, describe, it } from "node:test";
import { createServer as createHttpServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

import { READ_NARROWED_MESSAGES } from "./read-narrowing.js";
import { createServer } from "./server.js";

const TURN = "exec-turn-4173";
const RUN = {
  id: "run-1", agent_name: "atlas", status: "success", message: "work", response: "the answer",
  started_at: "2026-11-27T10:00:00Z", triggered_by: "agent",
};
const BATCH = { agent_name: "atlas", fan_out_id: "fo_abc", status: "completed", total: 1, completed: 1, failed: 0, running: 0, results: [] };

describe("#4173 by-id execution reads send the turn id (real transport)", () => {
  let backend: Server;
  let mcpServer: { stop: () => Promise<void> };
  let mcpUrl: URL;
  const seen: Array<{ url: string; turn: string | undefined }> = [];

  before(async () => {
    backend = createHttpServer((req, res) => {
      req.resume();
      req.on("end", () => {
        const send = (payload: unknown, narrowed = false) => {
          res.writeHead(200, {
            "Content-Type": "application/json",
            ...(narrowed ? { "X-Trinity-Read-Narrowed": "turn_person" } : {}),
          });
          res.end(JSON.stringify(payload));
        };
        if (req.url === "/api/mcp/validate") {
          return send({ valid: true, key_id: "k", user_id: "1", key_name: "agent-key", scope: "agent", agent_name: "atlas" });
        }
        const turn = req.headers["x-trinity-execution-id"];
        seen.push({ url: req.url ?? "", turn: Array.isArray(turn) ? turn[0] : turn });
        if (req.url === "/api/agents/atlas/executions/run-1") return send(RUN, true);
        if (req.url === "/api/agents/atlas/executions/run-1/log") {
          return send({ execution_id: "run-1", agent_name: "atlas", log: [{ type: "result" }] }, true);
        }
        if (req.url === "/api/agents/atlas/fan-out/fo_abc") return send(BATCH, true);
        return send({});
      });
    });
    await new Promise<void>((r) => backend.listen(0, "127.0.0.1", () => r()));
    const backendPort = (backend.address() as AddressInfo).port;

    const probe = createHttpServer();
    await new Promise<void>((r) => probe.listen(0, "127.0.0.1", () => r()));
    const mcpPort = (probe.address() as AddressInfo).port;
    await new Promise<void>((r) => probe.close(() => r()));

    const { server } = await createServer({
      trinityApiUrl: `http://127.0.0.1:${backendPort}`,
      requireApiKey: true,
      port: mcpPort,
    });
    await server.start({ transportType: "httpStream", httpStream: { port: mcpPort, host: "127.0.0.1" } });
    mcpServer = server;
    mcpUrl = new URL(`http://127.0.0.1:${mcpPort}/mcp`);
  });

  after(async () => {
    await mcpServer?.stop();
    await new Promise<void>((r) => backend.close(() => r()));
  });

  async function call(name: string, args: Record<string, unknown>): Promise<Record<string, any>> {
    const transport = new StreamableHTTPClientTransport(mcpUrl, {
      requestInit: { headers: { Authorization: "Bearer trinity_mcp_test" } },
      fetch: (url, init) => {
        const headers = new Headers(init?.headers);
        headers.set("X-Trinity-Execution-Id", TURN);
        return fetch(url, { ...init, headers });
      },
    });
    const client = new Client({ name: "t", version: "1.0.0" });
    await client.connect(transport);
    try {
      const result = (await client.callTool({ name, arguments: args })) as { content: Array<{ text: string }> };
      return JSON.parse(result.content[0].text);
    } finally {
      await client.close();
    }
  }

  const turnOf = (url: string) => seen.filter((s) => s.url === url).map((s) => s.turn);

  it("get_execution_result sends it on the detail read and on the log read", async () => {
    const out = await call("get_execution_result", { agent_name: "atlas", execution_id: "run-1", include_log: true });
    assert.equal(out.response, "the answer");
    assert.deepEqual(out.execution_log, [{ type: "result" }]);
    assert.deepEqual(turnOf("/api/agents/atlas/executions/run-1"), [TURN]);
    assert.deepEqual(turnOf("/api/agents/atlas/executions/run-1/log"), [TURN]);
  });

  it("a granted read is shown with the result and the narrowed note together", async () => {
    const out = await call("get_execution_result", { agent_name: "atlas", execution_id: "run-1" });
    assert.equal(out.status, "success");
    assert.deepEqual(out.narrowed, { code: "turn_person", message: READ_NARROWED_MESSAGES.turn_person });
  });

  it("get_fan_out_result sends it on the batch read", async () => {
    const out = await call("get_fan_out_result", { agent_name: "atlas", fan_out_id: "fo_abc" });
    assert.equal(out.status, "completed");
    assert.equal(out.narrowed.code, "turn_person");
    assert.deepEqual(turnOf("/api/agents/atlas/fan-out/fo_abc"), [TURN]);
  });
});
