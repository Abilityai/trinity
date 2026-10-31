/**
 * trinity-enterprise#991 — read an agent's permission edges and self-change
 * grants over MCP.
 *
 * Pins the tool-layer contract:
 *   - `get_agent_permissions` reads `GET /api/agents/{name}/permissions?strict=true`
 *     and returns the edges only (`available_agents` is the CALLER's roster, not
 *     data about the agent — ruling T4), labelled with what the read can see, so
 *     an edge outside the caller's view never reads as a removed edge (T3);
 *   - `get_agent_capability_grants` reads `GET /api/agents/{name}/capability-grants`
 *     and returns the payload verbatim;
 *   - an error is an error, never an empty answer: a 503 from the strict edge read
 *     and a refused grants read both throw. A refused peer reads as REFUSED — the
 *     grants route admits an agent key's own agent and the agents it spawned, never
 *     an edge peer (T1 = A), and the message says so;
 *   - the access rows claim exactly what the routes admit, and the edge read is
 *     gated where `server.ts` registers it (real transport, an agent-scoped key).
 *
 * Unit cases drive the real tool execute() with a fake TrinityClient
 * (requireApiKey=false → getClient() returns the fake directly, the seam of
 * assignments.test.ts). The wiring cases follow access-wiring.test.ts.
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { after, before, describe, it } from "node:test";
import { strict as assert } from "node:assert";
import { createServer as createHttpServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

import { createPermissionTools } from "./permissions.js";
import { ApiError } from "../client.js";
import type { TrinityClient } from "../client.js";
import { TOOL_ACCESS_POLICY } from "../access.js";
import { createServer } from "../server.js";

type Recorded = { method: string; args: unknown[] };

function apiError(status: number, detail: unknown): ApiError {
  return new ApiError(status, JSON.stringify({ detail }));
}

const PERMISSIONS = {
  source_agent: "alpha",
  permitted_agents: [{ name: "bravo", type: "agent", status: "running", permitted: true }],
  available_agents: [{ name: "charlie", type: "agent", status: "stopped", permitted: false }],
};

const GRANTS = {
  agent_name: "alpha",
  grants: [
    { capability: "agents.manage", granted: false, granted_by: null, granted_at: null },
    { capability: "skills.manage", granted: true, granted_by: "admin", granted_at: "2026-10-01T00:00:00Z" },
  ],
};

function makeTools(calls: Recorded[], overrides: Partial<TrinityClient> = {}) {
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    getAgentPermissionsStrict: async (name: string) => {
      calls.push({ method: "getAgentPermissionsStrict", args: [name] });
      return structuredClone(PERMISSIONS);
    },
    getAgentCapabilityGrants: async (name: string) => {
      calls.push({ method: "getAgentCapabilityGrants", args: [name] });
      return structuredClone(GRANTS);
    },
    ...overrides,
  };
  return createPermissionTools(fake as TrinityClient, false);
}

describe("ent#991 get_agent_permissions", () => {
  it("returns the agent's edges, labelled with what the read can see, without the caller's roster", async () => {
    const calls: Recorded[] = [];
    const out = JSON.parse(
      await makeTools(calls).getAgentPermissions.execute({ agent_name: "alpha" }, {}),
    );
    assert.deepEqual(calls, [{ method: "getAgentPermissionsStrict", args: ["alpha"] }]);
    assert.equal(out.source_agent, "alpha");
    assert.deepEqual(out.permitted_agents, PERMISSIONS.permitted_agents);
    assert.equal(out.available_agents, undefined, "the caller's roster is not data about the agent (T4)");
    assert.equal(out.observed_within, "caller_reach_with_container");
  });

  it("a Docker fault (503) is an error, never an empty edge list", async () => {
    const tools = makeTools([], {
      getAgentPermissionsStrict: async () => {
        throw apiError(503, "Container state unavailable");
      },
    });
    await assert.rejects(
      () => tools.getAgentPermissions.execute({ agent_name: "alpha" }, {}),
      (err: unknown) => err instanceof ApiError && err.status === 503,
    );
  });
});

describe("ent#991 get_agent_capability_grants", () => {
  it("returns the backend payload verbatim", async () => {
    const calls: Recorded[] = [];
    const out = JSON.parse(
      await makeTools(calls).getAgentCapabilityGrants.execute({ agent_name: "alpha" }, {}),
    );
    assert.deepEqual(calls, [{ method: "getAgentCapabilityGrants", args: ["alpha"] }]);
    assert.deepEqual(out, GRANTS);
  });

  it("a refused read (uniform 404) reads as refused, never as 'no grants'", async () => {
    const tools = makeTools([], {
      getAgentCapabilityGrants: async () => {
        throw apiError(404, "Agent not found");
      },
    });
    await assert.rejects(
      () => tools.getAgentCapabilityGrants.execute({ agent_name: "bravo" }, {}),
      (err: unknown) => {
        const msg = String((err as Error).message);
        assert.match(msg, /refused/i);
        assert.match(msg, /never through a permission edge/);
        assert.match(msg, /not .*no grants/i);
        return true;
      },
    );
  });

  it("any other failure passes through as itself", async () => {
    const tools = makeTools([], {
      getAgentCapabilityGrants: async () => {
        throw apiError(500, "boom");
      },
    });
    await assert.rejects(
      () => tools.getAgentCapabilityGrants.execute({ agent_name: "alpha" }, {}),
      (err: unknown) => err instanceof ApiError && err.status === 500,
    );
  });

  it("the description claims exactly what the route admits", () => {
    const d = makeTools([]).getAgentCapabilityGrants.description;
    assert.match(d, /itself or an agent it spawned/);
    assert.match(d, /never through a permission edge/);
  });
});

describe("ent#991 access rows", () => {
  it("the edge read is gated on its agent parameter like every fleet read", () => {
    assert.deepEqual(TOOL_ACCESS_POLICY.get_agent_permissions, { kind: "enforce", param: "agent_name" });
  });

  it("the grants read is not edge-gated — the MCP layer would admit an edge peer the route refuses", () => {
    const row = TOOL_ACCESS_POLICY.get_agent_capability_grants;
    assert.equal(row.kind, "baselined");
    assert.match((row as { owner: string }).owner, /OwnedAgentByName/);
    assert.match((row as { owner: string }).owner, /never through a permission edge/);
  });
});

// ---------------------------------------------------------------------------
// Wiring: the tools as an agent reaches them, through server.ts
// ---------------------------------------------------------------------------

const CALLER = "alpha";
const SIBLING = "bravo";

describe("ent#991 the tools are registered and gated in server.ts (real transport)", () => {
  let backend: Server;
  let mcpServer: { stop: () => Promise<void> };
  let mcpUrl: URL;
  let permitted: string[] = [];
  const backendHits: string[] = [];

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
          return send(200, {
            valid: true,
            key_id: "key-agent-991",
            user_id: "owner",
            user_email: "owner@example.com",
            key_name: "agent-key",
            scope: "agent",
            agent_name: CALLER,
          });
        }
        const reach = url.match(new RegExp(`^/api/agents/${CALLER}/reach/([^/?]+)$`));
        if (reach) {
          return send(200, { reachable: permitted.includes(decodeURIComponent(reach[1])) });
        }
        const perms = url.match(/^\/api\/agents\/([^/]+)\/permissions\?strict=true$/);
        if (perms) {
          backendHits.push(url);
          return send(200, { ...PERMISSIONS, source_agent: decodeURIComponent(perms[1]) });
        }
        const grants = url.match(/^\/api\/agents\/([^/]+)\/capability-grants$/);
        if (grants) {
          backendHits.push(url);
          // The route's own answer for an agent key naming an edge peer: uniform 404.
          if (decodeURIComponent(grants[1]) !== CALLER) return send(404, { detail: "Agent not found" });
          return send(200, GRANTS);
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

  const call = async (tool: string, args: Record<string, unknown>) => {
    const client = new Client({ name: "ent991", version: "1.0.0" });
    await client.connect(
      new StreamableHTTPClientTransport(mcpUrl, {
        requestInit: { headers: { Authorization: "Bearer trinity_mcp_test_agent_key_991" } },
      }),
    );
    try {
      const r: any = await client.callTool({ name: tool, arguments: args });
      return { isError: Boolean(r.isError), text: r.content.map((c: any) => c.text).join("\n") };
    } finally {
      await client.close();
    }
  };

  it("an agent key with no edge is refused before the edge read reaches the backend", async () => {
    permitted = [];
    backendHits.length = 0;
    const { text } = await call("get_agent_permissions", { agent_name: SIBLING });
    assert.match(text, new RegExp(`Agent '${CALLER}' is not permitted to communicate with '${SIBLING}'`));
    assert.deepEqual(backendHits, [], "the gate let the read through");
  });

  it("with an edge, the strict read runs against the sibling", async () => {
    permitted = [SIBLING];
    backendHits.length = 0;
    const { isError, text } = await call("get_agent_permissions", { agent_name: SIBLING });
    assert.equal(isError, false, text);
    assert.equal(JSON.parse(text).source_agent, SIBLING);
    assert.deepEqual(backendHits, [`/api/agents/${SIBLING}/permissions?strict=true`]);
  });

  it("its own grants read through; an edge peer's grants read as refused", async () => {
    permitted = [SIBLING];
    const own = await call("get_agent_capability_grants", { agent_name: CALLER });
    assert.equal(own.isError, false, own.text);
    assert.deepEqual(JSON.parse(own.text), GRANTS);
    const peer = await call("get_agent_capability_grants", { agent_name: SIBLING });
    assert.equal(peer.isError, true, peer.text);
    assert.match(peer.text, /refused/i);
  });
});
