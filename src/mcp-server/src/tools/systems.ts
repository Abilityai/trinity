/**
 * System Management Tools
 *
 * MCP tools for deploying and managing multi-agent systems via YAML manifests.
 */

import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

/**
 * Create system management tools with the given client
 * @param client - Base Trinity client (provides base URL, no auth when requireApiKey=true)
 * @param requireApiKey - Whether API key authentication is enabled
 */
export function createSystemTools(
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

  return {
    // ========================================================================
    // deploy_system - Deploy multi-agent system from YAML manifest
    // ========================================================================
    deploySystem: {
      name: "deploy_system",
      description:
        "Deploy a multi-agent system from a YAML manifest. " +
        "The manifest defines the system name, agents, permissions, schedules, and shared folders. " +
        "Supports dry_run mode for validation without deployment — a dry run resolves each `local:` template, "
        + "so `status: 'invalid'` with a populated `failed[]` means the manifest would not deploy cleanly. " +
        "Agents are created with naming pattern '{system}-{agent}' (e.g., 'content-production-orchestrator'). " +
        "Supports permission presets: 'full-mesh', 'orchestrator-workers', 'none', or explicit rules. " +
        "Deploy is best-effort: check `status` ('deployed' | 'partial' | 'failed') and `failed[]` " +
        "in the response — a partial deploy still creates the remaining agents. " +
        "Pass strict: true to restore abort-on-first-error.",
      parameters: z.object({
        manifest: z
          .string()
          .describe(
            "YAML manifest as a string. Format:\n" +
            "name: system-name\n" +
            "description: System description\n" +
            "prompt: System-wide instructions (optional)\n" +
            "agents:\n" +
            "  orchestrator:\n" +
            "    template: github:Org/repo\n" +
            "    folders: {expose: true, consume: true}\n" +
            "    schedules: [{name: daily, cron: '0 9 * * *', message: '...'}]\n" +
            "permissions:\n" +
            "  preset: full-mesh  # or orchestrator-workers, none, explicit"
          ),
        dry_run: z
          .boolean()
          .optional()
          .describe(
            "If true, validates the manifest without creating agents. " +
            "Returns preview of agents to be created and warnings."
          ),
        strict: z
          .boolean()
          .optional()
          .describe(
            "If true, abort the deploy on the first agent-create failure " +
            "(legacy behavior) instead of the default best-effort " +
            "continue-and-report."
          ),
      }),
      execute: async (
        { manifest, dry_run, strict }: { manifest: string; dry_run?: boolean; strict?: boolean },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        // Call backend deploy endpoint
        const response = await apiClient.request<{
          status: string; // "deployed" | "partial" | "failed" | "valid" (dry_run) | "invalid" (dry_run, #1841)
          system_name: string;
          agents_created: string[];
          agents_to_create?: Array<{ name: string; short_name: string; template: string }>;
          prompt_updated: boolean;
          permissions_configured?: number;
          schedules_created?: number;
          warnings: string[];
          failed?: Array<{
            name: string;
            short_name: string;
            template: string;
            reason: string;
            status_code?: number;
          }>;
        }>("POST", "/api/systems/deploy", {
          manifest,
          dry_run: dry_run || false,
          strict: strict || false,
        });

        return JSON.stringify(response, null, 2);
      },
    },

    // ========================================================================
    // list_systems - List all deployed systems (agents grouped by prefix)
    // ========================================================================
    listSystems: {
      name: "list_systems",
      description:
        "List all deployed systems with their agents. " +
        "Groups agents by system prefix (before first '-'). " +
        "For example, agents 'content-production-orchestrator' and 'content-production-writer' " +
        "are grouped under system 'content-production'. " +
        "Returns system summaries with agent counts and details.",
      parameters: z.object({}),
      execute: async (_params: unknown, context?: { session?: McpAuthContext }) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        const response = await apiClient.request<{
          systems: Array<{
            name: string;
            agent_count: number;
            agents: Array<{
              name: string;
              status: string;
              template?: string;
            }>;
            created_at?: string;
          }>;
        }>("GET", "/api/systems");

        return JSON.stringify(response, null, 2);
      },
    },

    // ========================================================================
    // restart_system - Restart all agents in a system
    // ========================================================================
    restartSystem: {
      name: "restart_system",
      description:
        "Restart all agents belonging to a system. " +
        "Membership is resolved from the system's tag when the agents carry one, " +
        "falling back to the '{system_name}-*' name prefix. " +
        "Stops then starts each member. " +
        "Useful after configuration changes (credentials, schedules, shared folders). " +
        "Returns list of successfully restarted agents and any failures. " +
        "Requires the 'creator' role AND a human caller: an agent-scoped key is " +
        "refused with 403 (#2373), because this restarts every member without the " +
        "per-agent spawn-scope check that start_agent/stop_agent apply one at a time.",
      parameters: z.object({
        system_name: z
          .string()
          .describe(
            "System to restart (e.g., 'content-production'). " +
            "Members are the agents tagged with the system, else those matching " +
            "'{system_name}-*'."
          ),
      }),
      execute: async (
        { system_name }: { system_name: string },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        const response = await apiClient.request<{
          restarted: string[];
          failed: string[];
        }>("POST", `/api/systems/${encodeURIComponent(system_name)}/restart`);

        return JSON.stringify(response, null, 2);
      },
    },

    // ========================================================================
    // get_system_manifest - Export system configuration as YAML manifest
    // ========================================================================
    getSystemManifest: {
      name: "get_system_manifest",
      description:
        "Generate a YAML manifest for a deployed system. " +
        "Reconstructs the system configuration from current agent settings. " +
        "Useful for backup, documentation, or replicating systems. " +
        "Returns YAML string that can be used with deploy_system.",
      parameters: z.object({
        system_name: z
          .string()
          .describe(
            "System prefix to export (e.g., 'content-production'). " +
            "All agents matching '{system_name}-*' will be included."
          ),
      }),
      execute: async (
        { system_name }: { system_name: string },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        // Backend returns YAML as text/plain
        const yaml = await apiClient.request<string>(
          "GET",
          `/api/systems/${encodeURIComponent(system_name)}/manifest`
        );

        return yaml;
      },
    },
  };
}
