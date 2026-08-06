/**
 * Per-agent MCP connector tools (ent#46, + #848 inline email auth).
 *
 * Two kinds of caller reach these tools, and they resolve BOTH the agent and
 * the backend credential differently:
 *
 *   1. **Connector key** (ent#46) — bound to exactly one agent server-side.
 *      The agent comes from the auth context, never from a tool argument, so
 *      the key can only ever reach its own agent. The backend independently
 *      fences connector keys to that agent and refuses owner operations.
 *
 *   2. **Email-verified anonymous session** (#848) — holds no API key at all.
 *      The agent is chosen from the set shared with the verified email, so
 *      these tools take an OPTIONAL `agent` argument: unambiguous when exactly
 *      one agent is available, required when several are. Backend calls go
 *      over the internal surface carrying the verified email, and the backend
 *      re-gates every one of them on that email's own access — the tool
 *      argument is a selection among already-authorized agents, never a way to
 *      reach an unauthorized one.
 *
 * An anonymous session that has NOT verified an email is advertised these
 * tools — the surface is deliberately static across login (see server.ts) — and
 * refuses to act until it has.
 *
 * Tools:
 *   - list_playbooks: the playbooks (skills) this connector exposes as actions
 *   - run_playbook:    run one exposed playbook with optional input
 *   - ask:             free-form chat fallback with the agent
 */
import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

/** Thrown when a caller must log in (or pick an agent) before we can act. */
class NeedsLoginError extends Error {
  constructor(
    message: string,
    readonly code: string,
    readonly available?: string[]
  ) {
    super(message);
  }
}

export function createConnectorTools(
  client: TrinityClient,
  requireApiKey: boolean
) {
  const isAnonymous = (auth?: McpAuthContext): boolean => auth?.scope === "anonymous";

  /** The verified email of an upgraded anonymous session, or undefined. */
  const verifiedEmail = (auth?: McpAuthContext): string | undefined =>
    isAnonymous(auth) ? (auth?.verifiedEmail as string | undefined) : undefined;

  const getClient = (authContext?: McpAuthContext): TrinityClient => {
    if (requireApiKey) {
      // #848: an email-verified anonymous session legitimately has no key. It
      // reaches the backend over the internal surface instead (see
      // resolveTarget / the client's inline-auth methods), so callers must go
      // through `act()` below rather than this client.
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
   * Resolve which agent this call is for, for either caller kind.
   *
   * Connector key → its bound agent; an explicit `agent` argument that
   * disagrees is refused rather than silently ignored, so a client cannot
   * believe it targeted one agent while reaching another.
   *
   * Anonymous session → the requested agent, or the sole available one.
   */
  const resolveAgent = (auth: McpAuthContext | undefined, requested?: string): string => {
    const email = verifiedEmail(auth);
    if (email) {
      const available = (auth?.agents as string[] | undefined) ?? [];
      if (requested) {
        // No `available.length > 0` guard: an empty list means NOTHING is
        // shared with this address, so every requested agent must be refused.
        // Guarding on non-empty let an attacker-chosen name pass straight
        // through in exactly the state the code models as "you have nothing" —
        // harmless today only because the backend re-gates uniformly, which is
        // the wrong thing to depend on for a client-side check whose entire job
        // is defence in depth.
        if (!available.includes(requested)) {
          throw new NeedsLoginError(
            available.length === 0
              ? "No agents are shared with your address yet — an owner needs to grant access."
              : `"${requested}" is not one of the agents shared with ${email}.`,
            available.length === 0 ? "no_agents_available" : "agent_not_available",
            available
          );
        }
        return requested;
      }
      if (available.length === 1) return available[0];
      if (available.length === 0) {
        throw new NeedsLoginError(
          "No agents are shared with your address yet — an owner needs to grant access.",
          "no_agents_available"
        );
      }
      throw new NeedsLoginError(
        "Several agents are available; pass `agent` to choose one.",
        "agent_required",
        available
      );
    }

    if (isAnonymous(auth)) {
      throw new NeedsLoginError(
        "You are not signed in. Call request_login with your email address, then verify_login with the code.",
        "login_required"
      );
    }

    // Connector key: the bound agent is authoritative.
    const bound = auth?.agentName;
    if (!bound) {
      throw new Error("Connector key is not bound to an agent");
    }
    if (requested && requested !== bound) {
      throw new NeedsLoginError(
        `This connector is bound to "${bound}" and cannot reach "${requested}".`,
        "agent_not_available",
        [bound]
      );
    }
    return bound;
  };

  /** Render a NeedsLoginError as a structured tool result rather than a throw. */
  const asError = (err: unknown): string | null => {
    if (!(err instanceof NeedsLoginError)) return null;
    return JSON.stringify(
      {
        error: err.code,
        message: err.message,
        ...(err.available ? { available_agents: err.available } : {}),
      },
      null,
      2
    );
  };

  const keyInfo = (authContext?: McpAuthContext) => ({
    keyId: authContext?.keyId,
    keyName: authContext?.keyName,
  });

  /** Fetch exposed playbooks for either caller kind. */
  const fetchPlaybooks = async (auth: McpAuthContext | undefined, agent: string) => {
    const email = verifiedEmail(auth);
    if (email) return client.getInlineConnectorPlaybooks(email, agent);
    return getClient(auth).getConnectorPlaybooks(agent);
  };

  /** Dispatch a chat for either caller kind. */
  const dispatchChat = async (
    auth: McpAuthContext | undefined,
    agent: string,
    message: string
  ) => {
    const email = verifiedEmail(auth);
    if (email) return client.inlineConnectorChat(email, agent, message);
    return getClient(auth).chat(agent, message, undefined, keyInfo(auth));
  };

  return {
    listPlaybooks: {
      name: "list_playbooks",
      description:
        "List the playbooks this agent exposes as actions you can run. " +
        "Each playbook is a named capability with an optional argument hint. " +
        "Use run_playbook to execute one.",
      parameters: z.object({
        agent: z
          .string()
          .optional()
          .describe(
            "Which agent, when several are available to you. Ignored for a " +
              "connector bound to a single agent."
          ),
      }),
      execute: async (
        { agent: requested }: { agent?: string },
        context?: { session?: McpAuthContext }
      ) => {
        try {
          const agent = resolveAgent(context?.session, requested);
          const playbooks = await fetchPlaybooks(context?.session, agent);
          return JSON.stringify({ agent, playbooks }, null, 2);
        } catch (err) {
          const structured = asError(err);
          if (structured) return structured;
          throw err;
        }
      },
    },

    runPlaybook: {
      name: "run_playbook",
      description:
        "Run one of this agent's exposed playbooks. Pass the playbook `name` " +
        "(from list_playbooks) and optional `input` describing what you want. " +
        "Returns the agent's response.",
      parameters: z.object({
        name: z.string().describe("Playbook name, exactly as returned by list_playbooks"),
        input: z.string().optional().describe("Optional input / arguments for the playbook"),
        agent: z
          .string()
          .optional()
          .describe("Which agent, when several are available to you."),
      }),
      execute: async (
        { name, input, agent: requested }: { name: string; input?: string; agent?: string },
        context?: { session?: McpAuthContext }
      ) => {
        try {
          const agent = resolveAgent(context?.session, requested);

          // Server-side enforcement: only run a playbook the connector actually
          // exposes (allow-list ∩ user_invocable). Never trust the client to
          // stay within the advertised set. This holds for both caller kinds —
          // the inline path resolves the same allow-list backend-side.
          const allowed = await fetchPlaybooks(context?.session, agent);
          if (!allowed.some((p) => p.name === name)) {
            return JSON.stringify({
              error: "playbook_not_exposed",
              message: `Playbook "${name}" is not exposed by this connector.`,
              available: allowed.map((p) => p.name),
            });
          }

          const message = input
            ? `Please run the "${name}" playbook.\n\nInput:\n${input}`
            : `Please run the "${name}" playbook.`;
          const result = await dispatchChat(context?.session, agent, message);
          return JSON.stringify(result, null, 2);
        } catch (err) {
          const structured = asError(err);
          if (structured) return structured;
          throw err;
        }
      },
    },

    ask: {
      name: "ask",
      description:
        "Ask this agent anything in free-form natural language (chat fallback " +
        "for when no specific playbook fits). Returns the agent's response.",
      parameters: z.object({
        message: z.string().describe("Your message to the agent"),
        agent: z
          .string()
          .optional()
          .describe("Which agent, when several are available to you."),
      }),
      execute: async (
        { message, agent: requested }: { message: string; agent?: string },
        context?: { session?: McpAuthContext }
      ) => {
        try {
          const agent = resolveAgent(context?.session, requested);
          const result = await dispatchChat(context?.session, agent, message);
          return JSON.stringify(result, null, 2);
        } catch (err) {
          const structured = asError(err);
          if (structured) return structured;
          throw err;
        }
      },
    },
  };
}
