/**
 * Operator Queue tools (OPS-001, #1101 read + #1104 respond)
 *
 * MCP tools exposing the Operating Room queue over MCP:
 *   - list_operator_queue       — broad listing, or scoped via the agent_name filter
 *   - get_operator_queue_item   — a single item by id
 *   - respond_to_operator_queue — resolve a pending item (answer / approve / deny)
 *
 * Access control crux: the backend resolves an agent-scoped MCP key to its
 * OWNER and filters by the owner's accessible agents — it does NOT apply
 * agent_permissions (architecture §5). So agent-to-agent gating lives HERE,
 * mirroring executions.ts (`checkAgentAccess`) and agents.ts (`list_agents`
 * post-filter). The write tool resolves the item's `agent_name` first, then
 * runs the SAME `checkAgentAccess` gate before proxying the response — an
 * agent-scoped key may resolve items for {self} ∪ permitted only. (#1104 v1
 * exposes `respond` only; `cancel` is deferred — wider blast radius.)
 */

import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";
import { accessDenied } from "../access.js";

/**
 * Pure helper: keep only items whose agent is in the allowed set. Used to gate
 * a broad (agent_name-omitted) listing for an agent-scoped key down to
 * {self} ∪ permitted. Exported so a unit test can pin the filter rule without
 * standing up a backend. Generic over `{ agent_name }` so it stays independent
 * of the full item shape (same spirit as agents.ts filtering on `{ name }`).
 */
export function filterQueueItemsForAgentScope<T extends { agent_name: string }>(
  items: T[],
  allowedNames: Set<string>,
): T[] {
  return items.filter((item) => allowedNames.has(item.agent_name));
}

export function createOperatorQueueTools(
  client: TrinityClient,
  requireApiKey: boolean,
) {
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
   * Agent-to-agent read gate (mirrors executions.ts). system → allow; user →
   * allow (the backend already scoped to the user's accessible agents); agent →
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

  return {
    // ========================================================================
    // list_operator_queue
    // ========================================================================
    listOperatorQueue: {
      name: "list_operator_queue",
      description:
        "List Operating Room (operator queue) items — alerts, questions, and " +
        "approval requests raised by agents for an operator to triage. Omit " +
        "agent_name for a broad listing across every agent you can access; pass " +
        "agent_name to scope to one agent (your own, or another you have " +
        "permission for). Filters: status " +
        "(pending/responded/acknowledged/expired/cancelled), type " +
        "(alert/question/approval), priority (critical/high/medium/low), since " +
        "(ISO 8601 timestamp). Read-only. Access control: agent-scoped keys see " +
        "only their own items plus agents they have explicit permission for.",
      parameters: z.object({
        agent_name: z
          .string()
          .optional()
          .describe(
            "Scope to a single agent (own or permitted). Omit for a broad listing across all accessible agents.",
          ),
        status: z
          .string()
          .optional()
          .describe("Filter by status: pending, responded, acknowledged, expired, cancelled."),
        type: z
          .string()
          .optional()
          .describe("Filter by type: alert, question, approval."),
        priority: z
          .string()
          .optional()
          .describe("Filter by priority: critical, high, medium, low."),
        since: z
          .string()
          .optional()
          .describe("Only items created after this ISO 8601 timestamp."),
        limit: z
          .number()
          .int()
          .min(1)
          .max(500)
          .optional()
          .default(100)
          .describe("Maximum number of items to return (1–500, default 100)."),
        offset: z
          .number()
          .int()
          .min(0)
          .optional()
          .default(0)
          .describe("Pagination offset (default 0)."),
      }),
      execute: async (
        params: {
          agent_name?: string;
          status?: string;
          type?: string;
          priority?: string;
          since?: string;
          limit?: number;
          offset?: number;
        },
        context?: { session?: McpAuthContext },
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        // Scoped request: gate the named agent up-front for agent-scoped keys.
        if (params.agent_name) {
          const access = await checkAgentAccess(apiClient, authContext, params.agent_name);
          if (!access.allowed) {
            console.log(`[list_operator_queue] Access denied: ${access.reason}`);
            return accessDenied(context, { error: "Access denied", reason: access.reason });
          }
        }

        try {
          const result = await apiClient.listOperatorQueue({
            status: params.status,
            type: params.type,
            priority: params.priority,
            agent_name: params.agent_name,
            since: params.since,
            limit: params.limit,
            offset: params.offset,
          });

          let items = result.items || [];

          // Broad listing under an agent-scoped key: the backend filtered to the
          // KEY OWNER's accessible agents — broader than this agent's permits.
          // Post-filter to {self} ∪ permitted. system/user scopes pass through.
          if (
            !params.agent_name &&
            authContext?.scope === "agent" &&
            authContext?.agentName
          ) {
            const caller = authContext.agentName;
            const permitted = await apiClient.getPermittedAgents(caller);
            const allowed = new Set([caller, ...permitted]);
            const before = items.length;
            items = filterQueueItemsForAgentScope(items, allowed);
            console.log(
              `[list_operator_queue] Agent '${caller}' filtered: ${items.length}/${before} items visible`,
            );
          }

          return JSON.stringify({ count: items.length, items }, null, 2);
        } catch (error) {
          const msg = error instanceof Error ? error.message : String(error);
          console.error(`[list_operator_queue] error: ${msg}`);
          return JSON.stringify({ error: msg }, null, 2);
        }
      },
    },

    // ========================================================================
    // get_operator_queue_item
    // ========================================================================
    getOperatorQueueItem: {
      name: "get_operator_queue_item",
      description:
        "Get a single Operating Room (operator queue) item by id — full detail " +
        "including title, question, options, context, status, priority, and any " +
        "operator response. Read-only. Access control: agent-scoped keys may " +
        "only read items belonging to themselves or agents they have explicit " +
        "permission for.",
      parameters: z.object({
        item_id: z.string().min(1).describe("Operator queue item id."),
      }),
      execute: async (
        params: { item_id: string },
        context?: { session?: McpAuthContext },
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        let item: { agent_name: string };
        try {
          item = await apiClient.getOperatorQueueItem(params.item_id);
        } catch (error) {
          const msg = error instanceof Error ? error.message : String(error);
          console.error(`[get_operator_queue_item] error: ${msg}`);
          return JSON.stringify({ error: msg }, null, 2);
        }

        // MCP-layer agent_permissions gate: the backend returned this item under
        // the KEY OWNER's access — re-check it against the calling agent's
        // permits before handing it over.
        const access = await checkAgentAccess(apiClient, authContext, item.agent_name);
        if (!access.allowed) {
          console.log(`[get_operator_queue_item] Access denied: ${access.reason}`);
          return accessDenied(context, { error: "Access denied", reason: access.reason });
        }

        return JSON.stringify(item, null, 2);
      },
    },

    // ========================================================================
    // respond_to_operator_queue (#1104)
    // ========================================================================
    respondToOperatorQueue: {
      name: "respond_to_operator_queue",
      description:
        "Respond to (resolve) a pending Operating Room (operator queue) item — " +
        "answer a question, or approve/deny an approval request. `response` is " +
        "the decision value (e.g. the chosen approval option, or the answer); " +
        "`response_text` is optional freeform context. Only items in the " +
        "'pending' state can be resolved — responding to an already-resolved, " +
        "expired, or cancelled item returns a structured error. Access control: " +
        "agent-scoped keys may only resolve items belonging to themselves or " +
        "agents they have explicit permission for.",
      parameters: z.object({
        item_id: z.string().min(1).describe("Operator queue item id to resolve."),
        response: z
          .string()
          .min(1)
          .describe(
            "The response/decision value — for an approval item the chosen option (e.g. 'approve'/'deny'); for a question, the answer.",
          ),
        response_text: z
          .string()
          .optional()
          .describe("Optional freeform text accompanying the response."),
      }),
      execute: async (
        params: { item_id: string; response: string; response_text?: string },
        context?: { session?: McpAuthContext },
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        // Resolve the item's agent_name first so the permission check has a
        // target (the caller only supplies an id). A read here also surfaces a
        // 404 cleanly before any write attempt.
        let item: { agent_name: string };
        try {
          item = await apiClient.getOperatorQueueItem(params.item_id);
        } catch (error) {
          const msg = error instanceof Error ? error.message : String(error);
          console.error(`[respond_to_operator_queue] lookup error: ${msg}`);
          return JSON.stringify({ error: msg }, null, 2);
        }

        // Same MCP-layer agent_permissions gate as the read tools — the backend
        // resolved the item under the KEY OWNER's access, so re-check it against
        // the calling agent's permits before allowing a write.
        const access = await checkAgentAccess(apiClient, authContext, item.agent_name);
        if (!access.allowed) {
          console.log(`[respond_to_operator_queue] Access denied: ${access.reason}`);
          return accessDenied(context, { error: "Access denied", reason: access.reason });
        }

        try {
          const updated = await apiClient.respondToOperatorQueueItem(params.item_id, {
            response: params.response,
            response_text: params.response_text,
          });
          return JSON.stringify(updated, null, 2);
        } catch (error) {
          // Backend 400s on a non-pending item (already responded / expired /
          // cancelled) — surface as a structured error, not a thrown exception.
          const msg = error instanceof Error ? error.message : String(error);
          console.error(`[respond_to_operator_queue] error: ${msg}`);
          return JSON.stringify({ error: msg }, null, 2);
        }
      },
    },
  };
}
