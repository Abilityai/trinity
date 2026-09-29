/**
 * abilityai/trinity-enterprise#628 — the agent-access policy is a mechanism, not a discipline.
 *
 * Pins:
 *   - `createServer()` boots with the REAL `TOOL_ACCESS_POLICY`: every registered tool has a
 *     row and every row names a registered tool. A stale row fails, so the table can never rot
 *     into a permanent allowlist; a new tool without a row cannot register at all.
 *   - `policyFor()` refuses the three shapes that would let the table lie: no row, an `enforce`
 *     on a parameter the tool does not declare, a `none` on a tool whose parameters name an agent.
 *   - `withAgentAccess()` gates BEFORE the tool runs: an agent key with no edge never reaches
 *     `execute`; self / permitted / absent-param / user / system / no-context all do. An unknown
 *     scope, or an agent key with no agent name, is denied — an allowlist, not a fallthrough (#2323).
 *   - The permission read fails CLOSED: an unreadable permitted list is an empty list.
 *   - Every `baselined` row names an owner (an issue or a backend gate), never free text.
 *
 * Runner: node:test → `node --import tsx --test src/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";
import { z } from "zod";

import { createServer } from "./server.js";
import {
  AGENT_TARGET_PARAMS,
  TOOL_ACCESS_POLICY,
  ToolAccessPolicyError,
  checkAgentEdge,
  policyFor,
  withAgentAccess,
} from "./access.js";
import { TrinityClient } from "./client.js";
import type { McpAuthContext } from "./types.js";

function fakeClient(opts: { permitted?: boolean } = {}) {
  const calls: string[] = [];
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    isAgentPermitted: async (a: string, b: string) => {
      calls.push(`perm:${a}->${b}`);
      return opts.permitted ?? false;
    },
  };
  return { fake: fake as unknown as TrinityClient, calls };
}

const session = (scope: string, agentName?: string): { session: McpAuthContext } => ({
  session: { scope, agentName, userId: "owner", keyId: "k1", keyName: "kn" } as unknown as McpAuthContext,
});

describe("ent#628 TOOL_ACCESS_POLICY is total over the registered tools", () => {
  it("createServer boots: every registered tool has a row, every row names a registered tool", async () => {
    const built = await createServer({
      trinityApiUrl: "http://127.0.0.1:65535", // nothing listens there: the health probe fails non-critically
      requireApiKey: true, // key mode needs no backend credential at construction
      inlineAuthEnabled: true, // so the #848 auth tools register and their rows are not "stale"
    });
    const registered = built.builtinToolNames;
    assert.ok(registered.size >= 120, `expected the full tool surface, got ${registered.size}`);
    for (const name of registered) {
      assert.ok(TOOL_ACCESS_POLICY[name], `registered tool '${name}' has no policy row`);
    }
    for (const name of Object.keys(TOOL_ACCESS_POLICY)) {
      assert.ok(registered.has(name), `stale policy row '${name}': the tool is gone or renamed — remove the row`);
    }
  });

  it("the loop tools carry the rows this fix is about", () => {
    assert.deepEqual(TOOL_ACCESS_POLICY.run_agent_loop, { kind: "enforce", param: "agent_name" });
    assert.equal(TOOL_ACCESS_POLICY.get_loop_status.kind, "in-tool");
    assert.equal(TOOL_ACCESS_POLICY.stop_loop.kind, "in-tool");
  });

  it("ent#596: the three skill-changing tools name the backend skill-manager fence, not ENT629", () => {
    // They were `baselined: ENT629` — "ungated at MCP, backend owner-equivalent".
    // The fence now exists (routers/skills.py get_skill_managed_agent_by_name),
    // so a row pointing back at ENT629 would claim the gap is still open.
    for (const tool of ["assign_skill_to_agent", "set_agent_skills", "sync_agent_skills", "unassign_skill_set"]) {
      const policy = TOOL_ACCESS_POLICY[tool];
      assert.equal(policy.kind, "baselined", tool);
      assert.match((policy as { owner: string }).owner, /get_skill_managed_agent_by_name.*#596/, tool);
    }
    // The READ stays on ENT629: reading an agent's skills is not changing them.
    assert.match((TOOL_ACCESS_POLICY.get_agent_skills as { owner: string }).owner, /#629/);
  });

  it("every baselined row names an owner — an issue or a backend gate, never free text", () => {
    const OWNER =
      /#\d+|reject_agent_principal|require_admin|assert_admin|_self_gate|_enforce_connector_scope|agent_permissions|rooms service/;
    for (const [name, policy] of Object.entries(TOOL_ACCESS_POLICY)) {
      if (policy.kind === "baselined") {
        assert.match(policy.owner, OWNER, `${name}: baselined owner does not name an issue or a backend gate`);
      }
    }
  });
});

describe("ent#628 policyFor refuses a table that could lie", () => {
  const tool = (name: string, keys: string[]) => ({
    name,
    parameters: z.object(Object.fromEntries(keys.map((k) => [k, z.string()]))),
  });

  it("a tool with no row cannot register", () => {
    assert.throws(() => policyFor(tool("frobnicate_agent", ["agent_name"])), ToolAccessPolicyError);
    assert.throws(() => policyFor(tool("poll_something", ["loop_id"])), /has no entry in TOOL_ACCESS_POLICY/);
  });

  it("an enforce row must name a parameter the tool declares", () => {
    assert.throws(
      () => policyFor(tool("x", ["message"]), { kind: "enforce", param: "agent_name" }),
      /declares no such parameter/
    );
  });

  it("a none row is refused when the parameters name an agent", () => {
    for (const key of AGENT_TARGET_PARAMS) {
      assert.throws(() => policyFor(tool("x", [key]), { kind: "none", why: "no target" }), /name an agent/);
    }
  });

  it("an explicit policy wins for a computed tool name (dynamic tools)", () => {
    const explicit = { kind: "in-tool" as const, how: "bound agent" };
    assert.deepEqual(policyFor(tool("chat_with_support_bot", ["message"]), explicit), explicit);
  });
});

describe("ent#628 withAgentAccess gates before execute", () => {
  const wrap = (fake: TrinityClient, ran: string[]) =>
    withAgentAccess(
      "probe",
      async (p: Record<string, unknown>) => {
        ran.push(String(p.agent_name ?? "<self>"));
        return "ran";
      },
      { kind: "enforce", param: "agent_name" },
      () => fake
    );

  it("an agent key without an edge never reaches the tool", async () => {
    const { fake, calls } = fakeClient({ permitted: false });
    const ran: string[] = [];
    const out = JSON.parse(await wrap(fake, ran)({ agent_name: "b" }, session("agent", "a")));
    assert.deepEqual(ran, []);
    assert.equal(out.success, false);
    assert.equal(out.error, "Access denied");
    assert.match(out.reason, /Permission denied: Agent 'a' is not permitted to communicate with 'b'/);
    assert.equal(out.caller, "a");
    assert.equal(out.target, "b");
    assert.deepEqual(calls, ["perm:a->b"]);
  });

  it("an agent key with an edge runs the tool", async () => {
    const { fake } = fakeClient({ permitted: true });
    const ran: string[] = [];
    assert.equal(await wrap(fake, ran)({ agent_name: "b" }, session("agent", "a")), "ran");
    assert.deepEqual(ran, ["b"]);
  });

  it("self — named or by omission — runs without a permission read", async () => {
    const { fake, calls } = fakeClient();
    const ran: string[] = [];
    await wrap(fake, ran)({ agent_name: "a" }, session("agent", "a"));
    await wrap(fake, ran)({}, session("agent", "a"));
    assert.deepEqual(ran, ["a", "<self>"]);
    assert.deepEqual(calls, []);
  });

  it("a user key passes through — the backend already scopes it, by role and per-user grant", async () => {
    const { fake, calls } = fakeClient({ permitted: false });
    const ran: string[] = [];
    await wrap(fake, ran)({ agent_name: "b" }, session("user"));
    assert.deepEqual(ran, ["b"]);
    assert.deepEqual(calls, []);
  });

  it("system scope and no session (dev mode) both pass through", async () => {
    const { fake, calls } = fakeClient({ permitted: false });
    const ran: string[] = [];
    await wrap(fake, ran)({ agent_name: "b" }, session("system", "trinity-system"));
    await wrap(fake, ran)({ agent_name: "b" }, undefined);
    assert.deepEqual(ran, ["b", "b"]);
    assert.deepEqual(calls, []);
  });

  it("an unknown scope is denied — allowlist, not fallthrough (#2323)", async () => {
    const { fake } = fakeClient({ permitted: true });
    const ran: string[] = [];
    const out = JSON.parse(await wrap(fake, ran)({ agent_name: "b" }, session("ops")));
    assert.deepEqual(ran, []);
    assert.equal(out.success, false);
    assert.equal(out.reason, "Agent 'b' not found or not accessible");
  });

  it("an agent key with no agent name is denied, not promoted to the user rule", async () => {
    const { fake, calls } = fakeClient({ permitted: true });
    const ran: string[] = [];
    const out = JSON.parse(await wrap(fake, ran)({ agent_name: "b" }, session("agent", undefined)));
    assert.deepEqual(ran, []);
    assert.equal(out.success, false);
    assert.equal(out.reason, "Agent 'b' not found or not accessible");
    assert.deepEqual(calls, []);
  });
});

describe("ent#628 the permission read fails CLOSED", () => {
  it("an unreadable permitted list is an empty list, and the edge is denied", async () => {
    const real = new TrinityClient("http://127.0.0.1:1");
    (real as unknown as { request: () => Promise<never> }).request = async () => {
      throw new Error("backend unreachable");
    };
    assert.deepEqual(await real.getPermittedAgents("a"), []);
    assert.equal(await real.isAgentPermitted("a", "b"), false);
    const out = await checkAgentEdge(real, session("agent", "a").session, "b");
    assert.equal(out.allowed, false);
  });
});
