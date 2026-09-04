/**
 * Agent Notification Tools (NOTIF-001)
 *
 * MCP tool for agents to send structured notifications to the Trinity platform.
 * Notifications are persisted, broadcast via WebSocket, and queryable via API.
 */

import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

/**
 * Create notification tools with the given client
 * @param client - Base Trinity client (provides base URL)
 * @param requireApiKey - Whether API key authentication is enabled
 */
export function createNotificationTools(
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
    // send_notification - Send a notification to Trinity
    // ========================================================================
    sendNotification: {
      name: "send_notification",
      description:
        "Send a notification to the Trinity platform. " +
        "Use this to notify users of important events: task completions, errors, " +
        "anomalies, questions needing input, or health issues. " +
        "Notifications are persisted, broadcast in real-time via WebSocket, and queryable via API.",
      parameters: z.object({
        notification_type: z.enum(["alert", "info", "status", "completion", "question"])
          .describe(
            "Type of notification: " +
            "'alert' for urgent issues needing attention, " +
            "'info' for general information, " +
            "'status' for progress updates, " +
            "'completion' for task/job completions, " +
            "'question' for user input needed"
          ),
        title: z.string().max(200)
          .describe("Short summary of the notification (required, max 200 chars)"),
        message: z.string().optional()
          .describe("Detailed explanation (optional)"),
        priority: z.enum(["low", "normal", "high", "urgent"]).default("normal")
          .describe("Priority level: 'low', 'normal' (default), 'high', or 'urgent'"),
        category: z.string().optional()
          .describe("Free-form category for grouping (e.g., 'progress', 'anomaly', 'health', 'error')"),
        metadata: z.record(z.string(), z.unknown()).optional()
          .describe("Any structured data to include with the notification"),
      }),
      execute: async (
        params: {
          notification_type: "alert" | "info" | "status" | "completion" | "question";
          title: string;
          message?: string;
          priority?: "low" | "normal" | "high" | "urgent";
          category?: string;
          metadata?: Record<string, unknown>;
        },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        // Validate title length
        if (!params.title || params.title.trim().length === 0) {
          return JSON.stringify({ success: false, error: "Title is required" }, null, 2);
        }
        if (params.title.length > 200) {
          return JSON.stringify({ success: false, error: "Title too long (max 200 characters)" }, null, 2);
        }

        console.log(`[send_notification] Creating notification: ${params.notification_type} - ${params.title}`);

        try {
          const result = await apiClient.createNotification({
            notification_type: params.notification_type,
            title: params.title.trim(),
            message: params.message,
            priority: params.priority || "normal",
            category: params.category,
            metadata: params.metadata,
          });

          return JSON.stringify({
            success: true,
            notification_id: result.id,
            agent_name: result.agent_name,
            created_at: result.created_at,
          }, null, 2);
        } catch (error) {
          const errorMessage = error instanceof Error ? error.message : String(error);
          console.error(`[send_notification] Error: ${errorMessage}`);
          return JSON.stringify({
            success: false,
            error: errorMessage,
          }, null, 2);
        }
      },
    },
  };
}
