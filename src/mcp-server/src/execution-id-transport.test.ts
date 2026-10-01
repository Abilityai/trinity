/**
 * #2392 — X-Trinity-Execution-Id is read per request, not per session.
 *
 * Real fastmcp server + real StreamableHTTP client, ONE session, two
 * send_message calls whose POSTs carry different header values. A handler
 * test cannot see this: freshness depends on fastmcp re-running
 * `authenticate` on every POST (see inline-auth-transport.test.ts, #2035).
 */
import { strict as assert } from "node:assert";
import { after, before, describe, it } from "node:test";
import { createServer as createHttpServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

import { createServer } from "./server.js";

describe("#2392 execution id header is per-request (real transport)", () => {
  let backend: Server;
  let mcpServer: { stop: () => Promise<void> };
  let mcpUrl: URL;
  const sentBodies: any[] = [];

  before(async () => {
    backend = createHttpServer((req, res) => {
      let body = "";
      req.on("data", (c) => (body += c));
      req.on("end", () => {
        const send = (payload: unknown) => {
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify(payload));
        };
        if (req.url === "/api/mcp/validate") {
          return send({ valid: true, key_id: "k", user_id: "1", key_name: "agent-key", scope: "agent", agent_name: "atlas" });
        }
        if (req.url === "/api/agents/atlas/messages") {
          sentBodies.push(JSON.parse(body));
          return send({ success: true, channel: "auto", message_id: "m" });
        }
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

  it("two calls on one session carry their own header value", async () => {
    let current = "exec-1";
    const transport = new StreamableHTTPClientTransport(mcpUrl, {
      requestInit: { headers: { Authorization: "Bearer trinity_mcp_test" } },
      // Stamp the CURRENT value on every POST — the header changes mid-session.
      fetch: (url, init) => {
        const headers = new Headers(init?.headers);
        headers.set("X-Trinity-Execution-Id", current);
        return fetch(url, { ...init, headers });
      },
    });
    const client = new Client({ name: "t", version: "1.0.0" });
    await client.connect(transport);
    try {
      const call = () =>
        client.callTool({
          name: "send_message",
          arguments: { recipient_email: "user@example.com", text: "hi", execution_id: "stale-param" },
        });
      await call();
      current = "manual";
      await call();
    } finally {
      await client.close();
    }
    assert.deepEqual(sentBodies.map((b) => b.execution_id), ["exec-1", "manual"]);
  });
});
