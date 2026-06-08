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

import { pickRecentMcpExecution } from "./client.js";
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
  it("picks newest non-terminal MCP row inside the window", () => {
    const rows = [
      exec({ id: "old", started_at: "2026-05-25T09:59:50.000Z" }),
      exec({ id: "newer", started_at: "2026-05-25T09:59:58.000Z" }),
      exec({ id: "newest", started_at: "2026-05-25T09:59:59.500Z" }),
    ];
    const picked = pickRecentMcpExecution(rows, { now: NOW_MS });
    assert.equal(picked?.id, "newest");
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
