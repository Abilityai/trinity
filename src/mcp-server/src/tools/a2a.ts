/**
 * A2A Control Tools (trinity-enterprise#160)
 *
 * The MCP surface for the A2A interoperability *management* plane — the third
 * surface (Invariant #13) alongside the enterprise backend router
 * (`/api/enterprise/a2a/*`) and the config UI (#158). Distinct from the runtime
 * outbound call (`call_a2a_agent`, abilityai/trinity#736): these tools toggle
 * exposure, read the served card, manage the inbound allow-list, and register
 * outbound endpoints.
 *
 * Gating is enforced at the backend it proxies:
 *  - every management route is `requires_entitlement("a2a")` → a 403 in an
 *    unentitled build, a 404 in an OSS-only build. Either way these tools return
 *    a structured `{ success: false, not_entitled: true }` — never a silent
 *    success.
 *  - mutating routes (exposure, allow-list, endpoints) are owner/admin AND
 *    human-only (`reject_agent_principal`) — an agent-scoped key gets a 403.
 *  - outbound credentials are write-only: they are accepted on register but the
 *    backend never returns them (only `has_credentials`), so no tool echoes a
 *    secret back.
 */

import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

export function createA2ATools(client: TrinityClient, requireApiKey: boolean) {
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

  /** Uniform error → structured result with honest gating flags (never throws). */
  const fail = (error: unknown): string => {
    const message = error instanceof Error ? error.message : String(error);
    const flags: Record<string, boolean> = {};
    // Unentitled build (403 "not licensed") or OSS-only build (404, route absent).
    if (/not licensed/i.test(message) || /\ba2a\b/i.test(message) && /\b403\b/.test(message)) {
      flags.not_entitled = true;
    }
    if (/\b404\b/.test(message) && !flags.not_entitled) flags.not_found = true;
    if (/human-only/i.test(message)) flags.human_only = true;
    if (/\b403\b/.test(message) && !flags.not_entitled && !flags.human_only) flags.not_authorized = true;
    if (/\b422\b/.test(message)) flags.invalid = true;
    return JSON.stringify({ success: false, error: message, ...flags }, null, 2);
  };

  const ok = (data: unknown): string => JSON.stringify({ success: true, ...(data as object) }, null, 2);

  return {
    // ========================================================================
    get_agent_a2a_config: {
      name: "get_agent_a2a_config",
      description:
        "Get an agent's A2A (agent-to-agent) control state: whether it is exposed over A2A, " +
        "its public Agent Card URL, advertised skills, the inbound identity allow-list, and the " +
        "registered outbound endpoints (credentials are never returned — only whether each has any). " +
        "Enterprise feature; returns { not_entitled: true } if A2A is not licensed for this instance.",
      parameters: z.object({
        agent_name: z.string().describe("The agent whose A2A config to read."),
      }),
      execute: async (params: { agent_name: string }, context?: { session?: McpAuthContext }) => {
        try {
          return ok({ config: await getClient(context?.session).getA2AConfig(params.agent_name) });
        } catch (e) {
          return fail(e);
        }
      },
    },

    // ========================================================================
    set_agent_a2a_exposure: {
      name: "set_agent_a2a_exposure",
      description:
        "Toggle whether an agent is exposed over A2A (agent-to-agent interoperability). " +
        "Owner/admin and human-only — an agent-scoped key is rejected. " +
        "Enterprise feature; returns { not_entitled: true } if A2A is not licensed.",
      parameters: z.object({
        agent_name: z.string().describe("The agent to expose or unexpose."),
        enabled: z.boolean().describe("true to expose the agent over A2A, false to unexpose."),
      }),
      execute: async (params: { agent_name: string; enabled: boolean }, context?: { session?: McpAuthContext }) => {
        try {
          return ok({ config: await getClient(context?.session).setA2AExposure(params.agent_name, params.enabled) });
        } catch (e) {
          return fail(e);
        }
      },
    },

    // ========================================================================
    get_agent_a2a_card: {
      name: "get_agent_a2a_card",
      description:
        "Return the served A2A Agent Card JSON for an agent (the OSS discovery card). " +
        "Read access follows the standard agent-access gate.",
      parameters: z.object({
        agent_name: z.string().describe("The agent whose Agent Card to fetch."),
      }),
      execute: async (params: { agent_name: string }, context?: { session?: McpAuthContext }) => {
        try {
          return ok({ card: await getClient(context?.session).getA2ACard(params.agent_name) });
        } catch (e) {
          return fail(e);
        }
      },
    },

    // ========================================================================
    set_a2a_inbound_allowlist: {
      name: "set_a2a_inbound_allowlist",
      description:
        "Manage which external identities may task an agent over A2A inbound. Provide `add` and/or " +
        "`remove` (identity strings — e.g. a caller URL, client id, or key id). Owner/admin and human-only. " +
        "Enterprise feature; returns { not_entitled: true } if A2A is not licensed.",
      parameters: z.object({
        agent_name: z.string().describe("The agent whose inbound allow-list to modify."),
        add: z.array(z.string()).optional().describe("Identities to add to the allow-list."),
        remove: z.array(z.string()).optional().describe("Identities to remove from the allow-list."),
      }),
      execute: async (
        params: { agent_name: string; add?: string[]; remove?: string[] },
        context?: { session?: McpAuthContext },
      ) => {
        if (!params.add?.length && !params.remove?.length) {
          return JSON.stringify(
            { success: false, error: "Provide at least one identity in 'add' or 'remove'.", invalid: true },
            null, 2,
          );
        }
        try {
          const config = await getClient(context?.session).updateA2AInboundAllowlist(params.agent_name, {
            add: params.add,
            remove: params.remove,
          });
          return ok({ config });
        } catch (e) {
          return fail(e);
        }
      },
    },

    // ========================================================================
    register_a2a_endpoint: {
      name: "register_a2a_endpoint",
      description:
        "Register (or update by name) an outbound external A2A endpoint an agent may call — this feeds " +
        "the runtime call_a2a_agent (abilityai/trinity#736). Optional `credentials` are stored encrypted " +
        "and NEVER returned by any read. Owner/admin and human-only. " +
        "Enterprise feature; returns { not_entitled: true } if A2A is not licensed.",
      parameters: z.object({
        agent_name: z.string().describe("The calling agent that owns this outbound endpoint."),
        name: z.string().min(1).max(200).describe("Operator-facing label for the endpoint (unique per agent)."),
        url: z.string().describe("The external A2A endpoint / Agent Card URL (http/https)."),
        credentials: z.string().max(8192).optional().describe(
          "Optional secret (token/API key) for calling the endpoint. Stored encrypted; never echoed back.",
        ),
      }),
      execute: async (
        params: { agent_name: string; name: string; url: string; credentials?: string },
        context?: { session?: McpAuthContext },
      ) => {
        try {
          const endpoint = await getClient(context?.session).registerA2AEndpoint(params.agent_name, {
            name: params.name,
            url: params.url,
            credentials: params.credentials,
          });
          return ok({ endpoint });
        } catch (e) {
          return fail(e);
        }
      },
    },

    // ========================================================================
    list_a2a_endpoints: {
      name: "list_a2a_endpoints",
      description:
        "List an agent's registered outbound A2A endpoints (credentials are never returned — only " +
        "has_credentials). Enterprise feature; returns { not_entitled: true } if A2A is not licensed.",
      parameters: z.object({
        agent_name: z.string().describe("The agent whose outbound endpoints to list."),
      }),
      execute: async (params: { agent_name: string }, context?: { session?: McpAuthContext }) => {
        try {
          return ok({ endpoints: await getClient(context?.session).listA2AEndpoints(params.agent_name) });
        } catch (e) {
          return fail(e);
        }
      },
    },

    // ========================================================================
    remove_a2a_endpoint: {
      name: "remove_a2a_endpoint",
      description:
        "Remove one outbound A2A endpoint by id. Owner/admin and human-only. " +
        "Enterprise feature; returns { not_entitled: true } if A2A is not licensed.",
      parameters: z.object({
        agent_name: z.string().describe("The agent that owns the endpoint."),
        endpoint_id: z.string().describe("The id of the endpoint to remove (from list_a2a_endpoints)."),
      }),
      execute: async (
        params: { agent_name: string; endpoint_id: string },
        context?: { session?: McpAuthContext },
      ) => {
        try {
          const result = await getClient(context?.session).removeA2AEndpoint(params.agent_name, params.endpoint_id);
          return ok({ result });
        } catch (e) {
          return fail(e);
        }
      },
    },
  };
}
