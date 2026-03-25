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
   * Public request method for custom API calls
   */
  async request<T>(
    method: string,
    path: string,
    body?: unknown,
    isRetry: boolean = false
  ): Promise<T> {
    if (!this.token) {
      throw new Error("Not authenticated. Call authenticate() first or setToken().");
    }

    const headers: Record<string, string> = {
      Authorization: `Bearer ${this.token}`,
    };

    if (body) {
      headers["Content-Type"] = "application/json";
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
        return this.request<T>(method, path, body, true);
      }
    }

    if (!response.ok) {
      const error = await response.text();
      throw new Error(`API error (${response.status}): ${error}`);
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
  async injectCredentials(name: string, files: Record<string, string>): Promise<{
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
      { files }
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
   * Returns private key (one-time display) or password and connection command
   * @param name - Agent name
   * @param ttlHours - Credential validity in hours (0.1-24, default: 4)
   * @param authMethod - Authentication method: "key" (default) or "password"
   */
  async createSshAccess(
    name: string,
    ttlHours: number = 4,
    authMethod: "key" | "password" = "key"
  ): Promise<SshAccessResponse> {
    return this.request<SshAccessResponse>(
      "POST",
      `/api/agents/${encodeURIComponent(name)}/ssh-access`,
      { ttl_hours: ttlHours, auth_method: authMethod }
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
    mcpKeyInfo?: { keyId?: string; keyName?: string }
  ): Promise<ChatResponse | { error: string; queue_status: "busy" | "queue_full"; retry_after: number; agent: string; details?: Record<string, unknown> }> {
    // Prepare headers
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...(this.token && { Authorization: `Bearer ${this.token}` }),
      "X-Via-MCP": "true",  // Always mark as MCP call for task tracking
    };

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

    const response = await fetch(`${this.baseUrl}/api/agents/${encodeURIComponent(name)}/chat`, {
      method: "POST",
      headers,
      body: JSON.stringify({ message }),
    });

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
    },
    sourceAgent?: string,
    mcpKeyInfo?: { keyId?: string; keyName?: string }
  ): Promise<ChatResponse | { status: "accepted"; execution_id: string; agent_name: string; message: string; async_mode: true }> {
    // Prepare headers
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...(this.token && { Authorization: `Bearer ${this.token}` }),
      "X-Via-MCP": "true",  // Always mark as MCP call for task tracking
    };

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
    };

    // Async mode returns immediately; sync mode waits for full execution
    const timeout = options?.async_mode ? 30 : (options?.timeout_seconds || 600) + 10;

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
}
