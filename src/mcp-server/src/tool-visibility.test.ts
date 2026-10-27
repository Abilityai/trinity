/**
 * #848 — tool-visibility gate must be an ALLOW-list, not a deny-check.
 *
 * Why this file exists: `fastmcp@4.12.2` — the version package-lock actually
 * resolves — makes a "not connector" deny-check fail OPEN in two independent
 * ways. (An earlier revision cited 4.4.0; both behaviours were re-verified on
 * 4.12.x and the conclusion is unchanged, but the chunk filename moved, so these
 * citations lead with the SYMBOL and treat the line as an "as of" locator.)
 *
 *   1. `FastMCP#createSession` skips filtering entirely for a falsy auth:
 *        const allowedTools = auth ? this.#tools.filter(
 *          (tool) => tool.canAccess ? tool.canAccess(auth) : true
 *        ) : this.#tools;
 *      (dist/chunk-5BQXF2VT.js:1998-2000) — a session with no auth context is
 *      handed EVERY registered tool, `canAccess` never runs. The
 *      `authenticated: false` guard just above (`:1994`) does not cover it.
 *   2. The stateful httpStream branch we run (index.ts: no `stateless: true`)
 *      does NOT reject an `authenticate()` returning undefined (`:1924-1926`);
 *      only the stateless branch has that guard (`:1874-1878`).
 *
 * Trinity is not exposed today only because our callback THROWS rather than
 * returning undefined. Inline email auth (#848) needs a pre-login session, so
 * the predicate must deny every scope it does not explicitly know.
 *
 * These tests import the REAL predicates from `server.ts` rather than booting a
 * FastMCP server — the property under test is the predicate itself, and
 * importing the module only *defines* `createServer` (no side effects, no live
 * backend needed).
 *
 * They deliberately do NOT re-declare the predicates. An earlier revision of
 * this file carried a hand-copied mirror whose comment claimed it was "kept in
 * sync" — it was not, and a scope added to `server.ts` would have left these
 * tests green. That is the exact trap in docs/memory/learnings.md (2026-07-16):
 * a mirrored constant has no owner, and a test pinning its own copy pins the
 * drift as the requirement.
 *
 * The final block is different in kind: it boots a REAL FastMCP server and
 * connects a REAL MCP client to pin that `canAccess` is enforced when a tool is
 * CALLED, not merely omitted from `tools/list`. The whole allow-list defence
 * assumes that; nothing else in the suite would notice fastmcp regressing to
 * advertisement-only filtering. It is deliberately behavioural rather than a
 * grep over `node_modules`, so it keeps holding across minified-chunk renames —
 * the churn that made the version citations above wrong twice.
 *
 * Runner: built-in node:test → `node --import tsx --test src/*.test.ts`.
 */
import { after, before, describe, it } from "node:test";
import { strict as assert } from "node:assert";
import { createServer } from "node:net";
import { createServer as createHttpServer } from "node:http";
import type { AddressInfo } from "node:net";

import { FastMCP } from "fastmcp";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

import { HUMAN_ONLY_KEYS_NOT_SHOWN, HUMAN_ONLY_POLICY_OWNER, HUMAN_ONLY_WHO_MAY_CALL, TOOL_ACCESS_POLICY } from "./access.js";
import {
  createServer as createTrinityServer,
  OPERATOR_SCOPES,
  makeOperatorOnly,
  connectorOnly,
  anonymousOnly,
  connectorOrAnonymous,
} from "./server.js";

/** The old, vulnerable predicate — kept to prove the fix actually changes behaviour. */
const legacyConnectorDenied = (auth: any): boolean => auth?.scope !== "connector";

/** Ask the OS for a free port so parallel test files cannot collide. */
async function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = createServer();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const addr = srv.address();
      if (typeof addr === "string" || addr === null) {
        srv.close(() => reject(new Error("no port")));
        return;
      }
      const { port } = addr;
      srv.close(() => resolve(port));
    });
  });
}

describe("#848 operator tool-visibility gate", () => {
  const operatorOnly = makeOperatorOnly(true);

  it("admits the three credentialed operator scopes", () => {
    for (const scope of ["user", "agent", "system"]) {
      assert.equal(operatorOnly({ scope }), true, `${scope} should be an operator`);
    }
  });

  it("denies connector-scoped keys (ent#46 isolation preserved)", () => {
    assert.equal(operatorOnly({ scope: "connector" }), false);
    assert.equal(connectorOnly({ scope: "connector" }), true);
  });

  it("denies a pre-login anonymous session — the #848 regression", () => {
    const anon = { scope: "anonymous", userId: "anonymous", keyName: "anonymous" };
    assert.equal(operatorOnly(anon), false, "anonymous must never see operator tools");
    assert.equal(connectorOnly(anon), false, "anonymous is not a connector either");
    // Prove the old predicate was the bug, not incidental.
    assert.equal(
      legacyConnectorDenied(anon),
      true,
      "legacy deny-check admitted anonymous — this is what #848 fixes"
    );
  });

  it("denies an unknown/future scope (fails CLOSED)", () => {
    for (const scope of ["", "guest", "readonly", "admin", "AGENT", "User"]) {
      assert.equal(
        operatorOnly({ scope }),
        false,
        `unknown scope '${scope}' must not be treated as an operator`
      );
    }
  });

  it("denies a context with no scope at all", () => {
    assert.equal(operatorOnly({}), false);
    assert.equal(operatorOnly({ userId: "x", keyName: "y" }), false);
  });

  it("denies absent auth when API-key auth is required", () => {
    assert.equal(operatorOnly(undefined), false);
    assert.equal(operatorOnly(null), false);
    // The legacy predicate admitted both — the exact fail-open being closed.
    assert.equal(legacyConnectorDenied(undefined), true);
    assert.equal(legacyConnectorDenied(null), true);
  });

  it("still admits absent auth in dev mode (MCP_REQUIRE_API_KEY=false)", () => {
    // Dev installs no authenticate callback, so FastMCP never calls canAccess
    // and advertises everything regardless; this only pins the predicate's own
    // behaviour so a direct caller sees the documented dev semantics.
    const devOperatorOnly = makeOperatorOnly(false);
    assert.equal(devOperatorOnly(undefined), true);
    assert.equal(devOperatorOnly(null), true);
    // A real connector key is still denied operator tools even in dev.
    assert.equal(devOperatorOnly({ scope: "connector" }), false);
  });

  it("pins the operator scope set — widening it must be deliberate", () => {
    // Reads the REAL set imported from server.ts, so adding a scope there fails
    // here until someone updates this line on purpose.
    assert.deepEqual([...OPERATOR_SCOPES].sort(), ["agent", "system", "user"]);
  });

  it("the anonymous and connector-or-anonymous gates behave as registered", () => {
    // These are the predicates actually passed to addAllTools for the #848
    // login tools and the connector group; exercised here against the real
    // implementations rather than a copy.
    assert.equal(anonymousOnly({ scope: "anonymous" }), true);
    assert.equal(anonymousOnly({ scope: "connector" }), false);
    assert.equal(anonymousOnly({ scope: "user" }), false);
    assert.equal(anonymousOnly(undefined), false);

    assert.equal(connectorOrAnonymous({ scope: "connector" }), true);
    assert.equal(connectorOrAnonymous({ scope: "anonymous" }), true);
    assert.equal(connectorOrAnonymous({ scope: "user" }), false);
    assert.equal(connectorOrAnonymous({ scope: "system" }), false);
    assert.equal(connectorOrAnonymous(undefined), false);
  });

  it("operator and connector gates are mutually exclusive for every scope", () => {
    for (const scope of ["user", "agent", "system", "connector", "anonymous", "nonsense"]) {
      assert.equal(
        operatorOnly({ scope }) && connectorOnly({ scope }),
        false,
        `scope '${scope}' must not satisfy both gates`
      );
    }
  });
});

/**
 * The load-bearing library assumption, tested against the library.
 *
 * `canAccess` is only a security control if a filtered-out tool cannot be
 * INVOKED. If fastmcp ever filtered `tools/list` but still dispatched
 * `tools/call`, every predicate above would keep passing while an anonymous
 * session could drive `delete_agent` by name — the gate would be a UI hint.
 *
 * As of fastmcp@4.12.2 the mechanism is not a per-call `canAccess` re-check (it
 * is never re-invoked): `setupToolHandlers(tools)` closes over a `toolsMap` built
 * from the FILTERED list, and the `CallToolRequestSchema` handler throws
 * `MethodNotFound` on a miss. This test does not encode that mechanism — only the
 * observable outcome — so a fastmcp refactor that preserves enforcement stays
 * green and one that drops it goes red.
 */
describe("#848 canAccess is enforced at call time, not just advertisement", () => {
  /**
   * Boot a real FastMCP httpStream server whose `authenticate` asserts `scope`,
   * register one operator-gated and one anonymous-gated tool using the REAL
   * predicates, and drive it with a real MCP client.
   */
  async function withProbeServer(
    scope: string,
    body: (client: Client, ran: { operator: number; anon: number }) => Promise<void>
  ): Promise<void> {
    const ran = { operator: 0, anon: 0 };
    const port = await freePort();

    const server = new FastMCP({
      name: "gate-probe",
      version: "0.0.0",
      // Mirrors Trinity's tiering: always returns a truthy context, never
      // undefined (which would skip filtering entirely — see the header).
      authenticate: async () => ({ scope, userId: "probe", keyName: "probe" }),
    });

    server.addTool({
      name: "operator_tool",
      description: "operator-only probe",
      canAccess: makeOperatorOnly(true) as (auth: any) => boolean,
      execute: async () => {
        ran.operator += 1;
        return "operator ran";
      },
    });
    server.addTool({
      name: "anon_tool",
      description: "anonymous-visible probe",
      canAccess: anonymousOnly as (auth: any) => boolean,
      execute: async () => {
        ran.anon += 1;
        return "anon ran";
      },
    });

    await server.start({
      transportType: "httpStream",
      httpStream: { port, host: "127.0.0.1" },
    });

    const client = new Client({ name: "probe-client", version: "0.0.0" });
    const transport = new StreamableHTTPClientTransport(
      new URL(`http://127.0.0.1:${port}/mcp`)
    );

    try {
      await client.connect(transport);
      await body(client, ran);
    } finally {
      await client.close().catch(() => {});
      await server.stop().catch(() => {});
    }
  }

  it("an anonymous session cannot LIST or CALL an operator tool", async () => {
    await withProbeServer("anonymous", async (client, ran) => {
      const listed = (await client.listTools()).tools.map((t) => t.name).sort();
      assert.deepEqual(
        listed,
        ["anon_tool"],
        "operator_tool must not be advertised to an anonymous session"
      );

      // The property that actually matters: naming it directly must fail.
      await assert.rejects(
        () => client.callTool({ name: "operator_tool", arguments: {} }),
        (err: unknown) => {
          const msg = String((err as Error)?.message ?? err);
          assert.match(
            msg,
            /not found|unknown tool|method not found/i,
            `expected a not-found rejection, got: ${msg}`
          );
          return true;
        },
        "calling a filtered-out tool by name must be rejected, not dispatched"
      );

      assert.equal(
        ran.operator,
        0,
        "SECURITY: the operator tool's body ran despite the gate — canAccess is " +
          "advertisement-only in this fastmcp version and the allow-list is not a control"
      );

      // Control: the tool the gate DOES admit works, so the assertion above is
      // about the gate and not about a broken probe server.
      await client.callTool({ name: "anon_tool", arguments: {} });
      assert.equal(ran.anon, 1, "anonymous tool should have executed");
    });
  });

  it("an operator session can call the operator tool (gate is not blanket-deny)", async () => {
    await withProbeServer("user", async (client, ran) => {
      const listed = (await client.listTools()).tools.map((t) => t.name).sort();
      assert.deepEqual(listed, ["operator_tool"], "operator sees only its own tier here");

      await client.callTool({ name: "operator_tool", arguments: {} });
      assert.equal(ran.operator, 1, "operator tool should have executed");

      // And the anonymous-only tool is symmetrically unreachable.
      await assert.rejects(() => client.callTool({ name: "anon_tool", arguments: {} }));
      assert.equal(ran.anon, 0);
    });
  });
});

describe("#736 outbound A2A tools are operator-scope only", () => {
  // These two tools place a CREDENTIALED outbound request to an
  // operator-registered peer. A connector key is consumption-only and bound to
  // one agent; an anonymous session holds no credential at all. Neither may
  // ever see them — and because the allow-list is what decides, that holds for
  // any scope added later without anyone remembering to think about A2A.
  //
  // The tools opt in purely by being an element of `toolGroups` in server.ts.
  // There is no per-tool annotation, so this test pins the PREDICATE against
  // the scopes rather than re-deriving the registration.
  const operatorOnly = makeOperatorOnly(true);

  it("advertises to user, agent and system scopes", () => {
    for (const scope of ["user", "agent", "system"]) {
      assert.equal(operatorOnly({ scope }), true, `${scope} should see call_a2a_agent`);
    }
  });

  it("hides them from connector and anonymous sessions", () => {
    for (const scope of ["connector", "anonymous"]) {
      assert.equal(operatorOnly({ scope }), false, `${scope} must not see call_a2a_agent`);
    }
  });

  it("hides them from a scope nobody has thought of yet", () => {
    // The reason this is an ALLOW-list: a deny-check admits every scope it has
    // not heard of, and `mcp_api_keys.scope` is free text with no CHECK
    // constraint.
    assert.equal(operatorOnly({ scope: "portal_delegate" }), false);
    assert.equal(operatorOnly({ scope: "some_future_tier" }), false);
  });

  it("registers the outbound tools in the operator group, not the connector one", async () => {
    const { createA2ACallTools } = await import("./tools/a2a_call.js");
    const tools = createA2ACallTools({ getBaseUrl: () => "http://x" } as any, false);
    assert.deepEqual(Object.keys(tools).sort(), ["call_a2a_agent", "get_a2a_task"]);
    const serverSrc = await import("node:fs").then((fs) =>
      fs.readFileSync(new URL("./server.ts", import.meta.url), "utf8"),
    );
    // Registration is by array membership; assert it is in toolGroups and NOT
    // in the connector/auth groups, which are added with different predicates.
    assert.match(serverSrc, /createA2ACallTools\(client, requireApiKey\)/);
    assert.doesNotMatch(serverSrc, /connectorGroup\s*=\s*createA2ACallTools/);
  });

  it("registers the AAuth tools in the operator group — advertised but inert when the flag is off (ent#878 T3, ent#879 T4)", async () => {
    // Taste T3: no env gate and no dynamic registration. The tools are ALWAYS
    // advertised to operator scopes; with `aauth_prototype_enabled` off the
    // backend answers 404 and the tools report `aauth_disabled: true`. The
    // backend 404 is the authority, exactly as for call_a2a_agent. ent#879's
    // call_aauth_resource registers through the SAME mechanism — one, never two.
    const { createAAuthTools } = await import("./tools/aauth.js");
    const tools = createAAuthTools({ getBaseUrl: () => "http://x" } as any, false);
    assert.deepEqual(Object.keys(tools), ["request_person_token", "call_aauth_resource"]);
    for (const name of Object.keys(tools)) {
      for (const scope of ["user", "agent", "system"]) {
        assert.equal(operatorOnly({ scope }), true, `${scope} should see ${name}`);
      }
      for (const scope of ["connector", "anonymous", "portal_delegate"]) {
        assert.equal(operatorOnly({ scope }), false, `${scope} must not see ${name}`);
      }
    }
    const serverSrc = await import("node:fs").then((fs) =>
      fs.readFileSync(new URL("./server.ts", import.meta.url), "utf8"),
    );
    assert.match(serverSrc, /createAAuthTools\(client, requireApiKey\)/);
    assert.doesNotMatch(serverSrc, /connectorGroup\s*=\s*createAAuthTools/);
    assert.doesNotMatch(serverSrc, /AAUTH_PROTOTYPE_ENABLED/);
  });
});

describe("#279 credential-vault tools are operator-scope only", () => {
  // list_available_credentials / fetch_credential deliver a granted secret to
  // the calling key's agent. A connector key is consumption-only and bound to
  // one agent; an anonymous session holds no credential at all. Neither may ever
  // see these tools — and because the allow-list is what decides, that holds for
  // any scope added later. The backend fetch route is agent-scoped-key-only, so
  // even a user/system operator key gets a named 403; the tool is nonetheless
  // advertised to the operator tier (it is license-blind and reports honestly).
  const operatorOnly = makeOperatorOnly(true);

  it("advertises to user, agent and system scopes", () => {
    for (const scope of ["user", "agent", "system"]) {
      assert.equal(
        operatorOnly({ scope }),
        true,
        `${scope} should see the credential-vault tools`,
      );
    }
  });

  it("hides them from connector and anonymous sessions", () => {
    for (const scope of ["connector", "anonymous"]) {
      assert.equal(
        operatorOnly({ scope }),
        false,
        `${scope} must not see the credential-vault tools`,
      );
    }
  });

  it("hides them from a scope nobody has thought of yet (fails CLOSED)", () => {
    assert.equal(operatorOnly({ scope: "portal_delegate" }), false);
    assert.equal(operatorOnly({ scope: "some_future_tier" }), false);
  });

  it("registers the vault tools in the operator group, not the connector one", async () => {
    const { createCredentialVaultTools } = await import(
      "./tools/credential_vault.js"
    );
    const tools = createCredentialVaultTools(
      { getBaseUrl: () => "http://x" } as any,
      false,
    );
    assert.deepEqual(Object.keys(tools).sort(), [
      "fetch_credential",
      "list_available_credentials",
    ]);
    const serverSrc = await import("node:fs").then((fs) =>
      fs.readFileSync(new URL("./server.ts", import.meta.url), "utf8"),
    );
    assert.match(serverSrc, /createCredentialVaultTools\(client, requireApiKey\)/);
    assert.doesNotMatch(
      serverSrc,
      /connectorGroup\s*=\s*createCredentialVaultTools/,
    );
  });
});

describe("ent#500 get_agent_assignments is operator-scope only", () => {
  // The tool names a real person — the human an agent works for. A connector key
  // is an end-user consumption credential bound to one agent, and an anonymous
  // session is pre-login; neither may ever see staff identities. The `agent`
  // scope IS in the allow-list, which is why the BACKEND self-scopes an agent
  // principal to its own roster: advertisement is not authorization, and an
  // agent-scoped key resolves to its owner carrying the owner's role.
  const operatorOnly = makeOperatorOnly(true);

  it("advertises to user, agent and system scopes", () => {
    for (const scope of ["user", "agent", "system"]) {
      assert.equal(
        operatorOnly({ scope }),
        true,
        `${scope} should see get_agent_assignments`,
      );
    }
  });

  it("hides it from connector and anonymous sessions", () => {
    for (const scope of ["connector", "anonymous"]) {
      assert.equal(
        operatorOnly({ scope }),
        false,
        `${scope} must not see get_agent_assignments`,
      );
    }
  });

  it("hides it from a scope nobody has thought of yet (fails CLOSED)", () => {
    assert.equal(operatorOnly({ scope: "portal_delegate" }), false);
    assert.equal(operatorOnly({ scope: "some_future_tier" }), false);
  });

  it("registers the tool in the operator group, not the connector one", async () => {
    const { createAssignmentTools } = await import("./tools/assignments.js");
    const tools = createAssignmentTools(
      { getBaseUrl: () => "http://x" } as any,
      false,
    );
    assert.deepEqual(Object.values(tools).map((t: any) => t.name), [
      "get_agent_assignments",
    ]);
    const serverSrc = await import("node:fs").then((fs) =>
      fs.readFileSync(new URL("./server.ts", import.meta.url), "utf8"),
    );
    assert.match(serverSrc, /createAssignmentTools\(client, requireApiKey\)/);
    assert.doesNotMatch(serverSrc, /connectorGroup\s*=\s*createAssignmentTools/);
  });

  it("exposes no write tool — an assignment is a grant, and the backend " +
     "refuses every non-interactive principal on the write path", async () => {
    const { createAssignmentTools } = await import("./tools/assignments.js");
    const tools = createAssignmentTools(
      { getBaseUrl: () => "http://x" } as any,
      false,
    );
    for (const tool of Object.values(tools) as any[]) {
      assert.doesNotMatch(
        tool.name,
        /^(create|set|update|delete|assign|remove)_/,
        `${tool.name} looks like a write tool; assignments are read-only over MCP`,
      );
    }
  });
});

/**
 * A real `createServer` in key mode behind a fake backend, driven over the real
 * streamable-HTTP transport. `/api/mcp/validate` answers with the row of the
 * key named in the bearer (`trinity_mcp_<key>`), so every scope is a real
 * session, the platform's `system` key included. Every other request is logged
 * (`requests`), so a test can prove a refused call never reached the backend,
 * and answered by `route` (default: 200 `{}`).
 */
type KeyRow = { scope: string; agent_name?: string };

interface KeyedServer {
  url: URL;
  requests: string[];
  stop: () => Promise<void>;
}

async function bootKeyedServer(
  keys: Record<string, KeyRow>,
  route: (method: string, path: string) => { status: number; body: unknown } = () => ({ status: 200, body: {} }),
): Promise<KeyedServer> {
  const requests: string[] = [];
  const backend = createHttpServer((req, res) => {
    req.resume();
    req.on("end", () => {
      const path = String(req.url ?? "");
      if (path === "/api/mcp/validate") {
        const bearer = String(req.headers.authorization ?? "").replace(/^Bearer trinity_mcp_/, "");
        const row = keys[bearer];
        res.writeHead(200, { "Content-Type": "application/json" });
        return res.end(JSON.stringify(row
          ? { valid: true, key_id: `key-${bearer}`, user_id: "owner", user_email: "owner@example.com", key_name: bearer, ...row }
          : { valid: false }));
      }
      requests.push(`${req.method} ${path}`);
      const { status, body } = route(String(req.method), path);
      res.writeHead(status, { "Content-Type": "application/json" });
      return res.end(JSON.stringify(body));
    });
  });
  await new Promise<void>((r) => backend.listen(0, "127.0.0.1", () => r()));
  const backendPort = (backend.address() as AddressInfo).port;
  const mcpPort = await freePort();
  const { server } = await createTrinityServer({
    trinityApiUrl: `http://127.0.0.1:${backendPort}`,
    requireApiKey: true,
    port: mcpPort,
  });
  await server.start({ transportType: "httpStream", httpStream: { port: mcpPort, host: "127.0.0.1" } });
  return {
    url: new URL(`http://127.0.0.1:${mcpPort}/mcp`),
    requests,
    stop: async () => {
      await server.stop().catch(() => {});
      await new Promise<void>((r) => backend.close(() => r()));
    },
  };
}

async function asKey<T>(url: URL, key: string, body: (client: Client) => Promise<T>): Promise<T> {
  const client = new Client({ name: `vis-${key}`, version: "1.0.0" });
  await client.connect(new StreamableHTTPClientTransport(url, {
    requestInit: { headers: { Authorization: `Bearer trinity_mcp_${key}` } },
  }));
  try {
    return await body(client);
  } finally {
    await client.close().catch(() => {});
  }
}

const listedTo = (url: URL, key: string): Promise<string[]> =>
  asKey(url, key, async (c) => (await c.listTools()).tools.map((t) => t.name));

const NOT_FOUND = /not found|unknown tool|method not found/i;

describe("#3435 human-only credential tools are not advertised to agent sessions", () => {
  // export_credentials (a person's call) and get_credential_encryption_key (an
  // admin's, human-only) can never be called with an agent-scoped key: the
  // backend refuses every one. Each carries its own canAccess allow-list. This
  // boots the REAL server in key mode and drives it over the real transport
  // with a user key and an agent key — listing AND calling, because
  // advertisement alone is not a control. import_credentials is shown to no
  // key at all since ent#719; the block below pins that.
  const CREDENTIAL_TOOLS = ["export_credentials", "get_credential_encryption_key",
    // ent#966 (M6): revoke_agent_delegation sits behind `require_person` — user keys only
    // (the system key is not shown it either since ent#719; the block below pins that).
    "revoke_agent_delegation"];

  /**
   * The other tools whose backend gate refuses every agent key (the `ADMIN_ONLY`
   * owner in access.ts) but which are still advertised to agent sessions.
   * SHRINK-ONLY: an entry that becomes hidden fails until it is removed here,
   * and a new admin-only tool that is advertised to agents fails until it gets
   * a canAccess. Never add an entry to make a new tool pass. Follow-up: convert
   * the rest (#3435 ruling TD-3).
   */
  const ADVERTISED_TO_AGENTS_BASELINE = [
    "get_agent_ssh_access",
    "get_nevermined_payments",
    "register_a2a_endpoint",
    "remove_a2a_endpoint",
    "set_a2a_inbound_allowlist",
    "set_agent_a2a_exposure",
  ];
  const ADMIN_ONLY_OWNER = /^backend rejects agent principals/;

  it("a user key lists them; an agent key neither lists nor can call them", async () => {
    const srv = await bootKeyedServer({
      user_key: { scope: "user" },
      agent_key: { scope: "agent", agent_name: "alpha" },
    });
    try {
      const userTools = await listedTo(srv.url, "user_key");
      const agentTools = await listedTo(srv.url, "agent_key");
      assert.ok(agentTools.length > 50, `only ${agentTools.length} tools for the agent session — the census is not seeing the server`);
      for (const name of CREDENTIAL_TOOLS) {
        assert.ok(userTools.includes(name), `${name} must be listed for a user key`);
        assert.ok(!agentTools.includes(name), `${name} must NOT be listed for an agent key`);
      }

      await asKey(srv.url, "agent_key", async (c) => {
        for (const name of CREDENTIAL_TOOLS) {
          await assert.rejects(
            () => c.callTool({ name, arguments: name === "get_credential_encryption_key" ? {} : { name: "alpha" } }),
            (err: unknown) => NOT_FOUND.test(String((err as Error)?.message ?? err)),
            `${name}: an agent key calling it by name must be rejected as not found`,
          );
        }
      });

      // Shrink-only guard over every admin-only row: hidden from agents, or baselined.
      const guarded = Object.entries(TOOL_ACCESS_POLICY)
        .filter(([, p]) => p.kind === "baselined" && ADMIN_ONLY_OWNER.test(p.owner))
        .map(([name]) => name)
        .filter((name) => userTools.includes(name));
      const advertised = guarded.filter((name) => agentTools.includes(name)).sort();
      assert.deepEqual(
        advertised.filter((n) => !ADVERTISED_TO_AGENTS_BASELINE.includes(n)),
        [],
        "an admin-only tool is advertised to agent sessions — give it a canAccess allow-list (the #3435 shape)",
      );
      const stale = ADVERTISED_TO_AGENTS_BASELINE.filter((n) => !agentTools.includes(n));
      assert.deepEqual(stale, [], "baseline entries no longer advertised to agents — remove them (shrink-only)");
    } finally {
      await srv.stop();
    }
  });
});

describe("ent#719 human-only tools are listed exactly to the keys their backend rule admits", () => {
  /**
   * Each tool, and the key scopes that may be shown it. Written out here, not
   * derived from the code under test: `person` routes admit a signed-in session
   * or the person's own user-scoped key, `interactive` routes (credential binds)
   * a signed-in session only, and the encryption key stays an admin's read that
   * the platform's system key may also make. `set_agent_github_pat` is listed
   * to user keys because clearing is a person's call; setting is refused there.
   * Three more tools front a person-only route outside the agent's own
   * settings: closing a room (its moderator acts), revoking a Nevermined
   * mandate (`require_person`) and answering an operator-queue item
   * (`reject_non_person_principal`).
   */
  const EXPECTED: Record<string, string[]> = {
    rename_agent: ["user"],
    initialize_github_sync: ["user"],
    set_agent_github_pat: ["user"],
    export_credentials: ["user"],
    clear_agent_subscription: ["user"],
    toggle_nevermined: ["user"],
    inject_credentials: [],
    import_credentials: [],
    assign_subscription: [],
    configure_nevermined: [],
    get_credential_encryption_key: ["user", "system"],
    close_room: ["user"],
    revoke_agent_delegation: ["user"],
    respond_to_operator_queue: ["user"],
  };
  const KEYS: Record<string, KeyRow> = {
    user: { scope: "user" },
    agent: { scope: "agent", agent_name: "alpha" },
    system: { scope: "system", agent_name: "trinity-system" },
    connector: { scope: "connector", agent_name: "alpha" },
    portal_delegate: { scope: "portal_delegate" },
    ops: { scope: "ops" },
    some_future_tier: { scope: "some_future_tier" },
  };
  /** Arguments that satisfy each tool's schema, so a call is decided by visibility alone. */
  const ARGS: Record<string, Record<string, unknown>> = {
    rename_agent: { name: "ghost", new_name: "ghost-2" },
    initialize_github_sync: { agent_name: "ghost", repo_owner: "example", repo_name: "ghost" },
    set_agent_github_pat: { agent_name: "ghost", pat: "" },
    export_credentials: { name: "ghost" },
    clear_agent_subscription: { agent_name: "ghost" },
    toggle_nevermined: { agent_name: "ghost", enabled: false },
    inject_credentials: { name: "ghost", files: { ".env": "EXAMPLE=1\n" } },
    import_credentials: { name: "ghost" },
    assign_subscription: { agent_name: "ghost", subscription_name: "example" },
    configure_nevermined: {
      agent_name: "ghost", nvm_api_key: "sandbox:example", nvm_environment: "sandbox",
      nvm_agent_id: "example", nvm_plan_id: "example", credits_per_request: 1,
    },
    get_credential_encryption_key: {},
    close_room: { room_id: "r-ghost" },
    revoke_agent_delegation: { agent_name: "ghost" },
    respond_to_operator_queue: { item_id: "q-ghost", response: "approve" },
  };

  // A fresh install: no agents, rooms or queue items. The agent list is empty,
  // and every route answers the backend's own 404 for a thing that does not
  // exist: the rooms service's coded `room_not_found`, the queue's string, and
  // the agent 404 everywhere else.
  const freshInstall = (method: string, path: string) => {
    if (method === "GET" && path === "/api/agents") return { status: 200, body: [] };
    if (path === "/api/credentials/encryption-key") return { status: 200, body: { status: "ok" } };
    if (path.startsWith("/api/rooms/")) {
      return { status: 404, body: { detail: { code: "room_not_found", message: "Room not found" } } };
    }
    if (path.startsWith("/api/operator-queue/")) return { status: 404, body: { detail: "Queue item not found" } };
    return { status: 404, body: { detail: "Agent not found" } };
  };

  let srv: KeyedServer;
  const listing: Record<string, string[]> = {};

  before(async () => {
    srv = await bootKeyedServer(KEYS, freshInstall);
    for (const key of Object.keys(KEYS)) listing[key] = await listedTo(srv.url, key);
  });
  after(async () => {
    await srv?.stop();
  });

  it("each tool is listed to exactly the scopes its rule admits, for every key scope", () => {
    assert.ok(listing.user.length > 100, `only ${listing.user.length} tools for a user key — the census is not seeing the server`);
    assert.ok(listing.system.length > 50, `only ${listing.system.length} tools for the system key — the census is not seeing the server`);
    for (const [tool, scopes] of Object.entries(EXPECTED)) {
      const shown = Object.keys(KEYS).filter((key) => listing[key].includes(tool)).sort();
      assert.deepEqual(shown, [...scopes].sort(), `${tool} is listed to [${shown}], expected [${scopes}]`);
    }
  });

  it("the system key keeps get_credential_encryption_key and reaches the backend with it", async () => {
    srv.requests.length = 0;
    const out = await asKey(srv.url, "system", (c) => c.callTool({ name: "get_credential_encryption_key", arguments: {} }));
    assert.notEqual((out as { isError?: boolean }).isError, true, JSON.stringify(out));
    assert.deepEqual(srv.requests, ["GET /api/credentials/encryption-key"]);
  });

  it("a key calling a tool it is not shown gets 'not found', and the backend is never asked", async () => {
    let refused = 0;
    for (const key of ["user", "agent", "system"]) {
      const hidden = Object.keys(EXPECTED).filter((tool) => !EXPECTED[tool].includes(KEYS[key].scope));
      srv.requests.length = 0;
      await asKey(srv.url, key, async (c) => {
        for (const tool of hidden) {
          await assert.rejects(
            () => c.callTool({ name: tool, arguments: ARGS[tool] }),
            (err: unknown) => NOT_FOUND.test(String((err as Error)?.message ?? err)),
            `${tool}: a ${key} key calling it by name must be rejected as not found`,
          );
          refused += 1;
        }
      });
      assert.deepEqual(srv.requests, [], `a ${key} key's refused calls reached the backend: ${srv.requests.join(", ")}`);
    }
    // 4 interactive tools for the user key; 14 for the agent key; 13 for the system key.
    assert.equal(refused, 4 + 14 + 13);
  });

  it("every policy row that names a human-only rule is listed as that rule says", () => {
    const byRule = { person: ["user"], interactive: [] as string[] };
    let rows = 0;
    for (const [tool, policy] of Object.entries(TOOL_ACCESS_POLICY)) {
      if (policy.kind !== "baselined") continue;
      for (const [rule, scopes] of Object.entries(byRule)) {
        if (policy.owner !== HUMAN_ONLY_POLICY_OWNER[rule as keyof typeof HUMAN_ONLY_POLICY_OWNER]) continue;
        rows += 1;
        const shown = Object.keys(KEYS).filter((key) => listing[key].includes(tool));
        assert.deepEqual(shown, scopes, `${tool}: its row says ${rule}, but it is listed to [${shown}]`);
      }
    }
    // Every tool above but set_agent_github_pat (two rules on one tool), the key
    // read, and the three whose rows keep their own kind (close_room `none`,
    // respond_to_operator_queue `in-tool`, revoke_agent_delegation `enforce`);
    // the first test above pins those three.
    assert.equal(rows, 9, "the rows naming a human-only rule are not the nine this guard expects");
  });

  it("each description says who may call it and fits the 2,048-character cap, hidden tools included", async () => {
    const { createAgentTools } = await import("./tools/agents.js");
    const { createSubscriptionTools } = await import("./tools/subscriptions.js");
    const { createNeverminedTools } = await import("./tools/nevermined.js");
    const { createRoomTools } = await import("./tools/rooms.js");
    const { createOperatorQueueTools } = await import("./tools/operator_queue.js");
    const base = { getBaseUrl: () => "http://x" } as any;
    const all = [
      createAgentTools(base, true), createSubscriptionTools(base, true), createNeverminedTools(base, true),
      createRoomTools(base, true), createOperatorQueueTools(base, true),
    ].flatMap((group) => Object.values(group) as Array<{ name: string; description: string }>);
    // A room's moderator and a queue item's reader are not "the agent's owner",
    // so those two carry their own caller sentence, and the shared keys sentence.
    const OWN_CALLER: Record<string, RegExp> = {
      close_room: /WHO MAY CALL: the room's moderator or an admin, as a person/,
      respond_to_operator_queue: /WHO MAY CALL: a person with access to the item's agent/,
    };
    for (const [tool, scopes] of Object.entries(EXPECTED)) {
      const found = all.find((t) => t.name === tool);
      assert.ok(found, `${tool} is not registered`);
      assert.ok(found.description.length <= 2048, `${tool}: ${found.description.length} characters`);
      if (tool === "get_credential_encryption_key") continue;
      const rule = scopes.length === 0 ? "interactive" : "person";
      assert.ok(found.description.includes(HUMAN_ONLY_KEYS_NOT_SHOWN[rule]), `${tool}: the description does not say which keys are not shown it`);
      if (OWN_CALLER[tool]) {
        assert.match(found.description, OWN_CALLER[tool], `${tool}: the description does not name its own caller`);
      } else {
        assert.ok(found.description.includes(HUMAN_ONLY_WHO_MAY_CALL[rule]), `${tool}: the description does not carry the ${rule} sentence`);
      }
    }
    const pat = all.find((t) => t.name === "set_agent_github_pat")!;
    assert.match(pat.description, /setting[^.]*signed-in session/i, "set_agent_github_pat must say setting needs a signed-in session");
  });

  it("fresh install: a user key on an instance with no agents lists its tools and gets a clean 404", async () => {
    const shown = Object.keys(EXPECTED).filter((tool) => EXPECTED[tool].includes("user") && tool !== "get_credential_encryption_key");
    assert.equal(shown.length, 9);
    // Three tools map the backend's 404 to their own answer instead of a tool
    // error; each answer still comes from the backend, never from the tool list.
    const OWN_404: Record<string, RegExp> = {
      close_room: /room_request_failed[\s\S]*\b404\b[\s\S]*room_not_found/,
      revoke_agent_delegation: /agent_not_found_or_no_access/,
      respond_to_operator_queue: /\b404\b[\s\S]*Queue item not found/,
    };
    srv.requests.length = 0;
    await asKey(srv.url, "user", async (c) => {
      const agents = await c.callTool({ name: "list_agents", arguments: {} });
      const text = (agents as { content: Array<{ text?: string }> }).content[0]?.text ?? "";
      assert.deepEqual(JSON.parse(text), [], "an instance with no agents lists none");
      for (const tool of shown) {
        const out = (await c.callTool({ name: tool, arguments: ARGS[tool] })) as {
          isError?: boolean; content: Array<{ text?: string }>;
        };
        const body = out.content.map((p) => p.text ?? "").join(" ");
        if (OWN_404[tool]) {
          assert.notEqual(out.isError, true, `${tool}: expected its own not-found answer, got ${body}`);
          assert.match(body, OWN_404[tool], `${tool}: expected its own answer to the backend's 404, got ${body}`);
          continue;
        }
        assert.equal(out.isError, true, `${tool}: expected the backend's 404 as a tool error, got ${body}`);
        assert.match(body, /\b404\b/, `${tool}: expected the backend's 404, got ${body}`);
      }
    });
    // Every call was decided by the backend, not by the tool list.
    assert.equal(srv.requests.length, 1 + shown.length, srv.requests.join(", "));
  });
});
