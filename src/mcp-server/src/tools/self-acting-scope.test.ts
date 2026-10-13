/**
 * trinity#2975 — a self-acting tool accepts the platform orchestrator's
 * system-scoped key.
 *
 * `report`, the canvas tools and the metrics tools take no target parameter on
 * purpose: they act as the caller, so the identity has to come from the key.
 * Each resolved that itself with `scope === "agent" && agentName`, which
 * refused `trinity-system` — whose key is `scope: "system"` carrying
 * `agentName: "trinity-system"` (`system_agent_service` mints it agent-scoped,
 * then flips the scope; #1816 makes re-minting it as `agent` impossible). The
 * platform's own agent could read everything and publish nothing: its daily
 * fleet-health report and its canvas fell back to files and the operator queue.
 *
 * One rule now, `access.resolveActingAgent`, and this file pins BOTH halves of
 * it — the admission and the refusals — through the REAL tool `execute()` with
 * a fake TrinityClient, per tool, because the bug was three copies of one line.
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";
import { resolveActingAgent, SELF_ACTING_SCOPES } from "../access.js";
import { createReportTools } from "./reports.js";
import { createCanvasTools } from "./canvas.js";
import { createMetricsTools } from "./metrics.js";
import type { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

const SYSTEM_AGENT = "trinity-system";

function ctx(over: Partial<McpAuthContext>): McpAuthContext {
  return {
    userId: "admin", userEmail: "a@example.com", keyName: "k",
    mcpApiKey: "trinity_mcp_x", ...over,
  } as McpAuthContext;
}

const SYSTEM_CTX = ctx({ scope: "system", agentName: SYSTEM_AGENT });
const AGENT_CTX = ctx({ scope: "agent", agentName: "worker" });

/** Records which agent name reached the client, and whether the network was touched. */
function recorder() {
  const calls: { method: string; agent: string }[] = [];
  const client = {
    getBaseUrl: () => "http://backend:8000",
    async createReport(agent: string) { calls.push({ method: "createReport", agent }); return { id: "r1", report_type: "t" }; },
    setToken: () => {},
    async getCanvasContext() { return null; },
    async writeCanvas(agent: string) { calls.push({ method: "writeCanvas", agent }); return { canvas_id: "default", version: 1 }; },
    async patchCanvas(agent: string) { calls.push({ method: "patchCanvas", agent }); return { canvas_id: "default", version: 2, blocks: [] }; },
    async recordMetrics(agent: string) { calls.push({ method: "recordMetrics", agent }); return { success: true, agent_name: agent, recorded: 1, deduplicated: 0, replayed: false, points: [] }; },
    async refreshMetricDefinitions(agent: string) { calls.push({ method: "refreshMetricDefinitions", agent }); return { success: true, declared: 0 }; },
  } as unknown as TrinityClient;
  return { calls, client };
}

const REPORT_ARGS = { report_type: "trinity_system.fleet_health", title: "Fleet health", payload: { markdown: "x" }, display_hint: "markdown" };
const CANVAS_ARGS = { title: "Fleet", blocks: [{ kind: "markdown", payload: { content: "x" } }] };
const METRIC_ARGS = { points: [{ metric: "agents_running", value: 3 }] };

/** Every self-acting tool, with the arguments that reach its client call. */
function surfaces(client: TrinityClient) {
  const reports = createReportTools(client, false);
  const canvas = createCanvasTools(client, false);
  const metrics = createMetricsTools(client, false);
  return [
    { name: "report", run: (c: McpAuthContext) => reports.report.execute(REPORT_ARGS as never, { session: c }), method: "createReport" },
    { name: "set_canvas", run: (c: McpAuthContext) => canvas.set_canvas.execute(CANVAS_ARGS as never, { session: c }), method: "writeCanvas" },
    { name: "patch_canvas", run: (c: McpAuthContext) => canvas.patch_canvas.execute({ blocks: [{ id: "b1", kind: "markdown", payload: { content: "x" } }] } as never, { session: c }), method: "patchCanvas" },
    { name: "record_metrics", run: (c: McpAuthContext) => metrics.recordMetrics.execute(METRIC_ARGS as never, { session: c }), method: "recordMetrics" },
    { name: "refresh_metric_definitions", run: (c: McpAuthContext) => metrics.refreshMetricDefinitions.execute({} as never, { session: c }), method: "refreshMetricDefinitions" },
  ];
}

describe("trinity#2975 — the system agent publishes as itself", () => {
  it("every self-acting tool accepts the system-scoped key and acts AS trinity-system", async () => {
    for (const surface of surfaces(recorder().client)) {
      const { calls, client } = recorder();
      const only = surfaces(client).find((s) => s.name === surface.name)!;
      const out = JSON.parse((await only.run(SYSTEM_CTX)) as string);
      assert.notEqual(out.success, false, `${surface.name} refused the system key: ${JSON.stringify(out)}`);
      assert.deepEqual(
        calls.map((c) => [c.method, c.agent]),
        [[surface.method, SYSTEM_AGENT]],
        `${surface.name} did not act as ${SYSTEM_AGENT}`,
      );
    }
  });

  it("an agent-scoped key is unchanged — it still acts as its own agent", async () => {
    for (const surface of surfaces(recorder().client)) {
      const { calls, client } = recorder();
      const only = surfaces(client).find((s) => s.name === surface.name)!;
      await only.run(AGENT_CTX);
      assert.deepEqual(calls.map((c) => c.agent), ["worker"], surface.name);
    }
  });

  for (const [label, bad] of [
    ["a user key", ctx({ scope: "user" })],
    ["a connector key bound to an agent", ctx({ scope: "connector", agentName: "worker" })],
    ["a portal_delegate key", ctx({ scope: "portal_delegate", agentName: "worker" })],
    ["an ops key", ctx({ scope: "ops" })],
    ["an anonymous session", ctx({ scope: "anonymous" })],
    ["a system key that names no agent", ctx({ scope: "system" })],
    ["a scope this union has never heard of", ctx({ scope: "future" as never, agentName: "worker" })],
  ] as const) {
    it(`${label} is refused, and never reaches the network`, async () => {
      for (const surface of surfaces(recorder().client)) {
        const { calls, client } = recorder();
        const only = surfaces(client).find((s) => s.name === surface.name)!;
        const out = JSON.parse((await only.run(bad)) as string);
        assert.equal(out.success, false, `${surface.name} admitted ${label}: ${JSON.stringify(out)}`);
        assert.match(String(out.error), /requires a key that carries an agent identity/);
        assert.deepEqual(calls, [], `${surface.name} called the backend for ${label}`);
      }
    });
  }
});

describe("resolveActingAgent — the rule itself", () => {
  it("is an ALLOWLIST over a free-text scope column", () => {
    assert.deepEqual([...SELF_ACTING_SCOPES].sort(), ["agent", "system"]);
  });

  it("returns the key's own agent, never a caller-supplied one", () => {
    assert.equal(resolveActingAgent(SYSTEM_CTX, "x"), SYSTEM_AGENT);
    assert.equal(resolveActingAgent(AGENT_CTX, "x"), "worker");
  });

  it("names what was presented, so the operator can tell the two refusals apart", () => {
    assert.throws(() => resolveActingAgent(ctx({ scope: "system" }), "The report tool"),
      /The report tool: .*'system'-scoped and names no agent/s);
    assert.throws(() => resolveActingAgent(ctx({ scope: "user" }), "The report tool"),
      /'user'-scoped/);
    assert.throws(() => resolveActingAgent(undefined, "The report tool"), /unscoped/);
  });
});
