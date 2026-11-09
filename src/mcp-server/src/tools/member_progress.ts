/**
 * Member setup progress card — self-reporting tool for the member's assistant agent (ent#1175)
 *
 * The member's assistant agent calls ``set_member_progress`` to
 * advance a step in the member's onboarding card. The backend writes the step
 * and broadcasts a ``member_progress_update`` WS event so the member's browser
 * tab updates without reloading.
 *
 * Auth: agent-scoped key naming itself (``assert_own_agent`` on the backend).
 * The five canonical step keys are: assistant_ready, trinity_being_prepared,
 * trinity_ready, first_agent_built, first_run.
 */

import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

const ok = (obj: unknown): string => JSON.stringify(obj, null, 2);
const fail = (error: string): string =>
  JSON.stringify({ success: false, error }, null, 2);

const VALID_STEPS = [
  "assistant_ready",
  "trinity_being_prepared",
  "trinity_ready",
  "first_agent_built",
  "first_run",
] as const;

const VALID_STATES = ["pending", "in_progress", "done", "failed"] as const;

export function createMemberProgressTools(
  client: TrinityClient,
  requireApiKey: boolean
) {
  const getClient = (authContext?: McpAuthContext): TrinityClient => {
    if (requireApiKey) {
      if (!authContext?.mcpApiKey) {
        throw new Error(
          "MCP API key authentication required but no API key found in request context"
        );
      }
      const userClient = new TrinityClient(client.getBaseUrl());
      userClient.setToken(authContext.mcpApiKey);
      return userClient;
    }
    return client;
  };

  return {
    setMemberProgress: {
      name: "set_member_progress",
      description:
        "Report progress on the member onboarding setup card (ent#1175). " +
        "Call this as your own setup proceeds to update the five-step card the " +
        "member sees: assistant_ready → trinity_being_prepared → trinity_ready " +
        "→ first_agent_built → first_run. The card updates in the member's browser " +
        "immediately via WebSocket. Set state to 'in_progress' when a step starts, " +
        "'done' when it completes, 'failed' with a plain-language note when it " +
        "cannot complete. Unknown step keys are silently ignored.",
      parameters: z.object({
        agent_name: z
          .string()
          .min(1)
          .describe("Your own agent name (the member's assistant agent)."),
        step: z
          .enum(VALID_STEPS)
          .describe(
            "Step key: assistant_ready | trinity_being_prepared | trinity_ready " +
              "| first_agent_built | first_run"
          ),
        state: z
          .enum(VALID_STATES)
          .describe("New state: pending | in_progress | done | failed"),
        note: z
          .string()
          .optional()
          .describe(
            "Short plain-language note shown to the member (e.g. what failed and what happens next)."
          ),
      }),
      execute: async (
        params: {
          agent_name: string;
          step: string;
          state: string;
          note?: string;
        },
        context?: { session?: McpAuthContext }
      ) => {
        const apiClient = getClient(context?.session);
        const { agent_name, step, state, note } = params;
        console.log(
          `[set_member_progress] agent=${agent_name} step=${step} state=${state}`
        );

        try {
          const result = await apiClient.request(
            "POST",
            `/api/enterprise/client-portal/agents/${encodeURIComponent(agent_name)}/progress/step`,
            { step, state, note }
          );
          return ok({ success: true, ...(result as Record<string, unknown>) });
        } catch (err) {
          const msg = err instanceof Error ? err.message : String(err);
          console.error(`[set_member_progress] error: ${msg}`);
          return fail(`Could not set progress step: ${msg}`);
        }
      },
    },
  };
}
