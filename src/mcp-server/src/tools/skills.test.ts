/**
 * #2703 — assign_skill_to_agent / set_agent_skills carry the backend's
 * `delivery` report through unchanged (three-surface sync, Invariant #13).
 *
 * Before this the tools returned `{success, message}` and their descriptions
 * said the skill "will be injected when the agent starts" — true, and the
 * reason a Library assign silently did nothing until a Sync. Now the backend
 * delivers on assign and reports per skill; these tests pin that the tool
 * layer passes that block through verbatim and that the descriptions name the
 * vocabulary an agent has to read.
 *
 * Drives the real tool execute() with a fake TrinityClient (requireApiKey=false
 * → getClient() returns the fake directly).
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";
import { createSkillsTools } from "./skills.js";
import type { TrinityClient } from "../client.js";

type Recorded = { method: string; path: string; body?: unknown };

function makeTools(calls: Recorded[], answer: unknown) {
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    request: async (method: string, path: string, body?: unknown) => {
      calls.push({ method, path, body });
      return answer as never;
    },
  };
  return createSkillsTools(fake as TrinityClient, false);
}

const DELIVERY = {
  status: "not_delivered",
  reason: "injection_in_progress",
  skills: { research: { status: "not_delivered" } },
};

describe("assign_skill_to_agent (#2703)", () => {
  it("passes the backend delivery block through verbatim", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls, {
      success: true, message: "Skill assigned", skill: { skill_name: "research" }, delivery: DELIVERY,
    });
    const out = JSON.parse(
      await tools.assignSkillToAgent.execute({ agent_name: "acme-bot", skill_name: "research" }, {}),
    );
    assert.equal(calls[0].method, "POST");
    assert.equal(calls[0].path, "/api/agents/acme-bot/skills/research");
    assert.deepEqual(out.delivery, DELIVERY);           // never re-derived
    assert.equal(out.success, true);
  });

  it("describes the delivery vocabulary the agent has to read", () => {
    const tools = makeTools([], {});
    const d = tools.assignSkillToAgent.description;
    for (const word of ["injected", "pending_start", "in_progress", "not_delivered", "sync_agent_skills"]) {
      assert.ok(d.includes(word), `description names ${word}`);
    }
    assert.ok(!d.includes("will be injected when the agent starts"), "the old promise is gone");
  });
});

describe("set_agent_skills (#2703)", () => {
  it("passes both halves — delivery and removal — through", async () => {
    const calls: Recorded[] = [];
    const removal = { status: "completed", skills_removed: 1, skills_failed: 0, results: {} };
    const tools = makeTools(calls, {
      success: true, agent_name: "acme-bot", skills_assigned: 2, skills: ["a", "b"],
      delivery: { status: "injected", skills: { b: { status: "injected" } } }, removal,
    });
    const out = JSON.parse(
      await tools.setAgentSkills.execute({ agent_name: "acme-bot", skills: ["a", "b"] }, {}),
    );
    assert.equal(calls[0].method, "PUT");
    assert.deepEqual(calls[0].body, { skills: ["a", "b"] });
    assert.equal(out.delivery.status, "injected");
    assert.deepEqual(out.removal, removal);
  });
});
