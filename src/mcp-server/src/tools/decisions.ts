/**
 * Seat Decision Record Tools (trinity-enterprise#638, ruling R25)
 *
 * A role companion records WHY a thing was approved, deferred or killed for
 * the seat it serves — the alternatives that were live, the criterion that
 * discriminated, what would reverse it, a review date — and reads the seat's
 * standing decisions to reuse a criterion and cite it. The record is lintable,
 * not prose: the backend refuses prose-where-a-field-belongs with a receipt
 * naming each field, and a record with no alternatives as a note.
 *
 * The seat is resolved server-side from `execution_id` (the MEM-001 rule —
 * the agent never names a person) and no email ever comes back: the decider
 * is labelled `seat` / `owner`.
 *
 * `record_decision` sends a deterministic Idempotency-Key over
 * (execution_id, decided), so a transport retry records once.
 */

import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";
import { deriveMcpIdempotencyKey } from "./chat.js";

export function createDecisionTools(client: TrinityClient, requireApiKey: boolean) {
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

  const resolveAgent = (authContext: McpAuthContext | undefined, agent_name?: string): string | null =>
    agent_name || (authContext?.scope === "agent" ? authContext.agentName ?? null : null);

  const noAgent = () =>
    JSON.stringify(
      {
        success: false,
        error:
          "Cannot determine agent name. Pass agent_name explicitly or call this tool " +
          "using an agent-scoped MCP key.",
      },
      null,
      2
    );

  return {
    recordDecision: {
      name: "record_decision",
      description:
        "Record a decision for the seat you are serving — why something was approved, deferred or " +
        "killed — so the seat's judgment compounds instead of evaporating with this chat (Tandem R25).\n\n" +
        "**A decision, not a note**: give `outcome`, what was `decided`, the `alternatives` that were live " +
        "(at least one — with none it is a note; keep it in memory instead), the `criterion` that made the " +
        "winner win (the reusable part), what would `reversal` it, and a `review_by` date (YYYY-MM-DD, after " +
        "today, within a year). One line per field; put reasoning in `notes`. Prose in a field is refused with a " +
        "receipt naming the field.\n\n" +
        "**Reuse**: your prompt's 'standing decisions' block lists this seat's active records with ids; when a " +
        "decision leans on one, pass its id in `cites` — that is the platform's health metric.\n\n" +
        "**Direction is not a seat decision**: pricing, positioning, roadmap → pass `scope: \"direction\"`; it is " +
        "kept as `routed` and you take it to canon as a proposal.\n\n" +
        "Works in a user-facing session (public link, Slack, Telegram, WhatsApp) or a scheduled run addressed to " +
        "one person. Supply your `execution_id` from the Execution Context block.",
      parameters: z.object({
        execution_id: z.string().min(1).describe("Your current execution_id (Execution Context block)."),
        outcome: z.enum(["approved", "deferred", "killed"]).describe("What happened to the thing decided."),
        decided: z.string().min(1).max(2000).describe("What was decided — one line."),
        alternatives: z
          .array(z.string().min(1).max(500))
          .max(32)
          .describe("The options that were live besides the one that won — at least one."),
        criterion: z.string().min(1).max(2000).describe("What made the winner win — one line, the reusable part."),
        reversal: z.string().min(1).max(2000).describe("What would reverse this decision — one line."),
        review_by: z.string().min(10).max(32).describe("YYYY-MM-DD — after today, within a year."),
        scope: z.enum(["seat", "direction"]).optional().describe("Default seat. direction → routed to canon."),
        notes: z.string().max(4000).optional().describe("Free prose: the reasoning, the trade-off."),
        ask_class: z
          .string()
          .max(128)
          .optional()
          .describe("Slug for the kind of ask (e.g. vendor-approval) — groups the graduation evidence."),
        decided_by_role: z.string().max(128).optional().describe("Your role id, if the platform does not know it."),
        cites: z.array(z.string()).max(32).optional().describe("Ids of this seat's earlier decisions this one leans on."),
        request_id: z.string().max(200).optional().describe("The decision request (operator queue item) this answers, if any."),
        agent_name: z
          .string()
          .optional()
          .describe("Agent name override. Defaults to the agent whose MCP key is making this call. Omit in normal use."),
      }),
      execute: async (
        args: {
          execution_id: string;
          outcome: "approved" | "deferred" | "killed";
          decided: string;
          alternatives: string[];
          criterion: string;
          reversal: string;
          review_by: string;
          scope?: "seat" | "direction";
          notes?: string;
          ask_class?: string;
          decided_by_role?: string;
          cites?: string[];
          request_id?: string;
          agent_name?: string;
        },
        context: any
      ) => {
        const authContext = requireApiKey ? context?.session : undefined;
        const apiClient = getClient(authContext);
        const resolvedAgent = resolveAgent(authContext, args.agent_name);
        if (!resolvedAgent) return noAgent();

        const { agent_name: _omit, ...body } = args;
        const idempotencyKey = deriveMcpIdempotencyKey([
          "record_decision", resolvedAgent, args.execution_id, args.decided,
        ]);
        console.log(`[record_decision] ${resolvedAgent} execution=${args.execution_id} outcome=${args.outcome}`);
        try {
          const result = await apiClient.recordSeatDecision(resolvedAgent, body, idempotencyKey);
          return JSON.stringify(result, null, 2);
        } catch (error) {
          const errorMessage = error instanceof Error ? error.message : String(error);
          console.error(`[record_decision] Error: ${errorMessage}`);
          return JSON.stringify({ success: false, error: errorMessage }, null, 2);
        }
      },
    },

    listSeatDecisions: {
      name: "list_seat_decisions",
      description:
        "The standing decisions of the seat you are serving — criterion first — so you can apply the same " +
        "criterion to the same kind of ask and cite the id in record_decision. Returns the seat's active " +
        "records (set include_history for superseded / closed / reversed / expired) and the seat's evidence " +
        "stats (recorded, reused, per ask class: criteria, reversals, stable). No person emails are returned.",
      parameters: z.object({
        execution_id: z.string().min(1).describe("Your current execution_id (Execution Context block)."),
        include_history: z.boolean().optional().describe("Also return non-active records (default false)."),
        agent_name: z.string().optional().describe("Agent name override. Omit in normal use."),
      }),
      execute: async (
        args: { execution_id: string; include_history?: boolean; agent_name?: string },
        context: any
      ) => {
        const authContext = requireApiKey ? context?.session : undefined;
        const apiClient = getClient(authContext);
        const resolvedAgent = resolveAgent(authContext, args.agent_name);
        if (!resolvedAgent) return noAgent();
        try {
          const result = await apiClient.listSeatDecisions(resolvedAgent, args.execution_id, !!args.include_history);
          return JSON.stringify(result, null, 2);
        } catch (error) {
          const errorMessage = error instanceof Error ? error.message : String(error);
          console.error(`[list_seat_decisions] Error: ${errorMessage}`);
          return JSON.stringify({ success: false, error: errorMessage }, null, 2);
        }
      },
    },
  };
}
