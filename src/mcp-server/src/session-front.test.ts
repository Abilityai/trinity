/**
 * #4060 — keyed MCP sessions survive an MCP server restart.
 *
 * The restart cases run a REAL fastmcp server behind the session front, stop
 * it, start a fresh one on the same port with the same secret, and drive a real
 * `StreamableHTTPClientTransport` client across the gap — the shape that broke
 * every connected client on every redeploy. The Trinity backend is a stub; what
 * is under test is solely the session layer.
 */
import { strict as assert } from "node:assert";
import { after, before, beforeEach, describe, it } from "node:test";
import http, { createServer as createHttpServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

import { createServer } from "./server.js";
import {
  bearerKey,
  createSessionIdCodec,
  idPrefix,
  REVIVE_MAX_AGE_MS,
  startWithSessionFront,
  type SessionFront,
} from "./session-front.js";

const SECRET = "test-internal-secret-4060";
const KEY_A = "trinity_mcp_key_a";
const KEY_B = "trinity_mcp_key_b";
const KEY_C = "trinity_mcp_key_c";

// ---------------------------------------------------------------------------
// The id codec
// ---------------------------------------------------------------------------

describe("#4060 the listener capture still has something to capture", () => {
  it("mcp-proxy creates its server through the default http import with a request listener", async () => {
    // startWithSessionFront swaps `http.createServer` for the duration of
    // `start()`. If a library upgrade stops calling it this way, revival turns
    // itself off (fail closed) — and this test says why.
    const { readFileSync, readdirSync } = await import("node:fs");
    const dir = new URL("../node_modules/mcp-proxy/dist/", import.meta.url);
    const src = readdirSync(dir).filter((f) => f.endsWith(".mjs")).map((f) => readFileSync(new URL(f, dir), "utf8")).join("\n");
    assert.match(src, /^import http from "http";/m);
    assert.match(src, /http\.createServer\(requestListener\)/);
  });
});

describe("#4060 signed session ids", () => {
  const codec = createSessionIdCodec(SECRET);
  const now = Date.UTC(2026, 9, 28);

  it("verifies only for the key it was issued to", () => {
    const id = codec.issue(KEY_A, now);
    assert.equal(codec.verify(id, KEY_A), Math.floor(now / 1000) * 1000);
    assert.equal(codec.verify(id, KEY_B), undefined);
  });

  it("is useless under another instance secret", () => {
    const id = codec.issue(KEY_A, now);
    assert.equal(createSessionIdCodec("another-secret").verify(id, KEY_A), undefined);
  });

  it("refuses a tampered, truncated, re-prefixed or non-canonical id", () => {
    const id = codec.issue(KEY_A, now);
    const [p, rand, issued, tag] = id.split(".");
    const flip = (s: string) => (s[0] === "A" ? "B" : "A") + s.slice(1);
    const forged = [
      [p, flip(rand), issued, tag],
      [p, rand, flip(issued), tag],
      [p, rand, issued, flip(tag)],
      ["t2", rand, issued, tag],
      [p, rand, issued],
      [p, rand, issued, tag, "x"],
      [p, rand + "=", issued, tag],
      [p, rand, issued, tag.slice(0, -1)],
    ].map((parts) => parts.join("."));
    // Node's base64url decoder ignores the unused low bits of the last char:
    // a different string can decode to the SAME bytes. It must not verify.
    const last = tag.at(-1)!;
    const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    const twin = alphabet[(alphabet.indexOf(last) ^ 1) % 64];
    forged.push([p, rand, issued, tag.slice(0, -1) + twin].join("."));
    for (const f of forged) assert.equal(codec.verify(f, KEY_A), undefined, f);
    assert.equal(codec.verify(crypto.randomUUID(), KEY_A), undefined, "a raw UUID is not ours");
  });

  it("logs only an 8-character prefix", () => {
    const id = codec.issue(KEY_A, now);
    assert.equal(idPrefix(id).length, 8);
    assert.ok(!idPrefix(id).includes(id.split(".")[3]));
  });

  it("applies server.ts's exact keyed/keyless predicate", () => {
    assert.equal(bearerKey(`Bearer ${KEY_A}`), KEY_A);
    assert.equal(bearerKey("Basic abc"), undefined, "a non-Bearer header is the anonymous tier");
    assert.equal(bearerKey("bearer abc"), undefined);
    assert.equal(bearerKey(undefined), undefined);
  });
});

// ---------------------------------------------------------------------------
// Real servers across a restart
// ---------------------------------------------------------------------------

describe("#4060 keyed sessions survive a restart (real transport)", () => {
  let backend: Server;
  let backendUrl: string;
  let port: number;
  let mcp: { stop: () => Promise<void> } | undefined;
  let front: SessionFront;
  let validateCalls = 0;
  let revoked = new Set<string>();
  let auditRows: any[] = [];

  const users: Record<string, string> = { [KEY_A]: "42", [KEY_B]: "43", [KEY_C]: "44" };

  before(async () => {
    backend = createHttpServer((req, res) => {
      let body = "";
      req.on("data", (c) => (body += c));
      req.on("end", () => {
        const send = (status: number, payload: unknown) => {
          res.writeHead(status, { "Content-Type": "application/json" });
          res.end(JSON.stringify(payload));
        };
        if (req.url === "/api/mcp/validate") {
          validateCalls++;
          const key = bearerKey(req.headers.authorization);
          if (!key || !users[key] || revoked.has(key)) return send(401, { detail: "invalid" });
          return send(200, {
            valid: true,
            key_id: `id-${users[key]}`,
            user_id: users[key],
            user_email: `user${users[key]}@example.com`,
            key_name: `key-${users[key]}`,
            scope: "user",
          });
        }
        if (req.url === "/api/internal/audit") {
          auditRows.push(JSON.parse(body || "{}"));
          return send(200, {});
        }
        if (req.url === "/api/agents") return send(200, []);
        return send(200, {});
      });
    });
    await new Promise<void>((r) => backend.listen(0, "127.0.0.1", () => r()));
    backendUrl = `http://127.0.0.1:${(backend.address() as AddressInfo).port}`;
    const probe = createHttpServer();
    await new Promise<void>((r) => probe.listen(0, "127.0.0.1", () => r()));
    port = (probe.address() as AddressInfo).port;
    await new Promise<void>((r) => probe.close(() => r()));
  });

  after(async () => {
    await mcp?.stop();
    await new Promise<void>((r) => backend.close(() => r()));
  });

  beforeEach(() => {
    revoked = new Set();
    auditRows = [];
  });

  async function start(secret = SECRET): Promise<void> {
    const { server } = await createServer({
      trinityApiUrl: backendUrl,
      requireApiKey: true,
      inlineAuthEnabled: true,
      internalApiSecret: secret,
      port,
    });
    front = await startWithSessionFront(
      () => server.start({ transportType: "httpStream", httpStream: { port, host: "127.0.0.1" } }),
      { secret, host: "127.0.0.1", port }
    );
    server.on("disconnect", (event: any) => front.innerClosed(event?.session?.sessionId));
    mcp = server;
  }

  async function restart(secret = SECRET): Promise<void> {
    await mcp?.stop();
    mcp = undefined;
    await new Promise((r) => setTimeout(r, 150)); // let pooled client sockets see the close
    await start(secret);
  }

  /** One JSON-RPC POST on a fresh connection (no pooled socket across restarts). */
  function rpc(method: string, opts: { sessionId?: string; key?: string; authorization?: string; params?: unknown } = {}): Promise<{ status: number; sessionId?: string; body: string; headers: http.IncomingHttpHeaders }> {
    return new Promise((resolve, reject) => {
      const headers: Record<string, string> = {
        "content-type": "application/json",
        accept: "application/json, text/event-stream",
        "mcp-protocol-version": "2025-06-18",
      };
      if (opts.sessionId) headers["mcp-session-id"] = opts.sessionId;
      if (opts.key) headers.authorization = `Bearer ${opts.key}`;
      if (opts.authorization) headers.authorization = opts.authorization;
      const body = method === "initialize"
        ? { jsonrpc: "2.0", id: 1, method, params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "t", version: "1" } } }
        : { jsonrpc: "2.0", id: 1, method, params: opts.params ?? {} };
      const req = http.request({ host: "127.0.0.1", port, path: "/mcp", method: "POST", headers, agent: false }, (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => resolve({ status: res.statusCode ?? 0, sessionId: res.headers["mcp-session-id"] as string | undefined, body: data, headers: res.headers }));
      });
      req.on("error", reject);
      req.end(JSON.stringify(body));
    });
  }

  async function keyedSession(key: string): Promise<string> {
    const init = await rpc("initialize", { key });
    assert.equal(init.status, 200, init.body);
    assert.ok(init.sessionId?.startsWith("t1."), `a keyed session gets a signed id, got ${init.sessionId}`);
    await rpc("notifications/initialized", { key, sessionId: init.sessionId });
    return init.sessionId!;
  }

  const toolNames = (body: string): string[] => {
    const data = body.split("\n").find((l) => l.startsWith("data:"));
    return (JSON.parse(data ? data.slice(5) : body).result?.tools ?? []).map((t: any) => t.name);
  };

  it("a real client keeps its session id and its next tool call succeeds after a restart", async () => {
    await start();
    assert.equal(front.active, true, "the listener was captured");
    const client = new Client({ name: "restart-client", version: "1.0.0" });
    const transport = new StreamableHTTPClientTransport(new URL(`http://127.0.0.1:${port}/mcp`), {
      requestInit: { headers: { Authorization: `Bearer ${KEY_A}` } },
    });
    await client.connect(transport);
    const before = transport.sessionId;
    assert.ok(before?.startsWith("t1."));
    const toolsBefore = (await client.listTools()).tools.map((t) => t.name).sort();
    await client.callTool({ name: "list_agents", arguments: {} });

    await restart();
    const validationsBefore = validateCalls;
    const result: any = await client.callTool({ name: "list_agents", arguments: {} });
    assert.ok(!result.isError, JSON.stringify(result));
    assert.equal(transport.sessionId, before, "the client never noticed: same session id");
    assert.deepEqual((await client.listTools()).tools.map((t) => t.name).sort(), toolsBefore, "the key's tool list");

    const revivals = auditRows.filter((r) => r.event_action === "session_revived");
    assert.equal(revivals.length, 1, "one revival, then the revived session is reused");
    assert.equal(revivals[0].event_type, "mcp_operation");
    assert.equal(revivals[0].mcp_key_name, "key-42");
    assert.equal(revivals[0].target_id, idPrefix(before!));
    assert.ok(!JSON.stringify(revivals[0]).includes(before!), "the full session id is never audited");
    assert.ok(validateCalls > validationsBefore, "every request still re-validates the key");
    await client.close();
  });

  it("refuses a revived id to another key, to no key, and to a tampered id", async () => {
    const x = await keyedSession(KEY_A);
    await restart();
    assert.equal((await rpc("tools/list", { key: KEY_B, sessionId: x })).status, 404, "another valid key");
    assert.equal((await rpc("tools/list", { sessionId: x })).status !== 200, true, "no key");
    const tampered = x.slice(0, -2) + (x.at(-2) === "A" ? "B" : "A") + x.at(-1);
    assert.equal((await rpc("tools/list", { key: KEY_A, sessionId: tampered })).status, 404, "tampered");
    assert.equal((await rpc("tools/list", { key: KEY_A, sessionId: crypto.randomUUID() })).status, 404, "a client-chosen id");
    // The rightful key still gets it back.
    assert.equal((await rpc("tools/list", { key: KEY_A, sessionId: x })).status, 200);
    assert.equal((await rpc("tools/list", { key: KEY_B, sessionId: x })).status, 404, "bound once revived");
  });

  it("binds a LIVE session to its key (another valid key used to take it over)", async () => {
    const x = await keyedSession(KEY_A);
    assert.equal((await rpc("tools/list", { key: KEY_A, sessionId: x })).status, 200);
    assert.equal((await rpc("tools/list", { key: KEY_B, sessionId: x })).status, 404);
    assert.equal((await rpc("tools/call", { key: KEY_B, sessionId: x, params: { name: "list_agents", arguments: {} } })).status, 404);
  });

  it("never revives an anonymous session", async () => {
    const init = await rpc("initialize");
    assert.equal(init.status, 200, init.body);
    assert.ok(init.sessionId && !init.sessionId.startsWith("t1."), "a keyless session is never signed");
    assert.equal((await rpc("tools/list", { sessionId: init.sessionId })).status, 200);
    await restart();
    assert.equal((await rpc("tools/list", { sessionId: init.sessionId })).status, 404);
    assert.equal((await rpc("tools/list", { sessionId: init.sessionId, key: KEY_A })).status, 404, "nor with a key");
  });

  it("a revoked key fails on a revived session exactly as on a fresh one", async () => {
    const x = await keyedSession(KEY_C);
    await restart();
    revoked.add(KEY_C);
    const fresh = await rpc("initialize", { key: KEY_C });
    const revived = await rpc("tools/list", { key: KEY_C, sessionId: x });
    assert.equal(revived.status, fresh.status);
    assert.notEqual(revived.status, 200);
    assert.notEqual(revived.status, 404, "an auth failure, not a lost session");
  });

  /** The inner (library) session for the most recent keyed session. */
  const latestInnerSession = (): any => {
    const sessions: any[] = (mcp as any).sessions;
    return sessions[sessions.length - 1];
  };

  it("never shows a keyed client its inner session id, and never accepts one directly", async () => {
    const x = await keyedSession(KEY_A);
    const res = await rpc("tools/list", { key: KEY_A, sessionId: x });
    assert.equal(res.status, 200);
    assert.ok(res.sessionId === undefined || res.sessionId === x, `got ${res.sessionId}`);
    const innerId: string = latestInnerSession().sessionId;
    assert.ok(innerId && innerId !== x);
    assert.equal((await rpc("tools/list", { sessionId: innerId })).status, 404, "keyless with the inner id");
    assert.equal((await rpc("tools/list", { key: KEY_A, sessionId: innerId })).status, 404, "keyed with the inner id");
  });

  it("revives a session the library dropped (idle reap) without a restart", async () => {
    const x = await keyedSession(KEY_A);
    assert.equal((await rpc("tools/list", { key: KEY_A, sessionId: x })).status, 200);
    const session = latestInnerSession();
    const reaped = session.sessionId;
    await session.close(); // what mcp-proxy's idle reaper does; fastmcp emits `disconnect`
    await new Promise((r) => setTimeout(r, 100));
    const res = await rpc("tools/list", { key: KEY_A, sessionId: x });
    assert.equal(res.status, 200, res.body);
    assert.notEqual(latestInnerSession().sessionId, reaped, "a fresh inner session");
  });

  it("does not revive an explicitly deleted session in the same process", async () => {
    const x = await keyedSession(KEY_A);
    const del = await new Promise<number>((resolve) => {
      http.request({ host: "127.0.0.1", port, path: "/mcp", method: "DELETE", headers: { authorization: `Bearer ${KEY_A}`, "mcp-session-id": x, "mcp-protocol-version": "2025-06-18" }, agent: false }, (r) => { r.resume(); resolve(r.statusCode ?? 0); }).end();
    });
    assert.equal(del, 200);
    assert.equal((await rpc("tools/list", { key: KEY_A, sessionId: x })).status, 404);
  });

  it("refuses an id past the maximum age", async () => {
    const old = createSessionIdCodec(SECRET).issue(KEY_A, Date.now() - REVIVE_MAX_AGE_MS - 60_000);
    assert.equal((await rpc("tools/list", { key: KEY_A, sessionId: old })).status, 404);
  });

  it("rejects a request carrying two session ids", async () => {
    const x = await keyedSession(KEY_A);
    const status = await new Promise<number>((resolve) => {
      const req = http.request({ host: "127.0.0.1", port, path: "/mcp", method: "POST", agent: false, headers: [
        "content-type", "application/json", "accept", "application/json, text/event-stream",
        "authorization", `Bearer ${KEY_A}`, "mcp-session-id", x, "mcp-session-id", crypto.randomUUID(),
      ] as any }, (r) => { r.resume(); resolve(r.statusCode ?? 0); });
      req.end(JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list", params: {} }));
    });
    assert.equal(status, 400);
  });

  it("with no secret, behaves exactly as before: a restart loses the session", async () => {
    await restart("");
    assert.equal(front.active, false);
    const init = await rpc("initialize", { key: KEY_A });
    assert.ok(init.sessionId && !init.sessionId.startsWith("t1."));
    await restart("");
    assert.equal((await rpc("tools/list", { key: KEY_A, sessionId: init.sessionId })).status, 404);
    await restart();
  });

  it("tool lists stay per key on revival", async () => {
    const x = await keyedSession(KEY_A);
    const listed = toolNames((await rpc("tools/list", { key: KEY_A, sessionId: x })).body);
    await restart();
    assert.deepEqual(toolNames((await rpc("tools/list", { key: KEY_A, sessionId: x })).body).sort(), listed.sort());
  });
});
