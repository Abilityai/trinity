/**
 * Agent Event Tools (EVT-001)
 *
 * MCP tools for the lightweight pub/sub event system:
 * - emit_event: Emit a named event with structured payload
 * - subscribe_to_event: Subscribe to events from another agent
 * - list_event_subscriptions: List current event subscriptions
 * - delete_event_subscription: Remove an event subscription
 */

import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

/**
 * Create event tools with the given client
 */
export function createEventTools(
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
    // emit_event - Emit a named event
    // ========================================================================
    emitEvent: {
      name: "emit_event",
      description:
        "Emit a named event to the Trinity platform. " +
        "Other agents can subscribe to your events and receive automated tasks when they fire. " +
        "Use namespaced event types (e.g., 'prediction.resolved', 'report.generated', 'anomaly.detected'). " +
        "Include structured data in the payload - subscribers can reference it via {{payload.field}} templates. " +
        "NOTE: the 'agent.task.*' namespace (agent.task.completed / agent.task.failed) is RESERVED — the backend " +
        "emits those deterministically when your executions finish (#1578); emitting into it is rejected.",
      parameters: z.object({
        event_type: z.string()
          .describe(
            "Namespaced event type identifier (e.g., 'prediction.resolved', 'data.processed'). " +
            "Use dot-separated words to namespace your events."
          ),
        payload: z.record(z.string(), z.unknown()).optional()
          .describe("Structured data to include with the event. Subscribers can reference fields via {{payload.field}} in their message templates."),
      }),
      execute: async (
        params: {
          event_type: string;
          payload?: Record<string, unknown>;
        },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        if (!params.event_type || params.event_type.trim().length === 0) {
          return JSON.stringify({ success: false, error: "event_type is required" }, null, 2);
        }

        console.log(`[emit_event] Emitting: ${params.event_type}`);

        try {
          const result = await apiClient.emitEvent({
            event_type: params.event_type.trim(),
            payload: params.payload,
          });

          return JSON.stringify({
            success: true,
            event_id: result.id,
            source_agent: result.source_agent,
            event_type: result.event_type,
            subscriptions_triggered: result.subscriptions_triggered,
            created_at: result.created_at,
          }, null, 2);
        } catch (error) {
          const errorMessage = error instanceof Error ? error.message : String(error);
          console.error(`[emit_event] Error: ${errorMessage}`);
          return JSON.stringify({ success: false, error: errorMessage }, null, 2);
        }
      },
    },

    // ========================================================================
    // subscribe_to_event - Subscribe to events from another agent
    // ========================================================================
    subscribeToEvent: {
      name: "subscribe_to_event",
      description:
        "Subscribe to events from another agent. " +
        "When the source agent emits a matching event, you will automatically receive a task " +
        "with the target_message (payload fields interpolated). " +
        "Requires permission to communicate with the source agent. " +
        "Use {{payload.field}} placeholders in target_message to include event data. " +
        "TIP: subscribe to a source agent's backend-emitted 'agent.task.completed' / 'agent.task.failed' events " +
        "(#1578) to be woken with an automatic report-back task when its execution finishes — instead of polling " +
        "get_execution_result. Payload carries {execution_id, status, triggered_by, summary_or_error, duration_ms, " +
        "cost, fan_out_id, loop_id}. You cannot self-subscribe to your own 'agent.task.*' events (self-wake loop).",
      parameters: z.object({
        source_agent: z.string()
          .describe("Name of the agent to subscribe to events from"),
        event_type: z.string()
          .describe("Event type to subscribe to (e.g., 'prediction.resolved')"),
        target_message: z.string()
          .describe(
            "Message template to receive when event fires. " +
            "Use {{payload.field}} for dynamic values. " +
            "Example: 'Process prediction {{payload.pred_id}} with outcome {{payload.outcome}}'"
          ),
      }),
      execute: async (
        params: {
          source_agent: string;
          event_type: string;
          target_message: string;
        },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        // Determine subscriber agent from auth context
        const subscriberAgent = authContext?.agentName;
        if (!subscriberAgent) {
          return JSON.stringify({
            success: false,
            error: "Could not determine subscriber agent. This tool must be called with an agent-scoped MCP key.",
          }, null, 2);
        }

        console.log(`[subscribe_to_event] ${subscriberAgent} -> ${params.source_agent}:${params.event_type}`);

        try {
          const result = await apiClient.createEventSubscription(subscriberAgent, {
            source_agent: params.source_agent,
            event_type: params.event_type,
            target_message: params.target_message,
          });

          return JSON.stringify({
            success: true,
            subscription_id: result.id,
            subscriber_agent: result.subscriber_agent,
            source_agent: result.source_agent,
            event_type: result.event_type,
            message: `Subscribed to ${params.source_agent}:${params.event_type}. You will receive tasks when this event fires.`,
          }, null, 2);
        } catch (error) {
          const errorMessage = error instanceof Error ? error.message : String(error);
          console.error(`[subscribe_to_event] Error: ${errorMessage}`);
          return JSON.stringify({ success: false, error: errorMessage }, null, 2);
        }
      },
    },

    // ========================================================================
    // list_event_subscriptions - List subscriptions
    // ========================================================================
    listEventSubscriptions: {
      name: "list_event_subscriptions",
      description:
        "List your event subscriptions. " +
        "Shows events you subscribe to (direction='subscriber') or events others subscribe to from you (direction='source').",
      parameters: z.object({
        direction: z.enum(["subscriber", "source", "both"]).default("both")
          .describe("'subscriber' = events you listen for, 'source' = events others listen for from you, 'both' = all"),
      }),
      execute: async (
        params: { direction?: "subscriber" | "source" | "both" },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        const agentName = authContext?.agentName;
        if (!agentName) {
          return JSON.stringify({
            success: false,
            error: "Could not determine agent name. This tool must be called with an agent-scoped MCP key.",
          }, null, 2);
        }

        try {
          const result = await apiClient.listEventSubscriptions(
            agentName,
            params.direction || "both"
          );

          return JSON.stringify({
            agent_name: agentName,
            direction: params.direction || "both",
            count: result.count,
            subscriptions: result.subscriptions.map(s => ({
              id: s.id,
              subscriber_agent: s.subscriber_agent,
              source_agent: s.source_agent,
              event_type: s.event_type,
              target_message: s.target_message,
              enabled: s.enabled,
            })),
          }, null, 2);
        } catch (error) {
          const errorMessage = error instanceof Error ? error.message : String(error);
          return JSON.stringify({ success: false, error: errorMessage }, null, 2);
        }
      },
    },

    // ========================================================================
    // delete_event_subscription - Remove a subscription
    // ========================================================================
    deleteEventSubscription: {
      name: "delete_event_subscription",
      description: "Delete an event subscription by ID. Only the subscribing agent's owner can delete.",
      parameters: z.object({
        subscription_id: z.string().describe("ID of the subscription to delete"),
      }),
      execute: async (
        params: { subscription_id: string },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        try {
          const result = await apiClient.deleteEventSubscription(params.subscription_id);
          return JSON.stringify({
            success: true,
            message: `Subscription ${params.subscription_id} deleted`,
          }, null, 2);
        } catch (error) {
          const errorMessage = error instanceof Error ? error.message : String(error);
          return JSON.stringify({ success: false, error: errorMessage }, null, 2);
        }
      },
    },
  };
}
