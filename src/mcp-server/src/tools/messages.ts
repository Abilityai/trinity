/**
 * Proactive User Messaging Tools (Issue #321)
 *
 * MCP tool for agents to send proactive messages to specific users
 * by verified email across Telegram, Slack, and web channels.
 */

import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

/**
 * Create message tools with the given client
 * @param client - Base Trinity client (provides base URL)
 * @param requireApiKey - Whether API key authentication is enabled
 */
export function createMessageTools(
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
    // send_message - Send proactive message to a user by email
    // ========================================================================
    sendMessage: {
      name: "send_message",
      description:
        "Send a proactive message to a specific user by their verified email address. " +
        "The recipient must have opted in to receive proactive messages from this agent " +
        "(allow_proactive flag must be set in their sharing record). " +
        "Messages are delivered via Telegram, Slack, or web based on the channel parameter. " +
        "Rate limited to 10 messages per recipient per hour.",
      parameters: z.object({
        recipient_email: z.string().email()
          .describe(
            "The verified email address of the recipient. " +
            "Must be in agent_sharing with allow_proactive=1, or be the agent owner."
          ),
        text: z.string().min(1).max(4096)
          .describe("Message content to send (max 4096 characters)"),
        channel: z.enum(["auto", "telegram", "slack", "web"]).default("auto")
          .describe(
            "Target channel: 'auto' tries channels in order (telegram -> slack -> web), " +
            "or specify a specific channel. Currently Telegram and Slack are supported."
          ),
        reply_to_thread: z.boolean().default(false)
          .describe("Continue in the last thread with this user if one exists (channel-dependent)"),
        agent_name: z.string().optional()
          .describe(
            "Agent name to send as. Required for user-scoped API keys. " +
            "For agent-scoped keys, defaults to the calling agent."
          ),
        execution_id: z.string().optional()
          .describe(
            "Your current execution_id — shown in the 'Execution Context' block of your system " +
            "prompt as '- **Execution ID**: <id>'. Pass it so a re-delivery of this turn does NOT " +
            "send a duplicate message (effect-scoped idempotency, #1084). Optional: if omitted, " +
            "the send proceeds without dedup."
          ),
        dedup_label: z.string().optional()
          .describe(
            "Optional discriminator to intentionally send TWO distinct messages to the same " +
            "recipient in one turn (e.g. 'reminder'). Default empty → at-most-one message per " +
            "(recipient, channel) per turn."
          ),
      }),
      execute: async (
        params: {
          recipient_email: string;
          text: string;
          channel?: "auto" | "telegram" | "slack" | "web";
          reply_to_thread?: boolean;
          agent_name?: string;
          execution_id?: string;
          dedup_label?: string;
        },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        // Validate message
        if (!params.text || params.text.trim().length === 0) {
          return JSON.stringify({
            success: false,
            error: "Message cannot be empty",
          }, null, 2);
        }

        if (params.text.length > 4096) {
          return JSON.stringify({
            success: false,
            error: "Message exceeds 4096 character limit",
          }, null, 2);
        }

        // Validate email format
        const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
        if (!emailRegex.test(params.recipient_email)) {
          return JSON.stringify({
            success: false,
            error: "Invalid email address format",
          }, null, 2);
        }

        try {
          const agentName = getAgentName(authContext, params.agent_name);

          console.log(
            `[send_message] Sending to ${params.recipient_email} ` +
            `as ${agentName} via ${params.channel || "auto"} (${params.text.length} chars)`
          );

          const result = await apiClient.sendUserMessage(agentName, {
            recipient_email: params.recipient_email,
            text: params.text.trim(),
            channel: params.channel || "auto",
            reply_to_thread: params.reply_to_thread || false,
            execution_id: params.execution_id,
            dedup_label: params.dedup_label,
          });

          if (result.success) {
            return JSON.stringify({
              success: true,
              agent_name: agentName,
              recipient_email: params.recipient_email,
              channel: result.channel,
              message_id: result.message_id,
            }, null, 2);
          } else {
            return JSON.stringify({
              success: false,
              error: result.error || "Unknown error sending message",
            }, null, 2);
          }
        } catch (error) {
          const errorMessage = error instanceof Error ? error.message : String(error);
          console.error(`[send_message] Error: ${errorMessage}`);

          // Parse specific error types.
          // #186: the backend agent-access dependency now returns a uniform 404
          // "Agent not found" for both a non-existent and an inaccessible agent
          // (was 403). Match that exact detail here — before the generic 404
          // recipient-not-found branch below — so an agent-access denial is
          // reported as not-authorized, not misclassified as recipient_not_found.
          if (
            errorMessage.includes("403") ||
            errorMessage.includes("Not authorized") ||
            errorMessage.includes("Agent not found")
          ) {
            return JSON.stringify({
              success: false,
              error: "Not authorized to message this recipient. They must opt in via allow_proactive flag.",
              not_authorized: true,
            }, null, 2);
          }

          if (errorMessage.includes("429") || errorMessage.includes("Rate limit")) {
            return JSON.stringify({
              success: false,
              error: "Rate limited. Max 10 messages per recipient per hour.",
              rate_limited: true,
            }, null, 2);
          }

          if (errorMessage.includes("404") || errorMessage.includes("not found")) {
            return JSON.stringify({
              success: false,
              error: "Recipient not found. They may not have Telegram or Slack configured.",
              recipient_not_found: true,
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
