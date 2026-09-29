/**
 * Agent Monitoring Tools (MON-001)
 *
 * MCP tools for programmatic access to agent health monitoring.
 * Provides fleet-wide health status, individual agent health details,
 * and ability to trigger health checks.
 */

import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

/**
 * Create monitoring tools with the given client
 * @param client - Base Trinity client (provides base URL)
 * @param requireApiKey - Whether API key authentication is enabled
 */
export function createMonitoringTools(
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
    // get_fleet_health - Get fleet-wide health summary
    // ========================================================================
    getFleetHealth: {
      name: "get_fleet_health",
      description:
        "Get fleet-wide agent health summary. " +
        "Returns overall health status including counts of healthy, degraded, unhealthy, and critical agents. " +
        "Also returns a list of all agents with their current health status.",
      parameters: z.object({}),
      execute: async (
        _params: Record<string, never>,
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        console.log("[get_fleet_health] Fetching fleet health status");

        try {
          const result = await apiClient.getFleetHealth();

          return JSON.stringify({
            success: true,
            enabled: result.enabled,
            last_check_at: result.last_check_at,
            summary: result.summary,
            agents: result.agents.map((a: {
              name: string;
              status: string;
              docker_status?: string;
              network_reachable?: boolean;
              runtime_available?: boolean;
              last_check_at?: string;
              issues: string[];
            }) => ({
              name: a.name,
              status: a.status,
              docker_status: a.docker_status,
              network_reachable: a.network_reachable,
              last_check_at: a.last_check_at,
              issues_count: a.issues?.length || 0,
            })),
          }, null, 2);
        } catch (error) {
          const errorMessage = error instanceof Error ? error.message : String(error);
          console.error(`[get_fleet_health] Error: ${errorMessage}`);
          return JSON.stringify({
            success: false,
            error: errorMessage,
          }, null, 2);
        }
      },
    },

    // ========================================================================
    // get_agent_health - Get detailed health for a specific agent
    // ========================================================================
    getAgentHealth: {
      name: "get_agent_health",
      description:
        "Get detailed health information for a specific agent. " +
        "Returns multi-layer health check results including Docker container status, " +
        "network reachability, and business logic health (runtime availability, context usage). " +
        "Also includes recent issues and 24-hour uptime percentage.",
      parameters: z.object({
        agent_name: z.string()
          .describe("Name of the agent to check health for"),
      }),
      execute: async (
        params: { agent_name: string },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        console.log(`[get_agent_health] Fetching health for agent: ${params.agent_name}`);

        try {
          const result = await apiClient.getAgentHealth(params.agent_name);

          return JSON.stringify({
            success: true,
            agent_name: result.agent_name,
            aggregate_status: result.aggregate_status,
            last_check_at: result.last_check_at,
            docker: result.docker ? {
              container_status: result.docker.container_status,
              cpu_percent: result.docker.cpu_percent,
              memory_percent: result.docker.memory_percent,
              memory_mb: result.docker.memory_mb,
              restart_count: result.docker.restart_count,
              oom_killed: result.docker.oom_killed,
            } : null,
            network: result.network ? {
              reachable: result.network.reachable,
              latency_ms: result.network.latency_ms,
              error: result.network.error,
            } : null,
            business: result.business ? {
              status: result.business.status,
              runtime_available: result.business.runtime_available,
              claude_available: result.business.claude_available,
              context_percent: result.business.context_percent,
              active_execution_count: result.business.active_execution_count,
              recent_error_rate: result.business.recent_error_rate,
            } : null,
            issues: result.issues,
            uptime_percent_24h: result.uptime_percent_24h,
            avg_latency_24h_ms: result.avg_latency_24h_ms,
            // ent#479: informational only — a stale business metric is never
            // in `issues` and never moves `aggregate_status`. Read
            // get_metrics for the values behind these names.
            metrics: result.metrics ?? null,
          }, null, 2);
        } catch (error) {
          const errorMessage = error instanceof Error ? error.message : String(error);
          console.error(`[get_agent_health] Error: ${errorMessage}`);
          return JSON.stringify({
            success: false,
            error: errorMessage,
          }, null, 2);
        }
      },
    },

    // ========================================================================
    // trigger_health_check - Trigger immediate health check for an agent
    // ========================================================================
    triggerHealthCheck: {
      name: "trigger_health_check",
      description:
        "Trigger an immediate health check for a specific agent. " +
        "Admin only. Forces a fresh health check regardless of the normal schedule. " +
        "Returns the updated health status after the check completes.",
      parameters: z.object({
        agent_name: z.string()
          .describe("Name of the agent to check"),
      }),
      execute: async (
        params: { agent_name: string },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        console.log(`[trigger_health_check] Triggering health check for agent: ${params.agent_name}`);

        try {
          const result = await apiClient.triggerAgentHealthCheck(params.agent_name);

          return JSON.stringify({
            success: true,
            agent_name: result.agent_name,
            aggregate_status: result.aggregate_status,
            last_check_at: result.last_check_at,
            issues: result.issues,
            message: `Health check completed for ${params.agent_name}. Status: ${result.aggregate_status}`,
          }, null, 2);
        } catch (error) {
          const errorMessage = error instanceof Error ? error.message : String(error);
          console.error(`[trigger_health_check] Error: ${errorMessage}`);
          return JSON.stringify({
            success: false,
            error: errorMessage,
          }, null, 2);
        }
      },
    },
  };
}
