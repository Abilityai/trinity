/**
 * System credential vault runtime tools (trinity-enterprise#279) — the
 * agent-facing half of a governed platform-level credential store.
 *
 * An operator adds AES-256-GCM-encrypted named entries and grants them to
 * agents deny-by-default; an agent then DISCOVERS its granted names and FETCHES
 * a value by name at runtime, instead of only receiving credentials via static
 * file injection at container start (CRED-002). The fetched value is delivered
 * live and scrubbed from the agent's persisted transcript by the OSS
 * runtime-secret-scrub seam — the plaintext never lands in an execution log,
 * tool-call record, or idempotency snapshot.
 *
 * ── License-blind by design ────────────────────────────────────────────────
 * This module is OSS-core and knows nothing about entitlement. It proxies the
 * enterprise routes and reports whatever they say: a 404 (route absent, an
 * OSS-only build) and an unentitled 403 (the module mounted but not licensed)
 * are DIFFERENT operator situations, and the tool degrades honestly for each
 * rather than pretending the feature does not exist.
 *
 * ── Gating ────────────────────────────────────────────────────────────────
 * Advertisement: registered in `server.ts`'s `toolGroups`, i.e. the
 * `operatorOnly` ALLOW-list `{user, agent, system}`. Connector and anonymous
 * sessions never see these tools and cannot call them (a filtered-out tool is
 * absent from the call map — `MethodNotFound` on a direct call).
 *
 * Per-call: the backend fetch/discovery routes are agent-scoped-key-only (a
 * LITERAL `mcp_scope == "agent"` check). A user- or system-scoped key reaching
 * these tools gets a 403 `agent_key_required`, which the tool surfaces AS ITSELF
 * — a human operator on a user key must never be told an entitled feature does
 * not exist (D25). There is deliberately no client-side agent-name gate: the
 * value is bound to the calling key's own agent by the backend, so there is no
 * cross-agent target to guard (unlike the outbound A2A tools).
 */

import { z } from "zod";
import { TrinityClient, ApiError } from "../client.js";
import type { McpAuthContext } from "../types.js";

/**
 * The backend maps every refusal to `HTTPException(status, detail=…)`. For a
 * VaultError / the agent-key gate `detail` is a `{code, message}` DICT; for the
 * entitlement gate it is a plain STRING. FastAPI serializes both as
 * `{"detail": …}`, and `ApiError.message` keeps the raw body after the
 * `API error (<status>): ` prefix — so the tool can branch on the detail SHAPE
 * (dict-with-code vs string) rather than on status alone, which is what tells an
 * unlicensed build apart from an ungranted name (both are 403).
 */
function parseVaultError(error: unknown): {
  status?: number;
  code?: string;
  message: string;
  detailIsString: boolean;
} {
  const rawMessage = error instanceof Error ? error.message : String(error);
  const status = error instanceof ApiError ? error.status : undefined;

  const m = rawMessage.match(/^API error \(\d+\): ([\s\S]*)$/);
  let code: string | undefined;
  let detailIsString = false;
  let message = rawMessage;

  if (m) {
    try {
      const detail = (JSON.parse(m[1]) as { detail?: unknown })?.detail;
      if (detail && typeof detail === "object" && !Array.isArray(detail)) {
        const d = detail as { code?: unknown; message?: unknown };
        if (typeof d.code === "string") code = d.code;
        if (typeof d.message === "string") message = d.message;
      } else if (typeof detail === "string") {
        detailIsString = true;
        message = detail;
      }
    } catch {
      // Body wasn't JSON — keep the raw ApiError message.
    }
  }

  return { status, code, message, detailIsString };
}

export function createCredentialVaultTools(
  client: TrinityClient,
  requireApiKey: boolean,
) {
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

  /**
   * Discovery degrades to the `enabled:false` shape (the `list_runnable_skills`
   * contract) rather than throwing — an agent must be able to reason about "why
   * can't I see any credentials?". Branches on the refusal so the message is
   * actionable: agent-key-required is a "call me from an agent" hint (D25), an
   * unentitled 403 is a licensing message, a 404 is an OSS build with no vault.
   */
  const degradeList = (error: unknown): string => {
    const { status, code, message, detailIsString } = parseVaultError(error);
    let human: string;
    if (status === 404) {
      human = "The credential vault is not available on this platform.";
    } else if (status === 403 && code === "agent_key_required") {
      human =
        "These tools act as an agent; call them from an agent context (an agent-scoped key).";
    } else if (status === 403 && detailIsString) {
      human = "The credential vault is not licensed for this instance.";
    } else if (status === 403 && code === "vault_not_granted") {
      // Unlikely on /available (it never 403s a granted-set), but honest.
      human = message;
    } else {
      human = "The credential vault could not be reached.";
    }
    return JSON.stringify(
      {
        enabled: false,
        count: 0,
        credentials: [],
        message: human,
        detail: message,
      },
      null,
      2,
    );
  };

  /**
   * Fetch errors → honest structured flags (the outbound-A2A `fail()` shape:
   * `{success:false, error, ...flags}`, where the flags ARE the structure). The
   * tool NEVER throws — a thrown error reaches the agent as an opaque transport
   * failure it cannot reason about.
   */
  const failFetch = (error: unknown): string => {
    const { status, code, message, detailIsString } = parseVaultError(error);
    const flags: Record<string, unknown> = {};

    if (status === 403) {
      if (detailIsString) {
        // requires_entitlement → plain-string detail: unlicensed build.
        flags.not_entitled = true;
      } else if (code === "vault_not_granted") {
        // Deny-by-default; unknown ≡ ungranted (no existence oracle).
        flags.not_granted = true;
      } else if (code === "agent_key_required") {
        // A non-agent principal reached the tool — surface it AS ITSELF (D25).
        flags.agent_key_required = true;
      } else if (code === "vault_approval_required") {
        flags.approval_required = true;
      } else if (code) {
        flags.code = code;
      } else {
        flags.not_authorized = true;
      }
    } else if (status === 404) {
      // Route absent — OSS build with no vault at all.
      flags.not_available = true;
    } else if (status === 429) {
      flags.rate_limited = true;
      flags.retry_hint = "Too many credential fetches; wait a moment and retry.";
    } else if (status === 503) {
      // Three named degraded shapes, each a different recovery.
      if (code === "vault_staging_unavailable") {
        flags.staging_unavailable = true;
        flags.retryable = true;
      } else if (code === "vault_decrypt_failed") {
        flags.decrypt_failed = true;
        flags.admin_action_required = true; // an admin must run vault rewrap
      } else if (code === "vault_backend_unavailable") {
        flags.backend_unavailable = true;
        flags.retryable = true;
      } else {
        flags.unavailable = true;
        flags.retryable = true;
      }
    }

    return JSON.stringify({ success: false, error: message, ...flags }, null, 2);
  };

  return {
    // ========================================================================
    list_available_credentials: {
      name: "list_available_credentials",
      description:
        "List the platform-vault credentials this agent has been granted. Returns names, " +
        "descriptions and kinds only — NEVER a value. Use fetch_credential to obtain the value " +
        "of one by name. This is your own granted set (deny-by-default); an operator decides " +
        "which credentials each agent may read. An empty list means nothing has been granted to " +
        "you yet.",
      parameters: z.object({}),
      execute: async (
        _params: unknown,
        context?: { session?: McpAuthContext },
      ) => {
        try {
          const credentials = await getClient(
            context?.session,
          ).getAvailableCredentials();
          return JSON.stringify(
            {
              enabled: true,
              count: credentials.length,
              credentials,
            },
            null,
            2,
          );
        } catch (e) {
          return degradeList(e);
        }
      },
    },

    // ========================================================================
    fetch_credential: {
      name: "fetch_credential",
      description:
        "Fetch the plaintext VALUE of a vault credential you have been granted, by name. " +
        "Deny-by-default: an ungranted or unknown name returns { not_granted: true } — there is " +
        "no way to discover names you were not granted, so call list_available_credentials first. " +
        "The value is delivered live and is automatically scrubbed from your saved transcript; " +
        "use it for the task at hand and do NOT echo it back into your output. Pass your current " +
        "execution_id if you have one — it is audit context only and never changes the result.",
      parameters: z.object({
        name: z
          .string()
          .min(1)
          .max(128)
          .describe(
            "The credential name, exactly as returned by list_available_credentials.",
          ),
        execution_id: z
          .string()
          .max(128)
          .optional()
          .describe(
            "Your current execution id, if you have one. Audit context; never a refusal.",
          ),
      }),
      execute: async (
        params: { name: string; execution_id?: string },
        context?: { session?: McpAuthContext },
      ) => {
        try {
          const result = await getClient(context?.session).fetchCredential({
            name: params.name,
            execution_id: params.execution_id,
          });
          return JSON.stringify({ success: true, ...(result as object) }, null, 2);
        } catch (e) {
          return failFetch(e);
        }
      },
    },
  };
}
