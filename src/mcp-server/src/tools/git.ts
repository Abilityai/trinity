/**
 * Git tools (#905) — direct, deterministic (non-LLM) git operations over MCP.
 *
 * Exposes the agent git surface as MCP tools so an orchestrator can run
 * status / sync / log / pull / sync-state / reset WITHOUT round-tripping
 * through `chat_with_agent` and an LLM turn. Conflicts stay LLM-mediated: a
 * 409 surfaces the conflict type verbatim and points the caller back to
 * `chat_with_agent` to resolve.
 *
 * Two cross-cutting concerns, both mirroring existing tool modules:
 *
 *  - Per-call correlation (#905): each tool mints a `requestId`, stamps it on
 *    the shared audit context (so the `mcp_operation` row carries it), AND
 *    forwards it as `X-Request-ID` on the backend call. The backend adopts the
 *    header, so the resulting `git_operation` row carries the SAME id —
 *    `GET /api/audit-log?request_id=<id>` returns both, end to end.
 *
 *  - Agent-to-agent gating: the backend resolves an agent-scoped key to its
 *    OWNER and checks owner access — it does NOT apply `agent_permissions`
 *    (architecture §5). So the {self} ∪ permitted gate lives HERE, mirroring
 *    operator_queue.ts / executions.ts. The mutating endpoints (sync/reset)
 *    are additionally `OwnedAgentByName` on the backend — a shared (non-owner)
 *    key gets read+pull only (A2).
 */

import { randomUUID } from "node:crypto";
import { z } from "zod";
import { TrinityClient, ApiError } from "../client.js";
import type { McpAuthContext } from "../types.js";
import type { ToolCallContext } from "../audit.js";

/** git log -N is shelled in the agent server — clamp to a sane bound (#905). */
const GIT_LOG_MIN = 1;
const GIT_LOG_MAX = 100;
const GIT_LOG_DEFAULT = 10;

export function createGitTools(client: TrinityClient, requireApiKey: boolean) {
  const getClient = (authContext?: McpAuthContext): TrinityClient => {
    if (requireApiKey) {
      if (!authContext?.mcpApiKey) {
        throw new Error(
          "MCP API key authentication required but no API key found in request context",
        );
      }
      const userClient = new TrinityClient(client.getBaseUrl());
      userClient.setToken(authContext.mcpApiKey);
      return userClient;
    }
    return client;
  };

  /**
   * Agent-to-agent gate (mirrors operator_queue.ts). system → allow; user →
   * allow (backend already scoped to the owner's accessible agents); agent →
   * self, or a target the calling agent has been explicitly permitted.
   */
  const checkAgentAccess = async (
    apiClient: TrinityClient,
    authContext: McpAuthContext | undefined,
    targetAgent: string,
  ): Promise<{ allowed: boolean; reason?: string }> => {
    if (authContext?.scope === "system") {
      return { allowed: true };
    }
    if (authContext?.scope !== "agent" || !authContext?.agentName) {
      return { allowed: true };
    }
    const caller = authContext.agentName;
    if (targetAgent === caller) {
      return { allowed: true };
    }
    const permitted = await apiClient.getPermittedAgents(caller);
    if (!permitted.includes(targetAgent)) {
      return {
        allowed: false,
        reason: `Agent '${caller}' does not have permission to access '${targetAgent}'`,
      };
    }
    return { allowed: true };
  };

  /**
   * Shared runner: gate the target agent, mint+stamp the requestId, run the
   * proxied call, and normalize errors. A 409 ApiError is surfaced as a
   * structured conflict object (verbatim X-Conflict-Type/-Class) with a hint
   * back to chat_with_agent; everything else returns `{ error }`.
   */
  const run = async (
    toolName: string,
    agentName: string,
    authContext: McpAuthContext | undefined,
    context: ToolCallContext | undefined,
    call: (apiClient: TrinityClient, requestId: string) => Promise<unknown>,
  ): Promise<string> => {
    const apiClient = getClient(authContext);

    const access = await checkAgentAccess(apiClient, authContext, agentName);
    if (!access.allowed) {
      console.log(`[${toolName}] Access denied: ${access.reason}`);
      return JSON.stringify({ error: "Access denied", reason: access.reason }, null, 2);
    }

    // Mint one id per tool call and stamp it on the shared audit context so the
    // mcp_operation row carries the same id we forward as X-Request-ID. Set it
    // BEFORE the await so it's present even if the call throws.
    const requestId = randomUUID();
    if (context) {
      context.requestId = requestId;
    }

    try {
      const result = await call(apiClient, requestId);
      return JSON.stringify(result, null, 2);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        console.log(`[${toolName}] conflict (${error.conflictType ?? "unknown"}) on '${agentName}'`);
        return JSON.stringify(
          {
            error: "conflict",
            status: 409,
            conflict_type: error.conflictType,
            conflict_class: error.conflictClass,
            detail: error.message,
            hint:
              "This git operation hit a conflict that can't be resolved deterministically. " +
              `Resolve it via chat_with_agent(agent_name="${agentName}", ...).`,
          },
          null,
          2,
        );
      }
      const msg = error instanceof Error ? error.message : String(error);
      console.error(`[${toolName}] error: ${msg}`);
      return JSON.stringify({ error: msg }, null, 2);
    }
  };

  return {
    // ========================================================================
    // get_git_status
    // ========================================================================
    getGitStatus: {
      name: "get_git_status",
      description:
        "Get the live git status of an agent — current branch, remote URL, last " +
        "commit, changed/untracked files, and whether changes are pending sync. " +
        "Read-only and deterministic (no LLM turn). Access control: agent-scoped " +
        "keys may only query themselves or agents they have explicit permission for.",
      parameters: z.object({
        agent_name: z.string().min(1).describe("Agent whose git status to read."),
      }),
      execute: async (params: { agent_name: string }, context?: ToolCallContext) => {
        const authContext = context?.session;
        return run("get_git_status", params.agent_name, authContext, context, (api, rid) =>
          api.getGitStatus(params.agent_name, rid),
        );
      },
    },

    // ========================================================================
    // git_sync
    // ========================================================================
    gitSync: {
      name: "git_sync",
      description:
        "Stage all changes, commit, and push an agent's workspace to its working " +
        "branch (deterministic, no LLM turn). Optional: message (commit message), " +
        "paths (subset to sync), strategy (normal | pull_first | force_push). On a " +
        "conflict the tool returns a structured 409 with the conflict type and a " +
        "hint to resolve via chat_with_agent. Owner-only (a shared/non-owner key " +
        "gets read+pull only).",
      parameters: z.object({
        agent_name: z.string().min(1).describe("Agent to sync."),
        message: z.string().optional().describe("Custom commit message."),
        paths: z.array(z.string()).optional().describe("Specific paths to sync (default: all changes)."),
        strategy: z
          .enum(["normal", "pull_first", "force_push"])
          .optional()
          .describe("Sync strategy (default: normal)."),
      }),
      execute: async (
        params: { agent_name: string; message?: string; paths?: string[]; strategy?: string },
        context?: ToolCallContext,
      ) => {
        const authContext = context?.session;
        return run("git_sync", params.agent_name, authContext, context, (api, rid) =>
          api.gitSync(
            params.agent_name,
            { message: params.message, paths: params.paths, strategy: params.strategy },
            rid,
          ),
        );
      },
    },

    // ========================================================================
    // get_git_log
    // ========================================================================
    getGitLog: {
      name: "get_git_log",
      description:
        "Get recent git commits for an agent (sha, short_sha, message, author, " +
        "date). Read-only and deterministic. limit is clamped to 1–100. Access " +
        "control: agent-scoped keys may only query themselves or permitted agents.",
      parameters: z.object({
        agent_name: z.string().min(1).describe("Agent whose commit history to read."),
        limit: z
          .number()
          .int()
          .min(GIT_LOG_MIN)
          .max(GIT_LOG_MAX)
          .optional()
          .default(GIT_LOG_DEFAULT)
          .describe(`Number of commits to return (${GIT_LOG_MIN}–${GIT_LOG_MAX}, default ${GIT_LOG_DEFAULT}).`),
      }),
      execute: async (
        params: { agent_name: string; limit?: number },
        context?: ToolCallContext,
      ) => {
        const authContext = context?.session;
        // Defense in depth: clamp even if the schema default/bounds are bypassed
        // (the agent server shells `git log -${limit}`).
        const raw = params.limit ?? GIT_LOG_DEFAULT;
        const limit = Math.min(GIT_LOG_MAX, Math.max(GIT_LOG_MIN, Math.floor(raw)));
        return run("get_git_log", params.agent_name, authContext, context, (api, rid) =>
          api.getGitLog(params.agent_name, limit, rid),
        );
      },
    },

    // ========================================================================
    // git_pull
    // ========================================================================
    gitPull: {
      name: "git_pull",
      description:
        "Pull latest changes from GitHub into an agent's workspace (deterministic, " +
        "no LLM turn). strategy: clean (fail on conflict) | stash_reapply (stash, " +
        "pull, reapply) | force_reset (discard local, reset to remote). On a " +
        "conflict the tool returns a structured 409 with a hint to resolve via " +
        "chat_with_agent. Available to owners and shared accessors.",
      parameters: z.object({
        agent_name: z.string().min(1).describe("Agent to pull into."),
        strategy: z
          .enum(["clean", "stash_reapply", "force_reset"])
          .optional()
          .default("clean")
          .describe("Pull strategy (default: clean)."),
      }),
      execute: async (
        params: { agent_name: string; strategy?: string },
        context?: ToolCallContext,
      ) => {
        const authContext = context?.session;
        const strategy = params.strategy ?? "clean";
        return run("git_pull", params.agent_name, authContext, context, (api, rid) =>
          api.gitPull(params.agent_name, strategy, rid),
        );
      },
    },

    // ========================================================================
    // get_git_sync_state
    // ========================================================================
    getGitSyncState: {
      name: "get_git_sync_state",
      description:
        "Get the persisted git sync-health row for an agent (#389) — last sync " +
        "status/time, consecutive failures, ahead/behind counts. Read-only. " +
        "Access control: agent-scoped keys may only query themselves or permitted agents.",
      parameters: z.object({
        agent_name: z.string().min(1).describe("Agent whose sync-state to read."),
      }),
      execute: async (params: { agent_name: string }, context?: ToolCallContext) => {
        const authContext = context?.session;
        return run("get_git_sync_state", params.agent_name, authContext, context, (api, rid) =>
          api.getGitSyncState(params.agent_name, rid),
        );
      },
    },

    // ========================================================================
    // reset_to_main_preserve_state (DESTRUCTIVE)
    // ========================================================================
    resetToMainPreserveState: {
      name: "reset_to_main_preserve_state",
      description:
        "⚠️ DESTRUCTIVE recovery (#384): adopt origin/main as the new baseline and " +
        "FORCE-PUSH (--force-with-lease), preserving only the persistent-state " +
        "allowlist. Use ONLY to break a parallel-history deadlock that sync/pull " +
        "cannot resolve — it discards the agent's divergent history on the working " +
        "branch. Owner-only. Guardrails return a structured 409 (agent_busy, " +
        "no_git_config, no_remote_main).",
      parameters: z.object({
        agent_name: z.string().min(1).describe("Agent to reset to origin/main."),
      }),
      execute: async (params: { agent_name: string }, context?: ToolCallContext) => {
        const authContext = context?.session;
        return run("reset_to_main_preserve_state", params.agent_name, authContext, context, (api, rid) =>
          api.resetToMainPreserveState(params.agent_name, rid),
        );
      },
    },
  };
}
