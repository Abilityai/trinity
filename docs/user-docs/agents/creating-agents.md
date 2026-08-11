# Creating Agents

Agents are created from templates or from scratch. Each agent runs as an isolated Docker container with its own filesystem, credentials, and MCP server configuration.

> 📺 **Watch:** [Build an AI Recruiter Agent](https://youtu.be/K7hFWyFIf-Y) *(Jun 2026)* · [Build and Deploy Agents in Cursor](https://youtu.be/amqiysdlEWY) *(Apr 2026)* · [From Zero to Deployed](https://youtu.be/-TSZyekDS6o) *(Apr 2026)* · [all videos](../videos.md)

## Concepts

**Template sources** define where agent blueprints come from:

- **GitHub Template** -- A repository in `github:Org/repo` format. Supports branch selection with `github:Org/repo@branch`. **Public** repos clone with **no GitHub token** -- Trinity clones them anonymously. This is **source-mode only**: an anonymous clone can't push back, so pushing, Working-Branch mode, and fork-to-own still require a token. Private repos require a GitHub PAT.
- **Admin-Configured Templates** -- GitHub repos configured by an admin in Settings. Metadata (name, description, resources, MCP servers) is fetched from each repo's `template.yaml` via the GitHub API and cached for 10 minutes. These appear as cards in the **Library** page's Agent Templates section (`/library`; the old `/templates` path redirects there).
- **Local Templates** -- Auto-discovered from the `config/agent-templates/` directory and shown as a curated **Starter Templates** section on the Library page. The recommended starters (`scout`, `sage`, `scribe`) are ordered first; internal test and demo fixtures (marked `hidden: true` in their `template.yaml`) are hidden from the list but stay creatable by id.
- **From Scratch** -- Creates a minimal agent with a default `CLAUDE.md`.

**Where the GitHub template list comes from.** Trinity resolves it in order:

1. **Admin-configured list** — if an admin has curated GitHub templates in Settings, that list is authoritative and nothing else is consulted.
2. **Remote registry** — otherwise Trinity fetches a curated registry over HTTPS, so the starter catalogue can be refreshed without upgrading Trinity. The result is cached (about an hour) with a durable last-known-good copy, and every failure degrades quietly to the next tier.
3. **Bundled defaults** — the built-in list, which is empty by default.

A default install therefore shows starter templates plus whatever the registry offers, and never blocks agent creation on a registry being reachable.

**Template structure** follows a standard layout:

| File | Purpose |
|------|---------|
| `template.yaml` | Agent metadata: `display_name`, `description`, `resources`, `credentials`, `credential_setup`, `schedules`, `runtime` |
| `CLAUDE.md` | Agent instructions and system prompt |
| `.mcp.json.template` | MCP config template with `${VAR}` placeholders for credential injection |
| `.env.example` | Example credentials file listing required environment variables |

All bundled templates ship the canonical `.gitignore`, so an agent created from one never auto-commits caches, virtualenvs, or local databases into its repository.

**Runtime options** control which CLI the agent uses. An agent's runtime — Claude Code, OpenAI Codex, or Gemini CLI — is chosen via `runtime.type` in `template.yaml` (see [Agent Runtimes](agent-runtimes.md)):

- `claude-code` (default)
- `codex`
- `gemini-cli`

**Display label vs. slug** distinguishes an agent's two names:

- The **`name`** is a lowercase-hyphens **slug**. It is immutable, guarantees uniqueness, and is what URLs, MCP tool names, schedules, and webhooks resolve to.
- The **display label** is a separate, editable, human-facing name. It is non-unique and presentation-only; when blank it renders as the slug. You can set it at creation via the optional `display_label` field (max 120 characters), and change it later — see [Managing Agents](managing-agents.md#display-label).

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

### Declared Schedules

A template can declare the recurring work its agent is designed to do, in a `schedules:` block in `template.yaml`:

```yaml
schedules:
  - name: daily-briefing
    cron: "0 9 * * *"
    message: /daily-briefing
    enabled: true
    timezone: Europe/London
    description: Morning summary of overnight activity
```

Trinity materializes these as real schedules at creation — through the UI, the API, and MCP alike. Before this, a template's declared schedules were design documentation that nothing acted on.

Rules worth knowing:

- `name`, `cron`, and `message` are required. `cron` must be a strict 5-field Unix expression (`@daily` and 6-field forms are rejected). `timezone` must be an IANA zone.
- Up to 20 schedules per template. Unknown keys are ignored, so a template may carry extra design metadata.
- A declared schedule always inherits the agent's execution timeout, so it can never exceed the agent's own cap.
- The block is treated as untrusted input: a malformed entry is dropped with a named error rather than failing the creation, and the errors surface in the template catalogue and the compatibility report.
- Any `id:` you write is ignored — Trinity mints its own schedule ids.

See [Scheduling](../automation/scheduling.md).

### Importing an Existing GitHub Repository

When you create an agent from a repository you already have — rather than a curated template — you choose **how** Trinity should take it on:

| Intent | What happens | Git sync |
|--------|--------------|----------|
| **Clone** | The default. Trinity clones the repository and keeps it wired to that remote. | Yes — the agent pushes back to the source repo |
| **Fork** | Trinity forks the repository into your own GitHub account first (requires the template to declare `fork_to_own`). | Yes — to your fork, with `upstream` pointing at the original |
| **Copy** | Trinity takes a one-time snapshot of the files, strips the `.git` history, and gives the agent a standalone workspace. | **No** — no remote, no token, no sync |

**Copy** is the right choice when you want to start *from* someone's repository without staying attached to it. The agent gets the files and nothing else: no GitHub credentials are stored, no remote is configured, and the agent never appears on git-sync surfaces. If you later decide you do want a repository, use **Initialize GitHub Sync** on the agent's Git tab.

Creation refuses clearly rather than doing something surprising: an intent on a non-GitHub template, a fork without fork parameters, a copy or clone of a template that requires fork-to-own, and copy for an ephemeral agent all return a named error before anything is created. An unreadable or private source without a token is reported as "not found or private" — it never confirms whether a repository exists.

### Inline Compatibility Check

After creating an agent from a repository, the create dialog runs the compatibility check inline and shows the result before you leave. It waits for the agent to genuinely finish starting (not merely for the container to exist), then reports findings. If the agent fails to start, you are told so rather than left on a spinner.

The check is advisory — the agent exists either way, and you can re-run the analysis from the Overview tab at any time.

### UI Flow

1. Click **Create Agent** in the Dashboard header, or **Use Template** on the Library page.
2. Select a template source. GitHub templates display as cards with metadata from `template.yaml`. For a free-form repository, pick the import intent (clone / fork / copy).
3. Enter an agent name (lowercase, hyphens only) — this is the immutable slug.
4. Optionally set a **display label** (max 120 characters) — the friendly name shown across the UI. Leave it blank to render under the slug.
5. Click **Create**, then review the inline compatibility result.

### API

```
POST /api/agents
Content-Type: application/json
Authorization: Bearer <token>
Idempotency-Key: <optional-unique-key>

{
  "name": "my-agent",
  "display_label": "My Agent",
  "template": "github:Org/repo@branch",
  "import_intent": "copy"
}
```

`import_intent` accepts `fork`, `copy`, or `clone`, and applies only to `github:` templates. Omit it for the legacy behaviour. Supplying an `Idempotency-Key` makes a retried create safe: the same key within 24 hours replays the original response instead of creating a second agent, and a duplicate still in flight returns 409.

A copy-intent response carries an `import_snapshot` block recording the source repo, branch, commit SHA, and file count.

### MCP

```
create_agent(name="my-agent", template="github:Org/repo@branch", import_intent="copy")
```

The MCP tool accepts `copy` and `clone`. Fork stays UI/REST-only because it needs fork parameters.

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
- A public GitHub template clones with no token, but only in **source mode**. The anonymous clone can't push, so pushing changes back, Working-Branch mode, and fork-to-own still require a token.
- Template metadata from GitHub is cached for 10 minutes. Changes to `template.yaml` may not appear immediately.
- A **copy**-intent agent has no upstream by design. If both its container and its workspace volume are lost, a rebuild produces an empty workspace rather than re-fetching — Trinity will not silently pull whatever the source repository looks like *now*. Export the agent's data periodically if that matters; the creation audit entry records the source repo and exact commit.
- An unresolvable or invalid local template id is rejected at creation rather than producing an empty agent.

## See Also

- [Credential Management](../credentials/credential-management.md) -- How credentials are supplied to agents
- [GitHub PAT Setup](../integrations/github-pat-setup.md) -- Required for private templates and fork-to-own
- [Scheduling](../automation/scheduling.md) -- Running agents on a schedule
