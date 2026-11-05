/**
 * Subscription Management Tools (SUB-001)
 *
 * MCP tools for managing Claude Max/Pro subscription credentials:
 * - register_subscription: Register OAuth credentials (admin)
 * - list_subscriptions: List all subscriptions with agents
 * - assign_subscription: Assign to an agent
 * - get_agent_auth: Get auth status for an agent
 * - delete_subscription: Delete a subscription (admin)
 */

import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";
import { HUMAN_ONLY_WHO_MAY_CALL, humanOnlyToolAccess } from "../access.js";

/**
 * Create subscription management tools with the given client
 */
export function createSubscriptionTools(
  client: TrinityClient,
  requireApiKey: boolean
) {
  /**
   * Get Trinity client with appropriate authentication
   */
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
    // register_subscription - Register OAuth credentials (admin)
    // ========================================================================
    registerSubscription: {
      name: "register_subscription",
      description:
        "Register a Claude Max/Pro subscription token. " +
        "Admin-only. Takes a long-lived token from `claude setup-token`. " +
        "If a subscription with the same name exists, it will be updated. " +
        "Token must start with 'sk-ant-oat01-'.",
      parameters: z.object({
        name: z.string().describe("Unique name for the subscription (e.g., 'eugene-max')"),
        token: z.string().describe("Long-lived token from `claude setup-token` (sk-ant-oat01-...)"),
        subscription_type: z.string().optional().describe("Type: 'max' or 'pro'"),
        rate_limit_tier: z.string().optional().describe("Rate limit tier if known"),
      }),
      execute: async (
        params: {
          name: string;
          token: string;
          subscription_type?: string;
          rate_limit_tier?: string;
        },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        const result = await apiClient.registerSubscription(
          params.name,
          params.token,
          params.subscription_type,
          params.rate_limit_tier
        );

        return JSON.stringify({
          success: true,
          message: `Subscription '${params.name}' registered successfully`,
          subscription: {
            id: result.id,
            name: result.name,
            subscription_type: result.subscription_type,
            owner_email: result.owner_email,
          },
        }, null, 2);
      },
    },

    // ========================================================================
    // list_subscriptions - List all subscriptions with agents
    // ========================================================================
    listSubscriptions: {
      name: "list_subscriptions",
      description:
        "List all registered subscriptions with their assigned agents. " +
        "Admin-only. Shows subscription names, types, and which agents are using each.",
      parameters: z.object({}),
      execute: async (
        _params: unknown,
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        const subscriptions = await apiClient.listSubscriptions();

        if (subscriptions.length === 0) {
          return JSON.stringify({
            message: "No subscriptions registered. Use register_subscription to add one.",
            subscriptions: [],
          }, null, 2);
        }

        return JSON.stringify({
          count: subscriptions.length,
          subscriptions: subscriptions.map(s => ({
            name: s.name,
            id: s.id,
            subscription_type: s.subscription_type || "unknown",
            owner_email: s.owner_email,
            agent_count: s.agent_count,
            agents: s.agents,
          })),
        }, null, 2);
      },
    },

    // ========================================================================
    // assign_subscription - Assign to an agent
    // ========================================================================
    assignSubscription: {
      name: "assign_subscription",
      description:
        "Assign a subscription to an agent. " +
        "If the agent is running, credentials are injected immediately. " +
        "The agent will use the subscription's OAuth credentials for Claude authentication. " +
        HUMAN_ONLY_WHO_MAY_CALL.interactive +
        " Assign it under Settings, Integrations, Claude Subscriptions in the web UI instead.",
      canAccess: humanOnlyToolAccess("interactive"),
      parameters: z.object({
        agent_name: z.string().describe("Name of the agent to assign subscription to"),
        subscription_name: z.string().describe("Name of the subscription to assign"),
      }),
      execute: async (
        { agent_name, subscription_name }: { agent_name: string; subscription_name: string },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        const result = await apiClient.assignSubscription(agent_name, subscription_name);

        return JSON.stringify({
          success: result.success,
          message: result.message,
          agent_name: result.agent_name,
          subscription_name: result.subscription_name,
          injection_status: result.injection_result?.status || "not_attempted",
        }, null, 2);
      },
    },

    // ========================================================================
    // clear_agent_subscription - Clear subscription from agent
    // ========================================================================
    clearAgentSubscription: {
      name: "clear_agent_subscription",
      description:
        "Clear subscription assignment from an agent. " +
        "Agent will fall back to API key authentication. " +
        HUMAN_ONLY_WHO_MAY_CALL.person,
      canAccess: humanOnlyToolAccess("person"),
      parameters: z.object({
        agent_name: z.string().describe("Name of the agent to clear subscription from"),
      }),
      execute: async (
        { agent_name }: { agent_name: string },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        const result = await apiClient.clearAgentSubscription(agent_name);

        return JSON.stringify({
          success: result.success,
          message: result.message,
          agent_name: result.agent_name,
          previous_subscription: result.previous_subscription || "none",
        }, null, 2);
      },
    },

    // ========================================================================
    // get_agent_auth - Get auth status for an agent
    // ========================================================================
    getAgentAuth: {
      name: "get_agent_auth",
      description:
        "Get the authentication status for an agent. " +
        "Shows whether the agent is using subscription, API key, or not configured.",
      parameters: z.object({
        agent_name: z.string().describe("Name of the agent to check"),
      }),
      execute: async (
        { agent_name }: { agent_name: string },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        const status = await apiClient.getAgentAuth(agent_name);

        const modeDescriptions = {
          subscription: "Using Claude Max/Pro subscription (OAuth)",
          api_key: "Using platform API key (ANTHROPIC_API_KEY)",
          not_configured: "No authentication configured",
        };

        return JSON.stringify({
          agent_name: status.agent_name,
          auth_mode: status.auth_mode,
          description: modeDescriptions[status.auth_mode],
          subscription_name: status.subscription_name || null,
          has_api_key: status.has_api_key,
        }, null, 2);
      },
    },

    // ========================================================================
    // delete_subscription - Delete a subscription (admin)
    // ========================================================================
    deleteSubscription: {
      name: "delete_subscription",
      description:
        "Delete a subscription credential. " +
        "Admin-only. All assigned agents will fall back to API key authentication.",
      parameters: z.object({
        subscription_name: z.string().describe("Name of the subscription to delete"),
      }),
      execute: async (
        { subscription_name }: { subscription_name: string },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        const result = await apiClient.deleteSubscription(subscription_name);

        return JSON.stringify({
          success: result.success,
          message: result.message,
          agents_cleared: result.agents_cleared || [],
        }, null, 2);
      },
    },
  };
}
