/**
 * MCP Audit Logging (SEC-001 Phase 3)
 *
 * Fire-and-forget audit logging for MCP tool calls. POSTs entries to the
 * backend's internal audit endpoint (/api/internal/audit) using the shared
 * internal secret (C-003).
 *
 * Design constraints:
 * - Never block or delay tool execution — audit is best-effort
 * - Never throw — errors are logged to stderr and swallowed
 * - Captures tool name, MCP auth context, timing, and success/failure
 */

import type { McpAuthContext } from "./types.js";

const TRINITY_API_URL =
  process.env.TRINITY_API_URL || "http://localhost:8000";
const INTERNAL_SECRET = process.env.INTERNAL_API_SECRET || "";

interface AuditEntry {
  event_type: string;
  event_action: string;
  source: string;
  mcp_key_id?: string;
  mcp_key_name?: string;
  mcp_scope?: string;
  actor_agent_name?: string;
  target_type?: string;
  target_id?: string;
  request_id?: string;
  details?: Record<string, unknown>;
}

/**
 * Audit-wrapper call context (#905). The session is the MCP auth context; a
 * tool may also stamp a per-call `requestId` here (e.g. the git tools) — the
 * wrapper then carries it onto the `mcp_operation` row so it joins the backend
 * row created by the same forwarded X-Request-ID.
 */
export interface ToolCallContext {
  session?: McpAuthContext;
  requestId?: string;
}

/**
 * Resolve the audit target_id from a tool's params. Most agent-scoped tools
 * take the agent as `agent_name`; a few use `name`. Defensive — returns
 * undefined when neither is a string, so non-agent tools log no target.
 */
export function resolveTargetId(params: unknown): string | undefined {
  if (params && typeof params === "object") {
    const p = params as Record<string, unknown>;
    if (typeof p.agent_name === "string") return p.agent_name;
    if (typeof p.name === "string") return p.name;
  }
  return undefined;
}

/**
 * Post an audit entry to the backend. Fire-and-forget — never throws.
 */
async function postAudit(entry: AuditEntry): Promise<void> {
  try {
    if (!INTERNAL_SECRET) {
      // No secret configured — skip silently (local dev without docker)
      return;
    }

    const response = await fetch(`${TRINITY_API_URL}/api/internal/audit`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Internal-Secret": INTERNAL_SECRET,
      },
      body: JSON.stringify(entry),
    });

    if (!response.ok) {
      console.error(
        `[audit] POST /api/internal/audit failed: ${response.status} ${response.statusText}`
      );
    }
  } catch (error) {
    // Swallow — audit failures must never affect tool execution
    console.error(`[audit] failed to post audit entry:`, error);
  }
}

/**
 * Log an MCP tool call. Called by the audit wrapper in server.ts.
 */
export function logToolCall(
  toolName: string,
  authContext: McpAuthContext | undefined,
  durationMs: number,
  success: boolean,
  errorMessage?: string,
  targetId?: string,
  requestId?: string
): void {
  const details: Record<string, unknown> = {
    tool: toolName,
    duration_ms: durationMs,
    success,
  };
  if (errorMessage) {
    details.error = errorMessage;
  }

  // #905: target_id (the agent the tool acted on, resolved from params) and
  // request_id (the per-call correlation id a tool forwarded as X-Request-ID)
  // were both previously dropped — "we don't have params here". The wrapper now
  // passes them through so the row attributes the action AND joins to the
  // backend row triggered by the same id.
  const entry: AuditEntry = {
    event_type: "mcp_operation",
    event_action: "tool_call",
    source: "mcp",
    mcp_key_id: authContext?.keyId,
    mcp_key_name: authContext?.keyName,
    mcp_scope: authContext?.scope,
    actor_agent_name: authContext?.agentName,
    target_type: targetId ? "agent" : undefined,
    target_id: targetId,
    request_id: requestId,
    details,
  };

  // Fire and forget — don't await in the calling code path
  postAudit(entry).catch(() => {});
}

/**
 * Wrap a tool's execute function with audit logging.
 *
 * Returns a new execute function that:
 * 1. Records start time
 * 2. Calls original execute
 * 3. Fires audit log (non-blocking)
 * 4. Returns original result
 */
export function withAudit<T>(
  toolName: string,
  execute: (params: T, context?: ToolCallContext) => Promise<string>
): (params: T, context?: ToolCallContext) => Promise<string> {
  return async (params: T, context?: ToolCallContext) => {
    const start = Date.now();
    const authContext = context?.session;
    const targetId = resolveTargetId(params);

    try {
      const result = await execute(params, context);
      // Read requestId AFTER execute: a tool (e.g. git.ts) stamps it on the
      // shared context object so this row carries the same id it forwarded.
      logToolCall(toolName, authContext, Date.now() - start, true, undefined, targetId, context?.requestId);
      return result;
    } catch (error) {
      const msg = error instanceof Error ? error.message : String(error);
      logToolCall(toolName, authContext, Date.now() - start, false, msg, targetId, context?.requestId);
      throw error;
    }
  };
}
