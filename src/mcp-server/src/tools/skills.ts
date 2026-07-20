/**
 * Skills Management Tools
 *
 * MCP tools for managing Trinity agent skills:
 * - list_skills: List available skills from the library
 * - get_skill: Get skill details and content
 * - assign_skill_to_agent: Assign a skill to an agent
 * - sync_agent_skills: Inject assigned skills to a running agent
 */

import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

/**
 * Skill information from the library.
 * ent#183: carries the frontmatter contract + package metadata.
 */
interface SkillInfo {
  name: string;
  description: string | null;
  path: string;
  content?: string;
  automation?: string | null;
  user_invocable?: boolean;
  allowed_tools?: unknown;
  requires?: { packages: string[]; binaries: string[]; env: string[] };
  multi_file?: boolean;
  file_count?: number;
  size_bytes?: number;
  version?: string | null;
}

/**
 * Skills library status
 */
interface SkillsLibraryStatus {
  configured: boolean;
  url: string | null;
  branch: string;
  cloned: boolean;
  last_sync: string | null;
  commit_sha: string | null;
  skill_count: number;
}

/**
 * Skill injection result (ent#183: per-skill status + warnings are the
 * honest-result contract — surface them even on success)
 */
interface SkillInjectionResult {
  success: boolean;
  skills_injected: number;
  skills_unchanged?: number;
  skills_failed: number;
  results: Record<string, {
    success: boolean;
    status?: string;
    files_written?: number;
    error?: string;
    warnings?: string[];
  }>;
}

/**
 * Create skills management tools with the given client
 */
export function createSkillsTools(
  client: TrinityClient,
  requireApiKey: boolean
) {
  /**
   * Get Trinity client with appropriate authentication
   */
  const getClient = (authContext?: McpAuthContext): TrinityClient => {
    if (requireApiKey) {
      if (!authContext?.mcpApiKey) {
        throw new Error("MCP API key authentication required but no API key found in request context");
      }
      const userClient = new TrinityClient(client.getBaseUrl());
      userClient.setToken(authContext.mcpApiKey);
      return userClient;
    }
    return client;
  };

  return {
    // ========================================================================
    // list_skills - List available skills from library
    // ========================================================================
    listSkills: {
      name: "list_skills",
      description:
        "List all available skills from the skills library. " +
        "Returns skill names, descriptions, and paths. " +
        "Skills are loaded from the configured GitHub repository. " +
        "Use get_skill to get the full content of a specific skill.",
      parameters: z.object({}),
      execute: async (_params: unknown, context?: { session?: McpAuthContext }) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        const skills = await apiClient.request<SkillInfo[]>(
          "GET",
          "/api/skills/library"
        );

        if (skills.length === 0) {
          return JSON.stringify({
            message: "No skills available. The skills library may not be configured or synced.",
            hint: "Configure skills_library_url in Settings and sync the library.",
            skills: []
          }, null, 2);
        }

        return JSON.stringify({
          count: skills.length,
          skills: skills.map(s => ({
            name: s.name,
            description: s.description || "No description",
            path: s.path,
            automation: s.automation ?? null,
            user_invocable: s.user_invocable ?? true,
            requires: s.requires ?? { packages: [], binaries: [], env: [] },
            allowed_tools: s.allowed_tools ?? null,
            multi_file: s.multi_file ?? false,
            file_count: s.file_count ?? 0,
            size_bytes: s.size_bytes ?? 0,
            version: s.version ?? null
          }))
        }, null, 2);
      },
    },

    // ========================================================================
    // get_skill - Get skill details and content
    // ========================================================================
    getSkill: {
      name: "get_skill",
      description:
        "Get full details for a specific skill from the library, including its content. " +
        "Use this to see what a skill teaches an agent to do before assigning it.",
      parameters: z.object({
        skill_name: z.string().describe("Name of the skill to retrieve"),
      }),
      execute: async (
        { skill_name }: { skill_name: string },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        const skill = await apiClient.request<SkillInfo>(
          "GET",
          `/api/skills/library/${encodeURIComponent(skill_name)}`
        );

        return JSON.stringify(skill, null, 2);
      },
    },

    // ========================================================================
    // get_skills_library_status - Get library sync status
    // ========================================================================
    getSkillsLibraryStatus: {
      name: "get_skills_library_status",
      description:
        "Get the current status of the skills library. " +
        "Shows whether it's configured, synced, and how many skills are available.",
      parameters: z.object({}),
      execute: async (_params: unknown, context?: { session?: McpAuthContext }) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        const status = await apiClient.request<SkillsLibraryStatus>(
          "GET",
          "/api/skills/library/status"
        );

        return JSON.stringify(status, null, 2);
      },
    },

    // ========================================================================
    // assign_skill_to_agent - Assign a skill to an agent
    // ========================================================================
    assignSkillToAgent: {
      name: "assign_skill_to_agent",
      description:
        "Assign a skill to an agent. " +
        "The skill will be injected when the agent starts, or you can use sync_agent_skills to inject immediately. " +
        "Skills teach agents specific behaviors defined in SKILL.md files.",
      parameters: z.object({
        agent_name: z.string().describe("Name of the agent to assign the skill to"),
        skill_name: z.string().describe("Name of the skill to assign"),
      }),
      execute: async (
        { agent_name, skill_name }: { agent_name: string; skill_name: string },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        const result = await apiClient.request<{
          success: boolean;
          message: string;
          skill_name: string;
        }>(
          "POST",
          `/api/agents/${encodeURIComponent(agent_name)}/skills/${encodeURIComponent(skill_name)}`
        );

        return JSON.stringify(result, null, 2);
      },
    },

    // ========================================================================
    // set_agent_skills - Bulk update agent skills
    // ========================================================================
    setAgentSkills: {
      name: "set_agent_skills",
      description:
        "Set all skills for an agent (replaces existing assignments). " +
        "Use this to configure multiple skills at once.",
      parameters: z.object({
        agent_name: z.string().describe("Name of the agent"),
        skills: z.array(z.string()).describe("List of skill names to assign"),
      }),
      execute: async (
        { agent_name, skills }: { agent_name: string; skills: string[] },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        const result = await apiClient.request<{
          success: boolean;
          agent_name: string;
          skills_assigned: number;
          skills: string[];
        }>(
          "PUT",
          `/api/agents/${encodeURIComponent(agent_name)}/skills`,
          { skills }
        );

        return JSON.stringify(result, null, 2);
      },
    },

    // ========================================================================
    // sync_agent_skills - Inject skills to running agent
    // ========================================================================
    syncAgentSkills: {
      name: "sync_agent_skills",
      description:
        "Inject all assigned skills into a running agent as full directory " +
        "packages (SKILL.md + scripts/ + resources) under .claude/skills/. " +
        "Unconditional repair: re-injects even unchanged skills. Agent must be " +
        "running. Per-skill warnings (missing deps, skipped files) are " +
        "reported even on success.",
      parameters: z.object({
        agent_name: z.string().describe("Name of the agent to sync skills to"),
      }),
      execute: async (
        { agent_name }: { agent_name: string },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        const result = await apiClient.request<SkillInjectionResult>(
          "POST",
          `/api/agents/${encodeURIComponent(agent_name)}/skills/inject`
        );

        // ent#183 honest results: warnings (missing deps, skipped files) must
        // surface even when every skill succeeded — never collapse to a bare
        // success message.
        const warnings: Record<string, string[]> = {};
        for (const [name, r] of Object.entries(result.results || {})) {
          if (r.warnings && r.warnings.length > 0) warnings[name] = r.warnings;
        }
        if (result.success) {
          return JSON.stringify({
            success: true,
            message: `Injected ${result.skills_injected} skills to agent ${agent_name}` +
              (result.skills_unchanged ? ` (${result.skills_unchanged} already up to date)` : ""),
            skills_injected: result.skills_injected,
            skills_unchanged: result.skills_unchanged ?? 0,
            ...(Object.keys(warnings).length > 0 ? { warnings } : {})
          }, null, 2);
        } else {
          return JSON.stringify({
            success: false,
            message: `Injected ${result.skills_injected} skills, ${result.skills_failed} failed`,
            skills_injected: result.skills_injected,
            skills_unchanged: result.skills_unchanged ?? 0,
            skills_failed: result.skills_failed,
            results: result.results
          }, null, 2);
        }
      },
    },

    // ========================================================================
    // get_agent_skills - Get skills assigned to an agent
    // ========================================================================
    getAgentSkills: {
      name: "get_agent_skills",
      description:
        "Get the list of skills assigned to an agent.",
      parameters: z.object({
        agent_name: z.string().describe("Name of the agent"),
      }),
      execute: async (
        { agent_name }: { agent_name: string },
        context?: { session?: McpAuthContext }
      ) => {
        const authContext = context?.session;
        const apiClient = getClient(authContext);

        const skills = await apiClient.request<Array<{
          id: number;
          agent_name: string;
          skill_name: string;
          assigned_by: string;
          assigned_at: string;
        }>>(
          "GET",
          `/api/agents/${encodeURIComponent(agent_name)}/skills`
        );

        return JSON.stringify({
          agent_name,
          skill_count: skills.length,
          skills: skills.map(s => ({
            name: s.skill_name,
            assigned_by: s.assigned_by,
            assigned_at: s.assigned_at
          }))
        }, null, 2);
      },
    },
  };
}
