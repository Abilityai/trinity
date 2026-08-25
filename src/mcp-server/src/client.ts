/**
 * Trinity API Client
 *
 * Typed client for communicating with the Trinity Backend API.
 */

import type {
  Agent,
  AgentConfig,
  ChatResponse,
  ChatMessage,
  Template,
  TokenResponse,
  AgentAccessInfo,
  SshAccessResponse,
  AgentTemplateInfo,
  Schedule,
  ScheduleCreate,
  ScheduleUpdate,
  ScheduleExecution,
  ScheduleToggleResult,
  ScheduleTriggerResult,
  ActivityTimelineResponse,
  OperatorQueueItem,
  OperatorQueueListResponse,
  CompatibilityReport,
  AgentFileTreeResponse,
  ReportSummary,
} from "./types.js";

/**
 * Debug logging utility - only logs in development mode
 * Set DEBUG_MCP_CLIENT=true or NODE_ENV=development to enable
 */
const DEBUG = process.env.DEBUG_MCP_CLIENT === 'true' || process.env.NODE_ENV === 'development';

function debugLog(...args: any[]) {
  if (DEBUG) {
    console.log('[DEBUG]', ...args);
  }
}

/**
 * Typed error thrown by `request()` on a non-2xx response (#905). Carries the
 * HTTP status and the conflict-classification headers the git endpoints set
 * (`X-Conflict-Type`/`X-Conflict-Class`), so a tool can branch on the conflict
 * type (e.g. "use chat_with_agent") without parsing the free-form detail string.
 *
 * Backward-compatible: `.message` keeps the historical `API error (<status>): <body>`
 * shape, so existing callers/tests that match on the message still work, and
 * `instanceof Error` holds.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly conflictType?: string;
  readonly conflictClass?: string;
  /**
   * The raw response body, retained verbatim (ent#443).
   *
   * The message already embeds it, but a caller that needs to tell a SERVING
   * module's own refusal from the ABSENCE of that module has to parse it —
   * a served refusal authors `detail: {code, …}` while absence is a plain
   * string (FastAPI's own "Not Found", or an entitlement gate's sentence).
   * Re-deriving the body by regex off the message is the kind of thing that
   * breaks the day the message format changes, so it is kept as a field.
   */
  readonly body: string;

  constructor(status: number, body: string, headers?: Headers) {
    super(`API error (${status}): ${body}`);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    this.conflictType = headers?.get("X-Conflict-Type") ?? undefined;
    this.conflictClass = headers?.get("X-Conflict-Class") ?? undefined;
  }
}

/**
 * #914 pure matcher for the chat-timeout recovery lookup. Extracted from
 * `TrinityClient.findRecentMcpExecution` so a unit test can drive it
 * without spinning up a real backend.
 *
 * Selects the newest execution row that:
 *   - is in a non-terminal status (`pending`, `queued`, `running`)
 *   - was triggered via MCP (`triggered_by === "mcp"` or `"agent"`)
 *   - carries the calling key's `source_mcp_key_id` when one was supplied
 *     (rows with no key id pass — older backends or pre-AUDIT-001 rows)
 *   - started within the last `windowMs` (default 30s, covers the typical
 *     MCP gateway abort + a small clock-skew buffer)
 *
 * Returns `undefined` when nothing matches; caller falls back to a
 * clearer error message instead of returning a wrong execution_id.
 */
export function pickRecentMcpExecution(
  executions: ScheduleExecution[],
  opts: { mcpKeyId?: string; now?: number; windowMs?: number } = {},
): ScheduleExecution | undefined {
  const now = opts.now ?? Date.now();
  const windowMs = opts.windowMs ?? 30_000;
  const cutoffMs = now - windowMs;
  const nonTerminal = new Set(["pending", "queued", "running"]);
  const matches = executions.filter((e) => {
    if (!nonTerminal.has(e.status)) return false;
    if (e.triggered_by !== "mcp" && e.triggered_by !== "agent") return false;
    if (opts.mcpKeyId && e.source_mcp_key_id && e.source_mcp_key_id !== opts.mcpKeyId) {
      return false;
    }
    const started = Date.parse(e.started_at);
    if (Number.isNaN(started) || started < cutoffMs) return false;
    return true;
  });
  // Newest first — backend returns DESC by started_at, but sort defensively
  // in case the contract drifts.
  matches.sort((a, b) => Date.parse(b.started_at) - Date.parse(a.started_at));
  return matches[0];
}

/** Bound for #848 inline-auth control-plane calls (not chat). */
const INLINE_AUTH_TIMEOUT_MS = Number(process.env.MCP_INLINE_AUTH_TIMEOUT_MS || 15000);

export class TrinityClient {
  private baseUrl: string;
  private token?: string;
  private username?: string;
  private password?: string;

  constructor(baseUrl: string = "http://localhost:8000", token?: string) {
    this.baseUrl = baseUrl.replace(/\/$/, ""); // Remove trailing slash
    this.token = token;
  }

  /**
   * Authenticate with the Trinity API using username/password
   * Stores credentials for automatic re-authentication on token expiry
   */
  async authenticate(username: string, password: string): Promise<void> {
    // Store credentials for re-authentication
    this.username = username;
    this.password = password;

    const formData = new URLSearchParams();
    formData.append("username", username);
    formData.append("password", password);

    const response = await fetch(`${this.baseUrl}/token`, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body: formData,
    });

    // #2322 — parse BEFORE the status check. A login deferred by a second
    // factor answers 403 carrying `mfa_required`; reading only `statusText`
    // would report a bare "Forbidden" for the one failure with a specific,
    // actionable cause.
    const data = (await response
      .json()
      .catch(() => ({}))) as TokenResponse;

    if (data.mfa_required) {
      throw new Error(
        "Authentication failed: a second factor is required, which this client " +
          "cannot complete. Use a Trinity MCP API key instead of password auth.",
      );
    }

    if (!response.ok) {
      throw new Error(`Authentication failed: ${response.statusText}`);
    }

    // Belt for any 2xx that carries no session: a status code is not proof a
    // session was issued, and checking only `response.ok` is what stored
    // `undefined` here and turned a login refusal into unexplained 401s on
    // every later call.
    if (!data.access_token) {
      throw new Error("Authentication failed: the server returned no access token");
    }

    this.token = data.access_token;
  }

  /**
   * Re-authenticate using stored credentials
   */
  private async reauthenticate(): Promise<boolean> {
    if (!this.username || !this.password) {
      return false;
    }
    try {
      await this.authenticate(this.username, this.password);
      return true;
    } catch {
      return false;
    }
  }

  /**
   * Set the authentication token directly
   */
  setToken(token: string): void {
    this.token = token;
  }

  /**
   * Get the base URL for creating new client instances
   */
  getBaseUrl(): string {
    return this.baseUrl;
  }

  /**
   * Shared transport: auth header + single 401-reauth-retry + error mapping,
   * returning the raw `Response` so callers choose how to decode the body
   * (#919: the JSON `request` path and the plain-text `downloadAgentFile`
   * path differ only in `.json()` vs `.text()` — they must not duplicate the
   * auth/retry/error logic).
   */
  private async _fetch(
    method: string,
    path: string,
    body?: unknown,
    isRetry: boolean = false,
    requestId?: string,
    extraHeaders?: Record<string, string>
  ): Promise<Response> {
    if (!this.token) {
      throw new Error("Not authenticated. Call authenticate() first or setToken().");
    }

    const headers: Record<string, string> = {
      Authorization: `Bearer ${this.token}`,
    };

    if (body) {
      headers["Content-Type"] = "application/json";
    }

    // #1970: per-call attribution headers (X-Source-Agent / X-MCP-Key-*).
    // Authorization is skipped explicitly: this parameter exists to add
    // metadata, and letting it replace the bearer token would turn an audit
    // convenience into an auth-substitution surface.
    if (extraHeaders) {
      for (const [key, value] of Object.entries(extraHeaders)) {
        if (key.toLowerCase() === "authorization") continue;
        headers[key] = value;
      }
    }

    // #905: forward a caller-supplied correlation id. The backend's
    // add_request_id middleware adopts an incoming X-Request-ID, so the
    // server-side audit row (e.g. git_operation) carries the SAME id the MCP
    // tool stamps on its own mcp_operation audit row — making the two joinable.
    if (requestId) {
      headers["X-Request-ID"] = requestId;
    }

    // Security: Log requests without exposing tokens in production
    // In development, token presence is logged for debugging; in production, only basic info
    if (DEBUG) {
      debugLog(`[CLIENT] ${method} ${path} - Auth: ${this.token ? 'present' : 'missing'}`);
    }

    const response = await fetch(`${this.baseUrl}${path}`, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
    });

    // Handle 401 - attempt re-authentication once
    if (response.status === 401 && !isRetry) {
      console.log("Token expired, attempting re-authentication...");
      const success = await this.reauthenticate();
      if (success) {
        console.log("Re-authentication successful, retrying request...");
        return this._fetch(method, path, body, true, requestId, extraHeaders);
      }
    }

    if (!response.ok) {
      const error = await response.text();
      // #905: typed error so callers can branch on .status / .conflictType
      // without parsing the message. Message shape is unchanged for back-compat.
      throw new ApiError(response.status, error, response.headers);
    }

    return response;
  }

  /**
   * Public request method for custom API calls
   */
  // --- Shared sessions / rooms (ent#169) ------------------------------------
  // A room is a shared persistent RECORD; membership is the grant, so an
  // agent-scoped key reaches exactly the rooms its agent belongs to and the
  // backend answers a uniform 404 otherwise.

  async createRoom(body: {
    name: string;
    agents: string[];
    topic?: string;
    max_messages?: number;
    max_cost_usd?: number;
    ttl_hours?: number;
    scribe?: string;
  }): Promise<any> {
    return this.request("POST", "/api/rooms", body);
  }

  async listRooms(): Promise<{ rooms: any[] }> {
    return this.request("GET", "/api/rooms");
  }

  async readRoom(roomId: string, since = 0): Promise<any> {
    return this.request(
      "GET",
      `/api/rooms/${encodeURIComponent(roomId)}?since=${encodeURIComponent(String(since))}`
    );
  }

  async postToRoom(roomId: string, content: string): Promise<any> {
    return this.request("POST", `/api/rooms/${encodeURIComponent(roomId)}/messages`, {
      content,
    });
  }

  async closeRoom(roomId: string, reason?: string): Promise<any> {
    return this.request("POST", `/api/rooms/${encodeURIComponent(roomId)}/close`, { reason });
  }

  async request<T>(
    method: string,
    path: string,
    body?: unknown,
    isRetry: boolean = false,
    requestId?: string,
    extraHeaders?: Record<string, string>
  ): Promise<T> {
    const response = await this._fetch(method, path, body, isRetry, requestId, extraHeaders);

    // Handle 204 No Content (e.g., successful DELETE)
    if (response.status === 204) {
      return undefined as T;
    }

    // Check content type - if text/plain or text/yaml, return as string
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("text/plain") || contentType.includes("text/yaml") || contentType.includes("application/x-yaml")) {
      return (await response.text()) as T;
    }

    return (await response.json()) as T;
  }

  // ============================================================================
  // Agent Management
  // ============================================================================

  /**
   * List all agents in the Trinity platform
   */
  async listAgents(): Promise<Agent[]> {
    return this.request<Agent[]>("GET", "/api/agents");
  }

  /**
   * Get a specific agent by name
   */
  async getAgent(name: string): Promise<Agent> {
    return this.request<Agent>("GET", `/api/agents/${encodeURIComponent(name)}`);
  }

  /**
   * Get agent access information (owner, sharing status)
   * Used for agent-to-agent collaboration access control
   */
  async getAgentAccessInfo(name: string): Promise<AgentAccessInfo | null> {
    try {
      // The get_agent endpoint returns owner and is_shared fields
      const agent = await this.request<Agent & { owner?: string; is_shared?: boolean }>(
        "GET",
        `/api/agents/${encodeURIComponent(name)}`
      );
      return {
        name: agent.name,
        owner: agent.owner || "unknown",
        is_shared: agent.is_shared || false,
      };
    } catch {
      return null;
    }
  }

  /**
   * Get agent template info (full metadata from template.yaml)
   * Returns detailed information about the agent's capabilities, commands, etc.
   */
  async getAgentInfo(name: string): Promise<AgentTemplateInfo> {
    return this.request<AgentTemplateInfo>(
      "GET",
      `/api/agents/${encodeURIComponent(name)}/info`
    );
  }

  /**
   * Get the playbooks an agent's connector exposes (ent#46).
   * The backend enforces the allow-list + the user_invocable exclusion, so
   * this returns only what the connector is permitted to advertise as tools.
   */
  async getConnectorPlaybooks(
    name: string
  ): Promise<Array<{ name: string; description?: string; argument_hint?: string; automation?: string }>> {
    return this.request(
      "GET",
      `/api/agents/${encodeURIComponent(name)}/connector/playbooks`
    );
  }

  /**
   * Skills the calling agent is permitted to execute on the skill runner
   * (ent#139). The acting agent is resolved server-side from the API key, so
   * an agent-scoped key can never ask on another agent's behalf.
   * Returns `enabled: false` with an empty list when the feature is off.
   */
  async getRunnableSkills(): Promise<{
    caller_agent: string;
    enabled: boolean;
    skills: Array<{ name: string; description?: string; version?: string }>;
  }> {
    return this.request("GET", "/api/enterprise/skill-runner/available");
  }

  /**
   * Execute one library skill on the skill runner (ent#139). Server-side the
   * per-skill allow-list is re-checked at dispatch — this client never decides
   * what is runnable.
   */
  async runSkill(
    skillName: string,
    input?: string
  ): Promise<{ skill: string; result: string; cost?: number; execution_id?: string }> {
    return this.request("POST", "/api/enterprise/skill-runner/run", {
      skill_name: skillName,
      input,
    });
  }

  /**
   * Get the agent compatibility report (#668).
   * STATIC checks recompute live; pass includeAi=true to force a fresh AI
   * evaluation (otherwise the last persisted AI verdicts are returned).
   */
  async getAgentCompatibilityReport(
    name: string,
    includeAi: boolean = true
  ): Promise<CompatibilityReport> {
    return this.request<CompatibilityReport>(
      "GET",
      `/api/agents/${encodeURIComponent(name)}/compatibility?include_ai=${includeAi}`
    );
  }

  /**
   * Get permitted agents for a source agent (Phase 9.10)
   * Returns list of agent names that the source agent can communicate with
   */
  async getPermittedAgents(sourceAgent: string): Promise<string[]> {
    try {
      const response = await this.request<{ permitted_agents: Array<{ name: string }> }>(
        "GET",
        `/api/agents/${encodeURIComponent(sourceAgent)}/permissions`
      );
      return response.permitted_agents.map((a) => a.name);
    } catch {
      return [];
    }
  }

  /**
   * Check if source agent is permitted to call target agent (Phase 9.10)
   */
  async isAgentPermitted(sourceAgent: string, targetAgent: string): Promise<boolean> {
    const permitted = await this.getPermittedAgents(sourceAgent);
    return permitted.includes(targetAgent);
  }

  /**
   * Create a new agent
   *
   * RELIABILITY-006 (#525): forward idempotency key so a transport-level retry
   * of the same create replays the original response instead of colliding on
   * the agent name.
   */
  async createAgent(config: AgentConfig, idempotencyKey?: string): Promise<Agent> {
    const headers: Record<string, string> = {};
    if (idempotencyKey) {
      headers["Idempotency-Key"] = idempotencyKey;
    }
    return this.request<Agent>("POST", "/api/agents", config, false, undefined, headers);
  }

  /**
   * Delete an agent
   */
  async deleteAgent(name: string): Promise<{ message: string }> {
    return this.request<{ message: string }>(
      "DELETE",
      `/api/agents/${encodeURIComponent(name)}`
    );
  }

  /**
   * Start a stopped agent
   */
  async startAgent(name: string): Promise<{ message: string }> {
    return this.request<{ message: string }>(
      "POST",
      `/api/agents/${encodeURIComponent(name)}/start`
    );
  }

  /**
   * Stop a running agent
   */
  async stopAgent(name: string): Promise<{ message: string }> {
    return this.request<{ message: string }>(
      "POST",
      `/api/agents/${encodeURIComponent(name)}/stop`
    );
  }

  /**
   * Rename an agent
   */
  async renameAgent(
    name: string,
    newName: string
  ): Promise<{
    message: string;
    old_name: string;
    new_name: string;
    was_running: boolean;
    note?: string;
  }> {
    return this.request<{
      message: string;
      old_name: string;
      new_name: string;
      was_running: boolean;
      note?: string;
    }>("PUT", `/api/agents/${encodeURIComponent(name)}/rename`, {
      new_name: newName,
    });
  }

  /**
   * Get credential status from a running agent
   */
  async getCredentialStatus(name: string): Promise<{
    agent_name: string;
    files: Record<string, { exists: boolean; size?: number; modified?: string }>;
    credential_count: number;
  }> {
    return this.request<{
      agent_name: string;
      files: Record<string, { exists: boolean; size?: number; modified?: string }>;
      credential_count: number;
    }>(
      "GET",
      `/api/agents/${encodeURIComponent(name)}/credentials/status`
    );
  }

  /**
   * Inject credential files directly into a running agent
   * New simplified credential system (CRED-002)
   * @param name - Agent name
   * @param files - Map of file paths to contents (e.g., {".env": "KEY=value"})
   */
  async injectCredentials(
    name: string,
    files: Record<string, string>,
    filesB64: Record<string, string> = {},
  ): Promise<{
    status: string;
    files_written: string[];
    message: string;
  }> {
    return this.request<{
      status: string;
      files_written: string[];
      message: string;
    }>(
      "POST",
      `/api/agents/${encodeURIComponent(name)}/credentials/inject`,
      { files, files_b64: filesB64 }
    );
  }

  /**
   * Export credentials from agent to encrypted .credentials.enc file
   * New simplified credential system (CRED-002)
   * @param name - Agent name
   */
  async exportCredentials(name: string): Promise<{
    status: string;
    encrypted_file: string;
    files_exported: number;
  }> {
    return this.request<{
      status: string;
      encrypted_file: string;
      files_exported: number;
    }>(
      "POST",
      `/api/agents/${encodeURIComponent(name)}/credentials/export`
    );
  }

  /**
   * Import credentials from encrypted .credentials.enc file to agent
   * New simplified credential system (CRED-002)
   * @param name - Agent name
   */
  async importCredentials(name: string): Promise<{
    status: string;
    files_imported: string[];
    message: string;
  }> {
    return this.request<{
      status: string;
      files_imported: string[];
      message: string;
    }>(
      "POST",
      `/api/agents/${encodeURIComponent(name)}/credentials/import`
    );
  }

  /**
   * Get the platform's credential encryption key
   * Enables local agents to encrypt/decrypt .credentials.enc files
   * New simplified credential system (CRED-002)
   */
  async getEncryptionKey(): Promise<{
    key: string;
    algorithm: string;
    key_format: string;
    note: string;
  }> {
    return this.request<{
      key: string;
      algorithm: string;
      key_format: string;
      note: string;
    }>("GET", `/api/credentials/encryption-key`);
  }

  /**
   * Generate ephemeral, key-based SSH credentials for direct agent access.
   * The client supplies their public key (the private key never leaves the client).
   * Password auth was removed (#1615) — key auth is the only method.
   * @param name - Agent name
   * @param ttlHours - Credential validity in hours (0.1-24, default: 4)
   * @param publicKey - Client's SSH public key (required)
   */
  async createSshAccess(
    name: string,
    ttlHours: number = 4,
    publicKey?: string
  ): Promise<SshAccessResponse> {
    // #1615: key-based auth only (password auth removed).
    const body: Record<string, unknown> = { ttl_hours: ttlHours, auth_method: "key" };
    if (publicKey) {
      body.public_key = publicKey;
    }
    return this.request<SshAccessResponse>(
      "POST",
      `/api/agents/${encodeURIComponent(name)}/ssh-access`,
      body
    );
  }

  // ============================================================================
  // Chat & Communication
  // ============================================================================

  /**
   * Send a message to an agent and get a response
   * @param sourceAgent - Optional source agent name for agent-to-agent collaboration tracking
   * @param mcpKeyInfo - Optional MCP key info for execution origin tracking (AUDIT-001)
   *
   * Returns ChatResponse on success, or a queue status object if agent is busy (429).
   */
  async chat(
    name: string,
    message: string,
    sourceAgent?: string,
    mcpKeyInfo?: { keyId?: string; keyName?: string },
    idempotencyKey?: string
  ): Promise<
    | ChatResponse
    | { error: string; queue_status: "busy" | "queue_full"; retry_after: number; agent: string; details?: Record<string, unknown> }
    | { status: "queued_timeout"; agent: string; execution_id: string; message: string }
  > {
    // Prepare headers
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...(this.token && { Authorization: `Bearer ${this.token}` }),
      "X-Via-MCP": "true",  // Always mark as MCP call for task tracking
    };

    // RELIABILITY-006 (#525): forward idempotency key so an SDK-level retry of
    // the same tool call dedupes instead of dispatching a second execution.
    if (idempotencyKey) {
      headers["Idempotency-Key"] = idempotencyKey;
    }

    // Add X-Source-Agent header for collaboration tracking
    if (sourceAgent) {
      headers["X-Source-Agent"] = sourceAgent;
    }

    // Add MCP key info headers for execution origin tracking (AUDIT-001).
    // #2389 — INERT: the backend no longer reads these. No router declares
    // `X-MCP-Key-Id`/`X-MCP-Key-Name` any more (a scan over the router tree
    // asserts it), because `Header(None)` validated nowhere let any
    // authenticated caller name someone else's credential in audit rows AND in
    // durable execution provenance. Provenance now derives from the presented
    // bearer. Kept only so a rolling deploy has one less moving part — the value
    // sent is the same key the bearer already identifies, so dropping it is an
    // MCP rebuild for zero behaviour change. Delete freely; never re-add a
    // backend reader.
    if (mcpKeyInfo?.keyId) {
      headers["X-MCP-Key-ID"] = mcpKeyInfo.keyId;
    }
    if (mcpKeyInfo?.keyName) {
      headers["X-MCP-Key-Name"] = mcpKeyInfo.keyName;
    }

    // #914: bound the synchronous backend fetch with an MCP-server-side
    // timeout. The MCP client (e.g. Claude Code) imposes its own 30-60s
    // gateway timeout on this JSON-RPC call, and `fetch failed` propagation
    // is what drives naive callers into duplicate-queue retries. Aborting
    // before the gateway gives us a chance to look up the queued
    // execution_id and return a structured receipt instead.
    // `||` (not `??`) so a set-but-empty value coalesces to the default —
    // the TS twin of the #1076 os.getenv shadow bug. `'' ?? 25000` is `''`
    // and `Number('')` is 0, which would abort every sync chat instantly.
    // Compose injects `${MCP_CHAT_TIMEOUT_MS:-25000}` (non-empty) today, so
    // this is defense-in-depth against a future empty injection / `-e VAR=`.
    const timeoutMs = Number(process.env.MCP_CHAT_TIMEOUT_MS || 25000);
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
    const startTime = Date.now();

    let response: Response;
    try {
      response = await fetch(`${this.baseUrl}/api/agents/${encodeURIComponent(name)}/chat`, {
        method: "POST",
        headers,
        body: JSON.stringify({ message }),
        signal: controller.signal,
      });
    } catch (err) {
      // Abort or transport-level network failure. Distinguish abort vs.
      // other errors so the recovery path only fires when WE gave up,
      // not when the backend rejected the connection upfront.
      const isAbort = (err as Error)?.name === "AbortError";
      const isNetwork = (err as Error)?.name === "TypeError";
      if (isAbort || isNetwork) {
        const reason = isAbort ? "client abort" : "network error";
        debugLog(`[chat] ${reason} after ${Date.now() - startTime}ms on '${name}'; attempting execution-id lookup (#914)`);
        const receipt = await this.findRecentMcpExecution(name, mcpKeyInfo?.keyId);
        if (receipt) {
          return {
            status: "queued_timeout",
            agent: name,
            execution_id: receipt.id,
            message:
              `MCP-server timeout (${timeoutMs}ms) on chat_with_agent — task is still running on '${name}'. ` +
              `Poll get_execution_result(execution_id="${receipt.id}") instead of retrying; retry will duplicate-queue and Trinity's concurrent-duplicate guard will kill mid-execution (#914).`,
          };
        }
        // No match found — rethrow with a hint so the caller knows to
        // check the dashboard before retrying.
        throw new Error(
          `MCP-server timeout on chat_with_agent (${timeoutMs}ms) and no recent execution found on '${name}'. ` +
          `Check list_recent_executions(agent_name="${name}") on the dashboard before retrying to avoid duplicate-queue (#914).`
        );
      }
      throw err;
    } finally {
      clearTimeout(timeoutId);
    }

    // Handle 429 Too Many Requests (agent queue full)
    if (response.status === 429) {
      let details: Record<string, unknown> = {};
      try {
        details = await response.json() as Record<string, unknown>;
      } catch {
        // Ignore JSON parse errors
      }
      return {
        error: "Agent is busy",
        queue_status: "queue_full",
        retry_after: (details.retry_after as number) || 30,
        agent: name,
        details,
      };
    }

    if (!response.ok) {
      const error = await response.text();
      throw new Error(`API error (${response.status}): ${error}`);
    }

    return (await response.json()) as ChatResponse;
  }

  /**
   * #914 recovery lookup. After the chat() fetch aborts, query the
   * agent's recent executions and pick the newest non-terminal row that
   * (a) was triggered through MCP, (b) carries the calling key's id if
   * one was supplied, and (c) started within the last ~30s. The caller
   * uses this to return a structured `queued_timeout` receipt instead of
   * propagating `fetch failed`.
   *
   * Best-effort: any failure (executions endpoint unreachable, no rows,
   * no match) returns `undefined` and the caller falls back to a clearer
   * error message.
   */
  private async findRecentMcpExecution(
    agentName: string,
    mcpKeyId?: string,
  ): Promise<ScheduleExecution | undefined> {
    try {
      const recent = await this.getAgentExecutions(agentName, 10);
      return pickRecentMcpExecution(recent, { mcpKeyId, now: Date.now() });
    } catch (err) {
      debugLog(`[chat] findRecentMcpExecution failed for '${agentName}': ${(err as Error)?.message}`);
      return undefined;
    }
  }

  /**
   * Execute a parallel task on an agent (stateless, no conversation context)
   *
   * Unlike chat(), this method:
   * - Does NOT use execution queue (parallel allowed)
   * - Does NOT use --continue flag (stateless)
   * - Each call is independent and can run concurrently
   *
   * @param name - Agent name
   * @param message - The task to execute
   * @param options - Optional parameters including async_mode for fire-and-forget
   * @param sourceAgent - Optional source agent name for collaboration tracking
   * @param mcpKeyInfo - Optional MCP key info for execution origin tracking (AUDIT-001)
   */
  async task(
    name: string,
    message: string,
    options?: {
      model?: string;
      allowed_tools?: string[];
      system_prompt?: string;
      timeout_seconds?: number;
      async_mode?: boolean;
      // SELF-EXEC-001: Self-task options
      inject_result?: boolean;
      chat_session_id?: string;
      // ent#224: the CALLER's execution id, so the delegated task inherits the
      // originating channel/thread and its completion can be reported back.
      parent_execution_id?: string;
    },
    sourceAgent?: string,
    mcpKeyInfo?: { keyId?: string; keyName?: string },
    idempotencyKey?: string
  ): Promise<ChatResponse | { status: "accepted"; execution_id: string; agent_name: string; message: string; async_mode: true }> {
    // Prepare headers
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...(this.token && { Authorization: `Bearer ${this.token}` }),
      "X-Via-MCP": "true",  // Always mark as MCP call for task tracking
    };

    // RELIABILITY-006 (#525): forward idempotency key (SDK-retry dedup).
    if (idempotencyKey) {
      headers["Idempotency-Key"] = idempotencyKey;
    }

    // Add X-Source-Agent header for collaboration tracking
    if (sourceAgent) {
      headers["X-Source-Agent"] = sourceAgent;
    }

    // Add MCP key info headers for execution origin tracking (AUDIT-001).
    // #2389 — INERT: the backend no longer reads these. No router declares
    // `X-MCP-Key-Id`/`X-MCP-Key-Name` any more (a scan over the router tree
    // asserts it), because `Header(None)` validated nowhere let any
    // authenticated caller name someone else's credential in audit rows AND in
    // durable execution provenance. Provenance now derives from the presented
    // bearer. Kept only so a rolling deploy has one less moving part — the value
    // sent is the same key the bearer already identifies, so dropping it is an
    // MCP rebuild for zero behaviour change. Delete freely; never re-add a
    // backend reader.
    if (mcpKeyInfo?.keyId) {
      headers["X-MCP-Key-ID"] = mcpKeyInfo.keyId;
    }
    if (mcpKeyInfo?.keyName) {
      headers["X-MCP-Key-Name"] = mcpKeyInfo.keyName;
    }

    const body = {
      message,
      model: options?.model,
      allowed_tools: options?.allowed_tools,
      system_prompt: options?.system_prompt,
      timeout_seconds: options?.timeout_seconds,
      async_mode: options?.async_mode,
      // SELF-EXEC-001: Self-task options for result injection
      inject_result: options?.inject_result,
      chat_session_id: options?.chat_session_id,
      parent_execution_id: options?.parent_execution_id,   // ent#224
    };

    // Async mode returns immediately; sync mode waits for full execution.
    // When timeout_seconds is omitted, backend resolves the target agent's
    // configured execution_timeout_seconds (max 7200s). Use platform max + buffer
    // as the HTTP client ceiling so we don't abort before the backend does.
    const timeout = options?.async_mode
      ? 30
      : (options?.timeout_seconds ?? 7200) + 60;

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeout * 1000);

    try {
      const response = await fetch(`${this.baseUrl}/api/agents/${encodeURIComponent(name)}/task`, {
        method: "POST",
        headers,
        body: JSON.stringify(body),
        signal: controller.signal,
      });

      if (!response.ok) {
        const error = await response.text();
        throw new Error(`API error (${response.status}): ${error}`);
      }

      return (await response.json()) as ChatResponse;
    } finally {
      clearTimeout(timeoutId);
    }
  }

  /**
   * Get an agent's conversation history
   */
  async getChatHistory(name: string): Promise<ChatMessage[]> {
    // Backend returns array directly, not wrapped in { history: [...] }
    return this.request<ChatMessage[]>(
      "GET",
      `/api/agents/${encodeURIComponent(name)}/chat/history`
    );
  }

  /**
   * Get an agent's container logs
   */
  async getAgentLogs(name: string, lines: number = 100): Promise<string> {
    const response = await this.request<{ logs: string }>(
      "GET",
      `/api/agents/${encodeURIComponent(name)}/logs?tail=${lines}`
    );
    return response.logs;
  }

  // ============================================================================
  // Agent Workspace Files (#919 — pipeline introspection read surface)
  // ============================================================================

  /**
   * Recursively list files under a workspace directory via the existing
   * `GET /api/agents/{name}/files` surface (no new backend endpoint). Returns
   * a hierarchical tree; `path` values are relative to `/home/developer` and
   * each node carries an ISO-8601 `modified` mtime. A missing directory
   * surfaces as a 404 (mapped to `API error (404): …`) — callers decide
   * whether that means "empty" or "not found".
   *
   * @param name - Agent name
   * @param path - Directory to list (relative to home, e.g. `.trinity/pipelines`)
   * @param showHidden - Include dot-prefixed entries (default true here: the
   *   pipeline paths live under the hidden `.trinity` directory)
   */
  async listAgentFiles(
    name: string,
    path: string,
    showHidden: boolean = true
  ): Promise<AgentFileTreeResponse> {
    return this.request<AgentFileTreeResponse>(
      "GET",
      `/api/agents/${encodeURIComponent(name)}/files` +
        `?path=${encodeURIComponent(path)}&show_hidden=${showHidden}`
    );
  }

  /**
   * Download a single workspace file as plain text via the existing
   * `GET /api/agents/{name}/files/download` surface. Uses the shared `_fetch`
   * (auth + 401-retry + error mapping) but reads the body as text rather than
   * JSON — the download endpoint returns `PlainTextResponse`.
   *
   * @param name - Agent name
   * @param path - File path (relative to home, e.g.
   *   `.trinity/pipelines/demo.yaml`)
   */
  async downloadAgentFile(name: string, path: string): Promise<string> {
    const response = await this._fetch(
      "GET",
      `/api/agents/${encodeURIComponent(name)}/files/download` +
        `?path=${encodeURIComponent(path)}`
    );
    return response.text();
  }

  // ============================================================================
  // Fan-Out (FANOUT-001)
  // ============================================================================

  /**
   * Fan out N independent tasks to an agent in parallel and collect results.
   *
   * Each subtask follows the standard execution path (visible on dashboard).
   * Results are collected with per-task status and returned as a single response.
   */
  async fanOut(
    name: string,
    tasks: Array<{ id: string; message: string }>,
    options?: {
      agent?: string;
      timeout_seconds?: number;
      max_concurrency?: number;
      policy?: string;
      model?: string;
      system_prompt?: string;
      allowed_tools?: string[];
    },
    sourceAgent?: string,
    mcpKeyInfo?: { keyId?: string; keyName?: string },
    idempotencyKey?: string
  ): Promise<{
    fan_out_id: string;
    status: string;
    total: number;
    completed: number;
    failed: number;
    results: Array<{
      id: string;
      status: string;
      response?: string;
      error?: string;
      error_code?: string;
      execution_id?: string;
      cost?: number;
      context_used?: number;
      duration_ms?: number;
    }>;
  }> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...(this.token && { Authorization: `Bearer ${this.token}` }),
      "X-Via-MCP": "true",
    };

    // RELIABILITY-006 (#525): forward idempotency key (SDK-retry dedup).
    if (idempotencyKey) {
      headers["Idempotency-Key"] = idempotencyKey;
    }

    if (sourceAgent) {
      headers["X-Source-Agent"] = sourceAgent;
    }
    // #2389 — INERT: the backend no longer reads these. No router declares
    // `X-MCP-Key-Id`/`X-MCP-Key-Name` any more (a scan over the router tree
    // asserts it), because `Header(None)` validated nowhere let any
    // authenticated caller name someone else's credential in audit rows AND in
    // durable execution provenance. Provenance now derives from the presented
    // bearer. Kept only so a rolling deploy has one less moving part — the value
    // sent is the same key the bearer already identifies, so dropping it is an
    // MCP rebuild for zero behaviour change. Delete freely; never re-add a
    // backend reader.
    if (mcpKeyInfo?.keyId) {
      headers["X-MCP-Key-ID"] = mcpKeyInfo.keyId;
    }
    if (mcpKeyInfo?.keyName) {
      headers["X-MCP-Key-Name"] = mcpKeyInfo.keyName;
    }

    // Only include timeout_seconds if caller provided it, so the backend can
    // fall back to the target agent's configured execution_timeout_seconds.
    const body: Record<string, unknown> = {
      tasks,
      agent: options?.agent || "self",
      max_concurrency: options?.max_concurrency || 3,
      policy: options?.policy || "best-effort",
      model: options?.model,
      system_prompt: options?.system_prompt,
      allowed_tools: options?.allowed_tools,
    };
    if (options?.timeout_seconds !== undefined) {
      body.timeout_seconds = options.timeout_seconds;
    }

    // HTTP ceiling: when no explicit fan-out timeout, cover the platform max
    // per-agent timeout (7200s) + buffer so we don't abort before the backend.
    const timeout = (options?.timeout_seconds ?? 7200) + 60;
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeout * 1000);

    try {
      const response = await fetch(
        `${this.baseUrl}/api/agents/${encodeURIComponent(name)}/fan-out`,
        {
          method: "POST",
          headers,
          body: JSON.stringify(body),
          signal: controller.signal,
        }
      );

      if (!response.ok) {
        const error = await response.text();
        throw new Error(`API error (${response.status}): ${error}`);
      }

      return (await response.json()) as {
        fan_out_id: string;
        status: string;
        total: number;
        completed: number;
        failed: number;
        results: Array<{
          id: string;
          status: string;
          response?: string;
          error?: string;
          error_code?: string;
          execution_id?: string;
          cost?: number;
          context_used?: number;
          duration_ms?: number;
        }>;
      };
    } finally {
      clearTimeout(timeoutId);
    }
  }

  // ============================================================================
  // Templates
  // ============================================================================

  /**
   * List available agent templates
   */
  async listTemplates(): Promise<Template[]> {
    return this.request<Template[]>("GET", "/api/templates");
  }

  /**
   * Get a specific template by ID
   */
  async getTemplate(templateId: string): Promise<Template> {
    return this.request<Template>(
      "GET",
      `/api/templates/${encodeURIComponent(templateId)}`
    );
  }

  // ============================================================================
  // Schedule Management
  // ============================================================================

  /**
   * List all schedules for an agent
   */
  async listAgentSchedules(agentName: string): Promise<Schedule[]> {
    return this.request<Schedule[]>(
      "GET",
      `/api/agents/${encodeURIComponent(agentName)}/schedules`
    );
  }

  /**
   * Create a new schedule for an agent
   */
  async createAgentSchedule(
    agentName: string,
    schedule: ScheduleCreate
  ): Promise<Schedule> {
    return this.request<Schedule>(
      "POST",
      `/api/agents/${encodeURIComponent(agentName)}/schedules`,
      schedule
    );
  }

  /**
   * Get a specific schedule by ID
   */
  async getAgentSchedule(
    agentName: string,
    scheduleId: string
  ): Promise<Schedule> {
    return this.request<Schedule>(
      "GET",
      `/api/agents/${encodeURIComponent(agentName)}/schedules/${encodeURIComponent(scheduleId)}`
    );
  }

  /**
   * Update an existing schedule
   */
  async updateAgentSchedule(
    agentName: string,
    scheduleId: string,
    updates: ScheduleUpdate
  ): Promise<Schedule> {
    return this.request<Schedule>(
      "PUT",
      `/api/agents/${encodeURIComponent(agentName)}/schedules/${encodeURIComponent(scheduleId)}`,
      updates
    );
  }

  /**
   * Delete a schedule
   */
  async deleteAgentSchedule(
    agentName: string,
    scheduleId: string
  ): Promise<void> {
    await this.request<void>(
      "DELETE",
      `/api/agents/${encodeURIComponent(agentName)}/schedules/${encodeURIComponent(scheduleId)}`
    );
  }

  /**
   * Enable a schedule
   */
  async enableAgentSchedule(
    agentName: string,
    scheduleId: string
  ): Promise<ScheduleToggleResult> {
    return this.request<ScheduleToggleResult>(
      "POST",
      `/api/agents/${encodeURIComponent(agentName)}/schedules/${encodeURIComponent(scheduleId)}/enable`
    );
  }

  /**
   * Disable a schedule
   */
  async disableAgentSchedule(
    agentName: string,
    scheduleId: string
  ): Promise<ScheduleToggleResult> {
    return this.request<ScheduleToggleResult>(
      "POST",
      `/api/agents/${encodeURIComponent(agentName)}/schedules/${encodeURIComponent(scheduleId)}/disable`
    );
  }

  /**
   * Manually trigger a schedule execution
   *
   * @param sourceAgent - Triggering agent, for agent-scoped keys
   * @param mcpKeyInfo - MCP key identity (AUDIT-001 / #1970 execution origin)
   */
  async triggerAgentSchedule(
    agentName: string,
    scheduleId: string,
    sourceAgent?: string,
    mcpKeyInfo?: { keyId?: string; keyName?: string }
  ): Promise<ScheduleTriggerResult> {
    // #1970: send the same origin headers `chat()` does. The backend forwards
    // them to the scheduler, which persists them on the execution row. Without
    // this an MCP-triggered run attributes to the key OWNER but not to which
    // key or which agent pulled the trigger — the part that identifies the
    // actor when one human owns many keys and many agents.
    const headers: Record<string, string> = {};
    if (sourceAgent) {
      headers["X-Source-Agent"] = sourceAgent;
    }
    // #2389 — INERT: the backend no longer reads these. No router declares
    // `X-MCP-Key-Id`/`X-MCP-Key-Name` any more (a scan over the router tree
    // asserts it), because `Header(None)` validated nowhere let any
    // authenticated caller name someone else's credential in audit rows AND in
    // durable execution provenance. Provenance now derives from the presented
    // bearer. Kept only so a rolling deploy has one less moving part — the value
    // sent is the same key the bearer already identifies, so dropping it is an
    // MCP rebuild for zero behaviour change. Delete freely; never re-add a
    // backend reader.
    if (mcpKeyInfo?.keyId) {
      headers["X-MCP-Key-ID"] = mcpKeyInfo.keyId;
    }
    if (mcpKeyInfo?.keyName) {
      headers["X-MCP-Key-Name"] = mcpKeyInfo.keyName;
    }

    return this.request<ScheduleTriggerResult>(
      "POST",
      `/api/agents/${encodeURIComponent(agentName)}/schedules/${encodeURIComponent(scheduleId)}/trigger`,
      undefined,
      false,
      undefined,
      headers
    );
  }

  /**
   * Get execution history for a specific schedule
   */
  async getScheduleExecutions(
    agentName: string,
    scheduleId: string,
    limit: number = 20
  ): Promise<ScheduleExecution[]> {
    return this.request<ScheduleExecution[]>(
      "GET",
      `/api/agents/${encodeURIComponent(agentName)}/schedules/${encodeURIComponent(scheduleId)}/executions?limit=${limit}`
    );
  }

  /**
   * Get all executions for an agent across all schedules
   */
  async getAgentExecutions(
    agentName: string,
    limit: number = 20
  ): Promise<ScheduleExecution[]> {
    return this.request<ScheduleExecution[]>(
      "GET",
      `/api/agents/${encodeURIComponent(agentName)}/executions?limit=${limit}`
    );
  }

  /**
   * Get a specific execution by ID (MCP-007)
   */
  async getExecution(
    agentName: string,
    executionId: string
  ): Promise<ScheduleExecution> {
    return this.request<ScheduleExecution>(
      "GET",
      `/api/agents/${encodeURIComponent(agentName)}/executions/${encodeURIComponent(executionId)}`
    );
  }

  /**
   * Get the full execution log/transcript for an execution (MCP-007)
   */
  async getExecutionLog(
    agentName: string,
    executionId: string
  ): Promise<{ execution_id: string; agent_name: string; log: unknown }> {
    return this.request(
      "GET",
      `/api/agents/${encodeURIComponent(agentName)}/executions/${encodeURIComponent(executionId)}/log`
    );
  }

  /**
   * Get cross-agent activity timeline (MCP-007)
   */
  async getActivityTimeline(params: {
    start_time?: string;
    end_time?: string;
    activity_types?: string;
    limit?: number;
  } = {}): Promise<ActivityTimelineResponse> {
    const searchParams = new URLSearchParams();
    if (params.start_time) searchParams.set("start_time", params.start_time);
    if (params.end_time) searchParams.set("end_time", params.end_time);
    if (params.activity_types) searchParams.set("activity_types", params.activity_types);
    if (params.limit) searchParams.set("limit", String(params.limit));
    const qs = searchParams.toString();
    return this.request<ActivityTimelineResponse>(
      "GET",
      `/api/activities/timeline${qs ? `?${qs}` : ""}`
    );
  }

  // ============================================================================
  // Tags (ORG-001)
  // ============================================================================

  /**
   * List all unique tags with agent counts
   */
  async listAllTags(): Promise<{ tags: Array<{ tag: string; count: number }> }> {
    return this.request<{ tags: Array<{ tag: string; count: number }> }>(
      "GET",
      "/api/tags"
    );
  }

  /**
   * Get tags for a specific agent
   */
  async getAgentTags(name: string): Promise<{ agent_name: string; tags: string[] }> {
    return this.request<{ agent_name: string; tags: string[] }>(
      "GET",
      `/api/agents/${encodeURIComponent(name)}/tags`
    );
  }

  /**
   * Add a single tag to an agent
   */
  async addAgentTag(name: string, tag: string): Promise<{ agent_name: string; tags: string[] }> {
    return this.request<{ agent_name: string; tags: string[] }>(
      "POST",
      `/api/agents/${encodeURIComponent(name)}/tags/${encodeURIComponent(tag)}`
    );
  }

  /**
   * Remove a single tag from an agent
   */
  async removeAgentTag(name: string, tag: string): Promise<{ agent_name: string; tags: string[] }> {
    return this.request<{ agent_name: string; tags: string[] }>(
      "DELETE",
      `/api/agents/${encodeURIComponent(name)}/tags/${encodeURIComponent(tag)}`
    );
  }

  /**
   * Replace all tags for an agent
   */
  async setAgentTags(name: string, tags: string[]): Promise<{ agent_name: string; tags: string[] }> {
    return this.request<{ agent_name: string; tags: string[] }>(
      "PUT",
      `/api/agents/${encodeURIComponent(name)}/tags`,
      { tags }
    );
  }

  // ============================================================================
  // Notifications (NOTIF-001)
  // ============================================================================

  /**
   * Create a notification from an agent
   */
  async createNotification(data: {
    notification_type: string;
    title: string;
    message?: string;
    priority?: string;
    category?: string;
    metadata?: Record<string, unknown>;
  }): Promise<{
    id: string;
    agent_name: string;
    notification_type: string;
    title: string;
    message?: string;
    priority: string;
    category?: string;
    metadata?: Record<string, unknown>;
    status: string;
    created_at: string;
  }> {
    return this.request(
      "POST",
      "/api/notifications",
      data
    );
  }

  /**
   * Publish a structured agent report (#918). The path agent is the calling
   * agent (resolved from the auth context by the report tool); the backend
   * self-gates an agent-scoped key to itself.
   */
  async createReport(
    agentName: string,
    data: {
      report_type: string;
      title: string;
      payload: Record<string, unknown>;
      display_hint?: string;
      schema_version?: number;
      period_start?: string;
      period_end?: string;
    }
  ): Promise<{
    id: string;
    agent_name: string;
    report_type: string;
    title: string;
    created_at: string;
  }> {
    return this.request(
      "POST",
      `/api/agents/${encodeURIComponent(agentName)}/reports`,
      data
    );
  }

  /**
   * List reports across accessible agents — METADATA only, no payload (#1538).
   * The backend scopes to the caller's accessible agents; an agent-scoped key is
   * narrowed further to {self} ∪ permitted by the tool layer.
   */
  async listReports(params: {
    report_type?: string;
    hours?: number;
    search?: string;
    agent?: string;
    limit?: number;
    offset?: number;
  }): Promise<ReportSummary[]> {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== "") qs.append(k, String(v));
    }
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return this.request("GET", `/api/reports${suffix}`);
  }

  /**
   * List one agent's reports (metadata only) (#1538).
   */
  async listAgentReports(
    agentName: string,
    params: {
      report_type?: string;
      hours?: number;
      search?: string;
      limit?: number;
      offset?: number;
    }
  ): Promise<ReportSummary[]> {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== "") qs.append(k, String(v));
    }
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return this.request(
      "GET",
      `/api/agents/${encodeURIComponent(agentName)}/reports${suffix}`
    );
  }

  /**
   * Fetch one report INCLUDING its payload (#1538). The backend answers 404 —
   * not 403 — when the caller cannot access the owning agent, so an id cannot be
   * probed for existence.
   */
  async getReport(reportId: string): Promise<Record<string, unknown>> {
    return this.request("GET", `/api/reports/${encodeURIComponent(reportId)}`);
  }

  // ============================================================================
  // Operator Queue (OPS-001, #1101) — read surface
  // ============================================================================

  /**
   * List operator-queue (Operating Room) items with optional filters. The
   * backend applies owner-level accessible-agent filtering; the MCP tool layer
   * additionally gates agent-scoped keys down to agent_permissions.
   */
  async listOperatorQueue(params: {
    status?: string;
    type?: string;
    priority?: string;
    agent_name?: string;
    since?: string;
    limit?: number;
    offset?: number;
  } = {}): Promise<OperatorQueueListResponse> {
    const sp = new URLSearchParams();
    if (params.status) sp.set("status", params.status);
    if (params.type) sp.set("type", params.type);
    if (params.priority) sp.set("priority", params.priority);
    if (params.agent_name) sp.set("agent_name", params.agent_name);
    if (params.since) sp.set("since", params.since);
    if (params.limit !== undefined) sp.set("limit", String(params.limit));
    if (params.offset !== undefined) sp.set("offset", String(params.offset));
    const qs = sp.toString();
    return this.request<OperatorQueueListResponse>(
      "GET",
      `/api/operator-queue${qs ? `?${qs}` : ""}`,
    );
  }

  /**
   * Get a single operator-queue item by id.
   */
  async getOperatorQueueItem(itemId: string): Promise<OperatorQueueItem> {
    return this.request<OperatorQueueItem>(
      "GET",
      `/api/operator-queue/${encodeURIComponent(itemId)}`,
    );
  }

  /**
   * Respond to (resolve) a pending operator-queue item (OPS-001, #1104).
   * Proxies POST /api/operator-queue/{id}/respond. The backend 400s if the
   * item is not in a respondable (`pending`) state — surfaced as a thrown
   * Error the tool layer catches and returns as a structured `{ error }`.
   */
  async respondToOperatorQueueItem(
    itemId: string,
    body: { response: string; response_text?: string },
  ): Promise<OperatorQueueItem> {
    return this.request<OperatorQueueItem>(
      "POST",
      `/api/operator-queue/${encodeURIComponent(itemId)}/respond`,
      body,
    );
  }

  // ============================================================================
  // Outbound File Sharing (FILES-001)
  // ============================================================================

  /**
   * Mint a public download URL for a file the agent wrote to its
   * /home/developer/public/ directory. Requires the agent's file_sharing
   * toggle to be enabled (see PUT /api/agents/{name}/file-sharing).
   */
  async shareAgentFile(
    agentName: string,
    data: {
      filename: string;
      display_name?: string;
      expires_in?: number;
      execution_id?: string;
      dedup_label?: string;
    }
  ): Promise<{
    file_id: string;
    url: string;
    expires_at: string;
    size_bytes: number;
    mime_type?: string;
  }> {
    return this.request(
      "POST",
      `/api/agents/${encodeURIComponent(agentName)}/shared-files`,
      data
    );
  }

  // ============================================================================
  // Proactive Messaging (Issue #321)
  // ============================================================================

  /**
   * Send a proactive message to a user by verified email
   */
  async sendUserMessage(
    agentName: string,
    data: {
      recipient_email: string;
      text: string;
      channel?: "auto" | "telegram" | "slack" | "web";
      reply_to_thread?: boolean;
      execution_id?: string;
      dedup_label?: string;
    }
  ): Promise<{
    success: boolean;
    channel: string;
    message_id?: string;
    error?: string;
  }> {
    return this.request(
      "POST",
      `/api/agents/${encodeURIComponent(agentName)}/messages`,
      data
    );
  }

  /**
   * Deliver one reply of the current channel turn as a spoken voice note
   * (trinity-enterprise#117). The backend resolves the channel destination from
   * the execution_id and gates on the agent + per-channel voice flags. Fail-soft:
   * returns delivered=false (not an error) when voice can't be delivered.
   */
  async sendVoiceReply(
    agentName: string,
    data: {
      text: string;
      execution_id: string;
      dedup_label?: string;
    }
  ): Promise<{
    delivered: boolean;
    channel?: string;
    reason?: string;
    /** #2157: plain-language explanation of a refusal, for surfaces where the
     *  machine `reason` alone leads an agent to a wrong conclusion (the portal
     *  narrates the agent's text, so "no voice note here" ≠ "no voice here"). */
    guidance?: string;
  }> {
    return this.request(
      "POST",
      `/api/agents/${encodeURIComponent(agentName)}/voice-reply`,
      data
    );
  }

  // ============================================================================
  // VoIP Telephony (VOIP-001, #1056)
  // ============================================================================

  /**
   * Place an outbound phone call from the agent to a user. Server-side gated
   * (VoIP enabled + voice binding) and rate-limited.
   */
  async placeVoipCall(
    agentName: string,
    data: {
      to_number: string;
      context?: string;
      process_transcript?: boolean;
      execution_id?: string;
      dedup_label?: string;
    }
  ): Promise<{
    call_id: string;
    status: string;
    to_number: string;
    twilio_call_sid?: string;
  }> {
    return this.request(
      "POST",
      `/api/agents/${encodeURIComponent(agentName)}/voip/call`,
      data
    );
  }

  // ============================================================================
  // Per-User Memory Write (MEM-001, #888)
  // ============================================================================

  /**
   * Write the per-user memory blob for the user currently being served.
   * The user email is resolved server-side from the execution record —
   * the caller only supplies the execution_id and the memory text.
   */
  async writeUserMemory(
    agentName: string,
    data: {
      execution_id: string;
      memory_text: string;
    }
  ): Promise<{
    success: boolean;
    agent_name: string;
    user_email: string;
  }> {
    return this.request(
      "POST",
      `/api/agents/${encodeURIComponent(agentName)}/user-memory`,
      data
    );
  }

  // ============================================================================
  // Agent Event Subscriptions (EVT-001)
  // ============================================================================

  /**
   * Emit an event from an agent
   */
  async emitEvent(data: {
    event_type: string;
    payload?: Record<string, unknown>;
  }): Promise<{
    id: string;
    source_agent: string;
    event_type: string;
    payload?: Record<string, unknown>;
    subscriptions_triggered: number;
    created_at: string;
  }> {
    return this.request(
      "POST",
      "/api/events",
      data
    );
  }

  /**
   * Create an event subscription
   */
  async createEventSubscription(
    agentName: string,
    data: {
      source_agent: string;
      event_type: string;
      target_message: string;
      enabled?: boolean;
    }
  ): Promise<{
    id: string;
    subscriber_agent: string;
    source_agent: string;
    event_type: string;
    target_message: string;
    enabled: boolean;
    created_at: string;
  }> {
    return this.request(
      "POST",
      `/api/agents/${encodeURIComponent(agentName)}/event-subscriptions`,
      data
    );
  }

  /**
   * List event subscriptions for an agent
   */
  async listEventSubscriptions(
    agentName: string,
    direction: string = "subscriber"
  ): Promise<{
    count: number;
    subscriptions: Array<{
      id: string;
      subscriber_agent: string;
      source_agent: string;
      event_type: string;
      target_message: string;
      enabled: boolean;
    }>;
  }> {
    return this.request(
      "GET",
      `/api/agents/${encodeURIComponent(agentName)}/event-subscriptions?direction=${direction}`
    );
  }

  /**
   * Delete an event subscription
   */
  async deleteEventSubscription(subscriptionId: string): Promise<{ status: string }> {
    return this.request(
      "DELETE",
      `/api/event-subscriptions/${encodeURIComponent(subscriptionId)}`
    );
  }

  // ============================================================================
  // Health
  // ============================================================================

  /**
   * Check if the Trinity API is healthy (no auth required)
   */
  async healthCheck(): Promise<{ status: string; timestamp: string }> {
    const response = await fetch(`${this.baseUrl}/health`);
    if (!response.ok) {
      throw new Error(`Health check failed: ${response.statusText}`);
    }
    return (await response.json()) as { status: string; timestamp: string };
  }

  // ============================================================================
  // Inline email auth (#848)
  // ============================================================================
  //
  // Timeout for the three control-plane calls (request/verify/playbooks). Chat
  // uses the longer MCP_CHAT_TIMEOUT_MS ceiling, matching the key-based path.
  //
  // These bypass `_fetch` on purpose: an anonymous session has no bearer token,
  // and `_fetch` throws without one. They authenticate the *caller* (this MCP
  // server) with the internal secret instead. The secret proves who is asking;
  // it never proves authorization — the backend gates every inline-auth call on
  // the verified email's own access (`email_has_agent_access`).

  /**
   * Bounded fetch for the inline-auth surface (#848).
   *
   * These four calls originally used bare `fetch` with no timeout, so a slow or
   * hung agent pinned a socket and the anonymous tool call forever — and,
   * because no tool sets `timeoutMs`, FastMCP's own tool timeout never fires
   * either. The key-based chat path already bounds itself
   * (MCP_CHAT_TIMEOUT_MS, see `chat`); leaving the inline path unbounded made
   * the two caller tiers behave differently for the same underlying agent.
   */
  private async internalFetch(
    path: string,
    body: unknown,
    timeoutMs: number
  ): Promise<Response> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      return await fetch(`${this.baseUrl}${path}`, {
        method: "POST",
        headers: this.internalHeaders(),
        body: JSON.stringify(body),
        signal: controller.signal,
      });
    } catch (err) {
      if (err instanceof Error && err.name === "AbortError") {
        throw new Error(`Trinity did not respond within ${timeoutMs}ms.`);
      }
      throw err;
    } finally {
      clearTimeout(timer);
    }
  }

  private internalHeaders(): Record<string, string> {
    const secret = process.env.INTERNAL_API_SECRET || "";
    if (!secret) {
      throw new Error(
        "INTERNAL_API_SECRET is not set — inline email auth (#848) cannot reach the backend."
      );
    }
    return { "Content-Type": "application/json", "X-Internal-Secret": secret };
  }

  /**
   * Ask the backend to email a login code. Fire-and-forget by contract: the
   * backend always answers 202 with a constant body regardless of whether the
   * address is known, so this resolves identically in both cases and callers
   * must not branch on the result (#186 enumeration safety).
   */
  async requestInlineLoginCode(email: string, sessionId?: string): Promise<void> {
    const response = await this.internalFetch(
      "/api/internal/mcp-auth/request",
      { email, session_id: sessionId },
      INLINE_AUTH_TIMEOUT_MS
    );
    if (!response.ok) {
      throw new Error(`Inline login request failed: ${response.status}`);
    }
  }

  /**
   * Verify a login code and resolve what the address may reach. Returns the
   * verified identity plus the agents shared with it; never a credential.
   */
  async verifyInlineLoginCode(
    email: string,
    code: string,
    sessionId?: string
  ): Promise<{
    verified: boolean;
    username?: string;
    agents?: Array<{ name: string; description?: string }>;
  }> {
    const response = await this.internalFetch(
      "/api/internal/mcp-auth/verify",
      { email, code, session_id: sessionId },
      INLINE_AUTH_TIMEOUT_MS
    );
    if (response.status === 401) {
      return { verified: false };
    }
    if (!response.ok) {
      throw new Error(`Inline login verification failed: ${response.status}`);
    }
    return (await response.json()) as {
      verified: boolean;
      username?: string;
      agents?: Array<{ name: string; description?: string }>;
    };
  }

  /**
   * Exposed playbooks for `agent`, acting as a verified email.
   *
   * The backend re-gates this on `email_has_agent_access(agent, email)` — the
   * internal secret says who is asking, never what they may reach.
   */
  async getInlineConnectorPlaybooks(
    email: string,
    agent: string
  ): Promise<Array<{ name: string; description?: string }>> {
    const response = await this.internalFetch(
      "/api/internal/mcp-auth/playbooks",
      { email, agent },
      INLINE_AUTH_TIMEOUT_MS
    );
    if (response.status === 403) {
      throw new Error(`You do not have access to "${agent}".`);
    }
    if (!response.ok) {
      throw new Error(`Could not list playbooks for "${agent}": ${response.status}`);
    }
    return (await response.json()) as Array<{ name: string; description?: string }>;
  }

  /** Chat with `agent` acting as a verified email. Same backend gate as above. */
  async inlineConnectorChat(
    email: string,
    agent: string,
    message: string
  ): Promise<unknown> {
    // Chat gets the same ceiling as the key-based path so the two caller tiers
    // time out identically for the same agent.
    const response = await this.internalFetch(
      "/api/internal/mcp-auth/chat",
      { email, agent, message },
      Number(process.env.MCP_CHAT_TIMEOUT_MS || 25000)
    );
    if (response.status === 403) {
      throw new Error(`You do not have access to "${agent}".`);
    }
    if (!response.ok) {
      throw new Error(`Chat with "${agent}" failed: ${response.status}`);
    }
    return await response.json();
  }

  // ============================================================================
  // Agent Monitoring (MON-001)
  // ============================================================================

  /**
   * Get fleet-wide health status
   */
  async getFleetHealth(): Promise<{
    enabled: boolean;
    last_check_at: string | null;
    summary: {
      total_agents: number;
      healthy: number;
      degraded: number;
      unhealthy: number;
      critical: number;
      unknown: number;
    };
    agents: Array<{
      name: string;
      status: string;
      docker_status?: string;
      network_reachable?: boolean;
      runtime_available?: boolean;
      last_check_at?: string;
      issues: string[];
    }>;
  }> {
    return this.request("GET", "/api/monitoring/status");
  }

  /**
   * Get detailed health information for a specific agent
   */
  async getAgentHealth(agentName: string): Promise<{
    agent_name: string;
    aggregate_status: string;
    last_check_at: string | null;
    docker?: {
      agent_name: string;
      container_status: string;
      cpu_percent?: number;
      memory_percent?: number;
      memory_mb?: number;
      restart_count: number;
      oom_killed: boolean;
      checked_at: string;
    };
    network?: {
      agent_name: string;
      reachable: boolean;
      latency_ms?: number;
      error?: string;
      checked_at: string;
    };
    business?: {
      agent_name: string;
      status: string;
      runtime_available?: boolean;
      claude_available?: boolean;
      context_percent?: number;
      active_execution_count: number;
      stuck_execution_count: number;
      recent_error_rate: number;
      checked_at: string;
    };
    issues: string[];
    recent_alerts: unknown[];
    uptime_percent_24h?: number;
    avg_latency_24h_ms?: number;
  }> {
    return this.request(
      "GET",
      `/api/monitoring/agents/${encodeURIComponent(agentName)}`
    );
  }

  /**
   * Trigger an immediate health check for an agent (admin only)
   */
  async triggerAgentHealthCheck(agentName: string): Promise<{
    agent_name: string;
    aggregate_status: string;
    last_check_at: string | null;
    issues: string[];
  }> {
    return this.request(
      "POST",
      `/api/monitoring/agents/${encodeURIComponent(agentName)}/check`
    );
  }

  // ============================================================================
  // Subscription Management (SUB-001)
  // ============================================================================

  /**
   * Register a new subscription token
   * Admin-only. Stores encrypted long-lived token from `claude setup-token`.
   */
  async registerSubscription(
    name: string,
    token: string,
    subscriptionType?: string,
    rateLimitTier?: string
  ): Promise<{
    id: string;
    name: string;
    subscription_type?: string;
    owner_email?: string;
    created_at: string;
  }> {
    return this.request(
      "POST",
      "/api/subscriptions",
      {
        name,
        token,
        subscription_type: subscriptionType,
        rate_limit_tier: rateLimitTier,
      }
    );
  }

  /**
   * List all subscriptions with agent assignments
   */
  async listSubscriptions(): Promise<Array<{
    id: string;
    name: string;
    subscription_type?: string;
    owner_email?: string;
    agent_count: number;
    agents: string[];
  }>> {
    return this.request("GET", "/api/subscriptions");
  }

  /**
   * Get details for a specific subscription
   */
  async getSubscription(subscriptionId: string): Promise<{
    id: string;
    name: string;
    subscription_type?: string;
    owner_email?: string;
    agent_count: number;
    agents: string[];
  }> {
    return this.request(
      "GET",
      `/api/subscriptions/${encodeURIComponent(subscriptionId)}`
    );
  }

  /**
   * Delete a subscription
   */
  async deleteSubscription(subscriptionId: string): Promise<{
    success: boolean;
    message: string;
    agents_cleared: string[];
  }> {
    return this.request(
      "DELETE",
      `/api/subscriptions/${encodeURIComponent(subscriptionId)}`
    );
  }

  /**
   * Assign a subscription to an agent
   */
  async assignSubscription(
    agentName: string,
    subscriptionName: string
  ): Promise<{
    success: boolean;
    message: string;
    agent_name: string;
    subscription_name: string;
    injection_result?: { status: string };
  }> {
    return this.request(
      "PUT",
      `/api/subscriptions/agents/${encodeURIComponent(agentName)}?subscription_name=${encodeURIComponent(subscriptionName)}`
    );
  }

  /**
   * Clear subscription assignment from an agent
   */
  async clearAgentSubscription(agentName: string): Promise<{
    success: boolean;
    message: string;
    agent_name: string;
    previous_subscription?: string;
  }> {
    return this.request(
      "DELETE",
      `/api/subscriptions/agents/${encodeURIComponent(agentName)}`
    );
  }

  /**
   * Get authentication status for an agent
   */
  async getAgentAuth(agentName: string): Promise<{
    agent_name: string;
    auth_mode: "subscription" | "api_key" | "not_configured";
    subscription_name?: string;
    subscription_id?: string;
    has_api_key: boolean;
  }> {
    return this.request(
      "GET",
      `/api/subscriptions/agents/${encodeURIComponent(agentName)}/auth`
    );
  }

  // ============================================================================
  // Nevermined Payment Integration (NVM-001)
  // ============================================================================

  /**
   * Configure Nevermined payments for an agent
   */
  async configureNevermined(
    agentName: string,
    config: {
      nvm_api_key: string;
      nvm_environment: string;
      nvm_agent_id: string;
      nvm_plan_id: string;
      credits_per_request?: number;
    }
  ): Promise<{
    id: string;
    agent_name: string;
    nvm_environment: string;
    nvm_agent_id: string;
    nvm_plan_id: string;
    credits_per_request: number;
    enabled: boolean;
  }> {
    return this.request(
      "POST",
      `/api/nevermined/agents/${encodeURIComponent(agentName)}/config`,
      config
    );
  }

  /**
   * Get Nevermined config for an agent (no decrypted key)
   */
  async getNeverminedConfig(agentName: string): Promise<{
    id: string;
    agent_name: string;
    nvm_environment: string;
    nvm_agent_id: string;
    nvm_plan_id: string;
    credits_per_request: number;
    enabled: boolean;
  }> {
    return this.request(
      "GET",
      `/api/nevermined/agents/${encodeURIComponent(agentName)}/config`
    );
  }

  /**
   * Enable or disable Nevermined payments for an agent
   */
  async toggleNevermined(
    agentName: string,
    enabled: boolean
  ): Promise<{ detail: string; enabled: boolean }> {
    return this.request(
      "PUT",
      `/api/nevermined/agents/${encodeURIComponent(agentName)}/config/toggle?enabled=${enabled}`
    );
  }

  /**
   * Get payment history for an agent
   */
  async getNeverminedPayments(
    agentName: string,
    limit: number = 50
  ): Promise<Array<{
    id: string;
    agent_name: string;
    execution_id?: string;
    action: string;
    subscriber_address?: string;
    credits_amount?: number;
    tx_hash?: string;
    remaining_balance?: number;
    success: boolean;
    error?: string;
    created_at: string;
  }>> {
    return this.request(
      "GET",
      `/api/nevermined/agents/${encodeURIComponent(agentName)}/payments?limit=${limit}`
    );
  }

  // ============================================================================
  // Channel Groups (Issue #349 - Proactive Messaging)
  // ============================================================================

  /**
   * List Telegram groups for an agent
   */
  async listTelegramGroups(agentName: string): Promise<Array<{
    id: number;
    binding_id: number;
    chat_id: string;
    chat_title: string | null;
    chat_type: string;
    trigger_mode: string;
    welcome_enabled: boolean;
    welcome_text: string | null;
    is_active: boolean;
    created_at: string;
    updated_at: string;
  }>> {
    return this.request(
      "GET",
      `/api/agents/${encodeURIComponent(agentName)}/telegram/groups`
    );
  }

  /**
   * Send a proactive message to a Telegram group
   */
  async sendTelegramGroupMessage(
    agentName: string,
    chatId: string,
    message: string
  ): Promise<{
    ok: boolean;
    message_id?: number;
    chat_id: string;
    group_title?: string;
  }> {
    return this.request(
      "POST",
      `/api/agents/${encodeURIComponent(agentName)}/telegram/groups/${encodeURIComponent(chatId)}/messages`,
      { message }
    );
  }

  /**
   * List the Slack channels an agent is bound to (#350)
   */
  async listSlackChannels(agentName: string): Promise<{
    channels: Array<{
      channel_type: string;
      channel_id: string;
      channel_name: string | null;
      team_id: string;
      workspace_name: string | null;
      is_dm_default: boolean;
    }>;
    count: number;
  }> {
    return this.request(
      "GET",
      `/api/agents/${encodeURIComponent(agentName)}/slack/channels`
    );
  }

  /**
   * Send a proactive message to a Slack channel (#350)
   */
  async sendSlackChannelMessage(
    agentName: string,
    channelId: string,
    message: string,
    threadTs?: string
  ): Promise<{
    sent: boolean;
    channel_type: string;
    channel_id: string;
    channel_name?: string | null;
    thread_ts?: string | null;
  }> {
    return this.request(
      "POST",
      `/api/agents/${encodeURIComponent(agentName)}/slack/channels/${encodeURIComponent(channelId)}/messages`,
      threadTs ? { message, thread_ts: threadTs } : { message }
    );
  }

  // ============================================================================
  // Sequential Agent Loops (#740)
  // ============================================================================

  async startAgentLoop(
    agentName: string,
    data: {
      message: string;
      max_runs: number;
      stop_signal?: string;
      delay_seconds?: number;
      timeout_per_run?: number;
      max_duration_seconds?: number;
      max_cost_usd?: number;
      no_progress_threshold?: number;
      on_failure?: "abort" | "continue";
      max_consecutive_failures?: number;
      model?: string;
      allowed_tools?: string[];
    }
  ): Promise<{
    loop_id: string;
    status: string;
    agent_name: string;
    max_runs: number;
    on_failure?: string;
  }> {
    return this.request(
      "POST",
      `/api/agents/${encodeURIComponent(agentName)}/loops`,
      data
    );
  }

  async getLoopStatus(loopId: string): Promise<unknown> {
    return this.request(
      "GET",
      `/api/loops/${encodeURIComponent(loopId)}`
    );
  }

  async stopAgentLoop(
    loopId: string
  ): Promise<{ loop_id: string; status: string }> {
    return this.request(
      "POST",
      `/api/loops/${encodeURIComponent(loopId)}/stop`
    );
  }

  // ============================================================================
  // Agent Self-Reminders (#1296)
  // ============================================================================

  async setReminder(
    agentName: string,
    data: {
      message: string;
      delay_seconds?: number;
      fire_at?: string;
      model?: string;
      timeout_seconds?: number;
      allowed_tools?: string[];
    }
  ): Promise<{
    id: string;
    agent_name: string;
    message: string;
    fire_at: string;
    status: string;
    created_at: string;
  }> {
    return this.request(
      "POST",
      `/api/agents/${encodeURIComponent(agentName)}/reminders`,
      data
    );
  }

  async listReminders(agentName: string, status?: string): Promise<unknown> {
    const query = status
      ? `?status=${encodeURIComponent(status)}`
      : "";
    return this.request(
      "GET",
      `/api/agents/${encodeURIComponent(agentName)}/reminders${query}`
    );
  }

  async cancelReminder(
    agentName: string,
    reminderId: string
  ): Promise<{ id: string; status: string }> {
    return this.request(
      "POST",
      `/api/agents/${encodeURIComponent(agentName)}/reminders/${encodeURIComponent(reminderId)}/cancel`
    );
  }

  /**
   * Export an agent's runtime data (`/home/developer/data`) inline as a
   * base64 tar (#1169). Only succeeds for small datasets — large data must
   * use the streaming download endpoint (413 otherwise). The tar embeds a
   * self-describing manifest.json.
   * @param name - Agent name
   */
  async exportAgentData(name: string): Promise<{
    agent_name: string;
    size_bytes: number;
    format: string;
    filename: string;
    tar_base64: string;
  }> {
    return this.request(
      "POST",
      `/api/agents/${encodeURIComponent(name)}/data/export?format=base64`
    );
  }

  /**
   * Restore a base64 tar into an agent's `data/` directory (#1169). The
   * backend delegates to the agent-server restore primitive, which enforces
   * the `data/**` allowlist and rejects path traversal. Uploaded as multipart
   * (the binary doesn't fit the JSON request path).
   * @param name - Agent name
   * @param tarBase64 - base64-encoded tar, typically from exportAgentData
   */
  async importAgentData(
    name: string,
    tarBase64: string
  ): Promise<{
    agent_name: string;
    restored: string[];
    skipped: string[];
    bytes_received: number;
  }> {
    if (!this.token) {
      throw new Error(
        "Not authenticated. Call authenticate() first or setToken()."
      );
    }
    const buf = Buffer.from(tarBase64, "base64");
    const form = new FormData();
    form.append(
      "tarball",
      new Blob([buf], { type: "application/x-tar" }),
      "data.tar"
    );
    const response = await fetch(
      `${this.baseUrl}/api/agents/${encodeURIComponent(name)}/data/import`,
      {
        method: "POST",
        headers: { Authorization: `Bearer ${this.token}` },
        body: form,
      }
    );
    if (!response.ok) {
      const error = await response.text();
      throw new Error(`API error (${response.status}): ${error}`);
    }
    return response.json() as Promise<{
      agent_name: string;
      restored: string[];
      skipped: string[];
      bytes_received: number;
    }>;
  }

  // ============================================================================
  // Git Sync (#905) — direct, deterministic (non-LLM) git operations
  // ============================================================================

  /** Live git status for an agent (branch, remote, changes, sync_status). */
  async getGitStatus(name: string, requestId?: string): Promise<unknown> {
    return this.request<unknown>(
      "GET",
      `/api/agents/${encodeURIComponent(name)}/git/status`,
      undefined,
      false,
      requestId,
    );
  }

  /** Stage, commit, and push agent changes to the working branch. */
  async gitSync(
    name: string,
    body: { message?: string; paths?: string[]; strategy?: string },
    requestId?: string,
  ): Promise<unknown> {
    return this.request<unknown>(
      "POST",
      `/api/agents/${encodeURIComponent(name)}/git/sync`,
      body,
      false,
      requestId,
    );
  }

  /** Recent commit history for an agent. */
  async getGitLog(name: string, limit: number, requestId?: string): Promise<unknown> {
    return this.request<unknown>(
      "GET",
      `/api/agents/${encodeURIComponent(name)}/git/log?limit=${encodeURIComponent(String(limit))}`,
      undefined,
      false,
      requestId,
    );
  }

  /** Pull latest changes from GitHub with the given conflict strategy. */
  async gitPull(name: string, strategy: string, requestId?: string): Promise<unknown> {
    return this.request<unknown>(
      "POST",
      `/api/agents/${encodeURIComponent(name)}/git/pull`,
      { strategy },
      false,
      requestId,
    );
  }

  /** Persisted git sync-state row (#389) for an agent. */
  async getGitSyncState(name: string, requestId?: string): Promise<unknown> {
    return this.request<unknown>(
      "GET",
      `/api/agents/${encodeURIComponent(name)}/git/sync-state`,
      undefined,
      false,
      requestId,
    );
  }

  /** Destructive recovery: adopt origin/main, preserving instance state (#384). */
  async resetToMainPreserveState(name: string, requestId?: string): Promise<unknown> {
    return this.request<unknown>(
      "POST",
      `/api/agents/${encodeURIComponent(name)}/git/reset-to-main-preserve-state`,
      undefined,
      false,
      requestId,
    );
  }

  // ==========================================================================
  // A2A control plane (trinity-enterprise#160)
  // The management endpoints (config/exposure/allow-list/endpoints) proxy the
  // ENTITLEMENT-GATED enterprise router (`/api/enterprise/a2a/*`) — a 403 in an
  // unentitled build, a 404 in an OSS-only build. The served card is the OSS
  // #737 endpoint.
  // ==========================================================================

  /** Full A2A control state for one agent (exposure, card URL, allow-list, endpoints). */
  async getA2AConfig(name: string): Promise<unknown> {
    return this.request<unknown>(
      "GET",
      `/api/enterprise/a2a/${encodeURIComponent(name)}/config`,
    );
  }

  /** Toggle whether the agent is exposed over A2A. */
  async setA2AExposure(name: string, enabled: boolean): Promise<unknown> {
    return this.request<unknown>(
      "PUT",
      `/api/enterprise/a2a/${encodeURIComponent(name)}/exposure`,
      { enabled },
    );
  }

  /** The served A2A Agent Card JSON (OSS #737 endpoint). */
  async getA2ACard(name: string): Promise<unknown> {
    return this.request<unknown>(
      "GET",
      `/api/agents/${encodeURIComponent(name)}/a2a/agent-card`,
    );
  }

  /** Add and/or remove inbound identities on the agent's A2A allow-list. */
  async updateA2AInboundAllowlist(
    name: string,
    body: { add?: string[]; remove?: string[] },
  ): Promise<unknown> {
    return this.request<unknown>(
      "POST",
      `/api/enterprise/a2a/${encodeURIComponent(name)}/inbound-allowlist`,
      body,
    );
  }

  /** Register (or update by name) an outbound external A2A endpoint. */
  async registerA2AEndpoint(
    name: string,
    body: { name: string; url: string; credentials?: string },
  ): Promise<unknown> {
    return this.request<unknown>(
      "POST",
      `/api/enterprise/a2a/${encodeURIComponent(name)}/endpoints`,
      body,
    );
  }

  /** List the agent's registered outbound endpoints (credentials never returned). */
  async listA2AEndpoints(name: string): Promise<unknown> {
    return this.request<unknown>(
      "GET",
      `/api/enterprise/a2a/${encodeURIComponent(name)}/endpoints`,
    );
  }

  /** Remove one outbound endpoint by id. */
  async removeA2AEndpoint(name: string, endpointId: string): Promise<unknown> {
    return this.request<unknown>(
      "DELETE",
      `/api/enterprise/a2a/${encodeURIComponent(name)}/endpoints/${encodeURIComponent(endpointId)}`,
    );
  }
  // a2a_exposed is surfaced natively on GET /api/agents (ent#157), so list_agents
  // / get_agent carry it without a separate fetch — no client merge needed.

  // ==========================================================================
  // A2A runtime — OUTBOUND calls (abilityai/trinity#736)
  //
  // Distinct from the ent#160 control plane above in two ways that matter:
  //  - these hit OSS routes on /api/agents, NOT the entitlement-gated
  //    /api/enterprise/a2a/*. The epic's ruling is "Outbound = OSS", so a
  //    404 here means the kill switch is off or the build predates the
  //    feature — never "not licensed".
  //  - they carry their own AbortController. `request()` uses a bare `fetch`
  //    with no signal, and nginx is `proxy_read_timeout 86400`, so without one
  //    the MCP client's own 30-60s gateway timeout fires first: the agent sees
  //    `fetch failed` while the credentialed outbound call completes anyway.
  //    The side effect happened and, because `task_id` only comes back on
  //    success, the agent would hold no handle to poll. Aborting on OUR side
  //    first lets us return a structured receipt instead (the #914 shape).
  // ==========================================================================

  /** Bound below the MCP client's gateway abort so WE give up first (see above). */
  private a2aTimeoutMs(): number {
    // `||` not `??`: a set-but-empty env var must coalesce to the default.
    // `Number('')` is 0, which would abort every call instantly (the #1076 class).
    return Number(process.env.MCP_A2A_TIMEOUT_MS || 40000);
  }

  private async a2aFetch(path: string, body: unknown): Promise<unknown> {
    if (!this.token) {
      throw new Error("Not authenticated. Call authenticate() first or setToken().");
    }
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), this.a2aTimeoutMs());
    try {
      const response = await fetch(`${this.baseUrl}${path}`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${this.token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!response.ok) {
        throw new ApiError(response.status, await response.text(), response.headers);
      }
      return await response.json();
    } finally {
      clearTimeout(timeoutId);
    }
  }

  /** Task an external A2A agent through a PRE-REGISTERED endpoint (#736). */
  async callA2AAgent(
    name: string,
    body: {
      endpoint: string;
      message: string;
      dedup_label: string;
      context_id?: string;
      task_id?: string;
      execution_id?: string;
    },
  ): Promise<unknown> {
    return this.a2aFetch(`/api/agents/${encodeURIComponent(name)}/a2a/call`, body);
  }

  /** Poll a remote A2A task by id on the same registered endpoint (#736). */
  async getA2ATask(
    name: string,
    body: { endpoint: string; task_id: string },
  ): Promise<unknown> {
    return this.a2aFetch(`/api/agents/${encodeURIComponent(name)}/a2a/task`, body);
  }
}
