/**
 * trinity-enterprise#707 — sync health on the monitoring MCP tools.
 *
 * Pins:
 *   - get_fleet_health passes each agent's `sync` block (or null), the fleet
 *     `sync_summary`, and the `issues` strings (the red-sync recommendation
 *     lives there) through, next to the pre-#707 `issues_count`.
 *   - get_fleet_sync_audit exists, reads GET /api/fleet/sync-audit through the
 *     caller's own key, and returns the backend's audit unchanged.
 *   - its access-policy row is `none` (no agent target; the backend scopes the
 *     rows to the key owner's accessible agents — decision D12).
 *
 * Drives the real tool execute() with a fake TrinityClient (requireApiKey=false
 * → getClient() returns the fake directly, the git.test.ts seam).
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createMonitoringTools } from "./monitoring.js";
import { TOOL_ACCESS_POLICY } from "../access.js";
import type { TrinityClient } from "../client.js";

const RED_SYNC = {
  binding: "agent",
  auto_sync_enabled: false,
  ahead: 7,
  behind: 0,
  dirty_files: 12,
  last_successful_push_at: "2026-09-26T09:30:00.000000Z",
  divergence_age_s: 93600,
  state: "red",
  reason: "diverged 0 behind / 7 ahead for 26h",
  recommendation: "enable auto-sync",
  frozen: true,
};

const SYNC_SUMMARY = {
  git_bound: 1, diverged: 1, frozen: 1, auto_sync_off: 1, dirty: 1, red: 1, yellow: 0,
};

const FLEET = {
  enabled: true,
  last_check_at: "2026-09-27T10:00:00Z",
  summary: { total_agents: 2, healthy: 1, degraded: 0, unhealthy: 0, critical: 0, unknown: 1 },
  agents: [
    {
      name: "alpha",
      status: "healthy",
      docker_status: "running",
      network_reachable: true,
      last_check_at: "2026-09-27T10:00:00Z",
      issues: ["sync: diverged 0 behind / 7 ahead for 26h — enable auto-sync"],
      sync: RED_SYNC,
    },
    { name: "bravo", status: "unknown", issues: ["No health check data"], sync: null },
  ],
  sync_summary: SYNC_SUMMARY,
};

const AUDIT = {
  agents: [
    {
      name: "alpha",
      branch: "trinity/alpha/x",
      unpushed_commits: 7,
      dirty_tree: true,
      duplicate_binding: false,
      ahead: 7,
      behind: 0,
      dirty_files: 12,
      state: "red",
      reason: "diverged 0 behind / 7 ahead for 26h",
      recommendation: "enable auto-sync",
      frozen: true,
    },
  ],
  summary: { total: 1, in_sync: 0, ahead: 1, dirty: 1, duplicate_bindings: 0, diverged: 1, frozen: 1, auto_sync_off: 1, red: 1 },
};

function tools(overrides: Partial<TrinityClient> = {}, calls: string[] = []) {
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    getFleetHealth: async () => {
      calls.push("getFleetHealth");
      return FLEET as any;
    },
    getFleetSyncAudit: async () => {
      calls.push("getFleetSyncAudit");
      return AUDIT as any;
    },
    ...overrides,
  };
  return createMonitoringTools(fake as unknown as TrinityClient, false);
}

describe("ent#707 get_fleet_health carries sync health", () => {
  it("passes the sync block, the issues and the fleet totals through", async () => {
    const out = JSON.parse(await (tools().getFleetHealth.execute as any)({}, {}));
    assert.equal(out.success, true);
    assert.deepEqual(out.sync_summary, SYNC_SUMMARY);
    const alpha = out.agents.find((a: any) => a.name === "alpha");
    assert.deepEqual(alpha.sync, RED_SYNC);
    assert.deepEqual(alpha.issues, [
      "sync: diverged 0 behind / 7 ahead for 26h — enable auto-sync",
    ]);
    assert.equal(alpha.issues_count, 1); // the pre-#707 key is kept
    assert.equal(alpha.status, "healthy"); // sync never moves status
  });

  it("an agent with no git binding says sync: null, not an absent key", async () => {
    const out = JSON.parse(await (tools().getFleetHealth.execute as any)({}, {}));
    const bravo = out.agents.find((a: any) => a.name === "bravo");
    assert.ok(Object.prototype.hasOwnProperty.call(bravo, "sync"));
    assert.equal(bravo.sync, null);
  });

  it("an older backend without the fields yields null, never undefined", async () => {
    const legacy = {
      ...FLEET,
      sync_summary: undefined,
      agents: [{ name: "alpha", status: "healthy", issues: [] }],
    };
    const out = JSON.parse(
      await (tools({ getFleetHealth: async () => legacy as any }).getFleetHealth.execute as any)({}, {}),
    );
    assert.equal(out.sync_summary, null);
    assert.equal(out.agents[0].sync, null);
    assert.deepEqual(out.agents[0].issues, []);
  });
});

describe("ent#707 get_fleet_sync_audit", () => {
  it("is registered in the monitoring group with no parameters", () => {
    const tool = (tools() as any).getFleetSyncAudit;
    assert.ok(tool, "getFleetSyncAudit is missing from createMonitoringTools");
    assert.equal(tool.name, "get_fleet_sync_audit");
    assert.deepEqual(Object.keys(tool.parameters.shape), []);
  });

  it("returns the backend audit unchanged", async () => {
    const calls: string[] = [];
    const out = JSON.parse(await ((tools({}, calls) as any).getFleetSyncAudit.execute)({}, {}));
    assert.deepEqual(calls, ["getFleetSyncAudit"]);
    assert.equal(out.success, true);
    assert.deepEqual(out.agents, AUDIT.agents);
    assert.deepEqual(out.summary, AUDIT.summary);
  });

  it("a backend failure is a structured error, not a throw", async () => {
    const failing = tools({
      getFleetSyncAudit: async () => {
        throw new Error("API error (503): Service Unavailable");
      },
    }) as any;
    const out = JSON.parse(await failing.getFleetSyncAudit.execute({}, {}));
    assert.equal(out.success, false);
    assert.match(out.error, /503/);
  });

  it("its access row is `none`: no agent target, the backend scopes the rows (D12)", () => {
    const row = TOOL_ACCESS_POLICY.get_fleet_sync_audit as { kind: string; why?: string };
    assert.ok(row, "get_fleet_sync_audit has no TOOL_ACCESS_POLICY row");
    assert.equal(row.kind, "none");
    assert.match(row.why ?? "", /accessible_agent_names/);
  });
});
