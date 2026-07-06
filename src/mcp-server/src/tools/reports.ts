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
          "Arbitrary JSON body of the report (max 256 KB serialized)."
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
          });
          return JSON.stringify(
            {
              success: true,
              report_id: result.id,
              agent_name: result.agent_name,
              report_type: result.report_type,
              created_at: result.created_at,
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
  };
}
