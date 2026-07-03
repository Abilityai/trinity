/**
 * Channel Group Tools (Issue #349)
 *
 * MCP tools for agents to discover and send proactive messages to
 * Telegram groups (and future channel types like Slack).
 */

import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

/**
 * Create channel tools with the given client
 * @param client - Base Trinity client (provides base URL)
 * @param requireApiKey - Whether API key authentication is enabled
 */
export function createChannelTools(
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

  /**
   * Get the agent name from auth context.
   * For agent-scoped keys, this is the bound agent.
   * For user-scoped keys, the agent must be specified in the call.
   */
  const getAgentName = (
    authContext: McpAuthContext | undefined,
    specifiedAgent?: string
  ): string => {
    // If an agent name is specified, use that (will be validated by backend)
    if (specifiedAgent) {
      return specifiedAgent;
    }
    // For agent-scoped keys, use the bound agent
    if (authContext?.scope === "agent" && authContext.agentName) {
      return authContext.agentName;
    }
    throw new Error(
      "Agent name is required. Either use an agent-scoped API key or specify the agent_name parameter."
    );
  };

  return {
    // ========================================================================
    // list_channel_groups - List groups the agent can message
    // ========================================================================
    listChannelGroups: {
      name: "list_channel_groups",
      description:
        "List channel groups (Telegram groups, Slack channels) that this agent is connected to. " +
        "Returns group IDs, names, and configuration for each group. " +
        "Use this to discover which groups you can send proactive messages to.",
      parameters: z.object({
        channel_type: z.enum(["telegram", "slack"])
          .describe("Channel type to list groups for: 'telegram' groups or 'slack' channels."),
        agent_name: z.string().optional()
          .describe(
            "Agent name to list groups for. Required for user-scoped API keys. " +
            "For agent-scoped keys, defaults to the calling agent."
          ),
      }),
      execute: async (
        params: {
          channel_type: "telegram" | "slack";
          agent_name?: string;
        },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        try {
          const agentName = getAgentName(authContext, params.agent_name);

          console.log(`[list_channel_groups] Listing ${params.channel_type} groups for ${agentName}`);

          if (params.channel_type === "telegram") {
            const groups = await apiClient.listTelegramGroups(agentName);

            // Filter to active groups only and format response
            const activeGroups = groups
              .filter(g => g.is_active)
              .map(g => ({
                channel_type: "telegram",
                chat_id: g.chat_id,
                chat_title: g.chat_title || "Unnamed Group",
                chat_type: g.chat_type,
                trigger_mode: g.trigger_mode,
              }));

            return JSON.stringify({
              success: true,
              agent_name: agentName,
              channel_type: params.channel_type,
              groups: activeGroups,
              count: activeGroups.length,
            }, null, 2);
          }

          if (params.channel_type === "slack") {
            const result = await apiClient.listSlackChannels(agentName);
            const groups = result.channels.map(c => ({
              channel_type: "slack",
              chat_id: c.channel_id,
              chat_title: c.channel_name || "Unnamed Channel",
              workspace_name: c.workspace_name,
              is_dm_default: c.is_dm_default,
            }));

            return JSON.stringify({
              success: true,
              agent_name: agentName,
              channel_type: params.channel_type,
              groups,
              count: groups.length,
            }, null, 2);
          }

          return JSON.stringify({
            success: false,
            error: `Unsupported channel type: ${params.channel_type}`,
          }, null, 2);
        } catch (error) {
          const errorMessage = error instanceof Error ? error.message : String(error);
          console.error(`[list_channel_groups] Error: ${errorMessage}`);
          return JSON.stringify({
            success: false,
            error: errorMessage,
          }, null, 2);
        }
      },
    },

    // ========================================================================
    // send_group_message - Send a proactive message to a group
    // ========================================================================
    sendGroupMessage: {
      name: "send_group_message",
      description:
        "Send a proactive message to a channel group (Telegram group, Slack channel). " +
        "Use this to broadcast updates, alerts, or information to groups you're connected to. " +
        "Rate limited to prevent spam: 10 messages/hour/group, 100 messages/hour/agent.",
      parameters: z.object({
        channel_type: z.enum(["telegram", "slack"])
          .describe("Channel type: 'telegram' group or 'slack' channel."),
        chat_id: z.string()
          .describe("The group/channel ID to send the message to. Get this from list_channel_groups."),
        message: z.string().max(4096)
          .describe("Message text to send (max 4096 characters). Telegram supports HTML; Slack supports mrkdwn."),
        thread_ts: z.string().optional()
          .describe("Slack only: post in an existing thread (the parent message's ts). Ignored for Telegram."),
        agent_name: z.string().optional()
          .describe(
            "Agent name to send as. Required for user-scoped API keys. " +
            "For agent-scoped keys, defaults to the calling agent."
          ),
      }),
      execute: async (
        params: {
          channel_type: "telegram" | "slack";
          chat_id: string;
          message: string;
          thread_ts?: string;
          agent_name?: string;
        },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        // Validate message
        if (!params.message || params.message.trim().length === 0) {
          return JSON.stringify({
            success: false,
            error: "Message cannot be empty",
          }, null, 2);
        }

        if (params.message.length > 4096) {
          return JSON.stringify({
            success: false,
            error: "Message exceeds 4096 character limit",
          }, null, 2);
        }

        try {
          const agentName = getAgentName(authContext, params.agent_name);

          console.log(
            `[send_group_message] Sending to ${params.channel_type} group ${params.chat_id} ` +
            `as ${agentName} (${params.message.length} chars)`
          );

          if (params.channel_type === "telegram") {
            const result = await apiClient.sendTelegramGroupMessage(
              agentName,
              params.chat_id,
              params.message.trim()
            );

            return JSON.stringify({
              success: result.ok,
              agent_name: agentName,
              channel_type: params.channel_type,
              chat_id: result.chat_id,
              group_title: result.group_title,
              message_id: result.message_id,
            }, null, 2);
          }

          if (params.channel_type === "slack") {
            const result = await apiClient.sendSlackChannelMessage(
              agentName,
              params.chat_id,
              params.message.trim(),
              params.thread_ts
            );

            return JSON.stringify({
              success: result.sent,
              agent_name: agentName,
              channel_type: params.channel_type,
              chat_id: result.channel_id,
              group_title: result.channel_name,
              thread_ts: result.thread_ts,
            }, null, 2);
          }

          return JSON.stringify({
            success: false,
            error: `Unsupported channel type: ${params.channel_type}`,
          }, null, 2);
        } catch (error) {
          const errorMessage = error instanceof Error ? error.message : String(error);
          console.error(`[send_group_message] Error: ${errorMessage}`);

          // Parse rate limit errors
          if (errorMessage.includes("429") || errorMessage.includes("Rate limited")) {
            return JSON.stringify({
              success: false,
              error: "Rate limited. Please wait before sending more messages.",
              rate_limited: true,
            }, null, 2);
          }

          return JSON.stringify({
            success: false,
            error: errorMessage,
          }, null, 2);
        }
      },
    },
  };
}
