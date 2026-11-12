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
import { ApiError, TrinityClient as RealClient } from "../client.js";
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

describe("ent#661 — the turn id rides every project call; the audience gate's refusal is honest", () => {
  const session = { session: { executionId: "exec-42" } } as never;

  it("each tool forwards the platform-supplied turn id, never a parameter", async () => {
    const turns: unknown[] = [];
    const rec = (i: number) => async (...args: unknown[]) => {
      turns.push(args[i]);
      return i === 0 ? [] : { ok: true };
    };
    const tools = makeTools({
      listMyProjects: rec(0) as never,
      getMyProject: rec(1) as never,
      listProjectTasks: rec(2) as never,
      getProjectLog: rec(2) as never,
      addProjectLogEntry: rec(2) as never,
      readProjectContext: rec(3) as never,
    });
    await tools.list_projects.execute({}, session);
    await tools.get_project.execute({ project_id: PROJECT.id }, session);
    await tools.list_project_tasks.execute({ project_id: PROJECT.id }, session);
    await tools.get_project_log.execute({ project_id: PROJECT.id }, session);
    await tools.add_project_log_entry.execute({ project_id: PROJECT.id, kind: "note", body: "x" }, session);
    await tools.read_project_context.execute({ project_id: PROJECT.id, item_id: "ctx_1" }, session);
    assert.deepEqual(turns, ["exec-42", "exec-42", "exec-42", "exec-42", "exec-42", "exec-42"]);
  });

  it("the client sends it as X-Trinity-Execution-Id, and nothing without one", async () => {
    const client = new RealClient("http://localhost:8000");
    const seen: unknown[] = [];
    (client as unknown as { request: unknown }).request = async (...args: unknown[]) => {
      seen.push(args[5]);
      return { projects: [] };
    };
    await client.listMyProjects("exec-42");
    await client.listMyProjects();
    assert.deepEqual(seen, [{ "X-Trinity-Execution-Id": "exec-42" }, undefined]);
  });

  it("an outside audience is refused in words the agent can act on", async () => {
    const tools = makeTools({
      listMyProjects: async () => {
        throw apiError(403, { code: "external_audience", message: "m" });
      },
    });
    const out = JSON.parse(await tools.list_projects.execute({}, session));
    assert.equal(out.enabled, false);
    assert.match(out.message, /internal conversations/);
  });
});

describe("ent#661 v3 — the steward's tools", () => {
  const session = { session: { executionId: "exec-7" } } as never;

  it("get_steward_digest returns the digest and forwards the turn", async () => {
    const turns: unknown[] = [];
    const tools = makeTools({
      getStewardDigest: (async (turn?: string) => {
        turns.push(turn);
        return [{ project: { id: PROJECT.id }, attention: 2 }];
      }) as never,
    });
    const out = JSON.parse(await tools.get_steward_digest.execute({}, session));
    assert.equal(out.count, 1);
    assert.deepEqual(turns, ["exec-7"]);
  });

  it("set_project_health sends state and note, and a refusal is structured", async () => {
    const calls: unknown[][] = [];
    const tools = makeTools({
      setProjectHealth: (async (id: string, body: unknown, turn?: string) => {
        calls.push([id, body, turn]);
        return { health: { state: "at-risk" } };
      }) as never,
    });
    await tools.set_project_health.execute({ project_id: PROJECT.id, state: "at-risk", note: "late" }, session);
    assert.deepEqual(calls[0], [PROJECT.id, { state: "at-risk", note: "late" }, "exec-7"]);

    const refused = makeTools({
      setProjectHealth: (async () => {
        throw apiError(403, { code: "not_permitted", message: "Only the project's steward or its creator sets its health." });
      }) as never,
    });
    const out = JSON.parse(await refused.set_project_health.execute({ project_id: PROJECT.id, state: "on-track" }, session));
    assert.equal(out.success, false);
    assert.equal(out.code, "not_permitted");
  });

  it("link_to_project accepts an ask", async () => {
    const tools = makeTools();
    const kind = (tools.link_to_project.parameters as { shape: { kind: { options: string[] } } }).shape.kind.options;
    assert.ok(kind.includes("ask"));
  });
});

describe("ent#588 — an agent starts or imports a project (projects.manage)", () => {
  it("create_project sends name and goal, and the tracker only when given", async () => {
    const calls: unknown[][] = [];
    const tools = makeTools({
      createProjectAsAgent: async (body: Record<string, unknown>) => {
        calls.push(["create", body]);
        return { ...PROJECT, steward: { kind: "agent", ref: "a1" } };
      },
    } as Partial<TrinityClient>);
    const out = JSON.parse(await tools.create_project.execute({ name: "Q4 launch", goal: "Ship it." }));
    assert.equal(out.success, true);
    assert.equal(out.project.id, PROJECT.id);
    assert.deepEqual(calls, [["create", { name: "Q4 launch", goal: "Ship it." }]]);
  });

  it("without the grant the named refusal comes back, never a throw", async () => {
    const tools = makeTools({
      createProjectAsAgent: async () => {
        throw apiError(403, { code: "project_management_not_permitted", message: "No project-management permission." });
      },
    } as Partial<TrinityClient>);
    const out = JSON.parse(await tools.create_project.execute({ name: "x", goal: "y" }));
    assert.equal(out.success, false);
    assert.equal(out.code, "project_management_not_permitted");
  });

  it("create_project and import_project offer no steward: another agent needs an approval (#1194 ruling)", async () => {
    const calls: unknown[][] = [];
    const tools = makeTools({
      createProjectAsAgent: async (body: Record<string, unknown>) => { calls.push(["create", body]); return PROJECT; },
      importProjectAsAgent: async (...args: unknown[]) => { calls.push(["import", ...args]); return PROJECT; },
    } as Partial<TrinityClient>);
    assert.equal("steward" in (tools.create_project.parameters as any).shape, false);
    assert.equal("steward" in (tools.import_project.parameters as any).shape, false);
    await tools.create_project.execute({ name: "x", goal: "y", steward: "pm" } as any);
    await tools.import_project.execute({ path: "project_files/q4", steward: "pm" } as any);
    assert.deepEqual(calls[0], ["create", { name: "x", goal: "y" }]);
    assert.deepEqual(calls[1].slice(0, 2), ["import", "project_files/q4"]);
    assert.equal(calls[1].includes("pm"), false);
    assert.match(tools.create_project.description, /propose_project_changes/);
  });

  it("import_project forwards only the path; a second import's 409 is surfaced", async () => {
    const calls: unknown[][] = [];
    const tools = makeTools({
      importProjectAsAgent: async (path: string) => {
        calls.push(["import", path]);
        throw apiError(409, { code: "already_imported", message: "This folder was already imported." });
      },
    } as Partial<TrinityClient>);
    const out = JSON.parse(await tools.import_project.execute({ path: "project_files/q4" }));
    assert.deepEqual(calls, [["import", "project_files/q4"]]);
    assert.equal(out.success, false);
    assert.equal(out.code, "already_imported");
  });
});

describe("ent#980 — the steward proposes people; the creator approves once", () => {
  it("propose_project_changes forwards only what was given, without the project id", async () => {
    const calls: unknown[][] = [];
    const tools = makeTools({
      proposeProjectChanges: async (projectId: string, body: Record<string, unknown>) => {
        calls.push([projectId, body]);
        return { proposal_id: "ppr_1", state: "pending", waiting_on: "alice@example.com" };
      },
    } as Partial<TrinityClient>);
    const out = JSON.parse(await tools.propose_project_changes.execute({
      project_id: PROJECT.id,
      members: ["bob@example.com", "role:compliance-lead"],
      task_owners: [{ task_id: "T-001", owner: "bob@example.com" }],
    }));
    assert.equal(out.success, true);
    assert.equal(out.waiting_on, "alice@example.com");
    assert.deepEqual(calls, [[PROJECT.id, {
      members: ["bob@example.com", "role:compliance-lead"],
      task_owners: [{ task_id: "T-001", owner: "bob@example.com" }],
    }]]);
  });

  it("a pending proposal and a missing grant come back as named refusals", async () => {
    for (const [status, code] of [
      [409, "already_pending"], [403, "project_management_not_permitted"],
      // trinity-enterprise#1089: the four primary-contact refusals are relayed by name.
      [422, "primary_unassigned"], [422, "primary_cannot_open_agent"],
      [422, "primary_cannot_open_project"], [422, "primary_cannot_apply"],
    ] as const) {
      const tools = makeTools({
        proposeProjectChanges: async () => {
          throw apiError(status, { code, message: "refused" });
        },
      } as Partial<TrinityClient>);
      const out = JSON.parse(await tools.propose_project_changes.execute({ project_id: PROJECT.id, members: ["b@example.com"] }));
      assert.equal(out.success, false);
      assert.equal(out.code, code);
    }
  });
});

describe("trinity-enterprise#1089 — any agent on the project requests; its primary approves", () => {
  it("forwards remove_members and remove_agents with the other verbs", async () => {
    const calls: unknown[][] = [];
    const tools = makeTools({
      proposeProjectChanges: async (projectId: string, body: Record<string, unknown>) => {
        calls.push([projectId, body]);
        return { proposal_id: "ppr_2", state: "pending", waiting_on: "primary" };
      },
    } as Partial<TrinityClient>);
    const out = JSON.parse(await tools.propose_project_changes.execute({
      project_id: PROJECT.id,
      members: ["bob@example.com"],
      remove_members: ["carol@example.com"],
      remove_agents: ["scout"],
    }));
    assert.equal(out.success, true);
    assert.equal(out.waiting_on, "primary");
    assert.deepEqual(calls, [[PROJECT.id, {
      members: ["bob@example.com"],
      remove_members: ["carol@example.com"],
      remove_agents: ["scout"],
    }]]);
  });

  it("bounds the removal lists as the backend does (20 members, 10 agents)", () => {
    const shape = makeTools({} as Partial<TrinityClient>).propose_project_changes.parameters;
    const emails = (n: number) => Array.from({ length: n }, (_, i) => `p${i}@example.com`);
    const agents = (n: number) => Array.from({ length: n }, (_, i) => `a${i}`);
    assert.equal(shape.safeParse({ project_id: PROJECT.id, remove_members: emails(20) }).success, true);
    assert.equal(shape.safeParse({ project_id: PROJECT.id, remove_members: emails(21) }).success, false);
    assert.equal(shape.safeParse({ project_id: PROJECT.id, remove_agents: agents(10) }).success, true);
    assert.equal(shape.safeParse({ project_id: PROJECT.id, remove_agents: agents(11) }).success, false);
  });

  it("the description says who decides and what Approve does, within the budget", () => {
    const d = makeTools({} as Partial<TrinityClient>).propose_project_changes.description;
    assert.ok(d.length <= 2048, `${d.length} chars`);
    assert.ok(d.includes(
      "It goes to YOUR primary contact (Agent › Access) as one Inbox decision — not to the project's creator"));
    assert.ok(d.includes(
      "Approve applies it as them, once; anything they couldn't do on the project themselves is reported as not applied, by name"));
    assert.doesNotMatch(d, /As a project's steward/);
    for (const code of ["primary_unassigned", "primary_cannot_open_agent", "primary_cannot_open_project",
      "primary_cannot_apply", "already_pending", "proposal_pending", "project_management_not_permitted"]) {
      assert.ok(d.includes(code), `missing ${code}`);
    }
  });
});


describe("ent#1193 read_project_context", () => {
  it("forwards the item and the offset, and returns the part with its named truncation", async () => {
    const calls: unknown[][] = [];
    const tools = makeTools({
      readProjectContext: (async (pid: string, item: string, offset: number) => {
        calls.push([pid, item, offset]);
        return { id: item, text: "part one", truncated: true, shown_bytes: 8, total_bytes: 20, next_offset: 8 };
      }) as never,
    });
    const first = JSON.parse(await tools.read_project_context.execute({ project_id: PROJECT.id, item_id: "ctx_1" }, {}));
    assert.equal(first.success, true);
    assert.equal(first.truncated, true);
    assert.equal(first.next_offset, 8);
    await tools.read_project_context.execute({ project_id: PROJECT.id, item_id: "ctx_1", offset: 8 }, {});
    assert.deepEqual(calls, [[PROJECT.id, "ctx_1", 0], [PROJECT.id, "ctx_1", 8]]);
  });

  it("a 404 comes back as a structured result, never a throw", async () => {
    const tools = makeTools({
      readProjectContext: (async () => { throw apiError(404, { code: "context_not_found", message: "Not found." }); }) as never,
    });
    const out = JSON.parse(await tools.read_project_context.execute({ project_id: PROJECT.id, item_id: "nope" }, {}));
    assert.equal(out.success, false);
  });

  it("the client calls the agent route with the offset", async () => {
    const client = new RealClient("http://localhost:8000");
    const seen: unknown[] = [];
    (client as unknown as { request: unknown }).request = async (...args: unknown[]) => {
      seen.push([args[0], args[1]]);
      return {};
    };
    await client.readProjectContext("prj_1", "ctx 1", 204800);
    assert.deepEqual(seen, [["GET", "/api/enterprise/projects/agent/projects/prj_1/context/ctx%201?offset=204800"]]);
  });
});

describe("ent#1232 — task shape: todo, subtasks, blocked-by, tasks for people", () => {
  const TOOL_NAMES = [
    "list_projects", "get_project", "list_project_tasks", "create_project_task", "update_project_task",
    "add_project_task_note", "read_project_context", "get_project_log", "add_project_log_entry",
    "link_to_project", "get_steward_digest", "create_project", "import_project",
    "propose_project_changes", "set_project_health",
  ] as const;

  it("every project tool description stays under 2048 characters", () => {
    const tools = makeTools({} as Partial<TrinityClient>) as Record<string, { description: string }>;
    for (const name of TOOL_NAMES) {
      const d = tools[name].description;
      assert.ok(d.length < 2048, `${name}: ${d.length} chars`);
    }
  });

  it("list_project_tasks forwards view 'next' to the client, and only when asked", async () => {
    const calls: unknown[][] = [];
    const tools = makeTools({
      listProjectTasks: (async (pid: string, status: string, _turn?: string, view?: string) => {
        calls.push([pid, status, view]);
        return [{ id: "T-002", status: "todo", unblocked: true }];
      }) as never,
    });
    const out = JSON.parse(await tools.list_project_tasks.execute({ project_id: "prj_1", view: "next" }, {}));
    assert.equal(out.count, 1);
    await tools.list_project_tasks.execute({ project_id: "prj_1" }, {});
    assert.deepEqual(calls, [["prj_1", "open", "next"], ["prj_1", "open", undefined]]);
    const shape = tools.list_project_tasks.parameters;
    assert.equal(shape.safeParse({ project_id: "prj_1", view: "next" }).success, true);
    assert.equal(shape.safeParse({ project_id: "prj_1", view: "soon" }).success, false);
  });

  it("the client sends view as its own query parameter, never as a status", async () => {
    const client = new RealClient("http://localhost:8000");
    const seen: unknown[] = [];
    (client as unknown as { request: unknown }).request = async (...args: unknown[]) => {
      seen.push(args[1]);
      return { tasks: [] };
    };
    await client.listProjectTasks("prj_1", "open", undefined, "next");
    await client.listProjectTasks("prj_1", "open");
    assert.deepEqual(seen, [
      "/api/enterprise/projects/agent/projects/prj_1/tasks?status=open&view=next",
      "/api/enterprise/projects/agent/projects/prj_1/tasks?status=open",
    ]);
  });

  it("create_project_task forwards parent, blocked_by and for_person", async () => {
    const calls: unknown[][] = [];
    const tools = makeTools({
      createProjectTask: async (pid: string, body: Record<string, unknown>) => {
        calls.push([pid, body]);
        return { id: "T-005" };
      },
    } as Partial<TrinityClient>);
    await tools.create_project_task.execute({
      project_id: "prj_1", title: "Sign the contract", parent: "T-001", blocked_by: ["T-003", "T-004"],
      for_person: true, owner: "alex@example.com",
    } as never, {});
    const parsed = tools.create_project_task.parameters.parse({
      project_id: "prj_1", title: "x", parent: "T-001", blocked_by: ["T-003"], for_person: true,
    });
    assert.deepEqual([parsed.parent, parsed.blocked_by, parsed.for_person], ["T-001", ["T-003"], true]);
    assert.deepEqual(calls[0], ["prj_1", {
      title: "Sign the contract", parent: "T-001", blocked_by: ["T-003", "T-004"],
      for_person: true, owner: "alex@example.com",
    }]);
  });

  it("todo is a status a task can be set to; next is not", () => {
    const shape = makeTools({} as Partial<TrinityClient>).update_project_task.parameters;
    assert.equal(shape.safeParse({ project_id: "prj_1", task_id: "T-001", status: "todo" }).success, true);
    assert.equal(shape.safeParse({ project_id: "prj_1", task_id: "T-001", status: "next" }).success, false);
  });

  it("update_project_task: parent null clears it and is sent as null; links go by add and remove", async () => {
    const calls: unknown[][] = [];
    const tools = makeTools({
      updateProjectTask: async (pid: string, tid: string, body: Record<string, unknown>) => {
        calls.push([pid, tid, body]);
        return { id: tid };
      },
    } as Partial<TrinityClient>);
    const shape = tools.update_project_task.parameters;
    const parsed = shape.parse({ project_id: "prj_1", task_id: "T-002", parent: null, blocked_by_add: ["T-004"] });
    assert.equal(parsed.parent, null);
    assert.deepEqual(parsed.blocked_by_add, ["T-004"]);
    await tools.update_project_task.execute({
      project_id: "prj_1", task_id: "T-002", parent: null, blocked_by_add: ["T-004"], blocked_by_remove: ["T-003"],
    } as never, {});
    assert.deepEqual(calls[0], ["prj_1", "T-002", {
      parent: null, blocked_by_add: ["T-004"], blocked_by_remove: ["T-003"],
    }]);
    assert.equal(JSON.stringify(calls[0][2]).includes('"parent":null'), true);
  });

  it("caps the blocker lists at 20 ids of the form T-NNN, as the backend does", () => {
    const tools = makeTools({} as Partial<TrinityClient>);
    const ids = (n: number) => Array.from({ length: n }, (_, i) => `T-${String(i + 1).padStart(3, "0")}`);
    const create = tools.create_project_task.parameters;
    assert.equal(create.safeParse({ project_id: "p", title: "x", blocked_by: ids(20) }).success, true);
    assert.equal(create.safeParse({ project_id: "p", title: "x", blocked_by: ids(21) }).success, false);
    assert.equal(create.safeParse({ project_id: "p", title: "x", blocked_by: ["T-1; DROP"] }).success, false);
    const update = tools.update_project_task.parameters;
    assert.equal(update.safeParse({ project_id: "p", task_id: "T-001", blocked_by_add: ids(21) }).success, false);
    assert.equal(update.safeParse({ project_id: "p", task_id: "T-001", blocked_by_remove: ids(21) }).success, false);
  });

  it("a circle is refused by name with its path, never a throw", async () => {
    const tools = makeTools({
      updateProjectTask: async () => {
        throw apiError(422, { code: "blocker_cycle", message: "That link would make a circle: T-004 → T-007 → T-004." });
      },
    } as Partial<TrinityClient>);
    const out = JSON.parse(await tools.update_project_task.execute(
      { project_id: "prj_1", task_id: "T-004", blocked_by_add: ["T-007"] } as never, {}));
    assert.equal(out.success, false);
    assert.equal(out.code, "blocker_cycle");
    assert.match(out.error, /T-004 → T-007 → T-004/);
  });

  it("done without a done claim: the note is sent, and a missing one is refused by name", async () => {
    const calls: unknown[][] = [];
    const tools = makeTools({
      updateProjectTask: async (pid: string, tid: string, body: Record<string, unknown>) => {
        calls.push([pid, tid, body]);
        if (!body.note) {
          throw apiError(422, { code: "done_without_claim_needs_note", message: "Add a note saying how it was finished." });
        }
        return { id: tid, status: "done", closed_without_claim: true };
      },
    } as Partial<TrinityClient>);
    const refused = JSON.parse(await tools.update_project_task.execute(
      { project_id: "prj_1", task_id: "T-005", status: "done" } as never, {}));
    assert.equal(refused.success, false);
    assert.equal(refused.code, "done_without_claim_needs_note");
    const out = JSON.parse(await tools.update_project_task.execute(
      { project_id: "prj_1", task_id: "T-005", status: "done", note: "Shipped by hand." } as never, {}));
    assert.deepEqual(calls[1], ["prj_1", "T-005", { status: "done", note: "Shipped by hand." }]);
    assert.equal(out.closed_without_claim, true);
  });

  it("the descriptions say what done without a done claim means", () => {
    const tools = makeTools({} as Partial<TrinityClient>);
    for (const name of ["update_project_task", "list_project_tasks"] as const) {
      const d = tools[name].description;
      assert.ok(d.includes("its output may not exist; read the note"), `${name}: missing the meaning`);
      assert.ok(d.length < 2048, `${name}: ${d.length} chars`);
    }
    assert.ok(tools.update_project_task.description.includes("done_without_claim_needs_note"));
    assert.ok(tools.update_project_task.description.includes("closed_without_claim"));
    assert.ok(tools.list_project_tasks.description.includes("blockers_closed_without_claim"));
  });

  it("the descriptions name the new rules and their refusals", () => {
    const tools = makeTools({} as Partial<TrinityClient>);
    const create = tools.create_project_task.description;
    for (const word of ["todo", "parent", "blocked_by", "for_person", "one level", "blocker_cycle",
      "only_the_steward_assigns_people", "person_task_has_agent", "person_task_needs_owner"]) {
      assert.ok(create.includes(word), `create_project_task: missing ${word}`);
    }
    const update = tools.update_project_task.description;
    for (const word of ["blocked_by_add", "blocked_by_remove", "parent", "null", "person_marks_done",
      "blocker_cycle", "Finishing every subtask does not finish the parent"]) {
      assert.ok(update.includes(word), `update_project_task: missing ${word}`);
    }
    const list = tools.list_project_tasks.description;
    for (const word of ["view", "next", "waiting_for", "unblocked", "subtasks", "creator", "assigned_to"]) {
      assert.ok(list.includes(word), `list_project_tasks: missing ${word}`);
    }
  });
});
