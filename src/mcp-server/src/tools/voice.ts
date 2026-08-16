/**
 * Voice Reply Tool (trinity-enterprise#117)
 *
 * `send_voice_reply` lets an agent deliver ONE reply of the current channel turn
 * as a spoken voice note (ElevenLabs TTS) instead of text. Voice is a per-message
 * capability: enabling voice on the agent grants the ability; each reply is text
 * by default and becomes voice only when the agent explicitly calls this tool.
 *
 * The backend resolves the channel destination (chat id / thread / binding) from
 * the execution_id — the tool only needs the text to speak. Fail-soft: any
 * synthesis/delivery miss returns not-delivered so the agent falls back to text.
 */

import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

export function createVoiceReplyTools(
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

  const getAgentName = (
    authContext: McpAuthContext | undefined,
    specifiedAgent?: string
  ): string => {
    if (specifiedAgent) {
      return specifiedAgent;
    }
    if (authContext?.scope === "agent" && authContext.agentName) {
      return authContext.agentName;
    }
    throw new Error(
      "Agent name is required. Either use an agent-scoped API key or specify the agent_name parameter."
    );
  };

  return {
    // ========================================================================
    // send_voice_reply - Speak one reply of the current channel turn
    // ========================================================================
    sendVoiceReply: {
      name: "send_voice_reply",
      description:
        "Deliver a spoken voice note to the user you are currently talking with on a " +
        "messaging channel (Telegram, Slack, or WhatsApp), instead of plain text. " +
        "Use this ONLY when a spoken reply fits — a short confirmation, a greeting, a " +
        "read-aloud answer. The voice note is sent immediately; your normal final text " +
        "reply is still delivered as text unless you end your turn with [NO_REPLY]. " +
        "Requires voice to be enabled for this agent and allowed on the current channel; " +
        "if voice can't be delivered the call returns delivered=false and you should just " +
        "reply with text. Keep the spoken text short (a long reply is refused by the cost cap). " +
        "A refusal here is a limit on THIS TOOL, not proof the surface has no voice at all — " +
        "the Workspace, for one, reads your text aloud on the client's own speaker control. " +
        "Read the `guidance` field when present, act on it, and never quote a `reason` code " +
        "back to a user (#2157).",
      parameters: z.object({
        text: z.string().min(1).max(4096)
          .describe("The text to speak as a voice note. Keep it short and natural to hear."),
        execution_id: z.string().min(1)
          .describe(
            "Your current execution_id — shown in the 'Execution Context' block of your " +
            "system prompt as '- **Execution ID**: <id>'. Required: the backend resolves the " +
            "channel destination from it, and it dedupes a re-delivered turn (#1084)."
          ),
        dedup_label: z.string().optional()
          .describe(
            "Optional discriminator to intentionally send TWO distinct voice notes in one turn. " +
            "Default empty → at-most-one voice note per turn."
          ),
        agent_name: z.string().optional()
          .describe(
            "Agent name to send as. Required for user-scoped API keys. " +
            "For agent-scoped keys, defaults to the calling agent."
          ),
      }),
      execute: async (
        params: {
          text: string;
          execution_id: string;
          dedup_label?: string;
          agent_name?: string;
        },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        if (!params.text || params.text.trim().length === 0) {
          return JSON.stringify({ delivered: false, error: "Text cannot be empty" }, null, 2);
        }

        try {
          const agentName = getAgentName(authContext, params.agent_name);
          const result = await apiClient.sendVoiceReply(agentName, {
            text: params.text.trim(),
            execution_id: params.execution_id,
            dedup_label: params.dedup_label,
          });
          return JSON.stringify(
            {
              delivered: result.delivered,
              channel: result.channel,
              reason: result.reason,
              // #2157: the backend's plain-language explanation of a refusal, when
              // it has one. `reason` is a machine code an agent was relaying to
              // clients verbatim ("The system confirmed it: not_a_channel_turn");
              // this is the sentence it should reason from — and act on — instead.
              ...(result.guidance ? { guidance: result.guidance } : {}),
              // Hint the agent to still send text when voice didn't go out.
              fallback_to_text: !result.delivered,
            },
            null,
            2
          );
        } catch (error) {
          const errorMessage = error instanceof Error ? error.message : String(error);
          console.error(`[send_voice_reply] Error: ${errorMessage}`);

          if (errorMessage.includes("409")) {
            return JSON.stringify(
              { delivered: false, in_progress: true, error: "A voice note for this turn is already being sent." },
              null,
              2
            );
          }
          // 404 (agent not found / voice not available), 403, 422 (non-channel turn),
          // 503 (no key) all mean "can't speak" — the agent should reply with text.
          return JSON.stringify(
            { delivered: false, fallback_to_text: true, error: errorMessage },
            null,
            2
          );
        }
      },
    },
  };
}
