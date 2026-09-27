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
 * #2807: the same stub backend now RECORDS every `POST /api/internal/audit` the
 * server fires, so the last two cases prove the composition end to end over the
 * wire: a refused call's row says `denied`, and the permitted call that follows
 * on the SAME session carries no stale marker. The audit POST is fire-and-forget
 * and may land after the client already holds its result — and a row awaited by
 * `(tool, index-after-length)` is not an identity: the previous test's late row
 * matched first (#2952). So every call made through `asAgent().call` drains ITS
 * OWN row before returning, and the row is checked for position (the call
 * counter) AND identity (the session's own bearer, which the stub echoes back as
 * `key_name`). Subtests run one at a time (node:test default — do not add
 * `concurrency`), and one call is one row: a change to `withAudit`'s row count
 * breaks this file on purpose.
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
  /** Every audit row the MCP server posted (#2807): the label the operator reads. */
  const auditRows: any[] = [];
  /** Tool calls made through `asAgent().call` — the count the recorder must have caught up to before the next call. */
  let calls = 0;

  before(async () => {
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
            // #2952: echo the session's bearer so every row carries who called.
            key_name: String(req.headers.authorization ?? "").replace(/^Bearer /, ""),
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
        if (url === "/api/internal/audit" && req.method === "POST") {
          auditRows.push(JSON.parse(body));
          return send(200, { event_id: `ev_${auditRows.length}`, status: "logged" });
        }
        return send(200, {}); // health probe, anything else
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
      // #2807: the audit wrapper posts to `trinityApiUrl` with this secret — the
      // stub above records the rows, so the label is observable here.
      internalApiSecret: "test-internal-secret",
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
    // #2952: a per-session bearer — the stub echoes it back as `key_name`, so a row
    // carries which session made the call and the finder can check identity.
    const key = `${AGENT_KEY}-${name}`;
    const transport = new StreamableHTTPClientTransport(mcpUrl, {
      requestInit: { headers: { Authorization: `Bearer ${key}` } },
    });
    await client.connect(transport);
    const call = async (tool: string, args: Record<string, unknown>) => {
      assert.equal(
        auditRows.length,
        calls,
        `${auditRows.length} audit rows after ${calls} calls — the row count per call changed (audit.ts) or a call bypassed this helper (#2952): ${JSON.stringify(auditRows)}`,
      );
      const at = calls++;
      const r: any = await client.callTool({ name: tool, arguments: args });
      // Drain BEFORE parsing: an `isError` text would throw at the parse and skip the
      // drain, leaving this call's row in flight for the next call to misread.
      const row = await auditRowAt(at, tool, key);
      const out = JSON.parse(r.content.map((c: any) => c.text).join("\n"));
      return { out, row };
    };
    return { client, call };
  };

  const START = { message: "Reply with the single word: pong", max_runs: 1 };

  /** A clean slate per case; a dropped audit row now reds every case that made a call — by design (#2952). */
  const reset = (edge: string[]) => {
    permitted = edge;
    loopPosts.length = 0;
  };

  /**
   * The audit row at index `at` — THIS call's row, awaited because the POST is
   * fire-and-forget, and cross-checked for tool and session so a row from another
   * call can never be returned in its place (#2952).
   */
  const auditRowAt = async (at: number, tool: string, key: string, timeoutMs = 5000): Promise<any> => {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (auditRows.length > at) {
        const row = auditRows[at];
        assert.equal(row?.details?.tool, tool, `row ${at} is not this call's tool: ${JSON.stringify(row)}`);
        assert.equal(row?.mcp_key_name, key, `row ${at} is not this session's: ${JSON.stringify(row)}`);
        return row;
      }
      await new Promise((r) => setTimeout(r, 25));
    }
    throw new Error(`no audit row for call ${at} (${tool}) arrived within ${timeoutMs}ms; rows seen: ${JSON.stringify(auditRows)}`);
  };

  it("an agent key with no edge is refused at the REGISTERED tool, and no loop starts", async () => {
    reset([]);
    const { client, call } = await asAgent("628-no-edge");
    const { out } = await call("run_agent_loop", { agent_name: SIBLING, ...START });
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
    const { out } = await call("run_agent_loop", { agent_name: SIBLING, ...START });
    assert.equal(out.success, true, `expected the loop to start, got: ${JSON.stringify(out)}`);
    assert.equal(out.loop_id, "loop_1");
    assert.deepEqual(loopPosts, [SIBLING]);
    await client.close();
  });

  it("a loop on itself needs no edge and reads no permissions", async () => {
    reset([]);
    const before = permissionReads;
    const { client, call } = await asAgent("628-self");
    const { out } = await call("run_agent_loop", { ...START });
    assert.equal(out.success, true, `expected a self loop to start, got: ${JSON.stringify(out)}`);
    assert.deepEqual(loopPosts, [CALLER]);
    assert.equal(permissionReads, before, "a self loop paid a permission-edge read");
    await client.close();
  });

  it("#2807: a refused loop start is audited as a refusal, not as a successful call", async () => {
    reset([]);
    const { client, call } = await asAgent("2807-refused");
    const { out, row } = await call("run_agent_loop", { agent_name: SIBLING, ...START });
    assert.equal(out.error, "Access denied");

    assert.equal(row.details.success, false, `the refusal was audited as a success: ${JSON.stringify(row.details)}`);
    assert.equal(row.details.denied, true);
    assert.match(String(row.details.error), new RegExp(`Agent '${CALLER}' is not permitted to communicate with '${SIBLING}'`));
    assert.equal(row.target_id, SIBLING);
    assert.equal(row.actor_agent_name, CALLER);
    assert.equal(row.mcp_scope, "agent");
    assert.deepEqual(loopPosts, [], "the backend received a loop start the gate should have stopped");
    await client.close();
  });

  it("#2807: deny then allow on ONE session leaves no stale marker on the permitted call", async () => {
    reset([]);
    const { client, call } = await asAgent("2807-same-session");

    const { out: refused, row: first } = await call("run_agent_loop", { agent_name: SIBLING, ...START });
    assert.equal(refused.error, "Access denied");
    assert.equal(first.details.denied, true);

    permitted = [SIBLING];
    const { out: ok, row: second } = await call("run_agent_loop", { agent_name: SIBLING, ...START });
    assert.equal(ok.success, true, `expected the loop to start, got: ${JSON.stringify(ok)}`);
    assert.equal(second.details.success, true, `the permitted call inherited a stale refusal: ${JSON.stringify(second.details)}`);
    assert.equal(second.details.denied, undefined);
    assert.equal(second.details.error, undefined);
    assert.deepEqual(loopPosts, [SIBLING]);
    await client.close();
  });
});

// ---------------------------------------------------------------------------
// trinity-enterprise#611 — get_my_ask reads as the KEY's own agent, over the
// real transport. The stub backend answers the key validation by bearer (an
// agent key, a person's user-scoped key, the orchestrator's system key) and
// RECORDS every readback request: identity must come from the key, and a key
// with no agent identity must never reach the backend.
// ---------------------------------------------------------------------------

describe("trinity-enterprise#611 get_my_ask acts as the key's agent (real transport)", () => {
  let backend: Server;
  let mcpServer: { stop: () => Promise<void> };
  let mcpUrl: URL;
  const readbacks: string[] = [];

  const KEYS: Record<string, { scope: string; agent_name?: string }> = {
    "trinity_mcp_611_agent": { scope: "agent", agent_name: CALLER },
    "trinity_mcp_611_user": { scope: "user" },
    "trinity_mcp_611_system": { scope: "system", agent_name: "trinity-system" },
  };

  before(async () => {
    backend = createHttpServer((req, res) => {
      req.on("data", () => {});
      req.on("end", () => {
        const send = (status: number, payload: unknown) => {
          res.writeHead(status, { "Content-Type": "application/json" });
          res.end(JSON.stringify(payload));
        };
        const url = req.url ?? "";
        if (url === "/api/mcp/validate") {
          const key = String(req.headers.authorization ?? "").replace(/^Bearer /, "");
          const row = KEYS[key];
          if (!row) return send(401, { valid: false });
          return send(200, {
            valid: true, key_id: `id-${key}`, user_id: "owner",
            user_email: "owner@example.com", key_name: key, ...row,
          });
        }
        const readback = url.match(/^\/api\/agents\/([^/]+)\/operator-queue\/([^/]+)$/);
        if (readback && req.method === "GET") {
          readbacks.push(`${readback[1]}/${readback[2]}`);
          return send(200, { request_id: readback[2], agent_name: readback[1], disposition: "expired" });
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

  const callAs = async (key: string, args: Record<string, unknown>) => {
    const client = new Client({ name: key, version: "1.0.0" });
    const transport = new StreamableHTTPClientTransport(mcpUrl, {
      requestInit: { headers: { Authorization: `Bearer ${key}` } },
    });
    await client.connect(transport);
    const r: any = await client.callTool({ name: "get_my_ask", arguments: args });
    await client.close();
    return JSON.parse(r.content.map((c: any) => c.text).join("\n"));
  };

  it("an agent key reads its own ask — the backend is asked about the key's agent, and no other", async () => {
    readbacks.length = 0;
    const out = await callAs("trinity_mcp_611_agent", { request_id: "deploy-42" });
    assert.equal(out.disposition, "expired");
    assert.deepEqual(readbacks, [`${CALLER}/deploy-42`]);
  });

  it("an agent cannot aim it at a sibling — an agent_name argument is not part of the tool", async () => {
    readbacks.length = 0;
    await callAs("trinity_mcp_611_agent", { request_id: "deploy-42", agent_name: SIBLING }).catch(() => undefined);
    assert.ok(!readbacks.some((r) => r.startsWith(`${SIBLING}/`)), `the backend was asked about ${SIBLING}: ${readbacks}`);
  });

  it("a person's user-scoped key is refused before the backend is asked anything", async () => {
    readbacks.length = 0;
    const out = await callAs("trinity_mcp_611_user", { request_id: "deploy-42" });
    assert.equal(out.success, false);
    assert.match(out.error, /agent identity/);
    assert.deepEqual(readbacks, []);
  });

  it("the orchestrator's system key reads as trinity-system", async () => {
    readbacks.length = 0;
    await callAs("trinity_mcp_611_system", { request_id: "fleet-7" });
    assert.deepEqual(readbacks, ["trinity-system/fleet-7"]);
  });
});
