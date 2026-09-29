/**
 * #2392 — the execution id an effect tool sends to the backend.
 *
 * The agent's MCP config sends `X-Trinity-Execution-Id` on every request: the
 * current turn's execution id, or the literal `manual` for an interactive
 * session with no execution (forwarded as-is; the backend interprets it).
 * fastmcp re-runs `authenticate` on every POST, so the value on the auth
 * context is per-request. The agent-supplied `execution_id` param remains a
 * fallback for agent images that predate the header.
 */
import type { McpAuthContext } from "../types.js";

export const EXECUTION_ID_HEADER = "x-trinity-execution-id";

// 128 matches the backend's `execution_id` max_length, so an over-long value is
// dropped here instead of failing Pydantic validation downstream.
const EXECUTION_ID_RE = /^[A-Za-z0-9_.:-]{1,128}$/;

/** Trimmed header value, or undefined when absent or malformed (ignored, never an error). */
export function parseExecutionIdHeader(value: string | undefined): string | undefined {
  const v = value?.trim();
  return v && EXECUTION_ID_RE.test(v) ? v : undefined;
}

/** Header value wins; the agent-supplied param is the fallback. */
export function resolveExecutionId(
  authContext: McpAuthContext | undefined,
  paramValue: string | undefined
): string | undefined {
  return authContext?.executionId ?? paramValue;
}
