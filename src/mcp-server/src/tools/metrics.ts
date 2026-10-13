/**
 * Declared business metrics (trinity-enterprise#478)
 *
 * `record_metrics` is the ONLY way a business metric enters Trinity. Points are
 * validated against the metrics the agent's own `template.yaml` declares
 * (ent#477's registry) and stored as a real time series, which is what a
 * dashboard, a canvas chart and a comparison against last month can then read
 * — as opposed to a number written into prose, which nothing can aggregate.
 *
 * The agent is resolved server-side from the MCP auth context, never from tool
 * input, so a point cannot be attributed to another agent.
 *
 * The tool NEVER throws. Every failure comes back as a result with a flag,
 * because a thrown error ends the agent's turn over what is usually a
 * correctable mistake — and the 422 body carries a reason code per point
 * precisely so the agent can fix it and re-send.
 *
 * ent#479 adds `get_metrics` to this module (the read half) — ent#727 lets it
 * read another agent's numbers through a permission grant — and ent#666
 * adds `get_objectives` — the same numbers against the targets an objective
 * file sets for them, which is the ONE place a gap is computed.
 */

import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";
import { resolveActingAgent } from "../access.js";

/** Kept in step with `models.METRIC_BATCH_MAX_POINTS`; the backend enforces. */
const MAX_POINTS = 1000;

/**
 * Map a backend failure onto result flags.
 *
 * The retryable/not-retryable split is the load-bearing part: 503 is a store
 * outage and retrying is right, while a 500 here means the batch itself could
 * not be stored and will fail identically forever — an agent told to retry
 * that would retry forever.
 */
export function classifyMetricError(message: string): Record<string, boolean> {
  const flags: Record<string, boolean> = {};
  // #186: the agent-access dependency returns a uniform 404 for both an absent
  // and an inaccessible agent, so a 404 on this dep-gated route is an
  // authorization answer, not a routing one.
  if (/\b403\b/.test(message) || /\b404\b/.test(message)) flags.not_authorized = true;
  if (/\b413\b/.test(message)) flags.payload_too_large = true;
  if (/\b422\b/.test(message)) flags.invalid = true;
  if (/\b409\b/.test(message)) {
    flags.in_flight = true;
    flags.retryable = true;
  }
  if (/\b429\b/.test(message)) {
    // Two different 429s with two different remedies: wait a moment, versus
    // wait until tomorrow (or ask the operator to raise the cap).
    if (/daily_point_cap_exceeded/.test(message)) flags.quota_exceeded = true;
    else flags.rate_limited = true;
  }
  if (/\b503\b/.test(message)) flags.retryable = true;
  if (/\b500\b/.test(message)) flags.retryable = false;
  return flags;
}

/** Seconds to wait, if the backend said. */
export function extractRetryAfter(message: string): number | undefined {
  const match = /retry[- ]after[":\s]+(\d+)/i.exec(message);
  return match ? Number(match[1]) : undefined;
}

export function createMetricsTools(client: TrinityClient, requireApiKey: boolean) {
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
   * Resolve the recording agent from the auth context. These tools act AS the
   * caller and take no target parameter to spoof (the `report` / `set_canvas`
   * shape), so the identity comes from the key — one shared rule
   * (`resolveActingAgent`, #2975), which admits the platform orchestrator's
   * system-scoped key alongside an agent-scoped one.
   */
  const getAgentName = (
    authContext: McpAuthContext | undefined,
    toolName: string,
  ): string => resolveActingAgent(authContext, `The ${toolName} tool`);

  return {
    // ========================================================================
    // record_metrics — push observations of declared metrics
    // ========================================================================
    recordMetrics: {
      name: "record_metrics",
      description:
        "Record one or more observations of your DECLARED business metrics. " +
        "This is the only way a metric value enters Trinity as data rather than prose — " +
        "recorded points build a real time series that dashboards, canvas charts and " +
        "period-over-period comparisons read. Each point is {metric, value, ts?, dims?}. " +
        `Up to ${MAX_POINTS} points per call, all-or-nothing: if any point is invalid the ` +
        "whole batch is refused and each bad point comes back with a reason code and a fix. " +
        "A metric must be declared in your template.yaml `metrics:` block first — if you get " +
        "`metric_undeclared`, add it there and call refresh_metric_definitions. " +
        "Identity is (metric, ts, dims): re-sending the same value is deduplicated rather " +
        "than double-counted, and a different value at the same (metric, ts, dims) CORRECTS " +
        "the stored point in place — keep the ts of the period the number describes, and the " +
        "result counts it as `corrected`. Stamp `ts` yourself (RFC 3339 with an offset, e.g. 2026-09-22T08:00:00Z) " +
        "for an observation about a specific moment; omit it for 'now'. Pass `execution_id` " +
        "(from your Execution Context block) so a re-delivered turn replays instead of " +
        "recording twice — without it, and without `ts`, a retry is a new observation.",
      parameters: z.object({
        points: z
          .array(
            z.object({
              metric: z
                .string()
                .describe("A metric name declared in your template.yaml `metrics:` block."),
              value: z
                .union([z.number(), z.string()])
                .describe(
                  "A finite number for counter/gauge/percentage/duration/bytes metrics, " +
                    "or one of the declared status labels for a `status` metric. " +
                    "Not coerced: \"42\" is not 42, and true is not 1. " +
                    "A text value is a label, not a document: at most 1024 characters.",
                ),
              ts: z
                .string()
                .optional()
                .describe(
                  "RFC 3339 with an explicit offset (2026-09-22T08:00:00Z or +02:00). " +
                    "Defaults to now. A naive timestamp is refused as ambiguous.",
                ),
              dims: z
                .record(z.string(), z.string())
                .optional()
                .describe(
                  "Declared dimension keys → string labels, e.g. {region: 'eu'}. " +
                    "Only keys this metric declares are accepted; values are strings.",
                ),
            }),
          )
          .min(1)
          .max(MAX_POINTS)
          .describe("The observations to record."),
        idempotency_key: z
          .string()
          .max(128)
          .optional()
          .describe(
            "Optional. Re-sending the same batch under the same key returns the FIRST " +
              "result instead of recording again. The key is bound to the batch CONTENT, " +
              "so a different set of points under a reused key is still recorded — you " +
              "cannot lose data by reusing a key, only by re-sending identical points. " +
              "That includes restating a value back after correcting it: an identical " +
              "earlier batch replays (`replayed: true`, nothing written), so use a new key.",
          ),
        execution_id: z
          .string()
          .max(128)
          .optional()
          .describe(
            "Optional. The execution_id of the turn you are recording from (it is in your " +
              "Execution Context block). Links the batch to the turn and makes a re-delivered " +
              "turn replay rather than record twice. Within one turn, re-sending a batch " +
              "identical to an earlier one replays it (`replayed: true`, nothing written) even " +
              "if you corrected the value in between — pass a fresh `idempotency_key` to " +
              "restate it back.",
          ),
      }),
      execute: async (
        params: {
          points: Array<{
            metric: string;
            value: number | string;
            ts?: string;
            dims?: Record<string, string>;
          }>;
          idempotency_key?: string;
          execution_id?: string;
        },
        context?: { session?: McpAuthContext },
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        let agentName: string;
        try {
          agentName = getAgentName(authContext, "record_metrics");
        } catch (error) {
          return JSON.stringify(
            { success: false, error: error instanceof Error ? error.message : String(error) },
            null,
            2,
          );
        }

        console.log(`[record_metrics] ${agentName}: ${params.points.length} point(s)`);

        try {
          const result = await apiClient.recordMetrics(agentName, {
            points: params.points,
            idempotency_key: params.idempotency_key,
            execution_id: params.execution_id,
          });
          return JSON.stringify(
            {
              success: true,
              agent_name: result.agent_name,
              // Separate counts, on purpose: "we already had this" is an honest
              // outcome and must not read as a write that happened, and a
              // restated point (ent#729) is neither. `?? 0`: a backend that
              // predates corrections can never restate a row.
              recorded: result.recorded,
              deduplicated: result.deduplicated,
              corrected: result.corrected ?? 0,
              replayed: result.replayed,
              points: result.points,
            },
            null,
            2,
          );
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          console.error(`[record_metrics] Error: ${message}`);
          const flags = classifyMetricError(message);
          const retryAfter = extractRetryAfter(message);
          // The per-point errors are surfaced VERBATIM — they carry the reason
          // code and the hint that let the agent fix the batch itself. They
          // never contain the offending value (the backend omits it).
          const errors = extractPointErrors(message);
          return JSON.stringify(
            {
              success: false,
              error: message,
              ...flags,
              ...(retryAfter !== undefined ? { retry_after: retryAfter } : {}),
              ...(errors ? { errors } : {}),
            },
            null,
            2,
          );
        }
      },
    },

    // ========================================================================
    // get_metrics — read recorded metrics, with freshness (ent#479); another
    // agent's when you hold a permission grant on it (ent#727)
    // ========================================================================
    getMetrics: {
      name: "get_metrics",
      description:
        "Read recorded business metrics — YOURS by default, or another agent's with `agent`: " +
        "what it declared, the latest value of each, " +
        "how fresh it is, and a bounded series for charting. Answers from the point store, " +
        "so it works whether or not you are mid-turn and whether or not you have a " +
        "dashboard.yaml. A metric is STALE when no point has arrived within 2x its declared " +
        "cadence — a metric with no declared cadence is never stale (freshness: no_cadence), " +
        "and one that has never been recorded reads freshness: no_points, not stale. " +
        "By default every declared metric comes back with a downsampled series (<=120 " +
        "buckets each); pass `metric` to get the raw points of ONE metric (newest first, " +
        "capped by series_limit, `truncated` says when older points were dropped). " +
        "Dimensioned metrics come back as one series per dimension tuple, with `latest` " +
        "folded across them using the aggregation you declared. Chart `chart.buckets`, " +
        "not `series[0]` — `chart` is the one series that matches `latest`: the fold " +
        "across every dimension for sum/avg, and for `last` the single series named by " +
        "`chart.dims` (basis: series), because a cross-series `last` is not one number. " +
        "Pass `agent` to read ANOTHER agent's metrics: you must hold a permission grant on " +
        "it — the same grant that lets you chat_with_agent it, configured by an operator in " +
        "the Trinity UI. Without one the call is refused with `Access denied` (\"Permission " +
        "denied: Agent '<you>' is not permitted to communicate with '<agent>'\") or " +
        "`not_authorized`; that is not retryable. The answer has exactly the same shape and " +
        "stale rule as your own read.",
      parameters: z.object({
        agent: z
          .string()
          .optional()
          .describe(
            "Optional. Another agent whose metrics to read; omit for your own. Requires a " +
              "permission grant on that agent (the one chat_with_agent uses).",
          ),
        metric: z
          .string()
          .optional()
          .describe(
            "Optional. One declared metric name. Narrows the read to it AND switches the " +
              "series to raw points instead of buckets. An undeclared name comes back as " +
              "undeclared: true, not as an error you should retry.",
          ),
        window: z
          .enum(["auto", "24h", "7d", "30d", "90d"])
          .optional()
          .describe(
            "How far back to read. Default `auto` sizes the window to your declared " +
              "cadence (12 intervals, at least 24h, at most 90d), so a weekly metric still " +
              "has points in it.",
          ),
        since: z
          .string()
          .optional()
          .describe(
            "Optional RFC 3339 start, overriding `window` (e.g. month-to-date). The span " +
              "may not exceed the retention window.",
          ),
        until: z
          .string()
          .optional()
          .describe("Optional RFC 3339 end; defaults to now. Only meaningful with `since`."),
        include_retired: z
          .boolean()
          .optional()
          .describe(
            "Include metrics your template no longer declares but that still have points.",
          ),
      }),
      execute: async (
        params: {
          agent?: string;
          metric?: string;
          window?: string;
          since?: string;
          until?: string;
          include_retired?: boolean;
        },
        context?: { session?: McpAuthContext },
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);
        const { agent, ...options } = params;

        // A named target has already passed `checkAgentEdge` (the `enforce`
        // row on `agent` in access.ts) and the backend re-checks the grant;
        // an empty string reads as omitted there, and so it does here.
        let agentName: string;
        if (agent) {
          agentName = agent;
        } else {
          try {
            agentName = getAgentName(authContext, "get_metrics");
          } catch (error) {
            return JSON.stringify(
              { success: false, error: error instanceof Error ? error.message : String(error) },
              null,
              2,
            );
          }
        }

        try {
          const result = await apiClient.getAgentMetrics(agentName, options);
          return JSON.stringify({ success: true, ...result }, null, 2);
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          console.error(`[get_metrics] Error: ${message}`);
          const flags = classifyMetricError(message);
          // `metric_undeclared` is a 422, which classifies as `invalid`. Name
          // it separately with the fix attached: the remedy is a template edit
          // plus a refresh, not a retry of this read.
          if (/metric_undeclared/.test(message)) {
            return JSON.stringify(
              {
                success: false,
                undeclared: true,
                error: message,
                hint:
                  "declare it in template.yaml `metrics:` and call " +
                  "refresh_metric_definitions (or pass include_retired: true if it was retired)",
                ...flags,
              },
              null,
              2,
            );
          }
          return JSON.stringify({ success: false, error: message, ...flags }, null, 2);
        }
      },
    },

    // ========================================================================
    // get_objectives — what you are supposed to move, and where you are
    // ========================================================================
    getObjectives: {
      name: "get_objectives",
      // #3234: Claude Code shows a model only the first 2,048 characters of a
      // description, and this tool has no parameter to carry detail — so the
      // rules a model acts on come first and the wording stays inside the cap.
      description:
        "Read YOUR objectives joined to your metrics: for each objective you own or " +
        "support, every metric it names with its target, your current actual, how fresh " +
        "that number is, and the gap. This is the one place target and actual sit side by " +
        "side — do not re-derive a gap from get_metrics and an objective file (two answers " +
        "to one question is what this read removes). " +
        "`stale: true` means DO NOT ACT ON THIS NUMBER: no point arrived within 2x the " +
        "declared cadence, so recording a fresh one is the next action, not reporting the " +
        "gap (still computed beside it, for reference). " +
        "`gap.status` is POSITION relative to the target given direction, NEVER pace: " +
        "`behind` means on the wrong side of the target now, not late — judge pace yourself " +
        "from `by` and `horizon`, on every row. A metric to be HELD reads `on_target` " +
        "(within `tolerance`) or `off_target`, never behind/ahead. `not_computable` says why " +
        "in `gap.reason`. " +
        "`direction` is the declared-metric vocabulary only: `up_good`, `down_good`, " +
        "`neutral`, or null when undeclared. A HELD objective arrives as `neutral` with " +
        "`direction_source: \"objective\"` (your word kept as `objective_direction`). " +
        "A `role: \"guard\"` row must not move: always held, counted in `summary.guards`. " +
        "A metric the objective names but you do not declare is a row with `declared: false` " +
        "and a `finding` naming the fix (add it to template.yaml `metrics:` and call " +
        "refresh_metric_definitions) — never a blank. If it belongs to the role that OWNS " +
        "the objective and you only support it, the finding is `metric_not_declared_here` " +
        "and there is nothing for you to fix. " +
        "Objectives are read from your own canon files on every call: a `status:` other than " +
        "`active` is not returned; `unavailable` says when the files could not be reached, " +
        "and `source` counts the files listed, read, skipped by name and left unscanned, so " +
        "an empty answer is never ambiguous. `findings` covers only the objectives returned " +
        "to you — another role's YAML mistake is not yours to fix. Only your own objectives; " +
        "there is no agent parameter.",
      parameters: z.object({}),
      execute: async (
        _params: Record<string, never>,
        context?: { session?: McpAuthContext },
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        let agentName: string;
        try {
          agentName = getAgentName(authContext, "get_objectives");
        } catch (error) {
          return JSON.stringify(
            { success: false, error: error instanceof Error ? error.message : String(error) },
            null,
            2,
          );
        }

        try {
          const result = await apiClient.getAgentObjectives(agentName);
          return JSON.stringify({ success: true, ...result }, null, 2);
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          console.error(`[get_objectives] Error: ${message}`);
          return JSON.stringify(
            { success: false, error: message, ...classifyMetricError(message) },
            null,
            2,
          );
        }
      },
    },

    // ========================================================================
    // refresh_metric_definitions — make a template edit take effect
    // ========================================================================
    refreshMetricDefinitions: {
      name: "refresh_metric_definitions",
      description:
        "Re-read your template.yaml and reconcile your declared metrics registry. " +
        "Call this after adding or changing a metric in the `metrics:` block — until you do, " +
        "record_metrics will refuse the new name with `metric_undeclared`. " +
        "Returns what changed: created / updated / revived / retired, plus any type change " +
        "that was REFUSED (a metric's type is frozen once points exist under its name; to " +
        "change the shape, rename the metric).",
      parameters: z.object({}),
      execute: async (
        _params: Record<string, never>,
        context?: { session?: McpAuthContext },
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        let agentName: string;
        try {
          agentName = getAgentName(authContext, "refresh_metric_definitions");
        } catch (error) {
          return JSON.stringify(
            { success: false, error: error instanceof Error ? error.message : String(error) },
            null,
            2,
          );
        }

        try {
          const result = await apiClient.refreshMetricDefinitions(agentName);
          return JSON.stringify({ success: true, agent_name: agentName, ...result }, null, 2);
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          console.error(`[refresh_metric_definitions] Error: ${message}`);
          return JSON.stringify(
            { success: false, error: message, ...classifyMetricError(message) },
            null,
            2,
          );
        }
      },
    },
  };
}

/**
 * Pull the per-point `errors` array out of a 422 body embedded in an error
 * message. Best-effort by design: the flags above already tell the agent WHAT
 * happened, and a message shape this cannot parse must degrade to "no detail"
 * rather than to a thrown error inside the error path.
 */
export function extractPointErrors(message: string): unknown[] | undefined {
  const start = message.indexOf('{"reason"');
  const candidates = start >= 0 ? [message.slice(start)] : [message];
  for (const candidate of candidates) {
    try {
      const parsed = JSON.parse(candidate) as { errors?: unknown };
      if (Array.isArray(parsed.errors)) return parsed.errors;
    } catch {
      // fall through — no structured body available
    }
  }
  const match = /"errors"\s*:\s*(\[[\s\S]*?\])\s*[},]/.exec(message);
  if (match) {
    try {
      const parsed = JSON.parse(match[1]) as unknown;
      if (Array.isArray(parsed)) return parsed;
    } catch {
      return undefined;
    }
  }
  return undefined;
}
