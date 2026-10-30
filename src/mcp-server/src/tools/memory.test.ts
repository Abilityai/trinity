/**
 * trinity-enterprise#419 — write_user_memory's refusal reaches the agent.
 *
 * The backend refuses a poisoned write with `422 {detail: {code, message,
 * line}}` and stores nothing. The agent can only rewrite if the NAMED reason and
 * the line reach its tool result, so this drives the real `execute()` with a
 * fake TrinityClient that throws the same `ApiError` the real client throws on a
 * non-2xx, and asserts what the agent reads. It also pins that the published
 * description tells the agent what is refused (Invariant #13: the third surface
 * says what the boundary enforces); the 2,048-character budget is the global
 * `tool-description-budget.test.ts`.
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";
import { createMemoryTools } from "./memory.js";
import { ApiError } from "../client.js";
import type { TrinityClient } from "../client.js";

function makeTools(
  write: (agent: string, body: Record<string, unknown>) => Promise<unknown>,
  search: (agent: string, body: Record<string, unknown>) => Promise<unknown> = async () => ({}),
) {
  const fake = {
    getBaseUrl: () => "http://backend:8000",
    writeUserMemory: write,
    searchMemory: search,
  } as unknown as TrinityClient;
  return createMemoryTools(fake, false);
}

describe("write_user_memory (ent#419)", () => {
  it("hands the agent the refusal's code, message and line, as a success:false result", async () => {
    const detail = {
      code: "memory_link_in_instruction",
      message: "Line 2 puts a link inside an instruction. Store the link as a fact about the person.",
      line: 2,
    };
    const tools = makeTools(async () => {
      throw new ApiError(422, JSON.stringify({ detail }));
    });
    const out = JSON.parse(
      await tools.writeUserMemory.execute({ execution_id: "exec-1", memory_text: "x", agent_name: "analyst" }, {})
    );
    assert.equal(out.success, false);
    assert.match(out.error, /422/);
    assert.match(out.error, /memory_link_in_instruction/);
    assert.match(out.error, /"line":2/);
    assert.match(out.error, /Store the link as a fact/);
  });

  it("passes a stored write through unchanged", async () => {
    const calls: Array<{ agent: string; body: Record<string, unknown> }> = [];
    const tools = makeTools(async (agent, body) => {
      calls.push({ agent, body });
      return { success: true, agent_name: agent, write_id: "w1" };
    });
    const out = JSON.parse(
      await tools.writeUserMemory.execute(
        { execution_id: "exec-1", memory_text: "Prefers tea.", agent_name: "analyst" },
        {}
      )
    );
    assert.equal(out.success, true);
    assert.deepEqual(calls, [{ agent: "analyst", body: { execution_id: "exec-1", memory_text: "Prefers tea." } }]);
  });

  it("names every refusal code and the margin in its description", () => {
    const { description } = makeTools(async () => ({})).writeUserMemory;
    for (const code of [
      "memory_instruction",
      "memory_tool_directive",
      "memory_link_in_instruction",
      "memory_prompt_marker",
    ]) {
      assert.ok(description.includes(code), `description does not name ${code}`);
    }
    assert.ok(description.includes("`│ `"), "description does not tell the agent to drop the margin");
  });
});

describe("search_memory (ent#1028)", () => {
  it("hands the agent a memory_no_subject refusal as a success:false result with the code and message", async () => {
    const detail = {
      code: "memory_no_subject",
      message: "This execution serves no one whose memory can be searched.",
      reason: "channel_not_one_to_one",
    };
    const tools = makeTools(async () => ({}), async () => {
      throw new ApiError(422, JSON.stringify({ detail }));
    });
    const out = JSON.parse(
      await tools.searchMemory.execute({ execution_id: "exec-1", query: "deadline", agent_name: "analyst" }, {})
    );
    assert.equal(out.success, false);
    assert.match(out.error, /422/);
    assert.match(out.error, /memory_no_subject/);
    assert.match(out.error, /serves no one/);
  });

  it("passes execution_id, query and k through for the resolved agent and returns the hits", async () => {
    const calls: Array<{ agent: string; body: Record<string, unknown> }> = [];
    const hits = [
      { id: "f1", text: "The Acme deadline is 3 December", line: "(2026-10-21, scheduled run) The Acme deadline is 3 December",
        source: "schedule", execution_id: "e9", created_at: "2026-10-21T00:00:00Z", updated_at: "2026-10-21T00:00:00Z",
        relevance: 0.71, score: 0.69 },
    ];
    const tools = makeTools(async () => ({}), async (agent, body) => {
      calls.push({ agent, body });
      return { agent_name: agent, scope: "agent", hits };
    });
    const out = JSON.parse(
      await tools.searchMemory.execute({ execution_id: "exec-1", query: "Acme deadline", k: 5, agent_name: "analyst" }, {})
    );
    assert.deepEqual(calls, [{ agent: "analyst", body: { execution_id: "exec-1", query: "Acme deadline", k: 5 } }]);
    assert.equal(out.scope, "agent");
    assert.deepEqual(out.hits, hits);
  });

  it("refuses without an agent name on a key that carries none", async () => {
    const tools = makeTools(async () => ({}));
    const out = JSON.parse(await tools.searchMemory.execute({ execution_id: "exec-1", query: "x" }, {}));
    assert.equal(out.success, false);
    assert.match(out.error, /agent name/i);
  });

  it("tells the agent the injected block is the top of the same store, and that a search is recorded", () => {
    const { description } = makeTools(async () => ({})).searchMemory;
    assert.ok(description.includes("What you remember from earlier work"), "description does not name the injected block");
    assert.match(description, /recorded/i, "description does not say a search is recorded");
    assert.match(description, /execution/i, "description does not say the scope comes off the execution");
    assert.ok(description.length <= 2048, `description is ${description.length} chars`);
  });
});
