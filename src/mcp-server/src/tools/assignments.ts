/**
 * Role-assignment read tool (trinity-enterprise#500).
 *
 * An *assignment* records which human fills which business role for an agent,
 * and which of those humans the agent primarily serves. Roles themselves live
 * in the team's canon repository; Trinity records only who fills them today.
 *
 * Read-only by construction. Writes are NOT exposed here and must not be added:
 * creating an assignment is a GRANT, and the backend refuses every non-
 * interactive principal on the write endpoints — an MCP write tool would be a
 * tool that can only ever fail.
 *
 * ── License-blind by design ────────────────────────────────────────────────
 * This module is OSS-core and knows nothing about entitlement. It proxies the
 * route and reports what the route says.
 *
 * ── What the tool can and cannot tell apart ────────────────────────────────
 * Deliberately narrower than `credential_vault.ts`. That module branches on the
 * detail SHAPE (a `{code, message}` dict vs a plain string) because its backend
 * raises coded refusals. This route raises none: its only failures are the
 * entitlement gate (403, plain-string detail) and a UNIFORM 404 that covers
 * "route absent" (a build with no assignments module) and "no such agent /
 * no access" alike. That 404 is uniform on purpose — Invariant #8 enumeration
 * safety — so the tool merges those cases in its message rather than claiming a
 * distinction it does not have.
 *
 * ── Gating ────────────────────────────────────────────────────────────────
 * Advertisement: registered in `server.ts`'s `toolGroups`, i.e. the
 * `operatorOnly` ALLOW-list `{user, agent, system}`. `agent` is in that set,
 * which is exactly why the backend self-scopes an agent principal to its own
 * roster: advertisement is not authorization, and an agent-scoped key resolves
 * to its owner carrying the owner's role.
 */

import { z } from "zod";
import { TrinityClient, ApiError } from "../client.js";
import type { McpAuthContext } from "../types.js";

export function createAssignmentTools(
  client: TrinityClient,
  requireApiKey: boolean,
) {
  const getClient = (authContext?: McpAuthContext): TrinityClient => {
    if (requireApiKey) {
      if (!authContext?.mcpApiKey) {
        throw new Error(
          "MCP API key authentication required but no API key found in request context",
        );
      }
      const userClient = new TrinityClient(client.getBaseUrl());
      userClient.setToken(authContext.mcpApiKey);
      return userClient;
    }
    return client;
  };

  /**
   * Degrade to the `enabled:false` shape (the `list_runnable_skills` contract)
   * rather than throwing — a thrown error reaches the agent as an opaque
   * transport failure it cannot reason about, and "who do I work for?" is a
   * question the agent should be able to answer "nobody has told me" to.
   */
  const degrade = (agentName: string, error: unknown): string => {
    const status = error instanceof ApiError ? error.status : undefined;
    const raw = error instanceof Error ? error.message : String(error);
    let human: string;
    if (status === 404) {
      human =
        "No assignment record is available for that agent — either this platform " +
        "does not have the assignments module, or the agent does not exist or is " +
        "not one you can read.";
    } else if (status === 403) {
      human = "Role assignments are not licensed for this instance.";
    } else {
      human = "The assignment record could not be reached.";
    }
    return JSON.stringify(
      { enabled: false, agent_name: agentName, message: human, detail: raw },
      null,
      2,
    );
  };

  return {
    getAgentAssignments: {
      name: "get_agent_assignments",
      description:
        "Read the role assignments recorded for an agent: the primary human the agent " +
        "serves, the business role they fill, and the other stakeholders (approver, " +
        "collaborator, viewer). Call it with your OWN agent name to find out who you work " +
        "for — an agent may only read its own roster. Roles live in your team's canon " +
        "repository; this returns who fills them today, plus whether the recorded role " +
        "file has drifted since the assignment was made. Read-only: assignments are " +
        "written by instance admins, never by an agent.",
      parameters: z.object({
        agent_name: z
          .string()
          .describe(
            "The agent whose assignments to read. As an agent, use your own name.",
          ),
      }),
      execute: async (
        { agent_name }: { agent_name: string },
        context?: { session?: McpAuthContext },
      ) => {
        try {
          const result = await getClient(context?.session).getAgentAssignments(
            agent_name,
          );
          return JSON.stringify(result, null, 2);
        } catch (e) {
          return degrade(agent_name, e);
        }
      },
    },
  };
}
