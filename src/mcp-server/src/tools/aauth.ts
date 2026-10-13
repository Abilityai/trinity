/**
 * AAuth person token (trinity-enterprise#878) — `request_person_token`.
 *
 * An agent obtains a person token for a resource from its owner's person
 * server, as itself, after its owner has bound it once through the person
 * server's link-and-code flow. The backend does the signing, the caching and
 * the holding; this tool relays the OUTCOME — token metadata, or the link +
 * code the agent must hand to its owner — and never the token itself (the
 * backend response has no field for it; a defensive strip here is belt and
 * braces, not the mechanism).
 *
 * ── Self-acting, agent scope only ────────────────────────────────────────
 * There is no `agent_name` parameter: the acting agent comes from the key
 * (`resolveActingAgent`, the ask_operator shape). Unlike the other self-acting
 * tools, a `system`-scoped key is refused too: the backend route requires the
 * agent's OWN key (`User.agent_name` is populated only for agent scope), so
 * admitting `system` here would only move the 403 one hop later.
 *
 * ── Gating (plan taste T3) ────────────────────────────────────────────────
 * Registered in `server.ts`'s `toolGroups` → the `operatorOnly` allow-list,
 * ALWAYS. There is deliberately no env gate and no dynamic registration: the
 * demo flips `aauth_prototype_enabled` in `system_settings` at runtime, which
 * the MCP server cannot see. With the flag off the backend answers 404 and
 * this tool reports `aauth_disabled: true` — advertised but inert, the
 * `call_a2a_agent` precedent. The backend 404 is the authority.
 *
 * ── Binding hijack (plan T7, stated limit) ────────────────────────────────
 * The person server binds this agent to whoever approves FIRST. The link is
 * relayed by the agent in whatever channel it is in, so the description tells
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
    authContext: McpAuthContext | undefined,
  ): { agent?: string; reason?: string } => {
    if (authContext?.scope === "agent" && authContext.agentName) {
      return { agent: authContext.agentName };
    }
    const scope = authContext?.scope;
    return {
      reason:
        "request_person_token acts as the calling agent, so it needs an agent-scoped key " +
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
  const fail = (error: unknown): string => {
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
    }
    if (status === 409) flags.person_server_unconfigured = true;
    if (status === 422) flags.invalid = true;
    if (status === 429) flags.rate_limited = true;
    if (status === 502) flags.person_server_unreachable = true;
    if (status === 503) flags.state_unavailable = true;
    if (status === 504) flags.timeout = true;
    if (!status && /abort/i.test(message)) {
      // The gateway deadline hit first; the backend may still have minted a
      // code. Call again (without a handle): a pending record is reused.
      flags.timeout = true;
      flags.possibly_pending = true;
    }
    return JSON.stringify({ success: false, error: message, ...flags }, null, 2);
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
        const acting = actingAgent(context?.session);
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
  };
}
