/**
 * Documentation Tools
 *
 * MCP tools for accessing Trinity platform documentation
 */

import { z } from "zod";
import * as fs from "fs";
import * as path from "path";
import { fileURLToPath } from "url";

// Get the directory of this module
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

/**
 * Try to read the Trinity Compatible Agent Guide from various possible locations
 */
function readAgentGuide(): string {
  // Possible paths where the guide might be located
  const possiblePaths = [
    // Relative to src/mcp-server/src/tools/ -> docs/
    path.resolve(__dirname, "../../../../docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md"),
    // Relative to src/mcp-server/dist/tools/ -> docs/ (compiled)
    path.resolve(__dirname, "../../../../docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md"),
    // From project root (if cwd is project root)
    path.resolve(process.cwd(), "docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md"),
    // Inside container mount point
    "/app/docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md",
  ];

  for (const filePath of possiblePaths) {
    try {
      if (fs.existsSync(filePath)) {
        console.log(`[get_agent_requirements] Reading from: ${filePath}`);
        return fs.readFileSync(filePath, "utf-8");
      }
    } catch (e) {
      // Continue to next path
    }
  }

  // Fallback message if file not found
  return `# Trinity Compatible Agent Guide

Unable to load the full documentation file. Please refer to the documentation at:
https://github.com/abilityai/trinity/blob/main/docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md

## Quick Reference

### Required Files
- \`template.yaml\` - Agent metadata (name, display_name, description, resources)
- \`CLAUDE.md\` - Agent instructions (domain-specific only, planning is injected)
- \`.mcp.json.template\` - MCP server config with \${VAR} placeholders
- \`.env.example\` - Documents required credentials
- \`.gitignore\` - Must exclude secrets and platform directories

### template.yaml Minimum
\`\`\`yaml
name: my-agent
display_name: "My Agent"
description: "What this agent does"
resources:
  cpu: "2"
  memory: "4g"
\`\`\`

### Directory Structure
\`\`\`
my-agent/
├── template.yaml          # Required: Trinity metadata
├── CLAUDE.md              # Required: Agent instructions
├── .mcp.json.template     # Required if using MCP servers
├── .env.example           # Recommended: credential documentation
├── .gitignore             # Required: exclude secrets
├── memory/                # Agent's persistent state
└── outputs/               # Generated content
\`\`\`

### Platform-Injected (Do NOT include in your repo)
- \`.trinity/\` directory
- \`.claude/commands/trinity/\` commands
- Planning instructions in CLAUDE.md

For complete documentation, see the GitHub repository.
`;
}

/**
 * Create documentation tools
 */
export function createDocsTools() {
  return {
    // ========================================================================
    // get_agent_requirements - Get Trinity-compatible agent requirements
    // ========================================================================
    getAgentRequirements: {
      name: "get_agent_requirements",
      description:
        "Get the complete Trinity Compatible Agent Guide. " +
        "Returns the full documentation for creating Trinity-compatible agents, " +
        "including required files, directory structure, template.yaml schema, " +
        "credential management, platform injection, and best practices.",
      parameters: z.object({}),
      execute: async () => {
        const content = readAgentGuide();
        return content;
      },
    },
  };
}
