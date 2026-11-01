/**
 * Permission edges and self-change grants — read-only (trinity-enterprise#991)
 *
 * Two observations a reconciler cannot make under an agent key otherwise: which
 * agents an agent may call, and which self-change permissions it holds. Both are
 * reads over existing routes; granting stays a human-only surface (Invariant #8,
 * grant-vs-use), so there are no write tools here.
 *
 * An error is never an empty answer: "no edges" and "no grants" are facts about
 * the agent, and a fault or a refusal must not be mistaken for either.
 */

import { z } from "zod";
import { ApiError, TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

/** What the edge read can see: an edge whose target has no container, or lies
 *  outside the caller's reach, is not listed — absent here is not removed. */
const OBSERVED_WITHIN = "caller_reach_with_container";

const GRANTS_REFUSED = (agent: string) =>
  `Refused: the capability grants of '${agent}' are not readable with this key. ` +
  `An agent key reads only itself or an agent it spawned — never through a permission edge; ` +
  `a person key reads only as the agent's owner or an admin. ` +
  `This is a refusal, not 'no grants'. (A nonexistent agent reads the same: the 404 is uniform.)`;

/**
 * Create the permission-read tools with the given client
 * @param client - Base Trinity client (provides base URL)
 * @param requireApiKey - Whether API key authentication is enabled
 */
export function createPermissionTools(
  client: TrinityClient,
  requireApiKey: boolean
) {
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
    // get_agent_permissions - the agents an agent may call
    // ========================================================================
    getAgentPermissions: {
      name: "get_agent_permissions",
      description:
        "Get an agent's permission edges: the agents it is permitted to call, with each one's status. " +
        "Read-only; edges are granted in the Trinity UI. " +
        "An agent key may read itself, an agent it has an edge to, or one it spawned. " +
        "The list is observed within the caller's view (`observed_within`): an edge whose target has no " +
        "container, or lies outside your reach, is not listed — a missing edge is not a removed edge. " +
        "Fails with an error when Docker cannot be read, rather than answering 'no edges'.",
      parameters: z.object({
        agent_name: z.string().describe("The agent whose permission edges to read"),
      }),
      execute: async (
        { agent_name }: { agent_name: string },
        context?: { session?: McpAuthContext }
      ) => {
        const apiClient = getClient(context?.session);
        const result = await apiClient.getAgentPermissionsStrict(agent_name);
        return JSON.stringify(
          {
            source_agent: result.source_agent,
            permitted_agents: result.permitted_agents,
            observed_within: OBSERVED_WITHIN,
          },
          null,
          2
        );
      },
    },

    // ========================================================================
    // get_agent_capability_grants - the self-change permissions an agent holds
    // ========================================================================
    getAgentCapabilityGrants: {
      name: "get_agent_capability_grants",
      description:
        "Get an agent's self-change permissions: all five capabilities, each with granted true/false, " +
        "who granted it and when. Read-only; granting is admin-only in the Trinity UI. " +
        "An agent key reads itself or an agent it spawned — never through a permission edge. " +
        "A person key reads as the agent's owner or an admin. " +
        "Any other target is refused with an error, which is not the same as 'no grants'.",
      parameters: z.object({
        agent_name: z.string().describe("The agent whose capability grants to read"),
      }),
      execute: async (
        { agent_name }: { agent_name: string },
        context?: { session?: McpAuthContext }
      ) => {
        const apiClient = getClient(context?.session);
        try {
          const result = await apiClient.getAgentCapabilityGrants(agent_name);
          return JSON.stringify(result, null, 2);
        } catch (err) {
          if (err instanceof ApiError && err.status === 404) {
            throw new Error(GRANTS_REFUSED(agent_name));
          }
          throw err;
        }
      },
    },
  };
}
