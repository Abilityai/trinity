/**
 * trinity-enterprise#978 — a read narrowed by a running turn says so.
 *
 * While a shared person's or a visitor's turn runs on an agent, the backend
 * narrows every read made with the agent's key, and marks the response with
 * `X-Trinity-Read-Narrowed: <code>`. The tools that read executions show it as
 * `narrowed: {code, message}`, so an empty list or a "not found" is not read
 * as "nothing ran".
 *
 * Real HTTP: a backend that answers with or without the header, the real
 * TrinityClient, the real tools, called through the real withAudit wrapper
 * (which is what enters the per-call store the header is recorded in).
 */
import { strict as assert } from "node:assert";
import { after, before, beforeEach, describe, it } from "node:test";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { readFileSync, readdirSync } from "node:fs";

import { ApiError, TrinityClient } from "./client.js";
import { withAudit, type ToolCallContext } from "./audit.js";
import { currentReadNarrowing } from "./turn-context.js";
import { READ_NARROWED_MESSAGES, narrowedField } from "./read-narrowing.js";
import { createExecutionTools } from "./tools/executions.js";
import { createScheduleTools } from "./tools/schedules.js";
import { createProjectTools } from "./tools/projects.js";

type Answer = { status?: number; body?: unknown; narrowed?: string };

describe("ent#978 a narrowed read is shown as narrowed", () => {
  let backend: Server;
  let base: string;
  let answer: (url: string) => Answer;

  before(async () => {
    backend = createServer((req, res) => {
      const a = answer(req.url ?? "");
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      if (a.narrowed) headers["X-Trinity-Read-Narrowed"] = a.narrowed;
      res.writeHead(a.status ?? 200, headers);
      res.end(JSON.stringify(a.body ?? {}));
    });
    await new Promise<void>((r) => backend.listen(0, "127.0.0.1", () => r()));
    base = `http://127.0.0.1:${(backend.address() as AddressInfo).port}`;
  });
  after(() => backend.close());
  beforeEach(() => { answer = () => ({ body: [] }); });

  const session = (executionId?: string) =>
    ({ scope: "agent", agentName: "atlas", mcpApiKey: "agent-key", userId: "owner", executionId }) as never;
  /** Run a real tool the way the server does: through withAudit. */
  const call = async (tool: any, params: object, executionId: string | undefined = "exec-owner-1") => {
    const ctx: ToolCallContext = { session: session(executionId) };
    const text = await withAudit(tool.name, tool.execute)(params as never, ctx);
    return { out: JSON.parse(text), ctx };
  };
  const executions = () => createExecutionTools(new TrinityClient(base), true) as any;
  const schedules = () => createScheduleTools(new TrinityClient(base), true) as any;
  const projects = () => createProjectTools(new TrinityClient(base), true) as any;
  const RUN = { id: "e1", agent_name: "atlas", status: "success", message: "m", started_at: "t", triggered_by: "manual" };

  // ---- the measured pair -------------------------------------------------------------

  it("list_recent_executions: an empty narrowed list says it is narrowed, and why", async () => {
    answer = () => ({ body: [], narrowed: "running_turn" });
    const { out } = await call(executions().listRecentExecutions, { agent_name: "atlas" });
    assert.equal(out.execution_count, 0);
    assert.deepEqual(out.narrowed, { code: "running_turn", message: READ_NARROWED_MESSAGES.running_turn });
    assert.match(out.narrowed.message, /turn .* is running on this agent/);
  });

  it("the same read with no marker has no `narrowed` key at all", async () => {
    const { out } = await call(executions().listRecentExecutions, { agent_name: "atlas" });
    assert.equal("narrowed" in out, false);
  });

  it("a session with no turn id is marked too (the backend narrows it the same way)", async () => {
    answer = () => ({ body: [], narrowed: "running_turn" });
    const { out } = await call(executions().listRecentExecutions, { agent_name: "atlas" }, undefined);
    assert.equal(out.narrowed.code, "running_turn");
  });

  it("get_execution_result: a narrowed 'not found' is returned, marked, and audited as a failure", async () => {
    answer = () => ({ status: 404, body: { detail: "Execution not found" }, narrowed: "running_turn" });
    const { out, ctx } = await call(executions().getExecutionResult, { agent_name: "atlas", execution_id: "e-owner" });
    assert.equal(out.error, "Execution not found");
    assert.equal(out.execution_id, "e-owner");
    assert.deepEqual(out.narrowed, { code: "running_turn", message: READ_NARROWED_MESSAGES.running_turn });
    assert.equal(ctx.outcome?.kind, "failed");
  });

  it("get_execution_result: a 'not found' with no marker still throws, exactly as before", async () => {
    answer = () => ({ status: 404, body: { detail: "Execution not found" } });
    await assert.rejects(
      call(executions().getExecutionResult, { agent_name: "atlas", execution_id: "e-none" }),
      (e: unknown) => e instanceof ApiError && e.status === 404 && /API error \(404\)/.test(e.message));
  });

  it("get_execution_result: a run inside the narrowed view is read and marked", async () => {
    answer = () => ({ body: RUN, narrowed: "turn_person" });
    const { out, ctx } = await call(executions().getExecutionResult, { agent_name: "atlas", execution_id: "e1" });
    assert.equal(out.execution_id, "e1");
    assert.deepEqual(out.narrowed, { code: "turn_person", message: READ_NARROWED_MESSAGES.turn_person });
    assert.equal(ctx.outcome, undefined);
  });

  it("get_execution_result: another error under a narrowed view is not turned into 'not found'", async () => {
    answer = () => ({ status: 500, body: { detail: "boom" }, narrowed: "running_turn" });
    await assert.rejects(
      call(executions().getExecutionResult, { agent_name: "atlas", execution_id: "e1" }),
      (e: unknown) => e instanceof ApiError && e.status === 500);
  });

  // ---- the other narrowed reads --------------------------------------------------------

  it("get_fan_out_result: a narrowed 'not found' is returned and marked; unmarked still throws", async () => {
    answer = () => ({ status: 404, body: { detail: "Fan-out not found" }, narrowed: "running_turn" });
    const { out, ctx } = await call(executions().getFanOutResult, { agent_name: "atlas", fan_out_id: "fo_1" });
    assert.equal(out.error, "Fan-out not found");
    assert.equal(out.narrowed.code, "running_turn");
    assert.equal(ctx.outcome?.kind, "failed");
    answer = () => ({ status: 404, body: { detail: "Fan-out not found" } });
    await assert.rejects(call(executions().getFanOutResult, { agent_name: "atlas", fan_out_id: "fo_1" }));
  });

  it("get_fan_out_result: a batch inside the view is read and marked", async () => {
    answer = () => ({ body: { fan_out_id: "fo_1", status: "completed", total: 1, completed: 1, running: 0, tasks: [] },
                      narrowed: "running_turn" });
    const { out } = await call(executions().getFanOutResult, { agent_name: "atlas", fan_out_id: "fo_1" });
    assert.equal(out.status, "completed");
    assert.equal(out.narrowed.code, "running_turn");
  });

  it("get_agent_activity_summary: narrowed counts say so", async () => {
    answer = () => ({ body: { count: 0, activities: [] }, narrowed: "running_turn" });
    const { out } = await call(executions().getAgentActivitySummary, { agent_name: "atlas" });
    assert.equal(out.total_activities, 0);
    assert.equal(out.narrowed.code, "running_turn");
  });

  it("get_schedule_executions: both of its reads are marked", async () => {
    answer = () => ({ body: [], narrowed: "running_turn" });
    for (const params of [{ agent_name: "atlas" }, { agent_name: "atlas", schedule_id: "s1" }]) {
      const { out } = await call(schedules().getScheduleExecutions, params);
      assert.equal(out.narrowed.code, "running_turn", JSON.stringify(params));
    }
    answer = () => ({ body: [] });
    assert.equal("narrowed" in (await call(schedules().getScheduleExecutions, { agent_name: "atlas" })).out, false);
  });

  it("list_project_executions: a narrowed page says so", async () => {
    answer = () => ({ body: { executions: [], totals: {} }, narrowed: "running_turn" });
    const { out } = await call(projects().list_project_executions, { project_id: "prj_0123456789abcdef" });
    assert.equal(out.success, true);
    assert.equal(out.narrowed.code, "running_turn");
  });

  // ---- a restricted run's stamp is readable by id ------------------------------------------

  it("get_execution_result shows `restricted_turn` when the run was stamped, and nothing otherwise", async () => {
    const stamp = { tier: "share", kind: "person", cause: "creator_not_owner_tier", shell: false };
    answer = () => ({ body: { ...RUN, turn_integrity: JSON.stringify({ restricted_turn: stamp, other: 1 }) } });
    assert.deepEqual((await call(executions().getExecutionResult, { agent_name: "atlas", execution_id: "e1" })).out.restricted_turn, stamp);
    for (const raw of [undefined, null, "", "not json", JSON.stringify({ other: 1 }), JSON.stringify([1])]) {
      answer = () => ({ body: { ...RUN, turn_integrity: raw } });
      const { out } = await call(executions().getExecutionResult, { agent_name: "atlas", execution_id: "e1" });
      assert.equal("restricted_turn" in out, false, String(raw));
    }
  });

  // ---- the recorder -------------------------------------------------------------------

  it("the first code of a tool call sticks, and a later unmarked call does not clear it", async () => {
    const codes = ["turn_person", "running_turn", undefined];
    answer = () => ({ body: [], narrowed: codes.shift() });
    const seen = await withAudit("t", async () => {
      const c = new TrinityClient(base); c.setToken("k");
      await c.listAgents(); await c.listAgents(); await c.listAgents();
      return String(currentReadNarrowing());
    })({}, { session: session() });
    assert.equal(seen, "turn_person");
  });

  it("a value of the wrong shape is ignored; an unknown code keeps its code and gets the fallback text", async () => {
    const read = (narrowed: string) => {
      answer = () => ({ body: [], narrowed });
      return withAudit("t", async () => {
        const c = new TrinityClient(base); c.setToken("k");
        await c.listAgents();
        return JSON.stringify(narrowedField());
      })({}, { session: session() });
    };
    assert.deepEqual(JSON.parse(await read("Not A Code!")), {});
    const unknown = JSON.parse(await read("some_new_code")).narrowed;
    assert.equal(unknown.code, "some_new_code");
    assert.match(unknown.message, /narrowed/);
    assert.equal(Object.values(READ_NARROWED_MESSAGES).includes(unknown.message), false);
  });

  it("two concurrent tool calls never see each other's marker, and nothing leaks outside a call", async () => {
    answer = (url) => ({ body: [], narrowed: url.includes("limit=7") ? "running_turn" : undefined });
    const one = (limit: number) => call(executions().listRecentExecutions, { agent_name: "atlas", limit });
    const [marked, clean] = await Promise.all([one(7), one(9)]);
    assert.equal(marked.out.narrowed.code, "running_turn");
    assert.equal("narrowed" in clean.out, false);
    assert.equal(currentReadNarrowing(), undefined);
    assert.deepEqual(narrowedField(), {});
  });

  it("every code the backend sends has its own wording", () => {
    assert.deepEqual(Object.keys(READ_NARROWED_MESSAGES).sort(), ["running_turn", "turn_person", "unverified"]);
    for (const text of Object.values(READ_NARROWED_MESSAGES)) assert.ok(text.length > 40);
  });

  // ---- a new tool cannot forget it ---------------------------------------------------------

  it("every tool file that reads executions through the client shows the marker (guard)", () => {
    // The marker reaches an agent only if the tool spreads `narrowedField()`
    // into what it returns. chat.ts is the one exception: its read is an
    // internal retry-chain lookup of the caller's own turn, not an answer.
    // Per file, so it is a floor: it catches a new tool FILE, not a new tool
    // added to a file that already spreads the field.
    const NARROWED_READS = /\.(getAgentExecutions|getExecution|getExecutionLog|getFanOutResult|getActivityTimeline|getScheduleExecutions|listProjectExecutions)\(/;
    const EXEMPT = new Set(["chat.ts"]);
    const dir = new URL("./tools/", import.meta.url);
    const missing = readdirSync(dir)
      .filter((f) => f.endsWith(".ts") && !f.endsWith(".test.ts") && !EXEMPT.has(f))
      .filter((f) => {
        const src = readFileSync(new URL(f, dir), "utf8");
        return NARROWED_READS.test(src) && !/narrowedField\(\)/.test(src);
      });
    assert.deepEqual(missing, []);
  });
});
