/**
 * Agent Canvas Tools (ent#438, widened by ent#536)
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
 *
 * ent#536: one vocabulary for the agent and its voice mode. Every kind below
 * is rendered by the same component whoever wrote it, the voice panel tools
 * write the same default canvas (`main`) through the same backend path, and
 * `patch_canvas` replaces named blocks so a live board is not re-sent whole.
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

/** The default canvas — the one the voice mode draws on too (ent#536). */
export const DEFAULT_CANVAS_ID = "main";

// Keep in step with `CanvasBlockKind` in the backend `models.py` and
// `CANVAS_BLOCK_KINDS` in the frontend `canvasUtils.js`; the ent#438 test pins
// that every kind advertised here has a renderer.
const BLOCK_KINDS = [
  "table", "kpi", "markdown", "timeline", "json", "chart", "html", "image", "diagram",
] as const;

// ent#537 — starter layouts and the slots each names. Keep in step with
// `CANVAS_LAYOUT_SLOTS` in the backend `models.py` and `LAYOUTS` in the
// frontend `canvasLayouts.js`; `test_ent537_canvas_design_kit.py` pins them.
export const CANVAS_TEMPLATES = ["dashboard", "report", "brief", "status-board"] as const;
export const CANVAS_LAYOUT_SLOTS: Record<(typeof CANVAS_TEMPLATES)[number], readonly string[]> = {
  dashboard: ["header", "kpis", "main", "side", "footer"],
  report: ["header", "summary", "body", "figures", "appendix"],
  brief: ["header", "key-points", "body"],
  "status-board": ["header", "status", "issues", "next", "log"],
};

const LAYOUT_GUIDE = CANVAS_TEMPLATES
  .map((t) => `${t}(${CANVAS_LAYOUT_SLOTS[t].join(", ")})`)
  .join(" · ");

// One sentence per kind, with the payload shape the renderer actually reads.
// The platform prompt teaches the same shapes (test_ent536_canvas_prompt_guidance
// pins the two against each other).
const KIND_GUIDE =
  "chart = {type: bar|stacked_bar|line|area|pie|donut, series:[{label, unit?, color?, " +
  "points:[{ts, value}]}]} — one series per line, stack segment or slice; ts is a date/time " +
  "or a category name (a bar per series) · " +
  "kpi = {tiles:[{label, value, unit?}]} · " +
  "table = {columns:[...], rows:[[...]]} · " +
  "timeline = {events:[{ts, label, detail?}]} · " +
  "markdown = {markdown} — may embed ```chart / ```kpi / ```table fences (JSON inside) and " +
  "```mermaid fences, rendered as figures · " +
  "diagram = {mermaid: 'graph TD; A-->B'} (source ≤ 20,000 chars) · " +
  "image = {src, caption?} where src is an https URL, a path to a file in your workspace " +
  "(e.g. 'content/chart.png'), or data:image/png|jpeg|gif|webp;base64 under 64 KB · " +
  "html = {html} static markup, sanitised, scripts never run · json = raw. " +
  "Never put JavaScript in a block — you provide the data, Trinity draws it. " +
  "In html/markdown, style with the canvas kit classes ONLY (ck-card, ck-card-title, ck-grid-2/3/4, " +
  "ck-section, ck-callout ck-info|ck-success|ck-warning|ck-danger, ck-chip, ck-kpi, ck-table, " +
  "ck-figure + ck-caption, ck-muted); other classes, <style> and inline styles are dropped.";

const blockSchema = z.object({
  id: z.string().regex(/^[A-Za-z0-9._-]{1,64}$/).optional().describe(
    "Optional stable id for this block (letters, digits, dot, dash, underscore). Blocks without " +
    "one are assigned b1..bN in order; read them back with get_canvas and use them with patch_canvas.",
  ),
  kind: z.enum(BLOCK_KINDS).describe(KIND_GUIDE),
  title: z.string().max(300).optional().describe("Optional heading for this block."),
  slot: z.string().regex(/^[a-z][a-z0-9-]{0,31}$/).optional().describe(
    "Which slot of the canvas's `template` this block fills (e.g. 'kpis', 'main'). A block " +
    "without one, or naming a slot the template lacks, renders after the layout — never hidden.",
  ),
  payload: z.union([z.record(z.string(), z.unknown()), z.array(z.unknown())]).optional()
    .describe("The block's data, in the shape its kind describes."),
});

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
        "a live status board, a running tally, the latest version of an analysis, the chart someone just " +
        "asked for. Use `report` instead for a thing that happened once and should accumulate as a record " +
        "(a weekly summary, a completed run). Writing the same canvas_id again REPLACES it, " +
        "which is the point — that is how the surface stays current; to change a few blocks of a large " +
        "canvas use `patch_canvas`. Your default canvas is 'main' — the one your voice mode draws on too. " +
        "Pass `execution_id` and the result carries `visible_to_requester`: false means the audience you " +
        "chose does not reach the person in this conversation, and `visibility_note` says what to do " +
        "about it. null means it could not be determined — not that they cannot see it.",
      parameters: z.object({
        canvas_id: z.string().optional().describe(
          "The canvas to write. Omit for your default canvas 'main'; name another (e.g. 'pipeline', " +
          "'weekly') to keep a separate surface. 1-64 characters of letters, digits, dot, dash or " +
          "underscore. Reuse the SAME id to update a canvas; a new id makes a new one.",
        ),
        title: z.string().max(300).optional().describe("Short human-readable title for the canvas."),
        blocks: z.array(blockSchema).max(50).describe(
          // Keep in step with CANVAS_MAX_BLOCKS / CANVAS_BLOCKS_MAX_BYTES in
          // the backend `models.py`. The backend is the enforcer; this merely
          // reports the ceiling, so it must not understate it.
          "The canvas content, in order. At most 50 blocks, 512 KB serialized.",
        ),
        audience: z.enum(["operator", "roster"]).optional().describe(
          "Who sees it. 'operator' (default) = only your operator, on Agent Detail. " +
          "'roster' = also the people this agent is shared with, on your agent's Workspace page. " +
          "Choose 'roster' only for output you mean for them — it is how a canvas reaches a customer. " +
          "The default is usually WRONG when you are not talking to your operator: someone reaching " +
          "you through a public link or the Workspace reads the Workspace page, where an 'operator' " +
          "canvas does not appear at all. The result tells you — check `visible_to_requester`.",
        ),
        template: z.enum(CANVAS_TEMPLATES).optional().describe(
          "Optional starter layout. Each names the slots blocks fill via their `slot`: " +
          LAYOUT_GUIDE + ". Omit for stacked blocks. Unslotted blocks render after the layout.",
        ),
        execution_id: z.string().optional().describe(
          "Optional. The execution_id of the turn you are writing from. It stamps the canvas with " +
          "which run produced it, which is what lets Trinity tell a reader honestly whether the " +
          "canvas may be out of date.",
        ),
      }),
      execute: async (
        params: {
          canvas_id?: string;
          title?: string;
          blocks: Array<{ id?: string; kind: string; title?: string; slot?: string; payload?: unknown }>;
          audience?: "operator" | "roster";
          template?: (typeof CANVAS_TEMPLATES)[number];
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
          const result = await apiClient.writeCanvas(agentName, params.canvas_id || DEFAULT_CANVAS_ID, {
            title: params.title,
            blocks: params.blocks,
            audience: params.audience,
            // Omitted when undefined (JSON.stringify drops it), so an older
            // backend whose CanvasWrite forbids extras still accepts the write.
            template: params.template,
            execution_id: params.execution_id,
          });
          return JSON.stringify({ success: true, canvas: result }, null, 2);
        } catch (error) {
          return fail(error);
        }
      },
    },

    // ========================================================================
    // patch_canvas — replace only the named blocks (ent#536)
    // ========================================================================
    patch_canvas: {
      name: "patch_canvas",
      description:
        "Replace only the named blocks of one of your canvases, keeping everything else and the order " +
        "as it is. Use it to update the one chart or tile that changed on a large board instead of " +
        "re-sending the whole canvas with set_canvas. Every block must carry the `id` of a block the " +
        "canvas already holds (read them with get_canvas — blocks written without ids were assigned " +
        "b1..bN); an unknown id is refused by name, never appended, and there is still no append tool: " +
        "you name what changes. Each block you send replaces the stored block WHOLE (kind, title and " +
        "payload), so resend the title you want kept. Last write wins if two turns patch at once.",
      parameters: z.object({
        canvas_id: z.string().optional().describe("The canvas to patch. Omit for your default canvas 'main'."),
        blocks: z.array(blockSchema.extend({
          id: z.string().regex(/^[A-Za-z0-9._-]{1,64}$/).describe("The id of the block to replace."),
        })).min(1).max(50).describe("The replacement blocks, each carrying the id it replaces."),
        execution_id: z.string().optional().describe(
          "Optional. The execution_id of the turn you are writing from (provenance for the staleness mark).",
        ),
      }),
      execute: async (
        params: {
          canvas_id?: string;
          blocks: Array<{ id: string; kind: string; title?: string; slot?: string; payload?: unknown }>;
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
          const result = await apiClient.patchCanvas(agentName, params.canvas_id || DEFAULT_CANVAS_ID, {
            blocks: params.blocks,
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
        "Read one of your canvases back, with its blocks and their ids. Use this before updating so " +
        "you extend what is there instead of overwriting it — `set_canvas` replaces the whole canvas " +
        "and `patch_canvas` replaces the blocks you name; there is no append tool by design: read, " +
        "change, write is the only sequence that leaves the surface in a state you chose.",
      parameters: z.object({
        canvas_id: z.string().optional().describe("The canvas to read. Omit for your default canvas 'main'."),
      }),
      execute: async (
        params: { canvas_id?: string },
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
            { success: true, canvas: await apiClient.getCanvas(agentName, params.canvas_id || DEFAULT_CANVAS_ID) },
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
        "difference between 'done' and 'abandoned'. Succeeds whether or not the canvas existed. " +
        "RETIRE AS YOU GO: you have a fixed budget of canvases, and set_canvas refuses a NEW one " +
        "once you are at it (updating the ones you already have keeps working). A canvas per run " +
        "is what exhausts it — prefer rewriting one durable surface per topic to creating " +
        "'report-2026-09-08'-style ids, and clear the ones whose job is done.",
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
