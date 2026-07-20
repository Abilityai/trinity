/**
 * Inline email auth for keyless MCP sessions (#848).
 *
 * Lets an external user authenticate from inside their MCP client with the
 * existing 6-digit email-code flow — no pre-minted API key, no web-UI visit:
 *
 *   request_login({ email })  → a code is emailed (if the address is known)
 *   verify_login({ code })    → the session is bound to the verified email
 *
 * Design notes (see docs/memory/requirements/mcp.md §7.6):
 *
 *  - **Session, not a key.** Nothing is returned for the user to persist. The
 *    verified email is written onto the session's auth context IN PLACE —
 *    FastMCP hands every tool the same object by reference, so subsequent
 *    calls on the session observe the upgrade with no library support. The
 *    binding dies with the connection.
 *
 *  - **Enumeration safety.** `request_login` returns a single generic,
 *    branch-independent response whether or not the address is known, and
 *    emits no audit event. Any per-branch signal — wording, timing, an audit
 *    row — is an oracle for "is this email registered". This mirrors
 *    `routers/auth.py::request_email_code` (#186). `verify_login` outcomes ARE
 *    audited, matching the web path.
 *
 *  - **Whitelist bypass.** Inline login deliberately does not require the
 *    email whitelist, matching Telegram's inline /login. The whitelist-gated
 *    HTTP endpoints silently no-op for unknown addresses, so routing through
 *    them could never onboard the external users this feature exists for.
 *    Authorization is the channel access gate instead (email_has_agent_access
 *    → allow, else an access request), applied backend-side per call.
 *
 *  - **Rate limiting.** Telegram's inline login has none — tolerable behind a
 *    bot API, not on an open MCP port. The backend applies the same
 *    per-account and per-OTP limiters as the web flow; this layer adds a cheap
 *    per-session guard so a single connection cannot pump the backend.
 */
import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

/** Max request_login calls per session before we stop relaying to the backend. */
const MAX_REQUESTS_PER_SESSION = 5;
/** Max verify_login attempts per session. Backend limiters are authoritative. */
const MAX_VERIFY_ATTEMPTS_PER_SESSION = 10;

/**
 * The single response `request_login` ever returns. Deliberately constant:
 * callers must not be able to distinguish "code sent" from "unknown address".
 */
const GENERIC_REQUEST_RESPONSE = JSON.stringify(
  {
    status: "ok",
    message:
      "If that address can access an agent here, a 6-digit code is on its way. " +
      "Call verify_login with the code to finish signing in.",
  },
  null,
  2
);

/** Per-session counters. Keyed by the session's opaque id, not by email. */
interface SessionCounters {
  requests: number;
  verifies: number;
}

export function createAuthTools(client: TrinityClient, _requireApiKey: boolean) {
  const counters = new Map<string, SessionCounters>();

  const countersFor = (session?: McpAuthContext): SessionCounters => {
    const id = session?.sessionId ?? "unknown";
    let c = counters.get(id);
    if (!c) {
      c = { requests: 0, verifies: 0 };
      counters.set(id, c);
    }
    return c;
  };

  /**
   * Inline auth is only meaningful for an anonymous session. A key-bearing
   * session is already authenticated; these tools are not advertised to it,
   * but `canAccess` governs advertisement only — a client may still call a
   * tool by name, so every entry point re-checks.
   */
  const requireAnonymous = (session?: McpAuthContext): McpAuthContext => {
    if (!session || session.scope !== "anonymous") {
      throw new Error(
        "Inline login is only available to a keyless session. This session is " +
          "already authenticated with an API key."
      );
    }
    return session;
  };

  return {
    requestLogin: {
      name: "request_login",
      description:
        "Start signing in to Trinity with your email address. Sends a 6-digit " +
        "code to that address; call verify_login with the code to finish. " +
        "Use this when you have no API key configured.",
      parameters: z.object({
        email: z
          .string()
          .min(3)
          .max(254)
          .describe("The email address an agent has been shared with."),
      }),
      execute: async (
        args: { email: string },
        context?: { session?: McpAuthContext }
      ) => {
        const session = requireAnonymous(context?.session);
        const email = args.email.trim().toLowerCase();

        // Shape check only. Deliberately NOT a strict RFC validation: a
        // rejection here that the generic path would not produce is itself a
        // distinguishing signal.
        if (!email.includes("@") || email.startsWith("@") || email.endsWith("@")) {
          return GENERIC_REQUEST_RESPONSE;
        }

        const c = countersFor(session);
        c.requests += 1;
        if (c.requests > MAX_REQUESTS_PER_SESSION) {
          // Same generic body — a distinct "rate limited" reply would leak
          // that earlier attempts were well-formed.
          return GENERIC_REQUEST_RESPONSE;
        }

        // Remember the address so verify_login takes only the code. Pending is
        // an intent record, never proof: verification is backend-side.
        session.pendingEmail = email;

        try {
          await client.requestInlineLoginCode(email, session.sessionId);
        } catch (err) {
          // Swallow deliberately. Surfacing a backend error here (unknown
          // address, send failure, limiter) reopens the enumeration oracle the
          // generic response exists to close. The backend logs the real cause.
          console.log(
            `[#848] request_login relay failed (suppressed): ${
              err instanceof Error ? err.message : String(err)
            }`
          );
        }

        return GENERIC_REQUEST_RESPONSE;
      },
    },

    verifyLogin: {
      name: "verify_login",
      description:
        "Finish signing in to Trinity with the 6-digit code emailed to you by " +
        "request_login. On success this session can use the agents shared with " +
        "your address for as long as the connection stays open.",
      parameters: z.object({
        code: z
          .string()
          .regex(/^\d{6}$/, "The code is exactly 6 digits.")
          .describe("The 6-digit code from the email."),
        email: z
          .string()
          .optional()
          .describe(
            "Only needed if you did not call request_login on this connection."
          ),
      }),
      execute: async (
        args: { code: string; email?: string },
        context?: { session?: McpAuthContext }
      ) => {
        const session = requireAnonymous(context?.session);

        const email = (args.email ?? session.pendingEmail ?? "").trim().toLowerCase();
        if (!email) {
          return JSON.stringify(
            {
              status: "error",
              error: "no_pending_login",
              message:
                "Call request_login with your email address first, or pass email here.",
            },
            null,
            2
          );
        }

        const c = countersFor(session);
        c.verifies += 1;
        if (c.verifies > MAX_VERIFY_ATTEMPTS_PER_SESSION) {
          return JSON.stringify(
            {
              status: "error",
              error: "too_many_attempts",
              message:
                "Too many attempts on this connection. Reconnect and start again.",
            },
            null,
            2
          );
        }

        let result;
        try {
          result = await client.verifyInlineLoginCode(email, args.code, session.sessionId);
        } catch (err) {
          return JSON.stringify(
            {
              status: "error",
              error: "verification_failed",
              message: err instanceof Error ? err.message : String(err),
            },
            null,
            2
          );
        }

        if (!result?.verified) {
          // Uniform failure: never distinguish wrong-code from unknown-email.
          return JSON.stringify(
            {
              status: "error",
              error: "invalid_code",
              message: "That code is not valid or has expired. Request a new one.",
            },
            null,
            2
          );
        }

        // --- upgrade the session, in place ---------------------------------
        // Same object FastMCP holds, so every later tool call on this session
        // sees the binding. `scope` deliberately stays "anonymous": this
        // session still has no API key and must never satisfy `operatorOnly`.
        const agents = result.agents ?? [];
        session.verifiedEmail = email;
        session.userEmail = email;
        session.userId = result.username ?? email;
        // Convenience only — lets the connector tools default to the sole
        // agent and name the alternatives when there are several. Never the
        // authorization boundary; the backend re-gates every call.
        session.agents = agents.map((a) => a.name);
        delete session.pendingEmail;
        return JSON.stringify(
          {
            status: "ok",
            signed_in_as: email,
            agents: agents.map((a) => a.name),
            message:
              agents.length === 0
                ? "Signed in, but no agents are shared with this address yet. " +
                  "An owner needs to approve your access request."
                : agents.length === 1
                  ? `Signed in. You can use "${agents[0].name}" — try list_playbooks.`
                  : `Signed in. ${agents.length} agents are available; pass the ` +
                    `agent name to list_playbooks to pick one.`,
            note: "This sign-in lasts for this connection only.",
          },
          null,
          2
        );
      },
    },
  };
}
