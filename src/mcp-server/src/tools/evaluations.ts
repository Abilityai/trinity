/**
 * Evaluation read (abilityai/trinity-enterprise#937)
 *
 * `get_evaluation_summary` proxies `GET /api/agents/{name}/evaluations/summary`.
 * ONE tool for every reader (plan row 19): the proxy never decides what an
 * agent may see. The backend does — an agent key reads its own rows and the
 * pairs it is a side of (another agent it may reach reads as zeros), and a
 * machine principal never receives a judge's score. The answer carries check
 * ids and reason codes only, never evidence text, so nothing untrusted
 * re-enters the reader's context through this tool.
 *
 * The tool NEVER throws: a failure comes back as `success: false` with flags.
 */

import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";
import { resolveActingAgent, resolveClient } from "../access.js";

/** Map a backend failure onto result flags. */
export function classifyEvaluationError(message: string): Record<string, boolean> {
  const flags: Record<string, boolean> = {};
  // #186: the agent dependency answers absent and inaccessible with one 404.
  if (/\b403\b/.test(message) || /\b404\b/.test(message)) flags.not_authorized = true;
  if (/\b422\b/.test(message)) flags.invalid = true;
  if (/\b503\b/.test(message)) flags.retryable = true;
  return flags;
}

export function createEvaluationTools(client: TrinityClient, requireApiKey: boolean) {
  return {
    getEvaluationSummary: {
      name: "get_evaluation_summary",
      description:
        "Read how an agent's runs were graded over a window: runs evaluated and completed, " +
        "completion_rate, runs that passed the deterministic Tier-0 gate and checks_passed_rate, " +
        "per_check_fail (check id -> runs failing it), and up to 50 `failed` runs, each with " +
        "its execution_id, caller_agent, schedule_id and failed check ids with reason codes. " +
        "Evidence text is never returned. " +
        "Scope: an agent key reads ITS OWN runs, plus the runs between it and another agent " +
        "it is a side of (`caller` filters to runs that agent delegated to this one); naming " +
        "another agent you may reach returns only the pairs you are part of, often zeros. " +
        "Omit `agent_name` to read yourself. An agent or machine key NEVER sees judge scores: " +
        "`quality_mean`, `judge_human_agreement` and `agreement_n` are absent from its copy, " +
        "by rule — absent does not mean ungraded. A person's key sees them. " +
        "`period` is `day` (last 24 h), `week` (last 7 days, default) or `custom` with both " +
        "`since` and `until` (RFC 3339, at most 92 days apart). `truncated: true` means the " +
        "window held more runs than one read folds — narrow it before drawing a conclusion; " +
        "`failed_truncated: true` means more failed runs exist than were listed. Rates are " +
        "null, not 0, when nothing was counted. These are read-only; grades are written by " +
        "the platform, never by the graded agent.",
      parameters: z.object({
        agent_name: z
          .string()
          .max(64)
          .optional()
          .describe("Agent to read. Defaults to the calling agent; required for a person's key."),
        period: z.enum(["day", "week", "custom"]).optional().default("week"),
        since: z.string().max(40).optional().describe("Window start for period=custom (RFC 3339)"),
        until: z.string().max(40).optional().describe("Window end for period=custom (RFC 3339)"),
        caller: z
          .string()
          .max(64)
          .optional()
          .describe("Keep only runs this agent delegated to agent_name"),
      }),
      execute: async (
        params: { agent_name?: string; period?: string; since?: string; until?: string; caller?: string },
        context?: { session?: McpAuthContext },
      ) => {
        const authContext = context?.session;
        let agentName = params.agent_name;
        try {
          if (!agentName) agentName = resolveActingAgent(authContext, "get_evaluation_summary without agent_name");
          const apiClient = resolveClient(client, requireApiKey, authContext);
          const result = await apiClient.getAgentEvaluationSummary(agentName, {
            period: params.period,
            since: params.since,
            until: params.until,
            caller: params.caller,
          });
          return JSON.stringify({ success: true, ...result }, null, 2);
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          console.error(`[get_evaluation_summary] Error: ${message}`);
          return JSON.stringify(
            { success: false, error: message, ...classifyEvaluationError(message) },
            null,
            2,
          );
        }
      },
    },
  };
}
