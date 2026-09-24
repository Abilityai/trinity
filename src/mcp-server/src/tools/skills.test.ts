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

// #2914 — a same-named agent-authored skill is a named `conflict`, never a
// silent overwrite: assign passes it through, sync surfaces it on the success
// branch (a conflict is not a failure), and the listing carries the durable
// row verdict.
describe("name conflicts (#2914)", () => {
  it("assign passes a conflict delivery through and the description names it", async () => {
    const calls: Recorded[] = [];
    const delivery = {
      status: "conflict", conflicts: ["backlog"],
      skills: { backlog: { status: "conflict", error: "name_conflict: …" } },
    };
    const tools = makeTools(calls, { success: true, message: "Skill assigned", delivery });
    const out = JSON.parse(
      await tools.assignSkillToAgent.execute({ agent_name: "acme-bot", skill_name: "backlog" }, {}),
    );
    assert.deepEqual(out.delivery, delivery);
    assert.ok(tools.assignSkillToAgent.description.includes("`conflict`"));
  });

  it("sync surfaces conflicts even though the run succeeded", async () => {
    const tools = makeTools([], {
      success: true, skills_injected: 1, skills_unchanged: 0, skills_failed: 0,
      skills_conflict: 1, conflicts: ["backlog"],
      results: {
        research: { success: true, status: "injected", files_written: 2, warnings: [] },
        backlog: { success: false, status: "conflict", files_written: 0,
                   error: "name_conflict: …", warnings: [] },
      },
    });
    const out = JSON.parse(await tools.syncAgentSkills.execute({ agent_name: "acme-bot" }, {}));
    assert.equal(out.success, true);
    assert.deepEqual(out.conflicts, ["backlog"]);
    assert.equal(out.skills_conflict, 1);
    assert.ok(out.message.includes("backlog"), "the message names the conflicted skill");
  });

  it("get_agent_skills carries delivery_status per row and lists the conflicts", async () => {
    const tools = makeTools([], [
      { id: 1, agent_name: "acme-bot", skill_name: "backlog", assigned_by: "alice",
        assigned_at: "2026-09-21T00:00:00Z", delivery_status: "conflict" },
      { id: 2, agent_name: "acme-bot", skill_name: "research", assigned_by: "alice",
        assigned_at: "2026-09-21T00:00:00Z", delivery_status: null },
    ]);
    const out = JSON.parse(await tools.getAgentSkills.execute({ agent_name: "acme-bot" }, {}));
    assert.deepEqual(out.conflicts, ["backlog"]);
    assert.equal(out.skills[0].delivery_status, "conflict");
    assert.equal(out.skills[1].delivery_status, null);
  });
});

// ent#530 — `set:<name>` routes to the set routes (same ent#596 fence); the
// listing says why each skill is present.
function makeRoutedTools(calls: Recorded[], answers: Record<string, unknown>) {
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    request: async (method: string, path: string, body?: unknown) => {
      calls.push({ method, path, body });
      if (!(path in answers)) throw new Error(`404 ${path}`);
      return answers[path] as never;
    },
  };
  return createSkillsTools(fake as TrinityClient, false);
}

describe("skill sets (ent#530)", () => {
  it("assign_skill_to_agent routes set:<name> to the set route, never the skill route", async () => {
    const calls: Recorded[] = [];
    const tools = makeRoutedTools(calls, {
      "/api/agents/acme-bot/skill-sets/dev-backlog": { success: true, set_name: "dev-backlog", members_added: ["groom"] },
    });
    const out = JSON.parse(
      await tools.assignSkillToAgent.execute({ agent_name: "acme-bot", skill_name: "set:dev-backlog" }, {}),
    );
    assert.equal(calls.length, 1);
    assert.equal(calls[0].method, "POST");
    assert.equal(calls[0].path, "/api/agents/acme-bot/skill-sets/dev-backlog");
    assert.deepEqual(out.members_added, ["groom"]);
  });

  it("a plain skill name still hits the skill route", async () => {
    const calls: Recorded[] = [];
    const tools = makeRoutedTools(calls, { "/api/agents/acme-bot/skills/research": { success: true } });
    await tools.assignSkillToAgent.execute({ agent_name: "acme-bot", skill_name: "research" }, {});
    assert.equal(calls[0].path, "/api/agents/acme-bot/skills/research");
  });

  it("set_agent_skills forwards set: entries to the PUT unchanged (the backend splits them)", async () => {
    const calls: Recorded[] = [];
    const tools = makeRoutedTools(calls, { "/api/agents/acme-bot/skills": { success: true, sets: ["dev-backlog"] } });
    const out = JSON.parse(
      await tools.setAgentSkills.execute({ agent_name: "acme-bot", skills: ["research", "set:dev-backlog"] }, {}),
    );
    assert.deepEqual(calls[0].body, { skills: ["research", "set:dev-backlog"] });
    assert.deepEqual(out.sets, ["dev-backlog"]);
  });

  it("unassign_skill_set DELETEs the set route, with or without the prefix", async () => {
    for (const name of ["dev-backlog", "set:dev-backlog"]) {
      const calls: Recorded[] = [];
      const tools = makeRoutedTools(calls, {
        "/api/agents/acme-bot/skill-sets/dev-backlog": { success: true, members_removed: ["groom"] },
      });
      const out = JSON.parse(await tools.unassignSkillSet.execute({ agent_name: "acme-bot", set_name: name }, {}));
      assert.equal(calls[0].method, "DELETE");
      assert.equal(calls[0].path, "/api/agents/acme-bot/skill-sets/dev-backlog");
      assert.deepEqual(out.members_removed, ["groom"]);
    }
  });

  it("get_agent_skills reports individual + via_sets per skill and the agent's sets", async () => {
    const tools = makeRoutedTools([], {
      "/api/agents/acme-bot/skills": [
        { id: 1, agent_name: "acme-bot", skill_name: "groom", assigned_by: "alice",
          assigned_at: "2026-09-24T00:00:00Z", individual: false, via_sets: ["dev-backlog"] },
        { id: 2, agent_name: "acme-bot", skill_name: "research", assigned_by: "alice",
          assigned_at: "2026-09-24T00:00:00Z" },
      ],
      "/api/agents/acme-bot/skill-sets": [{ name: "dev-backlog", status: "partial", members: [] }],
    });
    const out = JSON.parse(await tools.getAgentSkills.execute({ agent_name: "acme-bot" }, {}));
    assert.deepEqual(out.skills[0].via_sets, ["dev-backlog"]);
    assert.equal(out.skills[0].individual, false);
    assert.equal(out.skills[1].individual, true);        // a legacy row reads as individual
    assert.deepEqual(out.skills[1].via_sets, []);
    assert.deepEqual(out.sets, [{ name: "dev-backlog", status: "partial" }]);
  });

  it("get_agent_skills still answers when the set list cannot be read", async () => {
    const tools = makeRoutedTools([], {
      "/api/agents/acme-bot/skills": [
        { id: 1, agent_name: "acme-bot", skill_name: "research", assigned_by: "alice", assigned_at: "x" },
      ],
    });
    const out = JSON.parse(await tools.getAgentSkills.execute({ agent_name: "acme-bot" }, {}));
    assert.equal(out.skill_count, 1);
    assert.equal(out.sets, undefined);
  });
});
