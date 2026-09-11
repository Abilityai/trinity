/**
 * Tests for #914 — chat-timeout recovery picks the right execution row.
 *
 * Exercises `pickRecentMcpExecution` directly. The fetch-abort + lookup
 * integration is covered by live verification (see PR description); this
 * file pins the matcher's selection rules so a future edit can't silently
 * regress the rules used to attribute a `queued_timeout` receipt.
 *
 * Runner: built-in `node:test`. No new devDependency. Run via:
 *   node --import tsx --test src/client.test.ts
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import {
  pickRecentMcpExecution,
  extractIdempotencyExecutionId,
  CHAT_RECOVERY_TRIGGERS,
  TASK_RECOVERY_TRIGGERS,
  pickRecentFanOut,
  FAN_OUT_RECOVERY_TRIGGERS,
} from "./client.js";
import type { ScheduleExecution } from "./types.js";

const ISO_NOW = "2026-05-25T10:00:00.000Z";
const NOW_MS = Date.parse(ISO_NOW);

function exec(over: Partial<ScheduleExecution>): ScheduleExecution {
  return {
    id: over.id ?? "abc",
    schedule_id: over.schedule_id ?? "sched-1",
    agent_name: over.agent_name ?? "bdr-agent",
    status: over.status ?? "running",
    started_at: over.started_at ?? ISO_NOW,
    triggered_by: over.triggered_by ?? "mcp",
    message: over.message ?? "do thing",
    ...over,
  } as ScheduleExecution;
}

describe("#914 pickRecentMcpExecution", () => {
  it("picks the sole non-terminal MCP row inside the window", () => {
    const rows = [
      exec({ id: "terminal", status: "success", started_at: "2026-05-25T09:59:50.000Z" }),
      exec({ id: "stale", started_at: "2026-05-25T09:59:00.000Z" }), // outside window
      exec({ id: "mine", started_at: "2026-05-25T09:59:58.000Z" }),
    ];
    const picked = pickRecentMcpExecution(rows, { now: NOW_MS });
    assert.equal(picked?.id, "mine");
  });

  it("#2661 SUPERSEDES newest-wins: several indistinguishable rows yield nothing", () => {
    // This case asserted `newest` until #2661. That rule was only ever safe on
    // the queue-serialised /chat route, and reusing it for the concurrent /task
    // route would have attributed a peer's execution to the aborting caller.
    // Ambiguity now yields no receipt — identical to the pre-#914 behaviour for
    // that call, so nothing regresses; a WRONG id would have.
    const rows = [
      exec({ id: "old", started_at: "2026-05-25T09:59:50.000Z" }),
      exec({ id: "newer", started_at: "2026-05-25T09:59:58.000Z" }),
      exec({ id: "newest", started_at: "2026-05-25T09:59:59.500Z" }),
    ];
    assert.equal(pickRecentMcpExecution(rows, { now: NOW_MS }), undefined);
  });

  it("filters out terminal statuses (success / failed / cancelled / skipped)", () => {
    const rows = [
      exec({ id: "ok", status: "success" }),
      exec({ id: "bad", status: "failed" }),
      exec({ id: "killed", status: "cancelled" }),
      exec({ id: "skip", status: "skipped" }),
    ];
    assert.equal(pickRecentMcpExecution(rows, { now: NOW_MS }), undefined);
  });

  it("accepts both `mcp` and `agent` triggered_by", () => {
    const rows = [
      exec({ id: "from-agent", triggered_by: "agent", started_at: "2026-05-25T09:59:58.000Z" }),
    ];
    assert.equal(pickRecentMcpExecution(rows, { now: NOW_MS })?.id, "from-agent");
  });

  it("rejects non-MCP triggered_by (schedule / chat / task)", () => {
    const rows = [
      exec({ id: "sched", triggered_by: "schedule" }),
      exec({ id: "ui", triggered_by: "chat" }),
      exec({ id: "task", triggered_by: "task" }),
    ];
    assert.equal(pickRecentMcpExecution(rows, { now: NOW_MS }), undefined);
  });

  it("rejects rows older than the window", () => {
    const rows = [
      exec({ id: "ancient", started_at: "2026-05-25T09:59:00.000Z" }), // 60s ago
    ];
    assert.equal(pickRecentMcpExecution(rows, { now: NOW_MS }), undefined);
  });

  it("scopes by mcpKeyId when provided — mismatched key id rejected", () => {
    const rows = [
      exec({ id: "mine", source_mcp_key_id: "key-A" }),
      exec({ id: "theirs", source_mcp_key_id: "key-B" }),
    ];
    const picked = pickRecentMcpExecution(rows, { now: NOW_MS, mcpKeyId: "key-A" });
    assert.equal(picked?.id, "mine");
  });

  it("accepts rows with no source_mcp_key_id even when caller supplies one", () => {
    // Older execution rows / pre-AUDIT-001 backends may lack the field.
    // Better to return a row than no row — caller still gets a usable
    // execution_id and the live-verify path will confirm correctness.
    const rows = [
      exec({ id: "legacy" /* no source_mcp_key_id */ }),
    ];
    const picked = pickRecentMcpExecution(rows, { now: NOW_MS, mcpKeyId: "key-A" });
    assert.equal(picked?.id, "legacy");
  });

  it("returns undefined on empty input", () => {
    assert.equal(pickRecentMcpExecution([], { now: NOW_MS }), undefined);
  });

  it("honours a custom windowMs", () => {
    const rows = [
      exec({ id: "x", started_at: "2026-05-25T09:59:40.000Z" }), // 20s ago
    ];
    // Tight 10s window — should reject.
    assert.equal(pickRecentMcpExecution(rows, { now: NOW_MS, windowMs: 10_000 }), undefined);
    // 60s window — should accept.
    assert.equal(
      pickRecentMcpExecution(rows, { now: NOW_MS, windowMs: 60_000 })?.id,
      "x",
    );
  });
});

/**
 * #2661 — the sync `/task` route reuses this matcher, and `/task` is the
 * CONCURRENT route. Every filter #914 relied on is identical across one
 * caller's concurrent tasks, so the newest-wins rule had to go.
 */
describe("#2661 pickRecentMcpExecution — attribution under concurrency", () => {
  it("returns undefined when two candidates survive, instead of guessing the newest", () => {
    // The regression this pins: three parallel tasks from ONE caller to ONE
    // agent share triggered_by, source_mcp_key_id and the window. Newest-wins
    // handed every aborting caller the same id — a well-formed FOREIGN result.
    const rows = [
      exec({ id: "task-a", message: "task A", started_at: "2026-05-25T09:59:57.000Z" }),
      exec({ id: "task-b", message: "task B", started_at: "2026-05-25T09:59:58.000Z" }),
      exec({ id: "task-c", message: "task C", started_at: "2026-05-25T09:59:59.000Z" }),
    ];
    assert.equal(
      pickRecentMcpExecution(rows, { now: NOW_MS, triggers: TASK_RECOVERY_TRIGGERS }),
      undefined,
    );
  });

  it("the message discriminator resolves that ambiguity to the caller's own row", () => {
    const rows = [
      exec({ id: "task-a", message: "task A", started_at: "2026-05-25T09:59:57.000Z" }),
      exec({ id: "task-b", message: "task B", started_at: "2026-05-25T09:59:58.000Z" }),
      exec({ id: "task-c", message: "task C", started_at: "2026-05-25T09:59:59.000Z" }),
    ];
    const picked = pickRecentMcpExecution(rows, {
      now: NOW_MS,
      triggers: TASK_RECOVERY_TRIGGERS,
      message: "task A",
    });
    // Deliberately the OLDEST row — proves selection is by identity, not recency.
    assert.equal(picked?.id, "task-a");
  });

  it("a non-matching message yields no receipt rather than a neighbouring row", () => {
    const rows = [exec({ id: "someone-else", message: "their task" })];
    assert.equal(
      pickRecentMcpExecution(rows, { now: NOW_MS, message: "my task" }),
      undefined,
    );
  });

  it("accepts self_task ONLY under the task trigger set", () => {
    const rows = [exec({ id: "self", triggered_by: "self_task" })];
    // /task sees SELF-EXEC-001 rows...
    assert.equal(
      pickRecentMcpExecution(rows, { now: NOW_MS, triggers: TASK_RECOVERY_TRIGGERS })?.id,
      "self",
    );
    // ...and /chat must NOT: it is queue-serialised, so a concurrently-running
    // parallel self-task row would otherwise be attributed to a queued chat.
    assert.equal(
      pickRecentMcpExecution(rows, { now: NOW_MS, triggers: CHAT_RECOVERY_TRIGGERS }),
      undefined,
    );
    // Default (no triggers passed) must stay the /chat set.
    assert.equal(pickRecentMcpExecution(rows, { now: NOW_MS }), undefined);
  });

  it("covers pending_retry, a real non-terminal status the original set omitted", () => {
    const rows = [exec({ id: "retrying", status: "pending_retry" as never })];
    assert.equal(pickRecentMcpExecution(rows, { now: NOW_MS })?.id, "retrying");
  });

  it("a window derived from a RAISED timeout still matches (the #2661 knob fix)", () => {
    // Operator sets MCP_CHAT_TIMEOUT_MS=45000 as the docs invite. The row is
    // then ~45s old at abort. Under the old fixed 30s window every receipt
    // silently degraded to the no-match throw — the knob disabled the feature.
    const rows = [exec({ id: "slow", started_at: "2026-05-25T09:59:15.000Z" })]; // 45s ago
    assert.equal(pickRecentMcpExecution(rows, { now: NOW_MS }), undefined); // old behaviour
    assert.equal(
      pickRecentMcpExecution(rows, { now: NOW_MS, windowMs: 45_000 + 10_000 })?.id,
      "slow",
    );
  });
});

describe("#2661 extractIdempotencyExecutionId", () => {
  it("reads FastAPI's detail-wrapped 409 body", () => {
    const body = JSON.stringify({
      detail: {
        error: "request_in_progress",
        message: "A request with this Idempotency-Key is still being processed.",
        execution_id: "exec-123",
      },
    });
    assert.equal(extractIdempotencyExecutionId(body), "exec-123");
  });

  it("reads a bare (unwrapped) body too", () => {
    assert.equal(
      extractIdempotencyExecutionId(JSON.stringify({ execution_id: "exec-9" })),
      "exec-9",
    );
  });

  it("never throws on a non-JSON or id-less body — it runs on an error path", () => {
    assert.equal(extractIdempotencyExecutionId("502 Bad Gateway"), undefined);
    assert.equal(extractIdempotencyExecutionId("{}"), undefined);
    assert.equal(extractIdempotencyExecutionId(JSON.stringify({ detail: "plain string" })), undefined);
    assert.equal(extractIdempotencyExecutionId(JSON.stringify({ execution_id: "" })), undefined);
  });
});

// ---------------------------------------------------------------------------
// #2670 — the fan-out batch matcher
// ---------------------------------------------------------------------------
//
// The third route of the #914 class, and the one where the ambiguity rule has
// to be restated rather than reused. `/task` refuses when more than one ROW
// survives, because it cannot tell which is the caller's. A fan-out stamps ONE
// `fan_out_id` on all N of its rows, so N survivors is the expected shape and
// the unit that must be unambiguous is the BATCH.

function fanExec(over: Partial<ScheduleExecution>): ScheduleExecution {
  return exec({ triggered_by: "fan_out", fan_out_id: "fo_A", ...over });
}

describe("#2670 pickRecentFanOut", () => {
  it("returns the batch when every row of it survives", () => {
    const rows = [
      fanExec({ id: "e1", message: "task one" }),
      fanExec({ id: "e2", message: "task two" }),
      fanExec({ id: "e3", message: "task three" }),
    ];
    const picked = pickRecentFanOut(rows, { now: NOW_MS, messages: ["task one", "task two", "task three"] });
    assert.equal(picked?.fan_out_id, "fo_A");
    assert.deepEqual(picked?.execution_ids.sort(), ["e1", "e2", "e3"]);
  });

  it("is not confused by N rows — that is the normal shape, not ambiguity", () => {
    // The rule #2661 needed on /task would refuse this outright.
    const rows = Array.from({ length: 10 }, (_, i) =>
      fanExec({ id: `e${i}`, message: `task ${i}` }));
    assert.equal(pickRecentFanOut(rows, { now: NOW_MS })?.fan_out_id, "fo_A");
  });

  it("refuses when TWO distinct batches survive", () => {
    const rows = [
      fanExec({ id: "e1", fan_out_id: "fo_A" }),
      fanExec({ id: "e2", fan_out_id: "fo_B" }),
    ];
    // A wrong batch id is worse than none: the caller would poll a foreign
    // batch and act on a well-formed result that is not theirs.
    assert.equal(pickRecentFanOut(rows, { now: NOW_MS }), undefined);
  });

  it("finds the batch even when part of it has already finished", () => {
    // By the time the gateway gives up, a batch is normally a MIX. Filtering to
    // non-terminal rows — which /chat and /task do — would drop exactly the
    // batches furthest along.
    const rows = [
      fanExec({ id: "e1", status: "success" }),
      fanExec({ id: "e2", status: "running" }),
      fanExec({ id: "e3", status: "failed" }),
    ];
    const picked = pickRecentFanOut(rows, { now: NOW_MS });
    assert.equal(picked?.fan_out_id, "fo_A");
    assert.equal(picked?.execution_ids.length, 3);
  });

  it("ignores rows from other routes even when they carry a fan_out_id", () => {
    const rows = [
      fanExec({ id: "mine" }),
      exec({ id: "other", triggered_by: "mcp", fan_out_id: "fo_Z" }),
    ];
    assert.equal(pickRecentFanOut(rows, { now: NOW_MS })?.fan_out_id, "fo_A");
  });

  it("ignores rows with no fan_out_id at all", () => {
    const rows = [exec({ id: "x", triggered_by: "fan_out" })];
    assert.equal(pickRecentFanOut(rows, { now: NOW_MS }), undefined);
  });

  it("scopes to the calling key when one is supplied", () => {
    const rows = [
      fanExec({ id: "mine", fan_out_id: "fo_A", source_mcp_key_id: "key-A" }),
      fanExec({ id: "theirs", fan_out_id: "fo_B", source_mcp_key_id: "key-B" }),
    ];
    assert.equal(
      pickRecentFanOut(rows, { now: NOW_MS, mcpKeyId: "key-A" })?.fan_out_id,
      "fo_A",
    );
  });

  it("lets a row with no key id through — older backends have none", () => {
    const rows = [fanExec({ id: "e1", source_mcp_key_id: undefined })];
    assert.equal(pickRecentFanOut(rows, { now: NOW_MS, mcpKeyId: "key-A" })?.fan_out_id, "fo_A");
  });

  it("uses the call's own messages as the discriminator", () => {
    // A fan-out has N messages, so this filter is STRONGER here than on a
    // single-message route: two batches that share no message cannot collide.
    const rows = [
      fanExec({ id: "mine", fan_out_id: "fo_A", message: "summarise Q1" }),
      fanExec({ id: "theirs", fan_out_id: "fo_B", message: "unrelated work" }),
    ];
    assert.equal(
      pickRecentFanOut(rows, { now: NOW_MS, messages: ["summarise Q1", "summarise Q2"] })?.fan_out_id,
      "fo_A",
    );
  });

  it("drops rows outside the window", () => {
    const rows = [fanExec({ id: "old", started_at: new Date(NOW_MS - 120_000).toISOString() })];
    assert.equal(pickRecentFanOut(rows, { now: NOW_MS, windowMs: 35_000 }), undefined);
    assert.equal(pickRecentFanOut(rows, { now: NOW_MS, windowMs: 300_000 })?.fan_out_id, "fo_A");
  });

  it("returns nothing for an empty page rather than throwing", () => {
    assert.equal(pickRecentFanOut([], { now: NOW_MS }), undefined);
  });

  it("treats an unparseable started_at as out of window", () => {
    const rows = [fanExec({ id: "bad", started_at: "not a date" })];
    assert.equal(pickRecentFanOut(rows, { now: NOW_MS }), undefined);
  });

  it("declares its own trigger set rather than borrowing /chat's or /task's", () => {
    // The per-call-site rule #2661 wrote down: a fan-out row is `fan_out` and
    // nothing else, and widening this would let another route's row name a batch.
    assert.deepEqual([...FAN_OUT_RECOVERY_TRIGGERS], ["fan_out"]);
    assert.ok(![...CHAT_RECOVERY_TRIGGERS].includes("fan_out" as never));
    assert.ok(![...TASK_RECOVERY_TRIGGERS].includes("fan_out" as never));
  });
});
