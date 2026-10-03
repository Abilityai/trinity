/**
 * A2A runtime tools (abilityai/trinity#736) — the OUTBOUND call.
 *
 * A Trinity agent tasks an external A2A agent (Google ADK, LangChain, AWS
 * Bedrock, a remote Trinity) and gets the answer back in the same tool call.
 *
 * Deliberately a separate module from `tools/a2a.ts`, which scopes itself in
 * its own header to the *management* plane and whose `fail()` helper is built
 * around entitlement flags. Folding the runtime call in there would mix an
 * OSS-core surface into a module that reports `not_entitled`, and the first
 * confusing bug report would be an operator told their outbound call needs a
 * licence when the kill switch is simply off. (That module's three OUTBOUND
 * control tools are OSS-core too and use their own status-based mapper, for the
 * same reason — see its header.)
 *
 * ── The target is a NAME, never a URL ─────────────────────────────────────
 * `endpoint` is a reference into an operator-registered list, resolved by the
 * backend. That is not an ergonomic choice: an agent's tool arguments are
 * LLM-generated and prompt-injectable, so a URL parameter would make any
 * document the agent reads a lever on a credentialed, server-side request from
 * inside the platform network. The issue's filed AC asked for
 * `call_a2a_agent(agent_card_url, …)`; it is rejected, and `register_a2a_endpoint`
 * has described itself as feeding this tool since it shipped — the design
 * always intended a registry.
 *
 * ── Gating ────────────────────────────────────────────────────────────────
 * Advertisement: registered in `server.ts`'s `toolGroups`, i.e. the
 * `operatorOnly` ALLOW-list `{user, agent, system}`. Connector and anonymous
 * sessions never see these tools and cannot call them (`setupToolHandlers`
 * omits a filtered-out tool from the map, so a call throws MethodNotFound).
 *
 * Per-call: SELF-ONLY for `scope === "agent"`, deliberately NOT the
 * `{self} ∪ permitted` gate the other tool modules use. The backend route is
 * self-only — an agent may spend only its OWN agent's endpoint credential — so
 * a `{self} ∪ permitted` check here would deny a strict SUBSET of what the
 * backend denies: it would block nothing, while costing a `getPermittedAgents`
 * round-trip on every call. An inert check added to satisfy a documentation
 * sentence is worse than no check, because the next reader believes it does
 * something.
 */

import { z } from "zod";
import { TrinityClient, ApiError } from "../client.js";
import type { McpAuthContext } from "../types.js";
import { resolveExecutionId } from "./execution_id.js";
import { accessDenied } from "../access.js";

export function createA2ACallTools(client: TrinityClient, requireApiKey: boolean) {
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
   * Self-only for agent-scoped keys — see the module header for why this is
   * NOT `{self} ∪ permitted`. `system` and user-scoped keys pass (the backend
   * scopes them to what their owner can access).
   */
  const checkSelf = (
    authContext: McpAuthContext | undefined,
    targetAgent: string,
  ): { allowed: boolean; reason?: string } => {
    if (authContext?.scope !== "agent" || !authContext?.agentName) {
      return { allowed: true };
    }
    if (authContext.agentName === targetAgent) {
      return { allowed: true };
    }
    return {
      allowed: false,
      reason:
        `Agent '${authContext.agentName}' may only place outbound A2A calls as itself, ` +
        `not as '${targetAgent}'. Outbound calls are attributed to the calling agent — ` +
        `its rate budget, its audit row, its activity row.`,
    };
  };

  /**
   * The backend's own `detail` object, when it sent one (#3185).
   *
   * FastAPI serialises `HTTPException(detail={...})` as `{"detail": {...}}`, and
   * the 402 / 502 payment outcomes carry `{reason, message, payment?,
   * remote_status?, task_id?}` there. Read defensively, exactly like
   * `extractIdempotencyExecutionId` and `askRefusal`: this runs on an error
   * path, so a parser that throws would replace a readable refusal with an
   * opaque crash. A non-JSON body (an nginx 402 page, a proxy's text) yields
   * `undefined` and the status still speaks for itself.
   */
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

  /**
   * Errors → honest structured flags. Tools never throw; a thrown error
   * reaches the agent as an opaque transport failure it cannot reason about.
   */
  const fail = (error: unknown): string => {
    const message = error instanceof Error ? error.message : String(error);
    const flags: Record<string, unknown> = {};
    const status = error instanceof ApiError ? error.status : undefined;
    const detail = detailOf(error);
    const reason = typeof detail?.reason === "string" ? detail.reason : undefined;

    if (status === 404) {
      // Two different 404s, and telling them apart is the difference between
      // "register an endpoint" and "ask an admin to enable the feature".
      flags.not_found = true;
      if (/endpoint_not_found/i.test(message)) {
        flags.endpoint_not_found = true;
        // Retrying with a guessed name cannot succeed — the list is operator-owned.
      } else {
        flags.outbound_disabled = true;
      }
    }
    if (status === 402) {
      // The remote is priced and wants paying (#3185). NOT retryable by the
      // agent in any form: nothing it can do differs from what it just did, and
      // the platform does not buy tokens — a person does, and an admin stores
      // the token on the endpoint. So the flag an agent acts on is
      // `do_not_retry`, set whatever the body turned out to contain.
      flags.payment_required = true;
      flags.do_not_retry = true;
      if (detail?.payment !== undefined) flags.payment = detail.payment;
      if (typeof detail?.task_id === "string") flags.task_id = detail.task_id;
      if (typeof detail?.message === "string") flags.message = detail.message;
    }
    if (status === 403) flags.not_authorized = true;
    if (status === 409) flags.duplicate_in_flight = true;
    if (status === 429) flags.rate_limited = true;
    if (status === 502) {
      flags.remote_error = true;
      // One status, three different next actions. `rpc_forbidden` is the
      // remote's own 403 (ask the operator about access), `payment_rejected` is
      // a payment token it refused (the operator tops up or re-pastes), and
      // anything else is an ordinary remote failure a retry may survive.
      if (reason === "rpc_forbidden") flags.remote_forbidden = true;
      if (reason === "payment_rejected") {
        flags.payment_rejected = true;
        flags.do_not_retry = true;
      }
      if (typeof detail?.message === "string") flags.message = detail.message;
    }
    if (status === 504) flags.timeout = true;
    if (status === 400 || status === 422) flags.invalid = true;

    // #914 shape: WE aborted before the MCP gateway would have, so the
    // outbound call may well have completed on the remote. Say so — the
    // alternative is an agent that retries a side effect it already caused.
    if (!status && /abort/i.test(message)) {
      flags.timeout = true;
      flags.possibly_delivered = true;
    }

    return JSON.stringify({ success: false, error: message, ...flags }, null, 2);
  };

  return {
    // ========================================================================
    call_a2a_agent: {
      name: "call_a2a_agent",
      description:
        "Task an EXTERNAL A2A-protocol agent (Google ADK, LangChain, AWS Bedrock, another " +
        "Trinity) and return its answer. The target must be PRE-REGISTERED by an administrator " +
        "— you choose it by name with `endpoint`, and you cannot supply a URL. Ask your " +
        "operator for the registered name if you do not know it; there is deliberately no " +
        "agent-facing listing, because the registered URLs are the shape of the fleet's " +
        "integrations. (`list_a2a_endpoints` (operator-only) shows the registered names.) " +
        "`dedup_label` is required and must DIFFER for each distinct question you ask in this " +
        "turn: calls are deduplicated on the endpoint and conversation, not on your message, so " +
        "reusing a label returns the earlier answer. If the remote replies with state 'working' " +
        "or 'submitted', poll get_a2a_task with the returned task_id. " +
        "If the result carries `payment_required`, the remote charges for this call: relay the " +
        "`payment` details to a person ONCE and stop — do not retry, and do not call a different " +
        "endpoint instead. Only a person can buy the token, and an admin stores it on the " +
        "endpoint; when you are told to try again afterwards, pass the `task_id` the refusal " +
        "returned so the remote resumes the same task rather than starting a new charge.",
      parameters: z.object({
        agent_name: z.string().describe(
          "The Trinity agent placing the call. An agent-scoped key may only pass its own name.",
        ),
        endpoint: z.string().min(1).max(200).describe(
          "Name (or id) of a pre-registered outbound A2A endpoint. NOT a URL.",
        ),
        message: z.string().min(1).max(100000).describe("The message to send to the remote agent."),
        dedup_label: z.string().min(1).max(200).describe(
          "A short label distinguishing this call from others in the same turn, e.g. " +
          "'step-1-research'. Required — reusing one replays the earlier answer.",
        ),
        context_id: z.string().max(200).optional().describe(
          "Continue an existing remote conversation (from a previous call's context_id).",
        ),
        task_id: z.string().max(200).optional().describe(
          "Continue a specific remote task (e.g. answering an input-required prompt).",
        ),
        execution_id: z.string().max(200).optional().describe(
          "Your current execution id, if you have one. Enables at-most-once delivery on retry." +
          " The platform normally supplies this automatically (#2392); this param is a fallback for older agent images.",
        ),
      }),
      execute: async (
        params: {
          agent_name: string;
          endpoint: string;
          message: string;
          dedup_label: string;
          context_id?: string;
          task_id?: string;
          execution_id?: string;
        },
        context?: { session?: McpAuthContext },
      ) => {
        const access = checkSelf(context?.session, params.agent_name);
        if (!access.allowed) {
          return accessDenied(
            context,
            { success: false, error: "Access denied", reason: access.reason, not_authorized: true },
          );
        }
        try {
          const result = await getClient(context?.session).callA2AAgent(params.agent_name, {
            endpoint: params.endpoint,
            message: params.message,
            dedup_label: params.dedup_label,
            context_id: params.context_id,
            task_id: params.task_id,
            execution_id: resolveExecutionId(context?.session, params.execution_id),
          });
          return JSON.stringify({ success: true, ...(result as object) }, null, 2);
        } catch (e) {
          return fail(e);
        }
      },
    },

    // ========================================================================
    get_a2a_task: {
      name: "get_a2a_task",
      description:
        "Poll a remote A2A task by id, on the same pre-registered endpoint that started it. " +
        "Use this when call_a2a_agent returned state 'working' or 'submitted', or when a call " +
        "timed out after the remote had already accepted the task. A `payment_required` result " +
        "here means the same as it does on a call: relay it to a person once and stop polling.",
      parameters: z.object({
        agent_name: z.string().describe("The Trinity agent that placed the original call."),
        endpoint: z.string().min(1).max(200).describe(
          "The same registered endpoint name the task was started on.",
        ),
        task_id: z.string().min(1).max(200).describe("The remote task id from the original call."),
      }),
      execute: async (
        params: { agent_name: string; endpoint: string; task_id: string },
        context?: { session?: McpAuthContext },
      ) => {
        const access = checkSelf(context?.session, params.agent_name);
        if (!access.allowed) {
          return accessDenied(
            context,
            { success: false, error: "Access denied", reason: access.reason, not_authorized: true },
          );
        }
        try {
          const result = await getClient(context?.session).getA2ATask(params.agent_name, {
            endpoint: params.endpoint,
            task_id: params.task_id,
          });
          return JSON.stringify({ success: true, ...(result as object) }, null, 2);
        } catch (e) {
          return fail(e);
        }
      },
    },
  };
}
