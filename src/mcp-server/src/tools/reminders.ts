/**
 * Agent Self-Reminders (#1296)
 *
 * Three MCP tools backing the durable one-shot deferred self-trigger primitive:
 *   - set_reminder     — schedule a future re-invocation of the agent (once)
 *   - list_reminders   — list pending (or all) reminders
 *   - cancel_reminder  — cancel a pending reminder before it fires
 *
 * The time-deferred sibling of run_agent_loop (#740): a loop runs iterations
 * back-to-back now; a reminder fires once, later. When the timer fires, Trinity
 * dispatches a normal execution of the SAME agent carrying the reminder message
 * (shares the agent's max_parallel_tasks budget, exactly like loops).
 *
 * Self-scoping: an agent-scoped key defaults to its bound agent name and the
 * backend enforces self-only (AC #5). Idempotency is enforced server-side over
 * the raw input, so a retry of the same (message, fire spec) dedupes without a
 * client-supplied key.
 */

import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

export function createReminderTools(
  client: TrinityClient,
  requireApiKey: boolean
) {
  const getClient = (authContext?: McpAuthContext): TrinityClient => {
    if (requireApiKey) {
      if (!authContext?.mcpApiKey) {
        throw new Error(
          "MCP API key authentication required but no API key found in request context"
        );
      }
      const userClient = new TrinityClient(client.getBaseUrl());
      userClient.setToken(authContext.mcpApiKey);
      return userClient;
    }
    return client;
  };

  const resolveAgentName = (
    authContext: McpAuthContext | undefined,
    specifiedAgent?: string
  ): string => {
    if (specifiedAgent) return specifiedAgent;
    if (authContext?.scope === "agent" && authContext.agentName) {
      return authContext.agentName;
    }
    throw new Error(
      "agent_name is required. Use an agent-scoped MCP key or pass agent_name."
    );
  };

  return {
    // ========================================================================
    // set_reminder
    // ========================================================================
    setReminder: {
      name: "set_reminder",
      description:
        "Schedule a ONE-SHOT future re-invocation of yourself with a message " +
        "you pick (\"remind me to check this PR in 2h\", \"follow up tomorrow " +
        "9am\"). The time-deferred sibling of run_agent_loop: a loop runs " +
        "iterations back-to-back now; a reminder fires exactly once, later. " +
        "When it fires, Trinity dispatches a normal execution of THIS agent " +
        "carrying the message (through the standard capacity/slot path, so it " +
        "shares your max_parallel_tasks budget). Provide EXACTLY ONE of " +
        "`delay_seconds` (relative) or `fire_at` (absolute ISO-8601). The fire " +
        "must be at least 60s and at most 30 days out. Durable — survives a " +
        "backend/scheduler restart. Use `list_reminders`/`cancel_reminder` to " +
        "manage pending ones.",
      parameters: z.object({
        agent_name: z
          .string()
          .optional()
          .describe(
            "Target agent (must be yourself). Required for user-scoped keys; " +
              "agent-scoped keys default to the bound agent."
          ),
        message: z
          .string()
          .min(1)
          .max(4000)
          .describe("The reminder message you will be re-invoked with."),
        delay_seconds: z
          .number()
          .int()
          .min(60)
          .max(2592000)
          .optional()
          .describe(
            "Relative delay in seconds (60 = 1 min … 2592000 = 30 days). " +
              "Provide this XOR fire_at."
          ),
        fire_at: z
          .string()
          .optional()
          .describe(
            "Absolute ISO-8601 time to fire (UTC recommended, e.g. " +
              "'2026-08-01T09:00:00Z'). Provide this XOR delay_seconds."
          ),
        model: z
          .string()
          .optional()
          .describe("Model override for the fired execution (default: agent default)."),
        timeout_seconds: z
          .number()
          .int()
          .min(10)
          .optional()
          .describe(
            "Execution timeout for the fired task (clamped to the agent's " +
              "configured execution_timeout_seconds)."
          ),
        allowed_tools: z
          .array(z.string())
          .optional()
          .describe("Tool restrictions applied to the fired execution."),
      }),
      execute: async (
        params: {
          agent_name?: string;
          message: string;
          delay_seconds?: number;
          fire_at?: string;
          model?: string;
          timeout_seconds?: number;
          allowed_tools?: string[];
        },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);
        const agentName = resolveAgentName(authContext, params.agent_name);

        console.log(
          `[set_reminder] agent=${agentName} ` +
            `${params.delay_seconds !== undefined ? `delay=${params.delay_seconds}s` : `fire_at=${params.fire_at}`}`
        );

        try {
          const result = await apiClient.setReminder(agentName, {
            message: params.message,
            delay_seconds: params.delay_seconds,
            fire_at: params.fire_at,
            model: params.model,
            timeout_seconds: params.timeout_seconds,
            allowed_tools: params.allowed_tools,
          });
          return JSON.stringify({ success: true, ...(result as object) }, null, 2);
        } catch (error) {
          const msg = error instanceof Error ? error.message : String(error);
          console.error(`[set_reminder] error: ${msg}`);
          return JSON.stringify({ success: false, error: msg }, null, 2);
        }
      },
    },

    // ========================================================================
    // list_reminders
    // ========================================================================
    listReminders: {
      name: "list_reminders",
      description:
        "List your reminders (pending by default), soonest fire first. Pass " +
        "`status: \"all\"` to include fired/cancelled/failed ones.",
      parameters: z.object({
        agent_name: z
          .string()
          .optional()
          .describe("Target agent. Agent-scoped keys default to the bound agent."),
        status: z
          .string()
          .optional()
          .describe(
            "Filter by status (default 'pending'). 'all' lists every status; " +
              "or one of pending|firing|fired|cancelled|failed."
          ),
      }),
      execute: async (
        params: { agent_name?: string; status?: string },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);
        const agentName = resolveAgentName(authContext, params.agent_name);
        try {
          const result = await apiClient.listReminders(agentName, params.status);
          return JSON.stringify({ success: true, reminders: result }, null, 2);
        } catch (error) {
          const msg = error instanceof Error ? error.message : String(error);
          return JSON.stringify({ success: false, error: msg }, null, 2);
        }
      },
    },

    // ========================================================================
    // cancel_reminder
    // ========================================================================
    cancelReminder: {
      name: "cancel_reminder",
      description:
        "Cancel a pending reminder before it fires. Already-cancelled is a " +
        "no-op; a reminder that has already fired (or is firing) cannot be " +
        "cancelled.",
      parameters: z.object({
        agent_name: z
          .string()
          .optional()
          .describe("Target agent. Agent-scoped keys default to the bound agent."),
        reminder_id: z
          .string()
          .min(1)
          .describe("ID returned by set_reminder / list_reminders."),
      }),
      execute: async (
        params: { agent_name?: string; reminder_id: string },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);
        const agentName = resolveAgentName(authContext, params.agent_name);
        try {
          const result = await apiClient.cancelReminder(
            agentName,
            params.reminder_id
          );
          return JSON.stringify({ success: true, ...(result as object) }, null, 2);
        } catch (error) {
          const msg = error instanceof Error ? error.message : String(error);
          return JSON.stringify({ success: false, error: msg }, null, 2);
        }
      },
    },
  };
}
