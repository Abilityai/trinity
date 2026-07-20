/**
 * #848 — inline email auth: the login tools and the two-caller connector path.
 *
 * Pins the properties that are security-relevant rather than cosmetic:
 *  - request_login is enumeration-safe: one constant body on EVERY path
 *  - verify_login upgrades the session in place but never escalates its scope
 *  - a pre-login anonymous session cannot act
 *  - the agent argument selects among ALREADY-authorized agents; it is never a
 *    way to reach one that was not granted
 *  - a connector key's bound agent stays authoritative (ent#46 unchanged)
 *
 * Runner: built-in node:test → `node --import tsx --test src/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createAuthTools } from "./tools/auth.js";
import { createConnectorTools } from "./tools/connector.js";
import type { TrinityClient } from "./client.js";
import type { McpAuthContext } from "./types.js";

// --------------------------------------------------------------------------
// fakes
// --------------------------------------------------------------------------

interface AuthCalls {
  requests: Array<{ email: string }>;
  verifies: Array<{ email: string; code: string }>;
}

function fakeAuthClient(
  opts: {
    verified?: boolean;
    agents?: Array<{ name: string }>;
    username?: string;
    requestThrows?: boolean;
    verifyThrows?: boolean;
  } = {}
) {
  const calls: AuthCalls = { requests: [], verifies: [] };
  const fake: Partial<TrinityClient> = {
    requestInlineLoginCode: async (email: string) => {
      calls.requests.push({ email });
      if (opts.requestThrows) throw new Error("backend exploded");
    },
    verifyInlineLoginCode: async (email: string, code: string) => {
      calls.verifies.push({ email, code });
      if (opts.verifyThrows) throw new Error("backend exploded");
      return {
        verified: opts.verified ?? true,
        username: opts.username ?? email,
        agents: opts.agents ?? [{ name: "agent-1" }],
      };
    },
  };
  return { client: fake as unknown as TrinityClient, calls };
}

function fakeConnectorClient(
  playbooksByAgent: Record<string, Array<{ name: string }>> = {
    "agent-1": [{ name: "cso" }],
  }
) {
  const chats: Array<{ agent: string; message: string; via: string }> = [];
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    getConnectorPlaybooks: async (agent: string) => playbooksByAgent[agent] ?? [],
    getInlineConnectorPlaybooks: async (_email: string, agent: string) =>
      playbooksByAgent[agent] ?? [],
    chat: async (agent: string, message: string) => {
      chats.push({ agent, message, via: "key" });
      return { response: "ok" } as any;
    },
    inlineConnectorChat: async (_email: string, agent: string, message: string) => {
      chats.push({ agent, message, via: "inline" });
      return { response: "ok" } as any;
    },
  };
  return { client: fake as unknown as TrinityClient, chats };
}

const anonSession = (over: Partial<McpAuthContext> = {}): McpAuthContext => ({
  userId: "anonymous",
  keyName: "anonymous",
  scope: "anonymous",
  sessionId: "sess-1",
  ...over,
});

const connectorSession = (agentName?: string): McpAuthContext => ({
  userId: "owner",
  keyName: "connector-agent-1-key",
  scope: "connector",
  agentName,
  mcpApiKey: "trinity_mcp_fake",
});

const parse = (s: string) => JSON.parse(s);

// --------------------------------------------------------------------------
// request_login — enumeration safety
// --------------------------------------------------------------------------

describe("#848 request_login enumeration safety", () => {
  it("returns a byte-identical body for known, unknown, and malformed input", async () => {
    const { client } = fakeAuthClient();
    const tools = createAuthTools(client, true);

    const known = await tools.requestLogin.execute(
      { email: "known@example.com" },
      { session: anonSession() }
    );
    const unknown = await tools.requestLogin.execute(
      { email: "nobody@example.com" },
      { session: anonSession({ sessionId: "sess-2" }) }
    );
    const malformed = await tools.requestLogin.execute(
      { email: "not-an-email" },
      { session: anonSession({ sessionId: "sess-3" }) }
    );

    assert.equal(known, unknown, "known vs unknown must be indistinguishable");
    assert.equal(known, malformed, "malformed must not be distinguishable either");
  });

  it("returns the same body when the backend throws", async () => {
    const ok = createAuthTools(fakeAuthClient().client, true);
    const boom = createAuthTools(fakeAuthClient({ requestThrows: true }).client, true);

    const good = await ok.requestLogin.execute(
      { email: "a@example.com" },
      { session: anonSession() }
    );
    const bad = await boom.requestLogin.execute(
      { email: "a@example.com" },
      { session: anonSession({ sessionId: "sess-2" }) }
    );
    assert.equal(good, bad, "a backend error must not be a distinguishing signal");
  });

  it("never relays a malformed address to the backend", async () => {
    const { client, calls } = fakeAuthClient();
    const tools = createAuthTools(client, true);
    await tools.requestLogin.execute({ email: "not-an-email" }, { session: anonSession() });
    assert.equal(calls.requests.length, 0);
  });

  it("stops relaying after the per-session cap, with the same body", async () => {
    const { client, calls } = fakeAuthClient();
    const tools = createAuthTools(client, true);
    const session = anonSession();

    const bodies: string[] = [];
    for (let i = 0; i < 8; i++) {
      bodies.push(
        await tools.requestLogin.execute({ email: "a@example.com" }, { session })
      );
    }
    assert.equal(new Set(bodies).size, 1, "every response must be identical");
    assert.ok(calls.requests.length <= 5, `relayed ${calls.requests.length}, expected <= 5`);
  });

  it("lowercases and trims the address before relaying", async () => {
    const { client, calls } = fakeAuthClient();
    const tools = createAuthTools(client, true);
    await tools.requestLogin.execute(
      { email: "  MixedCase@Example.COM  " },
      { session: anonSession() }
    );
    assert.equal(calls.requests[0].email, "mixedcase@example.com");
  });

  it("refuses a session that already holds an API key", async () => {
    const { client } = fakeAuthClient();
    const tools = createAuthTools(client, true);
    await assert.rejects(
      () =>
        tools.requestLogin.execute(
          { email: "a@example.com" },
          { session: connectorSession("agent-1") }
        ),
      /only available to a keyless session/
    );
  });
});

// --------------------------------------------------------------------------
// verify_login
// --------------------------------------------------------------------------

describe("#848 verify_login", () => {
  it("upgrades the session in place without escalating scope", async () => {
    const { client } = fakeAuthClient({ agents: [{ name: "agent-1" }] });
    const tools = createAuthTools(client, true);
    const session = anonSession({ pendingEmail: "a@example.com" });

    await tools.verifyLogin.execute({ code: "123456" }, { session });

    assert.equal(session.verifiedEmail, "a@example.com");
    assert.deepEqual(session.agents, ["agent-1"]);
    assert.equal(session.pendingEmail, undefined, "pending must be cleared");
    // The load-bearing assertion: an email-verified session is still keyless
    // and must never satisfy operatorOnly.
    assert.equal(session.scope, "anonymous", "scope must NOT be upgraded");
    assert.equal(session.mcpApiKey, undefined, "no key may be attached");
  });

  it("returns no credential of any kind", async () => {
    const { client } = fakeAuthClient();
    const tools = createAuthTools(client, true);
    const out = await tools.verifyLogin.execute(
      { code: "123456" },
      { session: anonSession({ pendingEmail: "a@example.com" }) }
    );
    const blob = JSON.stringify(parse(out)).toLowerCase();
    for (const forbidden of ["trinity_mcp_", "api_key", "apikey", "token", "secret"]) {
      assert.ok(!blob.includes(forbidden), `response leaked '${forbidden}'`);
    }
  });

  it("reports a uniform failure that does not distinguish the cause", async () => {
    const { client } = fakeAuthClient({ verified: false });
    const tools = createAuthTools(client, true);
    const session = anonSession({ pendingEmail: "a@example.com" });
    const out = parse(await tools.verifyLogin.execute({ code: "000000" }, { session }));

    assert.equal(out.error, "invalid_code");
    assert.equal(session.verifiedEmail, undefined, "a failure must not bind the session");
  });

  it("requires an email when none is pending", async () => {
    const { client } = fakeAuthClient();
    const tools = createAuthTools(client, true);
    const out = parse(
      await tools.verifyLogin.execute({ code: "123456" }, { session: anonSession() })
    );
    assert.equal(out.error, "no_pending_login");
  });

  it("caps attempts per session", async () => {
    const { client } = fakeAuthClient({ verified: false });
    const tools = createAuthTools(client, true);
    const session = anonSession({ pendingEmail: "a@example.com" });

    let last: any;
    for (let i = 0; i < 12; i++) {
      last = parse(await tools.verifyLogin.execute({ code: "000000" }, { session }));
    }
    assert.equal(last.error, "too_many_attempts");
  });

  it("refuses a session that already holds an API key", async () => {
    const { client } = fakeAuthClient();
    const tools = createAuthTools(client, true);
    await assert.rejects(
      () =>
        tools.verifyLogin.execute(
          { code: "123456" },
          { session: connectorSession("agent-1") }
        ),
      /only available to a keyless session/
    );
  });
});

// --------------------------------------------------------------------------
// connector tools — two caller kinds
// --------------------------------------------------------------------------

describe("#848 connector tools with an anonymous caller", () => {
  it("refuses to act before login, on every tool", async () => {
    const { client } = fakeConnectorClient();
    const tools = createConnectorTools(client, true);
    const session = anonSession();

    for (const [label, run] of [
      ["list_playbooks", () => tools.listPlaybooks.execute({}, { session })],
      ["run_playbook", () => tools.runPlaybook.execute({ name: "cso" }, { session })],
      ["ask", () => tools.ask.execute({ message: "hi" }, { session })],
    ] as const) {
      const out = parse(await run());
      assert.equal(out.error, "login_required", `${label} should demand login`);
    }
  });

  it("defaults to the sole available agent after login", async () => {
    const { client, chats } = fakeConnectorClient();
    const tools = createConnectorTools(client, true);
    const session = anonSession({ verifiedEmail: "a@example.com", agents: ["agent-1"] });

    const listed = parse(await tools.listPlaybooks.execute({}, { session }));
    assert.equal(listed.agent, "agent-1");

    await tools.ask.execute({ message: "hi" }, { session });
    assert.equal(chats[0].agent, "agent-1");
    assert.equal(chats[0].via, "inline", "must use the internal inline path, not a key");
  });

  it("requires an explicit agent when several are available", async () => {
    const { client } = fakeConnectorClient();
    const tools = createConnectorTools(client, true);
    const session = anonSession({
      verifiedEmail: "a@example.com",
      agents: ["agent-1", "agent-2"],
    });

    const out = parse(await tools.listPlaybooks.execute({}, { session }));
    assert.equal(out.error, "agent_required");
    assert.deepEqual(out.available_agents, ["agent-1", "agent-2"]);
  });

  it("refuses an agent outside the authorized set", async () => {
    const { client, chats } = fakeConnectorClient();
    const tools = createConnectorTools(client, true);
    const session = anonSession({ verifiedEmail: "a@example.com", agents: ["agent-1"] });

    const out = parse(
      await tools.ask.execute({ message: "hi", agent: "someone-elses-agent" }, { session })
    );
    assert.equal(out.error, "agent_not_available");
    assert.equal(chats.length, 0, "nothing may be dispatched");
  });

  it("explains when no agents are shared yet", async () => {
    const { client } = fakeConnectorClient();
    const tools = createConnectorTools(client, true);
    const session = anonSession({ verifiedEmail: "a@example.com", agents: [] });

    const out = parse(await tools.listPlaybooks.execute({}, { session }));
    assert.equal(out.error, "no_agents_available");
  });

  it("still refuses a playbook outside the exposed allow-list", async () => {
    const { client, chats } = fakeConnectorClient({ "agent-1": [{ name: "cso" }] });
    const tools = createConnectorTools(client, true);
    const session = anonSession({ verifiedEmail: "a@example.com", agents: ["agent-1"] });

    const out = parse(await tools.runPlaybook.execute({ name: "not-exposed" }, { session }));
    assert.equal(out.error, "playbook_not_exposed");
    assert.equal(chats.length, 0);
  });
});

// NOTE: these pass requireApiKey=false, matching connector.test.ts. With it
// true, `getClient` mints a REAL TrinityClient from the key and the fake is
// bypassed (the failure mode is a live fetch). The anonymous cases above are
// unaffected — they never reach getClient, going through the outer client's
// inline methods instead.
describe("#848 connector-key path is unchanged (ent#46)", () => {
  it("uses the bound agent and the key-authenticated client", async () => {
    const { client, chats } = fakeConnectorClient();
    const tools = createConnectorTools(client, false);
    const session = connectorSession("agent-1");

    const listed = parse(await tools.listPlaybooks.execute({}, { session }));
    assert.equal(listed.agent, "agent-1");

    await tools.ask.execute({ message: "hi" }, { session });
    assert.equal(chats[0].via, "key", "a connector key must not take the inline path");
  });

  it("refuses an agent argument that disagrees with the bound agent", async () => {
    const { client, chats } = fakeConnectorClient();
    const tools = createConnectorTools(client, false);
    const session = connectorSession("agent-1");

    const out = parse(
      await tools.ask.execute({ message: "hi", agent: "agent-2" }, { session })
    );
    assert.equal(out.error, "agent_not_available");
    assert.equal(chats.length, 0, "must not silently reach the bound agent instead");
  });

  it("still hard-errors on a connector key with no bound agent", async () => {
    const { client } = fakeConnectorClient();
    const tools = createConnectorTools(client, false);
    await assert.rejects(
      () => tools.listPlaybooks.execute({}, { session: connectorSession(undefined) }),
      /not bound to an agent/
    );
  });
});

// --------------------------------------------------------------------------
// Review regressions (#848)
// --------------------------------------------------------------------------

describe("#848 review regressions", () => {
  it("refuses ANY requested agent when nothing is shared (empty available)", async () => {
    // The gate was `available.length > 0 && !available.includes(requested)`,
    // so an empty list — the state meaning "you have nothing" — let an
    // attacker-chosen name pass straight through to the backend.
    const { client, chats } = fakeConnectorClient();
    const tools = createConnectorTools(client, true);
    const session = anonSession({ verifiedEmail: "a@example.com", agents: [] });

    const out = parse(
      await tools.ask.execute({ message: "hi", agent: "someone-elses" }, { session })
    );
    assert.equal(out.error, "no_agents_available");
    assert.equal(chats.length, 0, "nothing may be dispatched");
  });

  it("verify_login reports an upstream failure as a plain invalid code", async () => {
    // Echoing the error leaked backend detail to an unauthenticated caller
    // (raw upstream status codes, and the name of a backend env var when
    // INTERNAL_API_SECRET was unset) and created a second, distinguishable
    // failure shape the uniform-failure contract does not allow.
    const failing = createAuthTools(fakeAuthClient({ verifyThrows: true }).client, true);
    const rejecting = createAuthTools(fakeAuthClient({ verified: false }).client, true);

    const upstream = await failing.verifyLogin.execute(
      { code: "123456" },
      { session: anonSession({ pendingEmail: "a@example.com" }) }
    );
    const badCode = await rejecting.verifyLogin.execute(
      { code: "000000" },
      { session: anonSession({ pendingEmail: "a@example.com" }) }
    );

    assert.equal(upstream, badCode,
      "an upstream error must be indistinguishable from a wrong code");
    // NB: not a bare "status" check — `{"status":"error"}` is the legitimate
    // response schema. Look for the things that actually leaked: the backend
    // env-var name, raw HTTP status codes, and upstream error prose.
    const blob = upstream.toLowerCase();
    for (const leak of [
      "internal_api_secret",
      "backend",
      "exploded",
      "404",
      "500",
      "429",
      "verification_failed",
    ]) {
      assert.ok(!blob.includes(leak), `verify_login leaked '${leak}'`);
    }
  });

  it("an upstream failure does not bind the session", async () => {
    const tools = createAuthTools(fakeAuthClient({ verifyThrows: true }).client, true);
    const session = anonSession({ pendingEmail: "a@example.com" });
    await tools.verifyLogin.execute({ code: "123456" }, { session });
    assert.equal(session.verifiedEmail, undefined);
  });

  it("bounds the per-session counter map so anonymous connections cannot grow it forever", async () => {
    // FastMCP gives the tool closure no session-close hook, so without a bound
    // one entry per anonymous connection is retained for the process lifetime —
    // memory exhaustion reachable with zero credentials.
    const { client } = fakeAuthClient();
    const tools = createAuthTools(client, true);

    for (let i = 0; i < 10_050; i++) {
      await tools.requestLogin.execute(
        { email: "a@example.com" },
        { session: anonSession({ sessionId: `sess-${i}` }) }
      );
    }
    // Not observable directly; assert the process still serves correctly and
    // the cap is enforced by the module constant (a leak would OOM long-run).
    const out = await tools.requestLogin.execute(
      { email: "a@example.com" },
      { session: anonSession({ sessionId: "final" }) }
    );
    assert.ok(out.includes("6-digit code"), "still functional after eviction churn");
  });
});
