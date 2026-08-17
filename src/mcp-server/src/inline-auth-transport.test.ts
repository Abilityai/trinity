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
const API_KEY = "trinity_mcp_test_key";

describe("#2035 keyless sign-in survives across requests (real transport)", () => {
  let backend: Server;
  let mcpServer: { stop: () => Promise<void> };
  let mcpUrl: URL;
  /** Backend key-validation hits — the observable for "re-validates per POST". */
  let validateCalls = 0;

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
          case "/api/mcp/validate":
            validateCalls++;
            return send(200, {
              valid: true,
              key_id: "key-1",
              user_id: "42",
              user_email: "operator@example.com",
              key_name: "test-key",
              scope: "user",
            });
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
    const transport = new StreamableHTTPClientTransport(mcpUrl);
    await client.connect(transport);
    const text = async (tool: string, args: Record<string, unknown>) => {
      const r: any = await client.callTool({ name: tool, arguments: args });
      return r.content.map((c: any) => c.text).join("\n");
    };
    return { client, transport, text };
  };

  /** A keyed client — the ordinary operator config, credential on the wire. */
  const connectKeyed = async (name: string) => {
    const client = new Client({ name, version: "1.0.0" });
    const transport = new StreamableHTTPClientTransport(mcpUrl, {
      requestInit: { headers: { Authorization: `Bearer ${API_KEY}` } },
    });
    await client.connect(transport);
    return { client, transport };
  };

  /**
   * One JSON-RPC POST built by hand.
   *
   * Needed because the SDK client always opens with `initialize`, and the case
   * under test is a request that joins an EXISTING transport session while
   * choosing its own credential — which is exactly the shape an attacker (or a
   * confused client) would send, and exactly what condition 3 is about.
   * Responses come back as SSE unless the server is in JSON mode, so both are
   * handled.
   */
  const rawRpc = async (
    method: string,
    opts: { sessionId?: string; authorization?: string } = {}
  ): Promise<any> => {
    const res = await fetch(mcpUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json, text/event-stream",
        "mcp-protocol-version": "2025-06-18",
        ...(opts.sessionId ? { "mcp-session-id": opts.sessionId } : {}),
        ...(opts.authorization ? { Authorization: opts.authorization } : {}),
      },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params: {} }),
    });
    const raw = await res.text();
    if (!raw.includes("data:")) return { status: res.status, body: JSON.parse(raw) };
    const payload = raw
      .split("\n")
      .filter((l) => l.startsWith("data:"))
      .map((l) => l.slice(5).trim())
      .join("");
    return { status: res.status, body: JSON.parse(payload) };
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

  // -------------------------------------------------------------------------
  // Condition 3 — memoization is anonymous-tier only
  // -------------------------------------------------------------------------
  //
  // "Never consulted when an Authorization header is present. This line is
  // load-bearing and needs its own test." (@vybe, #2035.)
  //
  // The observable is the backend's key-validation hit count, NOT the session's
  // tool list. `FastMCPSession#updateAuth` sets `#auth` and stops there — the
  // callable tool map is the one built at session creation and is only rebuilt
  // by `toolsListChanged` — so a mid-session identity change is invisible in
  // `tools/list` and cannot discriminate here. Validation calls can: a memo
  // consulted on the keyed path would return a context WITHOUT asking the
  // backend, so the counter would stop advancing.
  //
  // The structural half of this condition — that the store is unreachable from
  // the keyed branch at all — is pinned by the source guard in
  // `inline-auth-scope.test.ts`, which is the stronger statement.

  it("re-validates a keyed session's credential on every request", async () => {
    const start = validateCalls;
    const { client } = await connectKeyed("2035-keyed-revalidate");
    const afterConnect = validateCalls;
    assert.ok(afterConnect > start, "initialize did not validate the key");

    await client.listTools();
    const afterFirstList = validateCalls;
    assert.ok(
      afterFirstList > afterConnect,
      "a keyed request was served from a memo instead of re-validating its key"
    );

    await client.listTools();
    assert.ok(validateCalls > afterFirstList, "a repeat request skipped validation");
    await client.close();
  });

  it("advertises operator tools to a keyed session, never the keyless set", async () => {
    const { client } = await connectKeyed("2035-keyed-visibility");
    const names = (await client.listTools()).tools.map((t) => t.name);
    assert.ok(names.includes("list_agents"), "keyed session did not get operator tools");
    assert.ok(
      !names.includes("request_login"),
      "keyed session was handed the anonymous tool surface"
    );
    await client.close();
  });

  it("re-validates the key even when that session id has a live signed-in memo", async () => {
    // The precise shape condition 3 forbids: a request that presents a session
    // id with a live, SIGNED-IN anonymous entry, and also presents a key. If
    // the memo were consulted first, the backend would never be asked.
    const keyless = await connect("2035-memo-vs-key");
    const verified = await keyless.text("verify_login", { email: EMAIL, code: "123456" });
    assert.match(verified, /signed_in_as/);
    const sessionId = keyless.transport.sessionId;
    assert.ok(sessionId, "transport reported no session id");

    const before = validateCalls;
    await rawRpc("tools/list", {
      sessionId,
      authorization: `Bearer ${API_KEY}`,
    });
    assert.equal(
      validateCalls,
      before + 1,
      "an Authorization header was served from the anonymous memo instead of re-validating"
    );

    await keyless.client.close();
  });

  it("leaves the anonymous memo intact after a keyed request joins the session", async () => {
    // The other half of the separation: the keyed branch must not WRITE to the
    // store either, or it would clobber the entry the keyless client depends on
    // and re-break #2035 for anyone sharing a session id with a keyed caller.
    const keyless = await connect("2035-memo-survives-key");
    await keyless.text("verify_login", { email: EMAIL, code: "123456" });
    const sessionId = keyless.transport.sessionId;
    assert.ok(sessionId, "transport reported no session id");

    await rawRpc("tools/list", { sessionId, authorization: `Bearer ${API_KEY}` });

    const playbooks = await keyless.text("list_playbooks", { agent: AGENT });
    assert.doesNotMatch(
      playbooks,
      /login_required/,
      "a keyed request overwrote the anonymous session's memoized identity"
    );
    await keyless.client.close();
  });

  // -------------------------------------------------------------------------
  // Condition 1 — the transport session id must not reach the log in full
  // -------------------------------------------------------------------------

  it("never writes a full transport session id to stdout", async () => {
    // Captured at `process.stdout.write`, BELOW the console wrapper, so this
    // sees exactly the bytes Vector would ship to /data/logs. Capturing at
    // `console.log` instead would sit above the redactor and prove nothing.
    const captured: string[] = [];
    const realWrite = process.stdout.write.bind(process.stdout);
    (process.stdout as any).write = (chunk: unknown, ...rest: unknown[]) => {
      captured.push(typeof chunk === "string" ? chunk : String(chunk));
      return (realWrite as any)(chunk, ...rest);
    };

    try {
      const { client, transport, text } = await connect("2035-log-redaction");
      const sessionId = transport.sessionId;
      assert.ok(sessionId, "transport reported no session id");
      await text("request_login", { email: EMAIL });
      // Sends the DELETE that reaches mcp-proxy's third log site. `close()`
      // alone does not — it only aborts the SSE stream.
      await transport.terminateSession();
      await client.close();

      const log = captured.join("");
      assert.ok(
        !log.includes(sessionId!),
        `the transport session id reached the log in full: ${sessionId}`
      );
      // Negative control. Without this the case passes vacuously if mcp-proxy
      // simply stopped logging, and the guard would rot into a no-op.
      assert.ok(
        log.includes(`${sessionId!.slice(0, 8)}-REDACTED`),
        "no redacted session id in the log — the log site was never exercised, " +
          "so the absence above proves nothing"
      );
    } finally {
      (process.stdout as any).write = realWrite;
    }
  });
});
