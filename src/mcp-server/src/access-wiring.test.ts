/**
 * abilityai/trinity-enterprise#628 — the gate is WIRED, not merely defined.
 *
 * `access.test.ts` proves `withAgentAccess` denies, and `tools/loops.test.ts` proves
 * the composition it builds BY HAND denies. Neither executes the one line that makes
 * the mechanism real: `server.ts::addToolWithAudit` choosing to wrap an `enforce`
 * row's `execute` before handing it to `withAudit`. Build that wrapper there and throw
 * it away, and both files stay green while `run_agent_loop` is ungated again — the
 * reviewer's mutation on PR #2826, the #2811 class (the reaction tested, the wiring
 * that calls it not).
 *
 * So this file drives the tool the way an agent does: a real `createServer` in key
 * mode, a real MCP client presenting an AGENT-scoped key over the streamable-HTTP
 * transport, and a stub backend that answers the key validation and the permission
 * edge read and — the observable — COUNTS every `POST /api/agents/<target>/loops` it
 * receives. Without an edge the count stays at zero and the caller reads the denial;
 * with an edge the loop starts. Pattern: `inline-auth-transport.test.ts` (#2035).
 *
 * Runner: node:test → `node --import tsx --test src/*.test.ts`.
 */
import { strict as assert } from "node:assert";
import { after, before, describe, it } from "node:test";
import { createServer as createHttpServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

import { createServer } from "./server.js";

const CALLER = "alpha";
const SIBLING = "bravo";
const AGENT_KEY = "trinity_mcp_test_agent_key";

describe("ent#628 run_agent_loop is gated where server.ts registers it (real transport)", () => {
  let backend: Server;
  let mcpServer: { stop: () => Promise<void> };
  let mcpUrl: URL;
  /** The edge the stub backend reports for the caller — mutated per test. */
  let permitted: string[] = [];
  /** Every loop start the backend received: the side effect the gate must prevent. */
  const loopPosts: string[] = [];
  /** Permission-edge reads — a self loop must not pay one. */
  let permissionReads = 0;

  before(async () => {
    process.env.INTERNAL_API_SECRET = "test-internal-secret";

    backend = createHttpServer((req, res) => {
      let body = "";
      req.on("data", (c) => (body += c));
      req.on("end", () => {
        const send = (status: number, payload: unknown) => {
          res.writeHead(status, { "Content-Type": "application/json" });
          res.end(JSON.stringify(payload));
        };
        const url = req.url ?? "";
        if (url === "/api/mcp/validate") {
          return send(200, {
            valid: true,
            key_id: "key-agent-1",
            user_id: "owner",
            user_email: "owner@example.com",
            key_name: "alpha-key",
            scope: "agent",
            agent_name: CALLER,
          });
        }
        if (url === `/api/agents/${CALLER}/permissions`) {
          permissionReads++;
          return send(200, {
            source_agent: CALLER,
            permitted_agents: permitted.map((name) => ({ name })),
            available_agents: [],
          });
        }
        const loop = url.match(/^\/api\/agents\/([^/]+)\/loops$/);
        if (loop && req.method === "POST") {
          loopPosts.push(loop[1]);
          return send(202, {
            loop_id: `loop_${loopPosts.length}`,
            agent_name: loop[1],
            status: "queued",
            max_runs: 1,
            on_failure: "abort",
          });
        }
        return send(200, {}); // health probe, audit posts, anything else
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
    await server.start({
      transportType: "httpStream",
      httpStream: { port: mcpPort, host: "127.0.0.1" },
    });
    mcpServer = server;
    mcpUrl = new URL(`http://127.0.0.1:${mcpPort}/mcp`);
  });

  after(async () => {
    await mcpServer?.stop();
    await new Promise<void>((r) => backend.close(() => r()));
  });

  /** A client holding agent A's own key — what a playbook call looks like on the wire. */
  const asAgent = async (name: string) => {
    const client = new Client({ name, version: "1.0.0" });
    const transport = new StreamableHTTPClientTransport(mcpUrl, {
      requestInit: { headers: { Authorization: `Bearer ${AGENT_KEY}` } },
    });
    await client.connect(transport);
    const call = async (tool: string, args: Record<string, unknown>) => {
      const r: any = await client.callTool({ name: tool, arguments: args });
      return JSON.parse(r.content.map((c: any) => c.text).join("\n"));
    };
    return { client, call };
  };

  const START = { message: "Reply with the single word: pong", max_runs: 1 };

  /** Each case starts from a clean slate so one defect reads as one red, not three. */
  const reset = (edge: string[]) => {
    permitted = edge;
    loopPosts.length = 0;
  };

  it("an agent key with no edge is refused at the REGISTERED tool, and no loop starts", async () => {
    reset([]);
    const { client, call } = await asAgent("628-no-edge");
    const out = await call("run_agent_loop", { agent_name: SIBLING, ...START });
    assert.equal(out.success, false, `expected a refusal, got: ${JSON.stringify(out)}`);
    assert.equal(out.error, "Access denied");
    assert.match(out.reason, new RegExp(`Agent '${CALLER}' is not permitted to communicate with '${SIBLING}'`));
    assert.equal(out.loop_id, undefined);
    assert.deepEqual(loopPosts, [], "the backend received a loop start the gate should have stopped");
    await client.close();
  });

  it("the same key with an edge starts the loop on the sibling", async () => {
    reset([SIBLING]);
    const { client, call } = await asAgent("628-edge");
    const out = await call("run_agent_loop", { agent_name: SIBLING, ...START });
    assert.equal(out.success, true, `expected the loop to start, got: ${JSON.stringify(out)}`);
    assert.equal(out.loop_id, "loop_1");
    assert.deepEqual(loopPosts, [SIBLING]);
    await client.close();
  });

  it("a loop on itself needs no edge and reads no permissions", async () => {
    reset([]);
    const before = permissionReads;
    const { client, call } = await asAgent("628-self");
    const out = await call("run_agent_loop", { ...START });
    assert.equal(out.success, true, `expected a self loop to start, got: ${JSON.stringify(out)}`);
    assert.deepEqual(loopPosts, [CALLER]);
    assert.equal(permissionReads, before, "a self loop paid a permission-edge read");
    await client.close();
  });
});
