/**
 * Agent Report Tools (#918)
 *
 * MCP tool for agents to publish structured reports (telemetry / arbitrary
 * domain reports) to the Trinity platform. Reports are persisted, surfaced on
 * the Agent Detail "Reports" tab and a fleet-wide Reports view, and broadcast
 * as a thin WebSocket trigger.
 *
 * The agent + author are resolved server-side from the MCP auth context — never
 * from tool input — so a report cannot be attributed to another agent.
 */

import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";


/**
 * Pure helper: keep only summaries whose agent is in the allowed set. Gates a
 * broad (agent_name-omitted) listing under an agent-scoped key down to
 * {self} ∪ permitted. Exported so a unit test can pin the rule without standing
 * up a backend — mirrors `filterQueueItemsForAgentScope` (#1104).
 */
export function filterReportsForAgentScope<T extends { agent_name: string }>(
  reports: T[],
  allowedNames: Set<string>,
): T[] {
  return reports.filter((r) => allowedNames.has(r.agent_name));
}

export function createReportTools(client: TrinityClient, requireApiKey: boolean) {
  const getClient = (authContext?: McpAuthContext): TrinityClient => {
    if (requireApiKey) {
      if (!authContext?.mcpApiKey) {
        throw new Error("MCP API key authentication required but no API key found in request context");
      }
      const userClient = new TrinityClient(client.getBaseUrl());
      userClient.setToken(authContext.mcpApiKey);
      return userClient;
    }
    return client;
  };

  /**
   * Resolve the reporting agent from the auth context. The report tool is
   * agent-facing: it requires an agent-scoped key so a report can only ever be
   * attributed to the calling agent (no spoofing).
   */
  const getAgentName = (authContext: McpAuthContext | undefined): string => {
    if (authContext?.scope === "agent" && authContext.agentName) {
      return authContext.agentName;
    }
    throw new Error(
      "The report tool requires an agent-scoped API key (it publishes a report as the calling agent)."
    );
  };

  /**
   * Agent-to-agent READ gate (mirrors operator_queue.ts / executions.ts).
   * system → allow; user → allow (the backend already scoped to the user's
   * accessible agents); agent → self, or a target it has been permitted.
   *
   * Note this is the READ path: the write tool above requires an agent-scoped
   * key and the backend self-gates it to the calling agent, so reading another
   * agent's reports never widens what you can WRITE.
   */
  const checkAgentAccess = async (
    apiClient: TrinityClient,
    authContext: McpAuthContext | undefined,
    targetAgent: string,
  ): Promise<{ allowed: boolean; reason?: string }> => {
    if (authContext?.scope === "system") return { allowed: true };
    if (authContext?.scope !== "agent" || !authContext?.agentName) {
      return { allowed: true };
    }
    const caller = authContext.agentName;
    if (targetAgent === caller) return { allowed: true };
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
    // report - Publish a structured report to Trinity
    // ========================================================================
    report: {
      name: "report",
      description:
        "Publish a structured report to Trinity for display on the dashboard. " +
        "Use this to surface domain-specific results that would otherwise be buried in chat: " +
        "leads found, emails sent, deals progressed, KPI snapshots, weekly summaries, health rollups. " +
        "The report is persisted and shown on your agent's Reports tab and the fleet Reports view.",
      parameters: z.object({
        report_type: z.string().describe(
          "Namespaced report type, lower_snake segments joined by '.', " +
          "e.g. 'recon.weekly_summary', 'prospector.leads_found', 'ops.daily_health', 'custom.notes'."
        ),
        title: z.string().max(300).describe("Short human-readable title (required, max 300 chars)."),
        payload: z.record(z.unknown()).describe(
          // Keep in step with REPORT_PAYLOAD_MAX_BYTES (backend `models.py`).
          // This is the SECOND agent-facing statement of the ceiling; it was
          // left at 256 KB while #1537 raised the real cap to 5 MiB — the same
          // divergence the platform prompt carried (#1838 review). The backend
          // is the enforcer; both descriptions merely report it, so neither may
          // understate it or agents pre-aggregate away what the cap now allows.
          "Arbitrary JSON body of the report (max 5 MB serialized)."
        ),
        display_hint: z.enum(["table", "kpi", "markdown", "timeline", "json"]).optional()
          .describe(
            "How the dashboard should render the payload. Omit to let Trinity infer from report_type, " +
            "falling back to a JSON viewer. 'table' = {columns,rows}; 'kpi' = {tiles:[{label,value}]}; " +
            "'markdown' = {markdown}; 'timeline' = {events:[{ts,label,detail}]}; 'json' = raw."
          ),
        schema_version: z.number().int().min(1).max(1000).optional()
          .describe("Optional schema version for this report_type (default 1)."),
        period_start: z.string().optional()
          .describe("Optional ISO-8601 start of the period this report covers."),
        period_end: z.string().optional()
          .describe("Optional ISO-8601 end of the period this report covers."),
        // ent#365 — the audience. Without it a report is operator-only, which
        // is what every report was before this field existed. The backend
        // checks the address against YOUR OWN roster and refuses an address it
        // does not already share you with, so this cannot reach a stranger.
        audience_email: z.string().optional()
          .describe(
            "Optional. The Workspace user this report is FOR — it then appears as a deliverable " +
            "on their agent page, and (with execution_id) as a card in the chat that produced it. " +
            "Must be someone this agent is already shared with. Omit for an operator-only report."
          ),
        // Only meaningful alongside an audience: it places the card in the
        // right conversation. The backend resolves the session itself; the id
        // is never trusted as a conversation pointer.
        execution_id: z.string().optional()
          .describe(
            "Optional. The execution_id of the turn you are publishing from, so the deliverable " +
            "appears in that Workspace chat. Omit for a scheduled run — it still lists on the agent page."
          ),
      }),
      execute: async (
        params: {
          report_type: string;
          title: string;
          payload: Record<string, unknown>;
          display_hint?: "table" | "kpi" | "markdown" | "timeline" | "json";
          schema_version?: number;
          period_start?: string;
          period_end?: string;
          audience_email?: string;
          execution_id?: string;
        },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        let agentName: string;
        try {
          agentName = getAgentName(authContext);
        } catch (error) {
          return JSON.stringify(
            { success: false, error: error instanceof Error ? error.message : String(error) },
            null,
            2
          );
        }

        console.log(`[report] ${agentName}: ${params.report_type} - ${params.title}`);

        try {
          const result = await apiClient.createReport(agentName, {
            report_type: params.report_type,
            title: params.title,
            payload: params.payload,
            display_hint: params.display_hint,
            schema_version: params.schema_version,
            period_start: params.period_start,
            period_end: params.period_end,
            audience_email: params.audience_email,
            execution_id: params.execution_id,
          });
          return JSON.stringify(
            {
              success: true,
              report_id: result.id,
              agent_name: result.agent_name,
              report_type: result.report_type,
              created_at: result.created_at,
              // Echoed so an agent can tell an addressed deliverable from an
              // operator-only one without re-reading it.
              addressed_to: params.audience_email ?? null,
            },
            null,
            2
          );
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          console.error(`[report] Error: ${message}`);
          const flags: Record<string, boolean> = {};
          // #186: the backend agent-access dependency now returns a uniform 404
          // for both non-existent and inaccessible agents (was 403), so treat a
          // 404 on this dep-gated endpoint as not-authorized too.
          if (/\b403\b/.test(message) || /\b404\b/.test(message)) flags.not_authorized = true;
          if (/\b413\b/.test(message)) flags.payload_too_large = true;
          if (/\b422\b/.test(message)) flags.invalid = true;
          if (/\b429\b/.test(message)) flags.rate_limited = true;
          return JSON.stringify({ success: false, error: message, ...flags }, null, 2);
        }
      },
    },

    // ========================================================================
    // list_reports — read back what has been reported (#1538)
    // ========================================================================
    listReports: {
      name: "list_reports",
      description:
        "Read back reports that were previously published — yours by default. " +
        "Use it before writing a new report to avoid duplicating or contradicting " +
        "one you already filed, to resume a series (e.g. last week's summary), or " +
        "to diff this period against the last. Returns METADATA only (id, type, " +
        "title, period, created_at) — call get_report with an id for the payload. " +
        "Omit agent_name for your own reports plus any agent you have permission " +
        "for. Filters: report_type (exact), hours (time window, 0 = all-time), " +
        "search (title/type substring). Read-only.",
      parameters: z.object({
        agent_name: z
          .string()
          .optional()
          .describe(
            "Scope to a single agent (your own, or one you are permitted). Omit for every agent you can access.",
          ),
        report_type: z
          .string()
          .optional()
          .describe("Exact report_type to filter by, e.g. 'recon.weekly_summary'."),
        // The backend does NOT accept an arbitrary window: `_VALID_HOURS` is a
        // whitelist and anything outside it is silently coerced to 168. Accepting
        // a free integer here would let `hours: 48` answer with 7 days of reports
        // and no indication the window was ignored, so the enum is mirrored.
        hours: z
          .union([
            z.literal(0),
            z.literal(1),
            z.literal(6),
            z.literal(24),
            z.literal(168),
            z.literal(720),
          ])
          .optional()
          .describe(
            "Time window: one of 0 (all-time), 1, 6, 24, 168 (default, 7d), 720 (30d). " +
              "Other values are rejected — the backend only honours these.",
          ),
        search: z
          .string()
          .max(200)
          .optional()
          .describe(
            "Substring match on title and report_type (and agent name on a broad listing). " +
              "Payload contents are NOT searched — see #1537.",
          ),
        limit: z.number().int().min(1).max(200).optional().default(50)
          .describe("Maximum reports to return (1–200, default 50)."),
        offset: z.number().int().min(0).optional().default(0)
          .describe("Pagination offset (default 0)."),
      }),
      execute: async (
        params: {
          agent_name?: string;
          report_type?: string;
          hours?: number;
          search?: string;
          limit?: number;
          offset?: number;
        },
        context?: { session?: McpAuthContext },
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        if (params.agent_name) {
          const access = await checkAgentAccess(apiClient, authContext, params.agent_name);
          if (!access.allowed) {
            console.log(`[list_reports] Access denied: ${access.reason}`);
            return JSON.stringify({ error: "Access denied", reason: access.reason }, null, 2);
          }
        }

        try {
          // Scoped listing uses the per-agent route (it takes report_type but no
          // time window); a broad listing uses the fleet route.
          let reports = params.agent_name
            ? await apiClient.listAgentReports(params.agent_name, {
                report_type: params.report_type,
                hours: params.hours,
                search: params.search,
                limit: params.limit,
                offset: params.offset,
              })
            : await apiClient.listReports({
                report_type: params.report_type,
                hours: params.hours,
                search: params.search,
                limit: params.limit,
                offset: params.offset,
              });

          // Broad listing under an agent-scoped key: the backend filtered to the
          // KEY OWNER's accessible agents — broader than this agent's permits.
          // Narrow to {self} ∪ permitted (same rule as list_operator_queue).
          if (
            !params.agent_name &&
            authContext?.scope === "agent" &&
            authContext?.agentName
          ) {
            const caller = authContext.agentName;
            const permitted = await apiClient.getPermittedAgents(caller);
            const allowed = new Set([caller, ...permitted]);
            const before = reports.length;
            reports = filterReportsForAgentScope(reports, allowed);
            console.log(
              `[list_reports] Agent '${caller}' filtered: ${reports.length}/${before} visible`,
            );
          }

          return JSON.stringify({ count: reports.length, reports }, null, 2);
        } catch (error) {
          const msg = error instanceof Error ? error.message : String(error);
          console.error(`[list_reports] error: ${msg}`);
          return JSON.stringify({ error: msg }, null, 2);
        }
      },
    },

    // ========================================================================
    // get_report — one report including its payload (#1538)
    // ========================================================================
    getReport: {
      name: "get_report",
      description:
        "Fetch one report INCLUDING its payload, by id (ids come from " +
        "list_reports). Use it to read back what you actually wrote — the numbers " +
        "in last period's summary, the rows of a table you filed — rather than " +
        "re-deriving them. Read-only.",
      parameters: z.object({
        report_id: z.string().describe("Report id, as returned by list_reports."),
      }),
      execute: async (
        params: { report_id: string },
        context?: { session?: McpAuthContext },
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        try {
          const report = await apiClient.getReport(params.report_id);

          // The backend gates on the KEY OWNER's access and answers 404 (not
          // 403) when it fails, so an id can't be probed for existence. An
          // agent-scoped key is narrower than its owner, so re-check the owning
          // agent here and return the SAME not-found shape — widening the
          // disclosure for agent keys would defeat the backend's own choice.
          // Fail CLOSED: if the response carries no owning agent, the re-check
          // below cannot run, so an agent-scoped key would receive a payload
          // nobody gated for it. A missing agent_name means the contract changed
          // or the response is malformed — neither is a reason to hand over the
          // body. Non-agent scopes are already gated by the backend.
          const owner = (report as { agent_name?: string }).agent_name;
          if (authContext?.scope === "agent") {
            if (!owner) {
              console.error("[get_report] response carried no agent_name — refusing");
              return JSON.stringify({ error: "Report not found" }, null, 2);
            }
            const access = await checkAgentAccess(apiClient, authContext, owner);
            if (!access.allowed) {
              console.log(`[get_report] Access denied: ${access.reason}`);
              return JSON.stringify({ error: "Report not found" }, null, 2);
            }
          }

          return JSON.stringify(report, null, 2);
        } catch (error) {
          const msg = error instanceof Error ? error.message : String(error);
          console.error(`[get_report] error: ${msg}`);
          return JSON.stringify({ error: msg }, null, 2);
        }
      },
    },
  };
}
