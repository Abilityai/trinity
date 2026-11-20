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
