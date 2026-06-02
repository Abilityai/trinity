/**
 * Chat & Communication Tools
 *
 * MCP tools for interacting with Trinity agents: chat, get history, get logs
 * Includes access control for agent-to-agent collaboration.
 */

import { z } from "zod";
import { createHash } from "crypto";
import { TrinityClient } from "../client.js";
import type { McpAuthContext, AgentAccessCheckResult } from "../types.js";

/**
 * RELIABILITY-006 (#525): derive a deterministic Idempotency-Key for an MCP
 * tool call. A transport-level retry of the same call (same caller, agent,
 * mode, model, message) resolves to the same key, so the backend short-circuits
 * the duplicate instead of dispatching a second execution. Byte-identical
 * requests within the 24h TTL dedupe — standard idempotency semantics.
 */
function deriveMcpIdempotencyKey(parts: (string | undefined)[]): string {
  const h = createHash("sha256");
  h.update(parts.map((p) => p ?? "").join(" "));
  return `mcp:${h.digest("hex")}`;
}

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
        "Best for independent tasks, batch processing, orchestrator delegation.\n" +
        "- `async=true` (with parallel=true): Fire-and-forget mode. Returns immediately with execution_id. " +
        "Poll GET /api/agents/{name}/executions/{execution_id} for results." +
        "\n\n**#914 Gateway-Timeout Receipt (sync chat mode only):** " +
        "If the MCP-server's synchronous fetch to the backend takes longer than `MCP_CHAT_TIMEOUT_MS` " +
        "(default 25s, set under the typical 30-60s MCP gateway ceiling), the call returns " +
        "`{status: \"queued_timeout\", agent, execution_id, message}` instead of a generic `fetch failed`. " +
        "The task IS still running on the agent — call `get_execution_result(execution_id)` to poll for the " +
        "result instead of retrying. Retrying will duplicate-queue and Trinity's concurrent-duplicate guard " +
        "will kill mid-execution, burning budget. For tasks you know will exceed the gateway timeout, prefer " +
        "`parallel=true, async=true` from the start.",
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
          .describe(
            "Execution timeout in seconds. If omitted, uses the target agent's " +
            "configured execution_timeout_seconds (default 900s, max 7200s). " +
            "Only applies when parallel=true."
          ),
        async: z
          .boolean()
          .optional()
          .default(false)
          .describe(
            "If true, return immediately with execution_id (fire-and-forget). " +
            "Only applies when parallel=true. Poll the execution endpoint for results."
          ),
        inject_result: z
          .boolean()
          .optional()
          .default(false)
          .describe(
            "If true and this is a self-task (calling yourself), inject the result as a message " +
            "in your current chat session. Requires chat_session_id. Only applies to self-calls."
          ),
        chat_session_id: z
          .string()
          .optional()
          .describe(
            "Chat session ID to link this self-task to. Required for inject_result=true. " +
            "The result will be injected into this session when the task completes."
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
          async: asyncMode,
          inject_result,
          chat_session_id,
        }: {
          agent_name: string;
          message: string;
          parallel?: boolean;
          model?: string;
          allowed_tools?: string[];
          system_prompt?: string;
          timeout_seconds?: number;
          async?: boolean;
          inject_result?: boolean;
          chat_session_id?: string;
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

        // Pass source agent for collaboration tracking
        const sourceAgent = authContext?.scope === "agent" ? authContext.agentName : undefined;

        // SELF-EXEC-001: Detect self-call (agent calling itself)
        const isSelfTask = sourceAgent !== undefined && sourceAgent === agent_name;

        // Log collaboration or self-task
        if (authContext?.scope === "agent") {
          if (isSelfTask) {
            console.log(`[Self-Task] ${authContext.agentName} calling itself (${parallel ? 'parallel' : 'sequential'}, inject_result=${inject_result})`);
          } else {
            console.log(`[Agent Collaboration] ${authContext.agentName} -> ${agent_name} (${parallel ? 'parallel' : 'sequential'})`);
          }
        }

        // Build MCP key info for execution origin tracking (AUDIT-001)
        const mcpKeyInfo = authContext ? {
          keyId: authContext.keyId,
          keyName: authContext.keyName,
        } : undefined;

        // RELIABILITY-006 (#525): deterministic key so a transport retry of this
        // exact call dedupes at the backend.
        const idempotencyKey = deriveMcpIdempotencyKey([
          sourceAgent || authContext?.userId,
          agent_name,
          parallel ? "task" : "chat",
          model,
          asyncMode ? "async" : "sync",
          message,
        ]);

        // Use parallel task mode or sequential chat mode based on parameter
        if (parallel) {
          // Parallel task mode - stateless, no queue
          const modeDesc = asyncMode ? 'async (fire-and-forget)' : 'sync';
          const taskDesc = isSelfTask ? 'self-task' : 'parallel task';
          console.log(`[Parallel Task] Sending ${taskDesc} to ${agent_name} (${modeDesc})`);
          const response = await apiClient.task(
            agent_name,
            message,
            {
              model,
              allowed_tools,
              system_prompt,
              timeout_seconds,
              async_mode: asyncMode,
              // SELF-EXEC-001: Pass inject_result and chat_session_id for self-tasks
              inject_result: isSelfTask ? inject_result : undefined,
              chat_session_id: isSelfTask ? chat_session_id : undefined,
            },
            sourceAgent,
            mcpKeyInfo,
            idempotencyKey
          );
          return JSON.stringify(response, null, 2);
        }

        // Sequential chat mode - uses queue, maintains context
        const response = await apiClient.chat(agent_name, message, sourceAgent, mcpKeyInfo, idempotencyKey);

        // #914: MCP-server gateway timeout — task still running on agent.
        // Surface the structured receipt so the caller polls rather than retries.
        if ('status' in response && response.status === 'queued_timeout') {
          console.log(`[Chat Timeout Recovery] Agent '${agent_name}' execution_id=${response.execution_id} — caller should poll get_execution_result (#914)`);
          return JSON.stringify(response, null, 2);
        }

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

    // ========================================================================
    // fan_out - Parallel task dispatch and result collection (FANOUT-001)
    // ========================================================================
    fanOut: {
      name: "fan_out",
      description:
        "Fan out N independent tasks to an agent in parallel and collect all results. " +
        "Each task runs as a separate stateless execution with its own fresh context window. " +
        "Results are collected and returned together with per-task status. " +
        "All subtask executions are tracked on the dashboard with full observability. " +
        "\n\n**Use cases:** batch predictions, parallel analysis, ensemble methods, " +
        "any workload that is embarrassingly parallel. " +
        "\n\n**Concurrency:** Controlled by max_concurrency (default 3, max 10). " +
        "Tasks beyond the limit queue internally until a slot frees up. " +
        "\n\n**Timeout:** Overall deadline for the entire fan-out. Tasks still running " +
        "when the deadline hits are marked as failed with timeout error.",
      parameters: z.object({
        agent_name: z
          .string()
          .describe("The name of the agent to fan out tasks to (typically yourself)"),
        tasks: z
          .array(
            z.object({
              id: z.string().describe("Unique task identifier (alphanumeric, hyphens, underscores, max 64 chars)"),
              message: z.string().describe("The task message/prompt to execute"),
            })
          )
          .min(1)
          .max(50)
          .describe("Array of tasks to execute in parallel (1-50 tasks)"),
        timeout_seconds: z
          .number()
          .optional()
          .describe(
            "Overall deadline in seconds for the entire fan-out (max: 3600). " +
            "If omitted, no outer deadline is applied — each sub-task is still " +
            "bounded by the target agent's configured execution_timeout_seconds."
          ),
        max_concurrency: z
          .number()
          .optional()
          .default(3)
          .describe("Maximum number of tasks to run simultaneously (default: 3, max: 10)"),
        model: z
          .string()
          .optional()
          .describe("Model override for all subtasks (sonnet, opus, haiku)"),
        system_prompt: z
          .string()
          .optional()
          .describe("System prompt to append for all subtasks"),
        allowed_tools: z
          .array(z.string())
          .optional()
          .describe("Restrict which tools subtasks can use"),
      }),
      execute: async (
        {
          agent_name,
          tasks,
          timeout_seconds,
          max_concurrency,
          model,
          system_prompt,
          allowed_tools,
        }: {
          agent_name: string;
          tasks: Array<{ id: string; message: string }>;
          timeout_seconds?: number;
          max_concurrency?: number;
          model?: string;
          system_prompt?: string;
          allowed_tools?: string[];
        },
        context: any
      ) => {
        const authContext = requireApiKey ? context?.session : undefined;
        const apiClient = getClient(authContext);

        const accessCheck = await checkAgentAccess(apiClient, authContext, agent_name);
        if (!accessCheck.allowed) {
          console.log(`[Access Denied] ${authContext?.agentName || authContext?.userId || "unknown"} -> ${agent_name}: ${accessCheck.reason}`);
          return JSON.stringify({
            error: "Access denied",
            reason: accessCheck.reason,
          }, null, 2);
        }

        const sourceAgent = authContext?.scope === "agent" ? authContext.agentName : undefined;
        const mcpKeyInfo = authContext ? {
          keyId: authContext.keyId,
          keyName: authContext.keyName,
        } : undefined;

        console.log(`[Fan-Out] ${sourceAgent || "user"} -> ${agent_name}: ${tasks.length} tasks (concurrency=${max_concurrency || 3})`);

        // RELIABILITY-006 (#525): deterministic key over the whole fan-out so a
        // transport retry replays the original batch instead of re-dispatching.
        const idempotencyKey = deriveMcpIdempotencyKey([
          sourceAgent || authContext?.userId,
          agent_name,
          "fan_out",
          model,
          JSON.stringify(tasks),
        ]);

        const response = await apiClient.fanOut(
          agent_name,
          tasks,
          {
            timeout_seconds,
            max_concurrency,
            model,
            system_prompt,
            allowed_tools,
          },
          sourceAgent,
          mcpKeyInfo,
          idempotencyKey
        );

        return JSON.stringify(response, null, 2);
      },
    },
  };
}
