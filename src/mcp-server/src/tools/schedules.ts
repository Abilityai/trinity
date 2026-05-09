/**
 * Schedule Management Tools
 *
 * MCP tools for managing agent schedules: list, create, get, update, delete, toggle, trigger, executions
 */

import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

/**
 * Create schedule management tools with the given client
 * @param client - Base Trinity client (provides base URL, no auth when requireApiKey=true)
 * @param requireApiKey - Whether API key authentication is enabled
 */
export function createScheduleTools(
  client: TrinityClient,
  requireApiKey: boolean
) {
  /**
   * Get Trinity client with appropriate authentication
   * When requireApiKey is true, REQUIRES MCP API key from auth context
   * When requireApiKey is false, uses the base client (backward compatibility)
   */
  const getClient = (authContext?: McpAuthContext): TrinityClient => {
    if (requireApiKey) {
      // MCP API key is REQUIRED - no fallback
      if (!authContext?.mcpApiKey) {
        throw new Error("MCP API key authentication required but no API key found in request context");
      }
      // Create new client instance authenticated with user's MCP API key
      const userClient = new TrinityClient(client.getBaseUrl());
      userClient.setToken(authContext.mcpApiKey);
      return userClient;
    }
    // API key auth disabled - use base client (backward compatibility)
    return client;
  };

  /**
   * Check if agent-scoped key can access target agent for schedule operations
   * Agent-scoped keys can access: self, permitted agents (for read/toggle/trigger)
   * Agent-scoped keys CANNOT: create/update/delete schedules on OTHER agents
   */
  const checkAgentAccess = async (
    apiClient: TrinityClient,
    authContext: McpAuthContext | undefined,
    targetAgent: string,
    operation: "read" | "write"
  ): Promise<{ allowed: boolean; reason?: string }> => {
    // System-scoped keys have full access
    if (authContext?.scope === "system") {
      return { allowed: true };
    }

    // User-scoped keys rely on backend access control
    if (authContext?.scope !== "agent" || !authContext?.agentName) {
      return { allowed: true };
    }

    const callerAgentName = authContext.agentName;

    // Agent can always access its own schedules
    if (targetAgent === callerAgentName) {
      return { allowed: true };
    }

    // For write operations, agents cannot modify other agents' schedules
    if (operation === "write") {
      return {
        allowed: false,
        reason: `Agent '${callerAgentName}' cannot create/update/delete schedules on other agents. Only self-scheduling is allowed.`,
      };
    }

    // For read operations, check permissions
    const permittedAgents = await apiClient.getPermittedAgents(callerAgentName);
    if (!permittedAgents.includes(targetAgent)) {
      return {
        allowed: false,
        reason: `Agent '${callerAgentName}' does not have permission to access '${targetAgent}'`,
      };
    }

    return { allowed: true };
  };

  return {
    // ========================================================================
    // list_agent_schedules - List all schedules for an agent (REQ-1)
    // ========================================================================
    listAgentSchedules: {
      name: "list_agent_schedules",
      description:
        "List all schedules configured for an agent with their status, last/next execution times. " +
        "Returns schedule details including cron expression, message, enabled status, and timezone. " +
        "Access control: agents can only list schedules on self or permitted agents.",
      parameters: z.object({
        agent_name: z.string().describe("Name of the agent to list schedules for"),
      }),
      execute: async (
        { agent_name }: { agent_name: string },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        // Access control for agent-scoped keys
        const accessCheck = await checkAgentAccess(apiClient, authContext, agent_name, "read");
        if (!accessCheck.allowed) {
          console.log(`[list_agent_schedules] Access denied: ${accessCheck.reason}`);
          return JSON.stringify({
            error: "Access denied",
            reason: accessCheck.reason,
          }, null, 2);
        }

        const schedules = await apiClient.listAgentSchedules(agent_name);

        console.log(`[list_agent_schedules] Listed ${schedules.length} schedules for agent '${agent_name}'`);

        return JSON.stringify({
          agent_name,
          schedule_count: schedules.length,
          schedules,
        }, null, 2);
      },
    },

    // ========================================================================
    // create_agent_schedule - Create a new schedule (REQ-2)
    // ========================================================================
    createAgentSchedule: {
      name: "create_agent_schedule",
      description:
        "Create a new cron-based schedule for an agent. " +
        "The schedule will automatically trigger the agent with the specified message at the configured times. " +
        "Cron format: minute hour day-of-month month day-of-week (5 fields). " +
        "Examples: '0 9 * * *' (daily 9am), '0 9 * * 1-5' (weekdays 9am), '*/30 * * * *' (every 30 min). " +
        "Access control: agents can only create schedules on themselves, not other agents.",
      parameters: z.object({
        agent_name: z.string().describe("Target agent name"),
        name: z.string().describe("Human-readable schedule name (e.g., 'Daily Report')"),
        cron_expression: z
          .string()
          .describe(
            "5-field cron expression (min hour day month dow). " +
            "Examples: '0 9 * * *' (daily 9am), '0 9 * * 1-5' (weekdays 9am), " +
            "'*/15 * * * *' (every 15 min), '0 0 1 * *' (monthly)"
          ),
        message: z.string().describe("Task message sent to agent when schedule triggers"),
        timezone: z
          .string()
          .optional()
          .default("UTC")
          .describe("Timezone for schedule (e.g., 'America/New_York', 'Europe/London'). Default: UTC"),
        description: z.string().optional().describe("Optional description of the schedule's purpose"),
        enabled: z
          .boolean()
          .optional()
          .default(true)
          .describe("Whether to enable the schedule immediately (default: true)"),
        timeout_seconds: z
          .number()
          .int()
          .positive()
          .optional()
          .describe("Execution timeout in seconds (default: 900 = 15 minutes)"),
        allowed_tools: z
          .array(z.string())
          .optional()
          .describe("List of tools the agent is allowed to use during execution. If omitted, all tools are allowed."),
        model: z
          .string()
          .optional()
          .describe("Model override for this schedule's executions (e.g., 'claude-sonnet-4-20250514'). If omitted, uses agent default."),
        max_retries: z
          .number()
          .int()
          .min(0)
          .max(5)
          .optional()
          .default(1)
          .describe("Max retry attempts on failure (0-5). Default: 1. Set to 0 to disable retries."),
        retry_delay_seconds: z
          .number()
          .int()
          .min(30)
          .max(600)
          .optional()
          .default(60)
          .describe("Seconds between retry attempts (30-600). Default: 60. Rate-limited failures use 2x delay."),
      }),
      execute: async (
        args: {
          agent_name: string;
          name: string;
          cron_expression: string;
          message: string;
          timezone?: string;
          description?: string;
          enabled?: boolean;
          timeout_seconds?: number;
          allowed_tools?: string[];
          model?: string;
          max_retries?: number;
          retry_delay_seconds?: number;
        },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        // Access control: agents can only create schedules on themselves
        const accessCheck = await checkAgentAccess(apiClient, authContext, args.agent_name, "write");
        if (!accessCheck.allowed) {
          console.log(`[create_agent_schedule] Access denied: ${accessCheck.reason}`);
          return JSON.stringify({
            error: "Access denied",
            reason: accessCheck.reason,
            hint: "Agents can only create schedules on themselves, not other agents.",
          }, null, 2);
        }

        const schedule = await apiClient.createAgentSchedule(args.agent_name, {
          name: args.name,
          cron_expression: args.cron_expression,
          message: args.message,
          timezone: args.timezone,
          description: args.description,
          enabled: args.enabled,
          timeout_seconds: args.timeout_seconds,
          allowed_tools: args.allowed_tools,
          model: args.model,
          max_retries: args.max_retries,
          retry_delay_seconds: args.retry_delay_seconds,
        });

        console.log(`[create_agent_schedule] Created schedule '${schedule.name}' (${schedule.id}) for agent '${args.agent_name}'`);

        return JSON.stringify({
          status: "created",
          schedule: {
            id: schedule.id,
            name: schedule.name,
            cron_expression: schedule.cron_expression,
            message: schedule.message,
            enabled: schedule.enabled,
            timezone: schedule.timezone,
            next_run_at: schedule.next_run_at,
          },
        }, null, 2);
      },
    },

    // ========================================================================
    // get_agent_schedule - Get schedule details (REQ-3)
    // ========================================================================
    getAgentSchedule: {
      name: "get_agent_schedule",
      description:
        "Get detailed information about a specific schedule including full configuration and execution history timestamps. " +
        "Access control: agents can only get schedules on self or permitted agents.",
      parameters: z.object({
        agent_name: z.string().describe("Agent name"),
        schedule_id: z.string().describe("Schedule ID to retrieve"),
      }),
      execute: async (
        { agent_name, schedule_id }: { agent_name: string; schedule_id: string },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        // Access control
        const accessCheck = await checkAgentAccess(apiClient, authContext, agent_name, "read");
        if (!accessCheck.allowed) {
          console.log(`[get_agent_schedule] Access denied: ${accessCheck.reason}`);
          return JSON.stringify({
            error: "Access denied",
            reason: accessCheck.reason,
          }, null, 2);
        }

        const schedule = await apiClient.getAgentSchedule(agent_name, schedule_id);

        console.log(`[get_agent_schedule] Retrieved schedule '${schedule.name}' for agent '${agent_name}'`);

        return JSON.stringify(schedule, null, 2);
      },
    },

    // ========================================================================
    // update_agent_schedule - Update a schedule (REQ-4)
    // ========================================================================
    updateAgentSchedule: {
      name: "update_agent_schedule",
      description:
        "Update an existing schedule's configuration. Only specified fields will be updated. " +
        "Access control: agents can only update their own schedules.",
      parameters: z.object({
        agent_name: z.string().describe("Agent name"),
        schedule_id: z.string().describe("Schedule ID to update"),
        name: z.string().optional().describe("New schedule name"),
        cron_expression: z.string().optional().describe("New cron expression"),
        message: z.string().optional().describe("New task message"),
        timezone: z.string().optional().describe("New timezone"),
        description: z.string().optional().describe("New description"),
        enabled: z.boolean().optional().describe(
          "Enable or disable the schedule. WARNING: only include this field if you explicitly intend to " +
          "change the enabled state. Omitting it leaves the current state unchanged. " +
          "Prefer toggle_agent_schedule for toggling; setting enabled=true here will re-enable a " +
          "previously disabled schedule."
        ),
        timeout_seconds: z
          .number()
          .int()
          .positive()
          .optional()
          .describe("New execution timeout in seconds"),
        allowed_tools: z
          .array(z.string())
          .optional()
          .describe("New list of allowed tools. If omitted, keeps current value."),
        model: z
          .string()
          .optional()
          .describe("New model override for executions"),
        max_retries: z
          .number()
          .int()
          .min(0)
          .max(5)
          .optional()
          .describe("New max retry attempts (0-5). If omitted, keeps current value."),
        retry_delay_seconds: z
          .number()
          .int()
          .min(30)
          .max(600)
          .optional()
          .describe("New retry delay in seconds (30-600). If omitted, keeps current value."),
      }),
      execute: async (
        args: {
          agent_name: string;
          schedule_id: string;
          name?: string;
          cron_expression?: string;
          message?: string;
          timezone?: string;
          description?: string;
          enabled?: boolean;
          timeout_seconds?: number;
          allowed_tools?: string[];
          model?: string;
          max_retries?: number;
          retry_delay_seconds?: number;
        },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        // Access control: agents can only update their own schedules
        const accessCheck = await checkAgentAccess(apiClient, authContext, args.agent_name, "write");
        if (!accessCheck.allowed) {
          console.log(`[update_agent_schedule] Access denied: ${accessCheck.reason}`);
          return JSON.stringify({
            error: "Access denied",
            reason: accessCheck.reason,
            hint: "Agents can only update their own schedules.",
          }, null, 2);
        }

        // Build updates object (exclude undefined fields)
        const updates: Record<string, unknown> = {};
        if (args.name !== undefined) updates.name = args.name;
        if (args.cron_expression !== undefined) updates.cron_expression = args.cron_expression;
        if (args.message !== undefined) updates.message = args.message;
        if (args.timezone !== undefined) updates.timezone = args.timezone;
        if (args.description !== undefined) updates.description = args.description;
        if (args.enabled !== undefined) updates.enabled = args.enabled;
        if (args.timeout_seconds !== undefined) updates.timeout_seconds = args.timeout_seconds;
        if (args.allowed_tools !== undefined) updates.allowed_tools = args.allowed_tools;
        if (args.model !== undefined) updates.model = args.model;
        if (args.max_retries !== undefined) updates.max_retries = args.max_retries;
        if (args.retry_delay_seconds !== undefined) updates.retry_delay_seconds = args.retry_delay_seconds;

        const schedule = await apiClient.updateAgentSchedule(
          args.agent_name,
          args.schedule_id,
          updates
        );

        console.log(`[update_agent_schedule] Updated schedule '${schedule.name}' for agent '${args.agent_name}'`);

        return JSON.stringify({
          status: "updated",
          schedule: {
            id: schedule.id,
            name: schedule.name,
            cron_expression: schedule.cron_expression,
            enabled: schedule.enabled,
            next_run_at: schedule.next_run_at,
          },
        }, null, 2);
      },
    },

    // ========================================================================
    // delete_agent_schedule - Delete a schedule (REQ-5)
    // ========================================================================
    deleteAgentSchedule: {
      name: "delete_agent_schedule",
      description:
        "Permanently delete a schedule and its execution history. This action cannot be undone. " +
        "Access control: agents can only delete their own schedules.",
      parameters: z.object({
        agent_name: z.string().describe("Agent name"),
        schedule_id: z.string().describe("Schedule ID to delete"),
      }),
      execute: async (
        { agent_name, schedule_id }: { agent_name: string; schedule_id: string },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        // Access control: agents can only delete their own schedules
        const accessCheck = await checkAgentAccess(apiClient, authContext, agent_name, "write");
        if (!accessCheck.allowed) {
          console.log(`[delete_agent_schedule] Access denied: ${accessCheck.reason}`);
          return JSON.stringify({
            error: "Access denied",
            reason: accessCheck.reason,
            hint: "Agents can only delete their own schedules.",
          }, null, 2);
        }

        // Get schedule name before deletion for response
        let scheduleName = schedule_id;
        try {
          const schedule = await apiClient.getAgentSchedule(agent_name, schedule_id);
          scheduleName = schedule.name;
        } catch {
          // Schedule might not exist, will fail on delete
        }

        await apiClient.deleteAgentSchedule(agent_name, schedule_id);

        console.log(`[delete_agent_schedule] Deleted schedule '${scheduleName}' from agent '${agent_name}'`);

        return JSON.stringify({
          status: "deleted",
          schedule_id,
          message: `Schedule '${scheduleName}' deleted successfully`,
        }, null, 2);
      },
    },

    // ========================================================================
    // toggle_agent_schedule - Enable/disable a schedule (REQ-6)
    // ========================================================================
    toggleAgentSchedule: {
      name: "toggle_agent_schedule",
      description:
        "Enable or disable a schedule without deleting it. Disabled schedules won't run but preserve their configuration. " +
        "Access control: agents can toggle schedules on self or permitted agents.",
      parameters: z.object({
        agent_name: z.string().describe("Agent name"),
        schedule_id: z.string().describe("Schedule ID"),
        enabled: z.boolean().describe("True to enable, false to disable"),
      }),
      execute: async (
        { agent_name, schedule_id, enabled }: { agent_name: string; schedule_id: string; enabled: boolean },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        // Access control for agent-scoped keys (read access for toggle)
        const accessCheck = await checkAgentAccess(apiClient, authContext, agent_name, "read");
        if (!accessCheck.allowed) {
          console.log(`[toggle_agent_schedule] Access denied: ${accessCheck.reason}`);
          return JSON.stringify({
            error: "Access denied",
            reason: accessCheck.reason,
          }, null, 2);
        }

        const result = enabled
          ? await apiClient.enableAgentSchedule(agent_name, schedule_id)
          : await apiClient.disableAgentSchedule(agent_name, schedule_id);

        console.log(`[toggle_agent_schedule] ${enabled ? "Enabled" : "Disabled"} schedule '${schedule_id}' for agent '${agent_name}'`);

        return JSON.stringify({
          status: enabled ? "enabled" : "disabled",
          schedule_id,
          schedule_name: result.schedule_name,
          next_run_at: result.next_run_at,
        }, null, 2);
      },
    },

    // ========================================================================
    // trigger_agent_schedule - Manually trigger a schedule (REQ-7)
    // ========================================================================
    triggerAgentSchedule: {
      name: "trigger_agent_schedule",
      description:
        "Manually trigger immediate execution of a schedule, regardless of its cron timing. " +
        "Creates an execution record with triggered_by='manual'. The execution goes through the standard queue. " +
        "Access control: agents can trigger schedules on self or permitted agents.",
      parameters: z.object({
        agent_name: z.string().describe("Agent name"),
        schedule_id: z.string().describe("Schedule ID to trigger"),
      }),
      execute: async (
        { agent_name, schedule_id }: { agent_name: string; schedule_id: string },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        // Access control for agent-scoped keys (read access for trigger)
        const accessCheck = await checkAgentAccess(apiClient, authContext, agent_name, "read");
        if (!accessCheck.allowed) {
          console.log(`[trigger_agent_schedule] Access denied: ${accessCheck.reason}`);
          return JSON.stringify({
            error: "Access denied",
            reason: accessCheck.reason,
          }, null, 2);
        }

        const result = await apiClient.triggerAgentSchedule(agent_name, schedule_id);

        console.log(`[trigger_agent_schedule] Triggered schedule '${schedule_id}' for agent '${agent_name}', execution_id: ${result.execution_id}`);

        return JSON.stringify({
          status: "triggered",
          schedule_id,
          execution_id: result.execution_id,
          message: `Schedule triggered. Execution started with ID '${result.execution_id}'.`,
        }, null, 2);
      },
    },

    // ========================================================================
    // get_schedule_executions - Get execution history (REQ-8)
    // ========================================================================
    getScheduleExecutions: {
      name: "get_schedule_executions",
      description:
        "Get execution history for a specific schedule or all schedules on an agent. " +
        "Returns status, timing, cost, and context usage for each execution. " +
        "Access control: agents can only view executions on self or permitted agents.",
      parameters: z.object({
        agent_name: z.string().describe("Agent name"),
        schedule_id: z
          .string()
          .optional()
          .describe("Specific schedule ID (optional - if omitted, returns all agent executions)"),
        limit: z
          .number()
          .optional()
          .default(20)
          .describe("Maximum number of executions to return (default: 20, max: 100)"),
      }),
      execute: async (
        { agent_name, schedule_id, limit = 20 }: { agent_name: string; schedule_id?: string; limit?: number },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        // Access control for agent-scoped keys
        const accessCheck = await checkAgentAccess(apiClient, authContext, agent_name, "read");
        if (!accessCheck.allowed) {
          console.log(`[get_schedule_executions] Access denied: ${accessCheck.reason}`);
          return JSON.stringify({
            error: "Access denied",
            reason: accessCheck.reason,
          }, null, 2);
        }

        // Clamp limit to max 100
        const effectiveLimit = Math.min(Math.max(1, limit), 100);

        const executions = schedule_id
          ? await apiClient.getScheduleExecutions(agent_name, schedule_id, effectiveLimit)
          : await apiClient.getAgentExecutions(agent_name, effectiveLimit);

        console.log(`[get_schedule_executions] Retrieved ${executions.length} executions for agent '${agent_name}'${schedule_id ? ` schedule '${schedule_id}'` : ""}`);

        return JSON.stringify({
          agent_name,
          schedule_id: schedule_id || null,
          execution_count: executions.length,
          executions,
        }, null, 2);
      },
    },
  };
}
