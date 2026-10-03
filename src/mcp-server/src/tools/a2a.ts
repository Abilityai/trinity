/**
 * A2A Control Tools (trinity-enterprise#160, abilityai/trinity-enterprise#761)
 *
 * The MCP surface for the A2A interoperability *management* plane — the third
 * surface (Invariant #13) alongside the backend routers it proxies and the
 * config UI (#158). Distinct from the runtime outbound call (`call_a2a_agent`,
 * abilityai/trinity#736): these tools toggle exposure, read the served card,
 * manage the inbound allow-list, and manage the outbound endpoint registry.
 *
 * ── Two planes, two backends, two error mappers ───────────────────────────
 * INBOUND (exposure, the inbound identity allow-list, the full config read)
 * proxies `/api/enterprise/a2a/*`, which is entitlement-gated: a 403 in an
 * unentitled build, a 404 in an OSS-only build. Those four tools use `fail()`,
 * which reports a structured `{ success: false, not_entitled: true }` rather
 * than a silent success.
 *
 * OUTBOUND control (`register_a2a_endpoint` / `list_a2a_endpoints` /
 * `remove_a2a_endpoint`) is available in every edition by ruling, so those
 * three address the OSS settings routes over the platform-wide outbound
 * endpoint store (#736) — the same store the runtime call resolves against.
 * They use `failOutbound()`, on which `not_entitled` is structurally
 * unreachable: there is no entitlement to report, and telling an operator to
 * buy a licence for an auth error is worse than returning the raw status.
 *
 * ── Gating ───────────────────────────────────────────────────────────────
 *  - every mutating route (inbound and outbound) is human-only
 *    (`reject_agent_principal`) — an agent-scoped key gets a 403. The outbound
 *    routes are additionally admin-tier: the registry is platform-wide, so a
 *    write grants a credentialed egress target to every agent on the instance
 *    (Invariant #8's grant-vs-use line).
 *  - outbound credentials are write-only: accepted on register, never returned
 *    by any read (only `has_credentials`), so no tool echoes a secret back.
 *
 * Agent-to-agent gating on the reads (#736 F8). `get_agent_a2a_config` keeps
 * the `{self} ∪ permitted` check: the backend resolves an agent-scoped key to
 * its OWNER and checks owner access, so on a single-owner install any agent
 * could otherwise enumerate a SIBLING agent's A2A config — the shape of that
 * fleet's integrations. That is a real leak rather than a cosmetic one, and
 * requirements mcp.md §32.3 already claimed the gate existed (it did not — the
 * claim was drift, and #736 corrects both halves).
 *
 * `list_a2a_endpoints` no longer carries that check: the store it reads is
 * platform-wide and its route refuses every agent principal outright, so a
 * permission lookup would deny a strict subset of what the backend denies
 * while costing a round trip. It refuses agent-scoped keys itself instead,
 * which is the honest answer and is audited as a denial.
 */

import { z } from "zod";
import { TrinityClient, ApiError } from "../client.js";
import type { McpAuthContext } from "../types.js";
import { accessDenied } from "../access.js";
import type { DenyCallContext } from "../access.js";

export function createA2ATools(client: TrinityClient, requireApiKey: boolean) {
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

  /** Uniform error → structured result with honest gating flags (never throws). */
  const fail = (error: unknown): string => {
    const message = error instanceof Error ? error.message : String(error);
    const flags: Record<string, boolean> = {};
    // Unentitled build (403 "not licensed") or OSS-only build (404, route absent).
    if (/not licensed/i.test(message) || /\ba2a\b/i.test(message) && /\b403\b/.test(message)) {
      flags.not_entitled = true;
    }
    if (/\b404\b/.test(message) && !flags.not_entitled) flags.not_found = true;
    if (/human-only/i.test(message)) flags.human_only = true;
    if (/\b403\b/.test(message) && !flags.not_entitled && !flags.human_only) flags.not_authorized = true;
    if (/\b422\b/.test(message)) flags.invalid = true;
    return JSON.stringify({ success: false, error: message, ...flags }, null, 2);
  };

  const ok = (data: unknown): string => JSON.stringify({ success: true, ...(data as object) }, null, 2);

  /**
   * Error mapper for the three OUTBOUND control tools (ent#761).
   *
   * Branches on the HTTP status, not on the body text, and cannot emit
   * `not_entitled` at all. `fail()` above infers that flag from the body — any
   * 403 whose body happens to mention "a2a" trips it — and the routes these
   * three tools proxy carry no entitlement on any build, so the flag would
   * always be a lie: the operator would be told to buy a licence for what is
   * really an admin-tier or human-only refusal.
   *
   * A thrown value with no status (a transport failure) yields the message
   * alone, with no flags, rather than a guess.
   */
  const failOutbound = (error: unknown): string => {
    const message = error instanceof Error ? error.message : String(error);
    const status = error instanceof ApiError ? error.status : undefined;
    const flags: Record<string, boolean> = {};
    if (status === 403) {
      if (/human-only/i.test(message)) flags.human_only = true;
      else flags.not_authorized = true;
    }
    if (status === 404) {
      flags.not_found = true;
      // The route itself always exists on this build, so a 404 naming an
      // endpoint is a bad reference — distinguish it so a caller can re-list.
      if (/endpoint/i.test(message)) flags.endpoint_not_found = true;
    }
    if (status === 422 || status === 400) flags.invalid = true;
    return JSON.stringify({ success: false, error: message, ...flags }, null, 2);
  };


  /**
   * `{self} ∪ permitted` for agent-scoped keys (the `tools/git.ts` shape).
   * system → allow; user-scoped → allow (the backend already scopes them to the
   * owner's accessible agents); agent → self, or an explicitly permitted target.
   *
   * Applied to the two READS only — see the module header.
   */
  const checkAgentAccess = async (
    apiClient: TrinityClient,
    authContext: McpAuthContext | undefined,
    targetAgent: string,
  ): Promise<{ allowed: boolean; reason?: string }> => {
    if (authContext?.scope === "system") return { allowed: true };
    if (authContext?.scope !== "agent" || !authContext?.agentName) return { allowed: true };
    const caller = authContext.agentName;
    if (targetAgent === caller) return { allowed: true };
    const permitted = await apiClient.getPermittedAgents(caller);
    if (!permitted.includes(targetAgent)) {
      return {
        allowed: false,
        reason: `Agent '${caller}' does not have permission to access '${targetAgent}'`,
      };
    }
    return { allowed: true };
  };

  const denied = (context: DenyCallContext | undefined, reason?: string): string =>
    accessDenied(context, { success: false, error: "Access denied", reason, not_authorized: true });

  return {
    // ========================================================================
    get_agent_a2a_config: {
      name: "get_agent_a2a_config",
      description:
        "Get an agent's A2A (agent-to-agent) control state: whether it is exposed over A2A, " +
        "its public Agent Card URL, advertised skills, the inbound identity allow-list, and the " +
        "registered outbound endpoints (credentials are never returned — only whether each has any). " +
        "Note: `config.outbound_endpoints` is the per-agent registry managed by the agent's A2A panel; " +
        "the registry the runtime resolves an outbound call against is the platform-wide one " +
        "`list_a2a_endpoints` shows. " +
        "Enterprise feature; returns { not_entitled: true } if A2A is not licensed for this instance.",
      parameters: z.object({
        agent_name: z.string().describe("The agent whose A2A config to read."),
      }),
      execute: async (params: { agent_name: string }, context?: { session?: McpAuthContext }) => {
        const apiClient = getClient(context?.session);
        const access = await checkAgentAccess(apiClient, context?.session, params.agent_name);
        if (!access.allowed) return denied(context, access.reason);
        try {
          return ok({
            config: await apiClient.getA2AConfig(params.agent_name),
            // Signpost rather than silent divergence: the two lists answer
            // different questions and only one of them is what a call resolves.
            outbound_endpoints_note:
              "`config.outbound_endpoints` is the per-agent registry managed by the agent's A2A panel. "
              + "The registry the runtime resolves an outbound call against is the platform-wide one "
              + "`list_a2a_endpoints` shows; register there with `register_a2a_endpoint`.",
          });
        } catch (e) {
          return fail(e);
        }
      },
    },

    // ========================================================================
    set_agent_a2a_exposure: {
      name: "set_agent_a2a_exposure",
      description:
        "Toggle whether an agent is exposed over A2A (agent-to-agent interoperability). " +
        "Owner/admin and human-only — an agent-scoped key is rejected. " +
        "Enterprise feature; returns { not_entitled: true } if A2A is not licensed.",
      parameters: z.object({
        agent_name: z.string().describe("The agent to expose or unexpose."),
        enabled: z.boolean().describe("true to expose the agent over A2A, false to unexpose."),
      }),
      execute: async (params: { agent_name: string; enabled: boolean }, context?: { session?: McpAuthContext }) => {
        try {
          return ok({ config: await getClient(context?.session).setA2AExposure(params.agent_name, params.enabled) });
        } catch (e) {
          return fail(e);
        }
      },
    },

    // ========================================================================
    get_agent_a2a_card: {
      name: "get_agent_a2a_card",
      description:
        "Return the served A2A Agent Card JSON for an agent (the OSS discovery card). " +
        "Read access follows the standard agent-access gate.",
      parameters: z.object({
        agent_name: z.string().describe("The agent whose Agent Card to fetch."),
      }),
      execute: async (params: { agent_name: string }, context?: { session?: McpAuthContext }) => {
        try {
          return ok({ card: await getClient(context?.session).getA2ACard(params.agent_name) });
        } catch (e) {
          return fail(e);
        }
      },
    },

    // ========================================================================
    set_a2a_inbound_allowlist: {
      name: "set_a2a_inbound_allowlist",
      description:
        "Manage which external identities may task an agent over A2A inbound. Provide `add` and/or " +
        "`remove` (identity strings — e.g. a caller URL, client id, or key id). Owner/admin and human-only. " +
        "Enterprise feature; returns { not_entitled: true } if A2A is not licensed.",
      parameters: z.object({
        agent_name: z.string().describe("The agent whose inbound allow-list to modify."),
        add: z.array(z.string()).optional().describe("Identities to add to the allow-list."),
        remove: z.array(z.string()).optional().describe("Identities to remove from the allow-list."),
      }),
      execute: async (
        params: { agent_name: string; add?: string[]; remove?: string[] },
        context?: { session?: McpAuthContext },
      ) => {
        if (!params.add?.length && !params.remove?.length) {
          return JSON.stringify(
            { success: false, error: "Provide at least one identity in 'add' or 'remove'.", invalid: true },
            null, 2,
          );
        }
        try {
          const config = await getClient(context?.session).updateA2AInboundAllowlist(params.agent_name, {
            add: params.add,
            remove: params.remove,
          });
          return ok({ config });
        } catch (e) {
          return fail(e);
        }
      },
    },

    // ========================================================================
    register_a2a_endpoint: {
      name: "register_a2a_endpoint",
      description:
        "Register (or update by name) an outbound external A2A endpoint — this is the platform-wide " +
        "registry the runtime call_a2a_agent resolves against (abilityai/trinity#736), so every agent " +
        "on the instance may call what you register here. Optional `credentials` are stored encrypted " +
        "and NEVER returned by any read; `clear_credentials: true` removes a stored one. " +
        "`credential_kind` says what the credential IS: pass 'payment_token' for an x402 token bought " +
        "after a `payment_required` refusal, so it rides as payment (x402 metadata plus the " +
        "`payment-signature` header) in addition to the Bearer header every credentialed call carries. " +
        "Omit it and the kind is inferred from the value; the response reports what was stored. " +
        "Admin and human-only — registering an endpoint decides where a credentialed server-side " +
        "request may go, so an agent-scoped key is refused. " +
        "Outbound calling also has its own switch: the response reports `outbound_enabled`.",
      parameters: z.object({
        agent_name: z.string().optional().describe(
          "Ignored on this build: the outbound registry is platform-wide, not per agent. "
          + "Accepted so existing callers keep working — every agent on the instance may call every registered endpoint.",
        ),
        name: z.string().min(1).max(200).describe("Operator-facing label for the endpoint (unique on this instance)."),
        url: z.string().describe("The external A2A endpoint / Agent Card URL (https)."),
        credentials: z.string().max(8192).optional().describe(
          "Optional secret (token/API key) for calling the endpoint. Stored encrypted; never echoed back.",
        ),
        clear_credentials: z.boolean().optional().describe(
          "Remove the stored secret for this endpoint. Cannot be combined with `credentials`.",
        ),
        credential_kind: z.enum(["api_key", "payment_token"]).optional().describe(
          "What the credential is: 'payment_token' for an x402 payment token (attached as payment in "
          + "addition to the Bearer header), 'api_key' for an ordinary secret (Authorization: Bearer "
          + "only). Omit to let the platform infer "
          + "it from the value. Send it alone to re-label a credential already stored.",
        ),
      }),
      execute: async (
        params: {
          agent_name?: string;
          name: string;
          url: string;
          credentials?: string;
          clear_credentials?: boolean;
          credential_kind?: "api_key" | "payment_token";
        },
        context?: { session?: McpAuthContext },
      ) => {
        if (params.clear_credentials && params.credential_kind) {
          // Contradictory instructions about one slot (#3185). Refused here as
          // well as at the route, so the caller learns it without spending a
          // round trip — and is never told a payment token is registered when
          // the clear emptied the slot.
          return JSON.stringify(
            {
              success: false,
              error:
                "Pass either `credential_kind` or `clear_credentials: true`, not both — "
                + "clearing the credential also drops the kind that described it.",
              invalid: true,
            },
            null, 2,
          );
        }
        if (params.clear_credentials && params.credentials) {
          // The store honours the clear and DROPS the supplied secret, so the
          // caller would be left believing a credential is stored.
          return JSON.stringify(
            {
              success: false,
              error:
                "Pass either `credentials` or `clear_credentials: true`, not both — "
                + "the supplied secret would be dropped.",
              invalid: true,
            },
            null, 2,
          );
        }
        try {
          const result = await getClient(context?.session).registerA2AEndpoint({
            name: params.name,
            url: params.url,
            credentials: params.credentials,
            clear_credentials: params.clear_credentials,
            credential_kind: params.credential_kind,
          });
          return ok({
            endpoint: result?.endpoint,
            outbound_enabled: result?.enabled,
            // The store's single-use warning, relayed verbatim: an x402 v3 token
            // authorises ONE settlement, so without this the second call reads
            // as a mystery 402 on an endpoint that just worked.
            ...(result?.hint ? { credential_hint: result.hint } : {}),
            // Honest status: a registered endpoint is still uncallable while the
            // switch is off, and that is one admin step away.
            ...(result?.enabled === false
              ? {
                  hint:
                    "Outbound calls are off: an admin sets a2a_outbound_enabled "
                    + "(PUT /api/settings/a2a_outbound_enabled) or A2A_OUTBOUND_ENABLED=true",
                }
              : {}),
          });
        } catch (e) {
          return failOutbound(e);
        }
      },
    },

    // ========================================================================
    list_a2a_endpoints: {
      name: "list_a2a_endpoints",
      description:
        "List the registered outbound A2A endpoints — the platform-wide registry call_a2a_agent " +
        "resolves against (credentials are never returned, only has_credentials) — and report whether " +
        "outbound calling is switched on. Admin and human-only; an agent-scoped key is refused, " +
        "because the registered URLs are the shape of the fleet's integrations.",
      parameters: z.object({
        agent_name: z.string().optional().describe(
          "Ignored on this build: the outbound registry is platform-wide, not per agent. "
          + "Accepted so existing callers keep working — every agent on the instance may call every registered endpoint.",
        ),
      }),
      execute: async (params: { agent_name?: string }, context?: { session?: McpAuthContext }) => {
        // Refused here rather than at the backend: the route is human-only, so
        // a `{self} ∪ permitted` lookup would deny a strict subset of what the
        // backend denies while costing a round trip — an inert check is worse
        // than none, because the next reader believes it does something.
        if (context?.session?.scope === "agent") {
          return accessDenied(context, {
            success: false,
            error: "This operation is human-only; agent-scoped keys cannot read the outbound registry.",
            human_only: true,
          });
        }
        try {
          const result = await getClient(context?.session).listA2AEndpoints();
          return ok({ endpoints: result?.endpoints ?? [], outbound_enabled: result?.enabled });
        } catch (e) {
          return failOutbound(e);
        }
      },
    },

    // ========================================================================
    remove_a2a_endpoint: {
      name: "remove_a2a_endpoint",
      description:
        "Remove one endpoint from the platform-wide outbound A2A registry. Admin and human-only; " +
        "removing an endpoint makes every agent's call_a2a_agent on that name fail with " +
        "endpoint_not_found.",
      parameters: z.object({
        agent_name: z.string().optional().describe(
          "Ignored on this build: the outbound registry is platform-wide, not per agent. "
          + "Accepted so existing callers keep working — every agent on the instance may call every registered endpoint.",
        ),
        endpoint_id: z.string().min(1).describe(
          "id or name — pass the id when in doubt; first match wins across id and name "
          + "(both come from list_a2a_endpoints).",
        ),
      }),
      execute: async (
        params: { agent_name?: string; endpoint_id: string },
        context?: { session?: McpAuthContext },
      ) => {
        try {
          const result = await getClient(context?.session).removeA2AEndpoint(params.endpoint_id);
          return ok({ removed: result?.removed ?? params.endpoint_id });
        } catch (e) {
          return failOutbound(e);
        }
      },
    },
  };
}
