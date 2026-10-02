/**
 * trinity-enterprise#638 — the seat decision record tools.
 *
 * Pins the tool-layer contract:
 *   - `record_decision` POSTs the grammar fields (never `agent_name` in the
 *     body) with a DETERMINISTIC Idempotency-Key over (agent, execution_id,
 *     decided) — a transport retry records once, a different decision does not
 *     collide;
 *   - `list_seat_decisions` GETs with `execution_id` (and `include_history`
 *     only when asked) — the seat is never named by the caller;
 *   - both tools resolve the agent from an agent-scoped key and refuse a
 *     user key with no `agent_name` in the shared envelope;
 *   - a backend refusal (the grammar receipt) reaches the agent as the
 *     `success:false` envelope, not a thrown transport error;
 *   - both tools carry an `enforce:{param:"agent_name"}` policy row, so the
 *     server boots (`policyFor` throws on a missing row) and a foreign target
 *     is gated at registration.
 *
 * Drives the real execute() with a fake TrinityClient (requireApiKey=false).
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";
import { createDecisionTools } from "./decisions.js";
import { policyFor } from "../access.js";
import { ApiError } from "../client.js";
import type { TrinityClient } from "../client.js";

type Recorded = { method: string; agent: string; body?: unknown; key?: string; execId?: string; history?: boolean };

function makeTools(calls: Recorded[], fail?: () => never) {
  const fake = {
    getBaseUrl: () => "http://backend:8000",
    async recordSeatDecision(agent: string, body: Record<string, unknown>, key?: string) {
      if (fail) fail();
      calls.push({ method: "record", agent, body, key });
      return { success: true, decision: { id: "d1", status: "active" }, hint: null };
    },
    async listSeatDecisions(agent: string, execId: string, history: boolean) {
      if (fail) fail();
      calls.push({ method: "list", agent, execId, history });
      return { agent_name: agent, decisions: [], stats: { recorded: 0, reused: 0 } };
    },
  } as unknown as TrinityClient;
  return createDecisionTools(fake, false);
}

const RECORD = {
  execution_id: "exec-1",
  outcome: "approved" as const,
  decided: "Renew the Acme contract",
  alternatives: ["let it lapse", "renegotiate first"],
  criterion: "renewal cost below the switching cost",
  reversal: "Acme raises the price above the switching cost",
  review_by: "2027-01-31",
};

describe("record_decision", () => {
  it("POSTs the grammar fields without agent_name and with a deterministic Idempotency-Key", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    await tools.recordDecision.execute({ ...RECORD, agent_name: "companion" }, {});
    await tools.recordDecision.execute({ ...RECORD, agent_name: "companion" }, {});
    await tools.recordDecision.execute({ ...RECORD, decided: "Let Acme lapse", agent_name: "companion" }, {});
    assert.equal(calls.length, 3);
    assert.equal(calls[0].agent, "companion");
    const body = calls[0].body as Record<string, unknown>;
    assert.equal(body.agent_name, undefined, "agent_name never rides the body");
    assert.equal(body.execution_id, "exec-1");
    assert.deepEqual(body.alternatives, RECORD.alternatives);
    assert.ok(calls[0].key?.startsWith("mcp:"));
    assert.equal(calls[0].key, calls[1].key, "same args ⇒ same key (a retry records once)");
    assert.notEqual(calls[0].key, calls[2].key, "a different decision does not collide");
  });

  it("refuses a user-scoped call with no agent_name in the shared envelope", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    const out = JSON.parse(await tools.recordDecision.execute(RECORD, {}));
    assert.equal(out.success, false);
    assert.match(out.error, /agent name/i);
    assert.equal(calls.length, 0);
  });

  it("returns a backend refusal (the grammar receipt) as success:false, never a throw", async () => {
    const tools = makeTools([], () => {
      throw new ApiError(422, JSON.stringify({ detail: { code: "decision_prose_only", receipt: { fields: { criterion: "one line" } } } }));
    });
    const out = JSON.parse(await tools.recordDecision.execute({ ...RECORD, agent_name: "companion" }, {}));
    assert.equal(out.success, false);
    assert.match(out.error, /decision_prose_only/);
  });
});

describe("list_seat_decisions", () => {
  it("GETs by execution_id; include_history only when asked", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    await tools.listSeatDecisions.execute({ execution_id: "exec-1", agent_name: "companion" }, {});
    await tools.listSeatDecisions.execute({ execution_id: "exec-1", include_history: true, agent_name: "companion" }, {});
    assert.deepEqual(calls.map((c) => [c.execId, c.history]), [["exec-1", false], ["exec-1", true]]);
  });
});

describe("get_autonomy (ent#641)", () => {
  it("GETs by execution_id — the seat is never a parameter", async () => {
    const calls: Recorded[] = [];
    const fake = {
      getBaseUrl: () => "http://backend:8000",
      async getSeatAutonomy(agent: string, execId: string) {
        calls.push({ method: "autonomy", agent, execId });
        return { agent_name: agent, level: "L2", classes: [] };
      },
    } as unknown as TrinityClient;
    const tools = createDecisionTools(fake, false);
    const out = JSON.parse(await tools.getAutonomy.execute({ execution_id: "exec-1", agent_name: "companion" }, {}));
    assert.equal(out.level, "L2");
    assert.deepEqual(calls, [{ method: "autonomy", agent: "companion", execId: "exec-1" }]);
  });

  it("refuses a user-scoped call with no agent_name, and never reaches the network", async () => {
    const calls: Recorded[] = [];
    const fake = {
      getBaseUrl: () => "http://backend:8000",
      async getSeatAutonomy(agent: string, execId: string) { calls.push({ method: "autonomy", agent, execId }); return {}; },
    } as unknown as TrinityClient;
    const out = JSON.parse(await createDecisionTools(fake, false).getAutonomy.execute({ execution_id: "e" }, {}));
    assert.equal(out.success, false);
    assert.equal(calls.length, 0);
  });
});

describe("access policy (ent#628 rows)", () => {
  it("every tool carries an enforce row on agent_name, so the server boots and a foreign target is gated", () => {
    for (const name of ["record_decision", "list_seat_decisions", "get_autonomy"]) {
      const policy = policyFor({ name, parameters: { shape: { agent_name: {} } } } as any);
      assert.deepEqual(policy, { kind: "enforce", param: "agent_name" }, name);
    }
  });
});
