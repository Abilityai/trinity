/**
 * Agent-Defined Pipeline Introspection (#919)
 *
 * Trinity does NOT own the DAG, the execution semantics, or the recovery
 * policy of an agent's long-running multi-stage pipeline — the agent owns all
 * of that (Architectural Invariant #8). These two tools are a thin, read-only
 * window onto a uniform file contract the agent publishes inside its own
 * container:
 *
 *   ~/.trinity/pipelines/<pipeline_id>.yaml                  — definition
 *   ~/.trinity/pipeline-state/<pipeline_id>/<instance>.json  — runtime state
 *
 * They wrap the EXISTING `GET /api/agents/{name}/files` (recursive list) and
 * `GET /api/agents/{name}/files/download` (read) surfaces — no new backend
 * endpoint, no DB table, no backend parsing of pipeline semantics. The YAML
 * definition is parsed here (hardened) only to produce a compact summary; the
 * authoritative contract lives in docs/schemas/agent-pipeline*.schema.json.
 */

import { z } from "zod";
import YAML from "yaml";
import { TrinityClient } from "../client.js";
import type { McpAuthContext, AgentFileNode } from "../types.js";

// Canonical file-contract roots, relative to the agent home (/home/developer).
// Relative paths keep us off a hardcoded home path — the agent-server anchors
// reads at the WORKDIR home (Decision #7).
const PIPELINES_DIR = ".trinity/pipelines";
const PIPELINE_STATE_DIR = ".trinity/pipeline-state";

// Read fan-out cap: at most this many pipelines are summarised in one
// list_agent_pipelines call. Beyond it we truncate (and log) rather than
// issuing an unbounded set of downloads. "No silent caps": the drop is logged.
const MAX_PIPELINES = 50;

// Per-file parse ceiling. Both the YAML definition and the JSON state file are
// size-capped BEFORE parse so a hostile/oversized file can't drive a parse
// blow-up. 256 KiB mirrors the agent-compatibility collector's per-file cap.
const MAX_FILE_BYTES = 256 * 1024;

// Strict id grammar. `pipeline_id`/`instance_id` are interpolated into download
// paths and the agent-server download endpoint has no deny-list (only a
// `/home/developer` prefix check), so a `..` segment would read arbitrary home
// files (.env, .ssh/id_*). The class rejects `/` and `%` (encoded slashes);
// the refinement additionally rejects `..` (which the class alone would admit,
// since `.` is allowed). [P1 SECURITY — Decision #4 / T1]
const ID_PATTERN = /^[A-Za-z0-9._-]+$/;

const idSchema = z
  .string()
  .min(1)
  .max(128)
  .regex(
    ID_PATTERN,
    "must match ^[A-Za-z0-9._-]+$ (no '/', no path separators, no encoded slashes)"
  )
  .refine((v) => !v.includes(".."), "must not contain '..' (path traversal)");

// ---------------------------------------------------------------------------
// Pure helpers (exported for unit tests)
// ---------------------------------------------------------------------------

/** Extract the HTTP status from a `TrinityClient` "API error (NNN): …" throw. */
export function httpStatusFromError(err: unknown): number | null {
  const msg = err instanceof Error ? err.message : String(err);
  const m = msg.match(/API error \((\d{3})\)/);
  return m ? parseInt(m[1], 10) : null;
}

/** A 404 means "no such file/dir" → empty / not-found. Everything else
 * (400 agent-stopped, 5xx unreachable) is a real error the caller surfaces so
 * "no such pipeline" is never confused with "agent down" (Decision #6). */
export function isNotFound(err: unknown): boolean {
  return httpStatusFromError(err) === 404;
}

/** `demo.yaml` → `demo`; `inst-1.json` → `inst-1`. */
export function stripExtension(filename: string): string {
  const dot = filename.lastIndexOf(".");
  return dot > 0 ? filename.slice(0, dot) : filename;
}

/** Depth-first collect every `file` node from a recursive file tree. */
export function flattenFiles(nodes: AgentFileNode[] | undefined): AgentFileNode[] {
  const out: AgentFileNode[] = [];
  for (const node of nodes ?? []) {
    if (node.type === "file") {
      out.push(node);
    } else if (node.type === "directory") {
      out.push(...flattenFiles(node.children));
    }
  }
  return out;
}

/**
 * Pick the latest state instance from a set of file nodes, using only the tree
 * (no content read): newest `modified` mtime first, tie-broken by lexically
 * descending filename. `updated_at` is the authoritative *semantic* timestamp
 * and is surfaced in the result, but selection runs off mtime — the physical
 * write-time the agent stamps on every state update — so the read fan-out
 * stays at one download per pipeline (Decision #2/#3; instance_ids SHOULD be
 * time-sortable, which the filename tie-break leans on).
 */
export function pickLatestInstanceNode(
  nodes: AgentFileNode[] | undefined
): AgentFileNode | null {
  const jsonFiles = (nodes ?? []).filter(
    (n) => n.type === "file" && n.name.endsWith(".json")
  );
  if (jsonFiles.length === 0) return null;
  return jsonFiles.slice().sort((a, b) => {
    const ta = Date.parse(a.modified ?? "");
    const tb = Date.parse(b.modified ?? "");
    if (!Number.isNaN(ta) && !Number.isNaN(tb) && ta !== tb) return tb - ta;
    return b.name.localeCompare(a.name);
  })[0];
}

/** Count escalations still open. Conservative: an entry with no recognisable
 * status counts as open (better to over-report attention than hide it). */
export function countOpenEscalations(state: unknown): number {
  const esc = (state as { escalations?: unknown })?.escalations;
  if (!Array.isArray(esc)) return 0;
  return esc.filter((e) => {
    if (e == null || typeof e !== "object") return false;
    const o = e as Record<string, unknown>;
    if (typeof o.open === "boolean") return o.open;
    if (typeof o.resolved === "boolean") return !o.resolved;
    if (typeof o.status === "string") {
      return !["resolved", "closed", "done"].includes(o.status.toLowerCase());
    }
    return true;
  }).length;
}

/** Hardened YAML parse: size-cap first, then parse with duplicate-key
 * rejection (`uniqueKeys`) and the default alias-expansion guard
 * (`maxAliasCount`, billion-laughs). JS YAML never instantiates arbitrary
 * classes, so there is no PyYAML-style code-exec surface to disable. Throws
 * on oversize or malformed input — the caller turns that into an item-level
 * error, never aborting the whole list. */
export function parseYamlHardened(text: string): unknown {
  if (Buffer.byteLength(text, "utf8") > MAX_FILE_BYTES) {
    throw new Error(
      `definition exceeds ${MAX_FILE_BYTES}-byte parse cap`
    );
  }
  return YAML.parse(text, { uniqueKeys: true, maxAliasCount: 100 });
}

/** Compact, shape-tolerant summary of a parsed pipeline definition. */
export function summarizeDefinition(def: unknown): {
  name?: unknown;
  description?: unknown;
  version?: unknown;
  stages: unknown[];
  stage_count: number;
} {
  if (def == null || typeof def !== "object") {
    return { stages: [], stage_count: 0 };
  }
  const d = def as Record<string, unknown>;
  const stages = Array.isArray(d.stages)
    ? d.stages
        .map((s) =>
          s != null && typeof s === "object"
            ? ((s as Record<string, unknown>).id ??
              (s as Record<string, unknown>).name ??
              null)
            : s
        )
        .filter((x) => x != null)
    : [];
  return {
    name: d.name,
    description: d.description,
    version: d.version,
    stages,
    stage_count: stages.length,
  };
}

/** Health summary lifted from a parsed instance state file. */
export function summarizeInstance(
  state: unknown,
  fallbackInstanceId: string
): {
  instance_id: string;
  current_stage: unknown;
  health: unknown;
  updated_at: unknown;
  open_escalations: number;
} {
  const s = (state ?? {}) as Record<string, unknown>;
  return {
    instance_id:
      typeof s.instance_id === "string" && s.instance_id
        ? s.instance_id
        : fallbackInstanceId,
    current_stage: s.current_stage ?? null,
    health: s.health ?? null,
    updated_at: s.updated_at ?? null,
    open_escalations: countOpenEscalations(state),
  };
}

const ok = (obj: unknown): string => JSON.stringify(obj, null, 2);
const fail = (error: string): string =>
  JSON.stringify({ success: false, error }, null, 2);

// ---------------------------------------------------------------------------
// Tool factory
// ---------------------------------------------------------------------------

export function createPipelineTools(
  client: TrinityClient,
  requireApiKey: boolean
) {
  /** Pick the right client instance based on auth mode (mirrors files.ts). */
  const getClient = (authContext?: McpAuthContext): TrinityClient => {
    if (requireApiKey) {
      if (!authContext?.mcpApiKey) {
        throw new Error(
          "MCP API key authentication required but no API key found in request context"
        );
      }
      const userClient = new TrinityClient(client.getBaseUrl());
      userClient.setToken(authContext.mcpApiKey);
      return userClient;
    }
    return client;
  };

  /** Download + parse a single instance state JSON, size-capped. */
  const readInstanceState = async (
    apiClient: TrinityClient,
    agentName: string,
    path: string
  ): Promise<unknown> => {
    const text = await apiClient.downloadAgentFile(agentName, path);
    if (Buffer.byteLength(text, "utf8") > MAX_FILE_BYTES) {
      throw new Error(`state file exceeds ${MAX_FILE_BYTES}-byte cap`);
    }
    return JSON.parse(text);
  };

  return {
    // ========================================================================
    // list_agent_pipelines — enumerate pipelines + latest-instance health
    // ========================================================================
    listAgentPipelines: {
      name: "list_agent_pipelines",
      description:
        "List the agent-defined pipelines an agent publishes under " +
        "~/.trinity/pipelines/*.yaml, each with a health summary drawn from " +
        "its latest runtime instance under ~/.trinity/pipeline-state/. " +
        "Trinity does not own the DAG or execution — this is a read-only view " +
        "of the agent's own files. Returns [] when the agent publishes no " +
        "pipelines. The agent must be running (its files live in the " +
        "container); a stopped or unreachable agent surfaces a real error, " +
        "not an empty list.",
      parameters: z.object({
        agent_name: z
          .string()
          .min(1)
          .describe("Name of the agent whose pipelines to list."),
      }),
      execute: async (
        params: { agent_name: string },
        context?: { session?: McpAuthContext }
      ) => {
        const apiClient = getClient(context?.session);
        const agentName = params.agent_name;
        console.log(`[list_agent_pipelines] agent=${agentName}`);

        // Step 1 — discover pipeline definitions. 404 → no pipelines dir → [].
        let defFiles: AgentFileNode[];
        try {
          const tree = await apiClient.listAgentFiles(
            agentName,
            PIPELINES_DIR,
            true
          );
          defFiles = flattenFiles(tree.tree).filter(
            (f) => f.name.endsWith(".yaml") || f.name.endsWith(".yml")
          );
        } catch (err) {
          if (isNotFound(err)) return ok([]);
          const msg = err instanceof Error ? err.message : String(err);
          console.error(`[list_agent_pipelines] list error: ${msg}`);
          return fail(
            `Could not read pipelines for '${agentName}' ` +
              `(agent stopped or unreachable): ${msg}`
          );
        }

        if (defFiles.length === 0) return ok([]);

        // Step 2 — one recursive list of all instance state, grouped by
        // pipeline_id (top-level dirs). A missing state dir (no instances yet)
        // is a 404 → every pipeline simply has latest=null; a 400/5xx is real.
        const stateByPipeline = new Map<string, AgentFileNode[]>();
        try {
          const stateTree = await apiClient.listAgentFiles(
            agentName,
            PIPELINE_STATE_DIR,
            true
          );
          for (const dir of stateTree.tree ?? []) {
            if (dir.type === "directory") {
              stateByPipeline.set(dir.name, flattenFiles(dir.children));
            }
          }
        } catch (err) {
          if (!isNotFound(err)) {
            const msg = err instanceof Error ? err.message : String(err);
            console.error(`[list_agent_pipelines] state list error: ${msg}`);
            return fail(
              `Could not read pipeline state for '${agentName}' ` +
                `(agent stopped or unreachable): ${msg}`
            );
          }
        }

        // Cap the fan-out — log (don't silently drop) when truncating.
        let pipelines = defFiles;
        if (pipelines.length > MAX_PIPELINES) {
          console.log(
            `[list_agent_pipelines] agent=${agentName} truncating ` +
              `${pipelines.length} → ${MAX_PIPELINES} pipelines`
          );
          pipelines = pipelines.slice(0, MAX_PIPELINES);
        }

        // Step 3 — per pipeline: parse the definition + read its latest
        // instance. A single malformed file is an item-level error and never
        // aborts the list.
        const results = await Promise.all(
          pipelines.map(async (def) => {
            const pipelineId = stripExtension(def.name);
            try {
              const defText = await apiClient.downloadAgentFile(
                agentName,
                def.path
              );
              const parsed = parseYamlHardened(defText);
              const summary = summarizeDefinition(parsed);

              let latest: ReturnType<typeof summarizeInstance> | null = null;
              const latestNode = pickLatestInstanceNode(
                stateByPipeline.get(pipelineId)
              );
              if (latestNode) {
                const state = await readInstanceState(
                  apiClient,
                  agentName,
                  latestNode.path
                );
                latest = summarizeInstance(
                  state,
                  stripExtension(latestNode.name)
                );
              }
              return { pipeline_id: pipelineId, ...summary, latest };
            } catch (err) {
              const msg = err instanceof Error ? err.message : String(err);
              return { pipeline_id: pipelineId, error: msg };
            }
          })
        );

        return ok(results);
      },
    },

    // ========================================================================
    // get_agent_pipeline_state — full parsed state for one instance
    // ========================================================================
    getAgentPipelineState: {
      name: "get_agent_pipeline_state",
      description:
        "Read the full runtime state JSON for one instance of an agent's " +
        "pipeline (~/.trinity/pipeline-state/<pipeline_id>/<instance_id>.json). " +
        "Omit instance_id to get the latest instance. Returns a clean " +
        "not-found result (never a 500) when the pipeline or instance does " +
        "not exist; a stopped or unreachable agent surfaces a distinct error.",
      parameters: z.object({
        agent_name: z
          .string()
          .min(1)
          .describe("Name of the agent that owns the pipeline."),
        pipeline_id: idSchema.describe(
          "Pipeline id (the <pipeline_id> in pipelines/<pipeline_id>.yaml). " +
            "Must match ^[A-Za-z0-9._-]+$."
        ),
        instance_id: idSchema
          .optional()
          .describe(
            "Specific instance id. Omit to return the latest instance. " +
              "Must match ^[A-Za-z0-9._-]+$ when supplied."
          ),
      }),
      execute: async (
        params: {
          agent_name: string;
          pipeline_id: string;
          instance_id?: string;
        },
        context?: { session?: McpAuthContext }
      ) => {
        const apiClient = getClient(context?.session);
        const { agent_name: agentName, pipeline_id: pipelineId } = params;
        console.log(
          `[get_agent_pipeline_state] agent=${agentName} ` +
            `pipeline=${pipelineId} instance=${params.instance_id ?? "(latest)"}`
        );

        let targetPath: string;
        let resolvedInstanceId: string;

        if (params.instance_id) {
          // Explicit instance — both ids are zod-validated, safe to interpolate.
          resolvedInstanceId = params.instance_id;
          targetPath = `${PIPELINE_STATE_DIR}/${pipelineId}/${resolvedInstanceId}.json`;
        } else {
          // Latest instance — list the pipeline's state dir and pick newest.
          let tree;
          try {
            tree = await apiClient.listAgentFiles(
              agentName,
              `${PIPELINE_STATE_DIR}/${pipelineId}`,
              true
            );
          } catch (err) {
            if (isNotFound(err)) {
              return fail(
                `No state found for pipeline '${pipelineId}' on '${agentName}'.`
              );
            }
            const msg = err instanceof Error ? err.message : String(err);
            return fail(
              `Could not read state for '${pipelineId}' on '${agentName}' ` +
                `(agent stopped or unreachable): ${msg}`
            );
          }
          const latestNode = pickLatestInstanceNode(tree.tree);
          if (!latestNode) {
            return fail(
              `Pipeline '${pipelineId}' on '${agentName}' has no instances.`
            );
          }
          targetPath = latestNode.path;
          resolvedInstanceId = stripExtension(latestNode.name);
        }

        // Download + parse the target instance.
        let state: unknown;
        try {
          state = await readInstanceState(apiClient, agentName, targetPath);
        } catch (err) {
          if (isNotFound(err)) {
            return fail(
              `Instance '${resolvedInstanceId}' of pipeline '${pipelineId}' ` +
                `not found on '${agentName}'.`
            );
          }
          if (err instanceof SyntaxError) {
            return fail(
              `State file for '${resolvedInstanceId}' is not valid JSON.`
            );
          }
          const msg = err instanceof Error ? err.message : String(err);
          return fail(
            `Could not read state for '${pipelineId}/${resolvedInstanceId}' ` +
              `on '${agentName}': ${msg}`
          );
        }

        return ok({
          success: true,
          agent_name: agentName,
          pipeline_id: pipelineId,
          instance_id: resolvedInstanceId,
          state,
        });
      },
    },
  };
}
