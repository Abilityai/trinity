/**
 * abilityai/trinity-enterprise#1105 — `project_id` on chat_with_agent and
 * fan_out is forwarded to the backend only when set, and changes the
 * idempotency key only when set; get_execution_result carries the backend's
 * `project_id` only when the backend returned one.
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createChatTools } from "./chat.js";
import { createExecutionTools } from "./executions.js";
import type { TrinityClient } from "../client.js";

type Call = { kind: string; args: unknown[] };

function makeFake(execution: Record<string, unknown> = {}) {
  const calls: Call[] = [];
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    chat: (async (...args: unknown[]) => {
      calls.push({ kind: "chat", args });
      return { response: "ok", execution_id: "e1" };
    }) as any,
    task: (async (...args: unknown[]) => {
      calls.push({ kind: "task", args });
      return { response: "ok", execution_id: "e2" };
    }) as any,
    fanOut: (async (...args: unknown[]) => {
      calls.push({ kind: "fanOut", args });
      return { fan_out_id: "fo_1", status: "completed", total: 1, completed: 1, failed: 0, results: [] };
    }) as any,
    getExecution: (async () => ({
      id: "e1", agent_name: "bravo", status: "success", message: "m", started_at: "2026-11-12T00:00:00Z",
      triggered_by: "agent", ...execution,
    })) as any,
  };
  return { fake: fake as TrinityClient, calls };
}

const NO_SESSION = {};

describe("ent#1105 chat_with_agent project_id", () => {
  it("sequential: passes project_id to the /chat call when set", async () => {
    const { fake, calls } = makeFake();
    const tools = createChatTools(fake, false, false);
    await tools.chatWithAgent.execute({ agent_name: "bravo", message: "hi", project_id: "p1" } as any, NO_SESSION);
    const chat = calls.find((c) => c.kind === "chat")!;
    assert.equal(chat.args[6], "p1");
  });

  it("sequential: sends no project_id when unset, and the key is unchanged by its absence", async () => {
    const a = makeFake();
    await createChatTools(a.fake, false, false).chatWithAgent.execute({ agent_name: "bravo", message: "hi" } as any, NO_SESSION);
    const b = makeFake();
    await createChatTools(b.fake, false, false).chatWithAgent.execute(
      { agent_name: "bravo", message: "hi", project_id: "p1" } as any, NO_SESSION);
    const chatA = a.calls.find((c) => c.kind === "chat")!;
    const chatB = b.calls.find((c) => c.kind === "chat")!;
    assert.equal(chatA.args[6], undefined);
    assert.notEqual(chatA.args[4], chatB.args[4], "a named project is a different dispatch");
  });

  it("parallel: project_id rides in the /task options only when set", async () => {
    const a = makeFake();
    await createChatTools(a.fake, false, false).chatWithAgent.execute(
      { agent_name: "bravo", message: "hi", parallel: true, project_id: "p1" } as any, NO_SESSION);
    const optsA = a.calls.find((c) => c.kind === "task")!.args[2] as Record<string, unknown>;
    assert.equal(optsA.project_id, "p1");

    const b = makeFake();
    await createChatTools(b.fake, false, false).chatWithAgent.execute(
      { agent_name: "bravo", message: "hi", parallel: true } as any, NO_SESSION);
    const optsB = b.calls.find((c) => c.kind === "task")!.args[2] as Record<string, unknown>;
    assert.equal("project_id" in optsB, false);
  });

  it("the schema bounds project_id to 1..64 characters", () => {
    const { fake } = makeFake();
    const schema = createChatTools(fake, false, false).chatWithAgent.parameters;
    assert.equal(schema.safeParse({ agent_name: "a", message: "m", project_id: "" }).success, false);
    assert.equal(schema.safeParse({ agent_name: "a", message: "m", project_id: "x".repeat(65) }).success, false);
    assert.equal(schema.safeParse({ agent_name: "a", message: "m", project_id: "p1" }).success, true);
  });
});

describe("ent#1105 fan_out project_id", () => {
  const TASKS = [{ id: "t1", message: "go" }];

  it("forwards project_id in the batch options only when set", async () => {
    const a = makeFake();
    await createChatTools(a.fake, false, false).fanOut.execute(
      { agent_name: "bravo", tasks: TASKS, project_id: "p1" } as any, NO_SESSION);
    const callA = a.calls.find((c) => c.kind === "fanOut")!;
    assert.equal((callA.args[2] as Record<string, unknown>).project_id, "p1");

    const b = makeFake();
    await createChatTools(b.fake, false, false).fanOut.execute({ agent_name: "bravo", tasks: TASKS } as any, NO_SESSION);
    const callB = b.calls.find((c) => c.kind === "fanOut")!;
    assert.equal("project_id" in (callB.args[2] as Record<string, unknown>), false);
    assert.notEqual(callA.args[5], callB.args[5], "a named project is a different batch");
  });
});

describe("ent#1105 get_execution_result project_id", () => {
  it("carries project_id when the backend returned one", async () => {
    const { fake } = makeFake({ project_id: "p1" });
    const out = JSON.parse(await (createExecutionTools(fake, false).getExecutionResult.execute as any)(
      { agent_name: "bravo", execution_id: "e1" }, NO_SESSION));
    assert.equal(out.project_id, "p1");
  });

  it("omits project_id when the backend returned none", async () => {
    const { fake } = makeFake({ project_id: null });
    const out = JSON.parse(await (createExecutionTools(fake, false).getExecutionResult.execute as any)(
      { agent_name: "bravo", execution_id: "e1" }, NO_SESSION));
    assert.equal("project_id" in out, false);
  });
});
