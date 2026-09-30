/**
 * Workspace Projects read tools (trinity-enterprise#661).
 *
 * A project links the Workspace chats and rooms where a piece of work happens.
 * An agent's turn in a linked chat carries a one-line "[Project] …" note with
 * the project id; these tools let the agent read what that project is for.
 * Read-only by design: agents do not create or restructure projects.
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
  if (status === 403 && detailIsString) return "Projects are not licensed for this instance.";
  if (status === 404) return "Projects are not available on this platform.";
  return "Projects could not be reached.";
}

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
          const projects = await getClient(context?.session).listMyProjects();
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
          const project = await getClient(context?.session).getMyProject(params.project_id);
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
  };
}
