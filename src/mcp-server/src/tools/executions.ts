/**
 * Execution Query Tools (MCP-007)
 *
 * MCP tools for querying execution history, results, and agent activity.
 * Enables async polling pattern: chat_with_agent(async=true) -> get_execution_result(id)
 */

import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

/**
 * Create execution query tools with the given client
 * @param client - Base Trinity client (provides base URL, no auth when requireApiKey=true)
 * @param requireApiKey - Whether API key authentication is enabled
 */
export function createExecutionTools(
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
   * Check if agent-scoped key can access target agent for read operations.
   * Execution queries are always read-only.
   */
  const checkAgentAccess = async (
    apiClient: TrinityClient,
    authContext: McpAuthContext | undefined,
    targetAgent: string
  ): Promise<{ allowed: boolean; reason?: string }> => {
    if (authContext?.scope === "system") {
      return { allowed: true };
    }

    if (authContext?.scope !== "agent" || !authContext?.agentName) {
      return { allowed: true };
    }

    const callerAgentName = authContext.agentName;

    if (targetAgent === callerAgentName) {
      return { allowed: true };
    }

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
    // list_recent_executions - List recent executions for an agent
    // ========================================================================
    listRecentExecutions: {
      name: "list_recent_executions",
      description:
        "List recent executions for an agent across all trigger types (schedule, manual, MCP, chat). " +
        "Returns execution summaries with status, timing, cost, and context usage. " +
        "Use this to check what tasks ran recently or to find an execution_id for get_execution_result. " +
        "Access control: agents can only list executions on self or permitted agents.",
      parameters: z.object({
        agent_name: z.string().describe("Name of the agent to list executions for"),
        limit: z
          .number()
          .optional()
          .default(20)
          .describe("Maximum number of executions to return (default: 20, max: 100)"),
        status: z
          .string()
          .optional()
          .describe("Filter by status: pending, running, success, failed, cancelled"),
      }),
      execute: async (
        { agent_name, limit = 20, status }: { agent_name: string; limit?: number; status?: string },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        const accessCheck = await checkAgentAccess(apiClient, authContext, agent_name);
        if (!accessCheck.allowed) {
          console.log(`[list_recent_executions] Access denied: ${accessCheck.reason}`);
          return JSON.stringify({
            error: "Access denied",
            reason: accessCheck.reason,
          }, null, 2);
        }

        const effectiveLimit = Math.min(Math.max(1, limit), 100);
        const executions = await apiClient.getAgentExecutions(agent_name, effectiveLimit);

        // Client-side status filter (backend doesn't support it on this endpoint)
        const filtered = status
          ? executions.filter(e => e.status === status)
          : executions;

        console.log(`[list_recent_executions] Retrieved ${filtered.length} executions for agent '${agent_name}'${status ? ` (status=${status})` : ""}`);

        return JSON.stringify({
          agent_name,
          execution_count: filtered.length,
          executions: filtered,
        }, null, 2);
      },
    },

    // ========================================================================
    // get_execution_result - Get details and result of a specific execution
    // ========================================================================
    getExecutionResult: {
      name: "get_execution_result",
      description:
        "Get the full result of a specific execution including response text, cost, and status. " +
        "Use this to poll for results after chat_with_agent(async=true, parallel=true) returns an execution_id. " +
        "Optionally include the full execution transcript (tool calls, thinking, responses). " +
        "Access control: agents can only view executions on self or permitted agents.",
      parameters: z.object({
        agent_name: z.string().describe("Name of the agent that ran the execution"),
        execution_id: z.string().describe("Execution ID to retrieve (returned by async chat_with_agent or list_recent_executions)"),
        include_log: z
          .boolean()
          .optional()
          .default(false)
          .describe("Include full execution transcript/log (can be large). Default: false"),
      }),
      execute: async (
        { agent_name, execution_id, include_log = false }: { agent_name: string; execution_id: string; include_log?: boolean },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        const accessCheck = await checkAgentAccess(apiClient, authContext, agent_name);
        if (!accessCheck.allowed) {
          console.log(`[get_execution_result] Access denied: ${accessCheck.reason}`);
          return JSON.stringify({
            error: "Access denied",
            reason: accessCheck.reason,
          }, null, 2);
        }

        const execution = await apiClient.getExecution(agent_name, execution_id);

        console.log(`[get_execution_result] Retrieved execution ${execution_id} for agent '${agent_name}' (status=${execution.status})`);

        const result: Record<string, unknown> = {
          execution_id: execution.id,
          agent_name: execution.agent_name,
          status: execution.status,
          message: execution.message,
          response: execution.response || null,
          error: execution.error || null,
          started_at: execution.started_at,
          completed_at: execution.completed_at || null,
          duration_ms: execution.duration_ms || null,
          triggered_by: execution.triggered_by,
          cost: execution.cost || null,
          context_used: execution.context_used || null,
          context_max: execution.context_max || null,
          model_used: execution.model_used || null,
        };

        if (include_log) {
          try {
            const logData = await apiClient.getExecutionLog(agent_name, execution_id);
            result.execution_log = logData.log;
          } catch (e) {
            result.execution_log = null;
            result.log_error = `Failed to retrieve log: ${e instanceof Error ? e.message : String(e)}`;
          }
        }

        return JSON.stringify(result, null, 2);
      },
    },

    // ========================================================================
    // get_agent_activity_summary - High-level activity summary for monitoring
    // ========================================================================
    getAgentActivitySummary: {
      name: "get_agent_activity_summary",
      description:
        "Get a high-level activity summary for an agent over a time window. " +
        "Returns counts of activities by type and state (e.g., 5 chat_start completed, 2 schedule_start failed). " +
        "Useful for monitoring agents, checking if scheduled tasks ran, and building dashboards. " +
        "Access control: agents can only view activity for self or permitted agents.",
      parameters: z.object({
        agent_name: z
          .string()
          .optional()
          .describe("Agent name to summarize. If omitted, returns activity across all accessible agents."),
        hours: z
          .number()
          .optional()
          .default(24)
          .describe("Number of hours to look back (default: 24, max: 168 = 7 days)"),
      }),
      execute: async (
        { agent_name, hours = 24 }: { agent_name?: string; hours?: number },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        // If agent_name is specified, check access
        if (agent_name) {
          const accessCheck = await checkAgentAccess(apiClient, authContext, agent_name);
          if (!accessCheck.allowed) {
            console.log(`[get_agent_activity_summary] Access denied: ${accessCheck.reason}`);
            return JSON.stringify({
              error: "Access denied",
              reason: accessCheck.reason,
            }, null, 2);
          }
        }

        const effectiveHours = Math.min(Math.max(1, hours), 168);
        const startTime = new Date(Date.now() - effectiveHours * 60 * 60 * 1000).toISOString();

        const timeline = await apiClient.getActivityTimeline({
          start_time: startTime,
          limit: 500,
        });

        // Filter to specific agent if requested
        const activities = agent_name
          ? timeline.activities.filter(a => a.agent_name === agent_name)
          : timeline.activities;

        // Aggregate by type and state
        const byType: Record<string, Record<string, number>> = {};
        const byAgent: Record<string, number> = {};

        for (const activity of activities) {
          const type = activity.activity_type;
          const state = activity.activity_state;

          if (!byType[type]) byType[type] = {};
          byType[type][state] = (byType[type][state] || 0) + 1;

          byAgent[activity.agent_name] = (byAgent[activity.agent_name] || 0) + 1;
        }

        console.log(`[get_agent_activity_summary] Summarized ${activities.length} activities${agent_name ? ` for '${agent_name}'` : ""} over ${effectiveHours}h`);

        return JSON.stringify({
          agent_name: agent_name || "all",
          hours: effectiveHours,
          total_activities: activities.length,
          by_type: byType,
          by_agent: agent_name ? undefined : byAgent,
        }, null, 2);
      },
    },
  };
}
