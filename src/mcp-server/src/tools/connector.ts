/**
 * Per-agent MCP connector tools (ent#46).
 *
 * These tools are exposed ONLY to connector-scoped keys (see the canAccess
 * gating in server.ts). The agent is taken from the auth context (the key is
 * bound to exactly one agent server-side), never from a tool argument, so a
 * connector key can only ever reach its own agent. The backend additionally
 * fences connector keys to their bound agent and refuses owner operations.
 *
 * Tools:
 *   - list_playbooks: the playbooks (skills) this connector exposes as actions
 *   - run_playbook:    run one exposed playbook with optional input
 *   - ask:             free-form chat fallback with the agent
 */
import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

export function createConnectorTools(
  client: TrinityClient,
  requireApiKey: boolean
) {
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

  // The bound agent for a connector key. Hard error if somehow missing — a
  // connector key without an agent must never silently fall through to a
  // different agent.
  const boundAgent = (authContext?: McpAuthContext): string => {
    const name = authContext?.agentName;
    if (!name) {
      throw new Error("Connector key is not bound to an agent");
    }
    return name;
  };

  const keyInfo = (authContext?: McpAuthContext) => ({
    keyId: authContext?.keyId,
    keyName: authContext?.keyName,
  });

  return {
    listPlaybooks: {
      name: "list_playbooks",
      description:
        "List the playbooks this agent exposes as actions you can run. " +
        "Each playbook is a named capability with an optional argument hint. " +
        "Use run_playbook to execute one.",
      parameters: z.object({}),
      execute: async (_args: unknown, context?: { session?: McpAuthContext }) => {
        const apiClient = getClient(context?.session);
        const agent = boundAgent(context?.session);
        const playbooks = await apiClient.getConnectorPlaybooks(agent);
        return JSON.stringify({ agent, playbooks }, null, 2);
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
      }),
      execute: async (
        { name, input }: { name: string; input?: string },
        context?: { session?: McpAuthContext }
      ) => {
        const apiClient = getClient(context?.session);
        const agent = boundAgent(context?.session);

        // Server-side enforcement: only run a playbook the connector actually
        // exposes (allow-list ∩ user_invocable). Never trust the client to
        // stay within the advertised set.
        const allowed = await apiClient.getConnectorPlaybooks(agent);
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
        const result = await apiClient.chat(agent, message, undefined, keyInfo(context?.session));
        return JSON.stringify(result, null, 2);
      },
    },

    ask: {
      name: "ask",
      description:
        "Ask this agent anything in free-form natural language (chat fallback " +
        "for when no specific playbook fits). Returns the agent's response.",
      parameters: z.object({
        message: z.string().describe("Your message to the agent"),
      }),
      execute: async (
        { message }: { message: string },
        context?: { session?: McpAuthContext }
      ) => {
        const apiClient = getClient(context?.session);
        const agent = boundAgent(context?.session);
        const result = await apiClient.chat(agent, message, undefined, keyInfo(context?.session));
        return JSON.stringify(result, null, 2);
      },
    },
  };
}
