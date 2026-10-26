/**
 * Nevermined Payment Tools (NVM-001)
 *
 * MCP tools for managing Nevermined x402 payment configuration:
 * - configure_nevermined: Set up payment config for an agent
 * - get_nevermined_config: Read payment config
 * - toggle_nevermined: Enable/disable payments
 * - get_nevermined_payments: Payment history
 * - get_agent_spending: What the agent can still spend from its Nevermined card mandate (ent#966)
 * - revoke_agent_delegation: Revoke the agent's Nevermined card mandate (ent#966)
 */

import { z } from "zod";
import { ApiError, TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";
import { accessDenied } from "../access.js";

/**
 * ent#966 (M6): the scopes that may see `revoke_agent_delegation`. An
 * ALLOW-list (#848 — a null or unknown scope fails closed). The backend's
 * `require_person` refuses every agent key, so an agent session is never shown
 * a tool it cannot call.
 */
const REVOKE_DELEGATION_SCOPES: ReadonlySet<string> = new Set(["user", "system"]);
export const revokeDelegationAccess = (auth: any): boolean =>
  REVOKE_DELEGATION_SCOPES.has(auth?.scope ?? "");

/**
 * The backend `detail` of a refusal, readable: a string, or the
 * `{code, message}` envelope the delegation routes and `require_person` use.
 * Never the raw body — a 403 without a JSON `detail` gets a fixed sentence.
 */
function refusalDetail(error: ApiError): { message: string; code?: string } {
  try {
    const parsed = JSON.parse(error.body);
    const d = parsed?.detail;
    if (typeof d === "string") return { message: d };
    if (d && typeof d.message === "string") {
      return { message: d.message, ...(typeof d.code === "string" ? { code: d.code } : {}) };
    }
  } catch {
    // not JSON — keep the fixed sentence; the body is never echoed
  }
  return { message: `Access denied (HTTP ${error.status})` };
}

const AGENT_NOT_FOUND = { error: "agent_not_found_or_no_access" };

/**
 * Create Nevermined payment tools with the given client
 */
export function createNeverminedTools(
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
    // configure_nevermined - Set up payment config for an agent
    // ========================================================================
    configureNevermined: {
      name: "configure_nevermined",
      description:
        "Configure Nevermined x402 payment settings for an agent. " +
        "Requires owner or admin access. The NVM API key is encrypted at rest. " +
        "Key format: 'env:jwt' (e.g., 'sandbox:eyJhbGci...').",
      parameters: z.object({
        agent_name: z.string().describe("Agent name to configure"),
        nvm_api_key: z.string().describe("NVM API Key in 'env:jwt' format"),
        nvm_environment: z
          .enum(["sandbox", "live", "staging_sandbox", "staging_live", "custom"])
          .default("sandbox")
          .describe("Nevermined environment"),
        nvm_agent_id: z.string().describe("Registered agent ID from Nevermined"),
        nvm_plan_id: z.string().describe("Registered plan ID from Nevermined"),
        credits_per_request: z.number().int().min(1).default(1).describe("Credits to charge per request"),
      }),
      execute: async (
        params: {
          agent_name: string;
          nvm_api_key: string;
          nvm_environment: string;
          nvm_agent_id: string;
          nvm_plan_id: string;
          credits_per_request: number;
        },
        context?: { session?: McpAuthContext }
      ) => {
        const apiClient = getClient(context?.session);
        const result = await apiClient.configureNevermined(params.agent_name, {
          nvm_api_key: params.nvm_api_key,
          nvm_environment: params.nvm_environment,
          nvm_agent_id: params.nvm_agent_id,
          nvm_plan_id: params.nvm_plan_id,
          credits_per_request: params.credits_per_request,
        });
        return JSON.stringify({ success: true, config: result }, null, 2);
      },
    },

    // ========================================================================
    // get_nevermined_config - Read payment config
    // ========================================================================
    getNeverminedConfig: {
      name: "get_nevermined_config",
      description:
        "Get Nevermined payment configuration for an agent. " +
        "Returns config details (no decrypted API key). " +
        "Requires owner or admin access.",
      parameters: z.object({
        agent_name: z.string().describe("Agent name to query"),
      }),
      execute: async (
        params: { agent_name: string },
        context?: { session?: McpAuthContext }
      ) => {
        const apiClient = getClient(context?.session);
        try {
          const result = await apiClient.getNeverminedConfig(params.agent_name);
          return JSON.stringify(result, null, 2);
        } catch (error: any) {
          // #186: the backend read-access check now returns a uniform 404 for a
          // non-existent OR inaccessible agent (was 403 for inaccessible). So an
          // inaccessible agent is reported here as `configured: false`, same as a
          // genuinely-unconfigured one — intentional and enumeration-safe (the
          // caller cannot distinguish inaccessible / non-existent / unconfigured).
          if (error?.response?.status === 404) {
            return JSON.stringify({ configured: false, agent_name: params.agent_name }, null, 2);
          }
          throw error;
        }
      },
    },

    // ========================================================================
    // toggle_nevermined - Enable/disable payments
    // ========================================================================
    toggleNevermined: {
      name: "toggle_nevermined",
      description:
        "Enable or disable Nevermined payments for an agent. " +
        "Agent must have a Nevermined config before enabling. " +
        "Requires owner or admin access.",
      parameters: z.object({
        agent_name: z.string().describe("Agent name"),
        enabled: z.boolean().describe("Whether to enable (true) or disable (false) payments"),
      }),
      execute: async (
        params: { agent_name: string; enabled: boolean },
        context?: { session?: McpAuthContext }
      ) => {
        const apiClient = getClient(context?.session);
        const result = await apiClient.toggleNevermined(params.agent_name, params.enabled);
        return JSON.stringify(result, null, 2);
      },
    },

    // ========================================================================
    // get_nevermined_payments - Payment history
    // ========================================================================
    getNeverminedPayments: {
      name: "get_nevermined_payments",
      description:
        "Get payment history for an agent. " +
        "Shows verify, settle, settle_failed, and reject events. " +
        "Requires owner or admin access.",
      parameters: z.object({
        agent_name: z.string().describe("Agent name to query"),
        limit: z.number().int().min(1).max(200).default(50).describe("Max entries to return"),
      }),
      execute: async (
        params: { agent_name: string; limit: number },
        context?: { session?: McpAuthContext }
      ) => {
        const apiClient = getClient(context?.session);
        const result = await apiClient.getNeverminedPayments(params.agent_name, params.limit);
        return JSON.stringify({ payments: result, count: result.length }, null, 2);
      },
    },

    // ========================================================================
    // get_agent_spending - Live budget of the agent's Nevermined card mandate (ent#966)
    // ========================================================================
    getAgentSpending: {
      name: "get_agent_spending",
      description:
        "Read what this agent can still spend from its Nevermined card mandate " +
        "(remaining cents, expiry, purchases used) — live from Nevermined, " +
        "'unknown' if Nevermined is unreachable. Agent keys: own agent only.",
      parameters: z.object({
        agent_name: z.string().describe("Agent whose Nevermined spending to read"),
      }),
      execute: async (
        params: { agent_name: string },
        context?: { session?: McpAuthContext }
      ) => {
        const apiClient = getClient(context?.session);
        try {
          // 200 in every non-error state (configured:false, funded:false,
          // live.status:"unknown") — the body is passed through as-is.
          const result = await apiClient.getAgentSpending(params.agent_name);
          return JSON.stringify(result, null, 2);
        } catch (error) {
          // Uniform 404 (#186): missing, inaccessible, or a sibling read with an
          // agent key — one answer, never "configured: false".
          if (error instanceof ApiError && error.status === 404) {
            return JSON.stringify(AGENT_NOT_FOUND, null, 2);
          }
          throw error;
        }
      },
    },

    // ========================================================================
    // revoke_agent_delegation - Revoke the agent's Nevermined card mandate (ent#966)
    // ========================================================================
    revokeAgentDelegation: {
      name: "revoke_agent_delegation",
      description:
        "Revoke the agent's Nevermined card mandate. Immediate: Nevermined refuses " +
        "the agent's next paid call; Trinity removes NVM_DELEGATION_ID from the agent. " +
        "Owner or admin, human keys only.",
      canAccess: revokeDelegationAccess,
      parameters: z.object({
        agent_name: z.string().describe("Agent whose Nevermined card mandate to revoke"),
      }),
      execute: async (
        params: { agent_name: string },
        context?: { session?: McpAuthContext }
      ) => {
        const apiClient = getClient(context?.session);
        try {
          const result = await apiClient.revokeAgentDelegation(params.agent_name);
          return JSON.stringify(result, null, 2);
        } catch (error) {
          if (!(error instanceof ApiError)) throw error;
          if (error.status === 404) {
            const { code, message } = refusalDetail(error);
            // An agent with no active mandate is a named 404; anything else is
            // the uniform agent 404.
            if (code === "no_active_delegation") {
              return JSON.stringify({ success: false, error: message, code }, null, 2);
            }
            return JSON.stringify(AGENT_NOT_FOUND, null, 2);
          }
          if (error.status === 403) {
            const { code, message } = refusalDetail(error);
            return accessDenied(context, {
              success: false,
              error: message,
              ...(code ? { code } : {}),
              not_authorized: true,
            });
          }
          throw error;
        }
      },
    },
  };
}
