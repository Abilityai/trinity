/**
 * Shared sessions / rooms (ent#169).
 *
 * A room is a shared persistent RECORD, never a shared CONTEXT: every agent
 * keeps its own isolated session and is handed only the transcript it has not
 * seen. Turn-taking is mechanical — you are woken iff you were @mentioned — so
 * nothing here has to decide who speaks next.
 *
 * These tools are a thin proxy. All authorization lives server-side: membership
 * is the grant, and a non-member gets a uniform 404 (never a 403 that would
 * confirm the room exists). The acting identity comes from the API key, so an
 * agent-scoped key always acts as its own agent and cannot post as anyone else.
 *
 * Rooms are OSS core since ent#443, so on a CURRENT backend the routes always
 * answer. Against an older backend that does not serve them, these tools return
 * a structured "not enabled" result rather than throwing a transport error at
 * the agent.
 */
import { z } from "zod";
import { ApiError, TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

const NOT_ENABLED = {
  error: "shared_sessions_not_enabled",
  message: "Shared sessions are not enabled on this Trinity instance.",
};

/**
 * Is this error the ABSENCE of the rooms surface, rather than a refusal BY it?
 *
 * The status alone cannot answer that, and reading it as if it could is how a
 * real refusal gets reported to an agent as "rooms are switched off on this
 * instance" — sending whoever reads it after entirely the wrong thing. The
 * serving module authors every refusal as a structured `detail: {code, …}`:
 * 403 for an agent it may not reach, and a deliberately uniform 404 for a room
 * the caller is not a member of (never a 403, which would confirm the room
 * exists). Absence is a plain string — FastAPI's own "Not Found" for a route
 * that was never mounted, or an older build's entitlement sentence.
 *
 * So the discriminator is the SHAPE of `detail`, not the status code. This
 * mirrors the rule the Workspace already follows for the same distinction
 * (#2128) — before ent#443 the two were genuinely indistinguishable here,
 * because an unentitled 403 really did mean "not enabled".
 */
export function unavailable(e: unknown): boolean {
  const status = e instanceof ApiError ? e.status : undefined;
  if (status !== 404 && status !== 403) {
    // Not an ApiError at all (transport failure), or some other status.
    // Fall back to the pre-ent#443 string test ONLY for a non-ApiError, so an
    // older client path still degrades the way it used to.
    if (e instanceof ApiError) return false;
    const msg = e instanceof Error ? e.message : String(e);
    return msg.includes("404") || msg.includes("403");
  }

  const body = (e as ApiError).body;
  try {
    const detail = (JSON.parse(body) as { detail?: unknown }).detail;
    // A coded object is the module speaking. Anything else — a plain string,
    // a missing detail, an unparseable body — reads as absence, which keeps
    // the friendly result as the default for everything we do not recognise.
    if (detail && typeof detail === "object" && "code" in (detail as object)) {
      return false;
    }
  } catch {
    // Not JSON — absence.
  }
  return true;
}

export function createRoomTools(client: TrinityClient, requireApiKey: boolean) {
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

  const guard = async (fn: () => Promise<unknown>): Promise<string> => {
    try {
      return JSON.stringify(await fn(), null, 2);
    } catch (e) {
      if (unavailable(e)) return JSON.stringify(NOT_ENABLED, null, 2);
      return JSON.stringify(
        { error: "room_request_failed", message: e instanceof Error ? e.message : String(e) },
        null,
        2
      );
    }
  };

  return {
    createRoom: {
      name: "create_room",
      description:
        "Open a shared session (room) with one or more agents so you can work a " +
        "topic together across many turns. You must have access to every agent " +
        "you add. Rooms are bounded: they close when they hit their message, " +
        "cost, or time budget.",
      parameters: z.object({
        name: z.string().describe("Short room name"),
        agents: z.array(z.string()).min(1).describe("Agent names to add as participants"),
        topic: z.string().optional().describe("What the room is for"),
        max_messages: z.number().int().positive().optional().describe("Message budget (default 60)"),
        max_cost_usd: z.number().positive().optional().describe("Cost budget in USD"),
        ttl_hours: z.number().int().min(0).optional().describe("Auto-close after N hours (default 24; 0 = never)"),
        scribe: z.string().optional().describe("Participant designated to record outcomes"),
      }),
      execute: async (args: any, context?: { session?: McpAuthContext }) =>
        guard(() => getClient(context?.session).createRoom(args)),
    },

    listRooms: {
      name: "list_rooms",
      description:
        "List the shared sessions (rooms) you are a participant in, with their " +
        "status, message count and participant count.",
      parameters: z.object({}),
      execute: async (_a: unknown, context?: { session?: McpAuthContext }) =>
        guard(() => getClient(context?.session).listRooms()),
    },

    readRoom: {
      name: "read_room",
      description:
        "Read a room's transcript and participants. Pass `since` (a message seq) " +
        "to fetch only what is new — the cheap way to catch up on a long room.",
      parameters: z.object({
        room_id: z.string(),
        since: z.number().int().min(0).optional().describe("Return only messages after this seq"),
      }),
      execute: async (
        { room_id, since }: { room_id: string; since?: number },
        context?: { session?: McpAuthContext }
      ) => guard(() => getClient(context?.session).readRoom(room_id, since ?? 0)),
    },

    postToRoom: {
      name: "post_to_room",
      description:
        "Post a message to a room. @mention a participant by name to wake them — " +
        "a message with no mention joins the transcript without waking anyone. " +
        "You post as yourself; you cannot post as another participant.",
      parameters: z.object({
        room_id: z.string(),
        content: z.string().describe("Message text; @mention participants to wake them"),
      }),
      execute: async (
        { room_id, content }: { room_id: string; content: string },
        context?: { session?: McpAuthContext }
      ) => guard(() => getClient(context?.session).postToRoom(room_id, content)),
    },

    closeRoom: {
      name: "close_room",
      description:
        "Close a room. The transcript stays readable; no further messages can be " +
        "posted. Idempotent.",
      parameters: z.object({
        room_id: z.string(),
        reason: z.string().optional(),
      }),
      execute: async (
        { room_id, reason }: { room_id: string; reason?: string },
        context?: { session?: McpAuthContext }
      ) => guard(() => getClient(context?.session).closeRoom(room_id, reason)),
    },
  };
}
