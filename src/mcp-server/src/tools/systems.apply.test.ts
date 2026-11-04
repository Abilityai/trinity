/**
 * trinity-enterprise#695 — the `apply_system` tool contract, through the
 * REGISTERED tool.
 *
 * `apply_system` is the reconcile sibling of `deploy_system`: a new tool rather
 * than a `mode` argument, so an older MCP server answers "unknown tool" instead
 * of dropping `mode` and running CREATE (which suffixes the fleet). What lives
 * in tool CODE, and is therefore pinned here:
 *
 * 1. **`dry_run` defaults to TRUE** (`dry_run ?? true`), the opposite of
 *    `deploy_system`. An `|| false` typo would silently invert it.
 * 2. **It never throws.** Client resolution sits inside the same `try` as the
 *    request, so even a missing API key is a returned object.
 * 3. **A thin mapping.** Only 404/405 (a build without the route) and 403 are
 *    mapped here; every other JSON OBJECT body is returned AS the result with
 *    `http_status` and `success: false` added, so a status-code change in the
 *    backend contract cannot silently break the tool. A JSON scalar, array or
 *    null, or a non-JSON body, comes back as `{success: false, http_status,
 *    detail}`.
 * 4. **Audit honesty.** A 403 goes through `access.ts::accessDenied`, so its
 *    tool-call audit row says `denied: true` (#2807). Every other returned
 *    failure (400, 401, 409, 422, 503, 500, 404/405, a non-object body, a
 *    missing key, a dropped connection) goes through `audit.ts::toolFailed`:
 *    its row says `success: false` with a short `error` and NO `denied` key
 *    (#4026). A 2xx, including a dry-run `invalid` and a real-run `partial`,
 *    stays `success: true`. `teardown_system` is pinned the same way below,
 *    through the same registered server.
 * 5. **`deploy_system` returns its failure report** (#4027): its request and
 *    every 2xx result are byte-identical to create mode as ent#695 froze it
 *    (AC6), but a non-2xx answer (the 500 `failed` report above all) is a
 *    RETURNED result stamped like apply's, never a thrown error. A missing key
 *    and a dropped connection still throw.
 *
 * Harness: a real `createServer` in key mode, a real MCP client over the
 * streamable-HTTP transport presenting a person's USER-scoped key (apply's
 * legitimate caller), and a stub backend that answers key validation, RECORDS
 * every other request it receives (method, path, Authorization, body) and every
 * fire-and-forget audit row, and answers `/api/systems/apply` and
 * `/api/systems/deploy` with whatever the test sets (or drops the connection).
 * The backend sits on a random port of 127.0.0.1 — not the default
 * `localhost:8000` — a backend at another origin than the default. Two cases go
 * through the SAME registered tools in-process, via FastMCP's own
 * `server.connect(transport, auth)`: a session with no API key, which the HTTP
 * transport cannot produce (its `authenticate` refuses that request before any
 * tool runs); and another HOSTNAME, in-process by choice so an https DNS origin
 * needs no DNS or TLS (a second, unstarted server on
 * `https://trinity-b.example.test:8443`, `fetch` stubbed). Pattern:
 * `access-wiring.test.ts` (#2952 row draining).
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { strict as assert } from "node:assert";
import { after, afterEach, before, describe, it } from "node:test";
import { createServer as createHttpServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";

import { createServer } from "../server.js";
import { createSystemTools } from "./systems.js";
import { TOOL_ACCESS_POLICY, policyFor } from "../access.js";
import { TrinityClient } from "../client.js";

type McpServer = Awaited<ReturnType<typeof createServer>>["server"];

const MANIFEST = "name: acme\nagents:\n  worker:\n    template: local:worker\n";

/** A backend answer the test chooses: status, body text, content type — or a dropped connection. */
interface Reply {
  status: number;
  body: string;
  contentType?: string;
  /** Destroy the socket instead of answering: a transport failure with no HTTP status. */
  disconnect?: boolean;
}
const jsonReply = (status: number, payload: unknown): Reply => ({ status, body: JSON.stringify(payload) });

/** A request the stub backend received, other than key validation and audit. */
interface Recorded {
  method: string;
  path: string;
  authorization: string;
  host: string;
  body: unknown;
}

// Representative bodies of the backend apply contract. The tool does not
// interpret them; these tests prove they reach the caller intact.
const PLAN = {
  mode: "apply",
  dry_run: true,
  system_name: "acme",
  status: "valid",
  members: [{ name: "acme-worker", short_name: "worker", verdict: "create", changes: [], restart_required: false, applied: null }],
  drift: [],
  blockers: [],
  failed: [],
  warnings: [],
};
const REFUSED_409 = {
  mode: "apply",
  dry_run: false,
  system_name: "acme",
  status: "refused",
  members: [{ name: "acme-a", short_name: "a", verdict: "blocked", changes: [], restart_required: false, applied: null }],
  drift: [],
  blockers: [
    {
      code: "membership_evidence_prefix_only",
      member: "acme-a",
      detail: "acme-a matches the system by name only",
      remedy: "add this system's tag (MCP tag_agent)",
      retryable: false,
    },
  ],
  failed: [],
  warnings: [],
};
const REFUSED_503 = {
  ...REFUSED_409,
  blockers: [{ code: "membership_tags_unreadable", member: null, detail: "tags unreadable", remedy: "retry", retryable: true }],
};
const FAILED_500 = {
  ...PLAN,
  dry_run: false,
  status: "failed",
  failed: [{ name: "acme-worker", short_name: "worker", step: "create", reason: "docker daemon gone" }],
};

describe("ent#695 apply_system through the registered tool (real transport, key mode, non-default origin)", () => {
  let backend: Server;
  let mcpServer: McpServer;
  let mcpUrl: URL;
  let backendOrigin: string;
  /** Every non-validation, non-audit request — the side effect under test. */
  const requests: Recorded[] = [];
  /** Every audit row the MCP server posted (#2807). */
  const auditRows: any[] = [];
  /** Tool calls made through `session().call` — rows must keep pace (#2952). */
  let calls = 0;
  let applyReply: Reply = jsonReply(200, PLAN);
  let deployReply: Reply = jsonReply(200, { status: "valid", system_name: "acme", agents_created: [], warnings: [] });
  let teardownReply: Reply = jsonReply(200, { status: "preview", system_name: "acme", members: [] });

  before(async () => {
    backend = createHttpServer((req, res) => {
      let raw = "";
      req.on("data", (c) => (raw += c));
      req.on("end", () => {
        const answer = (r: Reply) => {
          if (r.disconnect) {
            req.socket.destroy();
            return;
          }
          res.writeHead(r.status, { "Content-Type": r.contentType ?? "application/json" });
          res.end(r.body);
        };
        const url = req.url ?? "";
        const authorization = String(req.headers.authorization ?? "");
        if (url === "/api/mcp/validate") {
          // A person's user-scoped key, unless the key's name says agent or
          // system (the two non-person scopes the visibility case presents);
          // the bearer is echoed back as key_name so every audit row carries
          // which session made the call (#2952).
          const scope = authorization.includes("_agentkey_") ? "agent"
            : authorization.includes("_systemkey_") ? "system" : "user";
          return answer(jsonReply(200, {
            valid: true,
            key_id: `key-${scope}-1`,
            user_id: "owner",
            user_email: "owner@example.com",
            key_name: authorization.replace(/^Bearer /, ""),
            scope,
            ...(scope === "agent" ? { agent_name: "alpha" } : {}),
          }));
        }
        if (url === "/api/internal/audit" && req.method === "POST") {
          auditRows.push(JSON.parse(raw));
          return answer(jsonReply(200, { event_id: `ev_${auditRows.length}`, status: "logged" }));
        }
        requests.push({
          method: req.method ?? "",
          path: url,
          authorization,
          host: String(req.headers.host ?? ""),
          body: raw ? JSON.parse(raw) : undefined,
        });
        if (url === "/api/systems/apply") return answer(applyReply);
        if (url === "/api/systems/deploy") return answer(deployReply);
        if (url.startsWith("/api/enterprise/system-teardown/")) return answer(teardownReply);
        return answer(jsonReply(200, {})); // health probe, anything else
      });
    });
    await new Promise<void>((r) => backend.listen(0, "127.0.0.1", () => r()));
    backendOrigin = `http://127.0.0.1:${(backend.address() as AddressInfo).port}`;

    // The MCP port is probed then bound, so another process can take it in
    // between: retry on EADDRINUSE (startHTTPServer rejects on a listen error)
    // rather than flake.
    for (let attempt = 1; ; attempt++) {
      const probe = createHttpServer();
      await new Promise<void>((r) => probe.listen(0, "127.0.0.1", () => r()));
      const mcpPort = (probe.address() as AddressInfo).port;
      await new Promise<void>((r) => probe.close(() => r()));

      const { server } = await createServer({
        trinityApiUrl: backendOrigin,
        requireApiKey: true,
        port: mcpPort,
        internalApiSecret: "test-internal-secret",
      });
      try {
        await server.start({ transportType: "httpStream", httpStream: { port: mcpPort, host: "127.0.0.1" } });
      } catch (error) {
        if ((error as NodeJS.ErrnoException)?.code === "EADDRINUSE" && attempt < 5) continue;
        throw error;
      }
      mcpServer = server;
      mcpUrl = new URL(`http://127.0.0.1:${mcpPort}/mcp`);
      break;
    }
  });

  /**
   * Every in-process session opened below. FastMCP's `stop()` does not close a
   * `connect()`ed session and its ping interval keeps the process alive, so a
   * test that fails before its own close would hang the run instead of
   * reporting red: close them all here as well.
   */
  const openInProcess: Array<() => Promise<void>> = [];

  after(async () => {
    for (const close of openInProcess.splice(0)) await close().catch(() => {});
    await mcpServer?.stop();
    await new Promise<void>((r) => backend.close(() => r()));
  });

  /** A clean slate per case. */
  const reset = (apply: Reply = jsonReply(200, PLAN)) => {
    requests.length = 0;
    applyReply = apply;
  };
  /** A clean slate for a teardown case. */
  const resetTeardown = (teardown: Reply) => {
    requests.length = 0;
    teardownReply = teardown;
  };

  /** THIS call's audit row, awaited (fire-and-forget POST) and checked for tool and session (#2952). */
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
    throw new Error(`no audit row for call ${at} (${tool}) within ${timeoutMs}ms; rows: ${JSON.stringify(auditRows)}`);
  };

  /** `call(tool, args)` for one connected client: one call, its own drained audit row (#2952). */
  const callerFor = (client: Client, key: string) => async (tool: string, args: Record<string, unknown>) => {
    assert.equal(auditRows.length, calls, `${auditRows.length} audit rows after ${calls} calls (#2952)`);
    const at = calls++;
    let r: any;
    try {
      r = await client.callTool({ name: tool, arguments: args });
    } catch (error) {
      // A protocol-level refusal (e.g. "Unknown tool") never reaches the
      // audit wrapper, so it writes no row: un-count it, so one such failure
      // reds its own test instead of every call after it.
      calls--;
      throw error;
    }
    // Drain BEFORE parsing, so a non-JSON result never leaves this call's row in flight.
    const row = await auditRowAt(at, tool, key);
    const text: string = r.content.map((c: any) => c.text).join("\n");
    return { text, isError: r.isError === true, row };
  };

  /** A client holding a person's own user-scoped key, over the real HTTP transport. */
  const session = async (name: string) => {
    const key = `trinity_mcp_695_${name}`;
    const client = new Client({ name, version: "1.0.0" });
    await client.connect(
      new StreamableHTTPClientTransport(mcpUrl, { requestInit: { headers: { Authorization: `Bearer ${key}` } } })
    );
    return { client, call: callerFor(client, key), key };
  };

  /**
   * The SAME registered server, driven in-process through FastMCP's own
   * session with an auth context supplied directly — for the one shape the
   * HTTP `authenticate` step can never produce (it refuses a keyless request
   * before any tool runs).
   */
  const inProcess = async (auth: { keyName: string } & Record<string, unknown>) => {
    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
    const client = new Client({ name: auth.keyName, version: "1.0.0" });
    const [serverSession] = await Promise.all([
      (mcpServer as any).connect(serverTransport, auth),
      client.connect(clientTransport),
    ]);
    // The caller owns a connect()ed session: close it, or its ping interval
    // keeps the process alive after the last test.
    let closed = false;
    const close = async () => {
      if (closed) return;
      closed = true;
      await client.close();
      await serverSession.close();
    };
    openInProcess.push(close);
    return { client, close, call: callerFor(client, auth.keyName) };
  };

  const parsed = (text: string) => JSON.parse(text);

  // -------------------------------------------------------------------------
  describe("the request it sends", () => {
    it("previews by default: posts exactly {manifest, dry_run: true} to /api/systems/apply and returns the body verbatim", async () => {
      reset();
      const s = await session("default");
      const { text, isError, row } = await s.call("apply_system", { manifest: MANIFEST });

      assert.equal(isError, false);
      assert.equal(requests.length, 1, `expected one backend request, got ${JSON.stringify(requests)}`);
      assert.equal(requests[0].method, "POST");
      assert.equal(requests[0].path, "/api/systems/apply");
      assert.deepEqual(
        requests[0].body,
        { manifest: MANIFEST, dry_run: true },
        "dry_run must default to TRUE — the opposite of deploy_system"
      );
      assert.deepEqual(Object.keys(requests[0].body as object), ["manifest", "dry_run"], "no stray keys (the route forbids extras)");
      assert.equal(text, JSON.stringify(PLAN, null, 2), "a 2xx body is returned verbatim");
      assert.equal(row.details.success, true);
      assert.equal(row.details.denied, undefined);
      await s.client.close();
    });

    it("applies only when dry_run is explicitly false", async () => {
      reset(jsonReply(200, { ...PLAN, dry_run: false, status: "applied" }));
      const s = await session("explicit-false");
      await s.call("apply_system", { manifest: MANIFEST, dry_run: false });
      assert.deepEqual(requests[0].body, { manifest: MANIFEST, dry_run: false });
      await s.client.close();
    });

    it("sends an explicit dry_run: true as true", async () => {
      reset();
      const s = await session("explicit-true");
      await s.call("apply_system", { manifest: MANIFEST, dry_run: true });
      assert.deepEqual(requests[0].body, { manifest: MANIFEST, dry_run: true });
      await s.client.close();
    });
  });

  // -------------------------------------------------------------------------
  describe("a build without the route fails closed", () => {
    for (const [status, detail] of [
      [404, "Not Found"],
      [405, "Method Not Allowed"],
    ] as const) {
      it(`${status} → not_available, never a fallback to create`, async () => {
        reset(jsonReply(status, { detail }));
        const s = await session(`absent-${status}`);
        const { text, isError, row } = await s.call("apply_system", { manifest: MANIFEST, dry_run: false });
        const out = parsed(text);

        assert.equal(isError, false, "returned, not thrown");
        assert.equal(out.success, false);
        assert.equal(out.not_available, true);
        assert.equal(out.http_status, status);
        assert.equal(out.detail, detail);
        assert.match(out.error, /not available/);
        assert.match(out.error, /do NOT fall back to deploy_system/, "the actionable warning: create would suffix existing members");
        assert.match(out.error, /Upgrade the instance/, "and what to do instead");
        assert.deepEqual(
          requests.map((r) => r.path),
          ["/api/systems/apply"],
          "exactly one request, and never to /api/systems/deploy"
        );
        assert.equal(row.details.denied, undefined, "an absent route is not an authorization denial");
        assert.equal(row.details.success, false, `a not_available result was audited as a success: ${JSON.stringify(row.details)}`);
        assert.equal(row.details.error, `HTTP ${status}: apply route absent (not_available)`);
        // Byte-for-byte what the tool returned before #4026: the stamp is out-of-band.
        assert.equal(
          text,
          JSON.stringify(
            {
              success: false,
              not_available: true,
              http_status: status,
              error:
                `System apply is not available on this Trinity build: POST /api/systems/apply answered ${status}, `
                + "so the backend predates apply mode. Nothing was planned or written, and do NOT fall back to "
                + "deploy_system: it is create-only and would create suffixed copies (`<name>_2`) of members "
                + "that already exist. Upgrade the instance to a build with apply mode, or change existing "
                + "members one at a time with the per-agent tools.",
              detail,
            },
            null,
            2
          )
        );
        await s.client.close();
      });
    }

    it("a proxy's non-JSON 404 is still not_available, with the raw text as detail", async () => {
      const html = "<html><body>404 Not Found</body></html>";
      reset({ status: 404, body: html, contentType: "text/html" });
      const s = await session("absent-html");
      const { text } = await s.call("apply_system", { manifest: MANIFEST });
      const out = parsed(text);
      assert.equal(out.not_available, true);
      assert.equal(out.http_status, 404);
      assert.equal(out.detail, html);
      await s.client.close();
    });
  });

  // -------------------------------------------------------------------------
  describe("every other JSON object body is the result, and none of them is a denial", () => {
    const VALIDATION_422 = { detail: [{ loc: ["body", "mode"], msg: "Extra inputs are not permitted", type: "extra_forbidden" }] };
    for (const [status, body, reason] of [
      [400, { detail: "Invalid manifest: agents must be a mapping" }, "HTTP 400: Invalid manifest: agents must be a mapping"],
      [401, { detail: "Invalid or expired API key" }, "HTTP 401: Invalid or expired API key"],
      [409, REFUSED_409, "HTTP 409: status 'refused'"],
      [422, VALIDATION_422, "HTTP 422"],
      [503, REFUSED_503, "HTTP 503: status 'refused'"],
      [500, FAILED_500, "HTTP 500: status 'failed'"],
      // An empty report `status` is not a status: the reason falls through to the detail.
      [409, { status: "", detail: "refused without a status" }, "HTTP 409: refused without a status"],
    ] as const) {
      it(`a ${status} body comes back AS the result, with http_status and success: false, and is audited as a failure, not a denial`, async () => {
        reset(jsonReply(status, body));
        const s = await session(`body-${status}`);
        const { text, isError, row } = await s.call("apply_system", { manifest: MANIFEST, dry_run: false });

        assert.equal(isError, false, "a refusal is a result, not a thrown error");
        // Byte-for-byte: the body's own keys first, then http_status, then success.
        assert.equal(text, JSON.stringify({ ...body, http_status: status, success: false }, null, 2));
        assert.equal(row.details.denied, undefined, `a ${status} is not an authorization denial: only a 403 is`);
        assert.equal(row.details.success, false, `a returned ${status} was audited as a success: ${JSON.stringify(row.details)}`);
        assert.equal(row.details.error, reason);
        await s.client.close();
      });
    }
  });

  // -------------------------------------------------------------------------
  describe("the audit reason stays short", () => {
    it("a long string detail is capped on the row, never on the caller's result", async () => {
      const long = "Invalid manifest: " + "x".repeat(500);
      reset(jsonReply(400, { detail: long }));
      const s = await session("long-detail");
      const { text, row } = await s.call("apply_system", { manifest: MANIFEST, dry_run: false });

      assert.equal(text, JSON.stringify({ detail: long, http_status: 400, success: false }, null, 2), "the caller still gets the full detail");
      assert.equal(row.details.error, `HTTP 400: ${long.slice(0, 200)}…`);
      await s.client.close();
    });
  });

  // -------------------------------------------------------------------------
  describe("anything else becomes {success: false, http_status, detail}", () => {
    const html = "<html><body><h1>502 Bad Gateway</h1></body></html>";
    for (const [label, reply, detail] of [
      ["a non-JSON body", { status: 502, body: html, contentType: "text/html" } as Reply, html],
      ["a JSON string", jsonReply(502, "upstream unavailable"), "upstream unavailable"],
      ["a JSON array", jsonReply(502, ["upstream", "unavailable"]), ["upstream", "unavailable"]],
      ["JSON null", jsonReply(500, null), null],
    ] as const) {
      it(`${label}: the raw value is the detail, and it is not a denial`, async () => {
        reset(reply);
        const s = await session(`detail-${label.replace(/\W+/g, "-")}`);
        const { text, isError, row } = await s.call("apply_system", { manifest: MANIFEST, dry_run: false });

        assert.equal(isError, false);
        assert.equal(text, JSON.stringify({ success: false, http_status: reply.status, detail }, null, 2));
        assert.equal(row.details.denied, undefined);
        assert.equal(row.details.success, false, `a returned ${reply.status} was audited as a success: ${JSON.stringify(row.details)}`);
        assert.equal(row.details.error, `HTTP ${reply.status}`, "a raw body (HTML, say) never lands on the audit row");
        await s.client.close();
      });
    }
  });

  // -------------------------------------------------------------------------
  describe("a 403 is the one authorization denial", () => {
    it("403 → not_permitted with the human-only hint, audited as denied", async () => {
      reset(jsonReply(403, { detail: "This action requires a person's credential" }));
      const s = await session("forbidden");
      const { text, isError, row } = await s.call("apply_system", { manifest: MANIFEST, dry_run: false });
      const out = parsed(text);

      assert.equal(isError, false);
      assert.equal(out.success, false);
      assert.equal(out.not_permitted, true);
      assert.equal(out.http_status, 403);
      assert.equal(out.detail, "This action requires a person's credential");
      assert.match(out.error, /apply is human-only: run it with a person's credential/);
      assert.equal(out.not_available, undefined, "403 is not 404 — do not conflate");
      assert.equal(
        text,
        JSON.stringify(
          {
            success: false,
            not_permitted: true,
            http_status: 403,
            error:
              "Refused (403): apply is human-only: run it with a person's credential (an interactive session "
              + "or an operator's own user-scoped key) holding the 'creator' role. Agent, connector and system "
              + "keys are refused, because apply writes to existing agents. `detail` names the gate that refused.",
            detail: "This action requires a person's credential",
          },
          null,
          2
        ),
        "the 403 envelope is byte-identical"
      );

      assert.equal(row.details.success, false, `the denial was audited as a success: ${JSON.stringify(row.details)}`);
      assert.equal(row.details.denied, true);
      assert.equal(row.details.error, out.error);
      await s.client.close();
    });

    it("on ONE session, a 403 then a 409: only the 403 is a denial (no stale marker)", async () => {
      const s = await session("deny-then-refuse");
      reset(jsonReply(403, { detail: "human-only" }));
      const first = await s.call("apply_system", { manifest: MANIFEST, dry_run: false });
      reset(jsonReply(409, REFUSED_409));
      const second = await s.call("apply_system", { manifest: MANIFEST, dry_run: false });

      assert.equal(first.row.details.denied, true);
      assert.equal(second.row.details.denied, undefined, `the 409 inherited the 403's denial: ${JSON.stringify(second.row.details)}`);
      assert.equal(parsed(second.text).status, "refused");
      await s.client.close();
    });

    it("on ONE session, a 409 then a 200: the 200 is a success (no stale failure marker)", async () => {
      const s = await session("fail-then-ok");
      reset(jsonReply(409, REFUSED_409));
      const first = await s.call("apply_system", { manifest: MANIFEST, dry_run: false });
      reset(jsonReply(200, PLAN));
      const second = await s.call("apply_system", { manifest: MANIFEST });

      assert.equal(first.row.details.success, false);
      assert.equal(second.row.details.success, true, `the 200 inherited the 409's failure: ${JSON.stringify(second.row.details)}`);
      assert.equal(second.row.details.error, undefined);
      await s.client.close();
    });
  });

  // -------------------------------------------------------------------------
  describe("a 2xx is a success, whatever the report's own status says", () => {
    for (const [label, body] of [
      ["a dry-run 'invalid' plan", { ...PLAN, status: "invalid" }],
      ["a real-run 'partial' apply", { ...PLAN, dry_run: false, status: "partial", failed: [{ name: "acme-worker", step: "grant", reason: "x" }] }],
    ] as const) {
      it(`${label}: returned verbatim, audited success: true`, async () => {
        reset(jsonReply(200, body));
        const s = await session(`ok-${label.replace(/\W+/g, "-")}`);
        const { text, isError, row } = await s.call("apply_system", { manifest: MANIFEST, dry_run: body.dry_run });

        assert.equal(isError, false);
        assert.equal(text, JSON.stringify(body, null, 2));
        assert.equal(row.details.success, true, `a 2xx was audited as a failure: ${JSON.stringify(row.details)}`);
        assert.equal(row.details.error, undefined);
        assert.equal(row.details.denied, undefined);
        await s.client.close();
      });
    }
  });

  // -------------------------------------------------------------------------
  describe("the other shape: another origin, with the caller's own key", () => {
    it("forwards each call to the configured (non-default) backend origin carrying THAT session's key", async () => {
      reset();
      const a = await session("origin-a");
      const b = await session("origin-b");
      await a.call("apply_system", { manifest: MANIFEST });
      await b.call("apply_system", { manifest: MANIFEST });

      assert.notEqual(backendOrigin, "http://localhost:8000", "the harness must not sit on the default origin");
      assert.deepEqual(
        requests.map((r) => [r.path, r.authorization, r.host]),
        [
          ["/api/systems/apply", `Bearer ${a.key}`, new URL(backendOrigin).host],
          ["/api/systems/apply", `Bearer ${b.key}`, new URL(backendOrigin).host],
        ],
        "each call reaches the configured origin with its own session's key — never a shared or default credential"
      );
      await a.client.close();
      await b.client.close();
    });
  });

  // -------------------------------------------------------------------------
  describe("deploy_system: the request is unchanged (create mode, AC6)", () => {
    it("still posts {manifest, dry_run: false, strict: false} to /api/systems/deploy on defaults", async () => {
      reset();
      const s = await session("deploy-defaults");
      await s.call("deploy_system", { manifest: MANIFEST });
      assert.equal(requests.length, 1);
      assert.equal(requests[0].method, "POST");
      assert.equal(requests[0].path, "/api/systems/deploy");
      assert.deepEqual(requests[0].body, { manifest: MANIFEST, dry_run: false, strict: false });
      assert.deepEqual(Object.keys(requests[0].body as object), ["manifest", "dry_run", "strict"]);
      await s.client.close();
    });

    it("passes explicit dry_run and strict through", async () => {
      reset();
      const s = await session("deploy-explicit");
      await s.call("deploy_system", { manifest: MANIFEST, dry_run: true, strict: true });
      assert.deepEqual(requests[0].body, { manifest: MANIFEST, dry_run: true, strict: true });
      await s.client.close();
    });
  });

  // -------------------------------------------------------------------------
  // #4027: a non-2xx deploy answer is a RETURNED result, not a thrown error.
  describe("deploy_system returns its failure report (#4027)", () => {
    const DEPLOY_DEFAULT = jsonReply(200, { status: "valid", system_name: "acme", agents_created: [], warnings: [] });
    const resetDeploy = (reply: Reply) => {
      requests.length = 0;
      deployReply = reply;
    };
    afterEach(() => {
      deployReply = DEPLOY_DEFAULT;
    });
    const FAILED_DEPLOY = {
      status: "failed",
      system_name: "acme",
      agents_created: [],
      prompt_updated: false,
      warnings: [],
      failed: [
        { name: "acme-worker", short_name: "worker", template: "local:worker", reason: "Template not found", status_code: 404 },
        { name: "acme-writer", short_name: "writer", template: "local:writer", reason: "Template not found", status_code: 404 },
      ],
    };

    it("a failed (500) deploy comes back AS the report, with http_status and success: false, audited as a failure", async () => {
      resetDeploy(jsonReply(500, FAILED_DEPLOY));
      const s = await session("deploy-failed-500");
      const { text, isError, row } = await s.call("deploy_system", { manifest: MANIFEST, dry_run: false });

      assert.equal(isError, false, `a failed deploy is a result, not a thrown error: ${text}`);
      assert.equal(text, JSON.stringify({ ...FAILED_DEPLOY, http_status: 500, success: false }, null, 2));
      const out = parsed(text);
      assert.equal(out.status, "failed");
      assert.deepEqual(out.failed, FAILED_DEPLOY.failed, "every member's sanitized reason reaches the caller (ent#125)");

      assert.equal(row.details.success, false, `a failed deploy was audited as a success: ${JSON.stringify(row.details)}`);
      assert.equal(row.details.error, "HTTP 500: status 'failed'");
      assert.equal(row.details.denied, undefined, "a failed deploy is not an authorization denial");
      await s.client.close();
    });

    it("a 403 (the agents.manage capability fence) is returned, and audited as a denial", async () => {
      const detail = {
        code: "agent_management_not_permitted",
        capability: "agents.manage",
        message: "This agent does not hold the agent-management permission.",
      };
      resetDeploy(jsonReply(403, { detail }));
      const s = await session("deploy-403");
      const { text, isError, row } = await s.call("deploy_system", { manifest: MANIFEST, dry_run: false });

      assert.equal(isError, false, `the registered tool threw: ${text}`);
      assert.deepEqual(parsed(text), { detail, http_status: 403, success: false });
      assert.equal(row.details.success, false);
      assert.equal(row.details.denied, true, "a backend 403 stays a denial, as it was when it was thrown");
      assert.equal(row.details.error, "HTTP 403", "a structured detail gets the bare status, as teardown's does (#4026)");
      await s.client.close();
    });

    it("a 403 with a non-JSON body is still a returned object and still a denial", async () => {
      resetDeploy({ status: 403, body: "Forbidden", contentType: "text/plain" });
      const s = await session("deploy-403-text");
      const { text, isError, row } = await s.call("deploy_system", { manifest: MANIFEST, dry_run: false });

      assert.equal(isError, false);
      assert.deepEqual(parsed(text), { success: false, http_status: 403, detail: "Forbidden" });
      assert.equal(row.details.denied, true);
      assert.equal(row.details.error, "HTTP 403");
      await s.client.close();
    });

    const STRICT_ABORT = {
      detail: { error: "Deployment failed", failed_at: "acme-writer", created: ["acme-worker"], reason: "Template not found" },
    };
    const VALIDATION_422 = { detail: [{ loc: ["body", "manifest"], msg: "Field required", type: "missing" }] };
    for (const [label, status, body, reason] of [
      ["a 400 manifest error", 400, { detail: "nothing_to_deploy: every member is deploy: false" }, "HTTP 400: nothing_to_deploy: every member is deploy: false"],
      ["an exhausted 401 (key mode cannot re-authenticate)", 401, { detail: "Invalid or expired API key" }, "HTTP 401: Invalid or expired API key"],
      ["a 422 validation error", 422, VALIDATION_422, "HTTP 422"],
      ["a strict abort, with the members it created first", 404, STRICT_ABORT, "HTTP 404"],
      ["an unexpected 500 that is not a report", 500, { detail: "Deployment failed: boom" }, "HTTP 500: Deployment failed: boom"],
    ] as const) {
      it(`${label}: the body comes back AS the result with http_status and success: false, not a denial`, async () => {
        resetDeploy(jsonReply(status, body));
        const s = await session(`deploy-${status}-${label.length}`);
        const { text, isError, row } = await s.call("deploy_system", { manifest: MANIFEST, dry_run: false, strict: true });

        assert.equal(isError, false, `the registered tool threw: ${text}`);
        assert.deepEqual(parsed(text), { ...body, http_status: status, success: false });
        assert.equal(row.details.success, false);
        assert.equal(row.details.error, reason);
        assert.equal(row.details.denied, undefined);
        await s.client.close();
      });
    }

    it("a strict abort names the members it created, as a structured field", async () => {
      resetDeploy(jsonReply(500, STRICT_ABORT));
      const s = await session("deploy-strict-created");
      const { text } = await s.call("deploy_system", { manifest: MANIFEST, strict: true });
      assert.deepEqual(parsed(text).detail.created, ["acme-worker"]);
      await s.client.close();
    });

    const html = "<html><body><h1>502 Bad Gateway</h1></body></html>";
    for (const [label, reply, detail] of [
      ["a non-JSON body", { status: 502, body: html, contentType: "text/html" } as Reply, html],
      ["a JSON string", jsonReply(502, "upstream unavailable"), "upstream unavailable"],
      ["a JSON array", jsonReply(502, ["upstream", "unavailable"]), ["upstream", "unavailable"]],
      ["JSON null", jsonReply(500, null), null],
    ] as const) {
      it(`${label}: {success: false, http_status, detail}, the raw value never on the audit row`, async () => {
        resetDeploy(reply);
        const s = await session(`deploy-detail-${label.replace(/\W+/g, "-")}`);
        const { text, isError, row } = await s.call("deploy_system", { manifest: MANIFEST, dry_run: false });

        assert.equal(isError, false);
        assert.deepEqual(parsed(text), { success: false, http_status: reply.status, detail });
        assert.equal(row.details.success, false);
        assert.equal(row.details.error, `HTTP ${reply.status}`);
        assert.equal(row.details.denied, undefined);
        await s.client.close();
      });
    }

    for (const [label, body] of [
      ["deployed", { status: "deployed", system_name: "acme", agents_created: ["acme-worker"], prompt_updated: true, warnings: [] }],
      ["partial", { ...FAILED_DEPLOY, status: "partial", agents_created: ["acme-worker"], failed: [FAILED_DEPLOY.failed[1]] }],
      ["a dry-run valid", { status: "valid", system_name: "acme", agents_created: [], agents_to_create: [{ name: "acme-worker", short_name: "worker", template: "local:worker" }], prompt_updated: false, warnings: [] }],
      ["a dry-run invalid", { status: "invalid", system_name: "acme", agents_created: [], prompt_updated: false, warnings: [], failed: [FAILED_DEPLOY.failed[0]] }],
    ] as const) {
      it(`a 200 ${label} report is returned byte-for-byte as before, audited as a success`, async () => {
        resetDeploy(jsonReply(200, body));
        const s = await session(`deploy-200-${label.replace(/\W+/g, "-")}`);
        const { text, isError, row } = await s.call("deploy_system", { manifest: MANIFEST });

        assert.equal(isError, false);
        assert.equal(text, JSON.stringify(body, null, 2));
        assert.equal(row.details.success, true, "the call worked; the report's own status carries the outcome");
        assert.equal(row.details.error, undefined);
        assert.equal(row.details.denied, undefined);
        await s.client.close();
      });
    }

    it("on ONE session: a failed 500, a 200, a 403, a 500: no stamp outlives its own call", async () => {
      const s = await session("deploy-sequence");
      resetDeploy(jsonReply(500, FAILED_DEPLOY));
      const a = await s.call("deploy_system", { manifest: MANIFEST, dry_run: false });
      resetDeploy(jsonReply(200, { status: "deployed", system_name: "acme", agents_created: ["acme-worker"], warnings: [] }));
      const b = await s.call("deploy_system", { manifest: MANIFEST, dry_run: false });
      resetDeploy(jsonReply(403, { detail: "creator role required" }));
      const c = await s.call("deploy_system", { manifest: MANIFEST, dry_run: false });
      resetDeploy(jsonReply(500, FAILED_DEPLOY));
      const d = await s.call("deploy_system", { manifest: MANIFEST, dry_run: false });

      assert.deepEqual(
        [a, b, c, d].map(({ isError, row }) => [isError, row.details.success, row.details.denied]),
        [[false, false, undefined], [false, true, undefined], [false, false, true], [false, false, undefined]],
        "every answer is RETURNED (isError false), and no stamp outlives its own call"
      );
      await s.client.close();
    });

    it("the description tells the caller a failed deploy is a returned report (a description pin)", () => {
      const d = createSystemTools(new TrinityClient("http://127.0.0.1:1"), false).deploySystem.description;
      assert.match(d, /A `failed` deploy \(no member created\) comes back as that report with `http_status: 500`/);
      assert.match(d, /has `success: false`, `http_status` and the backend's `detail`/);
    });

    it("a session with no API key still THROWS, and nothing reaches the backend (unchanged)", async () => {
      resetDeploy(DEPLOY_DEFAULT);
      const s = await inProcess({ scope: "user", userId: "owner", keyName: "keyless-4027" });
      try {
        const { text, isError, row } = await s.call("deploy_system", { manifest: MANIFEST, dry_run: false });
        assert.equal(isError, true, `the keyless edge no longer throws: ${text}`);
        assert.match(text, /MCP API key authentication required/);
        assert.deepEqual(requests, []);
        assert.equal(row.details.success, false);
      } finally {
        await s.close();
      }
    });

    it("a dropped connection (no HTTP status) still THROWS (unchanged)", async () => {
      resetDeploy({ status: 0, body: "", disconnect: true });
      const s = await session("deploy-dropped");
      const { isError, row } = await s.call("deploy_system", { manifest: MANIFEST, dry_run: false });
      assert.equal(isError, true);
      assert.deepEqual(requests.map((r) => r.path), ["/api/systems/deploy"]);
      assert.equal(row.details.success, false);
      assert.equal(row.details.denied, undefined);
      await s.client.close();
    });
  });

  // -------------------------------------------------------------------------
  // #4026: teardown_system through the same registered server. Its caller
  // JSON is pinned byte-for-byte as it was before the stamp existed; only the
  // audit row changes.
  describe("teardown_system's audit row (#4026)", () => {
    const TEARDOWN_404 =
      "System teardown is not available on this platform. Remove members "
      + "individually with DELETE /api/agents/{name}.";
    const TEARDOWN_403 =
      "Refused. System teardown is licensed separately AND is human-only: a "
      + "machine credential (agent, connector, system, ops) cannot remove a "
      + "fleet, because that is the bulk form of an operation that is "
      + "spawn-scoped one agent at a time. Ask a human operator with the "
      + "creator role.";
    const TEARDOWN_503 =
      "Refused: the removal set could not be established — either the "
      + "membership tags or the agent roster could not be read — and teardown "
      + "will not derive a removal set from name prefixes alone. Nothing was "
      + "removed. The detail says which read failed; retry once that dependency "
      + "is reachable.";
    /** The `{success: false, error, detail, ...flags}` envelope teardownFailure has always returned. */
    const envelope = (status: number, body: string, message: string | null, flags: Record<string, unknown> = {}) => {
      const raw = `API error (${status}): ${body}`;
      return JSON.stringify({ success: false, error: message ?? raw, detail: raw, ...flags }, null, 2);
    };
    const FAILED_REPORT = {
      status: "failed",
      system_name: "acme",
      dry_run: false,
      members: [{ name: "acme-a", outcome: "failed", reason: "docker daemon gone" }],
    };
    const NOT_AUTHORIZED_REPORT = {
      ...FAILED_REPORT,
      members: [{ name: "acme-a", outcome: "skipped", reason: "not_authorized" }],
    };
    const call = (s: Awaited<ReturnType<typeof session>>) =>
      s.call("teardown_system", { system_name: "acme", dry_run: false });

    for (const [label, reply, expectedText, reason] of [
      ["404 (no teardown on this build)", jsonReply(404, { detail: "Not Found" }), envelope(404, '{"detail":"Not Found"}', TEARDOWN_404, { not_available: true }), "HTTP 404: Not Found (not_available)"],
      // A SERVING route's 404 (no such system): the caller's text is still the
      // "not available" message (trinity-enterprise#1100), but the row carries
      // the backend's own detail, so the two 404s read differently there.
      ["404 from a serving route (no such system)", jsonReply(404, { detail: "System 'acme' not found" }), envelope(404, `{"detail":"System 'acme' not found"}`, TEARDOWN_404, { not_available: true }), "HTTP 404: System 'acme' not found (not_available)"],
      ["503 (membership unverified)", jsonReply(503, { detail: "tags unreadable" }), envelope(503, '{"detail":"tags unreadable"}', TEARDOWN_503, { membership_unverified: true, retryable: true }), "HTTP 503: tags unreadable (membership_unverified, retryable)"],
      ["400 (confirmed set names no member)", jsonReply(400, { detail: "no current member" }), envelope(400, '{"detail":"no current member"}', null, { bad_request: true }), "HTTP 400: no current member (bad_request)"],
      ["500 carrying a failed report", jsonReply(500, FAILED_REPORT), JSON.stringify(FAILED_REPORT, null, 2), "HTTP 500: status 'failed'"],
      ["an unexpected status (502)", jsonReply(502, { detail: "bad gateway" }), envelope(502, '{"detail":"bad gateway"}', null), "HTTP 502: bad gateway"],
      ["a 500 whose body is not a failed report", jsonReply(500, { detail: "boom" }), envelope(500, '{"detail":"boom"}', null), "HTTP 500: boom"],
      ["a 500 whose body is malformed JSON", { status: 500, body: "not json{", contentType: "text/plain" } as Reply, envelope(500, "not json{", null), "HTTP 500"],
    ] as const) {
      it(`${label}: the caller's text is unchanged, the row is a failure and not a denial`, async () => {
        resetTeardown(reply);
        const s = await session(`td-${label.replace(/\W+/g, "-")}`);
        const { text, isError, row } = await call(s);

        assert.equal(isError, false, `teardown_system threw: ${text}`);
        assert.equal(text, expectedText);
        assert.equal(row.details.success, false, `a returned teardown failure was audited as a success: ${JSON.stringify(row.details)}`);
        assert.equal(row.details.error, reason);
        assert.equal(row.details.denied, undefined, "an operational refusal is not an authorization denial");
        assert.deepEqual(requests.map((r) => r.method), ["DELETE"]);
        await s.client.close();
      });
    }

    it("a dropped connection: still an object, audited as a failure with the transport error", async () => {
      resetTeardown({ status: 0, body: "", disconnect: true });
      const s = await session("td-dropped");
      const { text, isError, row } = await call(s);
      const out = parsed(text);

      assert.equal(isError, false);
      assert.deepEqual(Object.keys(out), ["success", "error", "detail"]);
      assert.equal(out.success, false);
      assert.equal(out.error, out.detail);
      assert.equal(row.details.success, false);
      assert.equal(row.details.error, out.error);
      assert.equal(row.details.denied, undefined);
      await s.client.close();
    });

    // The row's reason names the backend's own cause, so the two 403s stay
    // distinguishable on the row; the caller's paragraph is the same for both.
    for (const [label, body, reason] of [
      ["the licensing / human-only refusal", JSON.stringify({ detail: "Enterprise feature 'system_teardown' is not licensed" }), "HTTP 403: Enterprise feature 'system_teardown' is not licensed"],
      ["a failed report where every member was not_authorized", JSON.stringify(NOT_AUTHORIZED_REPORT), "HTTP 403: status 'failed'"],
      ["a structured detail", JSON.stringify({ detail: { code: "person_required" } }), "HTTP 403"],
      ["a non-JSON body", "Forbidden", "HTTP 403"],
    ] as const) {
      it(`403, ${label}: the caller's text is unchanged, the row is a denial`, async () => {
        resetTeardown({ status: 403, body });
        const s = await session(`td-403-${label.replace(/\W+/g, "-")}`);
        const { text, row } = await call(s);

        assert.equal(text, envelope(403, body, TEARDOWN_403, { not_permitted: true }));
        assert.equal(row.details.success, false, `a teardown 403 was audited as a success: ${JSON.stringify(row.details)}`);
        assert.equal(row.details.denied, true);
        assert.equal(row.details.error, reason);
        await s.client.close();
      });
    }

    for (const [label, body] of [
      ["a preview", { status: "preview", system_name: "acme", members: [{ name: "acme-a" }] }],
      ["a partial teardown", { status: "partial", system_name: "acme", members: [{ name: "acme-a", outcome: "removed" }, { name: "acme-b", outcome: "failed" }] }],
    ] as const) {
      it(`200 ${label}: returned verbatim, audited success: true`, async () => {
        resetTeardown(jsonReply(200, body));
        const s = await session(`td-ok-${label.replace(/\W+/g, "-")}`);
        const { text, row } = await call(s);

        assert.equal(text, JSON.stringify(body, null, 2));
        assert.equal(row.details.success, true);
        assert.equal(row.details.error, undefined);
        assert.equal(row.details.denied, undefined);
        await s.client.close();
      });
    }
  });

  // -------------------------------------------------------------------------
  describe("it never throws", () => {
    it("a session with no API key gets a RETURNED refusal from the registered tool, and nothing reaches the backend", async () => {
      reset();
      const s = await inProcess({ scope: "user", userId: "owner", keyName: "keyless-695" });
      try {
        // A REAL run, so the no-note assertion below is not vacuous (a dry run never carries one).
        const { text, isError, row } = await s.call("apply_system", { manifest: MANIFEST, dry_run: false });

        assert.equal(isError, false, `the registered tool threw: ${text}`);
        const out = parsed(text);
        assert.equal(out.success, false);
        assert.match(out.error, /MCP API key authentication required/);
        assert.equal(out.note, undefined, "nothing was sent, so nothing can have been applied");
        assert.deepEqual(requests, [], "no backend request may be made without the caller's key");
        assert.equal(row.details.denied, undefined);
        assert.equal(row.details.success, false, `a missing key was audited as a success: ${JSON.stringify(row.details)}`);
        assert.equal(row.details.error, out.error);
      } finally {
        await s.close();
      }
    });

    it("a dropped connection (no HTTP status) through the registered HTTP tool is still an object", async () => {
      reset({ status: 0, body: "", disconnect: true });
      const s = await session("dropped");
      const { text, isError, row } = await s.call("apply_system", { manifest: MANIFEST, dry_run: false });

      assert.equal(isError, false, `the registered tool threw: ${text}`);
      const out = parsed(text);
      assert.equal(out.success, false);
      assert.equal(typeof out.error, "string");
      assert.equal(out.http_status, undefined);
      assert.deepEqual(requests.map((r) => r.path), ["/api/systems/apply"], "the request reached the backend and was dropped there");
      assert.equal(row.details.denied, undefined);
      assert.equal(row.details.success, false, `a dropped connection was audited as a success: ${JSON.stringify(row.details)}`);
      assert.equal(row.details.error, `${out.error} (real run, outcome unknown)`);
      // A real run whose answer was lost may have applied writes: say so (fresh review #6).
      assert.match(out.note, /some writes may already have been applied/);
      assert.match(out.note, /dry_run: true/);
      await s.client.close();
    });

    it("a dropped DRY run carries no may-have-applied note (a dry run writes nothing)", async () => {
      reset({ status: 0, body: "", disconnect: true });
      const s = await session("dropped-dry");
      const { text, row } = await s.call("apply_system", { manifest: MANIFEST });
      const out = parsed(text);
      assert.equal(out.success, false);
      assert.equal(out.note, undefined);
      assert.equal(row.details.success, false);
      assert.equal(row.details.error, out.error, "a dry run cannot have applied anything, so no outcome-unknown suffix");
      await s.client.close();
    });
  });

  // -------------------------------------------------------------------------
  describe("a person-only tool (the backend's require_person)", () => {
    const asKey = async (key: string) => {
      const client = new Client({ name: key, version: "1.0.0" });
      await client.connect(
        new StreamableHTTPClientTransport(mcpUrl, { requestInit: { headers: { Authorization: `Bearer ${key}` } } })
      );
      return client;
    };

    it("a user key lists it; an agent key and a system key neither list nor can call it", async () => {
      reset();
      const user = await asKey("trinity_mcp_695_userkey_vis");
      try {
        assert.ok((await user.listTools()).tools.some((t) => t.name === "apply_system"));
      } finally {
        await user.close();
      }
      for (const key of ["trinity_mcp_695_agentkey_vis", "trinity_mcp_695_systemkey_vis"]) {
        const client = await asKey(key);
        try {
          const names = (await client.listTools()).tools.map((t) => t.name);
          assert.ok(names.length > 0, `${key}: the session lists no tools at all`);
          assert.ok(!names.includes("apply_system"), `${key}: apply_system is advertised to a session the backend refuses`);
          await assert.rejects(
            () => client.callTool({ name: "apply_system", arguments: { manifest: MANIFEST } }),
            (err: unknown) => /not found|unknown tool|method not found/i.test(String((err as Error)?.message ?? err)),
            `${key}: calling apply_system by name must be refused as an unknown tool`,
          );
        } finally {
          await client.close();
        }
      }
      assert.deepEqual(requests, [], "a hidden tool never reaches the backend");
    });
  });

  // -------------------------------------------------------------------------
  describe("registration", () => {
    it("is advertised next to deploy_system with exactly {manifest, dry_run?}", async () => {
      const s = await session("list");
      const { tools } = await s.client.listTools();
      const apply = tools.find((t) => t.name === "apply_system");
      assert.ok(apply, "apply_system is not registered");
      assert.ok(tools.some((t) => t.name === "deploy_system"));
      assert.deepEqual(Object.keys((apply.inputSchema as any).properties).sort(), ["dry_run", "manifest"]);
      assert.deepEqual((apply.inputSchema as any).required, ["manifest"]);
      await s.client.close();
    });
  });
});

// ---------------------------------------------------------------------------
// The other shape, another HOSTNAME: a second server configured for an https
// DNS origin on a non-default port, its REGISTERED tool driven in-process
// (FastMCP's own session), with `fetch` stubbed so nothing leaves the host.
// ---------------------------------------------------------------------------

const realFetch = globalThis.fetch;
const okJson = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("ent#695 the other shape: another HOSTNAME (https, non-default port), registered tool", () => {
  afterEach(() => {
    globalThis.fetch = realFetch;
  });

  it("a server configured for that origin forwards the call there, with the caller's key, and nowhere else", async () => {
    const origin = "https://trinity-b.example.test:8443";
    const seen: Array<{ url: string; authorization: string | undefined; body: unknown }> = [];
    let audited = 0;
    globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
      const url = String(input);
      if (url === `${origin}/api/internal/audit`) {
        audited++;
        return okJson({ status: "logged" });
      }
      const headers = (init?.headers ?? {}) as Record<string, string>;
      seen.push({ url, authorization: headers.Authorization, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      return okJson(PLAN);
    }) as typeof fetch;

    const { server } = await createServer({ trinityApiUrl: origin, requireApiKey: true, internalApiSecret: "s" });
    seen.length = 0; // the boot-time health probe is not the tool call under test
    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
    const client = new Client({ name: "hostname-b", version: "1.0.0" });
    const [serverSession] = await Promise.all([
      (server as any).connect(serverTransport, { scope: "user", userId: "u2", keyName: "kb", mcpApiKey: "trinity_mcp_caller_b" }),
      client.connect(clientTransport),
    ]);
    let r: any;
    try {
      r = await client.callTool({ name: "apply_system", arguments: { manifest: MANIFEST } });
      // The audit POST is fire-and-forget: wait for it so it never lands on the real fetch.
      for (const deadline = Date.now() + 5000; audited === 0 && Date.now() < deadline; ) {
        await new Promise((res) => setTimeout(res, 25));
      }
    } finally {
      // The caller owns a connect()ed session; its ping interval would keep the
      // process alive (a red run would hang instead of reporting).
      await client.close();
      await serverSession.close();
    }

    assert.equal(r.isError === true, false);
    assert.equal(r.content.map((c: any) => c.text).join("\n"), JSON.stringify(PLAN, null, 2));
    assert.deepEqual(seen, [
      { url: `${origin}/api/systems/apply`, authorization: "Bearer trinity_mcp_caller_b", body: { manifest: MANIFEST, dry_run: true } },
    ]);
    assert.equal(audited, 1, "the registered, audit-wrapped tool posted its row to the same origin");
  });
});

describe("ent#695 apply_system's access row and description", () => {
  const tools = createSystemTools(new TrinityClient("http://backend"), false);

  it("has an access.ts row (the server refuses to boot without one), and the row fits the tool's parameters", () => {
    assert.deepEqual(TOOL_ACCESS_POLICY.apply_system, {
      kind: "none",
      why: "a system manifest, no agent target; person-only: its own canAccess shows it to user-scoped sessions alone, and the backend's require_person refuses every other key",
    });
    assert.deepEqual(policyFor(tools.applySystem), TOOL_ACCESS_POLICY.apply_system);
  });

  it("pins the description's claims (a description pin: the behaviour itself is tested above)", () => {
    const d: string = tools.applySystem.description;
    assert.match(d, /dry_run defaults to TRUE/);
    assert.match(d, /never the HTTP code/);
    assert.match(d, /A result without `status`[^.]*has `success: false` and a `detail`/);
    assert.match(d, /never deletes, stops, restarts, renames or suffixes an existing agent/);
    assert.match(d, /person's credential/);
    assert.match(d, /not_available/);
    assert.match(d, /do NOT fall back to deploy_system/);
    assert.ok(d.length <= 2048, `description is ${d.length} characters; Claude Code cuts at 2048 (#3234)`);
  });
});
