/**
 * trinity-enterprise#661 — the Workspace Projects read tools.
 *
 * Pins the tool-layer contract:
 *   - `list_projects` / `get_project` proxy the agent read routes and never
 *     throw: every refusal comes back as a structured result the agent can
 *     reason about;
 *   - a 404 on the list is an OSS build (not available); a 404 on get is "not a
 *     project you are on" — the backend answers a missing project and one the
 *     agent is not active on identically, and the tool must not add a
 *     distinction the backend deliberately withholds;
 *   - a plain-string 403 is an unlicensed build, a `{code}` 403 is the
 *     agent-key gate — same status, different situations.
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createProjectTools } from "./projects.js";
import { ApiError } from "../client.js";
import type { TrinityClient } from "../client.js";

function apiError(status: number, detail: unknown): ApiError {
  return new ApiError(status, JSON.stringify({ detail }));
}

const PROJECT = {
  id: "prj_0123456789abcdef", name: "Q4 launch", goal: "Ship it.", status: "active",
  visibility: "members", steward: null, tracker_url: null,
};

function makeTools(overrides: Partial<TrinityClient> = {}, calls: unknown[][] = []) {
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    listMyProjects: async () => {
      calls.push(["list"]);
      return [PROJECT];
    },
    getMyProject: async (id: string) => {
      calls.push(["get", id]);
      return { ...PROJECT, members: [], agents: ["a1"], linked_chat_count: 2, linked_room_count: 0 };
    },
    ...overrides,
  };
  return createProjectTools(fake as TrinityClient, false);
}

describe("ent#661 projects — proxy shape", () => {
  it("list_projects returns the enabled:true shape", async () => {
    const out = JSON.parse(await makeTools().list_projects.execute({}, {}));
    assert.equal(out.enabled, true);
    assert.equal(out.count, 1);
    assert.equal(out.projects[0].id, PROJECT.id);
  });

  it("get_project forwards the id and returns the project", async () => {
    const calls: unknown[][] = [];
    const out = JSON.parse(
      await makeTools({}, calls).get_project.execute({ project_id: PROJECT.id }, {}),
    );
    assert.deepEqual(calls[0], ["get", PROJECT.id]);
    assert.equal(out.success, true);
    assert.equal(out.linked_chat_count, 2);
  });
});

describe("ent#661 projects — honest refusals, never a throw", () => {
  it("list: a 404 is an OSS build", async () => {
    const tools = makeTools({ listMyProjects: async () => { throw apiError(404, "Not Found"); } });
    const out = JSON.parse(await tools.list_projects.execute({}, {}));
    assert.equal(out.enabled, false);
    assert.match(out.message, /not available/);
  });

  it("list: a string 403 is unlicensed, a code 403 is the agent-key gate", async () => {
    const unlicensed = makeTools({ listMyProjects: async () => { throw apiError(403, "Feature not entitled"); } });
    assert.match(JSON.parse(await unlicensed.list_projects.execute({}, {})).message, /not licensed/);
    const notAgent = makeTools({
      listMyProjects: async () => {
        throw apiError(403, { code: "agent_key_required", message: "x" });
      },
    });
    assert.match(JSON.parse(await notAgent.list_projects.execute({}, {})).message, /agent context/);
  });

  it("get: a 404 is 'not a project you are on', with no further distinction", async () => {
    const tools = makeTools({
      getMyProject: async () => {
        throw apiError(404, { code: "project_not_found", message: "Project not found." });
      },
    });
    const out = JSON.parse(await tools.get_project.execute({ project_id: "prj_x" }, {}));
    assert.equal(out.success, false);
    assert.equal(out.not_found, true);
  });
});

describe("ent#661 v2 — tasks, log and items", () => {
  it("create_project_task forwards the body without the project id", async () => {
    const calls: unknown[][] = [];
    const tools = makeTools({
      createProjectTask: async (pid: string, body: Record<string, unknown>) => {
        calls.push([pid, body]);
        return { id: "T-001", title: body.title };
      },
    } as Partial<TrinityClient>);
    const out = JSON.parse(
      await tools.create_project_task.execute({ project_id: "prj_1", title: "Draft" }, {}),
    );
    assert.deepEqual(calls[0], ["prj_1", { title: "Draft" }]);
    assert.equal(out.success, true);
    assert.equal(out.id, "T-001");
  });

  it("a refused done surfaces the backend's code, never a throw", async () => {
    const tools = makeTools({
      updateProjectTask: async () => {
        throw apiError(403, { code: "done_needs_verification", message: "Move it to pending-verification." });
      },
    } as Partial<TrinityClient>);
    const out = JSON.parse(
      await tools.update_project_task.execute({ project_id: "prj_1", task_id: "T-001", status: "done" }, {}),
    );
    assert.equal(out.success, false);
    assert.equal(out.code, "done_needs_verification");
    assert.match(out.error, /pending-verification/);
  });

  it("a project the agent is not on reads as not_found", async () => {
    const tools = makeTools({
      addProjectLogEntry: async () => {
        throw apiError(404, { code: "project_not_found", message: "Project not found." });
      },
    } as Partial<TrinityClient>);
    const out = JSON.parse(
      await tools.add_project_log_entry.execute({ project_id: "prj_x", kind: "decision", body: "x" }, {}),
    );
    assert.equal(out.not_found, true);
  });

  it("link_to_project sends only kind and target", async () => {
    const calls: unknown[][] = [];
    const tools = makeTools({
      linkToProject: async (pid: string, body: Record<string, unknown>) => {
        calls.push([pid, body]);
        return body;
      },
    } as Partial<TrinityClient>);
    await tools.link_to_project.execute({ project_id: "prj_1", kind: "file", target_id: "f1" }, {});
    assert.deepEqual(calls[0], ["prj_1", { kind: "file", target_id: "f1" }]);
  });
});

describe("ent#661 v2 — the assignee maps to the task's agent field", () => {
  it("create_project_task sends assignee as agent", async () => {
    const calls: unknown[][] = [];
    const tools = makeTools({
      createProjectTask: async (pid: string, body: Record<string, unknown>) => {
        calls.push([pid, body]);
        return {};
      },
    } as Partial<TrinityClient>);
    await tools.create_project_task.execute({ project_id: "prj_1", title: "x", assignee: "scout" }, {});
    assert.deepEqual(calls[0], ["prj_1", { title: "x", agent: "scout" }]);
  });
});
