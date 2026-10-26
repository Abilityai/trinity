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

function makeTools(write: (agent: string, body: Record<string, unknown>) => Promise<unknown>) {
  const fake = {
    getBaseUrl: () => "http://backend:8000",
    writeUserMemory: write,
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
