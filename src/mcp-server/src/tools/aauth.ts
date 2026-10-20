/**
 * AAuth tools (trinity-enterprise#878 / #879) — `request_person_token`,
 * `call_aauth_resource`.
 *
 * An agent obtains a person token for a resource from its owner's person
 * server, as itself, after its owner has bound it once through the person
 * server's link-and-code flow (ent#878); and calls an allow-listed AAuth
 * resource as itself, climbing the requirement ladder — agent token → person
 * token → auth token — with the backend doing every exchange (ent#879). The
 * backend does the signing, the caching and the holding; these tools relay
 * the OUTCOME — token metadata, the resource's answer, or the link + code the
 * agent must hand to its owner — and never a token itself (the backend
 * responses have no field for one; a defensive strip here is belt and braces,
 * not the mechanism).
 *
 * ── Self-acting, agent scope only ────────────────────────────────────────
 * There is no `agent_name` parameter: the acting agent comes from the key
 * (`resolveActingAgent`, the ask_operator shape). Unlike the other self-acting
 * tools, a `system`-scoped key is refused too: the backend routes require the
 * agent's OWN key (`User.agent_name` is populated only for agent scope), so
 * admitting `system` here would only move the 403 one hop later.
 *
 * ── Gating (plan taste T3 / ent#879 T4) ──────────────────────────────────
 * Registered in `server.ts`'s `toolGroups` → the `operatorOnly` allow-list,
 * ALWAYS. There is deliberately no env gate and no dynamic registration: the
 * demo flips the AAuth switches (`aauth_enabled` + `aauth_prototype_enabled`,
 * ent#862) in `system_settings` at runtime, which the MCP server cannot see.
 * With a switch off the backend answers 404 and
 * both tools report `aauth_disabled: true` — advertised but inert, the
 * `call_a2a_agent` precedent. The backend 404 is the authority.
 *
 * ── Binding hijack (plan T7, stated limit) ────────────────────────────────
 * The person server binds this agent to whoever approves FIRST. The link is
 * relayed by the agent in whatever channel it is in, so the descriptions tell
 * the agent to relay it only to its owner, privately, and the audit row
 * records the execution and trigger source. Owner-only delivery is the
 * productization fix (DEBT_INBOX / #558 task G).
 */

import { z } from "zod";
import { TrinityClient, ApiError } from "../client.js";
import type { McpAuthContext } from "../types.js";
import { resolveExecutionId } from "./execution_id.js";
import { accessDenied } from "../access.js";

const HANDLE_RE = /^[A-Za-z0-9_-]{32}$/;
// pchar without `%` (the backend signs `@path` byte-for-byte as sent); no query / fragment.
const PATH_RE = /^\/[A-Za-z0-9._~!$&'()*+,;=:@/-]*$/;

/** Refusal reasons the agent MAY try again later (everything else is `do_not_retry`). */
const RETRYABLE_REFUSALS = new Set([
  "rate_limited", "state_unavailable", "ps_unreachable", "resource_unreachable", "unreachable",
  "effect_in_progress", "timeout",
]);

export function createAAuthTools(client: TrinityClient, requireApiKey: boolean) {
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

  /** The acting agent: agent scope only (see the module header). */
  const actingAgent = (
    tool: string,
    authContext: McpAuthContext | undefined,
  ): { agent?: string; reason?: string } => {
    if (authContext?.scope === "agent" && authContext.agentName) {
      return { agent: authContext.agentName };
    }
    const scope = authContext?.scope;
    return {
      reason:
        `${tool} acts as the calling agent, so it needs an agent-scoped key ` +
        `(this key is ${scope ? `'${scope}'-scoped` : "unscoped"}). The backend signs ONLY ` +
        "for the agent whose own key makes the request — a person or system key has no agent " +
        "identity to bind.",
    };
  };

  const detailOf = (error: unknown): Record<string, unknown> | undefined => {
    if (!(error instanceof ApiError)) return undefined;
    try {
      const parsed = JSON.parse(error.body) as unknown;
      const root = (parsed as { detail?: unknown })?.detail ?? parsed;
      return root && typeof root === "object" && !Array.isArray(root)
        ? (root as Record<string, unknown>)
        : undefined;
    } catch {
      return undefined;
    }
  };

  /** Errors → honest structured flags. The tool never throws. */
  const flagsOf = (error: unknown): Record<string, unknown> => {
    const message = error instanceof Error ? error.message : String(error);
    const flags: Record<string, unknown> = {};
    const status = error instanceof ApiError ? error.status : undefined;
    const detail = detailOf(error);
    const reason = typeof detail?.reason === "string" ? detail.reason : undefined;
    if (reason) flags.reason = reason;
    if (typeof detail?.message === "string") flags.message = detail.message;

    if (status === 404) {
      // T3: the backend 404 is "AAuth is off (or this instance has no issuer)".
      flags.not_found = true;
      flags.aauth_disabled = true;
    }
    if (status === 403) {
      flags.not_authorized = true;
      if (reason === "aauth_agent_only") flags.agent_only = true;
      if (reason === "resource_not_allowed") flags.resource_not_allowed = true;
    }
    if (status === 402) flags.payment_required = true;
    if (status === 409) {
      if (reason === "effect_in_progress") flags.in_progress = true;
      else flags.person_server_unconfigured = true;
    }
    if (status === 422) flags.invalid = true;
    if (status === 429) flags.rate_limited = true;
    if (status === 502) {
      // "the peer did something we refused or could not use" — for the person-token
      // tool the peer is always the person server (the ent#878 flag); for a resource
      // call it may be the resource, so the generic flag is set too.
      flags.peer_unusable = true;
      if (!reason || reason.startsWith("person_server")) flags.person_server_unreachable = true;
    }
    if (status === 503) flags.state_unavailable = true;
    if (status === 504) flags.timeout = true;
    if (!status && /abort/i.test(message)) {
      // The gateway deadline hit first; the backend may still have minted a
      // code (person token) or sent the call (resource). Call again (without a
      // handle): a pending record is reused, a completed call is replayed.
      flags.timeout = true;
      flags.possibly_pending = true;
    }
    return { error: message, ...flags };
  };

  const fail = (error: unknown): string =>
    JSON.stringify({ success: false, ...flagsOf(error) }, null, 2);

  /** ent#879: a refusal is an OUTCOME the agent reads, with the actor named in `next_step`. */
  const failResource = (error: unknown): string => {
    const flags = flagsOf(error);
    const detail = detailOf(error) ?? {};
    const reason = typeof flags.reason === "string" ? flags.reason : undefined;
    const possiblyExecuted = detail.possibly_executed === true;
    const refusal: Record<string, unknown> = {
      reason: reason ?? (flags.aauth_disabled ? "aauth_disabled" : "unknown"),
      message: flags.message ?? flags.error,
    };
    if (error instanceof ApiError) refusal.http_status = error.status;
    for (const k of ["resource", "resource_status", "retry_after", "challenges", "budget"]) {
      if (detail[k] !== undefined) refusal[k] = detail[k];
    }
    if (possiblyExecuted) refusal.possibly_executed = true;
    const doNotRetry = possiblyExecuted || !(reason ? RETRYABLE_REFUSALS.has(reason) : flags.possibly_pending === true);
    let next_step: string;
    if (flags.aauth_disabled) {
      next_step = "The AAuth prototype is off on this instance (or it has no issuer). Ask your operator; nothing you do will change it.";
    } else if (flags.agent_only) {
      next_step = "Only the agent's OWN key may call a resource as that agent. Do not route around this.";
    } else if (flags.resource_not_allowed) {
      next_step = "This resource is not on the instance allow-list (`aauth_resource_allowlist`). Ask your operator to list it; do not try another URL for the same resource.";
    } else if (possiblyExecuted) {
      next_step = "The call timed out AFTER the request was sent, so the resource may have executed it and charged for it. Do NOT re-send blindly; tell your owner and let them decide.";
    } else if (flags.payment_required) {
      next_step = "The resource wants payment for this call; the platform does not pay on your behalf. Tell your owner.";
    } else if (flags.rate_limited) {
      next_step = "A rate budget is exhausted; wait `retry_after` seconds (or a minute) before calling again.";
    } else if (flags.in_progress) {
      next_step = "The same labelled call is already running in this execution; wait for it instead of re-sending.";
    } else if (doNotRetry) {
      next_step = "The platform refused this call for the stated reason; do not retry on your own, tell your operator.";
    } else {
      next_step = "A transient failure; you may try once more after a short wait.";
    }
    return JSON.stringify({ success: false, outcome: "refusal", refusal, do_not_retry: doNotRetry, next_step, ...flags }, null, 2);
  };

  return {
    // ========================================================================
    request_person_token: {
      name: "request_person_token",
      description:
        "Obtain a PERSON TOKEN for a resource as yourself (AAuth), so the resource can tell whose " +
        "agent you are. The platform signs the request with your agent identity, holds the token, " +
        "and never shows it to you — you get its metadata (jti/aud/sub/exp). Outcomes: `issued` — " +
        "done, nothing to relay; `pending` — your OWNER must approve the binding once at the " +
        "person server: relay `interaction.link` (or url + code) PRIVATELY to your owner in this " +
        "conversation and nowhere else — whoever approves first becomes the person you act for — " +
        "and if no person is in this conversation, hand it to your owner through ask_operator " +
        "instead; then call again with `pending_handle` after `interaction.retry_after` seconds " +
        "until the outcome changes; `refused` — read `reason` and `next_step` (denied/expired/gone " +
        "are final: do not retry on your own). `resource` is an https host, e.g. " +
        "https://resource.example. Acts as the calling agent (agent-scoped key only). " +
        "`aauth_disabled` means the AAuth prototype is off on this instance — ask your operator.",
      parameters: z.object({
        resource: z.string().min(9).max(2048).describe(
          "The resource the token is for: an https server identifier (host only, no path).",
        ),
        pending_handle: z.string().regex(HANDLE_RE).optional().describe(
          "Resume a pending binding: the `interaction.pending_handle` from an earlier call.",
        ),
        wait: z.number().int().min(0).max(15).optional().describe(
          "Seconds the person server may hold the request open (default 5, 15 on resume).",
        ),
        execution_id: z.string().max(200).optional().describe(
          "Your current execution id, for the audit row. Normally supplied automatically.",
        ),
      }),
      execute: async (
        params: { resource: string; pending_handle?: string; wait?: number; execution_id?: string },
        context?: { session?: McpAuthContext },
      ) => {
        const acting = actingAgent("request_person_token", context?.session);
        if (!acting.agent) {
          return accessDenied(
            context,
            { success: false, error: "Access denied", reason: acting.reason, not_authorized: true },
          );
        }
        try {
          const result = await getClient(context?.session).requestPersonToken(acting.agent, {
            resource: params.resource,
            pending_handle: params.pending_handle,
            wait: params.wait,
            execution_id: resolveExecutionId(context?.session, params.execution_id),
            trigger_source: "mcp",
          });
          // The backend model has no token field; strip defensively anyway so a
          // future field named like one can never reach the agent through here.
          const { person_token: _pt, token: _t, ...safe } = (result ?? {}) as Record<string, unknown>;
          return JSON.stringify({ ...safe, agent: acting.agent }, null, 2);
        } catch (e) {
          return fail(e);
        }
      },
    },

    // ========================================================================
    call_aauth_resource: {
      name: "call_aauth_resource",
      description:
        "Call an allow-listed AAuth RESOURCE as yourself: `{resource}{path}` with `method` GET or POST " +
        "and a JSON `body`. The platform signs with your agent identity, climbs the resource's " +
        "requirement ladder for you (agent token → your owner's person token → an auth token minted " +
        "by the person server), holds every token and shows you none — `token_kind` says which was " +
        "used, `challenges` which rungs the resource demanded. Outcomes: `result` — `status`, `body` " +
        "(the resource's answer, sanitised, capped) and `budget` (cost/balance/unit, when the resource " +
        "reports one); a later call with a NEW `dedup_label` reuses the held auth token with no new " +
        "consent. `interaction_required` — your OWNER must consent at the person server: relay " +
        "`interaction.link` (or url + code) PRIVATELY to your owner in this conversation and nowhere " +
        "else (whoever approves first becomes the person you act for; use ask_operator when no " +
        "person is in this conversation), then call again with ONLY `pending_handle` after " +
        "`interaction.retry_after` seconds — the call you asked for is re-sent automatically once " +
        "consent arrives. `pending` — approval or a deferred answer is still in progress: call again " +
        "with `pending_handle`. `denied` / `expired` / `refusal` — read `reason`, `next_step` and " +
        "`do_not_retry`; never retry a denial or route around a refusal (`possibly_executed` means " +
        "the resource may already have run and charged for the call — tell your owner). " +
        "`dedup_label` is REQUIRED on a new call and names it within your execution (two different " +
        "questions need two labels; the same label replays the first answer). `justification` is " +
        "shown to the person. Acts as the calling agent (agent-scoped key only). `aauth_disabled` " +
        "means the AAuth prototype is off on this instance — ask your operator.",
      parameters: z.object({
        resource: z.string().min(9).max(2048).optional().describe(
          "The AAuth resource: https://<host> (host only; must be on the instance allow-list).",
        ),
        method: z.enum(["GET", "POST"]).optional().describe("GET or POST (default POST)."),
        path: z.string().min(1).max(512).regex(PATH_RE).optional().describe(
          "Absolute, unencoded resource path, e.g. /v1/search_entities (no query, no `..`).",
        ),
        body: z.record(z.string(), z.unknown()).nullable().optional().describe(
          "JSON object sent as the POST body (ignored for GET).",
        ),
        justification: z.string().max(500).optional().describe(
          "Why you need this, in one or two sentences — the person sees it when asked to consent.",
        ),
        dedup_label: z.string().min(1).max(200).optional().describe(
          "REQUIRED on a new call: a label naming this call within your execution (e.g. 'sanctions-check-acme').",
        ),
        pending_handle: z.string().regex(HANDLE_RE).optional().describe(
          "Resume a pending call: the `interaction.pending_handle` from an earlier answer — send it ALONE.",
        ),
        wait: z.number().int().min(0).max(15).optional().describe(
          "On resume: seconds the person server may hold the poll open (default 15).",
        ),
        execution_id: z.string().max(200).optional().describe(
          "Your current execution id, for the audit row and effect dedup. Normally supplied automatically.",
        ),
      }).superRefine((v, ctx) => {
        const callFields = ["resource", "method", "path", "body", "justification", "dedup_label"] as const;
        const present = callFields.filter((k) => v[k] !== undefined);
        if (v.pending_handle !== undefined) {
          if (present.length > 0) {
            ctx.addIssue({
              code: z.ZodIssueCode.custom,
              message: `pending_handle resumes the STORED call — do not send ${present.join(", ")} with it`,
            });
          }
          return;
        }
        if (v.wait !== undefined) {
          ctx.addIssue({ code: z.ZodIssueCode.custom, message: "wait applies to a resume (pending_handle) only" });
        }
        for (const k of ["resource", "path", "dedup_label"] as const) {
          if (v[k] === undefined) {
            ctx.addIssue({ code: z.ZodIssueCode.custom, message: `${k} is required on a new call` });
          }
        }
      }),
      execute: async (
        params: {
          resource?: string; method?: "GET" | "POST"; path?: string; body?: Record<string, unknown> | null;
          justification?: string; dedup_label?: string; pending_handle?: string; wait?: number; execution_id?: string;
        },
        context?: { session?: McpAuthContext },
      ) => {
        const acting = actingAgent("call_aauth_resource", context?.session);
        if (!acting.agent) {
          return accessDenied(
            context,
            { success: false, error: "Access denied", reason: acting.reason, not_authorized: true },
          );
        }
        const execution_id = resolveExecutionId(context?.session, params.execution_id);
        const request: Record<string, unknown> = params.pending_handle !== undefined
          ? { pending_handle: params.pending_handle, wait: params.wait, execution_id, trigger_source: "mcp" }
          : {
              resource: params.resource,
              method: params.method ?? "POST",
              path: params.path,
              body: params.body ?? null,
              justification: params.justification,
              dedup_label: params.dedup_label,
              execution_id,
              trigger_source: "mcp",
            };
        try {
          const result = await getClient(context?.session).callAAuthResource(acting.agent, request);
          // No backend field carries a token; strip anything named like one anyway.
          const { token: _t, auth_token: _a, person_token: _p, resource_token: _r, ...safe } =
            (result ?? {}) as Record<string, unknown>;
          return JSON.stringify({ ...safe, agent: acting.agent }, null, 2);
        } catch (e) {
          return failResource(e);
        }
      },
    },
  };
}
