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
import { afterEach, describe, it, mock } from "node:test";
import { strict as assert } from "node:assert";
import { createMemoryTools } from "./memory.js";
import { ApiError, TrinityClient } from "../client.js";

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
      "memory_turn_not_live",
      "memory_turn_ambiguous",
    ]) {
      assert.ok(description.includes(code), `description does not name ${code}`);
    }
    assert.ok(description.includes("`│ `"), "description does not tell the agent to drop the margin");
  });

  // The sentence that steers an agent off shared memory. #4121's trim to the
  // 2,048-character cap dropped it with every test green, so a cut now fails here.
  it("keeps the warning that shared memory leaks one person's data to everyone", () => {
    const { description } = makeTools(async () => ({})).writeUserMemory;
    assert.match(description, /auto-memory/);
    assert.match(description, /shared across ALL users/);
    assert.match(description, /leak one user's data to everyone else/);
  });

  // #4121: on a shared agent a live neighbour refuses the write, and the turn can
  // end before a retry works, so the agent must say the note was lost.
  it("tells the agent a refused write was not saved, and carries the turn refusal's code", async () => {
    const { description } = makeTools(async () => ({})).writeUserMemory;
    assert.match(description, /not saved/);
    const detail = {
      code: "memory_turn_ambiguous",
      message: "Another turn for someone else is running on this agent right now. Nothing was saved.",
    };
    const tools = makeTools(async () => {
      throw new ApiError(422, JSON.stringify({ detail }));
    });
    const out = JSON.parse(
      await tools.writeUserMemory.execute({ execution_id: "exec-1", memory_text: "x", agent_name: "analyst" }, {})
    );
    assert.equal(out.success, false);
    assert.match(out.error, /memory_turn_ambiguous/);
    assert.match(out.error, /Nothing was saved/);
  });

  it("offers no user-key path: with no agent key it names the agent's own key, never agent_name", async () => {
    let called = false;
    const tools = makeTools(async () => {
      called = true;
      return {};
    });
    const out = JSON.parse(await tools.writeUserMemory.execute({ execution_id: "exec-1", memory_text: "x" }, {}));
    assert.equal(out.success, false);
    assert.equal(called, false);
    assert.match(out.error, /agent's own MCP key/);
    assert.doesNotMatch(out.error, /Pass agent_name/);
  });
});

// #4121: both doors refuse a finished run, and a resumed session copies an id from
// its own history, so a typed id alone would lose a live turn's note. With a key on
// the request the real client is built per call, hence the prototype stub.
describe("the turn id both memory doors send (#4121)", () => {
  const session = (executionId?: string) => ({
    session: { mcpApiKey: "trinity_mcp_test", scope: "agent", agentName: "analyst", executionId },
  });
  const keyed = () => createMemoryTools({ getBaseUrl: () => "http://backend:8000" } as unknown as TrinityClient, true);

  afterEach(() => mock.restoreAll());

  it("write_user_memory sends the platform's header, not a stale typed id", async () => {
    const write = mock.method(TrinityClient.prototype, "writeUserMemory", async () => ({ success: true }) as never);
    await keyed().writeUserMemory.execute({ execution_id: "stale-from-history", memory_text: "x" }, session("the-live-turn"));
    assert.equal(write.mock.calls.length, 1);
    assert.deepEqual(write.mock.calls[0].arguments, ["analyst", { execution_id: "the-live-turn", memory_text: "x" }]);
  });

  it("search_memory sends the platform's header, not a stale typed id", async () => {
    const search = mock.method(TrinityClient.prototype, "searchMemory", async () => ({ hits: [] }) as never);
    await keyed().searchMemory.execute({ execution_id: "stale-from-history", query: "q" }, session("the-live-turn"));
    assert.deepEqual(search.mock.calls[0].arguments, ["analyst", { execution_id: "the-live-turn", query: "q" }]);
  });

  it("an interactive session's `manual` header is what goes out, so it cannot name someone's live turn", async () => {
    const write = mock.method(TrinityClient.prototype, "writeUserMemory", async () => ({ success: true }) as never);
    await keyed().writeUserMemory.execute({ execution_id: "someones-live-turn", memory_text: "x" }, session("manual"));
    assert.deepEqual(write.mock.calls[0].arguments, ["analyst", { execution_id: "manual", memory_text: "x" }]);
  });

  it("an agent image without the header falls back to the typed id", async () => {
    const write = mock.method(TrinityClient.prototype, "writeUserMemory", async () => ({ success: true }) as never);
    await keyed().writeUserMemory.execute({ execution_id: "typed", memory_text: "x" }, session());
    assert.deepEqual(write.mock.calls[0].arguments, ["analyst", { execution_id: "typed", memory_text: "x" }]);
  });

  it("a model typing `manual` with no header sends nothing and says so", async () => {
    const write = mock.method(TrinityClient.prototype, "writeUserMemory", async () => ({ success: true }) as never);
    const out = JSON.parse(await keyed().writeUserMemory.execute({ execution_id: "manual", memory_text: "x" }, session()));
    assert.equal(out.success, false);
    assert.match(out.error, /nothing was saved/);
    assert.equal(write.mock.calls.length, 0);
  });

  it("search_memory does the same with a typed `manual` and no header", async () => {
    const search = mock.method(TrinityClient.prototype, "searchMemory", async () => ({ hits: [] }) as never);
    const out = JSON.parse(await keyed().searchMemory.execute({ execution_id: "manual", query: "q" }, session()));
    assert.equal(out.success, false);
    assert.match(out.error, /No execution_id/);
    assert.equal(search.mock.calls.length, 0);
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
