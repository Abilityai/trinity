/**
 * trinity-enterprise#753 — list_skill_gates / set_skill_gate / clear_skill_gate,
 * the MCP surface of the per-agent skill gate map (Invariant #13).
 *
 * The tools are thin proxies; the backend decides who may (routers/skill_gate.py).
 * What the tool layer itself owns, and these pin:
 *   - each tool reaches its route, names url-encoded;
 *   - set_skill_gate sends ONLY the fields the caller gave — an omitted field
 *     keeps its stored value on the backend, an explicit null resets the
 *     deadline — and nothing else (a stray key never reaches the body);
 *   - the zod schema refuses what the backend would (a bad approver, a deadline
 *     outside 1..168, a boolean or a string for hours) before a round trip;
 *   - every tool has a TOOL_ACCESS_POLICY row (server.ts refuses to register one
 *     without), and its description fits the 2,048-character cap.
 *
 * Drives the real execute() with a fake TrinityClient (requireApiKey=false).
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";
import { createSkillsTools } from "./skills.js";
import { TOOL_ACCESS_POLICY } from "../access.js";
import type { TrinityClient } from "../client.js";

type Recorded = { method: string; path: string; body?: unknown };

function makeTools(calls: Recorded[], answer: unknown = { ok: true }) {
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    request: async (method: string, path: string, body?: unknown) => {
      calls.push({ method, path, body });
      return answer as never;
    },
  };
  return createSkillsTools(fake as TrinityClient, false);
}

describe("skill gate tools (trinity-enterprise#753)", () => {
  it("list_skill_gates reads the agent's map", async () => {
    const calls: Recorded[] = [];
    const answer = { agent_name: "fin bot", gates: [], approver_kinds: ["primary"] };
    const out = JSON.parse(await makeTools(calls, answer).listSkillGates.execute({ agent_name: "fin bot" }, {}));
    assert.deepEqual(calls, [{ method: "GET", path: "/api/agents/fin%20bot/skill-gates", body: undefined }]);
    assert.deepEqual(out, answer);
  });

  it("set_skill_gate sends only the fields given", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    await tools.setSkillGate.execute({ agent_name: "fin", skill_name: "pay-invoice" }, {});
    await tools.setSkillGate.execute({ agent_name: "fin", skill_name: "pay-invoice", deadline_hours: 12 }, {});
    await tools.setSkillGate.execute({ agent_name: "fin", skill_name: "pay-invoice", deadline_hours: null }, {});
    await tools.setSkillGate.execute({ agent_name: "fin", skill_name: "pay-invoice", approver: "approver" }, {});
    await tools.setSkillGate.execute(
      { agent_name: "fin", skill_name: "pay-invoice", approver: "primary", agent: "other" } as never, {});
    assert.ok(calls.every(c => c.method === "PUT" && c.path === "/api/agents/fin/skill-gates/pay-invoice"));
    assert.deepEqual(calls.map(c => c.body), [
      {},
      { deadline_hours: 12 },
      { deadline_hours: null },
      { approver: "approver" },
      { approver: "primary" },
    ]);
  });

  it("clear_skill_gate deletes the gate", async () => {
    const calls: Recorded[] = [];
    await makeTools(calls).clearSkillGate.execute({ agent_name: "fin", skill_name: "pay/invoice" }, {});
    assert.deepEqual(calls, [{ method: "DELETE", path: "/api/agents/fin/skill-gates/pay%2Finvoice", body: undefined }]);
  });

  it("the schema refuses what the backend would", () => {
    const p = makeTools([]).setSkillGate.parameters;
    const ok = (v: unknown) => p.safeParse({ agent_name: "fin", skill_name: "s", ...(v as object) }).success;
    assert.ok(ok({}));
    assert.ok(ok({ approver: "primary", deadline_hours: 1 }));
    assert.ok(ok({ deadline_hours: 168 }));
    assert.ok(ok({ deadline_hours: null }));
    for (const bad of [
      { approver: "owner" }, { approver: 1 },
      { deadline_hours: 0 }, { deadline_hours: 169 }, { deadline_hours: 2.5 },
      { deadline_hours: true }, { deadline_hours: "24" },
    ]) {
      assert.ok(!ok(bad), `refused: ${JSON.stringify(bad)}`);
    }
  });

  it("list_skills carries the library's approval recommendation", async () => {
    const tools = makeTools([], [
      { name: "deploy", description: "d", path: "p", approval: "recommended" },
      { name: "notes", description: "d", path: "p" },
    ]);
    const out = JSON.parse(await tools.listSkills.execute({}, {}));
    assert.deepEqual(out.skills.map((s: { approval: unknown }) => s.approval), ["recommended", null]);
  });

  it("every tool has an access-policy row and fits the description cap", () => {
    const tools = makeTools([]);
    for (const tool of [tools.listSkillGates, tools.setSkillGate, tools.clearSkillGate]) {
      const row = TOOL_ACCESS_POLICY[tool.name];
      assert.ok(row, `${tool.name} has a policy row`);
      assert.equal(row.kind, "baselined");
      assert.ok(tool.description.length <= 2048, `${tool.name}: ${tool.description.length}`);
    }
    assert.ok(tools.clearSkillGate.description.includes("never on the calling agent itself"));
    assert.ok(tools.setSkillGate.description.includes("never on the calling agent itself"));
  });
});
