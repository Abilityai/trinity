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
 *    mapped here; every other JSON body is returned AS the result with
 *    `http_status` and `success: false` added, so a status-code change in the
 *    backend contract cannot silently break the tool.
 * 4. **Audit honesty.** A 403 goes through `access.ts::accessDenied`, so its
 *    tool-call audit row says `denied: true` (#2807). A 409 / 503 / 500 is an
 *    operational refusal, not an authorization denial, and is not stamped as
 *    one. (Those rows read `success: true` today because `ToolOutcome` has only
 *    the `denied` kind — the registered follow-up in types.ts. These tests
 *    assert only that they are not denials, so the follow-up flips nothing
 *    here.)
 * 5. **`deploy_system` is unchanged** (create mode is byte-identical, AC6): its
 *    request and its throw-on-500 are pinned through the same registered path.
 *
 * Harness: a real `createServer` in key mode, a real MCP client over the
 * streamable-HTTP transport presenting a person's USER-scoped key (apply's
 * legitimate caller), and a stub backend that answers key validation, RECORDS
 * every other request it receives (method, path, Authorization, body) and every
 * fire-and-forget audit row, and answers `/api/systems/apply` and
 * `/api/systems/deploy` with whatever the test sets. The backend sits on a
 * random port of 127.0.0.1 — not the default `localhost:8000` — which is the
 * brief's "another origin" shape; the last suite adds another HOSTNAME
 * (https, non-default port, a DNS name) through the real client with a stubbed
 * `fetch`. Pattern: `access-wiring.test.ts` (#2952 row draining).
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { strict as assert } from "node:assert";
import { after, afterEach, before, describe, it } from "node:test";
import { createServer as createHttpServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

import { createServer } from "../server.js";
import { createSystemTools } from "./systems.js";
import { TOOL_ACCESS_POLICY, policyFor } from "../access.js";
import { configureAudit, withAudit } from "../audit.js";
import { TrinityClient } from "../client.js";

const MANIFEST = "name: acme\nagents:\n  worker:\n    template: local:worker\n";

/** A backend answer the test chooses: status, body text, content type. */
interface Reply {
  status: number;
  body: string;
  contentType?: string;
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
  let mcpServer: { stop: () => Promise<void> };
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

  before(async () => {
    backend = createHttpServer((req, res) => {
      let raw = "";
      req.on("data", (c) => (raw += c));
      req.on("end", () => {
        const answer = (r: Reply) => {
          res.writeHead(r.status, { "Content-Type": r.contentType ?? "application/json" });
          res.end(r.body);
        };
        const url = req.url ?? "";
        const authorization = String(req.headers.authorization ?? "");
        if (url === "/api/mcp/validate") {
          // A person's user-scoped key; the bearer is echoed back as key_name
          // so every audit row carries which session made the call (#2952).
          return answer(jsonReply(200, {
            valid: true,
            key_id: "key-user-1",
            user_id: "owner",
            user_email: "owner@example.com",
            key_name: authorization.replace(/^Bearer /, ""),
            scope: "user",
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
        return answer(jsonReply(200, {})); // health probe, anything else
      });
    });
    await new Promise<void>((r) => backend.listen(0, "127.0.0.1", () => r()));
    backendOrigin = `http://127.0.0.1:${(backend.address() as AddressInfo).port}`;

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
    await server.start({ transportType: "httpStream", httpStream: { port: mcpPort, host: "127.0.0.1" } });
    mcpServer = server;
    mcpUrl = new URL(`http://127.0.0.1:${mcpPort}/mcp`);
  });

  after(async () => {
    await mcpServer?.stop();
    await new Promise<void>((r) => backend.close(() => r()));
  });

  /** A clean slate per case. */
  const reset = (apply: Reply = jsonReply(200, PLAN)) => {
    requests.length = 0;
    applyReply = apply;
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

  /** A client holding a person's own user-scoped key. */
  const session = async (name: string) => {
    const key = `trinity_mcp_695_${name}`;
    const client = new Client({ name, version: "1.0.0" });
    await client.connect(
      new StreamableHTTPClientTransport(mcpUrl, { requestInit: { headers: { Authorization: `Bearer ${key}` } } })
    );
    const call = async (tool: string, args: Record<string, unknown>) => {
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
    return { client, call, key };
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
  describe("every other JSON body is the result", () => {
    for (const [status, report] of [
      [409, REFUSED_409],
      [503, REFUSED_503],
      [500, FAILED_500],
    ] as const) {
      it(`a ${status} report comes back AS the result, with http_status and success: false, and is not audited as a denial`, async () => {
        reset(jsonReply(status, report));
        const s = await session(`report-${status}`);
        const { text, isError, row } = await s.call("apply_system", { manifest: MANIFEST, dry_run: false });

        assert.equal(isError, false, "an operational refusal is a result, not a thrown error");
        assert.deepEqual(parsed(text), { ...report, http_status: status, success: false });
        assert.equal(row.details.denied, undefined, `a ${status} is an operational refusal, never an authorization denial`);
        await s.client.close();
      });
    }

    it("a 400 or 422 detail is passed through the same way", async () => {
      reset(jsonReply(422, { detail: [{ loc: ["body", "mode"], msg: "Extra inputs are not permitted", type: "extra_forbidden" }] }));
      const s = await session("validation-422");
      const { text } = await s.call("apply_system", { manifest: MANIFEST });
      const out = parsed(text);
      assert.equal(out.http_status, 422);
      assert.equal(out.success, false);
      assert.equal(out.detail[0].type, "extra_forbidden");
      await s.client.close();
    });

    it("a non-JSON body becomes {success: false, http_status, detail}", async () => {
      const html = "<html><body><h1>502 Bad Gateway</h1></body></html>";
      reset({ status: 502, body: html, contentType: "text/html" });
      const s = await session("non-json");
      const { text, isError, row } = await s.call("apply_system", { manifest: MANIFEST, dry_run: false });

      assert.equal(isError, false);
      assert.deepEqual(parsed(text), { success: false, http_status: 502, detail: html });
      assert.equal(row.details.denied, undefined);
      await s.client.close();
    });

    it("a JSON body that is not an object (a bare string) becomes {success: false, http_status, detail}", async () => {
      reset(jsonReply(502, "upstream unavailable"));
      const s = await session("json-scalar");
      const { text } = await s.call("apply_system", { manifest: MANIFEST, dry_run: false });
      assert.deepEqual(parsed(text), { success: false, http_status: 502, detail: "upstream unavailable" });
      await s.client.close();
    });
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
  describe("deploy_system is unchanged (create mode, AC6)", () => {
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

    it("still THROWS on a failed (500) deploy — the create-mode gap AC6 keeps", async () => {
      const report = { status: "failed", system_name: "acme", agents_created: [], warnings: [], failed: [{ name: "acme-worker", reason: "boom" }] };
      reset();
      deployReply = jsonReply(500, report);
      try {
        const s = await session("deploy-500");
        const { text, isError, row } = await s.call("deploy_system", { manifest: MANIFEST, dry_run: false });
        assert.equal(isError, true, `deploy_system no longer throws on 500: ${text}`);
        assert.match(text, /API error \(500\)/);
        assert.match(text, /"status":"failed"/, "the thrown message still embeds the report (ent#125)");
        assert.equal(row.details.success, false);
        await s.client.close();
      } finally {
        deployReply = jsonReply(200, { status: "valid", system_name: "acme", agents_created: [], warnings: [] });
      }
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
// Paths a real transport cannot reach, still through the audit wrapper.
// ---------------------------------------------------------------------------

const realFetch = globalThis.fetch;
const okJson = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("ent#695 apply_system never throws", () => {
  afterEach(() => {
    globalThis.fetch = realFetch;
  });

  it("a missing API key is RETURNED, and nothing is sent to the backend", async () => {
    // Over the transport, `authenticate` refuses a keyless request before any
    // tool runs, so this branch is reachable only by a session without a key;
    // it must still come back as an object (client resolution inside the try).
    const sent: string[] = [];
    configureAudit({ apiUrl: "http://audit.test", secret: "s" });
    globalThis.fetch = (async (input: unknown) => {
      const url = String(input);
      if (!url.endsWith("/api/internal/audit")) sent.push(url);
      return okJson({ status: "logged" });
    }) as typeof fetch;

    const tools = createSystemTools(new TrinityClient("http://backend.invalid"), true);
    const wrapped = withAudit("apply_system", tools.applySystem.execute);
    const text = await wrapped({ manifest: MANIFEST }, { session: { scope: "user", userId: "u1" } as any });
    const out = JSON.parse(text);

    assert.equal(out.success, false);
    assert.match(out.error, /MCP API key authentication required/);
    assert.deepEqual(sent, [], "no backend request may be made without the caller's key");
  });

  it("an unrecognised failure (no HTTP status) is still an object", async () => {
    const fake = {
      getBaseUrl: () => "http://backend",
      setToken: () => {},
      request: async () => {
        throw new Error("socket hang up");
      },
    } as unknown as TrinityClient;
    const tools = createSystemTools(fake, false);
    const out = JSON.parse(await tools.applySystem.execute({ manifest: MANIFEST }, {}));
    assert.equal(out.success, false);
    assert.match(out.error, /socket hang up/);
    assert.equal(out.http_status, undefined);
  });
});

describe("ent#695 the other shape: another HOSTNAME (https, non-default port), real client", () => {
  afterEach(() => {
    globalThis.fetch = realFetch;
  });

  it("forwards to that origin with the caller's key and reaches nothing else", async () => {
    const origin = "https://trinity-b.example.test:8443";
    const seen: Array<{ url: string; authorization: string | undefined; body: unknown }> = [];
    configureAudit({ apiUrl: origin, secret: "s" });
    globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/internal/audit")) return okJson({ status: "logged" });
      const headers = (init?.headers ?? {}) as Record<string, string>;
      seen.push({ url, authorization: headers.Authorization, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      return okJson(PLAN);
    }) as typeof fetch;

    const tools = createSystemTools(new TrinityClient(origin), true);
    const wrapped = withAudit("apply_system", tools.applySystem.execute);
    const text = await wrapped(
      { manifest: MANIFEST },
      { session: { scope: "user", userId: "u2", mcpApiKey: "trinity_mcp_caller_b" } as any }
    );

    assert.equal(text, JSON.stringify(PLAN, null, 2));
    assert.deepEqual(seen, [
      { url: `${origin}/api/systems/apply`, authorization: "Bearer trinity_mcp_caller_b", body: { manifest: MANIFEST, dry_run: true } },
    ]);
  });
});

describe("ent#695 apply_system's access row and description", () => {
  const tools = createSystemTools(new TrinityClient("http://backend"), false);

  it("has an access.ts row (the server refuses to boot without one), and the row fits the tool's parameters", () => {
    assert.deepEqual(TOOL_ACCESS_POLICY.apply_system, {
      kind: "none",
      why: "a system manifest; the backend enforces per-member manage checks and refuses agent keys",
    });
    assert.deepEqual(policyFor(tools.applySystem), TOOL_ACCESS_POLICY.apply_system);
  });

  it("tells the model what the code does", () => {
    const d: string = tools.applySystem.description;
    assert.match(d, /dry_run defaults to TRUE/);
    assert.match(d, /never the HTTP code/);
    assert.match(d, /never deletes, stops, restarts, renames or suffixes an existing agent/);
    assert.match(d, /person's credential/);
    assert.match(d, /not_available/);
    assert.match(d, /do NOT fall back to deploy_system/);
    assert.ok(d.length <= 2048, `description is ${d.length} characters; Claude Code cuts at 2048 (#3234)`);
  });
});
