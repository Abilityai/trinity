/**
 * #2035 — the regression test that actually pins the bug class.
 *
 * A real `StreamableHTTPClientTransport` client, a real fastmcp server, three
 * sequential tool calls over three separate HTTP POSTs. That last property is
 * the entire point: the bug lives in the gap BETWEEN requests, where
 * `mcp-proxy` re-runs `authenticate` and `FastMCPSession#updateAuth` replaces
 * the context. A handler-level test — invoking `execute` with a context object
 * the test owns — cannot observe it, which is exactly how #848 shipped green
 * with 30 passing tests.
 *
 * If this file becomes awkward to maintain, fix it. Do NOT replace it with a
 * handler test; that would restore the blind spot it exists to remove.
 *
 * The Trinity backend is stubbed on purpose. Whether the backend verifies a
 * code correctly has its own suite (`tests/unit/test_848_mcp_inline_auth.py`);
 * what is under test here is solely whether the MCP server REMEMBERS a verified
 * identity from one request to the next.
 */
import { strict as assert } from "node:assert";
import { after, before, describe, it } from "node:test";
import { createServer as createHttpServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

import { createServer } from "./server.js";

const EMAIL = "someone@example.com";
const AGENT = "demo-agent";

describe("#2035 keyless sign-in survives across requests (real transport)", () => {
  let backend: Server;
  let mcpServer: { stop: () => Promise<void> };
  let mcpUrl: URL;

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
        switch (req.url) {
          case "/api/internal/mcp-auth/request":
            return send(202, { status: "ok" });
          case "/api/internal/mcp-auth/verify":
            return send(200, {
              verified: true,
              username: EMAIL,
              agents: [{ name: AGENT, description: "a demo agent" }],
            });
          case "/api/internal/mcp-auth/playbooks":
            return send(200, [{ name: "do-thing", description: "does the thing" }]);
          default:
            return send(200, {});
        }
      });
    });
    await new Promise<void>((r) => backend.listen(0, "127.0.0.1", () => r()));
    const backendPort = (backend.address() as AddressInfo).port;

    // Bind, read the port, release — then hand it to fastmcp, which takes a
    // port rather than a listener.
    const probe = createHttpServer();
    await new Promise<void>((r) => probe.listen(0, "127.0.0.1", () => r()));
    const mcpPort = (probe.address() as AddressInfo).port;
    await new Promise<void>((r) => probe.close(() => r()));

    const { server } = await createServer({
      trinityApiUrl: `http://127.0.0.1:${backendPort}`,
      requireApiKey: true,
      inlineAuthEnabled: true,
      port: mcpPort,
    });
    // MUST be awaited — an un-awaited start races the first client connect.
    await server.start({
      transportType: "httpStream",
      httpStream: { port: mcpPort, host: "127.0.0.1" },
    });
    mcpServer = server;
    mcpUrl = new URL(`http://127.0.0.1:${mcpPort}/mcp`);
  });

  after(async () => {
    // Both are required or the runner never exits: fastmcp holds the listener,
    // and the stub holds its own.
    await mcpServer?.stop();
    await new Promise<void>((r) => backend.close(() => r()));
  });

  /** A keyless client — the connector config verbatim, no Authorization. */
  const connect = async (name: string) => {
    const client = new Client({ name, version: "1.0.0" });
    await client.connect(new StreamableHTTPClientTransport(mcpUrl));
    const text = async (tool: string, args: Record<string, unknown>) => {
      const r: any = await client.callTool({ name: tool, arguments: args });
      return r.content.map((c: any) => c.text).join("\n");
    };
    return { client, text };
  };

  it("rejects a tool call before login", async () => {
    const { client, text } = await connect("2035-pre-login");
    const out = await text("list_playbooks", { agent: AGENT });
    assert.match(out, /login_required/);
    await client.close();
  });

  it("advertises only the keyless tool set", async () => {
    const { client } = await connect("2035-visibility");
    const names = (await client.listTools()).tools.map((t) => t.name).sort();
    assert.deepEqual(names, [
      "ask",
      "list_playbooks",
      "request_login",
      "run_playbook",
      "verify_login",
    ]);
    await client.close();
  });

  it("carries the pending email from request_login into verify_login", async () => {
    const { client, text } = await connect("2035-pending");
    await text("request_login", { email: EMAIL });
    // No `email` argument: this passes only if the address written by
    // request_login survived into the NEXT request. Pre-fix: no_pending_login.
    const verified = await text("verify_login", { code: "123456" });
    assert.doesNotMatch(verified, /no_pending_login/, `pending email lost: ${verified}`);
    assert.match(verified, /signed_in_as/);
    await client.close();
  });

  it("authorizes the call AFTER verify_login — the #2035 regression", async () => {
    const { client, text } = await connect("2035-main");
    await text("request_login", { email: EMAIL });
    const verified = await text("verify_login", { code: "123456" });
    assert.match(verified, /signed_in_as/, `did not sign in: ${verified}`);

    // Pre-fix this returned login_required: the context holding verifiedEmail
    // had already been replaced by the time this POST was authenticated.
    const playbooks = await text("list_playbooks", { agent: AGENT });
    assert.doesNotMatch(
      playbooks,
      /login_required/,
      `session forgotten between calls: ${playbooks}`
    );
    assert.match(playbooks, /do-thing/);
    await client.close();
  });

  it("does not leak one session's identity into another", async () => {
    const signedIn = await connect("2035-iso-a");
    const anonymous = await connect("2035-iso-b");

    const verified = await signedIn.text("verify_login", { email: EMAIL, code: "123456" });
    assert.match(verified, /signed_in_as/);

    const out = await anonymous.text("list_playbooks", { agent: AGENT });
    assert.match(
      out,
      /login_required/,
      "an unauthenticated session inherited another session's identity"
    );

    await signedIn.client.close();
    await anonymous.client.close();
  });
});
