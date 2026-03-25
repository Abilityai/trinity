// Types for Trinity API responses

export interface Agent {
  name: string;
  type: string;
  status: string;
  port: number;  // SSH port only - UI no longer exposed externally
  created: string;
  resources: {
    cpu: string;
    memory: string;
  };
  container_id?: string;
}

export interface AgentConfig {
  name: string;
  type?: string;
  base_image?: string;
  resources?: {
    cpu: string;
    memory: string;
  };
  tools?: string[];
  mcp_servers?: string[];
  custom_instructions?: string;
  port?: number;  // SSH port - ui_port removed for security
  template?: string;
  source_branch?: string;  // Branch to track (default: main). Can also use github:owner/repo@branch syntax.
}

export interface ChatResponse {
  response: string;
  timestamp: string;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  timestamp: string;
}

export interface Template {
  id: string;
  display_name: string;
  description: string;
  mcp_servers: string[];
  resources: {
    cpu: string;
    memory: string;
  };
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
}

// Agent-to-Agent Collaboration Types

/**
 * MCP Authentication Context
 * Extends Record<string, unknown> to satisfy FastMCP's session type requirements
 */
export interface McpAuthContext extends Record<string, unknown> {
  userId: string;        // Username of the key owner
  userEmail?: string;    // Email of the key owner
  keyId?: string;        // MCP API key ID (AUDIT-001: for execution origin tracking)
  keyName: string;       // Name of the MCP API key
  agentName?: string;    // Agent name if scope is 'agent' or 'system' (for agent-to-agent)
  scope: "user" | "agent" | "system"; // Key scope: user=human, agent=regular agent, system=system agent (bypasses all permissions)
  mcpApiKey?: string;    // The actual MCP API key (for user-scoped requests to Trinity backend)
}

export interface AgentAccessInfo {
  name: string;
  owner: string;         // Owner username
  is_shared: boolean;    // Whether agent is shared
}

export interface AgentAccessCheckResult {
  allowed: boolean;
  reason?: string;       // Denial reason if not allowed
}

// Agent Template Info Types

export interface AgentCommand {
  name: string;
  description: string;
}

export interface AgentTemplateInfo {
  has_template: boolean;
  agent_name: string;
  name?: string;
  display_name?: string;
  tagline?: string;
  description?: string;
  version?: string;
  author?: string;
  type?: string;
  resources?: {
    cpu?: string;
    memory?: string;
  };
  capabilities?: string[];
  commands?: AgentCommand[];
  mcp_servers?: string[];
  sub_agents?: string[];
  platforms?: string[];
  tools?: string[];
  skills?: string[];
  use_cases?: string[];
  status?: "running" | "stopped";
}

// SSH Access Types

export interface SshConnectionInfo {
  command: string;
  host: string;
  port: number;
  user: string;
  password?: string;  // Only for password auth
}

export interface SshAccessResponse {
  status: string;
  agent: string;
  auth_method: "key" | "password";
  connection: SshConnectionInfo;
  private_key?: string;  // Only for key auth
  expires_at: string;
  expires_in_hours: number;
  instructions: string[];
}

// Schedule Management Types

export interface Schedule {
  id: string;
  agent_name: string;
  name: string;
  cron_expression: string;
  message: string;
  enabled: boolean;
  timezone: string;
  description?: string;
  created_at: string;
  updated_at: string;
  last_run_at?: string;
  next_run_at?: string;
  timeout_seconds: number;
  allowed_tools?: string[];
  model?: string;
}

export interface ScheduleCreate {
  name: string;
  cron_expression: string;
  message: string;
  timezone?: string;
  description?: string;
  enabled?: boolean;
  timeout_seconds?: number;
  allowed_tools?: string[];
  model?: string;
}

export interface ScheduleUpdate {
  name?: string;
  cron_expression?: string;
  message?: string;
  timezone?: string;
  description?: string;
  enabled?: boolean;
  timeout_seconds?: number;
  allowed_tools?: string[];
  model?: string;
}

export interface ScheduleExecution {
  id: string;
  schedule_id: string;
  agent_name: string;
  status: "pending" | "running" | "success" | "failed" | "cancelled";
  started_at: string;
  completed_at?: string;
  duration_ms?: number;
  message: string;
  response?: string;
  error?: string;
  triggered_by: "schedule" | "manual" | "mcp";
  context_used?: number;
  context_max?: number;
  cost?: number;
  tool_calls?: string;
  execution_log?: string;
  model_used?: string;
  claude_session_id?: string;
  source_agent_name?: string;
  source_user_email?: string;
}

// Execution Query Types (MCP-007)

export interface ActivityTimelineResponse {
  count: number;
  start_time?: string;
  end_time?: string;
  activity_types?: string[];
  activities: ActivityEntry[];
}

export interface ActivityEntry {
  id: string;
  agent_name: string;
  activity_type: string;
  activity_state: string;
  started_at: string;
  completed_at?: string;
  duration_ms?: number;
  triggered_by: string;
  details?: string;
  error?: string;
}

export interface ScheduleToggleResult {
  status: "enabled" | "disabled";
  schedule_id: string;
  schedule_name?: string;
  next_run_at?: string;
}

export interface ScheduleTriggerResult {
  status: "triggered";
  schedule_id: string;
  execution_id: string;
  message?: string;
}

// Subscription Management Types (SUB-001)

export interface Subscription {
  id: string;
  name: string;
  subscription_type?: string;
  rate_limit_tier?: string;
  owner_id: number;
  owner_email?: string;
  created_at: string;
  updated_at: string;
  agent_count: number;
}

export interface SubscriptionWithAgents extends Subscription {
  agents: string[];
}

export interface AgentAuthStatus {
  agent_name: string;
  auth_mode: "subscription" | "api_key" | "not_configured";
  subscription_name?: string;
  subscription_id?: string;
  has_api_key: boolean;
}
