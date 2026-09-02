/**
 * Agent Canvas Tools (ent#438)
 *
 * A **canvas** is a durable surface an agent renders onto and keeps CURRENT —
 * one row per (agent, canvas_id), updated in place. Reports (#918) are the
 * other half of the same idea and are deliberately not this: a report is
 * published once, addressed to a person, and accumulates as a record.
 *
 * "Which one do I want" is the question an agent actually has, so every
 * description below answers it rather than describing the API.
 *
 * The agent is resolved server-side from the MCP auth context — never from
 * tool input — and the backend additionally self-gates the write, so an
 * agent-scoped key cannot paint on a sibling's canvas.
 */

import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

/**
 * Keep only canvases whose agent is in the allowed set — the {self} ∪ permitted
 * narrowing an agent-scoped key gets on the READ path (the #1104 rule, mirrored
 * from reports.ts). Exported so a unit test can pin it without a backend.
 */
export function filterCanvasesForAgentScope<T extends { agent_name?: string }>(
  canvases: T[],
  allowedNames: Set<string>,
): T[] {
  return canvases.filter((c) => !!c.agent_name && allowedNames.has(c.agent_name));
}

const BLOCK_KINDS = [
  "table", "kpi", "markdown", "timeline", "json", "chart", "html",
] as const;

export function createCanvasTools(client: TrinityClient, requireApiKey: boolean) {
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

  /** The canvas is written AS the calling agent, so an agent-scoped key is required. */
  const getAgentName = (authContext: McpAuthContext | undefined): string => {
    if (authContext?.scope === "agent" && authContext.agentName) {
      return authContext.agentName;
    }
    throw new Error(
      "The canvas tools require an agent-scoped API key (a canvas belongs to the calling agent).",
    );
  };

  const fail = (error: unknown) =>
    JSON.stringify(
      { success: false, error: error instanceof Error ? error.message : String(error) },
      null,
      2,
    );

  return {
    // ========================================================================
    // set_canvas — create or replace a canvas
    // ========================================================================
    set_canvas: {
      name: "set_canvas",
      description:
        "Render structured output onto your canvas — a durable surface the people you work with can open, " +
        "which you UPDATE over time rather than re-publish. Use it for the thing that has a current state: " +
        "a live status board, a running tally, the latest version of an analysis. " +
        "Use `report` instead for a thing that happened once and should accumulate as a record " +
        "(a weekly summary, a completed run). Writing the same canvas_id again REPLACES it, " +
        "which is the point — that is how the surface stays current.",
      parameters: z.object({
        canvas_id: z.string().describe(
          "The canvas to write, e.g. 'status', 'pipeline', 'weekly'. 1-64 characters of letters, " +
          "digits, dot, dash or underscore. Reuse the SAME id to update a canvas; a new id makes a new one.",
        ),
        title: z.string().max(300).optional().describe("Short human-readable title for the canvas."),
        blocks: z.array(
          z.object({
            kind: z.enum(BLOCK_KINDS).describe(
              "table = {columns,rows} · kpi = {tiles:[{label,value}]} · markdown = {markdown} · " +
              "timeline = {events:[{ts,label,detail}]} · chart = {labels:[...],series:[{label,data}]} · " +
              "html = {html} (sanitised on render) · json = raw.",
            ),
            title: z.string().max(300).optional().describe("Optional heading for this block."),
            payload: z.union([z.record(z.unknown()), z.array(z.unknown())]).optional()
              .describe("The block's data, in the shape its kind describes."),
          }),
        ).max(50).describe(
          // Keep in step with CANVAS_MAX_BLOCKS / CANVAS_BLOCKS_MAX_BYTES in
          // the backend `models.py`. The backend is the enforcer; this merely
          // reports the ceiling, so it must not understate it.
          "The canvas content, in order. At most 50 blocks, 512 KB serialized.",
        ),
        audience: z.enum(["operator", "roster"]).optional().describe(
          "Who sees it. 'operator' (default) = only your operator, on Agent Detail. " +
          "'roster' = also the people this agent is shared with, on your agent's Workspace page. " +
          "Choose 'roster' only for output you mean for them — it is how a canvas reaches a customer.",
        ),
        execution_id: z.string().optional().describe(
          "Optional. The execution_id of the turn you are writing from. It stamps the canvas with " +
          "which run produced it, which is what lets Trinity tell a reader honestly whether the " +
          "canvas may be out of date.",
        ),
      }),
      execute: async (
        params: {
          canvas_id: string;
          title?: string;
          blocks: Array<{ kind: string; title?: string; payload?: unknown }>;
          audience?: "operator" | "roster";
          execution_id?: string;
        },
        context?: { session?: McpAuthContext },
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);
        let agentName: string;
        try {
          agentName = getAgentName(authContext);
        } catch (error) {
          return fail(error);
        }
        try {
          const result = await apiClient.writeCanvas(agentName, params.canvas_id, {
            title: params.title,
            blocks: params.blocks,
            audience: params.audience,
            execution_id: params.execution_id,
          });
          return JSON.stringify({ success: true, canvas: result }, null, 2);
        } catch (error) {
          return fail(error);
        }
      },
    },

    // ========================================================================
    // get_canvas — read back what is currently rendered
    // ========================================================================
    get_canvas: {
      name: "get_canvas",
      description:
        "Read one of your canvases back, with its blocks. Use this before updating so you extend what " +
        "is there instead of overwriting it — `set_canvas` replaces, and there is no append tool by " +
        "design: read, change, write is the only sequence that leaves the surface in a state you chose.",
      parameters: z.object({
        canvas_id: z.string().describe("The canvas to read."),
      }),
      execute: async (
        params: { canvas_id: string },
        context?: { session?: McpAuthContext },
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);
        let agentName: string;
        try {
          agentName = getAgentName(authContext);
        } catch (error) {
          return fail(error);
        }
        try {
          return JSON.stringify(
            { success: true, canvas: await apiClient.getCanvas(agentName, params.canvas_id) },
            null, 2,
          );
        } catch (error) {
          return fail(error);
        }
      },
    },

    // ========================================================================
    // list_canvases — what surfaces do I have
    // ========================================================================
    list_canvases: {
      name: "list_canvases",
      description:
        "List your canvases — id, title, audience and when each was last updated. Metadata only; " +
        "use get_canvas for the content.",
      parameters: z.object({}),
      execute: async (_params: unknown, context?: { session?: McpAuthContext }) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);
        let agentName: string;
        try {
          agentName = getAgentName(authContext);
        } catch (error) {
          return fail(error);
        }
        try {
          const rows = await apiClient.listCanvases(agentName);
          // Self-scoped by construction (the path agent IS the caller), so the
          // {self} ∪ permitted filter has nothing to remove here. Applied
          // anyway, so the narrowing lives with the tool rather than depending
          // on a route shape a future change could widen.
          const scoped = filterCanvasesForAgentScope(rows, new Set([agentName]));
          return JSON.stringify({ success: true, count: scoped.length, canvases: scoped }, null, 2);
        } catch (error) {
          return fail(error);
        }
      },
    },

    // ========================================================================
    // clear_canvas — remove a surface
    // ========================================================================
    clear_canvas: {
      name: "clear_canvas",
      description:
        "Remove one of your canvases entirely. Use when a surface is finished or was superseded — " +
        "leaving a stale canvas up is worse than removing it, because a reader cannot tell the " +
        "difference between 'done' and 'abandoned'. Succeeds whether or not the canvas existed.",
      parameters: z.object({
        canvas_id: z.string().describe("The canvas to remove."),
      }),
      execute: async (
        params: { canvas_id: string },
        context?: { session?: McpAuthContext },
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);
        let agentName: string;
        try {
          agentName = getAgentName(authContext);
        } catch (error) {
          return fail(error);
        }
        try {
          return JSON.stringify(
            { success: true, result: await apiClient.clearCanvas(agentName, params.canvas_id) },
            null, 2,
          );
        } catch (error) {
          return fail(error);
        }
      },
    },
  };
}
