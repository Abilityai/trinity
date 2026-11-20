/**
 * trinity-enterprise#1322: the Workspace project routes, called with a person's
 * own user-scoped key. The backend resolves that key to the person
 * (`get_portal_principal`) and applies their Workspace permissions, so the
 * person acts as themself: no agent, no act-as header, no turn.
 *
 * The read half mirrors the agent door's methods (`TrinityClient` project
 * methods) by name and return shape, so a read tool calls the same method on
 * whichever door the key's scope picks.
 */
import type { TrinityClient } from "../client.js";

const BASE = "/api/enterprise/projects";
type Row = Record<string, unknown>;

export class PersonProjectClient {
  constructor(private readonly client: TrinityClient) {}

  private get<T>(path: string): Promise<T> {
    return this.client.request<T>("GET", path);
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
}
