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

  constructor(status: number, body: string, headers?: Headers) {
    super(`API error (${status}): ${body}`);
    this.name = "ApiError";
    this.status = status;
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

    if (!response.ok) {
      throw new Error(`Authentication failed: ${response.statusText}`);
    }

    const data = (await response.json()) as TokenResponse;
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
    requestId?: string
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
        return this._fetch(method, path, body, true, requestId);
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
  async request<T>(
    method: string,
    path: string,
    body?: unknown,
    isRetry: boolean = false,
    requestId?: string
  ): Promise<T> {
    const response = await this._fetch(method, path, body, isRetry, requestId);

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
   */
  async createAgent(config: AgentConfig): Promise<Agent> {
    return this.request<Agent>("POST", "/api/agents", config);
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
   * Generate ephemeral SSH credentials for direct agent access
   * For key auth, client supplies their public key (private key never leaves client)
   * For password auth, server generates ephemeral password
   * @param name - Agent name
   * @param ttlHours - Credential validity in hours (0.1-24, default: 4)
   * @param authMethod - Authentication method: "key" (default) or "password"
   * @param publicKey - Client's SSH public key (required for "key" auth)
   */
  async createSshAccess(
    name: string,
    ttlHours: number = 4,
    authMethod: "key" | "password" = "key",
    publicKey?: string
  ): Promise<SshAccessResponse> {
    const body: Record<string, unknown> = { ttl_hours: ttlHours, auth_method: authMethod };
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

    // Add MCP key info headers for execution origin tracking (AUDIT-001)
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

    // Add MCP key info headers for execution origin tracking (AUDIT-001)
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
   */
  async triggerAgentSchedule(
    agentName: string,
    scheduleId: string
  ): Promise<ScheduleTriggerResult> {
    return this.request<ScheduleTriggerResult>(
      "POST",
      `/api/agents/${encodeURIComponent(agentName)}/schedules/${encodeURIComponent(scheduleId)}/trigger`
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
      model?: string;
      allowed_tools?: string[];
    }
  ): Promise<{
    loop_id: string;
    status: string;
    agent_name: string;
    max_runs: number;
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
}
