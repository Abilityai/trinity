/**
 * Outbound File Sharing Tools (FILES-001)
 *
 * Agents call `share_file` to publish a file from their
 * /home/developer/public/ directory and receive a public download URL
 * with a 7-day default expiration. The agent's owner must have enabled
 * file sharing for the agent (see Sharing panel in AgentDetail).
 */

import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

export function createFileTools(
  client: TrinityClient,
  requireApiKey: boolean
) {
  /** Pick the right client instance based on auth mode. */
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
    // ========================================================================
    // share_file - Publish a file and get a download URL
    // ========================================================================
    shareFile: {
      name: "share_file",
      description:
        "Publish a file from your /home/developer/public/ directory and " +
        "return a public download URL. The file is copied from the agent's " +
        "publish volume into platform storage; the URL includes a signed " +
        "token and expires after 7 days by default (configurable). " +
        "The owner must enable file sharing for this agent in the Sharing " +
        "panel before this tool can be used. " +
        "WHO SEES IT: the link works for anyone you give it to. Separately, the " +
        "file is listed in the Workspace Files tab of ONE person — the person " +
        "this conversation is with. The platform works that out from your " +
        "execution_id, so pass it. A turn with no person (a schedule, an " +
        "operator chat, an agent-to-agent call) lists the file for the agent's " +
        "owner only. The result tells you what happened: check " +
        "`visible_to_requester`.",
      parameters: z.object({
        filename: z
          .string()
          .min(1)
          .describe(
            "Relative path inside /home/developer/public/ (e.g. 'report.csv'). " +
              "Absolute paths and '..' segments are rejected."
          ),
        display_name: z
          .string()
          .optional()
          .describe(
            "Override the download filename shown to the user. Defaults to the basename of `filename`."
          ),
        expires_in: z
          .number()
          .int()
          .min(60)
          .max(604800)
          .optional()
          .describe(
            "Seconds until the link expires. Default 604800 (7 days). " +
              "Minimum 60, maximum 604800."
          ),
        execution_id: z
          .string()
          .optional()
          .describe(
            "Your current execution_id — shown in the 'Execution Context' block of your system " +
              "prompt. Pass it: it is how the platform knows which conversation this share belongs " +
              "to, and so whose Files tab lists the file — without it the file is listed for the " +
              "agent's owner only. It also lets a re-run of this turn replay the original signed " +
              "URL instead of minting a new token (#1084)."
          ),
        audience_email: z
          .string()
          .optional()
          .describe(
            "Optional. List the file in the Files tab of a DIFFERENT person instead of the one " +
              "this conversation is with. Must be the email of someone this agent is already " +
              "shared with; any other address is refused (AUDIENCE_NOT_ON_ROSTER). Omit it in the " +
              "ordinary case — the platform addresses the file to the person you are talking to."
          ),
        dedup_label: z
          .string()
          .optional()
          .describe(
            "Optional discriminator to intentionally create TWO distinct shares of the same file " +
              "in one turn. Default empty → at-most-one share per (filename, content, addressee) per turn."
          ),
      }),
      execute: async (
        params: {
          filename: string;
          display_name?: string;
          expires_in?: number;
          execution_id?: string;
          dedup_label?: string;
          audience_email?: string;
        },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        // Agent identity: only agent-scoped MCP keys can share files from
        // their own volume. User-scoped keys can't share files "from
        // nowhere" — the backend will 403 anyway, but we surface a cleaner
        // message here.
        const agentName = authContext?.agentName;
        if (!agentName) {
          return JSON.stringify(
            {
              success: false,
              error:
                "share_file requires an agent-scoped MCP key. This tool " +
                "cannot be called with a user-scoped key.",
            },
            null,
            2
          );
        }

        console.log(
          `[share_file] agent=${agentName} filename=${params.filename}`
        );

        try {
          const result = await apiClient.shareAgentFile(agentName, {
            filename: params.filename,
            display_name: params.display_name,
            expires_in: params.expires_in,
            execution_id: params.execution_id,
            dedup_label: params.dedup_label,
            audience_email: params.audience_email,
          });

          return JSON.stringify(
            {
              success: true,
              file_id: result.file_id,
              url: result.url,
              expires_at: result.expires_at,
              size_bytes: result.size_bytes,
              mime_type: result.mime_type,
              // ent#549 — honest status, in the names `set_canvas` uses. `false`
              // means the platform could not tell which conversation this came
              // from and listed the file for the owner only; the note says how
              // to fix it. `null` is no claim, never a guess.
              visible_to_requester: result.visible_to_requester ?? null,
              visibility_note: result.visibility_note ?? null,
              // Only ever the address THIS agent supplied. One the platform
              // resolved is never returned to a model.
              addressed_to: result.addressed_to ?? null,
            },
            null,
            2
          );
        } catch (error) {
          const errorMessage =
            error instanceof Error ? error.message : String(error);
          console.error(`[share_file] error: ${errorMessage}`);
          return JSON.stringify(
            { success: false, error: errorMessage },
            null,
            2
          );
        }
      },
    },
  };
}
