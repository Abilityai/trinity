# Skills and Playbooks

Platform-managed skills that can be assigned to agents and invoked from the UI via the Playbooks tab or chat autocomplete.

> 📺 **Watch:** [Building Agents — Playbooks, Plugins, Deployment](https://youtu.be/MDxRZBikf70) *(Apr 2026)* · [Build an AI Recruiter Agent](https://youtu.be/K7hFWyFIf-Y) *(Jun 2026)* · [all videos](../videos.md)

## Concepts

- **Skill** -- A reusable capability shipped as a full directory package `.claude/skills/<name>/` — a `SKILL.md` plus any optional `scripts/`, templates, and resources — stored in the platform's skills library. Skills contain instructions, tools, and procedures that agents can execute.
- **Skills Library** -- A GitHub repository synced to Trinity containing all available skills. Admins can trigger a sync from Settings.
- **Skill Assignment** -- An owner assigns skills to agents. Assigned skill packages are injected into the agent on startup.
- **Playbook** -- A skill invoked from the UI. The Playbooks tab shows assigned skills with a "Run" button.
- **Playbook Autocomplete** -- Type `/` in the Chat tab input to see a dropdown of available playbooks with ghost text showing command syntax and argument hints.

## How It Works

### Admin -- Managing Skills

1. Go to **Settings** or the **Skills** admin page.
2. Sync the skills library from GitHub.
3. Create, edit, or delete skills (full CRUD).
4. View skill details and usage.

### Owner -- Assigning Skills

1. Open the agent detail page.
2. Go to the skills/playbooks section.
3. Assign skills from the library to this agent.
4. Skills are injected on the next agent start.

### User -- Running Playbooks

1. Open agent detail, click the **Playbooks** tab.
2. See the list of assigned skills with descriptions.
3. Click **Run** on a playbook -- this sends the skill as a task to the agent.
4. Or: in the **Chat** tab, type `/` to autocomplete a playbook command.

### Skill Injection on Agent Start

When an agent starts, each assigned skill's whole package is written to the agent's `~/.claude/skills/<name>/` directory (SKILL.md plus its scripts and resources), and a **Platform Skills** section is written into the agent's `CLAUDE.md` listing the injected skills. The agent can then use them as slash commands during execution.

Injection is:

- **Tree-SHA versioned and idempotent** — a skill whose package is unchanged since the last inject is skipped on start; only changed skills transfer.
- **Pruned by manifest diff** — files removed from a skill in the library are removed from the agent on the next injection (only paths the platform wrote are ever touched; agent-authored files are left alone).
- **Gitignored** — injected skill directories are added to the agent's `.gitignore` and left untracked, so the agent's git auto-sync never commits platform packages.

This is a backward-compatible change with no feature flag. On an older agent image that predates package support, a multi-file skill degrades gracefully to **SKILL.md only** (the extra files are dropped with an honest warning) — the skill still works.

### Skill Frontmatter and Dependency Checks

A skill's `SKILL.md` frontmatter can declare:

- `requires:` with `packages`, `binaries`, and `env` lists — the dependencies the skill expects.
- `user_invocable:` — whether the skill appears as a runnable playbook.
- `allowed-tools:` — the tools the skill is permitted to use.

At injection, Trinity runs a **declaration-only dependency check** and produces honest per-skill warnings (for example, a missing binary or a missing environment variable) instead of failing the injection. Declared package installs are surfaced but not performed. Warnings are reflected in the agent's Platform Skills section so the owner can see what a skill still needs. Environment checks report variable **names** only — values are never read.

### Running Skills Without Assignment (Skill Runner)

Two MCP tools let an agent run a **permitted** self-contained skill without assigning it:

| Tool | Description |
|------|-------------|
| `list_runnable_skills()` | List the skills this agent is permitted to run (its own permitted set, decided by an operator — not the whole library) |
| `run_skill(skill_name, input?)` | Run a permitted skill and return its result |

The runner is a **separate workspace** — it cannot see the calling agent's files. Use it for self-contained skills (call an API, generate an artifact from the `input` you pass). A skill that must operate on the caller's own files still goes through assignment and injection instead.

The Skill Runner is an **enterprise-gated** surface. In a community build, `run_skill` and `list_runnable_skills` return a "disabled" result.

## For Agents

MCP tools available for skill and playbook management:

| Tool | Description |
|------|-------------|
| `list_skills()` | List all platform skills |
| `get_skill(id)` | Get skill details |
| `get_skills_library_status()` | Library sync status |
| `assign_skill_to_agent(skill_id, agent_name)` | Assign a skill to an agent |
| `set_agent_skills(agent_name, skill_ids)` | Set all skills for an agent |
| `sync_agent_skills(agent_name)` | Re-inject skills into a running agent |
| `get_agent_skills(agent_name)` | List skills assigned to an agent |

## See Also

- [Scheduling](scheduling.md) -- Automate skill execution on a schedule
