/**
 * #4281 (D9) — trigger_agent_schedule answers "already running" only for the
 * scheduler's running-lock 409 (#1968). Every other 409 — the backend's
 * `schedule_runs_as_shared_person`, or one added later — is a refusal the
 * calling agent gets with the backend's code and message. Telling it to poll
 * list_recent_executions for a run that was never started wastes its turns
 * and hides that it was refused.
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createScheduleTools } from "./schedules.js";
import { ApiError, type TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

// The bodies the backend sends (`routers/schedules.py::trigger_schedule`).
const LOCK_BODY = JSON.stringify({ detail: "Schedule is already executing" });
const SHARED_PERSON_MESSAGE =
  "This schedule's runs have the access of the person who created it, who is not the " +
  "agent's owner or an admin. A key cannot run it now with the owner's access; a person " +
  "at the agent's owner tier can, or can take it over.";
const SHARED_PERSON_BODY = JSON.stringify({
  detail: { code: "schedule_runs_as_shared_person", message: SHARED_PERSON_MESSAGE },
});

function fakeConflict(body: string): TrinityClient {
  return {
    getBaseUrl: () => "http://localhost:8000",
    triggerAgentSchedule: async () => {
      throw new ApiError(409, body);
    },
  } as unknown as TrinityClient;
}

const ctx = {
  session: { scope: "agent", agentName: "alpha", userId: "owner", keyId: "k", keyName: "kn" } as unknown as McpAuthContext,
};

type Exec = (p: Record<string, unknown>, c?: { session?: McpAuthContext }) => Promise<string>;

async function trigger(body: string): Promise<Record<string, unknown>> {
  const exec = createScheduleTools(fakeConflict(body), false).triggerAgentSchedule.execute as Exec;
  return JSON.parse(await exec({ agent_name: "alpha", schedule_id: "s1" }, ctx)) as Record<string, unknown>;
}

function assertRefusedNotPolling(out: Record<string, unknown>): void {
  assert.equal(out.status, "refused");
  assert.equal(out.retryable, false);
  assert.equal(out.schedule_id, "s1");
  assert.equal(out.agent_name, "alpha");
  assert.doesNotMatch(JSON.stringify(out), /list_recent_executions|in flight/i);
}

describe("#4281 trigger_agent_schedule: which 409 is 'already running'", () => {
  it("the running-lock 409 is still already_running (#1968)", async () => {
    const out = await trigger(LOCK_BODY);
    assert.equal(out.status, "already_running");
    assert.match(String(out.message), /list_recent_executions/);
  });

  it("schedule_runs_as_shared_person is a refusal carrying the backend's code and message", async () => {
    const out = await trigger(SHARED_PERSON_BODY);
    assertRefusedNotPolling(out);
    assert.equal(out.code, "schedule_runs_as_shared_person");
    assert.equal(out.message, SHARED_PERSON_MESSAGE);
  });

  it("a coded 409 the tool has never heard of is a refusal with that code", async () => {
    const out = await trigger(JSON.stringify({ detail: { code: "some_future_conflict", message: "Not now." } }));
    assertRefusedNotPolling(out);
    assert.equal(out.code, "some_future_conflict");
    assert.equal(out.message, "Not now.");
  });

  it("a plain-string 409 that is not the lock's is a refusal with that text, no code", async () => {
    const out = await trigger(JSON.stringify({ detail: "Schedule is paused for review" }));
    assertRefusedNotPolling(out);
    assert.equal(out.code, null);
    assert.equal(out.message, "Schedule is paused for review");
  });

  // Review: the lock is the backend's exact envelope `{"detail": "<lock text>"}`
  // and nothing else. The same words in any other shape are not the lock.
  it("a coded 409 whose message happens to be the lock text is still a refusal", async () => {
    const out = await trigger(JSON.stringify({ detail: { code: "some_future_conflict", message: "Schedule is already executing" } }));
    assertRefusedNotPolling(out);
    assert.equal(out.code, "some_future_conflict");
  });

  it("the lock text outside the backend's envelope is not the lock", async () => {
    for (const body of [
      "Schedule is already executing",
      JSON.stringify({ detail: { message: "Schedule is already executing" } }),
    ]) {
      const out = await trigger(body);
      assertRefusedNotPolling(out);
      assert.equal(out.code, null);
    }
  });

  it("a 409 whose body is not JSON is a refusal with the raw body, not already_running", async () => {
    const out = await trigger("upstream conflict");
    assertRefusedNotPolling(out);
    assert.equal(out.code, null);
    assert.equal(out.message, "upstream conflict");
  });
});
