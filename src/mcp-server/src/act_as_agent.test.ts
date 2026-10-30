/**
 * trinity-enterprise#1024 — an admin's user key acts on behalf of an agent.
 *
 * The self-acting tools took their identity from the key alone, so a local hub
 * on the operator's admin user key could not raise an ask, report, record
 * metrics or write a canvas. Every such tool now takes `agent_name`:
 *   - agent / system key: acts as itself; naming another agent is refused, and
 *     naming itself is harmless;
 *   - user key: acts for the named agent, sending `X-Trinity-Act-As-Agent` —
 *     the BACKEND admits it only for an admin (`dependencies.admit_act_as`);
 *     with no `agent_name` the refusal names the parameter;
 *   - connector / portal_delegate / ops / anonymous: refused, naming the key.
 *
 * Runner: node:test → `node --import tsx --test src/*.test.ts`.
 */
import { afterEach, describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { actingClient, resolveActingAgent } from "./access.js";
import { TrinityClient } from "./client.js";
import { createOperatorQueueTools } from "./tools/operator_queue.js";
import { createReportTools } from "./tools/reports.js";
import { createMetricsTools } from "./tools/metrics.js";
import { createCanvasTools } from "./tools/canvas.js";
import { createProjectTools } from "./tools/projects.js";
import type { McpAuthContext } from "./types.js";

const ctx = (scope: McpAuthContext["scope"], agentName?: string) =>
  ({ userId: "ops-admin", keyName: "hub", scope, agentName, mcpApiKey: "trinity_mcp_x" }) as McpAuthContext;

describe("#1024 resolveActingAgent — the scope matrix", () => {
  it("an agent key acts as itself, with or without naming itself", () => {
    assert.equal(resolveActingAgent(ctx("agent", "worker"), "T"), "worker");
    assert.equal(resolveActingAgent(ctx("agent", "worker"), "T", "worker"), "worker");
  });

  it("an agent key naming another agent is refused", () => {
    assert.throws(() => resolveActingAgent(ctx("agent", "worker"), "T", "other"), /acts only as 'worker'/);
  });

  it("the system key keeps acting as the agent it was minted for", () => {
    assert.equal(resolveActingAgent(ctx("system", "trinity-system"), "T"), "trinity-system");
    assert.throws(() => resolveActingAgent(ctx("system", "trinity-system"), "T", "worker"));
  });

  it("a user key acts for the named agent", () => {
    assert.equal(resolveActingAgent(ctx("user"), "T", "worker"), "worker");
    assert.equal(resolveActingAgent(ctx("user"), "T", "  worker "), "worker");
  });

  it("a user key with no agent_name is told to pass it", () => {
    assert.throws(() => resolveActingAgent(ctx("user"), "T"), /pass agent_name/);
    assert.throws(() => resolveActingAgent(ctx("user"), "T", "  "), /pass agent_name/);
  });

  for (const scope of ["connector", "portal_delegate", "ops", "anonymous"] as const) {
    it(`a ${scope} key is refused even naming an agent`, () => {
      assert.throws(() => resolveActingAgent(ctx(scope, "worker"), "T", "worker"), new RegExp(`'${scope}'-scoped`));
    });
  }
});

describe("#1024 the act-as header travels on a clone only", () => {
  const realFetch = globalThis.fetch;
  afterEach(() => { globalThis.fetch = realFetch; });

  it("asAgent adds X-Trinity-Act-As-Agent; the base client never carries it", async () => {
    const seen: Record<string, string>[] = [];
    globalThis.fetch = (async (_url: unknown, init: { headers: Record<string, string> }) => {
      seen.push(init.headers);
      return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
    }) as typeof fetch;
    const base = new TrinityClient("http://backend", "trinity_mcp_x");
    await base.asAgent("worker").request("GET", "/api/x", undefined, false, undefined,
      { "X-Trinity-Act-As-Agent": "spoofed" });
    await base.request("GET", "/api/x");
    assert.equal(seen[0]["X-Trinity-Act-As-Agent"], "worker", "a per-call header cannot override it");
    assert.equal(seen[1]["X-Trinity-Act-As-Agent"], undefined);
  });

  it("actingClient clones for a user key and leaves an agent key's client alone", () => {
    const base = new TrinityClient("http://backend", "t");
    assert.notEqual(actingClient(base, ctx("user"), "worker"), base);
    assert.equal(actingClient(base, ctx("agent", "worker"), "worker"), base);
  });
});

// --- one tool per family, wired end to end through a fake client -------------

type Call = { method: string; agent: string; actAs?: string; body?: unknown };

function fakeClient(calls: Call[]) {
  const make = (actAs?: string): any => ({
    getBaseUrl: () => "http://backend",
    asAgent: (name: string) => make(name),
    raiseAsk: async (agent: string, body: unknown) => {
      calls.push({ method: "raiseAsk", agent, actAs, body });
      return { status: "created" };
    },
    createReport: async (agent: string, body: unknown) => {
      calls.push({ method: "createReport", agent, actAs, body });
      return { id: "r1", agent_name: agent, report_type: "t", title: "t", created_at: "x" };
    },
    recordMetrics: async (agent: string, body: unknown) => {
      calls.push({ method: "recordMetrics", agent, actAs, body });
      return { accepted: 1 };
    },
    getCanvasContext: async () => ({}),
    writeCanvas: async (agent: string, _id: string, body: unknown) => {
      calls.push({ method: "writeCanvas", agent, actAs, body });
      return { id: "c1" };
    },
    // Project routes name no agent in the path: the header IS the identity.
    createProjectAsAgent: async (body: unknown) => {
      calls.push({ method: "createProjectAsAgent", agent: actAs ?? "(key)", actAs, body });
      return { project: { id: "prj_1" } };
    },
    createProjectTask: async (_pid: string, body: unknown) => {
      calls.push({ method: "createProjectTask", agent: actAs ?? "(key)", actAs, body });
      return { task: { id: "T-001" } };
    },
  });
  return make() as TrinityClient;
}

const run = async (tool: any, args: Record<string, unknown>, session: McpAuthContext) =>
  JSON.parse(await tool.execute(args, { session }));

describe("#1024 every family takes agent_name the same way", () => {
  const families = [
    ["ask_operator", (c: TrinityClient) => createOperatorQueueTools(c, false).askOperator,
      { request_id: "r-1", type: "question", title: "Approve?", question: "May I?" }, "raiseAsk"],
    ["report", (c: TrinityClient) => createReportTools(c, false).report,
      { report_type: "ops.note", title: "t", payload: { markdown: "x" } }, "createReport"],
    ["record_metrics", (c: TrinityClient) => createMetricsTools(c, false).recordMetrics,
      { points: [{ metric: "m", value: 1 }] }, "recordMetrics"],
    ["set_canvas", (c: TrinityClient) => createCanvasTools(c, false).set_canvas,
      { canvas_id: "c1", title: "t", blocks: [{ type: "markdown", text: "x" }] }, "writeCanvas"],
  ] as const;

  for (const [name, make, args, method] of families) {
    it(`${name}: a user key with agent_name acts for that agent, through the header`, async () => {
      const calls: Call[] = [];
      await run(make(fakeClient(calls)), { ...args, agent_name: "worker" }, ctx("user"));
      const call = calls.find((c) => c.method === method);
      assert.ok(call, `${method} was not called`);
      assert.equal(call!.agent, "worker");
      assert.equal(call!.actAs, "worker");
      assert.equal(JSON.stringify(call!.body).includes("agent_name"), false, "agent_name never travels in the body");
    });

    it(`${name}: a user key without agent_name is refused, naming the parameter`, async () => {
      const calls: Call[] = [];
      const out = await run(make(fakeClient(calls)), { ...args }, ctx("user"));
      assert.equal(calls.length, 0);
      assert.match(JSON.stringify(out), /agent_name/);
    });

    it(`${name}: an agent key acts as itself with no header`, async () => {
      const calls: Call[] = [];
      await run(make(fakeClient(calls)), { ...args }, ctx("agent", "worker"));
      const call = calls.find((c) => c.method === method);
      assert.equal(call!.agent, "worker");
      assert.equal(call!.actAs, undefined);
    });

    it(`${name}: a connector key naming an agent is refused`, async () => {
      const calls: Call[] = [];
      const out = await run(make(fakeClient(calls)), { ...args, agent_name: "worker" }, ctx("connector", "worker"));
      assert.equal(calls.length, 0);
      assert.match(JSON.stringify(out), /connector/);
    });
  }
});

describe("#1024 the project tools take agent_name too", () => {
  it("create_project with an admin's user key goes out as that agent", async () => {
    const calls: Call[] = [];
    const tools = createProjectTools(fakeClient(calls), false);
    await run(tools.create_project, { name: "Q4", goal: "ship", agent_name: "worker" }, ctx("user"));
    assert.equal(calls[0].method, "createProjectAsAgent");
    assert.equal(calls[0].actAs, "worker");
  });

  it("a task body never carries agent_name (the tool spreads its arguments)", async () => {
    const calls: Call[] = [];
    const tools = createProjectTools(fakeClient(calls), false);
    await run(tools.create_project_task,
      { project_id: "prj_1", title: "Do it", agent_name: "worker" }, ctx("user"));
    assert.equal(calls[0].actAs, "worker");
    assert.equal(JSON.stringify(calls[0].body).includes("agent_name"), false);
  });

  it("a user key without agent_name is told to pass it, and nothing is sent", async () => {
    const calls: Call[] = [];
    const out = await run(createProjectTools(fakeClient(calls), false).create_project,
      { name: "Q4", goal: "ship" }, ctx("user"));
    assert.equal(calls.length, 0);
    assert.equal(out.success, false);
    assert.match(out.error, /pass agent_name/);
  });

  it("an agent key naming another agent is refused; naming none is unchanged", async () => {
    const calls: Call[] = [];
    const tools = createProjectTools(fakeClient(calls), false);
    const out = await run(tools.create_project, { name: "Q4", goal: "ship", agent_name: "other" }, ctx("agent", "worker"));
    assert.equal(out.success, false);
    assert.match(out.error, /acts only as 'worker'/);
    await run(tools.create_project, { name: "Q4", goal: "ship" }, ctx("agent", "worker"));
    assert.equal(calls.length, 1);
    assert.equal(calls[0].actAs, undefined);
  });
});
