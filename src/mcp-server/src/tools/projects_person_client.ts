/**
 * trinity-enterprise#1322: the Workspace project routes, called with a person's
 * own user-scoped key. The backend resolves that key to the person
 * (`get_portal_principal`) and applies their Workspace permissions, so the
 * person acts as themself: no agent, no act-as header, no turn.
 *
 * The methods mirror the agent door's (`TrinityClient` project methods) by
 * name and return shape, so a tool calls the same method on whichever door the
 * key's scope picks. Write bodies are the same as the agent door's.
 */
import type { TrinityClient } from "../client.js";

const BASE = "/api/enterprise/projects";
type Row = Record<string, unknown>;

export class PersonProjectClient {
  constructor(private readonly client: TrinityClient) {}

  private get<T>(path: string): Promise<T> {
    return this.client.request<T>("GET", path);
  }

  private send<T>(method: string, path: string, body: unknown): Promise<T> {
    return this.client.request<T>(method, path, body);
  }

  private path(projectId: string, tail = ""): string {
    return `${BASE}/${encodeURIComponent(projectId)}${tail}`;
  }

  /** The projects the person can see; archived ones only when asked. */
  async listMyProjects(_turn?: string, includeArchived = false): Promise<Row[]> {
    const res = await this.get<{ projects: Row[] }>(includeArchived ? `${BASE}?include_archived=true` : BASE);
    return res.projects;
  }

  async getMyProject(projectId: string): Promise<Row> {
    return this.get(this.path(projectId));
  }

  async listProjectTasks(projectId: string, status = "open", _turn?: string, view?: string): Promise<Row[]> {
    const viewQuery = view ? `&view=${encodeURIComponent(view)}` : "";
    const res = await this.get<{ tasks: Row[] }>(this.path(projectId, `/tasks?status=${encodeURIComponent(status)}${viewQuery}`));
    return res.tasks;
  }

  async getProjectLog(projectId: string, limit = 50): Promise<Row[]> {
    const res = await this.get<{ entries: Row[] }>(this.path(projectId, `/log?limit=${limit}`));
    return res.entries;
  }

  /** The person route returns the whole item; it takes no offset. */
  async readProjectContext(projectId: string, itemId: string): Promise<Row> {
    return this.get(this.path(projectId, `/context/${encodeURIComponent(itemId)}`));
  }

  async getStewardDigest(): Promise<Row[]> {
    const res = await this.get<{ projects: Row[] }>(`${BASE}/stewarding`);
    return res.projects;
  }

  async listProjectExecutions(projectId: string, filters: Record<string, string | number | undefined> = {}): Promise<Row> {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(filters)) {
      if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
    }
    return this.get(this.path(projectId, qs.toString() ? `/executions?${qs.toString()}` : "/executions"));
  }

  // --- writes (CP-P3) ---------------------------------------------------------

  async createProjectTask(projectId: string, body: Record<string, unknown>): Promise<Row> {
    return this.send("POST", this.path(projectId, "/tasks"), body);
  }

  async updateProjectTask(projectId: string, taskId: string, body: Record<string, unknown>): Promise<Row> {
    return this.send("PATCH", this.path(projectId, `/tasks/${encodeURIComponent(taskId)}`), body);
  }

  async addProjectTaskNote(projectId: string, taskId: string, body: string): Promise<Row> {
    return this.send("POST", this.path(projectId, `/tasks/${encodeURIComponent(taskId)}/log`), { body });
  }

  async addProjectLogEntry(projectId: string, body: Record<string, unknown>): Promise<Row> {
    return this.send("POST", this.path(projectId, "/log"), body);
  }

  async linkToProject(projectId: string, body: Record<string, unknown>): Promise<Row> {
    return this.send("POST", this.path(projectId, "/items"), body);
  }

  async setProjectHealth(projectId: string, body: Record<string, unknown>): Promise<Row> {
    return this.send("POST", this.path(projectId, "/health"), body);
  }

  /** The person becomes the project's creator and first member. */
  async createProject(body: Record<string, unknown>): Promise<Row> {
    return this.send("POST", BASE, body);
  }

  /** One of an agent's folder projects, imported by a person who can use that agent. */
  async importProject(agentName: string, path: string): Promise<Row> {
    return this.send("POST", `${BASE}/import`, { agent_name: agentName, path });
  }

  // --- person-only acts (CP-P4) ----------------------------------------------

  async lifecycle(projectId: string, action: "start" | "stop" | "finish" | "archive" | "restore", note?: string): Promise<Row> {
    if (action === "finish") return this.send("PATCH", this.path(projectId), { status: "done" });
    if (action === "start" || action === "stop") return this.send("POST", this.path(projectId, `/${action}`), note ? { note } : undefined);
    return this.send("POST", this.path(projectId, `/${action}`), undefined);
  }

  async updateProject(projectId: string, body: Record<string, unknown>): Promise<Row> {
    return this.send("PATCH", this.path(projectId), body);
  }

  async setStewardWaking(projectId: string, on: boolean): Promise<Row> {
    return this.send("POST", this.path(projectId, on ? "/steward/resume" : "/steward/pause"), undefined);
  }

  async addMember(projectId: string, email: string): Promise<Row> {
    return this.send("POST", this.path(projectId, "/members"), { email });
  }

  async removeMember(projectId: string, email: string): Promise<unknown> {
    return this.send("DELETE", this.path(projectId, `/members/${encodeURIComponent(email)}`), undefined);
  }

  async addAgent(projectId: string, agentName: string): Promise<Row> {
    return this.send("POST", this.path(projectId, "/agents"), { agent_name: agentName });
  }

  async removeAgent(projectId: string, agentName: string): Promise<unknown> {
    return this.send("DELETE", this.path(projectId, `/agents/${encodeURIComponent(agentName)}`), undefined);
  }

  async waitingRuns(projectId: string): Promise<Row[]> {
    const res = await this.get<{ runs: Row[] }>(this.path(projectId, "/runs/waiting"));
    return res.runs;
  }

  async decideRuns(projectId: string, decision: "approve" | "decline", ids: string[], note?: string): Promise<Row[]> {
    const body: Record<string, unknown> = { ids };
    if (decision === "decline" && note) body.note = note;
    const res = await this.send<{ results: Row[] }>("POST", this.path(projectId, `/runs/${decision}`), body);
    return res.results;
  }
}
