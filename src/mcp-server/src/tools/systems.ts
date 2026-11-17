/**
 * System Management Tools
 *
 * MCP tools for deploying and managing multi-agent systems via YAML manifests.
 */

import { z } from "zod";
import { TrinityClient, ApiError } from "../client.js";
import { accessDenied, type DenyCallContext } from "../access.js";
import { toolFailed } from "../audit.js";
import type { McpAuthContext } from "../types.js";

/** The longest backend `detail` an audit reason carries (#4026): a row reason is a label, not a transcript. */
const AUDIT_DETAIL_MAX = 200;

/**
 * A SHORT audit-row reason for an HTTP failure (#4026): the status, then the
 * body's own `status` (an apply / teardown report) or its string `detail`
 * (capped), else the bare status. `body` is the PARSED body; anything that is
 * not a JSON object yields the bare status, so a raw HTML page never reaches
 * the row.
 */
function httpAuditReason(status: number, body: unknown): string {
  const obj =
    body !== null && typeof body === "object" && !Array.isArray(body)
      ? (body as Record<string, unknown>)
      : undefined;
  if (typeof obj?.status === "string" && obj.status) return `HTTP ${status}: status '${obj.status}'`;
  if (typeof obj?.detail === "string" && obj.detail) {
    const d = obj.detail;
    return `HTTP ${status}: ${d.length > AUDIT_DETAIL_MAX ? `${d.slice(0, AUDIT_DETAIL_MAX)}…` : d}`;
  }
  return `HTTP ${status}`;
}

/** `JSON.parse`, or `undefined` for a body that is not JSON. */
function parseJsonOrUndefined(text: string | undefined): unknown {
  if (!text) return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

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
 *
 * Audit (#4026): a returned result is not a thrown one, so each branch stamps
 * the call's outcome out-of-band, leaving the caller's JSON untouched. The 403
 * is an authorization refusal and goes through `accessDenied` (`denied`, the
 * #3435 precedent); every other branch, the `failed` report included, through
 * `toolFailed`. Each row's reason is short: the status, the backend's own
 * `status` or `detail` (`httpAuditReason`), then the flags.
 */
function teardownFailure(error: unknown, context: DenyCallContext | undefined): string {
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
        return toolFailed(context, report, httpAuditReason(500, report));
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

  const envelope = { success: false, error: message, detail: raw, ...flags };
  if (status === 403) {
    // The row names the backend's own cause (a licensing / human-only detail,
    // or a `failed` report whose members were all refused), so the 403s stay
    // distinguishable there; the caller's paragraph is the same for all.
    return accessDenied(context, envelope, httpAuditReason(403, parseJsonOrUndefined(body)));
  }
  // The row carries the backend's own detail and then the flags the caller was
  // given, so a route that is absent ("Not Found") and a serving route that
  // found no such system read differently on the row even where the caller's
  // message does not tell them apart.
  const flagNames = Object.keys(flags);
  const auditReason =
    status === undefined
      ? raw
      : httpAuditReason(status, parseJsonOrUndefined(body))
        + (flagNames.length > 0 ? ` (${flagNames.join(", ")})` : "");
  return toolFailed(context, envelope, auditReason);
}

/**
 * trinity-enterprise#695: the scopes that may see `apply_system`. An
 * ALLOW-list (#848: a null or unknown scope fails closed) of what the
 * backend's `require_person` admits over MCP, a person's own user-scoped key.
 * It refuses agent, connector and system keys, so those sessions are never
 * shown a tool they cannot call (the #3435 / ent#966 shape).
 */
const APPLY_SYSTEM_SCOPES: ReadonlySet<string> = new Set(["user"]);
export const applySystemAccess = (auth: any): boolean =>
  APPLY_SYSTEM_SCOPES.has(auth?.scope ?? "");

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
 * `failed[]` reach the caller intact. Anything else (a JSON scalar, array or
 * null, or a non-JSON body) comes back as `{success: false, http_status,
 * detail}`.
 *
 * Audit (#4026): every branch but the 403 is a returned failure that is NOT an
 * authorization denial, so it is stamped through `toolFailed` (`success:
 * false`, no `denied`) with a short reason: `httpAuditReason`, built here for
 * 404/405 and, for the generic tail, through `bodyAsResult` (shared with
 * deploy_system, #4027). A raw non-object body never reaches the row.
 */
function applyFailure(
  error: unknown,
  context: DenyCallContext | undefined,
  sentRealRun = false
): string {
  if (!(error instanceof ApiError)) {
    // No HTTP status at all: a missing API key (client resolution) or a
    // transport failure. Still an object, never a throw. A REAL run whose
    // request went out may have applied some writes before the answer was
    // lost; apply is idempotent, so the honest next step is a dry run.
    const message = error instanceof Error ? error.message : String(error);
    const result: Record<string, unknown> = { success: false, error: message };
    if (sentRealRun) {
      result.note =
        "No answer arrived for a real run: some writes may already have been applied. "
        + "Apply is idempotent: run apply_system with dry_run: true to see where the "
        + "system stands, then re-apply.";
    }
    return toolFailed(context, result, sentRealRun ? `${message} (real run, outcome unknown)` : message);
  }

  const status = error.status;
  const errorBody = readErrorBody(error);
  const { detail } = errorBody;

  if (status === 404 || status === 405) {
    return toolFailed(
      context,
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
      `HTTP ${status}: apply route absent (not_available)`
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

  const { envelope, auditReason } = bodyAsResult(status, errorBody);
  return toolFailed(context, envelope, auditReason);
}

/** An `ApiError` body, read once: the parsed value, whether it is a JSON object, and its `detail`. */
interface ErrorBody {
  body: unknown;
  isObject: boolean;
  detail: unknown;
}

function readErrorBody(error: ApiError): ErrorBody {
  // `JSON.parse` never yields `undefined`, so `undefined` here means "not JSON".
  const body = parseJsonOrUndefined(error.body);
  const isJson = body !== undefined;
  const isObject = isJson && body !== null && typeof body === "object" && !Array.isArray(body);
  // FastAPI's own refusals are `{detail}`: surface that, else the whole body.
  const detail = !isJson
    ? error.body
    : isObject && "detail" in (body as Record<string, unknown>)
      ? (body as Record<string, unknown>).detail
      : body;
  return { body, isObject, detail };
}

/**
 * A non-2xx answer whose body IS the result (ent#695 apply, #4027 deploy).
 *
 * A JSON OBJECT body comes back with `http_status` and `success: false`
 * added, so a report's `status` and `failed[]` reach the caller intact;
 * anything else (a JSON scalar, array or null, or a non-JSON body) becomes
 * `{success: false, http_status, detail}`.
 *
 * The audit reason is `httpAuditReason`'s (#4026), which gives anything that
 * is not a JSON object the bare status, so a raw body never reaches the row.
 */
function bodyAsResult(
  status: number,
  { body, isObject, detail }: ErrorBody
): { envelope: Record<string, unknown>; auditReason: string } {
  const envelope = isObject
    ? { ...(body as Record<string, unknown>), http_status: status, success: false }
    : { success: false, http_status: status, detail };
  return { envelope, auditReason: httpAuditReason(status, body) };
}

/**
 * Map a deploy failure onto the result the caller reads (#4027).
 *
 * A create-mode deploy in which every member failed answers 500 with the
 * full report as its body (ent#125), and that report is the only actionable
 * output: it comes back AS the result, through the same `bodyAsResult` as
 * apply, as does every other non-2xx (a 400 manifest error, a `strict`
 * abort, a 422). Only the audit label differs: a 403 is an authorization
 * refusal, stamped `denied` through `accessDenied` exactly as the thrown 403
 * it replaces was; everything else is stamped `failed` through `toolFailed`.
 *
 * No HTTP status at all (a missing API key, a lost connection) still throws,
 * as it always has: those are not deploy results.
 */
function deployFailure(error: unknown, context: DenyCallContext | undefined): string {
  if (!(error instanceof ApiError)) {
    throw error;
  }
  const { envelope, auditReason } = bodyAsResult(error.status, readErrorBody(error));
  return error.status === 403
    ? accessDenied(context, envelope, auditReason)
    : toolFailed(context, envelope, auditReason);
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
        "Agents are created with naming pattern '{system}-{agent}' (e.g., 'content-production-orchestrator'); " +
        "with `naming: bare` the agent key is the full agent name. " +
        "Supports permission presets: 'full-mesh', 'orchestrator-workers', 'none', or explicit rules. " +
        "While the instance limits agent-created agents to a list of template sources, an agent key's manifest with a member outside it is refused as a whole (403 `template_source_not_allowed`, `members[]`), and a dry run lists those members in `failed[]`. " +
        "Deploy is best-effort: check `status` ('deployed' | 'partial' | 'failed') and `failed[]` " +
        "in the response — a partial deploy still creates the remaining agents. " +
        "`deploy: false` members are skipped and listed in `agents_declared_only[]`; a manifest whose every " +
        "member is `deploy: false` is refused with a 400 `nothing_to_deploy` (use apply_system to report on them). " +
        "Pass strict: true to restore abort-on-first-error. " +
        "A `failed` deploy (no member created) comes back as that report with `http_status: 500` and " +
        "`success: false`; any other result without `status` (e.g. a 400 manifest error, a 403, a `strict` abort) " +
        "has `success: false`, `http_status` and the backend's `detail` when it sent one.",
      parameters: z.object({
        manifest: z
          .string()
          .describe(
            "YAML manifest as a string. Format:\n" +
            "name: system-name\n" +
            "naming: bare  # optional: agent keys are full agent names; default prefixed ('{name}-{agent}')\n" +
            "description: System description\n" +
            "prompt: System-wide instructions (optional; applied for an admin only, skipped with a prompt_requires_admin warning for any other caller)\n" +
            "agents:\n" +
            "  orchestrator:\n" +
            "    template: github:Org/repo\n" +
            "    kind: deployment  # optional: 'agent' (default; own branch + auto-push when the repo is the creator's own) or 'deployment' (pull-only)\n" +
            "    folders: {expose: true, consume: true}\n" +
            "    schedules: [{name: daily, cron: '0 9 * * *', message: '...'}]\n" +
            "    skills: [skill-name]  # optional, additive; also skill_sets: [set-name]\n" +
            "    gates: {skill-name: {approver: primary, deadline_hours: 24}}  # optional approval map\n" +
            "    grants: [agents.manage]  # optional; written only for an admin in a signed-in session\n" +
            "    seat: role-id  # optional canon seat the agent holds; written only for an admin in a signed-in session; person_seats: [{role, kind}] names no person\n" +
            "  catalog-entry:\n" +
            "    template: github:Org/other\n" +
            "    deploy: false  # optional: named in the system, never created\n" +
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
        context?: DenyCallContext
      ) => {
        const authContext = context?.session;
        // Outside the try, as before: a missing API key still throws (#4027 scope).
        const apiClient = getClient(authContext);

        // Call backend deploy endpoint
        try {
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
        } catch (error) {
          // A `failed` deploy is a 500 whose body IS the report (#4027).
          return deployFailure(error, context);
        }
      },
    },

    // ========================================================================
    // list_systems - List deployed systems by their system tag (#4129)
    // ========================================================================
    listSystems: {
      name: "list_systems",
      description:
        "List the systems deployed or applied from a manifest, by their system tag, with " +
        "the agents you can access that carry it. A tag is listed only when it is known " +
        "to be a system (its auto-created system view, an apply of that manifest, or a " +
        "member named '<system>-*'); a plain tag such as 'prod' is not. Names are never " +
        "split at a hyphen, so a system whose members are named bare (e.g. 'writer') " +
        "lists under its own name. Returns system summaries with agent counts and details.",
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
        "A system whose tagged members all lack that prefix is read as bare-named, and then " +
        "an untagged '{system_name}-*' agent is not a member. " +
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
            "System name to export (e.g., 'content-production'). " +
            "Members are the agents carrying the system's tag (every deploy and apply writes it), " +
            "plus untagged agents named '{system_name}-*' as a fallback — except when every " +
            "tagged member lacks that prefix (a bare-named system): then the tag alone decides."
          ),
        naming: z
          .enum(["prefixed", "bare"])
          .optional()
          .describe(
            "How agent keys are written. Omitted: 'bare' when no member carries the " +
            "'{system_name}-' prefix, else 'prefixed' (members without the prefix are omitted " +
            "and named in a leading '# unobserved:' comment). 'bare' keys every member by its full name."
          ),
      }),
      execute: async (
        { system_name, naming }: { system_name: string; naming?: "prefixed" | "bare" },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        // Backend returns YAML as text/plain
        const yaml = await apiClient.request<string>(
          "GET",
          `/api/systems/${encodeURIComponent(system_name)}/manifest` +
            (naming ? `?naming=${encodeURIComponent(naming)}` : "")
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
            + "'{system_name}-*'. list_systems lists systems deployed or applied "
            + "from a manifest, by their tag; take a system name from a deploy "
            + "or apply result, or confirm it with the detail route."
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
        context?: DenyCallContext
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
          return teardownFailure(error, context);
        }
      },
    },

    // ========================================================================
    // apply_system - Reconcile a live instance to a manifest (ent#695)
    //
    // A NEW tool rather than a `mode` on deploy_system: a released
    // deploy_system has no `mode` in its schema, so `mode: "apply"` sent to an
    // older instance would be dropped and run CREATE (suffixing the fleet),
    // while an unknown tool name fails closed. deploy_system's request and
    // 2xx results are untouched (its non-2xx answers return, #4027).
    // ========================================================================
    applySystem: {
      name: "apply_system",
      canAccess: applySystemAccess,
      description:
        "Reconcile a live instance to a system manifest: the idempotent sibling of deploy_system, "
        + "same manifest format. Per declared member: absent → created; existing → declared fields "
        + "updated in place; an undeclared agent carrying the system's tag → reported as drift. "
        + "It never deletes, stops, restarts, renames or suffixes an existing agent, so re-applying an "
        + "unchanged manifest changes nothing. "
        + "dry_run defaults to TRUE, unlike deploy_system: a call without it returns the plan "
        + "(per-member verdicts create | update | unchanged | blocked, drift, blockers) and writes "
        + "nothing; pass dry_run: false only to apply a plan a human has reviewed. "
        + "Switch on `status`, never the HTTP code: 'valid' | 'invalid' on a dry run; 'applied' | "
        + "'partial' | 'failed' | 'refused' on a real run (a refused run wrote nothing). A result "
        + "without `status` (a 400/422 manifest error, a 401, an unexpected 500) has `success: false` "
        + "and a `detail`. Each "
        + "`blockers[]` entry names its `code`, a `remedy` and whether it is `retryable`. Weak membership "
        + "evidence (an agent matching the system by NAME ONLY) is refused before any write; its "
        + "remedy says which tag to add (tag_agent). Under `naming: bare` an untagged agent the manifest "
        + "declares is adopted instead (one tag write, listed in `adopted[]`). "
        + "Per-member skills, skill_sets, gates, grants, seat and person_seats are additive: an "
        + "absent key or a held extra is reported, never revoked; grants and seat are written only for an "
        + "admin in a signed-in session; from an MCP key they are only reported. `summary` counts write | report | unobserved per group; "
        + "a real run's `write` and `adopted[]` name only what landed. "
        + "Human-only: needs the 'creator' role AND a person's credential (an interactive session or an "
        + "operator's own user-scoped key); agent, connector and system sessions are not shown it, "
        + "and their keys are refused (`not_permitted`). On a build without apply mode the result is `not_available: true`: "
        + "do NOT fall back to deploy_system: it is create-only and would make suffixed "
        + "copies (`_2`) of existing members.",
      parameters: z.object({
        manifest: z
          .string()
          .describe(
            "The system manifest as a YAML string, in the format deploy_system takes (name, "
            + "description, prompt, agents, permissions). Members are named '{name}-{agent}', or by "
            + "their agent key under `naming: bare`. Per member, optionally: skills, skill_sets, "
            + "gates ({skill: {approver, deadline_hours}}), grants, seat (a canon role id), "
            + "person_seats ([{role, kind}], never a person or an email; add `holder: member` with "
            + "kind primary to seat and share this instance's recorded member — written for an "
            + "admin's own key or session, on a member it creates or owns) and deploy: false (a "
            + "declared-only member: verdict declared_only, never created or deleted)."
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
        let sent = false;
        try {
          // Inside the try: getClient throws on a missing API key, and this
          // tool never throws.
          const apiClient = getClient(context?.session);
          sent = true;
          const response = await apiClient.request<unknown>("POST", "/api/systems/apply", {
            manifest,
            dry_run: preview,
          });
          return JSON.stringify(response, null, 2);
        } catch (error) {
          return applyFailure(error, context, sent && !preview);
        }
      },
    },
  };
}
