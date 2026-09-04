/**
 * Agent Management Tools
 *
 * MCP tools for managing Trinity agents: list, get, create, delete, start, stop
 */

import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";
import { deriveMcpIdempotencyKey } from "./chat.js";

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
        "List all agents in the Trinity platform with their status and resource allocation. " +
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
        "Returns the agent's status, port assignments, resource limits, and container ID.",
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
    // get_agent_info - Get agent template metadata and capabilities
    // ========================================================================
    getAgentInfo: {
      name: "get_agent_info",
      description:
        "Get full template metadata and capabilities for an agent. " +
        "Returns detailed information from the agent's template.yaml including: " +
        "display name, description, version, author, capabilities, available commands, " +
        "MCP servers, tools, skills, and example use cases. " +
        "Useful for understanding what an agent can do before interacting with it. " +
        "Access control: agents can only get info about agents they have permission to call.",
      parameters: z.object({
        name: z.string().describe("The name of the agent to get information about"),
      }),
      execute: async ({ name }: { name: string }, context?: { session?: McpAuthContext }) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        // Access control for agent-scoped keys
        if (authContext?.scope === "agent" && authContext?.agentName) {
          const callerAgentName = authContext.agentName;

          // Agent can always get info about itself
          if (name !== callerAgentName) {
            // Check if caller has permission to access target agent
            const permittedAgents = await apiClient.getPermittedAgents(callerAgentName);

            if (!permittedAgents.includes(name)) {
              console.log(`[get_agent_info] Agent '${callerAgentName}' denied access to '${name}' (not permitted)`);
              return JSON.stringify({
                error: "Access denied",
                reason: `Agent '${callerAgentName}' does not have permission to access '${name}'`,
                hint: "Request permission from the agent owner or use the agent permissions API",
              }, null, 2);
            }
          }

          console.log(`[get_agent_info] Agent '${callerAgentName}' accessing info for '${name}'`);
        }

        // System agents and user-scoped keys can access all agents they have access to
        const info = await apiClient.getAgentInfo(name);
        return JSON.stringify(info, null, 2);
      },
    },

    // ========================================================================
    // get_agent_compatibility_report - Trinity-compatibility validation (#668)
    // ========================================================================
    getAgentCompatibilityReport: {
      name: "get_agent_compatibility_report",
      description:
        "Run Trinity-compatibility validation on an agent's workspace and return the report. " +
        "Checks ~100 best-practice rules across file structure, security/secret hygiene, " +
        "template.yaml, CLAUDE.md quality, credentials, git config, skills, autonomy, dashboard, " +
        "cross-file consistency, and composability. Each result is HARD (will likely break Trinity), " +
        "SOFT (best practice), or INFO, with PASS/FAIL/SKIPPED status. Deterministic STATIC checks " +
        "run live; AI-evaluated checks read file contents (returns the last result unless include_ai=true). " +
        "Non-blocking and advisory. Access control: agents can only report on agents they may call.",
      parameters: z.object({
        agent_name: z.string().describe("The name of the agent to validate"),
        include_ai: z
          .boolean()
          .optional()
          .describe("Force a fresh AI evaluation (default true); false returns the last persisted AI verdicts"),
      }),
      execute: async (
        { agent_name, include_ai }: { agent_name: string; include_ai?: boolean },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        // Access control for agent-scoped keys (mirror get_agent_info).
        if (authContext?.scope === "agent" && authContext?.agentName) {
          const callerAgentName = authContext.agentName;
          if (agent_name !== callerAgentName) {
            const permittedAgents = await apiClient.getPermittedAgents(callerAgentName);
            if (!permittedAgents.includes(agent_name)) {
              return JSON.stringify({
                success: false,
                error: "Access denied",
                reason: `Agent '${callerAgentName}' does not have permission to access '${agent_name}'`,
              }, null, 2);
            }
          }
        }

        try {
          const report = await apiClient.getAgentCompatibilityReport(
            agent_name,
            include_ai !== false
          );
          return JSON.stringify({ success: true, ...report }, null, 2);
        } catch (error) {
          const errorMessage = error instanceof Error ? error.message : String(error);
          console.error(`[get_agent_compatibility_report] Error: ${errorMessage}`);
          return JSON.stringify({ success: false, error: errorMessage }, null, 2);
        }
      },
    },

    // ========================================================================
    // create_agent - Create a new agent
    // ========================================================================
    createAgent: {
      name: "create_agent",
      description:
        "Create a new agent in the Trinity platform. " +
        "You can create agents from: (1) any GitHub repo using 'github:owner/repo' format (PAT must have access), " +
        "(2) pre-defined templates from list_templates, or (3) local templates. " +
        "The agent will be started automatically after creation.",
      parameters: z.object({
        name: z
          .string()
          .describe(
            "Unique name for the agent. Will be sanitized for Docker compatibility."
          ),
        template: z
          .string()
          .optional()
          .describe(
            "Template to use for agent configuration. Supports: " +
            "(1) Any id returned by list_templates (e.g., 'local:scout'), " +
            "(2) Any GitHub repo with 'github:owner/repo' format - requires system GITHUB_PAT to have access; " +
            "this works whether or not the repo appears in list_templates, whose GitHub half comes from " +
            "the remote template registry by default and from an admin-curated list once one is configured " +
            "in Settings (an admin list takes full precedence), " +
            "(3) Local templates with 'local:template-name'. " +
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
        source_branch: z
          .string()
          .optional()
          .describe(
            "Branch to track for this agent. Default: 'main'. " +
            "Can also be specified in template URL as 'github:owner/repo@branch'."
          ),
        import_intent: z
          .enum(["copy", "clone"])
          .optional()
          .describe(
            "How to import a 'github:owner/repo' template (trinity-enterprise#15). " +
            "'clone' (default when omitted): normal git-synced clone that keeps tracking " +
            "the source repo. 'copy': point-in-time snapshot of the repo contents — no " +
            "git sync and no upstream tie; the response includes an import_snapshot " +
            "with the source repo and head SHA. 'fork' (copy into a new repo you own) " +
            "is available only in the web UI."
          ),
        ephemeral: z
          .object({
            max_executions: z
              .number()
              .int()
              .min(1)
              .max(100)
              .optional()
              .describe("Discard after this many terminal executions (1-100)"),
            ttl_seconds: z
              .number()
              .int()
              .min(60)
              .optional()
              .describe(
                "Discard after this many seconds (60..platform ceiling, default ceiling 24h)"
              ),
          })
          .optional()
          .describe(
            "Create as an ephemeral 'ghost' agent: budgeted, volume-less, hard-discarded " +
            "when the budget is exhausted (no soft-delete/recovery). At least one of " +
            "max_executions/ttl_seconds is required; a TTL is always stamped so no ghost is " +
            "immortal. The server suffixes the name for uniqueness — use the returned name. " +
            "Ghost keys are restricted to heartbeat/reports/notifications/self-info. " +
            "Intended for heterogeneous per-repo jobs (a different repo/config per ghost); " +
            "for burst parallelism on ONE agent use fan_out instead. " +
            "Requires the ephemeral_agents entitlement (403 otherwise)."
          ),
      }),
      execute: async (
        args: {
          name: string;
          template?: string;
          resources?: { cpu?: string; memory?: string };
          tools?: string[];
          mcp_servers?: string[];
          custom_instructions?: string;
          source_branch?: string;
          import_intent?: "copy" | "clone";
          ephemeral?: { max_executions?: number; ttl_seconds?: number };
        },
        context: any
      ) => {
        const config = {
          name: args.name,
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
          source_branch: args.source_branch,
          import_intent: args.import_intent,
          ephemeral: args.ephemeral,
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

        // RELIABILITY-006 (#525): deterministic key so a transport retry of
        // this exact create replays the original response instead of
        // dispatching a second create. Includes the agent NAME so two
        // different agents imported from the same repo never share a key.
        const idempotencyKey = deriveMcpIdempotencyKey([
          authContext?.userId,
          "create_agent",
          args.name,
          JSON.stringify(config),
        ]);

        const agent = await apiClient.createAgent(config, idempotencyKey);
        console.log("[CREATE_AGENT] Agent created successfully:", agent.name);

        let text = JSON.stringify(agent, null, 2);
        // trinity-enterprise#15: surface copy-snapshot provenance when present.
        if (agent.import_snapshot?.source_repo) {
          const sha = agent.import_snapshot.head_sha
            ? ` @ ${agent.import_snapshot.head_sha.slice(0, 7)}`
            : "";
          text += `\nImported a point-in-time copy of ${agent.import_snapshot.source_repo}${sha} (no git sync / upstream tie).`;
        }
        text +=
          "\nNext: run get_agent_compatibility_report to validate the imported workspace " +
          "(it runs against the RUNNING agent).";
        return text;
      },
    },

    // ========================================================================
    // rename_agent - Rename an agent (RENAME-001)
    // ========================================================================
    renameAgent: {
      name: "rename_agent",
      description:
        "Rename an agent in the Trinity platform. " +
        "Changes the agent name across all references (database, container, UI). " +
        "System agents cannot be renamed. Only owners or admins can rename agents. " +
        "The agent will be briefly stopped during the rename operation.",
      parameters: z.object({
        name: z.string().describe("The current name of the agent to rename"),
        new_name: z.string().describe("The new name for the agent (will be sanitized for Docker compatibility)"),
      }),
      execute: async (
        { name, new_name }: { name: string; new_name: string },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;

        // Prevent system agent from renaming itself
        if (authContext?.scope === "system" && authContext?.agentName === name) {
          console.log(`[rename_agent] System agent cannot rename itself`);
          return JSON.stringify({
            error: "Cannot rename system agent",
            reason: "System agents cannot be renamed.",
            agent: name
          }, null, 2);
        }

        console.log(`[rename_agent] Renaming agent '${name}' to '${new_name}'`);

        const apiClient = getClient(authContext);
        const result = await apiClient.renameAgent(name, new_name);
        return JSON.stringify(result, null, 2);
      },
    },

    // ========================================================================
    // delete_agent - Remove an agent
    // ========================================================================
    deleteAgent: {
      name: "delete_agent",
      description:
        "Delete an agent from the Trinity platform (owner or admin). " +
        "Durable agents are SOFT-deleted: the container is removed but data is " +
        "recoverable by an admin until the retention window (default 180d) expires. " +
        "Ephemeral 'ghost' agents are HARD-discarded immediately: container, storage, " +
        "and DB rows are purged with no recovery — this is also how a parent agent " +
        "discards a ghost it spawned before its budget expires. Agent-scoped keys may " +
        "only delete agents they spawned.",
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
    // get_credential_status - Get credential status from a running agent
    // ========================================================================
    getCredentialStatus: {
      name: "get_credential_status",
      description:
        "Get credential file status from a running agent. " +
        "Returns information about credential files (.env, .mcp.json, .credentials.enc) " +
        "inside the agent container, including whether they exist and when last modified. " +
        "Part of the simplified credential system (CRED-002).",
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
    // inject_credentials - Inject credential files directly into an agent
    // ========================================================================
    injectCredentials: {
      name: "inject_credentials",
      description:
        "Inject credential files directly into a running agent's workspace. " +
        "This is the new simplified credential system that writes files directly " +
        "without Redis or template processing. Supports a curated set of credential " +
        "file types: .env, .mcp.json, cloud service-account JSON (.config/gcloud/**), " +
        "kubeconfig (.kube/config), TLS/cert material (*.pem/*.key/*.crt/*.p12/*.pfx), " +
        "and SSH keys (.ssh/id_*). Use files_b64 for binary material. The agent must be running.",
      parameters: z.object({
        name: z.string().describe("The name of the agent to inject credentials into"),
        files: z.record(z.string(), z.string()).optional().describe(
          'Map of file paths to TEXT contents. Example: {".env": "KEY=value", ".config/gcloud/sa.json": "{...}"}'
        ),
        files_b64: z.record(z.string(), z.string()).optional().describe(
          'Map of file paths to BASE64-encoded binary contents (e.g. .p12/.pfx/DER cert material).'
        ),
      }),
      execute: async (
        { name, files, files_b64 }: { name: string; files?: Record<string, string>; files_b64?: Record<string, string> },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        const textFiles = files || {};
        const binFiles = files_b64 || {};
        console.log(`[inject_credentials] Injecting ${Object.keys(textFiles).length + Object.keys(binFiles).length} file(s) into agent '${name}'`);

        const result = await apiClient.injectCredentials(name, textFiles, binFiles);
        return JSON.stringify(result, null, 2);
      },
    },

    // ========================================================================
    // export_credentials - Export credentials to encrypted file in git
    // ========================================================================
    exportCredentials: {
      name: "export_credentials",
      description:
        "Export credential files from an agent to an encrypted .credentials.enc file. " +
        "Reads .env and .mcp.json from the agent, encrypts them, and writes .credentials.enc " +
        "to the agent's workspace. This file can be committed to git for portable credential storage. " +
        "Requires CREDENTIAL_ENCRYPTION_KEY to be configured on the backend. " +
        "The agent must be running.",
      parameters: z.object({
        name: z.string().describe("The name of the agent to export credentials from"),
      }),
      execute: async ({ name }: { name: string }, context?: { session?: McpAuthContext }) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        console.log(`[export_credentials] Exporting credentials from agent '${name}'`);

        const result = await apiClient.exportCredentials(name);
        return JSON.stringify(result, null, 2);
      },
    },

    // ========================================================================
    // import_credentials - Import credentials from encrypted file
    // ========================================================================
    importCredentials: {
      name: "import_credentials",
      description:
        "Import credentials from an encrypted .credentials.enc file into an agent. " +
        "Reads .credentials.enc from the agent's workspace, decrypts it, and writes " +
        "the credential files (.env, .mcp.json, etc.) to the workspace. " +
        "This is useful after cloning an agent's git repo. " +
        "Requires CREDENTIAL_ENCRYPTION_KEY to be configured on the backend. " +
        "The agent must be running.",
      parameters: z.object({
        name: z.string().describe("The name of the agent to import credentials into"),
      }),
      execute: async ({ name }: { name: string }, context?: { session?: McpAuthContext }) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        console.log(`[import_credentials] Importing credentials into agent '${name}'`);

        const result = await apiClient.importCredentials(name);
        return JSON.stringify(result, null, 2);
      },
    },

    // ========================================================================
    // export_agent_data - Export an agent's runtime data (#1169)
    // ========================================================================
    exportAgentData: {
      name: "export_agent_data",
      description:
        "Export an agent's runtime data (the /home/developer/data directory: SQLite DBs, " +
        "datasets) as a base64-encoded tar with an embedded manifest. Together with the " +
        "template URL and .credentials.enc, this is the portable artifact for moving an " +
        "agent to another Trinity instance (pair with import_agent_data). " +
        "INLINE LIMIT: only small datasets fit this MCP response (a few MB); larger data " +
        "must use the streaming download endpoint POST /api/agents/{name}/data/export. " +
        "Owner/admin only; the agent must be running.",
      parameters: z.object({
        name: z.string().describe("The name of the agent to export data from"),
      }),
      execute: async ({ name }: { name: string }, context?: { session?: McpAuthContext }) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        console.log(`[export_agent_data] Exporting data from agent '${name}'`);

        const result = await apiClient.exportAgentData(name);
        return JSON.stringify(result, null, 2);
      },
    },

    // ========================================================================
    // import_agent_data - Restore runtime data into an agent (#1169)
    // ========================================================================
    importAgentData: {
      name: "import_agent_data",
      description:
        "Restore runtime data into an agent's /home/developer/data directory from a " +
        "base64-encoded tar (typically produced by export_agent_data). The backend enforces " +
        "the data/** allowlist and rejects path traversal, so only data/ entries are written. " +
        "Owner/admin only; the agent must be running.",
      parameters: z.object({
        name: z.string().describe("The name of the agent to restore data into"),
        tar_base64: z
          .string()
          .describe("base64-encoded tar of the data directory, from export_agent_data"),
      }),
      execute: async (
        { name, tar_base64 }: { name: string; tar_base64: string },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        console.log(`[import_agent_data] Restoring data into agent '${name}'`);

        const result = await apiClient.importAgentData(name, tar_base64);
        return JSON.stringify(result, null, 2);
      },
    },

    // ========================================================================
    // get_credential_encryption_key - Get encryption key for local agents
    // ========================================================================
    getCredentialEncryptionKey: {
      name: "get_credential_encryption_key",
      description:
        "Get the platform's credential encryption key for local agents. " +
        "This allows local agents to encrypt/decrypt .credentials.enc files themselves, " +
        "enabling portable agents that work both locally and on Trinity. " +
        "Returns the key as a hex string (64 chars for AES-256). " +
        "SECURITY: Store securely and never commit to git.",
      parameters: z.object({}),
      execute: async (_params: unknown, context?: { session?: McpAuthContext }) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);
        const result = await apiClient.getEncryptionKey();
        return JSON.stringify(result, null, 2);
      },
    },

    // ========================================================================
    // get_agent_ssh_access - Generate ephemeral SSH credentials for agent
    // ========================================================================
    getAgentSshAccess: {
      name: "get_agent_ssh_access",
      description:
        "Generate ephemeral, key-based SSH credentials for direct terminal access to an agent container. " +
        "Generate a keypair locally (ssh-keygen -t ed25519) and provide the PUBLIC key; the server injects " +
        "it into the container and it expires automatically (default: 4 hours). Agent must be running. " +
        "The server never generates or handles private keys. Admin only. " +
        "(Password auth was removed — it never worked; key auth is the only method.)",
      parameters: z.object({
        agent_name: z.string().describe("Name of the agent to access"),
        ttl_hours: z
          .number()
          .optional()
          .default(4)
          .describe("How long the SSH key should be valid (0.1-24 hours, default: 4)"),
        public_key: z
          .string()
          .describe("Your SSH public key (required). Generate with: ssh-keygen -t ed25519. Provide the contents of ~/.ssh/id_ed25519.pub"),
      }),
      execute: async (
        { agent_name, ttl_hours = 4, public_key }: { agent_name: string; ttl_hours?: number; public_key?: string },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        console.log(`[get_agent_ssh_access] Generating key-based SSH access for agent '${agent_name}' (TTL: ${ttl_hours}h)`);

        const response = await apiClient.createSshAccess(agent_name, ttl_hours, public_key);

        return JSON.stringify(response, null, 2);
      },
    },

    // ========================================================================
    // deploy_local_agent - Deploy a Trinity-compatible local agent
    // ========================================================================
    deployLocalAgent: {
      name: "deploy_local_agent",
      description:
        "Deploy a Trinity-compatible local agent to the remote Trinity platform. " +
        "IMPORTANT: the calling agent must package the directory locally AND embed an integrity manifest (#2060) before calling this tool. " +
        "Steps for the calling agent: " +
        "1. Write the manifest INTO the agent directory (it ships inside the archive) — run from the agent directory: " +
        "`python3 -c \"import hashlib,json,os; ex={'.git','node_modules','__pycache__','.venv'}; es=[]\n" +
        "for dp,dn,fn in os.walk('.'):\n" +
        " dn[:]=[d for d in dn if d not in ex]\n" +
        " for d in list(dn):\n" +
        "  p=os.path.join(dp,d)\n" +
        "  if os.path.islink(p): es.append({'path':os.path.relpath(p,'.'),'link_target':os.readlink(p)}); dn.remove(d)\n" +
        " for f in fn:\n" +
        "  p=os.path.join(dp,f); r=os.path.relpath(p,'.')\n" +
        "  if r=='.trinity-manifest.json' or f.startswith('._'): continue\n" +
        "  if os.path.islink(p): es.append({'path':r,'link_target':os.readlink(p)})\n" +
        "  elif os.path.isfile(p): es.append({'path':r,'sha256':hashlib.sha256(open(p,'rb').read()).hexdigest()})\n" +
        "json.dump(es,open('.trinity-manifest.json','w'))\"` " +
        "(files carry sha256, symlinks carry link_target, dirs omitted; the manifest never lists itself). " +
        "2. Create the archive with the SAME excludes: `COPYFILE_DISABLE=1 tar -czf agent.tar.gz --exclude='.git' --exclude='node_modules' --exclude='__pycache__' --exclude='.venv' -C /path/to agent-dir` " +
        "(COPYFILE_DISABLE=1 prevents macOS '._*' AppleDouble pollution; such members are skipped server-side with a warning). " +
        "3. Base64 encode: `base64 -i agent.tar.gz` (macOS) or `base64 agent.tar.gz` (Linux) " +
        "4. Call this tool with the base64 archive. " +
        "The backend verifies the extracted tree against the embedded manifest and REFUSES drift (400 MANIFEST_DRIFT naming missing/altered/extra paths) — a pruned or mangled archive can never deploy silently; a missing manifest is refused (MANIFEST_REQUIRED). " +
        "In-root symlinks are preserved end to end; symlinks escaping the archive root are refused. " +
        "Caps: 50 MB compressed, 500 MB extracted, 10000 files. " +
        "HONEST CEILING: the archive rides this tool call's arguments, which are token-bound (~100-200 KB of base64 per turn in practice). Deploy LARGER agents from bash instead — `pip install trinity-cli && trinity deploy .` or curl POST /api/agents/deploy-local — same endpoint, same manifest verification, no token ceiling. " +
        "The archive must contain a template.yaml with 'name' and 'resources' fields. " +
        "Include .env and other credential files in the archive — they are deployed as-is. " +
        "If agent name exists, creates new version (my-agent-2) and stops old one. " +
        "Transport retries of the identical call are idempotent (an Idempotency-Key is derived from the arguments); re-running the packaging pipeline produces new bytes and deliberately deploys a new version.",
      parameters: z.object({
        archive: z
          .string()
          .describe(
            "Base64-encoded tar.gz archive of the agent directory, with .trinity-manifest.json embedded (see tool description). " +
            "The archive should contain the agent files at the root level (template.yaml, CLAUDE.md, .env, etc.). " +
            "Exclude .git, node_modules, __pycache__, and .venv from the archive."
          ),
        name: z
          .string()
          .optional()
          .describe("Agent name override (defaults to name from template.yaml)"),
      }),
      execute: async (
        args: { archive: string; name?: string },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        // Validate archive is provided
        if (!args.archive || args.archive.trim() === "") {
          throw new Error(
            "Archive is required. The calling agent must package the local directory as a base64-encoded tar.gz."
          );
        }

        // Basic validation - check it looks like base64
        if (!/^[A-Za-z0-9+/=]+$/.test(args.archive.replace(/\s/g, ""))) {
          throw new Error(
            "Invalid archive format. Must be a base64-encoded string."
          );
        }

        // Log for debugging
        const archiveSize = Math.round((args.archive.length * 3) / 4 / 1024);
        console.log(
          `[deploy_local_agent] Deploying archive: ~${archiveSize}KB`
        );

        // RELIABILITY-006 (#525) / #2060: deterministic key so a transport
        // retry of this exact call replays instead of minting ANOTHER version
        // (versioning makes an un-keyed retry fork twice). Same-args-only by
        // design: a re-run packaging pipeline produces new gzip bytes ⇒ a new
        // key ⇒ a visible new version — keying on content would false-replay
        // an intentional identical-content redeploy.
        const idempotencyKey = deriveMcpIdempotencyKey([
          authContext?.userId,
          "deploy_local_agent",
          args.name,
          args.archive,
        ]);

        // Call backend
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
          warnings?: string[];
          error?: string;
          code?: string;
          // #2060 evidence fields
          verified?: boolean;
          files_expected?: number;
          files_deployed?: number;
          symlinks_deployed?: number;
          compatibility_hard_count?: number;
        }

        const response = await apiClient.request<DeployLocalResponse>(
          "POST",
          "/api/agents/deploy-local",
          {
            archive: args.archive,
            name: args.name,
            // #2060: set in tool CODE, not a model-controlled parameter — the
            // MCP surface gets the integrity contract unconditionally.
            require_manifest: true,
          },
          false,
          undefined,
          { "Idempotency-Key": idempotencyKey }
        );

        return JSON.stringify(response, null, 2);
      },
    },

    // ========================================================================
    // initialize_github_sync - Initialize GitHub synchronization for an agent
    // ========================================================================
    initializeGithubSync: {
      name: "initialize_github_sync",
      description:
        "Initialize GitHub synchronization for an existing agent (not created from GitHub template). " +
        "Creates a GitHub repository (if requested), initializes git in the agent workspace, " +
        "commits the current state, pushes to GitHub, and creates a working branch for sync. " +
        "Requires GitHub Personal Access Token (PAT) to be configured in system settings with 'repo' scope. " +
        "Note: Agents created from GitHub templates already have git sync enabled in source mode (pull-only). " +
        "Agent must be running.",
      parameters: z.object({
        agent_name: z
          .string()
          .describe("The name of the agent to initialize GitHub sync for"),
        repo_owner: z
          .string()
          .describe("GitHub username or organization name (e.g., 'your-username')"),
        repo_name: z
          .string()
          .describe("Repository name (e.g., 'my-agent')"),
        create_repo: z
          .boolean()
          .optional()
          .default(true)
          .describe("Whether to create the repository if it doesn't exist (default: true)"),
        private: z
          .boolean()
          .optional()
          .default(true)
          .describe("Whether the new repository should be private (default: true)"),
        description: z
          .string()
          .optional()
          .describe("Repository description (optional)"),
      }),
      execute: async (
        args: {
          agent_name: string;
          repo_owner: string;
          repo_name: string;
          create_repo?: boolean;
          private?: boolean;
          description?: string;
        },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        console.log(
          `[initialize_github_sync] Initializing GitHub sync for agent '${args.agent_name}' -> ${args.repo_owner}/${args.repo_name}`
        );

        interface GitInitializeResponse {
          success: boolean;
          message: string;
          github_repo: string;
          working_branch: string;
          instance_id: string;
          repo_url: string;
        }

        const response = await apiClient.request<GitInitializeResponse>(
          "POST",
          `/api/agents/${args.agent_name}/git/initialize`,
          {
            repo_owner: args.repo_owner,
            repo_name: args.repo_name,
            create_repo: args.create_repo ?? true,
            private: args.private ?? true,
            description: args.description,
          }
        );

        return JSON.stringify(response, null, 2);
      },
    },

    // ========================================================================
    // get_agent_github_pat_status - Get GitHub PAT configuration status (#347)
    // ========================================================================
    getAgentGithubPatStatus: {
      name: "get_agent_github_pat_status",
      description:
        "Get the GitHub PAT configuration status for an agent. " +
        "Returns whether the agent has a custom GitHub PAT configured or uses the global PAT. " +
        "Does not return the actual PAT value for security.",
      parameters: z.object({
        agent_name: z
          .string()
          .describe("The name of the agent to check"),
      }),
      execute: async (
        args: { agent_name: string },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        console.log(
          `[get_agent_github_pat_status] Checking PAT status for agent '${args.agent_name}'`
        );

        interface GitHubPATStatusResponse {
          agent_name: string;
          configured: boolean;
          source: "agent" | "global";
          has_global: boolean;
        }

        const response = await apiClient.request<GitHubPATStatusResponse>(
          "GET",
          `/api/agents/${args.agent_name}/github-pat`
        );

        return JSON.stringify(response, null, 2);
      },
    },

    // ========================================================================
    // set_agent_github_pat - Set per-agent GitHub PAT (#347)
    // ========================================================================
    setAgentGithubPat: {
      name: "set_agent_github_pat",
      description:
        "Set a per-agent GitHub Personal Access Token. " +
        "The PAT is validated against GitHub API before saving and encrypted at rest. " +
        "When set, this agent uses this PAT (instead of the global PAT) for git AND " +
        "for the `gh` CLI / GitHub REST API — the container exposes it as GITHUB_PAT " +
        "plus GH_TOKEN/GITHUB_TOKEN, so `gh` and `gh api` auto-authenticate (#1574). " +
        "The token must carry the scopes the operation needs (e.g. `repo`, or Issues: " +
        "Read and write) — wiring makes it available but cannot grant missing scopes. " +
        "To clear the PAT and revert to global, pass an empty string. " +
        "Note: Agent must be restarted for the new PAT to take effect.",
      parameters: z.object({
        agent_name: z
          .string()
          .describe("The name of the agent"),
        pat: z
          .string()
          .describe("GitHub Personal Access Token (or empty string to clear)"),
      }),
      execute: async (
        args: { agent_name: string; pat: string },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        // Handle clear case
        if (!args.pat || args.pat.trim() === "") {
          console.log(
            `[set_agent_github_pat] Clearing PAT for agent '${args.agent_name}'`
          );

          const response = await apiClient.request<{ message: string }>(
            "DELETE",
            `/api/agents/${args.agent_name}/github-pat`
          );

          return JSON.stringify(response, null, 2);
        }

        console.log(
          `[set_agent_github_pat] Setting PAT for agent '${args.agent_name}'`
        );

        interface SetGitHubPATResponse {
          message: string;
          agent_name: string;
          github_username: string;
          source: string;
          note: string;
        }

        const response = await apiClient.request<SetGitHubPATResponse>(
          "PUT",
          `/api/agents/${args.agent_name}/github-pat`,
          { pat: args.pat }
        );

        return JSON.stringify(response, null, 2);
      },
    },
  };
}
