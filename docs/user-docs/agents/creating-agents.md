# Creating Agents

Agents are created from templates or from scratch. Each agent runs as an isolated Docker container with its own filesystem, credentials, and MCP server configuration.

> 📺 **Watch:** [Build an AI Recruiter Agent](https://youtu.be/K7hFWyFIf-Y) *(Jun 2026)* · [Build and Deploy Agents in Cursor](https://youtu.be/amqiysdlEWY) *(Apr 2026)* · [From Zero to Deployed](https://youtu.be/-TSZyekDS6o) *(Apr 2026)* · [all videos](../videos.md)

## Concepts

**Template sources** define where agent blueprints come from:

- **GitHub Template** -- A repository in `github:Org/repo` format. Supports branch selection with `github:Org/repo@branch`. Private repos require a GitHub PAT.
- **Admin-Configured Templates** -- GitHub repos configured by an admin in Settings. Metadata (name, description, resources, MCP servers) is fetched from each repo's `template.yaml` via the GitHub API and cached for 10 minutes. These appear as cards on the Templates page (`/templates`).
- **Local Templates** -- Auto-discovered from the `config/agent-templates/` directory.
- **From Scratch** -- Creates a minimal agent with a default `CLAUDE.md`.

**Template structure** follows a standard layout:

| File | Purpose |
|------|---------|
| `template.yaml` | Agent metadata: `display_name`, `description`, `resources`, `credentials`, `runtime` |
| `CLAUDE.md` | Agent instructions and system prompt |
| `.mcp.json.template` | MCP config template with `${VAR}` placeholders for credential injection |
| `.env.example` | Example credentials file listing required environment variables |

**Runtime options** control which CLI the agent uses. An agent's runtime — Claude Code, OpenAI Codex, or Gemini CLI — is chosen via `runtime.type` in `template.yaml` (see [Agent Runtimes](agent-runtimes.md)):

- `claude-code` (default)
- `codex`
- `gemini-cli`

## How It Works

When you create an agent, Trinity performs these steps in order:

1. Template is cloned (GitHub) or copied (local/from-scratch).
2. `base_image` is validated against the allowlist. By default only `trinity-agent-base:*` is permitted. Admins can configure additional allowed images.
3. A Docker container is built from the base image.
4. Template files are copied into `/home/developer/` inside the container.
5. Credential requirements are extracted from `.mcp.json.template`.
6. If API subscriptions exist, one is auto-assigned via round-robin (fewest agents first).
7. The agent starts automatically.
8. The container is labeled for fleet management: `trinity.platform=agent`, `trinity.agent-name`, `trinity.agent-type`, `trinity.template`.

### Compatibility Validation

Once an agent is running, Trinity validates its workspace against best-practice conventions and surfaces the results in the **Overview** tab on the Agent Detail page. This is advisory — it never blocks creation or deployment.

The check covers things like a present and valid `template.yaml`, a non-gitignored `.claude/` directory, defined playbooks, and accidentally committed secrets, grouped into findings ranked HARD / SOFT / INFO. Claude-specific checks (such as `CLAUDE.md` and `.claude/` skills) are skipped for Codex and Gemini agents.

The 10 gitignore-related findings offer a one-click **Fix** button: Trinity rewrites the agent's `.gitignore` in place. The change is uncommitted until the agent's next git sync. Re-run the analysis at any time with **Re-run analysis**.

### UI Flow

1. Click **Create Agent** on the Dashboard or Agents page.
2. Select a template source. GitHub templates display as cards with metadata from `template.yaml`.
3. Enter an agent name (lowercase, hyphens only).
4. Click **Create**.

### API

```
POST /api/agents
Content-Type: application/json
Authorization: Bearer <token>

{
  "name": "my-agent",
  "template": "github:Org/repo@branch"
}
```

### MCP

```
create_agent(name="my-agent", template="github:Org/repo@branch")
```

### Fork-to-Own Templates

Some templates are meant to be *owned* by the person deploying them, not run directly from the shared upstream repo. A template opts into this by declaring `fork_to_own: required` in its `template.yaml`. When you create an agent from such a template, Trinity copies the template into **your own** GitHub repository before building the container:

1. Trinity creates a destination repo under your account (**private by default**) using your GitHub PAT.
2. The template's default branch — with full history — is pushed into it. Your new agent's `origin` is this repo, so everything the agent commits stays in a repo you control.
3. Your PAT is saved as the agent's per-agent token, so restarts and recreations never fall back to a shared platform token.
4. A read-only `upstream` remote points back at the original template, so pulling in later template updates is a single `git pull upstream <branch>`.

**Prerequisite:** configure a GitHub PAT with repo-creation scope before creating the agent (see [GitHub PAT Setup](../integrations/github-pat-setup.md)). If the destination repo name is already taken, Trinity reuses it when it's empty or already holds the template's exact tip; if it's bound to another live agent or contains unrelated data, creation fails with a conflict so nothing is overwritten.

## For Agents

Agents created from templates inherit:

- The `CLAUDE.md` from the template as their system prompt.
- MCP server configuration from `.mcp.json.template`, with `${VAR}` placeholders resolved at runtime from injected credentials.
- Any files in the template repository, copied to `/home/developer/`.

The agent's container is labeled so it can be discovered and managed by the platform. After creation, credentials can be injected and the agent can be started, stopped, or scheduled independently.

## Limitations

- Agent names must be unique, lowercase, with hyphens allowed. No spaces or special characters.
- The `base_image` must match the configured allowlist. Requests for blocked images return HTTP 403.
- Private GitHub repositories require a GitHub PAT to be configured before use as a template source.
- Template metadata from GitHub is cached for 10 minutes. Changes to `template.yaml` may not appear immediately.

## See Also

- [Credential Management](../credentials/credential-management.md) -- How credentials are supplied to agents
- [GitHub PAT Setup](../integrations/github-pat-setup.md) -- Required for private templates and fork-to-own
- [Scheduling](../automation/scheduling.md) -- Running agents on a schedule
