/**
 * Trinity MCP Server
 *
 * FastMCP server that exposes Trinity agent management and chat capabilities
 * via the Model Context Protocol (MCP).
 */

import { FastMCP } from "fastmcp";
import { TrinityClient } from "./client.js";
import { createAgentTools } from "./tools/agents.js";
import { createChatTools } from "./tools/chat.js";
import { createSystemTools } from "./tools/systems.js";
import { createDocsTools } from "./tools/docs.js";
import { createSkillsTools } from "./tools/skills.js";
import { createScheduleTools } from "./tools/schedules.js";
import { createTagTools } from "./tools/tags.js";
import { createNotificationTools } from "./tools/notifications.js";
import { createSubscriptionTools } from "./tools/subscriptions.js";
import { createMonitoringTools } from "./tools/monitoring.js";
import { createNeverminedTools } from "./tools/nevermined.js";
import { createExecutionTools } from "./tools/executions.js";
import { createEventTools } from "./tools/events.js";
import { createChannelTools } from "./tools/channels.js";
import { createMessageTools } from "./tools/messages.js";
import { createFileTools } from "./tools/files.js";
import { createMemoryTools } from "./tools/memory.js";
import { createLoopTools } from "./tools/loops.js";
import { withAudit } from "./audit.js";
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
}

export interface McpApiKeyValidationResult {
  valid: boolean;
  key_id?: string;      // MCP API key ID (AUDIT-001)
  user_id?: string;
  user_email?: string;
  key_name?: string;
  agent_name?: string;  // Agent name if scope is 'agent' or 'system'
  scope?: "user" | "agent" | "system";  // Key scope: user=human, agent=regular agent, system=system agent (bypasses permissions)
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
  const {
    name = "trinity-orchestrator",
    version = "1.0.0" as const,
    trinityApiUrl = process.env.TRINITY_API_URL || "http://localhost:8000",
    trinityApiToken = process.env.TRINITY_API_TOKEN,
    trinityUsername = process.env.TRINITY_USERNAME || "admin",
    trinityPassword = process.env.TRINITY_PASSWORD,
    port = parseInt(process.env.MCP_PORT || "8080", 10),
    requireApiKey = process.env.MCP_REQUIRE_API_KEY === "true",
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
              scope: scope as "user" | "agent" | "system",
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

  // SEC-001 Phase 3: Wrap tool execute functions with audit logging.
  // withAudit captures tool name, auth context, timing, and success/failure,
  // then fires a non-blocking POST to the backend internal audit endpoint.
  function addToolWithAudit(tool: any): void {
    const wrapped = {
      ...tool,
      execute: withAudit(tool.name, tool.execute),
    };
    server.addTool(wrapped);
  }

  // Helper to register all tools from a tool group with audit wrapping
  function addAllTools(tools: Record<string, any>): void {
    for (const tool of Object.values(tools)) {
      addToolWithAudit(tool);
    }
  }

  // Build tool groups once, then register + count (SEC-001 Phase 3).
  const toolGroups: Record<string, any>[] = [
    createAgentTools(client, requireApiKey),
    createChatTools(client, requireApiKey),
    createSystemTools(client, requireApiKey),
    createDocsTools(),
    createSkillsTools(client, requireApiKey),
    createScheduleTools(client, requireApiKey),
    createTagTools(client, requireApiKey),
    createNotificationTools(client, requireApiKey),
    createFileTools(client, requireApiKey),       // FILES-001 — outbound file sharing
    createSubscriptionTools(client, requireApiKey),
    createMonitoringTools(client, requireApiKey),
    createNeverminedTools(client, requireApiKey),
    createExecutionTools(client, requireApiKey),
    createEventTools(client, requireApiKey),
    createChannelTools(client, requireApiKey),
    createMessageTools(client, requireApiKey),
    createMemoryTools(client, requireApiKey),     // MEM-001 write path (#888)
    createLoopTools(client, requireApiKey),       // Sequential agent loops (#740)
  ];
  for (const group of toolGroups) {
    addAllTools(group);
  }
  const totalTools = toolGroups.reduce((sum, g) => sum + Object.keys(g).length, 0);
  console.log(`Registered ${totalTools} tools with audit wrapping (SEC-001 Phase 3)`);

  return { server, port, client, requireApiKey };
}
