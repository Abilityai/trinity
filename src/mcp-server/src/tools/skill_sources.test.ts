/**
 * abilityai/trinity-enterprise#692 — skill-library SOURCE management over MCP.
 *
 * The fence is the PRINCIPAL, not the transport. The REST routes behind these
 * tools are `require_admin` (+ `reject_agent_principal`), which already admits an
 * admin's user-scoped key and the system agent and refuses agent and connector
 * keys. The tools must therefore:
 *
 *   1. be advertised ONLY to `user` and `system` scope — an allow-list (#848), so
 *      agent, connector, anonymous, `ops` and any scope invented tomorrow fail
 *      closed — and pinned over the real transport, not just the predicate;
 *   2. refuse a non-allowlisted session in-tool before any round trip (defence in
 *      depth; the backend refuses it regardless);
 *   3. proxy the exact REST route with the exact body, returning the REST body
 *      unchanged (AC 1 "byte-equivalent");
 *   4. hand back a REST refusal as the route's own named detail, not a generic
 *      failure (AC 4 — the embedded-credential refusal);
 *   5. tell `ran` / `refused_busy` / `failed` apart on sync, and say where a
 *      started fleet re-inject's outcome lands (AC 5).
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { after, before, describe, it } from "node:test";
import { strict as assert } from "node:assert";
import { createServer as createHttpServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

import { createSkillSourceTools, SOURCE_ADMIN_SCOPES } from "./skill_sources.js";
import { ApiError, type TrinityClient } from "../client.js";
import { TOOL_ACCESS_POLICY } from "../access.js";
import { createServer } from "../server.js";
import type { McpAuthContext } from "../types.js";

const TOOL_NAMES = [
  "list_skill_sources",
  "register_skill_source",
  "update_skill_source",
  "delete_skill_source",
  "sync_skill_source",
  "sync_skill_library",
];

type Recorded = { method: string; path: string; body?: unknown };

function makeTools(answer: unknown | ApiError, requireApiKey = false) {
  const calls: Recorded[] = [];
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    request: async (method: string, path: string, body?: unknown) => {
      calls.push({ method, path, body });
      if (answer instanceof ApiError) throw answer;
      return answer as never;
    },
  };
  return { tools: createSkillSourceTools(fake as TrinityClient, requireApiKey) as any, calls };
}

const session = (scope: string, agentName?: string) => ({
  session: { scope, agentName, userId: "owner", keyId: "k1", keyName: "kn", mcpApiKey: "trinity_mcp_x" } as unknown as McpAuthContext,
});

describe("ent#692 visibility — an allow-list of user and system scope", () => {
  const { tools } = makeTools({});

  it("exports exactly the six tools", () => {
    assert.deepEqual(Object.values(tools).map((t: any) => t.name).sort(), [...TOOL_NAMES].sort());
  });

  it("pins the scope set — widening it (e.g. a controller scope, ent#693) must be deliberate", () => {
    assert.deepEqual([...SOURCE_ADMIN_SCOPES].sort(), ["system", "user"]);
  });

  for (const name of TOOL_NAMES) {
    it(`${name}: advertised to user and system, hidden from everyone else`, () => {
      const tool = Object.values(tools).find((t: any) => t.name === name) as any;
      assert.equal(tool.canAccess({ scope: "user" }), true);
      assert.equal(tool.canAccess({ scope: "system" }), true);
      for (const scope of ["agent", "connector", "anonymous", "portal_delegate", "ops", "controller-from-the-future"]) {
        assert.equal(tool.canAccess({ scope, agentName: "a" }), false, `${name} leaked to ${scope}`);
      }
      assert.equal(tool.canAccess({}), false);
      assert.equal(tool.canAccess(undefined), false);
      assert.equal(tool.canAccess(null), false);
    });
  }

  it("every tool has a TOOL_ACCESS_POLICY row naming the backend admin fence", () => {
    for (const name of TOOL_NAMES) {
      const row = TOOL_ACCESS_POLICY[name] as any;
      assert.ok(row, `${name} has no policy row`);
      assert.equal(row.kind, "none");
      assert.match(row.why, /require_admin/);
    }
  });
});

describe("ent#692 in-tool gate (defence in depth)", () => {
  for (const scope of ["agent", "connector", "ops"]) {
    it(`refuses a ${scope} session before touching the backend, with the audited envelope`, async () => {
      const { tools, calls } = makeTools({});
      const ctx = session(scope, "some-agent");
      const out = JSON.parse(await tools.registerSkillSource.execute({ url: "https://github.com/acme/skills" }, ctx));
      assert.equal(out.success, false);
      assert.equal(out.error, "Access denied");
      assert.match(out.reason, /admin/);
      assert.equal(calls.length, 0);
      assert.equal((ctx as any).outcome?.kind, "denied");
    });
  }

  it("refuses an absent session when API keys are required, admits it in dev mode", async () => {
    const prod = makeTools({ sources: [] }, true);
    const out = JSON.parse(await prod.tools.listSkillSources.execute({}, undefined));
    assert.equal(out.error, "Access denied");
    assert.equal(prod.calls.length, 0);

    const dev = makeTools({ sources: [] }, false);
    await dev.tools.listSkillSources.execute({}, undefined);
    assert.equal(dev.calls.length, 1);
  });
});

describe("ent#692 proxying — the exact route, the exact body, the body back unchanged", () => {
  it("list_skill_sources → GET /api/skills/sources, verbatim", async () => {
    const body = { configured: true, sources: [{ id: "src_1", url: "https://github.com/acme/skills", last_error: "x" }] };
    const { tools, calls } = makeTools(body);
    const out = await tools.listSkillSources.execute({}, session("user"));
    assert.deepEqual(calls, [{ method: "GET", path: "/api/skills/sources", body: undefined }]);
    assert.deepEqual(JSON.parse(out), body);
  });

  it("register_skill_source → POST /api/skills/sources/apply with only the named fields", async () => {
    const body = { action: "created", source: { id: "src_1" } };
    const { tools, calls } = makeTools(body);
    const out = await tools.registerSkillSource.execute(
      { url: "https://github.com/acme/skills", ref: "v1", ref_type: "tag", priority: 50 },
      session("user"),
    );
    assert.equal(calls[0].method, "POST");
    assert.equal(calls[0].path, "/api/skills/sources/apply");
    assert.deepEqual(calls[0].body, { url: "https://github.com/acme/skills", ref: "v1", ref_type: "tag", priority: 50 });
    assert.deepEqual(JSON.parse(out), body);
  });

  it("register_skill_source omits unnamed fields (a re-apply never resets them)", async () => {
    const { tools, calls } = makeTools({ action: "unchanged", source: {} });
    await tools.registerSkillSource.execute({ url: "https://github.com/acme/skills" }, session("system"));
    assert.deepEqual(calls[0].body, { url: "https://github.com/acme/skills" });
  });

  it("update_skill_source → PUT /api/skills/sources/{id} with the patch only", async () => {
    const { tools, calls } = makeTools({ id: "src/1" });
    await tools.updateSkillSource.execute({ source_id: "src/1", ref: "v2", enabled: false }, session("user"));
    assert.equal(calls[0].method, "PUT");
    assert.equal(calls[0].path, "/api/skills/sources/src%2F1");
    assert.deepEqual(calls[0].body, { ref: "v2", enabled: false });
  });

  it("delete_skill_source → DELETE /api/skills/sources/{id}, verbatim", async () => {
    const body = { deleted: true, source_id: "src_1", checkout_reclaimed: true };
    const { tools, calls } = makeTools(body);
    const out = await tools.deleteSkillSource.execute({ source_id: "src_1" }, session("user"));
    assert.deepEqual(calls[0], { method: "DELETE", path: "/api/skills/sources/src_1", body: undefined });
    assert.deepEqual(JSON.parse(out), body);
  });

  it("a REST refusal comes back as the route's own named detail (AC 4)", async () => {
    const detail =
      "Repository URL must not embed a token or password. It is stored and displayed in plain text. " +
      "Configure a GitHub PAT in Settings for private repositories instead.";
    const { tools } = makeTools(new ApiError(400, JSON.stringify({ detail })));
    const out = JSON.parse(
      await tools.registerSkillSource.execute({ url: "https://tok_placeholder@github.com/acme/skills" }, session("user")),
    );
    assert.equal(out.success, false);
    assert.equal(out.status, 400);
    assert.equal(out.error, detail);
  });

  it("a structured detail (ambiguous_source) is passed through as an object", async () => {
    const detail = { code: "ambiguous_source", message: "more than one", source_ids: ["a", "b"] };
    const { tools } = makeTools(new ApiError(409, JSON.stringify({ detail })));
    const out = JSON.parse(await tools.registerSkillSource.execute({ url: "https://github.com/acme/skills" }, session("user")));
    assert.equal(out.status, 409);
    assert.deepEqual(out.error, detail);
  });

  it("a non-admin user key's backend refusal is reported, not swallowed", async () => {
    const { tools } = makeTools(new ApiError(403, JSON.stringify({ detail: "Admin access required" })));
    const out = JSON.parse(await tools.listSkillSources.execute({}, session("user")));
    assert.equal(out.status, 403);
    assert.equal(out.error, "Admin access required");
  });

  it("a 5xx propagates (a server fault is not a refusal)", async () => {
    const { tools } = makeTools(new ApiError(500, "boom"));
    await assert.rejects(() => tools.listSkillSources.execute({}, session("user")), /boom/);
  });
});

describe("ent#692 honest sync results (AC 5)", () => {
  it("sync_skill_library ran: the REST body intact, plus where the re-inject outcome lands", async () => {
    const body = { success: true, commit_sha: "abc1234", commit_changed: true, fleet_reinject_started: true };
    const { tools, calls } = makeTools(body);
    const out = JSON.parse(await tools.syncSkillLibrary.execute({}, session("user")));
    assert.deepEqual(calls[0], { method: "POST", path: "/api/skills/library/sync", body: undefined });
    assert.equal(out.outcome, "ran");
    assert.deepEqual(out.result, body);
    assert.equal(out.fleet_reinject.started, true);
    assert.match(out.fleet_reinject.read_outcome_with, /get_skills_library_status/);
    assert.match(out.fleet_reinject.read_outcome_with, /last_fleet_reinject/);
  });

  it("sync_skill_library without a re-inject says it did not start one", async () => {
    const { tools } = makeTools({ success: true, commit_changed: false, fleet_reinject_started: false });
    const out = JSON.parse(await tools.syncSkillLibrary.execute({}, session("user")));
    assert.equal(out.outcome, "ran");
    assert.equal(out.fleet_reinject.started, false);
  });

  it("409 busy is refused_busy and retryable, never a success", async () => {
    const { tools } = makeTools(new ApiError(409, JSON.stringify({ detail: "Skills library sync already in progress" })));
    const out = JSON.parse(await tools.syncSkillLibrary.execute({}, session("user")));
    assert.equal(out.outcome, "refused_busy");
    assert.equal(out.retryable, true);
    assert.equal(out.status, 409);
    assert.match(out.error, /in progress/);
  });

  it("400 is failed, with the route's error", async () => {
    const { tools } = makeTools(new ApiError(400, JSON.stringify({ detail: "No skills sources configured" })));
    const out = JSON.parse(await tools.syncSkillLibrary.execute({}, session("user")));
    assert.equal(out.outcome, "failed");
    assert.equal(out.error, "No skills sources configured");
  });

  it("sync_skill_source → POST /api/skills/sources/{id}/sync, same vocabulary", async () => {
    const ok = makeTools({ success: true, sources: [{ id: "src_1", status: "success" }] });
    const out = JSON.parse(await ok.tools.syncSkillSource.execute({ source_id: "src_1" }, session("user")));
    assert.deepEqual(ok.calls[0], { method: "POST", path: "/api/skills/sources/src_1/sync", body: undefined });
    assert.equal(out.outcome, "ran");
    assert.equal(out.fleet_reinject.started, false);

    const busy = makeTools(new ApiError(409, JSON.stringify({ detail: "busy" })));
    assert.equal(JSON.parse(await busy.tools.syncSkillSource.execute({ source_id: "src_1" }, session("user"))).outcome, "refused_busy");

    const missing = makeTools(new ApiError(404, JSON.stringify({ detail: "Skill source not found" })));
    const nf = JSON.parse(await missing.tools.syncSkillSource.execute({ source_id: "nope" }, session("user")));
    assert.equal(nf.outcome, "refused");
    assert.equal(nf.status, 404);
  });
});

// ---------------------------------------------------------------------------
// Over the real transport: what each kind of key is actually advertised.
// ---------------------------------------------------------------------------
describe("ent#692 visibility over the real transport (createServer + MCP client)", () => {
  let backend: Server;
  let mcpServer: { stop: () => Promise<void> };
  let mcpUrl: URL;
  const applyPosts: unknown[] = [];

  const SCOPE_BY_KEY: Record<string, { scope: string; agent_name?: string }> = {
    trinity_mcp_user: { scope: "user" },
    trinity_mcp_system: { scope: "system", agent_name: "trinity-system" },
    trinity_mcp_agent: { scope: "agent", agent_name: "alpha" },
    trinity_mcp_connector: { scope: "connector", agent_name: "alpha" },
  };

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
          const key = String(req.headers.authorization ?? "").replace(/^Bearer /, "");
          const row = SCOPE_BY_KEY[key];
          if (!row) return send(200, { valid: false });
          return send(200, { valid: true, key_id: key, user_id: "owner", key_name: key, ...row });
        }
        if (url === "/api/skills/sources/apply" && req.method === "POST") {
          applyPosts.push(JSON.parse(body));
          return send(201, { action: "created", source: { id: "src_1" } });
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

  async function withKey<T>(key: string, body: (client: Client) => Promise<T>): Promise<T> {
    const client = new Client({ name: "ent692", version: "0.0.0" });
    const transport = new StreamableHTTPClientTransport(mcpUrl, {
      requestInit: { headers: { Authorization: `Bearer ${key}` } },
    });
    await client.connect(transport);
    try {
      return await body(client);
    } finally {
      await client.close().catch(() => {});
    }
  }

  const listed = (key: string) =>
    withKey(key, async (c) => new Set((await c.listTools()).tools.map((t) => t.name)));

  for (const key of ["trinity_mcp_user", "trinity_mcp_system"]) {
    it(`${SCOPE_BY_KEY[key].scope} key sees all six tools`, async () => {
      const names = await listed(key);
      for (const t of TOOL_NAMES) assert.ok(names.has(t), `${t} missing for ${key}`);
    });
  }

  for (const key of ["trinity_mcp_agent", "trinity_mcp_connector"]) {
    it(`${SCOPE_BY_KEY[key].scope} key sees none of them, and cannot call one by name`, async () => {
      const names = await listed(key);
      for (const t of TOOL_NAMES) assert.ok(!names.has(t), `${t} advertised to ${key}`);
      await withKey(key, async (c) => {
        await assert.rejects(() =>
          c.callTool({ name: "register_skill_source", arguments: { url: "https://github.com/acme/skills" } }),
        );
      });
      assert.equal(applyPosts.length, 0, "the backend route was reached by a refused key");
    });
  }

  it("a user key's register_skill_source reaches the apply route with its body", async () => {
    const res: any = await withKey("trinity_mcp_user", (c) =>
      c.callTool({ name: "register_skill_source", arguments: { url: "https://github.com/acme/skills", ref: "v1" } }),
    );
    assert.deepEqual(applyPosts, [{ url: "https://github.com/acme/skills", ref: "v1" }]);
    assert.deepEqual(JSON.parse(res.content[0].text), { action: "created", source: { id: "src_1" } });
  });
});
