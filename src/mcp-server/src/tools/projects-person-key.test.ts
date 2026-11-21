/**
 * trinity-enterprise#1322 — a person's own user key reads a project over MCP,
 * as that person, on the Workspace routes. The tool picks the route from the
 * key's scope (an allow-list): `agent` keeps today's exact requests, `user`
 * goes to the person routes, anything else is refused inside the tool without
 * a request. The real tools and the real client run; only `fetch` is stubbed,
 * so each assertion is on the request that would leave the MCP server.
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { afterEach, beforeEach, describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createProjectTools } from "./projects.js";
import { TrinityClient } from "../client.js";

type Sent = { method: string; path: string; headers: Record<string, string>; body?: unknown };
let sent: Sent[] = [];
const realFetch = globalThis.fetch;

beforeEach(() => {
  sent = [];
  globalThis.fetch = (async (url: string | URL, init?: RequestInit) => {
    const u = new URL(String(url));
    sent.push({ method: String(init?.method ?? "GET"), path: u.pathname + u.search, headers: Object.fromEntries(new Headers(init?.headers).entries()), body: init?.body ? JSON.parse(String(init.body)) : undefined });
    const body = u.pathname.endsWith("/log") ? { entries: [] } : u.pathname.endsWith("/tasks") ? { tasks: [] } : { projects: [], id: "prj_1" };
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
});
afterEach(() => {
  globalThis.fetch = realFetch;
});

const tools = () => createProjectTools(new TrinityClient("http://backend.test"), true) as any;
const as = (scope: string) => ({ session: { scope, mcpApiKey: `trinity_mcp_${scope}`, executionId: "exec-1", agentName: scope === "agent" ? "alpha" : undefined } });
const P = "/api/enterprise/projects";

const READS: Array<[string, Record<string, unknown>, string, string]> = [
  // tool, args, person path, agent path (today's)
  ["list_projects", {}, `${P}`, `${P}/agent/projects`],
  ["get_project", { project_id: "prj_1" }, `${P}/prj_1`, `${P}/agent/projects/prj_1`],
  ["list_project_tasks", { project_id: "prj_1" }, `${P}/prj_1/tasks?status=open`, `${P}/agent/projects/prj_1/tasks?status=open`],
  ["get_project_log", { project_id: "prj_1" }, `${P}/prj_1/log?limit=50`, `${P}/agent/projects/prj_1/log?limit=50`],
  ["list_project_executions", { project_id: "prj_1" }, `${P}/prj_1/executions`, `${P}/agent/projects/prj_1/executions`],
  ["get_steward_digest", {}, `${P}/stewarding`, `${P}/agent/stewarding`],
  ["read_project_context", { project_id: "prj_1", item_id: "brief" }, `${P}/prj_1/context/brief`, `${P}/agent/projects/prj_1/context/brief?offset=0`],
];

describe("trinity-enterprise#1322 the read tools route by the key's scope", () => {
  for (const [tool, args, personPath, agentPath] of READS) {
    it(`${tool}: a user key reads as the person, on the Workspace route, never acting as an agent`, async () => {
      const out = JSON.parse(await tools()[tool].execute(args, as("user")));
      assert.notEqual(out.success ?? out.enabled, false, JSON.stringify(out));
      assert.deepEqual(sent.map((s) => `${s.method} ${s.path}`), [`GET ${personPath}`]);
      assert.equal(sent[0].headers["authorization"], "Bearer trinity_mcp_user");
      assert.equal(sent[0].headers["x-trinity-act-as-agent"], undefined);
    });

    it(`${tool}: an agent key keeps today's exact request`, async () => {
      const out = JSON.parse(await tools()[tool].execute(args, as("agent")));
      assert.notEqual(out.success ?? out.enabled, false, JSON.stringify(out));
      assert.deepEqual(sent.map((s) => `${s.method} ${s.path}`), [`GET ${agentPath}`]);
      assert.equal(sent[0].headers["x-trinity-execution-id"], "exec-1");
    });

    it(`${tool}: any other scope is refused in the tool, with no request`, async () => {
      for (const scope of ["system", "ops", "portal_delegate", "connector", "some_future_tier"]) {
        const out = JSON.parse(await tools()[tool].execute(args, as(scope)));
        assert.equal(out.success, false, `${scope}: ${JSON.stringify(out)}`);
        assert.equal(out.code, "key_not_supported", `${scope}: ${JSON.stringify(out)}`);
      }
      assert.deepEqual(sent, []);
    });

    it(`${tool}: a user key naming an agent is refused (act_as_not_offered), with no request`, async () => {
      const out = JSON.parse(await tools()[tool].execute({ ...args, agent_name: "alpha" }, as("user")));
      assert.equal(out.success, false);
      assert.equal(out.code, "act_as_not_offered");
      assert.match(out.error, /acts as you/);
      assert.deepEqual(sent, []);
    });
  }

  it("list_projects: include_archived reaches the person route; an agent key's request is unchanged", async () => {
    await tools().list_projects.execute({ include_archived: true }, as("user"));
    await tools().list_projects.execute({ include_archived: true }, as("agent"));
    assert.deepEqual(sent.map((s) => s.path), [`${P}?include_archived=true`, `${P}/agent/projects`]);
  });

  it("an agent key naming another agent is still refused", async () => {
    const out = JSON.parse(await tools().get_project.execute({ project_id: "prj_1", agent_name: "beta" }, as("agent")));
    assert.equal(out.success, false);
    assert.deepEqual(sent, []);
  });

  it("keyless dev mode keeps today's agent request", async () => {
    const base = new TrinityClient("http://backend.test");
    base.setToken("dev-token");
    const dev = createProjectTools(base, false) as any;
    await dev.get_project.execute({ project_id: "prj_1" }, {});
    assert.deepEqual(sent.map((s) => s.path), [`${P}/agent/projects/prj_1`]);
  });
});


const WRITES: Array<[string, Record<string, unknown>, string, string, Record<string, unknown>]> = [
  // tool, args, person request, agent request (today's), the body both send
  ["create_project_task", { project_id: "prj_1", title: "Do it", assignee: "bob" }, `POST ${P}/prj_1/tasks`, `POST ${P}/agent/projects/prj_1/tasks`, { title: "Do it", agent: "bob" }],
  ["update_project_task", { project_id: "prj_1", task_id: "T-001", status: "active" }, `PATCH ${P}/prj_1/tasks/T-001`, `PATCH ${P}/agent/projects/prj_1/tasks/T-001`, { status: "active" }],
  ["add_project_task_note", { project_id: "prj_1", task_id: "T-001", body: "half way" }, `POST ${P}/prj_1/tasks/T-001/log`, `POST ${P}/agent/projects/prj_1/tasks/T-001/log`, { body: "half way" }],
  ["add_project_log_entry", { project_id: "prj_1", kind: "decision", body: "We ship." }, `POST ${P}/prj_1/log`, `POST ${P}/agent/projects/prj_1/log`, { kind: "decision", body: "We ship." }],
  ["link_to_project", { project_id: "prj_1", kind: "report", target_id: "r1" }, `POST ${P}/prj_1/items`, `POST ${P}/agent/projects/prj_1/items`, { kind: "report", target_id: "r1" }],
  ["set_project_health", { project_id: "prj_1", state: "at-risk", note: "late" }, `POST ${P}/prj_1/health`, `POST ${P}/agent/projects/prj_1/health`, { state: "at-risk", note: "late" }],
  ["create_project", { name: "Q4", goal: "ship" }, `POST ${P}`, `POST ${P}/agent/projects`, { name: "Q4", goal: "ship" }],
];

describe("trinity-enterprise#1322 the write tools route by the key's scope", () => {
  for (const [tool, args, personReq, agentReq, body] of WRITES) {
    it(`${tool}: a user key acts as the person, on the Workspace route`, async () => {
      const out = JSON.parse(await tools()[tool].execute(args, as("user")));
      assert.equal(out.success, true, JSON.stringify(out));
      assert.deepEqual(sent.map((s) => `${s.method} ${s.path}`), [personReq]);
      assert.deepEqual(sent[0].body, body);
      assert.equal(sent[0].headers["authorization"], "Bearer trinity_mcp_user");
      assert.equal(sent[0].headers["x-trinity-act-as-agent"], undefined);
    });

    it(`${tool}: an agent key keeps today's exact request`, async () => {
      const out = JSON.parse(await tools()[tool].execute(args, as("agent")));
      assert.equal(out.success, true, JSON.stringify(out));
      assert.deepEqual(sent.map((s) => `${s.method} ${s.path}`), [agentReq]);
      assert.deepEqual(sent[0].body, body);
      assert.equal(sent[0].headers["x-trinity-execution-id"], "exec-1");
    });

    it(`${tool}: any other scope is refused in the tool, with no request`, async () => {
      for (const scope of ["system", "ops", "portal_delegate", "connector", "some_future_tier"]) {
        const out = JSON.parse(await tools()[tool].execute(args, as(scope)));
        assert.equal(out.code, "key_not_supported", `${scope}: ${JSON.stringify(out)}`);
      }
      assert.deepEqual(sent, []);
    });

    it(`${tool}: a user key naming an agent is refused (act_as_not_offered), with no request`, async () => {
      const out = JSON.parse(await tools()[tool].execute({ ...args, agent_name: "alpha" }, as("user")));
      assert.equal(out.code, "act_as_not_offered");
      assert.deepEqual(sent, []);
    });
  }

  it("import_project: a person names the agent whose folder it is; an agent key imports its own", async () => {
    const t = tools();
    const user = JSON.parse(await t.import_project.execute({ path: "project_files/q4", agent: "alpha" }, as("user")));
    const agent = JSON.parse(await t.import_project.execute({ path: "project_files/q4" }, as("agent")));
    assert.equal(user.success, true, JSON.stringify(user));
    assert.equal(agent.success, true, JSON.stringify(agent));
    assert.deepEqual(sent.map((s) => `${s.method} ${s.path}`), [`POST ${P}/import`, `POST ${P}/agent/projects/import`]);
    assert.deepEqual(sent[0].body, { agent_name: "alpha", path: "project_files/q4" });
    assert.deepEqual(sent[1].body, { path: "project_files/q4" });
  });

  it("import_project: a person's key without `agent` is refused by name, with no request", async () => {
    const out = JSON.parse(await tools().import_project.execute({ path: "project_files/q4" }, as("user")));
    assert.equal(out.code, "agent_required");
    assert.deepEqual(sent, []);
  });

  it("the two agent-only tools answer a user key that this tool is for an agent's key, with no request and no path", async () => {
    for (const [tool, args] of [["start_project_task", { project_id: "prj_1", task_id: "T-001" }], ["propose_project_changes", { project_id: "prj_1", summary: "x" }]] as const) {
      const out = JSON.parse(await tools()[tool].execute(args as never, as("user")));
      assert.equal(out.success, false, tool);
      assert.match(out.error, /for an agent's own key/, tool);
      assert.match(out.error, /A person's key, including an admin's, can't call it/, tool);
      assert.doesNotMatch(out.error, /agent context|from the agent itself|\b[a-z]+_[a-z_]+\b/, tool);
    }
    assert.deepEqual(sent, []);
  });

  it("no write tool listed to a user key answers it 'needs an agent key'", async () => {
    for (const [tool, args] of [...WRITES, ["import_project", { path: "p", agent: "alpha" }] as const]) {
      const out = await tools()[tool].execute(args, as("user"));
      assert.doesNotMatch(out, /agent-scoped key|agent context|agent's own key/, tool);
    }
  });

  it("create_project_task, import_project and create_project don't point at propose_project_changes", () => {
    for (const tool of ["create_project_task", "import_project", "create_project"]) {
      assert.doesNotMatch(tools()[tool].description, /propose_project_changes/, tool);
    }
  });
});

// ---------------------------------------------------------------------------
// CP-P4: the five person-only tools (orchestrator rulings 2).
// ---------------------------------------------------------------------------

type Reply = { status: number; body?: unknown };
/** Answer each request from `replies` by "METHOD path", else 200 {}. */
function answer(replies: Record<string, Reply>) {
  globalThis.fetch = (async (url: string | URL, init?: RequestInit) => {
    const u = new URL(String(url));
    const key = `${String(init?.method ?? "GET")} ${u.pathname + u.search}`;
    sent.push({ method: String(init?.method ?? "GET"), path: u.pathname + u.search, headers: Object.fromEntries(new Headers(init?.headers).entries()), body: init?.body ? JSON.parse(String(init.body)) : undefined });
    const r = replies[key] ?? { status: 200, body: { id: "prj_1" } };
    if (r.status === 204) return new Response(null, { status: 204 });
    return new Response(JSON.stringify(r.body ?? {}), { status: r.status, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
}
const run = async (tool: string, args: Record<string, unknown>, scope = "user") =>
  JSON.parse(await tools()[tool].execute(args, as(scope)));
const reqs = () => sent.map((s) => `${s.method} ${s.path}`);

describe("trinity-enterprise#1322 set_project_lifecycle", () => {
  const CASES: Array<[string, string, unknown]> = [
    ["start", `POST ${P}/prj_1/start`, { note: "go" }],
    ["stop", `POST ${P}/prj_1/stop`, { note: "go" }],
    ["finish", `PATCH ${P}/prj_1`, { status: "done" }],
    ["archive", `POST ${P}/prj_1/archive`, undefined],
    ["restore", `POST ${P}/prj_1/restore`, undefined],
  ];
  for (const [action, req, body] of CASES) {
    it(`${action} goes to the person route`, async () => {
      answer({});
      const out = await run("set_project_lifecycle", { project_id: "prj_1", action, note: "go" });
      assert.equal(out.success, true, JSON.stringify(out));
      assert.deepEqual(reqs(), [req]);
      assert.deepEqual(sent[0].body, body);
      assert.equal(out.note_recorded, action === "start" || action === "stop");
    });
  }
  it("the backend's refusal is passed through verbatim", async () => {
    answer({ [`POST ${P}/prj_1/start`]: { status: 403, body: { detail: { code: "not_permitted", message: "Only an editor can start it." } } } });
    const out = await run("set_project_lifecycle", { project_id: "prj_1", action: "start" });
    assert.equal(out.success, false);
    assert.equal(out.code, "not_permitted");
    assert.equal(out.error, "Only an editor can start it.");
  });
});

describe("trinity-enterprise#1322 update_project_settings: one request per concern, each outcome reported", () => {
  it("sends goal (never description), and steward, visibility and mode each in their own request", async () => {
    answer({});
    const out = await run("update_project_settings", {
      project_id: "prj_1", name: "N", goal: "G", tracker_url: "https://t.example/x",
      steward: { kind: "agent", ref: "alpha" }, visibility: "company", mode: "auto",
    });
    assert.equal(out.success, true, JSON.stringify(out));
    assert.deepEqual(reqs(), Array(4).fill(`PATCH ${P}/prj_1`));
    assert.deepEqual(sent.map((s) => s.body), [
      { name: "N", goal: "G", tracker_url: "https://t.example/x" },
      { steward: { kind: "agent", ref: "alpha" } },
      { visibility: "company" },
      { mode: "auto" },
    ]);
    assert.deepEqual(out.results.map((r: any) => [r.concern, r.ok]), [["fields", true], ["steward", true], ["visibility", true], ["mode", true]]);
  });

  it("a refused concern is reported by name and the others still say what happened", async () => {
    let n = 0;
    globalThis.fetch = (async (url: string | URL, init?: RequestInit) => {
      sent.push({ method: String(init?.method), path: new URL(String(url)).pathname, headers: {}, body: JSON.parse(String(init?.body)) });
      n += 1;
      if (n === 2) return new Response(JSON.stringify({ detail: { code: "mode_is_a_persons", message: "Only a person with edit rights sets the mode." } }), { status: 403 });
      return new Response(JSON.stringify({ id: "prj_1" }), { status: 200 });
    }) as typeof fetch;
    const out = await run("update_project_settings", { project_id: "prj_1", visibility: "members", mode: "track" });
    assert.equal(out.success, false);
    assert.deepEqual(out.results.map((r: any) => [r.concern, r.ok, r.code]), [["visibility", true, undefined], ["mode", false, "mode_is_a_persons"]]);
    assert.match(out.error, /mode/);
  });

  it("nothing to change is refused by name, with no request", async () => {
    answer({});
    const out = await run("update_project_settings", { project_id: "prj_1" });
    assert.equal(out.code, "nothing_to_change");
    assert.deepEqual(sent, []);
  });
});

describe("trinity-enterprise#1322 set_steward_waking", () => {
  it("on resumes, off pauses", async () => {
    answer({ [`POST ${P}/prj_1/steward/resume`]: { status: 200, body: { wake: { on: true } } } });
    const on = await run("set_steward_waking", { project_id: "prj_1", on: true });
    const off = await run("set_steward_waking", { project_id: "prj_1", on: false });
    assert.equal(on.success, true);
    assert.deepEqual(on.wake, { on: true });
    assert.equal(off.success, true);
    assert.deepEqual(reqs(), [`POST ${P}/prj_1/steward/resume`, `POST ${P}/prj_1/steward/pause`]);
  });
});

describe("trinity-enterprise#1322 manage_project_members: a result per item", () => {
  it("adds and removes each item on its own route, in order, encoded", async () => {
    answer({ [`DELETE ${P}/prj_1/members/a%2Bb%40example.com`]: { status: 204 }, [`DELETE ${P}/prj_1/agents/beta`]: { status: 204 } });
    const out = await run("manage_project_members", {
      project_id: "prj_1", add_members: ["x@example.com"], remove_members: ["a+b@example.com"], add_agents: ["alpha"], remove_agents: ["beta"],
    });
    assert.equal(out.success, true, JSON.stringify(out));
    assert.deepEqual(reqs(), [
      `POST ${P}/prj_1/members`, `DELETE ${P}/prj_1/members/a%2Bb%40example.com`, `POST ${P}/prj_1/agents`, `DELETE ${P}/prj_1/agents/beta`,
    ]);
    assert.deepEqual(sent[0].body, { email: "x@example.com" });
    assert.deepEqual(sent[2].body, { agent_name: "alpha" });
    assert.deepEqual(out.results.map((r: any) => [r.action, r.item, r.ok]), [
      ["add_member", "x@example.com", true], ["remove_member", "a+b@example.com", true], ["add_agent", "alpha", true], ["remove_agent", "beta", true],
    ]);
  });

  it("one refused item is reported and the rest still run", async () => {
    answer({ [`POST ${P}/prj_1/members`]: { status: 409, body: { detail: { code: "already_member", message: "Already a member." } } } });
    const out = await run("manage_project_members", { project_id: "prj_1", add_members: ["x@example.com"], add_agents: ["alpha"] });
    assert.equal(out.success, false);
    assert.deepEqual(out.results.map((r: any) => [r.item, r.ok, r.code]), [["x@example.com", false, "already_member"], ["alpha", true, undefined]]);
  });

  it("a project the person can't see stops the batch: the rest are reported not attempted", async () => {
    answer({ [`POST ${P}/prj_1/members`]: { status: 404, body: { detail: { code: "project_not_found", message: "Project not found." } } } });
    const out = await run("manage_project_members", { project_id: "prj_1", add_members: ["x@example.com"], add_agents: ["alpha"] });
    assert.equal(sent.length, 1);
    assert.deepEqual(out.results.map((r: any) => [r.item, r.ok, r.code]), [["x@example.com", false, "project_not_found"], ["alpha", false, "not_attempted"]]);
  });

  it("an item both added and removed, or more than 20 items, is refused with no request", async () => {
    answer({});
    const both = await run("manage_project_members", { project_id: "prj_1", add_agents: ["alpha"], remove_agents: ["alpha"] });
    const many = await run("manage_project_members", { project_id: "prj_1", add_members: Array.from({ length: 21 }, (_, i) => `p${i}@example.com`) });
    assert.equal(both.code, "item_in_both");
    assert.equal(many.code, "too_many_items");
    assert.deepEqual(sent, []);
  });
});

describe("trinity-enterprise#1322 decide_project_runs", () => {
  it("with no ids it lists the waiting runs with their ids", async () => {
    answer({ [`GET ${P}/prj_1/runs/waiting`]: { status: 200, body: { runs: [{ id: "run_1", task_id: "T-001" }] } } });
    const out = await run("decide_project_runs", { project_id: "prj_1" });
    assert.equal(out.success, true);
    assert.deepEqual(out.runs, [{ id: "run_1", task_id: "T-001" }]);
    assert.deepEqual(reqs(), [`GET ${P}/prj_1/runs/waiting`]);
  });

  // The backend's shape (enterprise projects `runs._batch`): HTTP 200, one
  // `{id, outcome}` per id — a run state when applied (`started` for an approve,
  // `declined` for a decline), a refusal code otherwise.
  it("approves and declines; success only when every id was applied", async () => {
    answer({
      [`POST ${P}/prj_1/runs/approve`]: { status: 200, body: { results: [{ id: "run_1", outcome: "started" }] } },
      [`POST ${P}/prj_1/runs/decline`]: { status: 200, body: { results: [{ id: "run_2", outcome: "declined" }] } },
    });
    const out = await run("decide_project_runs", { project_id: "prj_1", approve: ["run_1"], decline: ["run_2"], note: "not now" });
    assert.equal(out.success, true, JSON.stringify(out));
    assert.deepEqual(sent.map((s) => s.body), [{ ids: ["run_1"] }, { ids: ["run_2"], note: "not now" }]);
    assert.deepEqual(out.approved, [{ id: "run_1", outcome: "started", applied: true }]);
    assert.deepEqual(out.declined, [{ id: "run_2", outcome: "declined", applied: true }]);
  });

  it("an id the backend answers run_not_found is not applied, and success is false", async () => {
    answer({ [`POST ${P}/prj_1/runs/approve`]: { status: 200, body: { results: [
      { id: "run_1", outcome: "started" }, { id: "run_x", outcome: "run_not_found" },
    ] } } });
    const out = await run("decide_project_runs", { project_id: "prj_1", approve: ["run_1", "run_x"] });
    assert.equal(out.success, false, JSON.stringify(out));
    assert.match(out.error, /1 of 2 not applied/);
    assert.deepEqual(out.approved.map((r: any) => [r.id, r.applied]), [["run_1", true], ["run_x", false]]);
    assert.match(out.approved[1].why, /no waiting run/i);
  });

  it("an id the backend answers already_decided is not applied, and success is false", async () => {
    answer({ [`POST ${P}/prj_1/runs/decline`]: { status: 200, body: { results: [{ id: "run_2", outcome: "already_decided" }] } } });
    const out = await run("decide_project_runs", { project_id: "prj_1", decline: ["run_2"] });
    assert.equal(out.success, false, JSON.stringify(out));
    assert.deepEqual(out.declined.map((r: any) => [r.id, r.outcome, r.applied]), [["run_2", "already_decided", false]]);
    assert.match(out.declined[0].why, /already decided/i);
  });

  it("approvals that went through are reported when the decline request then fails", async () => {
    answer({
      [`POST ${P}/prj_1/runs/approve`]: { status: 200, body: { results: [{ id: "run_1", outcome: "started" }] } },
      [`POST ${P}/prj_1/runs/decline`]: { status: 503, body: { detail: "upstream unavailable" } },
    });
    const out = await run("decide_project_runs", { project_id: "prj_1", approve: ["run_1"], decline: ["run_2"] });
    assert.equal(out.success, false, JSON.stringify(out));
    assert.deepEqual(out.approved, [{ id: "run_1", outcome: "started", applied: true }]);
    assert.deepEqual(out.declined.map((r: any) => [r.id, r.applied]), [["run_2", false]]);
    assert.match(out.error, /approvals were applied/i);
  });

  // A request that succeeded is not every id applied: the claim "the approvals
  // were applied" holds only when each approve id was.
  it("a decline failure after a partly applied approve names both counts", async () => {
    answer({
      [`POST ${P}/prj_1/runs/approve`]: { status: 200, body: { results: [{ id: "r1", outcome: "started" }, { id: "r2", outcome: "already_decided" }] } },
      [`POST ${P}/prj_1/runs/decline`]: { status: 503, body: { detail: "upstream unavailable" } },
    });
    const out = await run("decide_project_runs", { project_id: "prj_1", approve: ["r1", "r2"], decline: ["r3"] });
    assert.equal(out.success, false, JSON.stringify(out));
    assert.deepEqual(out.approved.map((r: any) => [r.id, r.applied]), [["r1", true], ["r2", false]]);
    assert.doesNotMatch(out.error, /^The approvals were applied/i);
    assert.match(out.error, /1 of 2 approvals/i);
    assert.match(out.error, /declines were not/i);
  });

  it("every approval applied and the decline request fails: the approvals were applied", async () => {
    answer({
      [`POST ${P}/prj_1/runs/approve`]: { status: 200, body: { results: [{ id: "r1", outcome: "started" }, { id: "r2", outcome: "started" }] } },
      [`POST ${P}/prj_1/runs/decline`]: { status: 503, body: { detail: "upstream unavailable" } },
    });
    const out = await run("decide_project_runs", { project_id: "prj_1", approve: ["r1", "r2"], decline: ["r3"] });
    assert.equal(out.success, false, JSON.stringify(out));
    assert.match(out.error, /^The approvals were applied; the declines were not/);
  });

  it("an id in both lists is refused with no request", async () => {
    answer({});
    const out = await run("decide_project_runs", { project_id: "prj_1", approve: ["run_1"], decline: ["run_1"] });
    assert.equal(out.code, "id_in_both");
    assert.deepEqual(sent, []);
  });
});

describe("trinity-enterprise#1322 the person tools refuse every other key in the tool", () => {
  const PERSON_TOOLS: Array<[string, Record<string, unknown>]> = [
    ["set_project_lifecycle", { project_id: "prj_1", action: "start" }],
    ["update_project_settings", { project_id: "prj_1", mode: "auto" }],
    ["set_steward_waking", { project_id: "prj_1", on: true }],
    ["manage_project_members", { project_id: "prj_1", add_agents: ["alpha"] }],
    ["decide_project_runs", { project_id: "prj_1" }],
  ];
  for (const [tool, args] of PERSON_TOOLS) {
    it(`${tool}: an agent key is told it is a person's act; other scopes key_not_supported; no request`, async () => {
      answer({});
      const agent = await run(tool, args, "agent");
      assert.equal(agent.code, "persons_act", JSON.stringify(agent));
      for (const scope of ["system", "ops", "portal_delegate", "connector", "some_future_tier"]) {
        assert.equal((await run(tool, args, scope)).code, "key_not_supported", scope);
      }
      assert.deepEqual(sent, []);
    });
  }
});

// ---------------------------------------------------------------------------
// CP-P5: refusals read true for the key that reads them.
// ---------------------------------------------------------------------------

describe("trinity-enterprise#1322 explain(): only the entitlement gate reads as 'not licensed'", () => {
  const LICENSE = { status: 403, body: { detail: "Enterprise feature 'projects' is not licensed for this instance." } };
  const OTHER = { status: 403, body: { detail: "Your account has no email; projects need one." } };
  // Each source that turns a plain-string 403 into words: list_projects, get_project, and fail().
  const SOURCES: Array<[string, Record<string, unknown>, string, (o: any) => string]> = [
    ["list_projects", {}, `GET ${P}`, (o) => o.message],
    ["get_project", { project_id: "prj_1" }, `GET ${P}/prj_1`, (o) => o.error],
    ["add_project_log_entry", { project_id: "prj_1", kind: "note", body: "x" }, `POST ${P}/prj_1/log`, (o) => o.error],
  ];
  for (const [tool, args, route, text] of SOURCES) {
    it(`${tool}: the license gate's 403 says not licensed`, async () => {
      answer({ [route]: LICENSE });
      assert.equal(text(await run(tool, args)), "Projects are not licensed for this instance.");
    });
    it(`${tool}: any other string 403 is passed through unchanged`, async () => {
      answer({ [route]: OTHER });
      assert.equal(text(await run(tool, args)), "Your account has no email; projects need one.");
    });
  }
});

describe("trinity-enterprise#1322 read_project_context: a person's key takes no offset", () => {
  it("an offset from a person's key is refused by name, with no request", async () => {
    answer({});
    const out = await run("read_project_context", { project_id: "prj_1", item_id: "brief", offset: 100 });
    assert.equal(out.code, "offset_not_offered", JSON.stringify(out));
    assert.deepEqual(sent, []);
  });
  it("an agent key still pages by offset", async () => {
    answer({});
    await run("read_project_context", { project_id: "prj_1", item_id: "brief", offset: 100 }, "agent");
    assert.deepEqual(reqs(), [`GET ${P}/agent/projects/prj_1/context/brief?offset=100`]);
  });
  it("the description promises paging to an agent's key only", () => {
    const d: string = tools().read_project_context.description;
    assert.match(d, /agent's own key[^.]*200 KB/);
    assert.match(d, /person's own key reads the brief from its start/);
  });
});

describe("trinity-enterprise#1322 a session with no scope takes the agent path only when no key is required", () => {
  it("keyed: a scope-less session is refused in the tool, shared and person tools alike, with no request", async () => {
    answer({});
    const keyless = { session: { mcpApiKey: "trinity_mcp_x", executionId: "exec-1" } };
    for (const [tool, args] of [["get_project", { project_id: "prj_1" }], ["set_steward_waking", { project_id: "prj_1", on: true }]] as const) {
      const out = JSON.parse(await tools()[tool].execute(args, keyless));
      assert.equal(out.code, "key_not_supported", `${tool}: ${JSON.stringify(out)}`);
    }
    assert.deepEqual(sent, []);
  });
});

describe("trinity-enterprise#1322 the steward-proposal pointer", () => {
  const PAGE = { status: 200, body: { id: "prj_1", proposal: { ask_id: "oq-1", summary: "add bob" } } };
  it("a person's key reads how to decide a waiting proposal: its ask_id to respond_to_operator_queue, by the primary contacts", async () => {
    answer({ [`GET ${P}/prj_1`]: PAGE });
    const out = await run("get_project", { project_id: "prj_1" });
    assert.equal(out.proposal.ask_id, "oq-1");
    assert.match(out.proposal.how_to_decide, /proposal\.ask_id as item_id to respond_to_operator_queue/);
    assert.match(out.proposal.how_to_decide, /proposing agent's primary contacts/);
  });
  it("an agent key's page is unchanged", async () => {
    answer({ [`GET ${P}/agent/projects/prj_1`]: PAGE });
    const out = await run("get_project", { project_id: "prj_1" }, "agent");
    assert.deepEqual(out.proposal, { ask_id: "oq-1", summary: "add bob" });
  });
});
