/**
 * System Management Tools
 *
 * MCP tools for deploying and managing multi-agent systems via YAML manifests.
 */

import { z } from "zod";
import { TrinityClient, ApiError } from "../client.js";
import { accessDenied, type DenyCallContext } from "../access.js";
import type { McpAuthContext } from "../types.js";

/**
 * Collapse a teardown refusal into an honest, actionable shape (ent#454).
 *
 * `teardown_system` proxies an ENTITLEMENT-GATED route while knowing nothing
 * about entitlement, and the refusals it can meet are DIFFERENT operator
 * situations that must not be flattened into "it failed":
 *
 *   404  the route is absent — an OSS-only build with no teardown module
 *   403  mounted but not licensed for this instance
 *   403  an agent-scoped key called a human-only verb
 *   503  membership could not be verified; RETRY is the remedy
 *
 * It never throws: a thrown error reaches the agent as an opaque transport
 * failure it cannot reason about. The `{success: false, …flags}` shape is the
 * `call_a2a_agent` / `fetch_credential` precedent, where the flags ARE the
 * structure.
 */
function teardownFailure(error: unknown): string {
  const status = error instanceof ApiError ? error.status : undefined;
  const raw = error instanceof Error ? error.message : String(error);
  // `ApiError.body` is the raw response body, retained verbatim for exactly
  // this purpose (ent#443). Re-deriving it by regex off `message` is what
  // breaks the day the message format changes — its own docstring says so.
  const body = error instanceof ApiError ? error.body : undefined;
  const flags: Record<string, unknown> = {};
  let message = raw;

  // A `failed` teardown arrives as a 500 whose body IS the report, and that
  // report is the only actionable output — so it is a RESULT, not an error.
  if (status === 500 && body) {
    try {
      const report = JSON.parse(body);
      if (report && report.status === "failed") {
        return JSON.stringify(report, null, 2);
      }
    } catch {
      // Not JSON — fall through to the flag shape below.
    }
  }

  if (status === 404) {
    flags.not_available = true;
    message =
      "System teardown is not available on this platform. Remove members "
      + "individually with DELETE /api/agents/{name}.";
  } else if (status === 403) {
    flags.not_permitted = true;
    message =
      "Refused. System teardown is licensed separately AND is human-only: a "
      + "machine credential (agent, connector, system, ops) cannot remove a "
      + "fleet, because that is the bulk form of an operation that is "
      + "spawn-scoped one agent at a time. Ask a human operator with the "
      + "creator role.";
  } else if (status === 503) {
    flags.membership_unverified = true;
    flags.retryable = true;
    message =
      "Refused: the removal set could not be established — either the "
      + "membership tags or the agent roster could not be read — and teardown "
      + "will not derive a removal set from name prefixes alone. Nothing was "
      + "removed. The detail says which read failed; retry once that dependency "
      + "is reachable.";
  } else if (status === 400) {
    // The confirmed set named no current member — re-preview and re-confirm.
    flags.bad_request = true;
  }

  return JSON.stringify({ success: false, error: message, detail: raw, ...flags }, null, 2);
}

/**
 * Map an apply failure onto the result the caller reads (ent#695).
 *
 * Deliberately THIN, so a status-code change in the backend apply contract
 * cannot silently break this tool. Two situations are named here, no more:
 *
 *   404/405  the route is absent: a build that predates apply mode (there,
 *            `POST /api/systems/apply` matches `GET /{system_name}` by path
 *            only, hence 405)
 *   403      an authorization refusal, serialised through `accessDenied` so
 *            the tool-call audit row records a denial (#2807)
 *
 * Every other non-2xx with a JSON OBJECT body (the 409 / 503 / 500 apply
 * reports, a 400, 401 or 422) IS the result: the body comes back with
 * `http_status` and `success: false` added, so `status`, `blockers[]` and
 * `failed[]` reach the caller intact. Those are not authorization denials and
 * are NOT stamped as such: `ToolOutcome` has no non-denial kind yet
 * (types.ts). Anything else (a JSON scalar, array or null, or a non-JSON body)
 * comes back as `{success: false, http_status, detail}`.
 */
function applyFailure(error: unknown, context: DenyCallContext | undefined): string {
  if (!(error instanceof ApiError)) {
    // No HTTP status at all: a missing API key (client resolution) or a
    // transport failure. Still an object, never a throw.
    const message = error instanceof Error ? error.message : String(error);
    return JSON.stringify({ success: false, error: message }, null, 2);
  }

  const status = error.status;
  let body: unknown;
  let isJson = true;
  try {
    body = JSON.parse(error.body);
  } catch {
    isJson = false;
  }
  const isObject = isJson && body !== null && typeof body === "object" && !Array.isArray(body);
  // FastAPI's own refusals are `{detail}`: surface that, else the whole body.
  const detail = !isJson
    ? error.body
    : isObject && "detail" in (body as Record<string, unknown>)
      ? (body as Record<string, unknown>).detail
      : body;

  if (status === 404 || status === 405) {
    return JSON.stringify(
      {
        success: false,
        not_available: true,
        http_status: status,
        error:
          `System apply is not available on this Trinity build: POST /api/systems/apply answered ${status}, `
          + "so the backend predates apply mode. Nothing was planned or written, and do NOT fall back to "
          + "deploy_system: it is create-only and would create suffixed copies (`<name>_2`) of members "
          + "that already exist. Upgrade the instance to a build with apply mode, or change existing "
          + "members one at a time with the per-agent tools.",
        detail,
      },
      null,
      2
    );
  }

  if (status === 403) {
    return accessDenied(context, {
      success: false,
      not_permitted: true,
      http_status: 403,
      error:
        "Refused (403): apply is human-only: run it with a person's credential (an interactive session "
        + "or an operator's own user-scoped key) holding the 'creator' role. Agent, connector and system "
        + "keys are refused, because apply writes to existing agents. `detail` names the gate that refused.",
      detail,
    });
  }

  if (isObject) {
    return JSON.stringify(
      { ...(body as Record<string, unknown>), http_status: status, success: false },
      null,
      2
    );
  }
  return JSON.stringify({ success: false, http_status: status, detail }, null, 2);
}

/**
 * Create system management tools with the given client
 * @param client - Base Trinity client (provides base URL, no auth when requireApiKey=true)
 * @param requireApiKey - Whether API key authentication is enabled
 */
export function createSystemTools(
  client: TrinityClient,
  requireApiKey: boolean
) {
  /**
   * Get Trinity client with appropriate authentication
   * When requireApiKey is true, REQUIRES MCP API key from auth context
   * When requireApiKey is false, uses the base client (backward compatibility)
   */
  const getClient = (authContext?: McpAuthContext): TrinityClient => {
    if (requireApiKey) {
      // MCP API key is REQUIRED - no fallback
      if (!authContext?.mcpApiKey) {
        throw new Error("MCP API key authentication required but no API key found in request context");
      }
      // Create new client instance authenticated with user's MCP API key
      const userClient = new TrinityClient(client.getBaseUrl());
      userClient.setToken(authContext.mcpApiKey);
      return userClient;
    }
    // API key auth disabled - use base client (backward compatibility)
    return client;
  };

  return {
    // ========================================================================
    // deploy_system - Deploy multi-agent system from YAML manifest
    // ========================================================================
    deploySystem: {
      name: "deploy_system",
      description:
        "Deploy a multi-agent system from a YAML manifest. " +
        "With an agent key, this needs the agent-management permission an instance admin grants (ephemeral helpers excepted); without it the call is refused with `agent_management_not_permitted`. " +
        "The manifest defines the system name, agents, permissions, schedules, and shared folders. " +
        "Supports dry_run mode for validation without deployment — a dry run resolves each `local:` template, "
        + "so `status: 'invalid'` with a populated `failed[]` means the manifest would not deploy cleanly. " +
        "Agents are created with naming pattern '{system}-{agent}' (e.g., 'content-production-orchestrator'). " +
        "Supports permission presets: 'full-mesh', 'orchestrator-workers', 'none', or explicit rules. " +
        "Deploy is best-effort: check `status` ('deployed' | 'partial' | 'failed') and `failed[]` " +
        "in the response — a partial deploy still creates the remaining agents. " +
        "Pass strict: true to restore abort-on-first-error.",
      parameters: z.object({
        manifest: z
          .string()
          .describe(
            "YAML manifest as a string. Format:\n" +
            "name: system-name\n" +
            "description: System description\n" +
            "prompt: System-wide instructions (optional)\n" +
            "agents:\n" +
            "  orchestrator:\n" +
            "    template: github:Org/repo\n" +
            "    kind: deployment  # optional: 'agent' (default; own branch + auto-push when the repo is the creator's own) or 'deployment' (pull-only)\n" +
            "    folders: {expose: true, consume: true}\n" +
            "    schedules: [{name: daily, cron: '0 9 * * *', message: '...'}]\n" +
            "permissions:\n" +
            "  preset: full-mesh  # or orchestrator-workers, none, explicit"
          ),
        dry_run: z
          .boolean()
          .optional()
          .describe(
            "If true, validates the manifest without creating agents. " +
            "Returns preview of agents to be created and warnings."
          ),
        strict: z
          .boolean()
          .optional()
          .describe(
            "If true, abort the deploy on the first agent-create failure " +
            "(legacy behavior) instead of the default best-effort " +
            "continue-and-report."
          ),
      }),
      execute: async (
        { manifest, dry_run, strict }: { manifest: string; dry_run?: boolean; strict?: boolean },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        // Call backend deploy endpoint
        const response = await apiClient.request<{
          status: string; // "deployed" | "partial" | "failed" | "valid" (dry_run) | "invalid" (dry_run, #1841)
          system_name: string;
          agents_created: string[];
          agents_to_create?: Array<{ name: string; short_name: string; template: string }>;
          prompt_updated: boolean;
          permissions_configured?: number;
          schedules_created?: number;
          warnings: string[];
          failed?: Array<{
            name: string;
            short_name: string;
            template: string;
            reason: string;
            status_code?: number;
          }>;
        }>("POST", "/api/systems/deploy", {
          manifest,
          dry_run: dry_run || false,
          strict: strict || false,
        });

        return JSON.stringify(response, null, 2);
      },
    },

    // ========================================================================
    // list_systems - List all deployed systems (agents grouped by prefix)
    // ========================================================================
    listSystems: {
      name: "list_systems",
      description:
        "List all deployed systems with their agents. " +
        "Groups agents by system prefix (before first '-'). " +
        "For example, agents 'content-production-orchestrator' and 'content-production-writer' " +
        "are grouped under system 'content-production'. " +
        "Returns system summaries with agent counts and details.",
      parameters: z.object({}),
      execute: async (_params: unknown, context?: { session?: McpAuthContext }) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        const response = await apiClient.request<{
          systems: Array<{
            name: string;
            agent_count: number;
            agents: Array<{
              name: string;
              status: string;
              template?: string;
            }>;
            created_at?: string;
          }>;
        }>("GET", "/api/systems");

        return JSON.stringify(response, null, 2);
      },
    },

    // ========================================================================
    // restart_system - Restart all agents in a system
    // ========================================================================
    restartSystem: {
      name: "restart_system",
      description:
        "Restart all agents belonging to a system. " +
        "Membership is resolved from the system's tag when the agents carry one, " +
        "falling back to the '{system_name}-*' name prefix. " +
        "Stops then starts each member. " +
        "Useful after configuration changes (credentials, schedules, shared folders). " +
        "Returns list of successfully restarted agents and any failures. " +
        "Requires the 'creator' role AND a human caller: an agent-scoped key is " +
        "refused with 403 (#2373), because this restarts every member without the " +
        "per-agent spawn-scope check that start_agent/stop_agent apply one at a time.",
      parameters: z.object({
        system_name: z
          .string()
          .describe(
            "System to restart (e.g., 'content-production'). " +
            "Members are the agents tagged with the system, else those matching " +
            "'{system_name}-*'."
          ),
      }),
      execute: async (
        { system_name }: { system_name: string },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        const response = await apiClient.request<{
          restarted: string[];
          failed: string[];
        }>("POST", `/api/systems/${encodeURIComponent(system_name)}/restart`);

        return JSON.stringify(response, null, 2);
      },
    },

    // ========================================================================
    // get_system_manifest - Export system configuration as YAML manifest
    // ========================================================================
    getSystemManifest: {
      name: "get_system_manifest",
      description:
        "Generate a YAML manifest for a deployed system. " +
        "Reconstructs the system configuration from current agent settings. " +
        "Useful for backup, documentation, or replicating systems. " +
        "Returns YAML string that can be used with deploy_system.",
      parameters: z.object({
        system_name: z
          .string()
          .describe(
            "System prefix to export (e.g., 'content-production'). " +
            "All agents matching '{system_name}-*' will be included."
          ),
      }),
      execute: async (
        { system_name }: { system_name: string },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        // Backend returns YAML as text/plain
        const yaml = await apiClient.request<string>(
          "GET",
          `/api/systems/${encodeURIComponent(system_name)}/manifest`
        );

        return yaml;
      },
    },

    // ========================================================================
    // teardown_system - Remove a deployed system (ent#454)
    //
    // License-blind proxy over the entitlement-gated
    // DELETE /api/enterprise/system-teardown/{name}. This module knows nothing
    // about entitlement; it reports what the route says (see teardownFailure).
    // ========================================================================
    teardownSystem: {
      name: "teardown_system",
      description:
        "Remove a deployed multi-agent system: every member agent, plus the " +
        "system view its deploy auto-created. The inverse of deploy_system. " +
        "DESTRUCTIVE — dry_run defaults to TRUE, unlike deploy_system: call it " +
        "first, show the human the removal set, and pass the returned member " +
        "names back as `agents` to execute. " +
        "Each member goes through the standard per-agent soft-delete, so its " +
        "data, history and name are retained for the platform's recovery window " +
        "(180 days by default) and an admin can restore the record; recovery is " +
        "metadata-only, and members reported 'discarded' were ephemeral and have " +
        "NO recovery window. " +
        "Membership comes from the system's deploy tag, falling back to the " +
        "'{system_name}-*' name prefix — check each member's `evidence` field: " +
        "'prefix' means it matched by NAME ONLY and may belong to a different " +
        "system whose name starts the same way, so a human should confirm it. " +
        "Best-effort and honest: switch on `status` ('preview' | 'torn_down' | " +
        "'partial' | 'failed'), never the HTTP code, and read every member's " +
        "`outcome` — 'skipped' is not 'failed'. " +
        "Requires the 'creator' role AND a HUMAN caller: only an interactive " +
        "session or an operator's own user-scoped key is accepted — agent, " +
        "connector, system and every other credential kind is refused, because " +
        "this removes N agents in one call without the per-agent spawn-scope " +
        "check that delete_agent applies one at a time. " +
        "Licensed separately — on a build without it, the tool says so rather " +
        "than failing opaquely.",
      parameters: z.object({
        system_name: z
          .string()
          .describe(
            "System to remove (e.g., 'content-production'). Members are the "
            + "agents tagged with the system, else those matching "
            + "'{system_name}-*'. Do NOT take this name from list_systems, "
            + "which groups by the last hyphen and can report a name that is "
            + "not a real system."
          ),
        dry_run: z
          .boolean()
          .optional()
          .describe(
            "Defaults to TRUE. Returns the complete removal set — members with "
            + "their current status, evidence and ephemeral flag, the system "
            + "view, the tag — and writes NOTHING. A member whose status is 'no "
            + "container' exists as a record only (recovered or pruned); it is "
            + "removed the same way, which is what frees its name. Pass false "
            + "only to execute a removal a human has confirmed."
          ),
        agents: z
          .array(z.string())
          .optional()
          .describe(
            "The member names confirmed for removal. Only the intersection with "
            + "current membership is removed; a name that is no longer a member "
            + "comes back 'skipped: not_a_member' and a current member you omit "
            + "comes back 'skipped: not_confirmed'. Omit to remove every current "
            + "member."
          ),
        strict: z
          .boolean()
          .optional()
          .describe(
            "Stop at the first member that fails, marking the remainder "
            + "'aborted'. Default is best-effort: one failure never aborts the "
            + "rest."
          ),
      }),
      execute: async (
        {
          system_name,
          dry_run,
          agents,
          strict,
        }: {
          system_name: string;
          dry_run?: boolean;
          agents?: string[];
          strict?: boolean;
        },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        // Preview-first unless the caller explicitly says otherwise. The
        // asymmetry with deploy_system (which defaults false) is deliberate:
        // the cost of an unwanted preview is a wasted round trip, and the cost
        // of an unwanted execute is a deleted fleet.
        const preview = dry_run === undefined ? true : dry_run;

        try {
          const response = await apiClient.request<unknown>(
            "DELETE",
            `/api/enterprise/system-teardown/${encodeURIComponent(system_name)}`
              + `?dry_run=${preview}`,
            // A DELETE body: `client.request` carries one, and the confirmed
            // set belongs here rather than in the query string because it is
            // the time-of-check/time-of-use seam the server re-validates.
            { agents: agents ?? null, strict: strict ?? false }
          );
          return JSON.stringify(response, null, 2);
        } catch (error) {
          return teardownFailure(error);
        }
      },
    },

    // ========================================================================
    // apply_system - Reconcile a live instance to a manifest (ent#695)
    //
    // A NEW tool rather than a `mode` on deploy_system: a released
    // deploy_system has no `mode` in its schema, so `mode: "apply"` sent to an
    // older instance would be dropped and run CREATE (suffixing the fleet),
    // while an unknown tool name fails closed. deploy_system is untouched.
    // ========================================================================
    applySystem: {
      name: "apply_system",
      description:
        "Reconcile a live instance to a system manifest: the idempotent sibling of deploy_system, "
        + "same manifest format. Per declared member: absent → created; existing → its declared fields "
        + "updated in place; an agent carrying the system's tag but not declared → reported as drift. "
        + "It never deletes, stops, restarts, renames or suffixes an existing agent, so applying an "
        + "unchanged manifest twice changes nothing. "
        + "dry_run defaults to TRUE, unlike deploy_system: a call without it returns the plan "
        + "(per-member verdicts create | update | unchanged | blocked, drift, blockers) and writes "
        + "nothing; pass dry_run: false only to apply a plan a human has reviewed. "
        + "Switch on `status`, never the HTTP code: 'valid' | 'invalid' on a dry run; 'applied' | "
        + "'partial' | 'failed' | 'refused' on a real run (a refused run wrote nothing). Every entry in "
        + "`blockers[]` names its `code`, a `remedy` and whether it is `retryable`. Weak membership "
        + "evidence (an agent that matches the system by NAME ONLY) is refused before any write; its "
        + "remedy says which tag to add (tag_agent). "
        + "Human-only: needs the 'creator' role AND a person's credential (an interactive session or an "
        + "operator's own user-scoped key); agent, connector and system keys are refused "
        + "(`not_permitted`). On a build without apply mode the result is `not_available: true`: "
        + "do NOT fall back to deploy_system there, it is create-only and would create suffixed "
        + "copies (`_2`) of members that already exist.",
      parameters: z.object({
        manifest: z
          .string()
          .describe(
            "The system manifest as a YAML string, in the format deploy_system takes (name, "
            + "description, prompt, agents, permissions). Members are named '{name}-{agent}'."
          ),
        dry_run: z
          .boolean()
          .optional()
          .describe(
            "Defaults to TRUE: returns the plan and writes nothing. Pass false only to apply a "
            + "plan a human has reviewed."
          ),
      }),
      execute: async (
        { manifest, dry_run }: { manifest: string; dry_run?: boolean },
        context?: DenyCallContext
      ) => {
        // Preview-first unless the caller explicitly says otherwise (the
        // teardown_system precedent): an unwanted preview costs a round trip,
        // an unwanted apply writes to existing agents.
        const preview = dry_run ?? true;
        try {
          // Inside the try: getClient throws on a missing API key, and this
          // tool never throws.
          const apiClient = getClient(context?.session);
          const response = await apiClient.request<unknown>("POST", "/api/systems/apply", {
            manifest,
            dry_run: preview,
          });
          return JSON.stringify(response, null, 2);
        } catch (error) {
          return applyFailure(error, context);
        }
      },
    },
  };
}
