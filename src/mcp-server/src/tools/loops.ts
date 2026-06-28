/**
 * Sequential Agent Loops (#740)
 *
 * Three MCP tools backing the `run_agent_loop` primitive:
 *   - run_agent_loop    — start a loop; returns loop_id immediately
 *   - get_loop_status   — poll a loop's status and per-run summaries
 *   - stop_loop         — graceful stop (current iteration finishes)
 *
 * Permission rules match `chat_with_agent`: owner/admin/shared, or
 * explicit agent_permissions for agent-scoped keys. Backend enforces.
 */

import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

export function createLoopTools(
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
    // run_agent_loop
    // ========================================================================
    runAgentLoop: {
      name: "run_agent_loop",
      description:
        "Run an agent task sequentially up to N times. Server-side loop " +
        "state — the caller receives a loop_id immediately and can " +
        "disconnect. Each iteration is bounded by the agent's configured " +
        "execution timeout (overridable per loop). The message template " +
        "supports `{{run}}` (1-indexed iteration) and " +
        "`{{previous_response}}` (last iteration's response, trailing " +
        "2000 chars). When `stop_signal` is set the loop exits early on " +
        "any iteration whose response contains that string (recommended " +
        "sentinel: `[[DONE]]`); when unset, the loop runs exactly " +
        "`max_runs` iterations. Use `get_loop_status` to poll and " +
        "`stop_loop` to request a graceful stop.",
      parameters: z.object({
        agent_name: z
          .string()
          .optional()
          .describe(
            "Target agent. Required for user-scoped keys; agent-scoped keys " +
              "default to the bound agent."
          ),
        message: z
          .string()
          .min(1)
          .max(100_000)
          .describe(
            "Task message. Supports `{{run}}` and `{{previous_response}}` substitutions."
          ),
        max_runs: z
          .number()
          .int()
          .min(1)
          .max(100)
          .describe("Hard ceiling on iterations (1–100)."),
        stop_signal: z
          .string()
          .max(200)
          .optional()
          .describe(
            "Optional string that, when present in any iteration's response, " +
              "exits the loop early. Recommended: `[[DONE]]`."
          ),
        delay_seconds: z
          .number()
          .int()
          .min(0)
          .max(3600)
          .optional()
          .describe("Pause between iterations in seconds (default 0)."),
        timeout_per_run: z
          .number()
          .int()
          .min(10)
          .max(7200)
          .optional()
          .describe(
            "Per-iteration timeout in seconds (defaults to agent's configured execution_timeout_seconds)."
          ),
        max_duration_seconds: z
          .number()
          .int()
          .min(1)
          .max(604_800)
          .optional()
          .describe(
            "Optional loop-level wall-clock deadline in seconds (1–604800 = up to 7 days). " +
              "Checked at each iteration boundary; an in-flight run is never killed mid-turn. " +
              "When the deadline passes the loop stops with stop_reason='deadline_exceeded'. " +
              "Must be >= timeout_per_run (or the agent's execution timeout when unset). " +
              "Omit for no time bound (max_runs still applies)."
          ),
        no_progress_threshold: z
          .number()
          .int()
          .min(0)
          .refine((v) => v !== 1, "must be 0 (disabled) or >= 2")
          .optional()
          .describe(
            "Stop the loop after K consecutive runs that produce an identical " +
              "response (doom-loop / no-progress detection). 0 disables; default 3. " +
              "Detection is exact-hash on the normalized response text. Set 0 for " +
              "loops that legitimately repeat identical confirmations."
          ),
        model: z
          .string()
          .optional()
          .describe("Model override for every iteration (e.g., 'claude-opus-4-8', 'claude-sonnet-4-6'). If omitted, uses agent default."),
        allowed_tools: z
          .array(z.string())
          .optional()
          .describe("Tool restrictions applied to every iteration."),
      }),
      execute: async (
        params: {
          agent_name?: string;
          message: string;
          max_runs: number;
          stop_signal?: string;
          delay_seconds?: number;
          timeout_per_run?: number;
          max_duration_seconds?: number;
          no_progress_threshold?: number;
          model?: string;
          allowed_tools?: string[];
        },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);
        const agentName = resolveAgentName(authContext, params.agent_name);

        console.log(
          `[run_agent_loop] agent=${agentName} max_runs=${params.max_runs} ` +
            `stop_signal=${params.stop_signal ? "set" : "unset"}`
        );

        try {
          const result = await apiClient.startAgentLoop(agentName, {
            message: params.message,
            max_runs: params.max_runs,
            stop_signal: params.stop_signal,
            delay_seconds: params.delay_seconds,
            timeout_per_run: params.timeout_per_run,
            max_duration_seconds: params.max_duration_seconds,
            no_progress_threshold: params.no_progress_threshold,
            model: params.model,
            allowed_tools: params.allowed_tools,
          });
          return JSON.stringify({ success: true, ...result }, null, 2);
        } catch (error) {
          const msg = error instanceof Error ? error.message : String(error);
          console.error(`[run_agent_loop] error: ${msg}`);
          return JSON.stringify({ success: false, error: msg }, null, 2);
        }
      },
    },

    // ========================================================================
    // get_loop_status
    // ========================================================================
    getLoopStatus: {
      name: "get_loop_status",
      description:
        "Return the current status of a loop plus a per-run summary " +
        "(run_number, execution_id, status, response_preview, cost, " +
        "duration_ms) and the last full response. Caller must be the loop " +
        "initiator, the agent's owner, an admin, or have shared access.",
      parameters: z.object({
        loop_id: z.string().min(1).describe("ID returned by run_agent_loop."),
      }),
      execute: async (
        params: { loop_id: string },
        context?: { session?: McpAuthContext }
      ) => {
        const apiClient = getClient(context?.session);
        try {
          const result = await apiClient.getLoopStatus(params.loop_id);
          return JSON.stringify({ success: true, ...(result as object) }, null, 2);
        } catch (error) {
          const msg = error instanceof Error ? error.message : String(error);
          return JSON.stringify({ success: false, error: msg }, null, 2);
        }
      },
    },

    // ========================================================================
    // stop_loop
    // ========================================================================
    stopLoop: {
      name: "stop_loop",
      description:
        "Request a graceful stop of a running loop. The currently-executing " +
        "iteration finishes; subsequent iterations do not run. Returns " +
        "`stopping` if a runner was signaled, `already_done` if the loop " +
        "had already reached a terminal state.",
      parameters: z.object({
        loop_id: z.string().min(1).describe("ID of the loop to stop."),
      }),
      execute: async (
        params: { loop_id: string },
        context?: { session?: McpAuthContext }
      ) => {
        const apiClient = getClient(context?.session);
        try {
          const result = await apiClient.stopAgentLoop(params.loop_id);
          return JSON.stringify({ success: true, ...result }, null, 2);
        } catch (error) {
          const msg = error instanceof Error ? error.message : String(error);
          return JSON.stringify({ success: false, error: msg }, null, 2);
        }
      },
    },
  };
}
