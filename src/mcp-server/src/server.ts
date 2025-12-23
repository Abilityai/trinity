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
    trinityPassword = process.env.TRINITY_PASSWORD || "changeme",
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

  // Register agent management tools
  // Auth context will be passed via tool execution context (context.session)
  const agentTools = createAgentTools(client, requireApiKey);
  server.addTool(agentTools.listAgents);
  server.addTool(agentTools.getAgent);
  server.addTool(agentTools.createAgent);
  server.addTool(agentTools.deleteAgent);
  server.addTool(agentTools.startAgent);
  server.addTool(agentTools.stopAgent);
  server.addTool(agentTools.listTemplates);
  server.addTool(agentTools.reloadCredentials);
  server.addTool(agentTools.getCredentialStatus);
  server.addTool(agentTools.deployLocalAgent);

  // Register chat tools with auth context for access control
  const chatTools = createChatTools(client, requireApiKey);
  server.addTool(chatTools.chatWithAgent);
  server.addTool(chatTools.getChatHistory);
  server.addTool(chatTools.getAgentLogs);

  // Register system management tools (Phase 3)
  const systemTools = createSystemTools(client, requireApiKey);
  server.addTool(systemTools.deploySystem);
  server.addTool(systemTools.listSystems);
  server.addTool(systemTools.restartSystem);
  server.addTool(systemTools.getSystemManifest);

  // Register documentation tools
  const docsTools = createDocsTools();
  server.addTool(docsTools.getAgentRequirements);

  const totalTools = Object.keys(agentTools).length + Object.keys(chatTools).length + Object.keys(systemTools).length + Object.keys(docsTools).length;
  console.log(`Registered ${totalTools} tools (${Object.keys(agentTools).length} agent, ${Object.keys(chatTools).length} chat, ${Object.keys(systemTools).length} system, ${Object.keys(docsTools).length} docs)`);

  return { server, port, client, requireApiKey };
}
