/**
 * #2991 — MCP `start_agent` says when assigned skills did not all land.
 *
 * The backend start response now carries `skills_injection` + `skills_result`;
 * the tool keeps its first line (the start message) and, only when delivery was
 * not clean, adds the status and one line per skill that was not delivered.
 * Drives the real tool execute() with a fake TrinityClient.
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";
import { createAgentTools, skillsDeliveryLines } from "./agents.js";
import type { TrinityClient } from "../client.js";

function startWith(answer: unknown) {
  const calls: string[] = [];
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    startAgent: async (name: string) => { calls.push(name); return answer as never; },
  };
  const tools = createAgentTools(fake as TrinityClient, false) as Record<string, { execute: (a: unknown, c: unknown) => Promise<string> }>;
  return { run: () => tools.startAgent.execute({ name: "acme-bot" }, {}), calls };
}

describe("start_agent skill delivery (#2991)", () => {
  it("a clean start is just the message", async () => {
    const { run } = startWith({ message: "Agent acme-bot started", skills_injection: "success",
      skills_result: { status: "success", conflicts: [], skills: { research: { status: "injected" } } } });
    assert.equal(await run(), "Agent acme-bot started");
  });

  it("a no-op start (no skills, already running) adds nothing", async () => {
    for (const reason of ["no_skills", "container_already_running"]) {
      const { run } = startWith({ message: "Agent acme-bot started", skills_injection: "skipped",
        skills_result: { status: "skipped", reason, conflicts: [] } });
      assert.equal(await run(), "Agent acme-bot started");
    }
  });

  it("a conflict under a `success` status is still called out", async () => {
    const { run } = startWith({ message: "Agent acme-bot started", skills_injection: "success",
      skills_result: { status: "success", conflicts: ["backlog"],
        skills: { research: { status: "injected" }, backlog: { status: "conflict", code: "name_conflict" } } } });
    assert.equal(await run(), "Agent acme-bot started\nSkills delivery: success\n- backlog: conflict (name_conflict)");
  });

  it("a partial delivery names each skill that did not land", async () => {
    const { run } = startWith({ message: "Agent acme-bot started", skills_injection: "partial",
      skills_result: { status: "partial", conflicts: [], skills: {
        research: { status: "injected" }, big: { status: "failed", code: "skill_too_large" } } } });
    const out = (await run()).split("\n");
    assert.deepEqual(out, ["Agent acme-bot started", "Skills delivery: partial", "- big: failed (skill_too_large)"]);
  });

  it("an older backend without the fields reads as unknown, never as clean", () => {
    assert.deepEqual(skillsDeliveryLines({ message: "Agent acme-bot started" }), ["Skills delivery: unknown"]);
  });

  it("a skip with a failure reason is shown", () => {
    assert.deepEqual(
      skillsDeliveryLines({ message: "m", skills_injection: "skipped",
        skills_result: { status: "skipped", reason: "injection_already_running", conflicts: [],
          skills: { x: { status: "failed" } } } }),
      ["Skills delivery: skipped (injection_already_running)", "- x: failed"]);
  });
});
