/**
 * #3944 — an identical fan-out from a LATER turn is a new fan-out.
 *
 * `fan_out` derived its Idempotency-Key from caller, target, "fan_out", model
 * and the tasks. The caller's turn was not part of it, so the same batch sent
 * from a different turn within 24 h replayed the FIRST turn's `FanOutResult`:
 * the second turn's work never ran. #3296 / #3920 fixed this for
 * `chat_with_agent`; this is the same fix for `fan_out`:
 *   - same tasks, same turn  → same key (a transport retry still replays);
 *   - same tasks, other turn → a different key (a new fan-out);
 *   - a scheduler retry      → attempt 1's key (`delegationTurnScope`), so a
 *     retried scheduled run does not dispatch its batch a second time;
 *   - no turn (`manual`, user keys) → the key is byte-identical to before.
 *
 * Runner: built-in node:test → `node --import tsx --test src/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createChatTools, deriveMcpIdempotencyKey } from "./tools/chat.js";
import type { TrinityClient } from "./client.js";

const TASKS = [
  { id: "a", message: "summarise yesterday's incidents" },
  { id: "b", message: "list open alerts" },
];

const ROWS: Record<string, any> = {
  "exec-attempt-1": { id: "exec-attempt-1" },
  "exec-attempt-2": { id: "exec-attempt-2", retry_of_execution_id: "exec-attempt-1" },
  "exec-other-run": { id: "exec-other-run" },
};

function makeFanOut(keys: (string | undefined)[], lookups: string[] = []) {
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    getExecution: (async (_agent: string, id: string) => {
      lookups.push(id);
      if (!(id in ROWS)) throw new Error("404");
      return ROWS[id];
    }) as any,
    fanOut: (async (
      _name: string, _tasks: unknown, _options: unknown,
      _source?: string, _key?: unknown, idempotencyKey?: string,
    ) => {
      keys.push(idempotencyKey);
      return { fan_out_id: "fo_1", status: "completed", results: [] } as any;
    }) as any,
  };
  return createChatTools(fake as unknown as TrinityClient, false, false).fanOut;
}

const turn = (executionId?: string) => ({
  session: { scope: "agent", agentName: "caller", userId: "u1", keyId: "k1", keyName: "kn", executionId } as any,
});

const ARGS = { agent_name: "target", tasks: TASKS };

// What the key was before #3944, for this tool built without key mode (no
// authenticated caller, so the first part is undefined).
const legacyKey = (extra: string[] = []) =>
  deriveMcpIdempotencyKey([undefined, "target", "fan_out", undefined, JSON.stringify(TASKS), ...extra]);

describe("#3944 the fan_out key is scoped to the caller's turn", () => {
  it("the same tasks from the SAME turn keep one key (a retry still replays)", async () => {
    const keys: (string | undefined)[] = [];
    const tool = makeFanOut(keys);
    await tool.execute(ARGS as any, turn("exec-other-run"));
    await tool.execute(ARGS as any, turn("exec-other-run"));
    assert.ok(keys[0]);
    assert.equal(keys[0], keys[1]);
  });

  it("the same tasks from a DIFFERENT turn get a different key (a new fan-out runs)", async () => {
    const keys: (string | undefined)[] = [];
    const tool = makeFanOut(keys);
    await tool.execute(ARGS as any, turn("exec-attempt-1"));
    await tool.execute(ARGS as any, turn("exec-other-run"));
    assert.notEqual(keys[0], keys[1]);
  });

  it("a scheduler retry derives attempt 1's key", async () => {
    const keys: (string | undefined)[] = [];
    const tool = makeFanOut(keys);
    await tool.execute(ARGS as any, turn("exec-attempt-1"));
    await tool.execute(ARGS as any, turn("exec-attempt-2"));
    assert.equal(keys[0], keys[1]);
    assert.equal(keys[0], legacyKey(["turn:exec-attempt-1"]));
  });

  it("with no turn the key is byte-identical to the pre-#3944 derivation", async () => {
    for (const executionId of [undefined, "manual"]) {
      const keys: (string | undefined)[] = [];
      const lookups: string[] = [];
      await makeFanOut(keys, lookups).execute(ARGS as any, turn(executionId));
      assert.equal(keys[0], legacyKey(), `executionId=${executionId}`);
      assert.deepEqual(lookups, [], "no turn, so no retry-chain read");
    }
  });

  it("async mode keeps its own key, turn included", async () => {
    const keys: (string | undefined)[] = [];
    const tool = makeFanOut(keys);
    await tool.execute({ ...ARGS, async_mode: true } as any, turn("exec-other-run"));
    await tool.execute(ARGS as any, turn("exec-other-run"));
    assert.notEqual(keys[0], keys[1]);
    assert.equal(keys[0], legacyKey(["async", "turn:exec-other-run"]));
  });

  it("an unreadable row scopes to the turn itself, and the fan-out still goes out", async () => {
    const keys: (string | undefined)[] = [];
    await makeFanOut(keys).execute(ARGS as any, turn("exec-unknown"));
    assert.equal(keys.length, 1);
    assert.equal(keys[0], legacyKey(["turn:exec-unknown"]));
  });
});
