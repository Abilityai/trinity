/**
 * Workspace Projects read tools (trinity-enterprise#661).
 *
 * A project links the Workspace chats and rooms where a piece of work happens.
 * An agent's turn in a linked chat carries a one-line "[Project] …" note with
 * the project id; these tools let the agent read what that project is for.
 * Agents never restructure a project (members, visibility). Since ent#588 an agent
 * holding `projects.manage` can START one — on its owner's behalf, stewarding it —
 * and import one of its own folder projects. Since v2 they read the log and
 * tasks, record outcomes in the log, keep tasks current, and put their own
 * files / reports / decisions on the project — only on projects they are
 * ACTIVE on (the backend answers anything else with a uniform 404).
 *
 * The backend returns only projects this agent is ACTIVE on — its owner
 * consented to the link — and never other people's chat titles or content.
 *
 * ── License-blind by design ────────────────────────────────────────────────
 * Like `credential_vault.ts`, this module knows nothing about entitlement: a
 * 404 on the list is an OSS build, a plain-string 403 an unlicensed build, and
 * a `{code: "agent_key_required"}` 403 a non-agent caller. On `get_project` a
 * 404 is uniform on the backend (missing ≡ not on it) and stays uniform here.
 */

import { z } from "zod";
import { TrinityClient, ApiError } from "../client.js";
import type { McpAuthContext } from "../types.js";

function parseError(error: unknown): { status?: number; code?: string; message: string; detailIsString: boolean } {
  const rawMessage = error instanceof Error ? error.message : String(error);
  const status = error instanceof ApiError ? error.status : undefined;
  const m = rawMessage.match(/^API error \(\d+\): ([\s\S]*)$/);
  let code: string | undefined;
  let detailIsString = false;
  let message = rawMessage;
  if (m) {
    try {
      const detail = (JSON.parse(m[1]) as { detail?: unknown })?.detail;
      if (detail && typeof detail === "object" && !Array.isArray(detail)) {
        const d = detail as { code?: unknown; message?: unknown };
        if (typeof d.code === "string") code = d.code;
        if (typeof d.message === "string") message = d.message;
      } else if (typeof detail === "string") {
        detailIsString = true;
        message = detail;
      }
    } catch {
      // Body wasn't JSON — keep the raw ApiError message.
    }
  }
  return { status, code, message, detailIsString };
}

function explain(status: number | undefined, code: string | undefined, detailIsString: boolean): string {
  if (status === 403 && code === "agent_key_required") {
    return "These tools act as an agent; call them from an agent context (an agent-scoped key).";
  }
  if (status === 403 && code === "external_audience") {
    return "Project tools are for internal conversations; someone outside the company is in this one. Don't share project details here.";
  }
  if (status === 403 && code === "turn_unknown") {
    return "Project tools answer only inside a turn the platform can identify.";
  }
  if (status === 403 && detailIsString) return "Projects are not licensed for this instance.";
  if (status === 404) return "Projects are not available on this platform.";
  return "Projects could not be reached.";
}

/** The platform-supplied turn id of this request (#2392) — never a tool parameter. */
const turnOf = (context?: { session?: McpAuthContext }): string | undefined => context?.session?.executionId;

export function createProjectTools(client: TrinityClient, requireApiKey: boolean) {
  const getClient = (authContext?: McpAuthContext): TrinityClient => {
    if (requireApiKey) {
      if (!authContext?.mcpApiKey) {
        throw new Error("MCP API key authentication required but no API key found in request context");
      }
      const userClient = new TrinityClient(client.getBaseUrl());
      userClient.setToken(authContext.mcpApiKey);
      return userClient;
    }
    return client;
  };

  /** A write or read refusal as a structured result — never a throw. */
  const fail = (e: unknown): string => {
    const { status, code, message, detailIsString } = parseError(e);
    const known = status === 404 && code === "project_not_found";
    return JSON.stringify(
      {
        success: false,
        error: known ? "No project with this id that you are working on." : (code ? message : explain(status, code, detailIsString)),
        ...(code ? { code } : {}),
        ...(known ? { not_found: true } : {}),
      },
      null,
      2,
    );
  };

  const ok = (payload: unknown) => JSON.stringify({ success: true, ...(payload as object) }, null, 2);

  const projectId = z.string().min(1).max(64).describe("The project id, e.g. prj_0123456789abcdef.");
  const taskId = z.string().min(1).max(16).describe("The task id, e.g. T-003.");
  const statusEnum = z.enum(["active", "blocked", "needs-decision", "paused", "pending-verification", "done"]);

  return {
    // ========================================================================
    list_projects: {
      name: "list_projects",
      description:
        "List the Workspace projects you are working on: each project's id, name, goal, status, " +
        "steward and tracker link. Only projects your owner approved you for are listed. When a " +
        "conversation starts with a '[Project]' line, it names one of these projects by id.",
      parameters: z.object({}),
      execute: async (_params: unknown, context?: { session?: McpAuthContext }) => {
        try {
          const projects = await getClient(context?.session).listMyProjects(turnOf(context));
          return JSON.stringify({ enabled: true, count: projects.length, projects }, null, 2);
        } catch (e) {
          const { status, code, message, detailIsString } = parseError(e);
          return JSON.stringify(
            { enabled: false, count: 0, projects: [], message: explain(status, code, detailIsString), detail: message },
            null,
            2,
          );
        }
      },
    },

    // ========================================================================
    get_project: {
      name: "get_project",
      description:
        "Read one Workspace project you are working on, by id: its goal, status, steward, tracker " +
        "link, members, the other agents on it, and how many chats and rooms are linked. Other " +
        "people's chats are private, so they appear only as a count. Use the id from the " +
        "'[Project]' line at the top of the conversation, or from list_projects.",
      parameters: z.object({
        project_id: z.string().min(1).max(64).describe("The project id, e.g. prj_0123456789abcdef."),
      }),
      execute: async (params: { project_id: string }, context?: { session?: McpAuthContext }) => {
        try {
          const project = await getClient(context?.session).getMyProject(params.project_id, turnOf(context));
          return JSON.stringify({ success: true, ...(project as object) }, null, 2);
        } catch (e) {
          const { status, code, message, detailIsString } = parseError(e);
          const flags: Record<string, unknown> = {};
          if (status === 404 && code === "project_not_found") {
            flags.not_found = true;
            return JSON.stringify(
              { success: false, error: "No project with this id that you are working on.", ...flags },
              null,
              2,
            );
          }
          return JSON.stringify(
            { success: false, error: explain(status, code, detailIsString), detail: message },
            null,
            2,
          );
        }
      },
    },

    // ========================================================================
    list_project_tasks: {
      name: "list_project_tasks",
      description:
        "List a project's tasks. By default only open ones (not done, not awaiting verification); " +
        "pass status 'all', 'done' or a single status to see others. Each task has an id like T-003, " +
        "a title, status, owner, the executing agent and priority.",
      parameters: z.object({
        project_id: projectId,
        status: z.string().max(32).optional().describe("open (default), all, or one task status."),
      }),
      execute: async (p: { project_id: string; status?: string }, context?: { session?: McpAuthContext }) => {
        try {
          const tasks = await getClient(context?.session).listProjectTasks(p.project_id, p.status || "open", turnOf(context));
          return ok({ count: tasks.length, tasks });
        } catch (e) {
          return fail(e);
        }
      },
    },

    // ========================================================================
    create_project_task: {
      name: "create_project_task",
      description:
        "Add a task to a project you are working on. Give it a short title, and where you can an " +
        "objective and a definition of done (a checklist). It gets the next id (T-NNN). Priority is " +
        "set by people only; your tasks start at p2.",
      parameters: z.object({
        project_id: projectId,
        title: z.string().min(1).max(200),
        objective: z.string().max(4000).optional(),
        done_definition: z.string().max(4000).optional().describe("Markdown checklist, e.g. '- [ ] Brief reviewed'."),
        context: z.string().max(4000).optional(),
        owner: z.string().max(320).optional().describe("Who is accountable (a person's email or a name)."),
        assignee: z.string().max(320).optional().describe("The agent doing the work, if not you."),
        waiting_on: z.string().max(320).optional(),
      }),
      execute: async (p: Record<string, string> & { project_id: string }, context?: { session?: McpAuthContext }) => {
        const { project_id, assignee, ...rest } = p;
        const body = assignee ? { ...rest, agent: assignee } : rest;
        try {
          return ok(await getClient(context?.session).createProjectTask(project_id, body, turnOf(context)));
        } catch (e) {
          return fail(e);
        }
      },
    },

    // ========================================================================
    update_project_task: {
      name: "update_project_task",
      description:
        "Update a project task. To claim a task is finished, set status 'pending-verification' and put " +
        "your evidence in 'note' (it is logged as your done claim); a person or the project's steward " +
        "then verifies it and sets 'done'. A done task can only be reopened by a person. You cannot " +
        "change priority. Any 'note' is appended to the task's log.",
      parameters: z.object({
        project_id: projectId,
        task_id: taskId,
        status: statusEnum.optional(),
        title: z.string().min(1).max(200).optional(),
        owner: z.string().max(320).optional(),
        assignee: z.string().max(320).optional().describe("The agent doing the work."),
        waiting_on: z.string().max(320).optional(),
        objective: z.string().max(4000).optional(),
        done_definition: z.string().max(4000).optional(),
        context: z.string().max(4000).optional(),
        note: z.string().max(4000).optional().describe("Appended to the task log; your done claim when moving to pending-verification."),
      }),
      execute: async (p: Record<string, string> & { project_id: string; task_id: string }, context?: { session?: McpAuthContext }) => {
        const { project_id, task_id, assignee, ...rest } = p;
        const body = assignee ? { ...rest, agent: assignee } : rest;
        try {
          return ok(await getClient(context?.session).updateProjectTask(project_id, task_id, body, turnOf(context)));
        } catch (e) {
          return fail(e);
        }
      },
    },

    // ========================================================================
    add_project_task_note: {
      name: "add_project_task_note",
      description: "Append a note to a task's log (progress, a waiting-on, a hand-off). The log is append-only.",
      parameters: z.object({ project_id: projectId, task_id: taskId, body: z.string().min(1).max(4000) }),
      execute: async (p: { project_id: string; task_id: string; body: string }, context?: { session?: McpAuthContext }) => {
        try {
          return ok(await getClient(context?.session).addProjectTaskNote(p.project_id, p.task_id, p.body, turnOf(context)));
        } catch (e) {
          return fail(e);
        }
      },
    },

    // ========================================================================
    get_project_log: {
      name: "get_project_log",
      description:
        "Read a project's log: the decisions, deliverables, task changes, blockers and hand-offs that " +
        "people and agents recorded, oldest first within the page. get_project already includes the " +
        "latest 20; use this for more.",
      parameters: z.object({ project_id: projectId, limit: z.number().int().min(1).max(200).optional() }),
      execute: async (p: { project_id: string; limit?: number }, context?: { session?: McpAuthContext }) => {
        try {
          const entries = await getClient(context?.session).getProjectLog(p.project_id, p.limit ?? 50, turnOf(context));
          return ok({ count: entries.length, entries });
        } catch (e) {
          return fail(e);
        }
      },
    },

    // ========================================================================
    add_project_log_entry: {
      name: "add_project_log_entry",
      description:
        "Record one meaningful outcome in the project log, which every person and agent on the project " +
        "reads first. Write ONE entry per outcome — a decision made, a deliverable finished, a blocker, a " +
        "hand-off — never one per turn, and never a transcript. Task status changes are logged for you.",
      parameters: z.object({
        project_id: projectId,
        kind: z.enum(["decision", "deliverable", "blocker", "handoff", "note"]),
        body: z.string().min(1).max(4000).describe("What happened and what it means, in a few sentences."),
        task_id: z.string().max(16).optional().describe("The task this concerns, if any."),
      }),
      execute: async (p: { project_id: string; kind: string; body: string; task_id?: string }, context?: { session?: McpAuthContext }) => {
        const { project_id, ...body } = p;
        try {
          return ok(await getClient(context?.session).addProjectLogEntry(project_id, body, turnOf(context)));
        } catch (e) {
          return fail(e);
        }
      },
    },

    // ========================================================================
    link_to_project: {
      name: "link_to_project",
      description:
        "Put something you produced on a project so its members can find it: a file you shared, a " +
        "report you wrote, a decision you recorded, or an open ask you raised (an ask raised in a " +
        "chat that is already on the project is found without this). Only your own items, and only on " +
        "a project you are working on. Sharing with outside clients is a person's decision, not yours.",
      parameters: z.object({
        project_id: projectId,
        kind: z.enum(["file", "report", "decision", "ask"]),
        target_id: z.string().min(1).max(128).describe("The file, report, decision or ask id."),
      }),
      execute: async (p: { project_id: string; kind: string; target_id: string }, context?: { session?: McpAuthContext }) => {
        try {
          return ok(await getClient(context?.session).linkToProject(p.project_id, { kind: p.kind, target_id: p.target_id }, turnOf(context)));
        } catch (e) {
          return fail(e);
        }
      },
    },

    // ======================================================================== v3
    get_steward_digest: {
      name: "get_steward_digest",
      description:
        "What needs you on the projects you steward: for each, its health and whether an update is " +
        "due, tasks awaiting verification (verify or reopen them), tasks blocked or waiting on a " +
        "decision, tasks untouched for a week, open asks, and whether it has gone quiet. Start a " +
        "stewarding run here, then record health with set_project_health.",
      parameters: z.object({}),
      execute: async (_params: unknown, context?: { session?: McpAuthContext }) => {
        try {
          const projects = await getClient(context?.session).getStewardDigest(turnOf(context));
          return JSON.stringify({ success: true, count: projects.length, projects }, null, 2);
        } catch (e) {
          return fail(e);
        }
      },
    },

    // ================================================================ ent#588
    create_project: {
      name: "create_project",
      description:
        "Start a Workspace project for your owner (/project-init on Trinity): a name and the goal in " +
        "a sentence or two. Your owner becomes its creator and first member, you become its steward " +
        "and the agent on it, and only its members can see it — adding people or changing who can " +
        "see it stays your owner's. Needs the project-management permission an instance admin grants; " +
        "without it the call is refused with `project_management_not_permitted` and nothing is created. " +
        "Returns the project; work on it with the task and log tools.",
      parameters: z.object({
        name: z.string().min(1).max(200).describe("Short project name, e.g. 'Q4 launch'."),
        goal: z.string().min(1).max(2000).describe("What done looks like, in a sentence or two."),
        tracker_url: z.string().max(2000).optional().describe("An external tracker for its tasks, if it has one."),
      }),
      execute: async (p: { name: string; goal: string; tracker_url?: string }, context?: { session?: McpAuthContext }) => {
        try {
          const body: Record<string, unknown> = { name: p.name, goal: p.goal };
          if (p.tracker_url) body.tracker_url = p.tracker_url;
          return ok({ project: await getClient(context?.session).createProjectAsAgent(body, turnOf(context)) });
        } catch (e) {
          return fail(e);
        }
      },
    },

    // ========================================================================
    import_project: {
      name: "import_project",
      description:
        "Bring one of your own folder projects onto the platform, once (/project-init adopt on " +
        "Trinity): `project_files/<slug>` or `<canon>/projects/<slug>` with its project.md, task " +
        "files, log and decisions. Afterwards the platform is its home and nothing syncs back; a " +
        "second import of the same folder is refused. Needs the project-management permission; " +
        "your owner becomes the project's creator, you its steward.",
      parameters: z.object({
        path: z.string().min(1).max(300).describe("The project folder in your files, e.g. 'project_files/q4-launch'."),
      }),
      execute: async (p: { path: string }, context?: { session?: McpAuthContext }) => {
        try {
          return ok({ project: await getClient(context?.session).importProjectAsAgent(p.path, turnOf(context)) });
        } catch (e) {
          return fail(e);
        }
      },
    },

    // ========================================================================
    set_project_health: {
      name: "set_project_health",
      description:
        "As a project's steward, record how it is going: on-track, at-risk or off-track, with one " +
        "line saying why. It shows on the project and in the list, and is added to the project log. " +
        "Update it at least every two weeks, and whenever it changes.",
      parameters: z.object({
        project_id: projectId,
        state: z.enum(["on-track", "at-risk", "off-track"]),
        note: z.string().max(280).optional().describe("One line: why, e.g. 'Vendor two weeks late.'"),
      }),
      execute: async (p: { project_id: string; state: string; note?: string }, context?: { session?: McpAuthContext }) => {
        try {
          return ok(await getClient(context?.session).setProjectHealth(
            p.project_id, { state: p.state, note: p.note ?? "" }, turnOf(context)));
        } catch (e) {
          return fail(e);
        }
      },
    },
  };
}
