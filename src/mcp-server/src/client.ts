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
} from "./types.js";

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

    console.log(`[CLIENT] ${method} ${path} - Token: ${this.token.substring(0, 20)}... (length: ${this.token.length})`);

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
   * Reload credentials on a running agent
   * Fetches latest credentials from Redis and pushes them to the agent container
   */
  async reloadCredentials(name: string): Promise<{
    message: string;
    credential_count: number;
    updated_files: string[];
    note: string;
  }> {
    return this.request<{
      message: string;
      credential_count: number;
      updated_files: string[];
      note: string;
    }>(
      "POST",
      `/api/agents/${encodeURIComponent(name)}/credentials/reload`
    );
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

  // ============================================================================
  // Chat & Communication
  // ============================================================================

  /**
   * Send a message to an agent and get a response
   * @param sourceAgent - Optional source agent name for agent-to-agent collaboration tracking
   *
   * Returns ChatResponse on success, or a queue status object if agent is busy (429).
   */
  async chat(name: string, message: string, sourceAgent?: string): Promise<ChatResponse | { error: string; queue_status: "busy" | "queue_full"; retry_after: number; agent: string; details?: Record<string, unknown> }> {
    // Prepare headers
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...(this.token && { Authorization: `Bearer ${this.token}` }),
    };

    // Add X-Source-Agent header for collaboration tracking
    if (sourceAgent) {
      headers["X-Source-Agent"] = sourceAgent;
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
   * @param options - Optional parameters
   * @param sourceAgent - Optional source agent name for collaboration tracking
   */
  async task(
    name: string,
    message: string,
    options?: {
      model?: string;
      allowed_tools?: string[];
      system_prompt?: string;
      timeout_seconds?: number;
    },
    sourceAgent?: string
  ): Promise<ChatResponse> {
    // Prepare headers
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...(this.token && { Authorization: `Bearer ${this.token}` }),
    };

    // Add X-Source-Agent header for collaboration tracking
    if (sourceAgent) {
      headers["X-Source-Agent"] = sourceAgent;
    }

    const body = {
      message,
      model: options?.model,
      allowed_tools: options?.allowed_tools,
      system_prompt: options?.system_prompt,
      timeout_seconds: options?.timeout_seconds,
    };

    const timeout = (options?.timeout_seconds || 300) + 10; // Add buffer

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
}
