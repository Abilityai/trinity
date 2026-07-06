/**
 * Nevermined Payment Tools (NVM-001)
 *
 * MCP tools for managing Nevermined x402 payment configuration:
 * - configure_nevermined: Set up payment config for an agent
 * - get_nevermined_config: Read payment config
 * - toggle_nevermined: Enable/disable payments
 * - get_nevermined_payments: Payment history
 */

import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

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
  };
}
