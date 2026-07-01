/**
 * Dynamic dedicated-agent chat tools (#846)
 *
 * When an owner toggles `mcp_exposed` on an agent, the Trinity MCP server
 * registers a dedicated `chat_with_<slug>` tool at runtime (no restart) that is
 * functionally identical to `chat_with_agent` with the agent name pre-filled.
 *
 * Two pieces live here:
 *   - makeDedicatedChatTool: the tool factory (delegates to runAgentChat — no
 *     logic fork).
 *   - startExposedToolsReconciler: a poll loop over the backend's internal
 *     endpoint that diffs the exposed set and calls register/unregister handles
 *     exposed by createServer. Fail-open + in-flight mutex.
 *
 * Refresh is POLL (not WebSocket): the backend is the single source of truth for
 * the deterministic, collision-free tool name; FastMCP pushes
 * `notifications/tools/list_changed` to connected clients on add/remove.
 */

import { z } from "zod";
import { TrinityClient } from "../client.js";
import { runAgentChat } from "./chat.js";

/** One exposed agent as returned by GET /api/internal/mcp-exposed-agents. */
export interface ExposedAgentSpec {
  agent_name: string;
  tool_name: string;
  description: string;
}

/**
 * Build a dedicated chat tool bound to one agent. The schema mirrors
 * chat_with_agent minus `agent_name`; execute delegates to the shared
 * runAgentChat body so routing/idempotency/access are identical.
 */
export function makeDedicatedChatTool(
  client: TrinityClient,
  requireApiKey: boolean,
  agentChatPullEnabled: boolean,
  agentName: string,
  toolName: string,
  description: string
) {
  return {
    name: toolName,
    description,
    parameters: z.object({
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
          "Use for independent tasks that don't need conversation history."
        ),
      model: z
        .string()
        .optional()
        .describe(
          "Model override for this request — short alias (sonnet, opus, haiku) or a full ID. Only applies when parallel=true."
        ),
      allowed_tools: z
        .array(z.string())
        .optional()
        .describe("Restrict which tools the agent can use. Only applies when parallel=true."),
      system_prompt: z
        .string()
        .optional()
        .describe("Additional system prompt to append for this task. Only applies when parallel=true."),
      timeout_seconds: z
        .number()
        .optional()
        .describe("Per-task timeout override (clamped to the agent cap). Only applies when parallel=true."),
      async: z
        .boolean()
        .optional()
        .default(false)
        .describe(
          "If true, return immediately with execution_id (fire-and-forget). Only applies when parallel=true."
        ),
      inject_result: z
        .boolean()
        .optional()
        .default(false)
        .describe(
          "If true and this is a self-task (the bound agent calling itself), inject the result as a message in your current chat session. Requires chat_session_id. Only applies to self-calls."
        ),
      chat_session_id: z
        .string()
        .optional()
        .describe(
          "Chat session ID to link this self-task to. Required for inject_result=true."
        ),
    }),
    execute: async (
      params: {
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
      // Agent name is bound — never taken from caller input. Full parity with
      // chat_with_agent (incl. self-task inject_result/chat_session_id) so the
      // "functionally identical" contract holds with no logic fork.
      return runAgentChat(
        client,
        requireApiKey,
        agentChatPullEnabled,
        agentName,
        {
          message: params.message,
          parallel: params.parallel,
          model: params.model,
          allowed_tools: params.allowed_tools,
          system_prompt: params.system_prompt,
          timeout_seconds: params.timeout_seconds,
          async: params.async,
          inject_result: params.inject_result,
          chat_session_id: params.chat_session_id,
        },
        context
      );
    },
  };
}

export interface ReconcilerOptions {
  trinityApiUrl: string;
  internalSecret: string;
  client: TrinityClient;
  requireApiKey: boolean;
  agentChatPullEnabled: boolean;
  /** Register a dynamic tool with the connector-denied gate + bound audit target. */
  registerDynamicTool: (tool: any, canAccess: (auth: any) => boolean, auditTargetId: string) => void;
  /** Remove a previously-registered dynamic tool by name. */
  unregisterDynamicTool: (name: string) => void;
  /** Connector-tier visibility gate (mirrors operator tools). */
  connectorDenied: (auth: any) => boolean;
  /** Built-in tool names — the final collision guard. */
  builtinToolNames: Set<string>;
  /** Poll interval (ms). Default ~20s. */
  intervalMs?: number;
  /** Run one sync immediately on start (default true; tests set false to drive syncOnce deterministically). */
  runImmediately?: boolean;
  /** Injectable fetch for tests. */
  fetchImpl?: typeof fetch;
}

export interface ReconcilerHandle {
  /** Run one reconcile pass (also called by the interval). */
  syncOnce: () => Promise<void>;
  /** Stop the interval. */
  stop: () => void;
  /** Current agentName→toolName registered set (for tests/observability). */
  getCurrent: () => Map<string, string>;
}

/**
 * Poll the backend for exposed agents and reconcile dedicated tools.
 *
 * Fail-open: only mutate the tool set on a 200 with a valid array. On any
 * error / non-200 / parse failure, log and keep the last-known set (no flap).
 * An in-flight mutex prevents the startup sync and the interval tick from
 * overlapping and corrupting the diff map.
 */
export function startExposedToolsReconciler(opts: ReconcilerOptions): ReconcilerHandle {
  const {
    trinityApiUrl,
    internalSecret,
    client,
    requireApiKey,
    agentChatPullEnabled,
    registerDynamicTool,
    unregisterDynamicTool,
    connectorDenied,
    builtinToolNames,
    intervalMs = 20_000,
    runImmediately = true,
    fetchImpl = fetch,
  } = opts;

  // agentName -> toolName currently registered.
  const current = new Map<string, string>();
  let inFlight = false;

  async function syncOnce(): Promise<void> {
    if (inFlight) return; // mutex — startup sync ∥ interval tick can't race
    inFlight = true;
    try {
      let resp: Response;
      try {
        resp = await fetchImpl(`${trinityApiUrl}/api/internal/mcp-exposed-agents`, {
          headers: { "X-Internal-Secret": internalSecret },
        });
      } catch (e) {
        console.error("[#846 reconciler] poll failed (network) — keeping last-known set:", e);
        return; // fail-open
      }

      if (!resp.ok) {
        console.error(`[#846 reconciler] poll non-200 (${resp.status}) — keeping last-known set`);
        return; // fail-open
      }

      let body: any;
      try {
        body = await resp.json();
      } catch (e) {
        console.error("[#846 reconciler] poll parse failure — keeping last-known set:", e);
        return; // fail-open
      }

      if (!body || !Array.isArray(body.agents)) {
        console.error("[#846 reconciler] poll shape invalid — keeping last-known set");
        return; // fail-open
      }

      // Build desired agentName -> {toolName, description}, applying the final
      // built-in-collision guard (backend is authoritative for the slug, but we
      // never shadow a built-in tool).
      const desired = new Map<string, ExposedAgentSpec>();
      for (const a of body.agents as ExposedAgentSpec[]) {
        if (!a || typeof a.agent_name !== "string" || typeof a.tool_name !== "string") continue;
        if (builtinToolNames.has(a.tool_name)) {
          console.warn(`[#846 reconciler] skipping '${a.agent_name}': tool_name '${a.tool_name}' collides with a built-in tool`);
          continue;
        }
        desired.set(a.agent_name, {
          agent_name: a.agent_name,
          tool_name: a.tool_name,
          description: typeof a.description === "string" ? a.description : `Chat directly with the "${a.agent_name}" agent.`,
        });
      }

      // Unregister gone or re-slugged agents.
      for (const [name, toolName] of [...current.entries()]) {
        const want = desired.get(name);
        if (!want || want.tool_name !== toolName) {
          unregisterDynamicTool(toolName);
          current.delete(name);
        }
      }

      // Register new / re-slugged agents. The backend guarantees tool_name
      // uniqueness over the full set, but guard defensively: a duplicate
      // tool_name would make server.addTool overwrite/throw. Track names already
      // claimed this pass (kept tools included) and skip a dupe rather than
      // corrupt the set.
      const claimedToolNames = new Set<string>(current.values());
      for (const [name, spec] of desired.entries()) {
        if (current.get(name) === spec.tool_name) continue; // unchanged (already claimed)
        if (claimedToolNames.has(spec.tool_name)) {
          console.warn(`[#846 reconciler] duplicate tool_name '${spec.tool_name}' for agent '${name}' — skipping (backend should guarantee uniqueness)`);
          continue;
        }
        const tool = makeDedicatedChatTool(
          client,
          requireApiKey,
          agentChatPullEnabled,
          name,
          spec.tool_name,
          spec.description
        );
        // Per-tool guard: a single addTool failure must not abort the whole pass
        // (and starve every later agent). On failure, leave `name` out of
        // `current` so the next poll retries it.
        try {
          registerDynamicTool(tool, connectorDenied, name);
          current.set(name, spec.tool_name);
          claimedToolNames.add(spec.tool_name);
        } catch (e) {
          console.error(`[#846 reconciler] register failed for '${name}' (${spec.tool_name}) — will retry next poll:`, e);
        }
      }
    } finally {
      inFlight = false;
    }
  }

  // One sync on startup, then poll.
  if (runImmediately) void syncOnce();
  const timer = setInterval(() => {
    void syncOnce();
  }, intervalMs);
  // Don't keep the event loop alive solely for the poll.
  if (typeof (timer as any).unref === "function") (timer as any).unref();

  return {
    syncOnce,
    stop: () => clearInterval(timer),
    getCurrent: () => current,
  };
}
