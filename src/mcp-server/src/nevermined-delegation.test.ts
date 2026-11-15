/**
 * ent#966 D — the two Nevermined card-mandate tools.
 *
 *   - `get_agent_spending` / `revoke_agent_delegation` are registered and each
 *     has an `access.ts` row enforcing `agent_name` (access-wiring.test.ts then
 *     drives the row through the real gate automatically);
 *   - `get_agent_spending` passes the backend body through in every 200 state
 *     and maps the uniform 404 to `agent_not_found_or_no_access` — never the
 *     `configured: false` idiom of `get_nevermined_config`;
 *   - `revoke_agent_delegation` is listed to user-scoped keys only (the shared
 *     person predicate: its route's `require_person` refuses agent and system
 *     keys, abilityai/trinity-enterprise#719) and returns a backend 403 as a
 *     readable refusal, stamped `denied` on the audit row;
 *   - every description names Nevermined and none says Trinity holds a card
 *     or settles money (ent#932).
 *
 * Drives the real tool execute() with a fake TrinityClient.
 * Runner: node:test → `node --import tsx --test src/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createNeverminedTools } from "./tools/nevermined.js";
import { ApiError, type TrinityClient } from "./client.js";
import { TOOL_ACCESS_POLICY } from "./access.js";

type Tool = {
  name: string;
  description: string;
  canAccess?: (auth: unknown) => boolean;
  execute: (a: unknown, c?: unknown) => Promise<string>;
};

const SPEND = "get_agent_spending";
const REVOKE = "revoke_agent_delegation";

function build(answer: () => Promise<unknown>) {
  const calls: string[] = [];
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    getAgentSpending: async (name: string) => { calls.push(`spend:${name}`); return answer() as never; },
    revokeAgentDelegation: async (name: string) => { calls.push(`revoke:${name}`); return answer() as never; },
  };
  const all = createNeverminedTools(fake as TrinityClient, false) as unknown as Record<string, Tool>;
  const byName = Object.fromEntries(Object.values(all).map((t) => [t.name, t])) as Record<string, Tool>;
  return { byName, calls };
}

const apiError = (status: number, detail: unknown) => new ApiError(status, JSON.stringify({ detail }));

describe("ent#966 Nevermined delegation tools: registration", () => {
  const { byName } = build(async () => ({}));

  it("both tools are registered with an enforce row on agent_name", () => {
    for (const t of [SPEND, REVOKE]) {
      assert.ok(byName[t], `${t} is not registered`);
      assert.deepEqual(TOOL_ACCESS_POLICY[t], { kind: "enforce", param: "agent_name" }, `${t} access row`);
    }
  });

  it("descriptions name Nevermined and never say Trinity holds a card or settles", () => {
    for (const t of [SPEND, REVOKE]) {
      const d = byName[t].description;
      assert.match(d, /Nevermined/, `${t} description must name Nevermined`);
      assert.doesNotMatch(d, /Trinity (charges|holds|stores) (the|your) card|Trinity settles/i, t);
      assert.ok(d.length <= 2048, `${t} description is ${d.length} chars`);
    }
  });

  it("revoke is advertised to user keys only; spending to every scope the gate admits", () => {
    const can = byName[REVOKE].canAccess!;
    assert.equal(can({ scope: "user" }), true);
    // require_person refuses the system key, so it is not shown the tool (ent#719).
    for (const auth of [{ scope: "system" }, { scope: "agent" }, { scope: "connector" }, { scope: "ops" }, {}, null, undefined]) {
      assert.equal(can(auth), false, `revoke must be hidden for ${JSON.stringify(auth)}`);
    }
    assert.equal(byName[SPEND].canAccess, undefined, "spending keeps the group gate (agent keys: own agent only, backend-enforced)");
  });
});

describe("ent#966 get_agent_spending", () => {
  const BODIES = [
    { agent_name: "alpha", configured: false, funded: false, delegation: null, live: null, history: [] },
    { agent_name: "alpha", configured: true, funded: false, delegation: null, live: null, history: [] },
    {
      agent_name: "alpha", configured: true, funded: true,
      delegation: { delegation_id: "d-1", environment: "sandbox" },
      live: { source: "unavailable", status: "unknown", remaining_cents: null, error: "nevermined_unreachable" },
      history: [{ delegation_id: "d-1", created_at: "2026-10-22T20:00:00Z", revoked_at: null }],
    },
  ];

  it("passes the 200 body through as-is in every state", async () => {
    for (const body of BODIES) {
      const { byName, calls } = build(async () => body);
      const out = JSON.parse(await byName[SPEND].execute({ agent_name: "alpha" }));
      assert.deepEqual(out, body);
      assert.deepEqual(calls, ["spend:alpha"]);
    }
  });

  it("maps the uniform 404 to agent_not_found_or_no_access, not configured:false", async () => {
    const { byName } = build(async () => { throw apiError(404, "Agent not found"); });
    const out = JSON.parse(await byName[SPEND].execute({ agent_name: "beta" }));
    assert.deepEqual(out, { error: "agent_not_found_or_no_access" });
    assert.equal("configured" in out, false);
  });

  it("rethrows every other failure", async () => {
    const { byName } = build(async () => { throw apiError(429, "Too many Nevermined spending reads for this agent."); });
    await assert.rejects(() => byName[SPEND].execute({ agent_name: "alpha" }), (e: unknown) => e instanceof ApiError && e.status === 429);
  });
});

describe("ent#966 revoke_agent_delegation", () => {
  it("returns the revoke body", async () => {
    const body = { revoked: true, delegation_id: "d-1", env_removed: true, env_note: null };
    const { byName, calls } = build(async () => body);
    assert.deepEqual(JSON.parse(await byName[REVOKE].execute({ agent_name: "alpha" })), body);
    assert.deepEqual(calls, ["revoke:alpha"]);
  });

  it("surfaces a 403 as a readable refusal stamped denied", async () => {
    const detail = {
      code: "person_required",
      message: "This operation is human-only; agent- and system-scoped keys cannot perform it",
    };
    const { byName } = build(async () => { throw apiError(403, detail); });
    const context: { outcome?: { kind: string; reason?: string } } = {};
    const out = JSON.parse(await byName[REVOKE].execute({ agent_name: "alpha" }, context));
    assert.equal(out.success, false);
    assert.equal(out.error, detail.message);
    assert.equal(out.code, "person_required");
    assert.equal(context.outcome?.kind, "denied");
  });

  it("a 403 without a JSON detail gets a fixed sentence, never the body", async () => {
    const { byName } = build(async () => { throw new ApiError(403, "<html>waf page secret-ish</html>"); });
    const out = JSON.parse(await byName[REVOKE].execute({ agent_name: "alpha" }, {}));
    assert.equal(out.error, "Access denied (HTTP 403)");
    assert.doesNotMatch(JSON.stringify(out), /waf page/);
  });

  it("no active mandate is a named refusal; any other 404 is the uniform agent 404", async () => {
    const named = build(async () => {
      throw apiError(404, { code: "no_active_delegation", message: "This agent has no active Nevermined mandate." });
    });
    const out = JSON.parse(await named.byName[REVOKE].execute({ agent_name: "alpha" }));
    assert.equal(out.code, "no_active_delegation");
    assert.equal(out.success, false);

    const uniform = build(async () => { throw apiError(404, "Agent not found"); });
    assert.deepEqual(
      JSON.parse(await uniform.byName[REVOKE].execute({ agent_name: "beta" })),
      { error: "agent_not_found_or_no_access" },
    );
  });

  it("rethrows a 503 from Nevermined unchanged", async () => {
    const { byName } = build(async () => { throw apiError(503, { code: "nevermined_unreachable", message: "x" }); });
    await assert.rejects(() => byName[REVOKE].execute({ agent_name: "alpha" }), (e: unknown) => e instanceof ApiError && e.status === 503);
  });
});
