/**
 * Per-User Memory Tools (MEM-001, #888)
 *
 * Provides write_user_memory — the safe alternative to writing user PII to the
 * shared agent filesystem. The user email is resolved server-side from the
 * execution record so the agent cannot write memory for an arbitrary user.
 */

import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

export function createMemoryTools(client: TrinityClient, requireApiKey: boolean) {
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

  return {
    // ========================================================================
    // write_user_memory — persist facts about the current user
    // ========================================================================
    writeUserMemory: {
      name: "write_user_memory",
      description:
        "Persist facts about the user you are currently serving in an isolated, per-user memory store. " +
        "Use this instead of writing to ~/.claude/projects/memory/ — that path is shared across ALL users " +
        "of this agent and would leak one user's data to everyone else.\n\n" +
        "**When to use**: The user explicitly asks you to remember something, or you learn a fact " +
        "(name, preference, timezone, context) that would be useful in future sessions.\n\n" +
        "**How it works**: Supply your `execution_id` (from the Execution Context block in your system prompt) " +
        "and the complete updated memory blob. The platform resolves the user's email from the execution " +
        "record — you never touch email addresses directly.\n\n" +
        "**Write the complete blob**: Read the current memory from the 'What you know about this user' " +
        "block (if present), incorporate the new fact, and write everything back.\n\n" +
        "**Only works in user-facing sessions** (public link, Slack, Telegram, WhatsApp). " +
        "Returns an error if called from a scheduled task or agent-to-agent execution.",
      parameters: z.object({
        execution_id: z
          .string()
          .min(1)
          .describe(
            "Your current execution_id — shown in the 'Execution Context' block of your system prompt " +
            "as '- **Execution ID**: <id>'. Required so the platform can resolve the user's email."
          ),
        memory_text: z
          .string()
          .max(8000)
          .describe(
            "The complete updated memory blob for this user. Write factual, concise notes " +
            "(name, preferences, context, timezone, etc.). This replaces the previous memory entirely, " +
            "so include anything from the existing memory you want to keep."
          ),
        agent_name: z
          .string()
          .optional()
          .describe(
            "Agent name override. Defaults to the agent whose MCP key is making this call. " +
            "Omit in normal use."
          ),
      }),
      execute: async (
        {
          execution_id,
          memory_text,
          agent_name,
        }: {
          execution_id: string;
          memory_text: string;
          agent_name?: string;
        },
        context: any
      ) => {
        const authContext = requireApiKey ? context?.session : undefined;
        const apiClient = getClient(authContext);

        // Resolve agent name: prefer explicit param, fall back to key's agent_name, then error.
        const resolvedAgent =
          agent_name ||
          (authContext?.scope === "agent" ? authContext.agentName : undefined);

        if (!resolvedAgent) {
          return JSON.stringify(
            {
              success: false,
              error:
                "Cannot determine agent name. Pass agent_name explicitly or call this tool " +
                "using an agent-scoped MCP key.",
            },
            null,
            2
          );
        }

        console.log(
          `[write_user_memory] ${resolvedAgent} execution=${execution_id} ` +
          `memory_len=${memory_text.length}`
        );

        try {
          const result = await apiClient.writeUserMemory(resolvedAgent, {
            execution_id,
            memory_text,
          });
          return JSON.stringify(result, null, 2);
        } catch (error) {
          const errorMessage = error instanceof Error ? error.message : String(error);
          console.error(`[write_user_memory] Error: ${errorMessage}`);
          return JSON.stringify({ success: false, error: errorMessage }, null, 2);
        }
      },
    },
  };
}
