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
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";
import { createServer } from "node:net";

import { FastMCP } from "fastmcp";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

import {
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
