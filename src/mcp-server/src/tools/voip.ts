/**
 * VoIP Telephony Tools (VOIP-001, #1056 — Phase 1, outbound)
 *
 * MCP tool for an agent to place an outbound phone call to a user and hold a
 * real-time voice conversation over the Gemini Live bridge. The feature is
 * gated server-side: it only functions when VOIP is enabled platform-wide AND
 * the agent has a Twilio voice binding, and it is rate-limited + daily-capped.
 */

import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

export function createVoipTools(client: TrinityClient, requireApiKey: boolean) {
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
    if (specifiedAgent) return specifiedAgent;
    if (authContext?.scope === "agent" && authContext.agentName) {
      return authContext.agentName;
    }
    throw new Error(
      "Agent name is required. Either use an agent-scoped API key or specify the agent_name parameter."
    );
  };

  return {
    // ========================================================================
    // call_user - Place an outbound phone call to a user
    // ========================================================================
    callUser: {
      name: "call_user",
      description:
        "Place an outbound phone call to a user and hold a real-time, spoken " +
        "voice conversation. The agent dials the number over Twilio and talks " +
        "via the Gemini Live voice bridge; after the call ends, the full " +
        "transcript is handed back to you to process. Requires VoIP to be " +
        "enabled and a Twilio voice number configured for this agent. " +
        "Rate-limited and subject to a daily call cap (placing calls costs money).",
      parameters: z.object({
        to_number: z.string()
          .describe("Destination phone number in E.164 format, e.g. '+14155551234'."),
        context: z.string().max(2000).optional()
          .describe("Optional purpose/brief for the call, spoken-context for the agent."),
        process_transcript: z.boolean().default(true)
          .describe("If true (default), the agent processes the call transcript after it ends."),
        agent_name: z.string().optional()
          .describe("Agent to call as. Required for user-scoped keys; defaults to the calling agent."),
        execution_id: z.string().optional()
          .describe(
            "Your current execution_id — shown in the 'Execution Context' block of your system " +
            "prompt. Pass it so a re-delivery of this turn does NOT place a duplicate phone call " +
            "(effect-scoped idempotency, #1084). Optional: if omitted, the call proceeds without dedup."
          ),
        dedup_label: z.string().optional()
          .describe(
            "Optional discriminator to intentionally place TWO distinct calls to the same number " +
            "in one turn. Default empty → at-most-one call per number per turn."
          ),
      }),
      execute: async (
        params: {
          to_number: string;
          context?: string;
          process_transcript?: boolean;
          agent_name?: string;
          execution_id?: string;
          dedup_label?: string;
        },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);
        try {
          const agentName = getAgentName(authContext, params.agent_name);
          const result = await apiClient.placeVoipCall(agentName, {
            to_number: params.to_number,
            context: params.context,
            process_transcript: params.process_transcript ?? true,
            execution_id: params.execution_id,
            dedup_label: params.dedup_label,
          });
          return JSON.stringify({ success: true, agent_name: agentName, ...result }, null, 2);
        } catch (error) {
          const msg = error instanceof Error ? error.message : String(error);
          if (msg.includes("404")) {
            return JSON.stringify({
              success: false,
              error: "VoIP is not enabled, or no Twilio voice binding is configured for this agent.",
            }, null, 2);
          }
          if (msg.includes("429")) {
            return JSON.stringify({
              success: false,
              error: "Rate limited or daily call cap reached.",
              rate_limited: true,
            }, null, 2);
          }
          return JSON.stringify({ success: false, error: msg }, null, 2);
        }
      },
    },
  };
}
