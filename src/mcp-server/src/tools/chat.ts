/**
 * Chat & Communication Tools
 *
 * MCP tools for interacting with Trinity agents: chat, get history, get logs
 * Includes access control for agent-to-agent collaboration.
 */

import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext, AgentAccessCheckResult } from "../types.js";

/**
 * Check if caller has access to target agent
 *
 * Access rules for System-scoped keys (Phase 11.1):
 * - ALWAYS allowed - system agent bypasses all permission checks
 *
 * Access rules for User-scoped keys:
 * - Same owner: Always allowed
 * - Shared agent: Allowed
 * - Admin: Always allowed (bypass)
 * - Otherwise: Denied
 *
 * Access rules for Agent-scoped keys (Phase 9.10):
 * - Self: Always allowed
 * - Target in permitted list: Allowed
 * - Otherwise: Denied (even if same owner)
 */
async function checkAgentAccess(
  client: TrinityClient,
  authContext: McpAuthContext | undefined,
  targetAgentName: string
): Promise<AgentAccessCheckResult> {
  // If no auth context, allow (auth may be disabled)
  if (!authContext) {
    return { allowed: true };
  }

  // Phase 11.1: System-scoped keys bypass ALL permission checks
  // System agent can communicate with any agent
  if (authContext.scope === "system") {
    console.log(`[System Agent Access] ${authContext.agentName || "system"} -> ${targetAgentName} (bypassing permissions)`);
    return { allowed: true };
  }

  // Phase 9.10: Agent-scoped keys use permission system
  if (authContext.scope === "agent" && authContext.agentName) {
    const callerAgentName = authContext.agentName;

    // Self-call is always allowed
    if (callerAgentName === targetAgentName) {
      return { allowed: true };
    }

    // Check if target is in permitted list
    const isPermitted = await client.isAgentPermitted(callerAgentName, targetAgentName);
    if (isPermitted) {
      return { allowed: true };
    }

    // Not permitted
    return {
      allowed: false,
      reason: `Permission denied: Agent '${callerAgentName}' is not permitted to communicate with '${targetAgentName}'. ` +
        `Configure permissions in the Trinity UI.`
    };
  }

  // User-scoped keys: use existing ownership/sharing rules
  const callerOwner = authContext.userId;

  // Get target agent info
  const targetAgent = await client.getAgentAccessInfo(targetAgentName);
  if (!targetAgent) {
    return { allowed: false, reason: `Target agent '${targetAgentName}' not found` };
  }

  // Check access rules
  // 1. Same owner - allowed
  if (callerOwner === targetAgent.owner) {
    return { allowed: true };
  }

  // 2. Shared agent - allowed
  if (targetAgent.is_shared) {
    return { allowed: true };
  }

  // 3. Admin bypass (check if caller is admin user)
  if (callerOwner === "admin" || authContext.userId === "admin") {
    return { allowed: true };
  }

  // Otherwise - denied
  return {
    allowed: false,
    reason: `Access denied: Agent '${targetAgentName}' is owned by '${targetAgent.owner}' (not shared). ` +
      `Caller '${callerOwner}' cannot access it.`
  };
}

/**
 * Create chat tools with the given client
 * @param client - Base Trinity client (provides base URL, no auth when requireApiKey=true)
 * @param requireApiKey - Whether API key authentication is enabled
 */
export function createChatTools(client: TrinityClient, requireApiKey: boolean) {
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
    // chat_with_agent - Send a message to an agent
    // ========================================================================
    chatWithAgent: {
      name: "chat_with_agent",
      description:
        "Send a message to an agent and receive a response. " +
        "This is the primary way to delegate tasks to sub-agents. " +
        "The message will be processed by Claude Code running inside the agent container. " +
        "Responses may take some time depending on the complexity of the task. " +
        "\n\n**Execution Modes:**\n" +
        "- `parallel=false` (default): Sequential chat mode. Uses execution queue, maintains conversation history. " +
        "Best for multi-turn conversations requiring context.\n" +
        "- `parallel=true`: Parallel task mode. Stateless, no queue, can run N tasks concurrently. " +
        "Best for independent tasks, batch processing, orchestrator delegation.",
      parameters: z.object({
        agent_name: z.string().describe("The name of the agent to chat with"),
        message: z
          .string()
          .describe(
            "The message or task to send to the agent. Be clear and specific about what you want the agent to do."
          ),
        parallel: z
          .boolean()
          .optional()
          .default(false)
          .describe(
            "If true, run in parallel task mode (stateless, no queue). " +
            "Use for independent tasks that don't need conversation history. " +
            "Multiple parallel=true calls can run simultaneously."
          ),
        model: z
          .string()
          .optional()
          .describe(
            "Model override for this request (sonnet, opus, haiku). Only applies when parallel=true."
          ),
        allowed_tools: z
          .array(z.string())
          .optional()
          .describe(
            "Restrict which tools the agent can use (e.g., ['Read', 'Write']). Only applies when parallel=true."
          ),
        system_prompt: z
          .string()
          .optional()
          .describe(
            "Additional system prompt to append for this task. Only applies when parallel=true."
          ),
        timeout_seconds: z
          .number()
          .optional()
          .default(300)
          .describe(
            "Execution timeout in seconds (default: 300). Only applies when parallel=true."
          ),
      }),
      execute: async (
        {
          agent_name,
          message,
          parallel,
          model,
          allowed_tools,
          system_prompt,
          timeout_seconds,
        }: {
          agent_name: string;
          message: string;
          parallel?: boolean;
          model?: string;
          allowed_tools?: string[];
          system_prompt?: string;
          timeout_seconds?: number;
        },
        context: any
      ) => {
        // Get auth context from FastMCP session
        const authContext = requireApiKey ? context?.session : undefined;

        // Get authenticated client for this request
        const apiClient = getClient(authContext);

        const accessCheck = await checkAgentAccess(apiClient, authContext, agent_name);

        if (!accessCheck.allowed) {
          console.log(`[Access Denied] ${authContext?.agentName || authContext?.userId || "unknown"} -> ${agent_name}: ${accessCheck.reason}`);
          return JSON.stringify({
            error: "Access denied",
            reason: accessCheck.reason,
            caller: authContext?.agentName || authContext?.userId,
            target: agent_name,
          }, null, 2);
        }

        // Log successful collaboration
        if (authContext?.scope === "agent") {
          console.log(`[Agent Collaboration] ${authContext.agentName} -> ${agent_name} (${parallel ? 'parallel' : 'sequential'})`);
        }

        // Pass source agent for collaboration tracking
        const sourceAgent = authContext?.scope === "agent" ? authContext.agentName : undefined;

        // Use parallel task mode or sequential chat mode based on parameter
        if (parallel) {
          // Parallel task mode - stateless, no queue
          console.log(`[Parallel Task] Sending task to ${agent_name}`);
          const response = await apiClient.task(
            agent_name,
            message,
            {
              model,
              allowed_tools,
              system_prompt,
              timeout_seconds,
            },
            sourceAgent
          );
          return JSON.stringify(response, null, 2);
        }

        // Sequential chat mode - uses queue, maintains context
        const response = await apiClient.chat(agent_name, message, sourceAgent);

        // Check if response is a queue status (agent busy)
        if ('queue_status' in response) {
          console.log(`[Queue Full] Agent '${agent_name}' is busy, queue is full`);
          return JSON.stringify({
            status: "agent_busy",
            agent: agent_name,
            queue_status: response.queue_status,
            retry_after_seconds: response.retry_after,
            message: `Agent '${agent_name}' is currently busy. The execution queue is full. ` +
              `Please wait ${response.retry_after} seconds before retrying, or try a different agent. ` +
              `Consider using parallel=true for independent tasks.`,
            details: response.details,
          }, null, 2);
        }

        return JSON.stringify(response, null, 2);
      },
    },

    // ========================================================================
    // get_chat_history - Get conversation history with an agent
    // ========================================================================
    getChatHistory: {
      name: "get_chat_history",
      description:
        "Retrieve the conversation history with a specific agent. " +
        "Returns all messages exchanged with the agent in the current session. " +
        "Useful for reviewing what tasks have been assigned and their responses.",
      parameters: z.object({
        agent_name: z
          .string()
          .describe("The name of the agent to get chat history for"),
      }),
      execute: async ({ agent_name }: { agent_name: string }, context: any) => {
        const authContext = requireApiKey ? context?.session : undefined;
        const apiClient = getClient(authContext);
        const history = await apiClient.getChatHistory(agent_name);
        return JSON.stringify(history, null, 2);
      },
    },

    // ========================================================================
    // get_agent_logs - Get container logs for an agent
    // ========================================================================
    getAgentLogs: {
      name: "get_agent_logs",
      description:
        "Get the container logs for an agent. " +
        "Useful for debugging issues, checking startup messages, or monitoring agent activity. " +
        "Returns the most recent log lines from the agent's container.",
      parameters: z.object({
        agent_name: z
          .string()
          .describe("The name of the agent to get logs for"),
        lines: z
          .number()
          .optional()
          .default(100)
          .describe("Number of log lines to retrieve (default: 100)"),
      }),
      execute: async ({
        agent_name,
        lines,
      }: {
        agent_name: string;
        lines?: number;
      }, context: any) => {
        const authContext = requireApiKey ? context?.session : undefined;
        const apiClient = getClient(authContext);
        const logs = await apiClient.getAgentLogs(agent_name, lines || 100);
        return logs;
      },
    },
  };
}
