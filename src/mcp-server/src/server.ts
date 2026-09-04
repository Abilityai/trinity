/**
 * Trinity MCP Server
 *
 * FastMCP server that exposes Trinity agent management and chat capabilities
 * via the Model Context Protocol (MCP).
 */

import { randomUUID } from "node:crypto";
import { FastMCP } from "fastmcp";
import { TrinityClient } from "./client.js";
import { createAgentTools } from "./tools/agents.js";
import { createChatTools } from "./tools/chat.js";
import { createSystemTools } from "./tools/systems.js";
import { createDocsTools } from "./tools/docs.js";
import { createSkillsTools } from "./tools/skills.js";
import { createRoomTools } from "./tools/rooms.js";  // ent#169 shared sessions
import { createScheduleTools } from "./tools/schedules.js";
import { createTagTools } from "./tools/tags.js";
import { createNotificationTools } from "./tools/notifications.js";
import { createReportTools } from "./tools/reports.js";
import { createSubscriptionTools } from "./tools/subscriptions.js";
import { createMonitoringTools } from "./tools/monitoring.js";
import { createNeverminedTools } from "./tools/nevermined.js";
import { createExecutionTools } from "./tools/executions.js";
import { createEventTools } from "./tools/events.js";
import { createChannelTools } from "./tools/channels.js";
import { createMessageTools } from "./tools/messages.js";
import { createVoiceReplyTools } from "./tools/voice.js";
import { createVoipTools } from "./tools/voip.js";
import { createFileTools } from "./tools/files.js";
import { createPipelineTools } from "./tools/pipelines.js";
import { createMemoryTools } from "./tools/memory.js";
import { createLoopTools } from "./tools/loops.js";
import { createReminderTools } from "./tools/reminders.js";
import { createOperatorQueueTools } from "./tools/operator_queue.js";
import { createConnectorTools } from "./tools/connector.js";
import { createAuthTools } from "./tools/auth.js";
import { createGitTools } from "./tools/git.js";
import { createA2ATools } from "./tools/a2a.js";
import { createA2ACallTools } from "./tools/a2a_call.js";
import { createCredentialVaultTools } from "./tools/credential_vault.js";
import { withAudit } from "./audit.js";
import { installLogRedaction } from "./log-redaction.js";
import type { McpAuthContext } from "./types.js";

export interface ServerConfig {
  name?: string;
  version?: `${number}.${number}.${number}`;
  trinityApiUrl?: string;
  trinityApiToken?: string;
  trinityUsername?: string;
  trinityPassword?: string;
  port?: number;
  requireApiKey?: boolean;
  /**
   * #946 pull pilot. When true, an agent→agent (scope='agent', non-self)
   * sequential chat_with_agent is routed through the durable async /task path
   * instead of the synchronous /chat. Read from MCP_AGENT_CHAT_PULL_ENABLED at
   * startup (mirrors requireApiKey ← MCP_REQUIRE_API_KEY). Default OFF.
   */
  agentChatPullEnabled?: boolean;
  /**
   * #848 inline email auth. When true, a request arriving with NO Authorization
   * header opens an anonymous sentinel session that may call request_login /
   * verify_login (and the connector tools, which refuse to act until an email
   * is verified). An INVALID key is still rejected. Read from
   * MCP_INLINE_AUTH_ENABLED at startup. Default OFF — this is a posture change
   * on a network-exposed port, so it is opt-in.
   */
  inlineAuthEnabled?: boolean;
}

export interface McpApiKeyValidationResult {
  valid: boolean;
  key_id?: string;      // MCP API key ID (AUDIT-001)
  user_id?: string;
  user_email?: string;
  key_name?: string;
  agent_name?: string;  // Agent name if scope is 'agent' or 'system'
  // Key scope, as the backend's free-text `mcp_api_keys.scope` column reports
  // it. #1854: kept in step with `McpAuthContext["scope"]` — declaring a
  // narrower set here (it named only user/agent/system) makes the cast below
  // launder two live values, which is how `portal_delegate` went unnoticed.
  scope?: McpAuthContext["scope"];
}

// ---------------------------------------------------------------------------
// Tool-visibility gates (ent#46 connector isolation + #848 inline auth)
// ---------------------------------------------------------------------------
//
// Exported at module scope ON PURPOSE so `tool-visibility.test.ts` can import
// the REAL predicates. They previously lived inside `createServer()` and the
// test carried a hand-copied mirror — which is the drift trap recorded in
// docs/memory/learnings.md (2026-07-16): a mirrored constant has no owner, and
// a test that pins its own copy pins the drift as the requirement. Importing is
// side-effect-free here (this module only *defines* `createServer`), so the
// mirror had no justification.
//
// These MUST be allow-lists, not "not connector" deny-checks. Two fastmcp
// behaviours make a deny-check fail OPEN. Both re-verified against the version
// this repo actually installs — `fastmcp@4.12.2` (package-lock), NOT the 4.4.0
// an earlier revision of this comment cited. Cited by SYMBOL first and line
// second: the minified chunk filename changes between releases (4.4.0
// chunk-MDIESGNI.js → 4.12.x chunk-5BQXF2VT.js), so a bare line number rots
// silently and invites reasoning about a version nobody runs.
//   1. `FastMCP#createSession` skips filtering entirely when auth is falsy —
//      `const allowedTools = auth ? this.#tools.filter(...) : this.#tools`
//      (4.12.2 dist/chunk-5BQXF2VT.js:1998-2000) — so a session with no auth
//      context gets EVERY registered tool and `canAccess` never runs. The guard
//      immediately above it (`:1994`, present since 4.4.0) does not help: it
//      rejects only an auth OBJECT carrying `authenticated: false`, so falsy
//      auth still short-circuits past the filter.
//   2. The stateful httpStream branch (the one we run — index.ts sets no
//      `stateless: true`) does NOT reject an `authenticate()` that returns
//      undefined (`:1924-1926`); only the stateless branch guards it
//      (`:1874-1878`). Today our callback throws instead of returning
//      undefined, so nothing is exposed — but a pre-login session (#848) is
//      exactly the change that would trip it.
// A deny-check also admits every scope it has not heard of. Widening the
// operator surface must therefore be a deliberate edit to this set.
//
// Enforcement is real, not advertisement-only — but NOT via a per-call
// `canAccess`, which is never re-invoked. `setupToolHandlers(tools)` (`:1180`)
// closes over `toolsMap = new Map(tools.map(...))` built from the FILTERED list
// (`:1181`), and the `CallToolRequestSchema` handler does `toolsMap.get(name)` →
// `throw new McpError(ErrorCode.MethodNotFound)` (`:1214-1220`). A filtered-out
// tool is absent from the call map, not merely hidden from `tools/list`.
// `tool-visibility.test.ts` pins that property so a future fastmcp regression to
// advertisement-only filtering fails the suite instead of silently un-gating.

/** Scopes that denote a fully-credentialed operator principal. */
export const OPERATOR_SCOPES: ReadonlySet<string> = new Set([
  "user",
  "agent",
  "system",
]);

/**
 * Operator-tool gate. Curried over `requireApiKey` because dev mode
 * (MCP_REQUIRE_API_KEY=false) installs no authenticate callback, so FastMCP
 * never calls `canAccess` at all and every tool is advertised; that branch only
 * guards a direct caller of the predicate. With auth required, an absent
 * context is never an operator.
 */
export const makeOperatorOnly =
  (requireApiKey: boolean) =>
  (auth: any): boolean => {
    if (auth === undefined || auth === null) return !requireApiKey;
    return OPERATOR_SCOPES.has((auth as { scope?: string }).scope ?? "");
  };

/** Connector-tier gate (ent#46): end-user consumption keys bound to one agent. */
export const connectorOnly = (auth: any): boolean => auth?.scope === "connector";

/** #848 pre-login sentinel sessions. */
export const anonymousOnly = (auth: any): boolean => auth?.scope === "anonymous";

/**
 * #848: the anonymous session's advertised tool list is deliberately IDENTICAL
 * before and after login. The connector tools are advertised to anonymous
 * sessions up front and refuse to act until `verifiedEmail` is set.
 *
 * The reason is NOT that the list cannot change — an earlier version of this
 * comment claimed FastMCP had "no per-session refresh API", and that is wrong.
 * `FastMCPSession.toolsListChanged` re-filters a LIVE session against its
 * current `#auth` and rebuilds the call map via `setupToolHandlers`
 * (fastmcp@4.12.2 dist/chunk-5BQXF2VT.js:661-666), fanned to every session by
 * addTool/addTools/removeTool/removeTools (`:1550`, `:1561`, `:1754`, `:1765` →
 * `#toolsListChanged` `:2479-2481`) — and Trinity triggers exactly that every
 * ~20s via the #846 exposed-agents reconciler.
 *
 * That makes a login-state-dependent gate WORSE, not impossible: visibility
 * would flip asynchronously whenever the reconciler happened to fire, so the
 * same session would show different tools depending on timing unrelated to the
 * login. A static surface is deterministic. Related trap: `updateAuth` REPLACES
 * `#auth` (`:685-687`) rather than mutating it, so if anything ever calls it the
 * in-place upgrade `verify_login` performs is silently discarded and the session
 * reverts to pre-login — another reason not to key visibility on that state.
 */
export const connectorOrAnonymous = (auth: any): boolean =>
  connectorOnly(auth) || anonymousOnly(auth);

// ---------------------------------------------------------------------------
// Anonymous session store (#2035) — the identity #848 writes has to survive
// ---------------------------------------------------------------------------
//
// #848 upgrades a session by mutating its auth context IN PLACE: `verify_login`
// writes `verifiedEmail` onto the object every tool call receives. That is
// sound WITHIN one request and false ACROSS requests, which is the whole of
// bug #2035 — a verified session was discarded before anything could read it.
//
// MCP streamable HTTP is discrete POSTs, one per tool call, all carrying the
// same `Mcp-Session-Id`. `mcp-proxy`'s `handleStreamRequest` (bundled in
// fastmcp@4.12.2) re-authenticates EVERY one of them and then reinstalls the
// result on the existing session:
//
//     authResult = await authenticate(req);   // every post, not just initialize
//     if (sessionId) { ...; server.updateAuth(authResult); }
//
// and `FastMCPSession#updateAuth` is `this.#auth = auth` (4.12.2
// dist/chunk-5BQXF2VT.js:685-687) — REPLACE, not merge. So a callback that
// returns a fresh object per request throws away the upgrade every time.
//
// The replace-not-merge behaviour was already noted above (see the
// `connectorOnly` block), but only as a reason not to key tool VISIBILITY on
// login state. The consequence for the login itself — that it is re-run per
// request, so the upgrade never survives — was not followed through.
//
// The fix is to return the SAME object for the same transport session, so
// `updateAuth` reinstalls it rather than replacing it.
//
// THREE CONSTRAINTS, each load-bearing:
//
//  1. ANONYMOUS TIER ONLY. A keyed session must keep re-validating its key on
//     every request. Memoizing those too would let `Mcp-Session-Id` alone
//     stand in for a key that was never presented — strictly worse than the
//     status quo, where the credential is on the wire every time. The call
//     site enforces this structurally: this store is consulted only inside the
//     no-Authorization-header branch.
//  2. TTL, not just eviction, and it is the ONLY bound — verified, not
//     assumed. fastmcp does emit `disconnect` carrying the session, and
//     `FastMCPSession` does expose a `sessionId` getter, so evicting on
//     disconnect reads as available; it is not. Probed against this tree: the
//     `connect` event fires with `sessionId` still unset (the id is copied off
//     the transport, which has not minted one at initialize), and `disconnect`
//     does not fire at all for a client that simply goes away — the SDK's
//     `StreamableHTTPClientTransport.close()` only aborts the SSE stream, and
//     the DELETE that would close the server transport is sent solely by the
//     opt-in `terminateSession()`. A quitting client leaves no signal.
//     There is also no `logout` tool by decision (#2035), so nothing ends a
//     session early. Idle expiry therefore bounds how long an abandoned or
//     leaked id stays usable, and the absolute cap bounds one kept warm
//     deliberately — and both must be sized as if they were the only defence,
//     because they are.
//  3. BOUNDED. One entry per anonymous connection, unfreeable on disconnect,
//     would otherwise be retained for the process lifetime and an
//     unauthenticated caller could exhaust memory just by reconnecting in a
//     loop. Oldest-first, mirroring `tools/auth.ts`.
//
// SECURITY POSTURE, stated plainly: this makes `Mcp-Session-Id` bearer-
// equivalent for keyless sessions — whoever presents a live one is handed that
// session's verified identity. It is a 122-bit v4 UUID minted by the
// transport, so it is unguessable, but it must now be treated as a CREDENTIAL
// rather than a routing detail: do not log it, do not display it, do not put
// it in an error body. Note this store deliberately keys on the transport id
// while `McpAuthContext.sessionId` stays an independent random id — the
// correlation id that appears in logs and rate-limit keys is therefore NOT the
// credential. Trinity's own code holds that line, but the bundled `mcp-proxy`
// prints the raw id on three paths; `log-redaction.ts` truncates it at the
// console boundary and guards the assumption that keeps that narrow.
// Operator-facing consequence: the id also rides the wire as a plain header,
// so the keyless tier must be exposed over TLS only (docs/memory/requirements/
// mcp.md §7.6).
//
// What bounds the damage: #848 re-gates AUTHORIZATION on the backend for every
// call (`email_has_agent_access` + connector-enabled). Only authentication is
// remembered here, so a stolen session confers the asserted identity, not
// frozen permissions — unshare the agent and the very next call 403s.
//
// Accepted by @obasilakis on #2035, 2026-08-06, as the option that preserves
// the #848 sign-off's "session, not a minted key" constraint. The alternative
// (an opaque token returned by `verify_login`) puts a credential in the
// CALLING MODEL's context window, because MCP gives a tool no way to make a
// client attach a header to later requests.

/** Idle expiry. A session in active use refreshes; a leaked id dies. */
export const ANON_SESSION_IDLE_MS = 30 * 60 * 1000;
/**
 * Hard ceiling regardless of activity, so a warmed session still expires.
 *
 * 4h, not the 8h this shipped with: #2035 condition 2 asks for "hours, not
 * days" and to err aggressive, and constraint 2 above is why — nothing else
 * ends a session, so this number IS the exposure window for a leaked id on a
 * client that stays connected all day. Re-authenticating costs one email code.
 * Lowering it is always safe; raising it weakens every keyless session at once,
 * so `inline-auth-session.test.ts` pins the DIRECTION rather than the value.
 */
export const ANON_SESSION_MAX_MS = 4 * 60 * 60 * 1000;
/** Retained-entry cap. Insertion-ordered Map ⇒ the first key is the oldest. */
export const MAX_ANON_SESSIONS = 10_000;

interface AnonSessionEntry {
  ctx: McpAuthContext;
  createdAt: number;
  lastSeen: number;
}

export interface AnonymousSessionStore {
  /**
   * The anonymous context for this transport session, creating one on first
   * sight. Returns the SAME object for the same id so an in-place upgrade
   * survives `updateAuth`.
   *
   * `undefined` id ⇒ always a fresh, unstored context. That is the `initialize`
   * request, which precedes session-id assignment; nothing can have signed in
   * yet, so there is nothing to preserve.
   */
  resolve(transportSessionId: string | undefined, now?: number): McpAuthContext;
  /** Live entry count. For tests and diagnostics. */
  size(): number;
}

export function createAnonymousSessionStore(
  opts: { idleMs?: number; maxMs?: number; maxEntries?: number } = {}
): AnonymousSessionStore {
  const idleMs = opts.idleMs ?? ANON_SESSION_IDLE_MS;
  const maxMs = opts.maxMs ?? ANON_SESSION_MAX_MS;
  const maxEntries = opts.maxEntries ?? MAX_ANON_SESSIONS;
  const entries = new Map<string, AnonSessionEntry>();

  const freshContext = (): McpAuthContext => ({
    userId: "anonymous",
    keyName: "anonymous",
    scope: "anonymous",
    // Independent of the transport id ON PURPOSE — see the note above. This is
    // the value that reaches logs and rate-limit keys, so it must not be the
    // one that grants access.
    sessionId: randomUUID(),
  });

  const isLive = (e: AnonSessionEntry, now: number): boolean =>
    now - e.lastSeen < idleMs && now - e.createdAt < maxMs;

  return {
    resolve(transportSessionId, now = Date.now()) {
      if (!transportSessionId) return freshContext();

      const existing = entries.get(transportSessionId);
      if (existing) {
        if (isLive(existing, now)) {
          existing.lastSeen = now;
          return existing.ctx;
        }
        // Expired: drop it and fall through to a fresh, pre-login context.
        // Never resurrect — an expired session must re-authenticate.
        entries.delete(transportSessionId);
      }

      // Opportunistic sweep: no timer, so expiry is enforced on access. Bounded
      // by the cap below, so this stays cheap.
      for (const [id, e] of entries) {
        if (!isLive(e, now)) entries.delete(id);
      }
      while (entries.size >= maxEntries) {
        const oldest = entries.keys().next();
        if (oldest.done) break;
        entries.delete(oldest.value);
      }

      const ctx = freshContext();
      entries.set(transportSessionId, { ctx, createdAt: now, lastSeen: now });
      return ctx;
    },
    size: () => entries.size,
  };
}

/** Node lowercases header names; a repeated header arrives as an array. */
export function readHeader(value: string | string[] | undefined): string | undefined {
  if (Array.isArray(value)) return value[0];
  return value;
}

/**
 * Validate an MCP API key against the Trinity backend
 */
async function validateMcpApiKey(
  trinityApiUrl: string,
  apiKey: string
): Promise<McpApiKeyValidationResult | null> {
  try {
    const response = await fetch(`${trinityApiUrl}/api/mcp/validate`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey}`,
      },
    });

    if (response.ok) {
      return (await response.json()) as McpApiKeyValidationResult;
    }
    return null;
  } catch (error) {
    console.error("Failed to validate MCP API key:", error);
    return null;
  }
}

/**
 * Create and configure the Trinity MCP Server
 */
export async function createServer(config: ServerConfig = {}) {
  // #2035 condition 1. Installed FIRST, before anything in this function can
  // write, and unconditionally — not gated on `inlineAuthEnabled`, because a
  // log format that changes with a feature flag is one an operator learns
  // wrong. Truncating an id nobody treats as a secret costs nothing; the
  // reverse mistake writes live credentials to disk. See log-redaction.ts.
  installLogRedaction();

  const {
    name = "trinity-orchestrator",
    version = "1.0.0" as const,
    trinityApiUrl = process.env.TRINITY_API_URL || "http://localhost:8000",
    trinityApiToken = process.env.TRINITY_API_TOKEN,
    trinityUsername = process.env.TRINITY_USERNAME || "admin",
    trinityPassword = process.env.TRINITY_PASSWORD,
    port = parseInt(process.env.MCP_PORT || "8080", 10),
    requireApiKey = process.env.MCP_REQUIRE_API_KEY === "true",
    // #946 pull pilot — default OFF. Routing gate for agent→agent chat (see
    // ServerConfig.agentChatPullEnabled). Same env key the backend declares in
    // config.py (MCP_AGENT_CHAT_PULL_ENABLED) so a single-.env deploy can't drift.
    agentChatPullEnabled = process.env.MCP_AGENT_CHAT_PULL_ENABLED === "true",
    // #848 inline email auth — default OFF. When off, a request with no
    // Authorization header is rejected exactly as before and no session is
    // created. When on, it yields an anonymous sentinel session that may only
    // reach the inline-auth tools until verify_login upgrades it.
    inlineAuthEnabled = process.env.MCP_INLINE_AUTH_ENABLED === "true",
  } = config;

  // Create Trinity API client (base URL only)
  // When requireApiKey is true, tools will create per-request clients with user's MCP API key
  // No admin authentication needed - backend validates MCP API keys directly
  const client = new TrinityClient(trinityApiUrl);

  if (requireApiKey) {
    console.log("MCP API Key authentication: ENABLED (per-request validation)");
    console.log("No admin credentials needed - all requests use user's MCP API key");
  } else {
    // Only authenticate if API key auth is disabled (backward compatibility)
    if (trinityApiToken) {
      console.log("Using provided API token for authentication");
      client.setToken(trinityApiToken);
    } else {
      // Issue #692: refuse to start with no usable backend credential rather
      // than silently fall back to the well-known "changeme" password.
      if (!trinityPassword) {
        throw new Error(
          "MCP server has no usable backend credential: MCP_REQUIRE_API_KEY=false, " +
          "TRINITY_API_TOKEN unset, and TRINITY_PASSWORD unset. " +
          "Either enable API-key mode (MCP_REQUIRE_API_KEY=true) or set ADMIN_PASSWORD/TRINITY_PASSWORD in .env."
        );
      }
      console.log(`Authenticating with Trinity API as '${trinityUsername}'...`);
      try {
        await client.authenticate(trinityUsername, trinityPassword);
        console.log("Authentication successful");
      } catch (error) {
        console.error("Authentication failed:", error);
        throw error;
      }
    }
  }

  // Verify connection (health endpoint doesn't require auth)
  try {
    const health = await client.healthCheck();
    console.log(`Trinity API healthy: ${JSON.stringify(health)}`);
  } catch (error) {
    console.warn("Health check failed (non-critical):", error);
  }

  // #2035: per-server-instance, so a restart drops every keyless session (they
  // hold no credential to re-present anyway — signing in again is the
  // documented cost of "session, not a minted key").
  const anonymousSessions = createAnonymousSessionStore();

  // Create FastMCP server with authentication if required
  // Note: FastMCP authenticate must return Record<string, unknown> | undefined, not boolean
  const server = new FastMCP({
    name,
    version,
    authenticate: requireApiKey
      ? async (request) => {
          // Extract API key from Authorization header (http.IncomingMessage uses lowercase headers object)
          const authHeader = request.headers["authorization"] as string | undefined;
          if (!authHeader || !authHeader.startsWith("Bearer ")) {
            // #848: an ABSENT credential is an invitation to log in inline; an
            // INVALID one (below) stays an error. Gated OFF by default, in
            // which case this is the pre-#848 rejection, unchanged.
            if (inlineAuthEnabled) {
              // MUST be a truthy object: fastmcp@4.12.2 skips canAccess
              // filtering entirely for falsy auth, which would hand this
              // session every registered tool. Must also NOT carry
              // `authenticated: false`, which fastmcp treats as a rejection.
              //
              // #2035: resolved through the store, NOT constructed here, so the
              // SAME object comes back for the same transport session and the
              // in-place upgrade `verify_login` performs survives `updateAuth`
              // (which replaces rather than merges — see the store's note).
              // Reached only on the no-Authorization-header path, which is what
              // keeps the memo anonymous-tier-only.
              const anon = anonymousSessions.resolve(
                readHeader(request.headers["mcp-session-id"])
              );
              // Logs the independent correlation id, never the transport
              // session id — under #2035 the latter is bearer-equivalent.
              console.log(`MCP anonymous session (#848 inline auth): ${anon.sessionId}`);
              return anon;
            }
            console.log("MCP request rejected: Missing or invalid Authorization header");
            throw new Error("Missing or invalid Authorization header");
          }

          const apiKey = authHeader.substring(7);

          // Validate against Trinity backend
          const result = await validateMcpApiKey(trinityApiUrl, apiKey);

          if (result && result.valid) {
            const scope = result.scope || "user";
            const scopeLabel = scope === "system" ? "SYSTEM (full access)" : scope;
            console.log(
              `MCP request authenticated: user=${result.user_id}, key=${result.key_name}, scope=${scopeLabel}, agent=${result.agent_name || "n/a"}`
            );
            // Return auth context object (FastMCP stores this in session and passes to tools)
            // Includes agent info for agent-to-agent collaboration
            // System scope agents have full access to all agents (Phase 11.1)
            const authContext: McpAuthContext = {
              userId: result.user_id || "unknown",
              userEmail: result.user_email,
              keyId: result.key_id,  // MCP API key ID (AUDIT-001)
              keyName: result.key_name || "unknown",
              agentName: result.agent_name,  // Agent name if scope is 'agent' or 'system'
              // #1854: keep this cast in step with the McpAuthContext union —
              // `mcp_api_keys.scope` is free-text with no CHECK constraint, so a
              // value the union does not name arrives here as a plain string.
              scope: scope as McpAuthContext["scope"],
              mcpApiKey: apiKey,  // Store the actual API key for user-scoped requests
            };
            return authContext;
          }

          console.log("MCP request rejected: Invalid API key");
          throw new Error("Invalid API key");
        }
      : undefined,
  });

  console.log(`MCP API Key authentication: ${requireApiKey ? "ENABLED" : "DISABLED"}`);
  // #946 pilot — surface the routing mode at startup so the soak's control vs
  // treatment window is unambiguous in the logs.
  console.log(`Agent→agent chat pull routing (#946): ${agentChatPullEnabled ? "ON (async /task)" : "OFF (sync /chat)"}`);
  // #848 — a keyless session tier is a posture change; make it unambiguous in
  // the startup log which mode the server came up in.
  console.log(
    `Inline email auth (#848): ${
      inlineAuthEnabled
        ? "ON (keyless sessions may call request_login/verify_login)"
        : "OFF (a request without an API key is rejected)"
    }`
  );

  // #846: every registered tool name (built-in + dynamic) — the dynamic-tool
  // reconciler uses the built-in set as a final collision guard.
  const builtinToolNames = new Set<string>();

  // SEC-001 Phase 3: Wrap tool execute functions with audit logging.
  // withAudit captures tool name, auth context, timing, and success/failure,
  // then fires a non-blocking POST to the backend internal audit endpoint.
  // #846: auditTargetId binds the audited target for tools whose params carry
  // no agent_name (the dedicated chat_with_<slug> tools).
  function addToolWithAudit(
    tool: any,
    canAccess?: (auth: any) => boolean,
    auditTargetId?: string
  ): void {
    const wrapped: any = {
      ...tool,
      execute: withAudit(tool.name, tool.execute, auditTargetId),
    };
    // ent#46: per-auth tool visibility. FastMCP filters the advertised tool
    // list per session by canAccess(authContext). A tool's own canAccess (if
    // any) wins; otherwise we apply the group default.
    if (canAccess && wrapped.canAccess === undefined) {
      wrapped.canAccess = canAccess;
    }
    server.addTool(wrapped);
  }

  // Helper to register all tools from a tool group with audit wrapping
  function addAllTools(tools: Record<string, any>, canAccess?: (auth: any) => boolean): void {
    for (const tool of Object.values(tools)) {
      addToolWithAudit(tool, canAccess);
      builtinToolNames.add((tool as any).name);
    }
  }

  const operatorOnly = makeOperatorOnly(requireApiKey);

  // Build tool groups once, then register + count (SEC-001 Phase 3).
  const toolGroups: Record<string, any>[] = [
    createAgentTools(client, requireApiKey),
    createChatTools(client, requireApiKey, agentChatPullEnabled),
    createSystemTools(client, requireApiKey),
    createDocsTools(),
    createSkillsTools(client, requireApiKey),
    createScheduleTools(client, requireApiKey),
    createTagTools(client, requireApiKey),
    createNotificationTools(client, requireApiKey),
    createReportTools(client, requireApiKey),     // Agent Reports (#918)
    createFileTools(client, requireApiKey),       // FILES-001 — outbound file sharing
    createPipelineTools(client, requireApiKey),   // #919 — agent-defined pipeline introspection
    createSubscriptionTools(client, requireApiKey),
    createMonitoringTools(client, requireApiKey),
    createNeverminedTools(client, requireApiKey),
    createExecutionTools(client, requireApiKey),
    createEventTools(client, requireApiKey),
    createChannelTools(client, requireApiKey),
    createMessageTools(client, requireApiKey),
    createVoiceReplyTools(client, requireApiKey), // send_voice_reply — per-message voice (ent#117)
    createMemoryTools(client, requireApiKey),     // MEM-001 write path (#888)
    createLoopTools(client, requireApiKey),       // Sequential agent loops (#740)
    createReminderTools(client, requireApiKey),   // Agent self-reminders (#1296)
    createVoipTools(client, requireApiKey),       // VoIP telephony — call_user (VOIP-001, #1056)
    createOperatorQueueTools(client, requireApiKey), // Operator queue read + respond (OPS-001, #1101/#1104)
    createGitTools(client, requireApiKey),           // Direct git status/sync/log/pull/sync-state/reset (#905)
    createRoomTools(client, requireApiKey),          // Shared sessions / rooms (ent#169)
    createA2ATools(client, requireApiKey),           // A2A control plane — exposure/card/allow-list/endpoints (ent#160)
    createA2ACallTools(client, requireApiKey),       // A2A runtime — outbound call_a2a_agent / get_a2a_task (#736)
    createCredentialVaultTools(client, requireApiKey), // Credential vault runtime — list/fetch (license-blind proxy, ent#279)
  ];
  // Operator tools: visible ONLY to fully-credentialed operator scopes.
  for (const group of toolGroups) {
    addAllTools(group, operatorOnly);
  }
  // Connector tools (ent#46): connector keys, plus #848 anonymous sessions
  // (which they refuse to serve until verify_login binds an email).
  const connectorGroup = createConnectorTools(client, requireApiKey);
  addAllTools(connectorGroup, inlineAuthEnabled ? connectorOrAnonymous : connectorOnly);

  // #848 inline-auth tools: registered only when the feature is on, and
  // visible only to anonymous sessions (a key-bearing session has no use for
  // them). Each tool re-checks the scope at execute time — canAccess governs
  // advertisement, not authorization.
  const authGroup = inlineAuthEnabled ? createAuthTools(client, requireApiKey) : {};
  if (inlineAuthEnabled) {
    addAllTools(authGroup, anonymousOnly);
  }

  const totalTools =
    toolGroups.reduce((sum, g) => sum + Object.keys(g).length, 0) +
    Object.keys(connectorGroup).length +
    Object.keys(authGroup).length;
  console.log(`Registered ${totalTools} tools with audit wrapping (SEC-001 Phase 3)`);

  // #846: dynamic-tool registration handles. The exposed-agents reconciler
  // (index.ts) drives these — it NEVER calls server.addTool directly, so the
  // audit + canAccess wrapping is applied uniformly. addTool/removeTool after
  // server.start() fan a `notifications/tools/list_changed` to live sessions.
  const dynamicToolNames = new Set<string>();
  function registerDynamicTool(
    tool: any,
    canAccess: (auth: any) => boolean,
    auditTargetId: string
  ): void {
    addToolWithAudit(tool, canAccess, auditTargetId);
    dynamicToolNames.add(tool.name);
  }
  function unregisterDynamicTool(name: string): void {
    if (!dynamicToolNames.has(name)) return;
    try {
      // removeTool(name) is public FastMCP 4.x API and triggers list_changed.
      (server as any).removeTool(name);
    } catch (e) {
      console.error(`[#846] removeTool('${name}') failed:`, e);
    }
    dynamicToolNames.delete(name);
  }

  return {
    server,
    port,
    client,
    requireApiKey,
    agentChatPullEnabled,
    trinityApiUrl,
    operatorOnly,
    builtinToolNames,
    registerDynamicTool,
    unregisterDynamicTool,
  };
}
