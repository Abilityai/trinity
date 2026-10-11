/**
 * Skill-library SOURCE management tools (abilityai/trinity-enterprise#692).
 *
 * - list_skill_sources     → GET    /api/skills/sources
 * - register_skill_source  → POST   /api/skills/sources/apply   (idempotent on url)
 * - update_skill_source    → PUT    /api/skills/sources/{id}
 * - delete_skill_source    → DELETE /api/skills/sources/{id}
 * - sync_skill_source      → POST   /api/skills/sources/{id}/sync
 * - sync_skill_library     → POST   /api/skills/library/sync
 *
 * The fence is the PRINCIPAL, not the transport. Every route above is
 * `require_admin` + `reject_agent_principal`; `require_admin` admits an admin's
 * JWT, an admin owner's user-scoped key and the system agent, and refuses agent
 * and connector keys itself (#1890, #2323). These tools reach exactly that set,
 * so exposing them grants no authority the REST surface did not already grant.
 *
 * Thin proxy: no business logic here. The bodies the routes return are handed
 * back unchanged; the only thing added is the sync outcome vocabulary
 * (`ran` / `refused_busy` / `failed`), read off the HTTP status the route
 * already chooses.
 */

import { z } from "zod";
import { TrinityClient, ApiError } from "../client.js";
import type { McpAuthContext } from "../types.js";
import { accessDenied, type DenyCallContext } from "../access.js";

/**
 * Scopes that may see and call these tools — an ALLOW-LIST (#848: never a
 * deny-check, so an absent, unknown or future scope fails closed). These are
 * the MCP scopes `ADMIN_GATE_SCOPES` admits (`dependencies.py`); the backend
 * still decides the ROLE, so a user key whose owner is not an admin sees the
 * tools and is refused with the route's own `Admin access required`.
 *
 * Seam for a controller scope (abilityai/trinity-enterprise#693): it does not
 * exist yet. When it does it joins this set AND the routes opt it in per route
 * (`require_admin_allowing(...)`, the ops-key pattern) — never by widening
 * `ADMIN_GATE_SCOPES`.
 */
export const SOURCE_ADMIN_SCOPES: ReadonlySet<string> = new Set(["user", "system"]);

const canAccess = (auth: any): boolean => SOURCE_ADMIN_SCOPES.has(auth?.scope ?? "");

const DENIED_REASON =
  "skill-library source management is for admin principals only (an admin's user-scoped key or the system agent); " +
  "agent-scoped and connector keys are refused";

const REINJECT_OUTCOME_AT =
  "get_skills_library_status → last_fleet_reinject (counts; it belongs to this sync when its commit_sha matches " +
  "and finished_at is set)";

type Ctx = { session?: McpAuthContext } & DenyCallContext;

/** The route's own `detail` (string or object), falling back to the raw body. */
function routeDetail(error: ApiError): unknown {
  try {
    const parsed = JSON.parse(error.body);
    if (parsed && typeof parsed === "object" && "detail" in parsed) return parsed.detail;
    return parsed;
  } catch {
    return error.body;
  }
}

/** A 4xx comes back as the route's refusal; anything else propagates. */
function refusal(error: unknown): Record<string, unknown> {
  if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
    return { success: false, status: error.status, error: routeDetail(error) };
  }
  throw error;
}

/** Sync status vocabulary: the route answers 409 on the held lock, 400 on failure. */
function syncRefusal(error: unknown): Record<string, unknown> {
  const r = refusal(error);
  if (r.status === 409) return { outcome: "refused_busy", retryable: true, ...r };
  if (r.status === 400) return { outcome: "failed", ...r };
  return { outcome: "refused", ...r };
}

const pick = (params: Record<string, unknown>, keys: string[]): Record<string, unknown> =>
  Object.fromEntries(keys.filter((k) => params[k] !== undefined).map((k) => [k, params[k]]));

const json = (value: unknown): string => JSON.stringify(value, null, 2);

export function createSkillSourceTools(client: TrinityClient, requireApiKey: boolean) {
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
   * In-tool re-check (defence in depth — `canAccess` governs advertisement and
   * call dispatch, the backend governs authority). Absent auth is legitimate
   * only in dev mode.
   */
  const deny = (context: Ctx | undefined): string | undefined => {
    const scope = context?.session?.scope;
    const permitted = scope === undefined ? !requireApiKey : SOURCE_ADMIN_SCOPES.has(scope);
    if (permitted) return undefined;
    return accessDenied(context, { success: false, error: "Access denied", reason: DENIED_REASON });
  };

  /** Run one proxied call: gate, request, body back unchanged or the route's refusal. */
  const proxy = async (
    context: Ctx | undefined,
    method: string,
    path: string,
    body?: Record<string, unknown>,
  ): Promise<string> => {
    const denied = deny(context);
    if (denied) return denied;
    try {
      return json(await getClient(context?.session).request<unknown>(method, path, body));
    } catch (e) {
      return json(refusal(e));
    }
  };

  const sync = async (context: Ctx | undefined, path: string): Promise<string> => {
    const denied = deny(context);
    if (denied) return denied;
    try {
      const result = await getClient(context?.session).request<Record<string, unknown>>("POST", path);
      const started = result?.fleet_reinject_started === true;
      return json({
        outcome: "ran",
        result,
        fleet_reinject: started ? { started: true, read_outcome_with: REINJECT_OUTCOME_AT } : { started: false },
      });
    } catch (e) {
      return json(syncRefusal(e));
    }
  };

  const sourcePath = (id: string) => `/api/skills/sources/${encodeURIComponent(id)}`;

  return {
    listSkillSources: {
      name: "list_skill_sources",
      description:
        "List the skills library's sources in resolution order (first wins a name clash): id, name, url, ref, " +
        "ref_type, priority, enabled, last_sync, last_sync_status and last_error per source, plus the library-wide " +
        "sync summary and the last fleet re-inject report. Admin only: visible to an admin's user-scoped key and the " +
        "system agent; agent-scoped and connector keys never see it.",
      parameters: z.object({}),
      canAccess,
      execute: async (_params: unknown, context?: Ctx) => proxy(context, "GET", "/api/skills/sources"),
    },

    registerSkillSource: {
      name: "register_skill_source",
      description:
        "Register a skills repository as a library source, idempotently on its url: no source for the repository → " +
        "created (`action: \"created\"`); one source → the fields you name are updated in place (`updated`, or " +
        "`unchanged` when nothing differs) and fields you omit are kept; two sources for the repository on different " +
        "refs → refused with `ambiguous_source` naming their ids (use update_skill_source). Never creates a duplicate. " +
        "The url must not carry a token or password — a private repository resolves through the instance's own " +
        "GitHub PAT. Registering does not sync: call sync_skill_source next. Admin only.",
      parameters: z.object({
        url: z.string().min(1).max(500).describe("Repository URL, e.g. https://github.com/owner/repo"),
        ref: z.string().min(1).max(200).optional().describe("Branch or tag (default on create: main)"),
        ref_type: z.enum(["branch", "tag"]).optional().describe("`tag` pins and refuses a moved tag; `branch` tracks the head"),
        priority: z.number().int().min(1).max(10000).optional().describe("Lower wins a name clash (custom default 100, bundled 1000)"),
        name: z.string().min(1).max(100).optional().describe("Display name (default on create: owner/repo)"),
        enabled: z.boolean().optional(),
      }),
      canAccess,
      execute: async (params: Record<string, unknown>, context?: Ctx) =>
        proxy(context, "POST", "/api/skills/sources/apply",
          pick(params, ["url", "ref", "ref_type", "priority", "name", "enabled"])),
    },

    updateSkillSource: {
      name: "update_skill_source",
      description:
        "Patch one skill source by id: name, url, ref, ref_type, enabled, priority. Omitted fields are untouched. " +
        "Changing url/ref/ref_type clears its sync bookkeeping so a tag bump is not refused as a moved tag. Admin only.",
      parameters: z.object({
        source_id: z.string().min(1).describe("Source id from list_skill_sources"),
        name: z.string().min(1).max(100).optional(),
        url: z.string().min(1).max(500).optional(),
        ref: z.string().min(1).max(200).optional(),
        ref_type: z.enum(["branch", "tag"]).optional(),
        enabled: z.boolean().optional(),
        priority: z.number().int().min(1).max(10000).optional(),
      }),
      canAccess,
      execute: async (params: Record<string, unknown> & { source_id: string }, context?: Ctx) =>
        proxy(context, "PUT", sourcePath(params.source_id),
          pick(params, ["name", "url", "ref", "ref_type", "enabled", "priority"])),
    },

    deleteSkillSource: {
      name: "delete_skill_source",
      description:
        "Remove one skill source by id and reclaim its checkout. Agents' assignments are kept: a skill keeps " +
        "resolving through any other source that still ships it. Admin only.",
      parameters: z.object({ source_id: z.string().min(1).describe("Source id from list_skill_sources") }),
      canAccess,
      execute: async ({ source_id }: { source_id: string }, context?: Ctx) =>
        proxy(context, "DELETE", sourcePath(source_id)),
    },

    syncSkillSource: {
      name: "sync_skill_source",
      description:
        "Sync ONE skill source from its repository, leaving the others untouched. Returns `outcome`: `ran` (with the " +
        "route's per-source `result`), `refused_busy` (another sync holds the library lock — retryable), or `failed` " +
        "(with the route's error). Admin only.",
      parameters: z.object({ source_id: z.string().min(1).describe("Source id from list_skill_sources") }),
      canAccess,
      execute: async ({ source_id }: { source_id: string }, context?: Ctx) =>
        sync(context, `${sourcePath(source_id)}/sync`),
    },

    syncSkillLibrary: {
      name: "sync_skill_library",
      description:
        "Sync every enabled skill source. Returns `outcome`: `ran`, `refused_busy` (another sync holds the library " +
        "lock — retryable) or `failed`. When the library moved and fleet re-inject is on, the sync starts a background " +
        "re-inject to every running agent: `fleet_reinject.started` says so and names where its outcome lands " +
        "(get_skills_library_status → last_fleet_reinject). Admin only.",
      parameters: z.object({}),
      canAccess,
      execute: async (_params: unknown, context?: Ctx) => sync(context, "/api/skills/library/sync"),
    },
  };
}
