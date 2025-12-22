/**
 * Agent Management Tools
 *
 * MCP tools for managing Trinity agents: list, get, create, delete, start, stop
 */

import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

/**
 * Create agent management tools with the given client
 * @param client - Base Trinity client (provides base URL, no auth when requireApiKey=true)
 * @param requireApiKey - Whether API key authentication is enabled
 */
export function createAgentTools(
  client: TrinityClient,
  requireApiKey: boolean
) {
  /**
   * Get Trinity client with appropriate authentication
   * When requireApiKey is true, REQUIRES MCP API key from auth context
   * When requireApiKey is false, uses the base client (backward compatibility)
   */
  const getClient = (authContext?: McpAuthContext): TrinityClient => {
    if (requireApiKey) {
      // MCP API key is REQUIRED - no fallback
      if (!authContext?.mcpApiKey) {
        throw new Error("MCP API key authentication required but no API key found in request context");
      }
      // Create new client instance authenticated with user's MCP API key
      const userClient = new TrinityClient(client.getBaseUrl());
      userClient.setToken(authContext.mcpApiKey);
      return userClient;
    }
    // API key auth disabled - use base client (backward compatibility)
    return client;
  };

  return {
    // ========================================================================
    // list_agents - List all agents
    // ========================================================================
    listAgents: {
      name: "list_agents",
      description:
        "List all agents in the Trinity platform with their status, type, and resource allocation. " +
        "Returns an array of agents with details like name, status (running/stopped), ports, and creation time.",
      parameters: z.object({}),
      execute: async (_params: unknown, context?: { session?: McpAuthContext }) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);
        const agents = await apiClient.listAgents();

        // Phase 11.1: System-scoped keys see all agents (no filtering)
        if (authContext?.scope === "system") {
          console.log(`[list_agents] System agent - showing all ${agents.length} agents`);
          return JSON.stringify(agents, null, 2);
        }

        // Phase 9.10: Filter agents for agent-scoped keys
        // Agent-scoped keys only see permitted agents + self
        if (authContext?.scope === "agent" && authContext?.agentName) {
          const callerAgentName = authContext.agentName;
          const permittedAgents = await apiClient.getPermittedAgents(callerAgentName);

          // Include self and permitted agents
          const allowedNames = new Set([callerAgentName, ...permittedAgents]);
          const filteredAgents = agents.filter((a: { name: string }) => allowedNames.has(a.name));

          console.log(`[list_agents] Agent '${callerAgentName}' filtered: ${filteredAgents.length}/${agents.length} agents visible`);

          return JSON.stringify(filteredAgents, null, 2);
        }

        // User-scoped keys see all accessible agents (existing behavior)
        return JSON.stringify(agents, null, 2);
      },
    },

    // ========================================================================
    // get_agent - Get specific agent details
    // ========================================================================
    getAgent: {
      name: "get_agent",
      description:
        "Get detailed information about a specific agent by name. " +
        "Returns the agent's status, type, port assignments, resource limits, and container ID.",
      parameters: z.object({
        name: z.string().describe("The name of the agent to retrieve"),
      }),
      execute: async ({ name }: { name: string }, context?: { session?: McpAuthContext }) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);
        const agent = await apiClient.getAgent(name);
        return JSON.stringify(agent, null, 2);
      },
    },

    // ========================================================================
    // create_agent - Create a new agent
    // ========================================================================
    createAgent: {
      name: "create_agent",
      description:
        "Create a new agent in the Trinity platform. " +
        "You can specify a template to use pre-configured settings, or customize the agent type, resources, and tools. " +
        "The agent will be started automatically after creation.",
      parameters: z.object({
        name: z
          .string()
          .describe(
            "Unique name for the agent. Will be sanitized for Docker compatibility."
          ),
        type: z
          .string()
          .optional()
          .describe(
            "Agent type (e.g., 'business-assistant', 'code-developer'). Default: 'business-assistant'"
          ),
        template: z
          .string()
          .optional()
          .describe(
            "Template ID to use for agent configuration (e.g., 'ruby-social-media-agent'). " +
            "Templates include pre-configured .claude directories, MCP servers, and instructions."
          ),
        resources: z
          .object({
            cpu: z.string().optional().describe("CPU limit (e.g., '2')"),
            memory: z
              .string()
              .optional()
              .describe("Memory limit (e.g., '4g')"),
          })
          .optional()
          .describe("Resource limits for the agent container"),
        tools: z
          .array(z.string())
          .optional()
          .describe("List of tools to enable (e.g., ['filesystem', 'web_search'])"),
        mcp_servers: z
          .array(z.string())
          .optional()
          .describe("MCP servers to configure for the agent"),
        custom_instructions: z
          .string()
          .optional()
          .describe("Custom behavioral instructions for the agent"),
      }),
      execute: async (
        args: {
          name: string;
          type?: string;
          template?: string;
          resources?: { cpu?: string; memory?: string };
          tools?: string[];
          mcp_servers?: string[];
          custom_instructions?: string;
        },
        context: any
      ) => {
        const config = {
          name: args.name,
          type: args.type,
          template: args.template,
          resources: args.resources
            ? {
                cpu: args.resources.cpu || "2",
                memory: args.resources.memory || "4g",
              }
            : undefined,
          tools: args.tools,
          mcp_servers: args.mcp_servers,
          custom_instructions: args.custom_instructions,
        };

        // Get auth context from FastMCP session (set by authenticate callback)
        const authContext = requireApiKey ? context?.session : undefined;
        console.log("[CREATE_AGENT] Auth context:", {
          hasContext: !!context,
          hasSession: !!context?.session,
          hasAuthContext: !!authContext,
          userId: authContext?.userId,
          userEmail: authContext?.userEmail,
          scope: authContext?.scope,
          hasMcpApiKey: !!authContext?.mcpApiKey,
          mcpApiKeyPrefix: authContext?.mcpApiKey?.substring(0, 20),
        });

        const apiClient = getClient(authContext);
        console.log("[CREATE_AGENT] Created API client, calling backend...");

        const agent = await apiClient.createAgent(config);
        console.log("[CREATE_AGENT] Agent created successfully:", agent.name);
        return JSON.stringify(agent, null, 2);
      },
    },

    // ========================================================================
    // delete_agent - Remove an agent
    // ========================================================================
    deleteAgent: {
      name: "delete_agent",
      description:
        "Delete an agent from the Trinity platform. " +
        "This will stop the agent container and remove it. " +
        "Requires admin access. This action is irreversible.",
      parameters: z.object({
        name: z.string().describe("The name of the agent to delete"),
      }),
      execute: async ({ name }: { name: string }, context?: { session?: McpAuthContext }) => {
        const authContext = context?.session;

        // Phase 11.1: Prevent system agent from deleting itself
        // This is an extra safety check - backend also blocks this
        if (authContext?.scope === "system" && authContext?.agentName === name) {
          console.log(`[delete_agent] System agent cannot delete itself`);
          return JSON.stringify({
            error: "Cannot delete system agent",
            reason: "System agents cannot be deleted. Use re-initialization instead.",
            agent: name
          }, null, 2);
        }

        const apiClient = getClient(authContext);
        const result = await apiClient.deleteAgent(name);
        return result.message;
      },
    },

    // ========================================================================
    // start_agent - Start a stopped agent
    // ========================================================================
    startAgent: {
      name: "start_agent",
      description:
        "Start a stopped agent. " +
        "Use this to restart an agent that was previously stopped. " +
        "The agent must already exist in the platform.",
      parameters: z.object({
        name: z.string().describe("The name of the agent to start"),
      }),
      execute: async ({ name }: { name: string }, context?: { session?: McpAuthContext }) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);
        const result = await apiClient.startAgent(name);
        return result.message;
      },
    },

    // ========================================================================
    // stop_agent - Stop a running agent
    // ========================================================================
    stopAgent: {
      name: "stop_agent",
      description:
        "Stop a running agent. " +
        "This gracefully stops the agent container but preserves its configuration. " +
        "Use start_agent to restart it later.",
      parameters: z.object({
        name: z.string().describe("The name of the agent to stop"),
      }),
      execute: async ({ name }: { name: string }, context?: { session?: McpAuthContext }) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);
        const result = await apiClient.stopAgent(name);
        return result.message;
      },
    },

    // ========================================================================
    // list_templates - List available agent templates
    // ========================================================================
    listTemplates: {
      name: "list_templates",
      description:
        "List all available agent templates. " +
        "Templates provide pre-configured agent setups with .claude directories, MCP servers, and custom instructions. " +
        "Use a template ID with create_agent to quickly spin up a specialized agent.",
      parameters: z.object({}),
      execute: async (_params: unknown, context?: { session?: McpAuthContext }) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);
        const templates = await apiClient.listTemplates();
        return JSON.stringify(templates, null, 2);
      },
    },

    // ========================================================================
    // reload_credentials - Reload credentials on a running agent
    // ========================================================================
    reloadCredentials: {
      name: "reload_credentials",
      description:
        "Reload credentials on a running agent. " +
        "This fetches the latest credentials from the Trinity credential store and pushes them to the agent container. " +
        "Use this after adding or updating credentials to apply them without restarting the agent. " +
        "The agent must be running for this to work.",
      parameters: z.object({
        name: z.string().describe("The name of the agent to reload credentials for"),
      }),
      execute: async ({ name }: { name: string }, context?: { session?: McpAuthContext }) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);
        const result = await apiClient.reloadCredentials(name);
        return JSON.stringify(result, null, 2);
      },
    },

    // ========================================================================
    // get_credential_status - Get credential status from a running agent
    // ========================================================================
    getCredentialStatus: {
      name: "get_credential_status",
      description:
        "Get the credential status from a running agent. " +
        "Returns information about credential files inside the agent container, " +
        "including whether .env and .mcp.json exist and when they were last modified.",
      parameters: z.object({
        name: z.string().describe("The name of the agent to check credential status for"),
      }),
      execute: async ({ name }: { name: string }, context?: { session?: McpAuthContext }) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);
        const status = await apiClient.getCredentialStatus(name);
        return JSON.stringify(status, null, 2);
      },
    },

    // ========================================================================
    // deploy_local_agent - Deploy a Trinity-compatible local agent
    // ========================================================================
    deployLocalAgent: {
      name: "deploy_local_agent",
      description:
        "Deploy a Trinity-compatible local agent to Trinity platform. " +
        "Packages the agent directory, auto-imports credentials from .env, and deploys. " +
        "The agent must have a valid template.yaml file with 'name' and 'resources' fields. " +
        "If agent name exists, creates new version (my-agent-2) and stops old one.",
      parameters: z.object({
        path: z.string().describe("Absolute path to the local agent directory"),
        name: z
          .string()
          .optional()
          .describe("Agent name (defaults to name from template.yaml)"),
        include_env: z
          .boolean()
          .default(true)
          .describe("Include credentials from local .env file (default: true)"),
      }),
      execute: async (
        args: { path: string; name?: string; include_env?: boolean },
        context?: { session?: McpAuthContext }
      ) => {
        const fs = await import("fs");
        const path_module = await import("path");
        const { execSync } = await import("child_process");

        const authContext = context?.session;
        const apiClient = getClient(authContext);

        const agentPath = args.path;
        const includeEnv = args.include_env !== false;

        // 1. Validate path exists
        if (!fs.existsSync(agentPath)) {
          throw new Error(`Directory does not exist: ${agentPath}`);
        }

        // 2. Check for template.yaml (Trinity-compatible check)
        const templatePath = path_module.join(agentPath, "template.yaml");
        if (!fs.existsSync(templatePath)) {
          throw new Error(
            "Not Trinity-compatible: missing template.yaml. " +
              "Create a template.yaml with at least 'name' and 'resources' fields."
          );
        }

        // 3. Read and validate template.yaml
        let templateContent: string;
        try {
          templateContent = fs.readFileSync(templatePath, "utf-8");
        } catch (e) {
          throw new Error(`Failed to read template.yaml: ${e}`);
        }

        // Basic YAML validation (check for required fields)
        if (!templateContent.includes("name:")) {
          throw new Error(
            "Not Trinity-compatible: template.yaml missing 'name' field"
          );
        }
        if (!templateContent.includes("resources:")) {
          throw new Error(
            "Not Trinity-compatible: template.yaml missing 'resources' field"
          );
        }

        // 4. Read credentials from .env if requested
        const credentials: Record<string, string> = {};
        if (includeEnv) {
          const envPath = path_module.join(agentPath, ".env");
          if (fs.existsSync(envPath)) {
            const envContent = fs.readFileSync(envPath, "utf-8");
            for (const line of envContent.split("\n")) {
              const trimmed = line.trim();
              if (!trimmed || trimmed.startsWith("#")) continue;
              const eqIndex = trimmed.indexOf("=");
              if (eqIndex > 0) {
                const key = trimmed.substring(0, eqIndex).trim();
                let value = trimmed.substring(eqIndex + 1).trim();
                // Remove surrounding quotes
                if (
                  (value.startsWith('"') && value.endsWith('"')) ||
                  (value.startsWith("'") && value.endsWith("'"))
                ) {
                  value = value.slice(1, -1);
                }
                // Only include valid env var names
                if (/^[A-Z][A-Z0-9_]*$/.test(key) && value) {
                  credentials[key] = value;
                }
              }
            }
            console.log(
              `[deploy_local_agent] Read ${Object.keys(credentials).length} credentials from .env`
            );
          }
        }

        // 5. Create tar.gz archive
        // Use shell command for simplicity (tar is available on all platforms)
        const archivePath = `/tmp/trinity-deploy-${Date.now()}.tar.gz`;
        const excludes = [
          ".git",
          "node_modules",
          "__pycache__",
          ".venv",
          "venv",
          ".env", // We extract credentials separately, don't include in archive
          "*.pyc",
          ".DS_Store",
        ];

        const excludeArgs = excludes.map((e) => `--exclude='${e}'`).join(" ");

        try {
          execSync(
            `tar -czf "${archivePath}" ${excludeArgs} -C "${path_module.dirname(agentPath)}" "${path_module.basename(agentPath)}"`,
            { stdio: "pipe" }
          );
        } catch (e) {
          throw new Error(`Failed to create archive: ${e}`);
        }

        // 6. Read and base64 encode
        const archiveBuffer = fs.readFileSync(archivePath);
        const archiveBase64 = archiveBuffer.toString("base64");

        // Check size limit (50MB)
        if (archiveBuffer.length > 50 * 1024 * 1024) {
          fs.unlinkSync(archivePath);
          throw new Error(
            `Archive too large: ${(archiveBuffer.length / (1024 * 1024)).toFixed(1)}MB exceeds 50MB limit`
          );
        }

        // Cleanup archive
        try {
          fs.unlinkSync(archivePath);
        } catch {
          // Ignore cleanup errors
        }

        console.log(
          `[deploy_local_agent] Created archive: ${(archiveBuffer.length / 1024).toFixed(1)}KB`
        );

        // 7. Call backend
        interface DeployLocalResponse {
          status: string;
          agent?: {
            name: string;
            status: string;
            port: number;
            template: string;
          };
          versioning?: {
            base_name: string;
            previous_version?: string;
            previous_version_stopped: boolean;
            new_version: string;
          };
          credentials_imported: Record<
            string,
            {
              status: string;
              name: string;
              original?: string;
            }
          >;
          credentials_injected: number;
          error?: string;
          code?: string;
        }

        const response = await apiClient.request<DeployLocalResponse>(
          "POST",
          "/api/agents/deploy-local",
          {
            archive: archiveBase64,
            credentials:
              Object.keys(credentials).length > 0 ? credentials : undefined,
            name: args.name,
          }
        );

        return JSON.stringify(response, null, 2);
      },
    },
  };
}
