# Trinity Compatible Agent Guide

> **Comprehensive guide** for creating and deploying Trinity-compatible agents.
>
> This document covers template structure, inter-agent collaboration, and best practices.

---

## Table of Contents

1. [Overview](#overview)
2. [Required Files](#required-files)
3. [Directory Structure](#directory-structure)
4. [template.yaml Schema](#templateyaml-schema)
5. [Declaring Credentials](#declaring-credentials)
6. [CLAUDE.md Requirements](#claudemd-requirements)
7. [Runtime Options](#runtime-options)
8. [Credential Management](#credential-management)
9. [Inter-Agent Collaboration](#inter-agent-collaboration)
10. [Shared Folders](#shared-folders)
11. [Platform Skills](#platform-skills)
12. [Custom Metrics](#custom-metrics)
13. [Agent Dashboard](#agent-dashboard)
14. [Operator Communication Is Asynchronous](#operator-communication-is-asynchronous-fire-and-park--1402)
15. [Memory Management](#memory-management)
16. [Content Folder Convention](#content-folder-convention)
17. [Package Persistence](#package-persistence)
18. [Compatibility Checklist](#compatibility-checklist)
19. [Migration Guide](#migration-guide)
20. [Best Practices](#best-practices)
21. [Autonomous Agent Design](#autonomous-agent-design)

---

## Overview

Trinity deploys agents from GitHub repositories or local directories. The platform reads template metadata, extracts credential requirements, injects secrets and platform capabilities at runtime, and starts the agent container.

**What makes an agent "Trinity-compatible"?**

- Follows the required file structure (`template.yaml`, `CLAUDE.md`, etc.)
- Uses placeholder syntax for credentials (`${VAR}` in `.mcp.json.template`)
- Keeps domain-specific logic in agent, lets platform handle orchestration
- Never commits secrets to the repository

---

## Required Files

### 1. `template.yaml` (Required)

Metadata file that Trinity reads to understand your agent.

```yaml
# Required fields
name: my-agent                    # Unique identifier (lowercase, hyphens ok)
display_name: "My Agent"          # Human-readable name for UI
description: "What this agent does"

# Resource limits (required)
resources:
  cpu: "2"                        # CPU cores (string)
  memory: "4g"                    # Memory limit (e.g., "2g", "4g", "8g")
```

See [template.yaml Schema](#templateyaml-schema) for complete field reference.

### 2. `CLAUDE.md` (Required)

Agent instructions that Claude Code reads. This is your agent's "brain" - it defines behavior, workflows, available tools, and constraints.

```markdown
# Agent Name

## Purpose
What this agent does...

## Available Tools
- Tool 1: description
- Tool 2: description

## Workflows
How the agent should approach tasks...

## Constraints
What the agent should NOT do...
```

See [CLAUDE.md Requirements](#claudemd-requirements) for guidelines.

### 3. `.mcp.json.template` (Required if using MCP servers)

MCP server configuration with credential placeholders. At container startup Trinity renders this file into `.mcp.json`, replacing `${VAR}` with values from the credential store (#2007).

```json
{
  "mcpServers": {
    "server-name": {
      "command": "npx",
      "args": ["-y", "@org/mcp-server"],
      "env": {
        "API_KEY": "${API_KEY}",
        "API_SECRET": "${API_SECRET}"
      }
    }
  }
}
```

**Important:**
- Use `${VAR_NAME}` syntax for credential placeholders — **inside `env` blocks only**
- `${VAR:-default}` is also supported (the default is used when the credential is unset)
- Never commit actual secrets
- Server names must match `credentials.mcp_servers` keys in `template.yaml`

**Where substitution happens, and what happens when it can't**

Trinity substitutes **only inside `env`**. A `${VAR}` in `args` or `command` is
**not** expanded, because the MCP config validator rejects both: a bare `$` in
`args` reads as a shell metacharacter, and `command` must be a literal entry
from the runtime allowlist (`npx`, `uvx`, `python`, `python3`, `node`, `bun`,
`deno`, `docker`). Letting a credential value become the executed command is the
config-injection class Trinity closed deliberately — so if you need a path,
pass it through `env` and read it in your server, or use `uvx <package>` rather
than an absolute interpreter path.

Rendering is **merge-only and refuse-on-doubt**:

- A server already present in `.mcp.json` is left untouched — including the
  `trinity` entry Trinity injects, and anything you edited by hand. Re-running
  is a no-op, so a restart never reverts your changes.
- A server whose placeholders cannot be resolved (no such credential, or the
  value is empty) is **withheld**, not configured with a blank value. The
  reason is logged to the agent's container output, one line per withheld
  server.
- A server the validator rejects is withheld the same way, with the validator's
  own reason — so `"command": "uv"` tells you `uv` is not in the allowlist
  rather than failing later at exec time.

The rest of the servers install normally: one bad entry never costs you the
good ones.

### 4. `.env.example` (Recommended)

Documents all required environment variables. Helps users understand what credentials are needed.

```bash
# MCP Server Credentials
API_KEY=your-api-key-here
API_SECRET=your-api-secret-here

# Script/Tool Credentials
OTHER_VAR=value
```

### 5. `.gitignore` (Required)

Must exclude secrets, instance-specific files, and large content.

> **Source of truth**: this list mirrors `_GITIGNORE_PATTERNS` in
> `src/backend/services/git_service.py`. The Python constant is the
> source of truth — the platform appends every entry to the agent's
> `.gitignore` on init and again on every Push. A unit test
> (`tests/unit/test_github_init_gitignore.py::test_doc_and_constant_in_sync`)
> asserts the two stay in sync.

```gitignore
# Shell init / history (instance-specific)
.bash_logout
.bashrc
.profile
.bash_history
.sudo_as_admin_successful

# Credentials - NEVER COMMIT
.env
.env.*
.mcp.json
credentials.json
*.pem
*.key

# Instance-specific directories - DO NOT COMMIT
.cache/
.local/
.npm/
.ssh/
.trinity/*
!.trinity/pre-check
!.trinity/post-check
!.trinity/pre-snapshot
!.trinity/setup.sh
!.trinity/persistent-processes.allow
!.trinity/brain-orb/
!.trinity/pipelines/
!.trinity/plugins.yaml
.tmp/
.trinity-clone-tmp/

# Large generated content - DO NOT COMMIT
content/

# Bulk data / deps / cache / index dirs - DO NOT COMMIT (#1596)
# These churn on every run and bloat .git unboundedly under auto-sync.
# Git sync is for code + state, not datasets/indexes/deps — those belong
# in data_paths (#1169). Negate per-repo if genuinely needed (e.g. !keep.db).
node_modules/
.venv/
venv/
__pycache__/
*.pyc
*.pyo
.pytest_cache/
.mypy_cache/
.ruff_cache/
.ipynb_checkpoints/
*.sqlite
*.sqlite3
*.db

# Claude Code - commit commands/skills/agents, exclude runtime data
.claude.json
.claude.json.backup
.claude/projects/
.claude/statsig/
.claude/todos/
.claude/debug/
.claude/sessions/
.claude/shell-snapshots/
.claude/plugins/
# settings.json is baked by the Trinity base image with container-only hook
# paths (/opt/trinity/hooks/*.py). Committing it bricks any clone made outside
# the container: the missing hook script exits 2, which Claude Code reads as
# "block this tool call", so every Bash/Edit/Write fails there. (#2036)
.claude/settings.json
.claude/remote-settings.json
.claude/policy-limits.json
.claude/backups/
.claude/.last-cleanup
# Keep: .claude/commands/, .claude/skills/, .claude/agents/

# Temporary files
*.log
*.tmp
.DS_Store

# Local overrides
*.local.md
*.local.json
!.env.example
!.mcp.json.template
```

**What to commit from `.claude/`:**
- ✅ `.claude/commands/` - Slash commands
- ✅ `.claude/skills/` - Skills (seeded to platform library on first deploy)
- ✅ `.claude/agents/` - Sub-agents
- ✅ `.claude/settings.local.json` - Claude Code settings

**What NOT to commit from `.claude/`:**
- ❌ `.claude/projects/` - Session data
- ❌ `.claude/statsig/` - Analytics
- ❌ `.claude/todos/` - Temporary todo lists
- ❌ `.claude/debug/` - Debug logs
- ❌ `.claude/sessions/` - Per-session state
- ❌ `.claude/shell-snapshots/` - Shell environment snapshots

**Note on Skills**: Skills in templates are seeded to the **Platform Skills Library** on first deployment, then managed centrally. See [Platform Skills](#platform-skills).

---

## Directory Structure

Every Trinity-compatible agent follows this structure:

```
my-agent/
├── .git/
├── .gitignore                     # CRITICAL: excludes secrets
│
├── CLAUDE.md                      # Agent instructions
├── README.md                      # Human documentation
├── template.yaml                  # Trinity metadata + credential schema
│
├── .claude/
│   ├── agents/                    # Agent's own sub-agents (optional)
│   ├── commands/                  # Slash commands (optional)
│   ├── skills/                    # Symlinks to assigned platform skills (auto-managed)
│   ├── skills-library/            # Read-only mount of all platform skills
│   └── settings.local.json        # Claude Code settings
│
├── .mcp.json.template             # MCP config with ${VAR} placeholders
├── .env.example                   # Documents required credentials
│
├── docs/                          # Agent documentation (recommended)
│   └── ...
│
├── outputs/                       # Generated content (COMMITTED)
│   ├── reports/
│   └── data/
│
├── content/                       # Large generated assets (NOT COMMITTED)
│   ├── videos/                    # Generated video files
│   ├── audio/                     # Generated audio files
│   ├── images/                    # Generated images
│   └── exports/                   # Data exports, large files
│
├── data/                          # Runtime data — SQLite DBs, datasets (NOT COMMITTED; see data_paths)
│
├── scripts/                       # Helper scripts (optional)
└── resources/                     # Static resources (optional)
```

---

## Runtime Data (`data_paths`) — #1169

If your agent accumulates **runtime data** — a SQLite database, a scraped dataset, an embeddings index — put it under **`data/`** (i.e. `/home/developer/data/...` inside the container) and declare it in `template.yaml`:

```yaml
data_paths:
  - data/**            # everything under data/
  # or be specific:
  # - data/app.sqlite
  # - data/datasets/**
```

Why this matters:

- **It's already durable.** `/home/developer` is a persistent Docker volume that survives container restarts, recreation, image upgrades, and template re-pulls (`git reset --hard`). You do **not** need a separate volume.
- **It stays out of git.** When you declare `data_paths`, Trinity appends `data/` to your agent's `.gitignore` at creation, so runtime data is never committed (no repo bloat, no accidental data leak). A template shipping its own `.gitignore` should pre-include `data/`.
- **It's portable.** The owner can export the data as a tar and import it into another agent/instance:
  - `POST /api/agents/{name}/data/export` — download a tar of `data/` (or `?format=base64` for small data inline).
  - `POST /api/agents/{name}/data/import` — restore a tar back into `data/` (only `data/**` entries are written; path traversal is rejected).
  - MCP tools `export_agent_data` / `import_agent_data` carry the tar as base64 for small datasets.

Keep `data_paths` entries **under `data/`** — entries that escape the data root (absolute paths, `../`) are never snapshotted. Don't overlap `.trinity/`, `.claude/`, `.env`, `.mcp.json`, `git.commit_paths`, or `persistent_state` — those are managed separately. (Validation checks `DP-001..DP-005` enforce this.)

---

## Declaring Plugins (`plugins`) — #1704

If your agent relies on Claude Code **marketplace plugins** (skills / subagents /
hooks bundled by a marketplace), declare them in `template.yaml`. Trinity writes a
committed, secret-free `~/.trinity/plugins.yaml` manifest at creation and
**re-installs anything missing on every container start** — so the selection is
portable and survives a git-based reconstitution onto a fresh volume or a new
host, not just a plain recreate (which the durable workspace volume already keeps).

```yaml
plugins:
  marketplaces:
    - name: abilityai
      source: abilityai/abilities        # owner/repo shorthand, OR an https:// URL
  installed:
    - trinity@abilityai                    # plugin@marketplace (every entry's
                                           # marketplace must be declared above)
  # Alternatively, mirroring Claude Code's own settings.json shape:
  # enabledPlugins:
  #   trinity@abilityai: true              # value:false entries are dropped
```

Rules and why:

- **Opt-in.** An absent/empty `plugins:` block is a full no-op — no file, no side
  effect. Undeclared agents are unaffected.
- **Committed, so it's portable.** `~/.trinity/plugins.yaml` is committed to your
  agent's repo (unlike `data_paths`/`persistent_state`, which are volume-local).
  Your installed *plugin cache* (`~/.claude/plugins/`) and `~/.claude.json` stay
  gitignored — the manifest is a plugin-only, secret-free declaration, not the
  cache.
- **The `source` must be safe.** Use `owner/repo` shorthand or an `https://` URL.
  A URL **must not embed credentials** (`https://user:token@…` is rejected) —
  Trinity resolves a private marketplace's git credential from the agent's
  `GITHUB_PAT` env at install time, never from the manifest. No `../` traversal,
  no leading `-`.
- **Re-install is idempotent.** On start Trinity reads the current state and
  installs only what's missing — a volume-persisting restart runs zero installs.
  A public marketplace works with network only; a private one needs the agent's
  `GITHUB_PAT`.
- **Trust model.** `plugin@marketplace` pins identity, not a commit — a re-install
  re-fetches the marketplace's *current* content. (A commit-pinned mode is a
  planned follow-up.)

> **Runtime installs.** Plugins you install *after* creation via `/plugin install`
> (not declared in the template) are **not** captured into the manifest yet — put
> anything you want to survive a reconstitution in `template.yaml plugins:`.

---

## Deploy as-is, then onboard in place — ent#411

Everything above assumes you build the agent *first* and deploy it second. The
opposite order also works, and it is the shortest path for a repo that already
exists: **deploy the repo as-is, then let the agent make itself compatible.**

`create_agent(template: github:owner/repo)` already tolerates a repo with **no
`template.yaml`** — it creates exactly as it always has. What used to be missing
was the other half: the deployed agent had no way to *become* compatible without a
human cloning the repo locally, running the wizard, and opening a PR against a
repo they may not even own.

So the Trinity plugin is **pre-installed in the agent base image** and ensured on
every boot, whether or not anything is declared. A bare repo has no
`template.yaml` to declare it in — which is exactly why declaring it cannot be the
condition for having it.

```
create_agent(template: "github:owner/bare-repo")   # no template.yaml, no plugins:
  → agent starts, trinity@abilityai already present
  → /trinity:onboard  (in place, inside the container)
      writes template.yaml (incl. plugins:), .env.example,
             .gitignore, .mcp.json.template
      commits + pushes back with the agent's own PAT
  → get_agent_compatibility_report → 0 HARD findings
```

What to know:

- **Additive, never subtractive.** A `template.yaml plugins:` block that omits
  `trinity@abilityai` does **not** uninstall it. Reconcile means "install what is
  missing", never "make the installed set match the declaration".
- **The platform's marketplace name is pinned.** A manifest may add marketplaces
  freely, but it cannot re-point `abilityai` at another source — the manifest is
  on the agent-writable volume, and a redefinable platform marketplace would be an
  arbitrary-code-fetch primitive rather than a self-healing boot step.
- **Source mode is pull-only.** Files the agent writes inside its container are
  container-local until pushed. In-place onboarding therefore ends in a push (or,
  for a tokenless agent, an honest report that the result is local plus the patch)
  — otherwise the work is lost on the next reset.
- **Opt out with `TRINITY_PLATFORM_PLUGINS=0`** (runtime) or build the image with
  `--build-arg TRINITY_PREINSTALL_PLUGINS=0` (air-gapped builds). Neither is fatal
  on its own: the build still succeeds if the marketplace is unreachable, and the
  boot hook retries.
- **Base-image ordering caveat.** An agent running an image built before this
  change silently lacks the pre-install; its next start installs the plugin
  through the boot hook instead (same caveat as #1704's hook). Rebuild the base
  image (`./scripts/deploy/build-base-image.sh`) to get the zero-subprocess path.
- **How to tell what happened.** The boot reconciler writes
  `~/.trinity/plugins-state.json`, and compatibility check **I-006** reports it:
  installed, withheld *with the reason*, or switched off. "The marketplace was
  unreachable" and "the operator never wanted it" are different facts, and a bare
  presence flag cannot tell them apart.

---

## template.yaml Schema

Complete schema with all available fields:

```yaml
# === REQUIRED FIELDS ===
name: my-agent                    # Unique identifier (lowercase, hyphens ok)
display_name: "My Agent"          # Human-readable name for UI
description: |
  Multi-line description of what this agent does.

# Resource limits (required)
resources:
  cpu: "2"                        # CPU cores (string)
  memory: "4g"                    # Memory limit (e.g., "2g", "4g", "8g")

# === RUNTIME CONFIGURATION (Optional) ===
# Defaults to Claude Code if not specified
runtime:
  type: claude-code               # "claude-code" or "gemini-cli"
  model: sonnet                   # Optional model override (e.g., "gemini-2.5-pro")

# === CREDENTIAL SCHEMA ===
# Trinity uses this to inject secrets
credentials:
  # MCP server credentials - extracted from .mcp.json.template
  mcp_servers:
    server-name:                  # Must match server name in .mcp.json.template
      env_vars:
        - API_KEY
        - API_SECRET

  # Environment variables for scripts/tools
  env_file:
    - OTHER_VAR
    - ANOTHER_VAR

# === OPTIONAL METADATA ===
version: "1.0"
author: "Your Name"
updated: "2025-01-15"
tagline: "Short one-liner for dashboard cards"

# Example prompts (shown in Info tab as "What You Can Ask")
use_cases:
  - "Do something useful"
  - "Ask about [topic]"

# === CAPABILITIES & FEATURES ===

# Capabilities (shown as chips in UI)
capabilities:
  - feature_one
  - feature_two

# Sub-agents with descriptions (displayed in Info tab)
sub_agents:
  - name: helper-agent
    description: "What this sub-agent does"
  - name: another-agent
    description: "Another sub-agent description"

# Slash commands with descriptions
commands:
  - name: my-command
    description: "What this command does"
  - name: another-command
    description: "Another command description"

# MCP servers with descriptions (for Info tab display)
mcp_servers:
  - name: server-name
    description: "What this MCP server provides"

# Skills (for documentation/seeding - managed at platform level)
# Skills in .claude/skills/ are seeded to Platform Skills Library on first deploy
skills:
  - name: my-skill
    description: "What this skill enables"

# Supported platforms (if applicable)
platforms:
  - trinity
  - local

# === GIT CONFIGURATION ===
#
# NOTE (#2137): the `git:` block below is INERT — no Trinity backend code reads
# `push_enabled`, `commit_paths`, or `ignore_paths`, and none of the bundled
# templates declare it. Sync behaviour is decided by source-mode vs working-branch
# at agent creation, `agent_git_config.auto_sync_enabled`, and the agent's own
# `.gitignore`. It is documented here for historical shape only; the compatibility
# checks that used to validate it (T-017, G-003, G-004, G-005) were retired for
# gating on a field nothing consumes. Do not write it expecting an effect.
#
# Two sync modes are available:
#
# SOURCE MODE (default): Pull-only from GitHub
#   - Agent tracks the source branch (main) directly
#   - Changes are pulled from GitHub, never pushed back
#   - Use when developing locally and pushing to GitHub
#   - git.push_enabled is ignored in this mode
#
# WORKING BRANCH MODE (legacy): Bidirectional sync
#   - Agent creates unique branch: trinity/{agent}/{instance-id}
#   - Changes can be pushed back to GitHub
#   - Set source_mode=false when creating agent to enable
#
git:
  push_enabled: true              # Only applies to Working Branch Mode
  commit_paths:                   # Paths auto-committed on sync (Working Branch Mode only)
    - memory/
    - outputs/
    - CLAUDE.md
  ignore_paths:
    - .mcp.json
    - .env
    - "*.log"

# === SHARED FOLDERS ===
# File-based collaboration between agents
# These are DEFAULT values when agent is created from template
shared_folders:
  expose: false                   # Expose /home/developer/shared-out
  consume: false                  # Mount shared folders from permitted agents

# === CUSTOM METRICS ===
# Agent-specific KPIs displayed in Trinity UI
# See "Custom Metrics" section for complete documentation
metrics:
  - name: metric_name             # Required: internal identifier (snake_case)
    type: counter                 # Required: counter|gauge|percentage|status|duration|bytes
    label: "Display Label"        # Required: shown in UI
    description: "What this tracks"  # Optional: tooltip
    unit: "items"                 # Optional: unit label (gauge type)
    warning_threshold: 80         # Optional (percentage type): yellow if below
    critical_threshold: 50        # Optional (percentage type): red if below
    values:                       # Required for status type only
      - value: "active"           # Value written to metrics.json
        color: "green"            # green|red|yellow|gray|blue|orange
        label: "Active"           # Display label in UI

# === RUNTIME DATA (Optional) — #1169 ===
# Declared runtime-data globs under data/ (durable, gitignored, exportable).
# See the "Runtime Data (data_paths)" section.
data_paths:
  - data/**

# === PLUGINS (Optional) — #1704 ===
# Claude Code marketplace plugins to install + re-install on boot. Committed +
# secret-free. See the "Declaring Plugins (plugins)" section.
plugins:
  marketplaces:
    - name: abilityai
      source: abilityai/abilities   # owner/repo or an https:// URL (no userinfo)
  installed:
    - trinity@abilityai             # plugin@marketplace
```

---

## Declaring Credentials

Machine-readable contract: [`docs/schemas/trinity-agent-credentials.schema.json`](schemas/trinity-agent-credentials.schema.json).

Two keys, one contract:

- **`credentials:`** — *which* variables the agent needs, **by name only**.
- **`credential_setup:`** — *optional*, describes each one so an operator can fill
  it in.

```yaml
credentials:                          # names only, always
  mcp_servers:
    stripe:
      env_vars: [STRIPE_API_KEY]
  env_file: [VAULT_BASE_PATH]

credential_setup:                     # optional; DECORATES the names above
  - name: STRIPE_API_KEY
    title: "Stripe secret key"
    description: "Reads charges and customers for the weekly revenue report."
    required: true
    secret: true
    format: secret
    setup_url: https://dashboard.stripe.com/apikeys

  - name: VAULT_BASE_PATH
    title: "Obsidian vault path"
    description: "Where the agent reads and files notes."
    required: false
    secret: false
    format: dirpath
    default: "./Brain"
```

### The rule that makes two keys safe

**`credential_setup:` can only decorate; it cannot declare.** Every entry's `name`
must already appear in `credentials.mcp_servers.<server>.env_vars` or
`credentials.env_file`. An entry naming anything else is reported as a named error
and dropped — its valid siblings still apply.

That is deliberate. Two places describing the same variable is normally how
documentation rots; here only one of them can *introduce* a variable, so they
cannot drift apart. If you get the error below, the fix is always the same:

```
credential_setup[2].name: 'FOO' is not declared in `credentials:`.
Add it to `credentials.env_file` or `credentials.mcp_servers.<server>.env_vars` —
`credential_setup:` adds setup guidance for variables `credentials:` declares,
it does not declare them.
```

### Why `credentials:` stays names-only

You may not put objects under `credentials.env_file`. A Trinity instance older
than this feature reads that list and then looks each name up as a dictionary
key — hand it a list of mappings and it crashes *while writing the agent's `.env`*.
Enrichment therefore lives in a sibling key that an older instance simply never
reads, which is why you can enrich a template today without waiting for every
instance to upgrade.

### Field reference

| Field | Required | Meaning |
|---|---|---|
| `name` | ✅ | The variable. Must be declared in `credentials:`. |
| `title` | | Short label, e.g. "Stripe secret key". Defaults to `name`. |
| `description` | | One or two sentences: what it is for, what it unlocks. This is what an operator reads while deciding whether to hand over a key. |
| `required` | | `true`/`false`. **Omitting it on an enriched entry means `true`.** |
| `secret` | | Mask in the UI. **Defaults to `true`** — fail-safe. Set `false` for a path, base URL or account id. |
| `format` | | Input hint: `secret`, `filepath`, `dirpath`, `url`, `email`, `number`, `boolean`, `text`. A hint, never validation. |
| `setup_url` | | Where to obtain it. **https only, and no `user@host` form.** |
| `default` | | A safe, non-secret pre-fill. Never a real credential — `template.yaml` is committed. |

Any key you invent is a named error with a "did you mean" suggestion, so an
`is_required:` typo can't silently flip a field's meaning. Use an `x-` prefix
(`x-my-vendor-thing:`) for something Trinity should ignore.

**`required` is near-irreversible in practice.** Once templates are distributed,
tightening a variable from `false` to `true` is a breaking change for anything
downstream that gates on it. Mark conservatively.

### The zero-credential contract

**A Trinity-installable agent must run with nothing configured.** An agent that
needs a key before it can start is not installable — it is a project.

State it explicitly:

```yaml
credentials: {}
```

An absent block and an empty one mean the same thing to Trinity, and neither is an
error. `{}` is better anyway, because *absent* is ambiguous to a human reading your
template — it could equally mean you forgot. `{}` says "considered, and there are
none", and the catalog shows a 0-credential badge an operator can trust.
`scout`, `sage` and `scribe` in `config/agent-templates/` are the reference
examples.

### Degrade, don't demand

If a credential is genuinely optional, the agent must **still work without it**,
with that one capability switched off and an explanation of what is missing. Mark
it `required: false` and check for it at runtime rather than asserting at startup.

An agent that starts, tells you what it can do today, and names what it would need
to do more is installable. An agent that exits non-zero on a missing key is not.

### Don't ask for what Trinity already injects

Trinity sets these on the container itself at creation — an operator has nothing to
paste, and asking makes your setup checklist look longer than it is:

`ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `GEMINI_API_KEY`, `GITHUB_PAT`,
`GITHUB_REPO`, and anything prefixed `TRINITY_`, `GIT_`, `OTEL_` or `CLAUDE_CODE_`.

They are excluded from the catalog's credential count for exactly this reason. You
may still *reference* them in `.mcp.json.template` — just don't declare them as
something an operator supplies.

### Composition with fork-to-own (ent#109)

A template declaring `fork_to_own: required` is copied into a **user-owned** repo at
creation, and the user's own PAT does the create and push. So a fork-to-own template
must not declare `GITHUB_PAT` as an operator-supplied credential — the platform
already resolves it per-agent. Declare only the credentials your agent's *own* work
needs.

### The author cost, stated plainly

Declaring `STRIPE_API_KEY` in `credentials:` is a **separate edit** from
referencing `${STRIPE_API_KEY}` in `.mcp.json.template`, and the compatibility
check T-015 fails if the two disagree.

That is a real cost and we're not pretending otherwise: three of Trinity's own six
default GitHub templates declare **zero** credentials while their
`.mcp.json.template` references between two and six, and their `.env.example`
documents seven to twelve. Those templates are T-015-red today and stay red until
someone does the edit.

We keep it that way on purpose. If `.mcp.json.template` counted as a declaration,
it would become a *second* authority on what your agent needs — which is exactly the
drift this design exists to prevent. The practical order is: seed `credentials:`
from your `.mcp.json.template` first, then enrich.

### Two brace forms Trinity cannot see

Neither is detected by Trinity's `${VAR}` readers, so neither is safe to rely on:

- **`${my-key}`** — a hyphen is in no variable-name charset, so the reference is
  dropped silently.
- **`${VAR:-default}`** — the `:-` default form is **mis-substituted**: the whole
  `VAR:-default` string is looked up as a key, misses, and your argument becomes an
  empty string.

Use a plain `${VAR}` and put the default in `credential_setup[].default`.

---

## CLAUDE.md Requirements

Every Trinity-compatible agent MUST have a CLAUDE.md with **domain-specific** content.

### Recommended Structure

```markdown
# Agent Name

## Identity
Who this agent is and what it does.
Your role, expertise, and personality.

## Domain Expertise
What you specialize in. Your knowledge areas.

## Available Tools
What MCP servers and integrations you have access to.

## Workflows
Domain-specific processes you follow.
How you approach tasks in your specialty.

## Constraints
- Domain-specific rules
- Safety constraints for your area
- Things you should NOT do
```

### Imports

CLAUDE.md files can import other files using `@path/to/file` syntax:

```markdown
See @README.md for project overview and @package.json for npm commands.

# Additional Instructions
- Git workflow: @docs/git-instructions.md
- Individual preferences: @~/.claude/my-project-instructions.md
```

Imports support relative paths, absolute paths, and `~` for home directory. Not evaluated inside code blocks.

### Best Practices

| ✅ Include | ❌ Exclude |
|-----------|-----------|
| Bash commands Claude can't guess | Anything Claude can infer from code |
| Code style rules differing from defaults | Standard language conventions |
| Testing instructions and runners | Detailed API docs (link instead) |
| Repository etiquette (branch naming, PRs) | Information that changes frequently |
| Architectural decisions | Long explanations or tutorials |
| Common gotchas | Self-evident practices ("write clean code") |

**If Claude ignores rules**, the file is probably too long. Use emphasis for critical rules: `IMPORTANT: Always run tests before committing`

---

## Runtime Options

Trinity supports multiple AI runtimes, allowing you to choose the best provider for each agent's use case.

### Available Runtimes

| Runtime | Provider | Context Window | Pricing | Best For |
|---------|----------|----------------|---------|----------|
| `claude-code` | Anthropic | 200K tokens | Pay-per-use | Complex reasoning, code quality |
| `gemini-cli` | Google | 1M tokens | Free tier | Large codebases, data processing |

### Configuring Runtime in template.yaml

```yaml
# Option 1: Simple runtime selection
runtime:
  type: gemini-cli

# Option 2: With model override
runtime:
  type: gemini-cli
  model: gemini-2.5-pro

# Option 3: Claude with specific model
runtime:
  type: claude-code
  model: opus  # or sonnet, haiku
```

### Default Behavior

If `runtime:` is not specified, agents default to `claude-code` for backward compatibility.

### Environment Requirements

| Runtime | Required Environment Variable |
|---------|------------------------------|
| `claude-code` | `ANTHROPIC_API_KEY` |
| `gemini-cli` | `GOOGLE_API_KEY` |

See [Gemini Support Guide](GEMINI_SUPPORT.md) for detailed setup instructions and cost comparisons.

---

## Long-Running & Background Processes (the orphan sweeper)

Each agent container runs a periodic **orphan sweeper** (`agent_server`,
issue #817). On a fixed interval it reads the container cgroup and
**SIGKILLs every process not on an allowlist** — this is how Trinity
recovers CPU/event-loop starvation from processes a finished execution
leaked behind. A process is **spared** when it is any of:

- the agent-server and its parent chain, and PID 1 (container init);
- an **in-flight platform execution** (chat / task / schedule / loop) and
  every subprocess it spawned — these carry a registered PID;
- an **operator session** — an SSH session **or** a live `docker exec`
  session (PPID 0) and its children (#1153);
- a base-image essential (the keep-alive, sshd wrapper, guardrail writer);
- a process whose argv matches a pattern in
  **`~/.trinity/persistent-processes.allow`**.

**What this means for templates:**

- **Run maintenance work as a platform execution, not a detached background
  job.** A FAISS index build, a data refresh, a migration — launch it as a
  scheduled task, a `run_agent_loop`, or an MCP `chat`/`task`. It then has a
  registered PID (and, under the pull model, a lease) and is never swept.
  A bare `nohup … &` or a process double-forked away from the execution has
  no registered PID and **will be reaped** mid-run.
- **Long-lived daemons started from a `SessionStart` hook** (e.g. a local
  MCP HTTP server) must be declared in
  `~/.trinity/persistent-processes.allow` — one `fnmatch` glob per line,
  `#` for comments:

  ```
  # local MCP server started by SessionStart hook
  *my-mcp-server*
  ```

- **Operator debugging is safe.** A long `docker exec agent-<name> …` or
  `trinity ssh` session survives sweeps (#1153). When the session exits,
  any children it left behind reparent to PID 1 and are reaped on the next
  sweep — as intended.

Each kill is logged at **WARNING** with the victim's `pid` and a
length-capped `cmd=` — `docker logs agent-<name> 2>&1 | grep OrphanSweep`
shows exactly what was reaped if a job disappears unexpectedly.

---

## Credential Management

> **Updated 2026-02-05 (CRED-002)**: Simplified credential system using direct file injection with encrypted git storage.

### Credential Flow

Credentials are managed as files in the agent workspace:

1. **`.env`** — Source of truth for KEY=VALUE credentials
2. **`.mcp.json`** — MCP server configuration (edit directly, no template substitution)
3. **`.credentials.enc`** — Encrypted backup safe for git commits

### Injecting Credentials

- **Via UI**: Agent Detail → Credentials tab → Quick Inject (paste KEY=VALUE text)
- **Via MCP**: `inject_credentials(agent_name, {".env": "KEY=value\n"})`
- **Via API**: `POST /api/agents/{name}/credentials/inject`

### Export/Import for Git

To persist credentials across deployments:

1. **Export**: Click "Export to Git" → encrypts `.env` and `.mcp.json` → writes `.credentials.enc`
2. **Commit**: The `.credentials.enc` file is safe to commit (AES-256-GCM encrypted)
3. **Auto-import**: On agent start, if `.credentials.enc` exists but `.env` doesn't, credentials are automatically decrypted and injected

### Your Agent Should

- Read MCP credentials from `.mcp.json` (Claude Code does this automatically)
- Read script credentials from `.env` or environment variables
- Never store plaintext credentials in committed files
- Use `.credentials.enc` for secure credential persistence in git

---

## Inter-Agent Collaboration

### MCP Tools for Collaboration

Agents use Trinity MCP for collaboration:

```
mcp__trinity__chat_with_agent(
    agent_name="cornelius",
    message="Research: {topic}"
)
```

### Access Control

Permission checks before any agent-to-agent communication:

- **Same owner**: Allowed
- **Explicit permission granted**: Allowed (via Permissions tab)
- **Admin**: Allowed (bypass)
- **Otherwise**: Denied with error

### 60-Second MCP Call Timeout

> **Design Limitation**: Claude Code enforces a hardcoded 60-second timeout on all MCP HTTP tool calls. Any `chat_with_agent` call that takes longer than 60 seconds will fail regardless of the `timeout_seconds` parameter.

When designing agents that collaborate with other agents, ensure that:
- Synchronous MCP calls complete within 60 seconds
- Complex tasks use the async pattern (`parallel=true, async=true`) and poll for results
- Large data exchanges use shared folders instead of MCP return values

See the [Multi-Agent System Guide](MULTI_AGENT_SYSTEM_GUIDE.md#design-limitation-60-second-mcp-call-timeout) for workaround patterns.

### Delegation Pattern

When an agent needs help from another agent, it can create a delegation task:

```yaml
tasks:
  - id: "task-003"
    name: "Get research from Cornelius"
    status: "active"
    type: "delegation"
    delegate_to: "agent-cornelius"
    delegate_message: "Research AI agent architectures"
```

---

## Direct Git Operations (MCP) — #905

Trinity exposes the agent git surface as **direct, deterministic MCP tools** so an
orchestrator can run git without spending an LLM turn via `chat_with_agent`:

| Tool | What it does |
|------|--------------|
| `get_git_status` | Branch, remote, changed files, pending-sync flag (read-only) |
| `get_git_log` | Recent commits (`limit` clamped 1–100, read-only) |
| `get_git_sync_state` | Persisted sync-health row (last status, consecutive failures) |
| `git_sync` | Stage + commit + push to the working branch |
| `git_pull` | Pull from GitHub (`clean` / `stash_reapply` / `force_reset`) |
| `reset_to_main_preserve_state` | ⚠️ **Destructive** recovery — adopt `origin/main` and force-with-lease, preserving only the persistent-state allowlist |

Guidance:

- **Conflicts stay LLM-mediated.** A `git_sync`/`git_pull` conflict returns a
  structured `409` with `conflict_type`/`conflict_class` and a hint to resolve via
  `chat_with_agent` — the deterministic tools never try to auto-merge.
- **Auth.** `git_sync` and `reset_to_main_preserve_state` are **owner-only**; a
  shared (non-owner) key gets read + `git_pull` only. Agent-scoped keys may act on
  themselves or agents they have explicit permission for.
- **Traceability.** Every mutating call (sync/pull success *and* conflict, and every
  reset path) is audited, and the MCP tool-call row joins the backend git row via a
  shared request id (`GET /api/audit-log?request_id=`).

---

## Shared Folders

Trinity enables file-based collaboration between agents via shared Docker volumes.

### Folder Paths

| Path | Purpose |
|------|---------|
| `/home/developer/shared-out` | **Your agent's shared folder** - accessible to permitted agents |
| `/home/developer/shared-in/{agent-name}` | **Other agents' folders** - read/write access |

### How It Works

1. **Expose**: When enabled, creates a Docker volume (`agent-{name}-shared`) mounted at `/home/developer/shared-out`. Other permitted agents can mount this.

2. **Consume**: When enabled, mounts shared folders of all agents you have permission to call at `/home/developer/shared-in/{agent-name}`.

3. **Permissions**: Access follows the Agent Permissions system. Agent B can only mount Agent A's folder if:
   - Agent A has expose enabled
   - Agent B has consume enabled
   - Agent B has permission to call Agent A

### Template Configuration

Set defaults in `template.yaml`:

```yaml
shared_folders:
  expose: true    # Expose /home/developer/shared-out
  consume: true   # Mount shared folders from permitted agents
```

### Example Usage

**Agent A** (exposing):
```bash
echo "Data from Agent A" > /home/developer/shared-out/report.txt
```

**Agent B** (consuming, with permission to call Agent A):
```bash
cat /home/developer/shared-in/agent-a/report.txt
```

**Note**: Changes to shared folder config require an agent restart.

---

## Platform Skills

**Skills are the recommended way to encode reusable knowledge for agents.** A Skill is a markdown file that teaches Claude how to do something specific — like reviewing PRs using your team's standards or generating commit messages in your preferred format. When an agent encounters a task that matches a Skill's purpose, Claude automatically applies it.

Unlike slash commands (which require `/command` to invoke), **skills are model-invoked**: Claude decides which skills to use based on the task at hand. This makes skills ideal for organizational knowledge that should be consistently applied.

### How Trinity Manages Skills

1. **Platform stores skills** in a centralized library (synced from GitHub or created via UI)
2. **Admins manage skills** via the `/skills` page (create, edit, delete)
3. **Owners assign skills** to their agents via the Skills tab
4. **Skills are injected** into `~/.claude/skills/<name>/SKILL.md` on agent start
5. **CLAUDE.md is updated** with a "Platform Skills" section listing available skills

### Skill Types

Use naming conventions to indicate how a skill should be applied:

| Type | Naming Convention | When to Use | Example |
|------|-------------------|-------------|---------|
| `policy` | `policy-*` | Always-active rules that Claude follows implicitly | `policy-code-review`, `policy-security` |
| `procedure` | `procedure-*` | Step-by-step instructions for specific tasks | `procedure-incident-response`, `procedure-deploy` |
| `methodology` | (no prefix) | General guidance for approaches to problems | `verification`, `tdd`, `systematic-debugging` |

### Writing Effective Skills

Every skill needs a `SKILL.md` file with YAML frontmatter and markdown instructions:

```yaml
---
name: code-review
description: Reviews code for quality, security, and best practices. Use when reviewing pull requests, code changes, or asking "is this code good?"
---

# Code Review

## Instructions

When reviewing code, check for:
1. **Security issues** - SQL injection, XSS, exposed secrets
2. **Error handling** - Are all error cases handled?
3. **Performance** - Any obvious N+1 queries or inefficient loops?
4. **Readability** - Is the code self-documenting?

## Output Format

Provide feedback in three sections:
- **Critical**: Must fix before merge
- **Suggestions**: Would improve the code
- **Good**: Highlight what was done well
```

#### Frontmatter Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Skill identifier (lowercase, hyphens, max 64 chars) |
| `description` | Yes | What the skill does and when to use it. **Claude uses this to decide when to apply the skill.** |
| `allowed-tools` | No | Restrict which tools Claude can use (e.g., `Read, Grep, Glob` for read-only) |
| `disable-model-invocation` | No | Set `true` to prevent Claude from auto-invoking. User must invoke with `/skill-name`. Use for workflows with side effects. |
| `user-invocable` | No | Set `false` to hide from `/` menu. Use for background knowledge Claude should apply automatically but users shouldn't invoke directly. |
| `argument-hint` | No | Autocomplete hint (e.g., `[issue-number]`, `[filename] [format]`) |
| `model` | No | Override model for this skill (`sonnet`, `opus`, `haiku`) |
| `context` | No | Set to `fork` to run in isolated subagent context |
| `agent` | No | Subagent type when `context: fork` (`Explore`, `Plan`, or custom agent name) |
| `hooks` | No | Lifecycle hooks scoped to this skill (`PreToolUse`, `PostToolUse`, `Stop`) |

#### Invocation Control

| Frontmatter | User can invoke | Claude can invoke | Context behavior |
|-------------|-----------------|-------------------|------------------|
| (default) | ✅ | ✅ | Description loaded, full skill on invoke |
| `disable-model-invocation: true` | ✅ | ❌ | Not in context until user invokes |
| `user-invocable: false` | ❌ | ✅ | Description loaded, auto-applied when relevant |

#### String Substitutions

Skills support variable substitution:

| Variable | Description |
|----------|-------------|
| `$ARGUMENTS` | All arguments passed when invoking (e.g., `/fix-issue 123` → `123`) |
| `$ARGUMENTS[N]` or `$N` | Specific argument by index (`$0` = first, `$1` = second) |
| `${CLAUDE_SESSION_ID}` | Current session ID (useful for logging, session-specific files) |

```yaml
---
name: fix-issue
description: Fix a GitHub issue
disable-model-invocation: true
---
Fix GitHub issue $ARGUMENTS following our coding standards.
# Or: Migrate the $0 component from $1 to $2.
```

#### Dynamic Context Injection

The `!`command`` syntax runs shell commands before skill content is sent to Claude:

```yaml
---
name: pr-summary
description: Summarize a pull request
context: fork
agent: Explore
---
## Context
- PR diff: !`gh pr diff`
- Changed files: !`gh pr diff --name-only`

Summarize this pull request...
```

Commands execute immediately; output replaces the placeholder. Claude only sees the final rendered content.

#### Description Best Practices

The description is critical — Claude uses it to decide whether to apply the skill. A good description:

```yaml
# ❌ Bad - too vague
description: Helps with code

# ✅ Good - specific actions and trigger terms
description: Reviews code for quality, security, and best practices. Use when reviewing pull requests, code changes, or asking "is this code good?"
```

Include:
1. **What it does**: Specific capabilities (reviews, generates, validates)
2. **When to use it**: Trigger phrases users would say ("review this PR", "is this secure?")

### Restricting Tool Access

Use `allowed-tools` to limit what Claude can do when a skill is active:

```yaml
---
name: read-only-analysis
description: Analyze code without making changes
allowed-tools: Read, Grep, Glob
---
```

This is useful for:
- Read-only skills that shouldn't modify files
- Security-sensitive workflows
- Analysis tasks that should never write

### Skill Size Guidelines

- **Keep `SKILL.md` under 500 lines.** Move detailed reference material to separate files.
- **Skill descriptions budget**: ~15,000 characters total across all skills. If you have many skills, some may be excluded from context.
- **Bundled scripts** run without consuming context tokens — only their output does.

### Multi-File Skills

For complex skills, use progressive disclosure — essential info in `SKILL.md`, details in supporting files:

```
my-skill/
├── SKILL.md           # Overview and navigation (<500 lines)
├── reference.md       # Detailed docs (loaded when needed)
├── examples.md        # Usage examples
└── scripts/
    └── validate.py    # Utility script (executed, not loaded)
```

In `SKILL.md`, reference the files:

```markdown
For detailed API reference, see [reference.md](reference.md).

To validate input, run:
```bash
python scripts/validate.py input.txt
```

### Agent Perspective

Agents see assigned skills in the standard Claude Code location:

```
~/.claude/skills/
├── verification/
│   └── SKILL.md
├── systematic-debugging/
│   └── SKILL.md
└── policy-code-review/
    └── SKILL.md
```

The agent's `CLAUDE.md` is also updated with a "Platform Skills" section:

```markdown
## Platform Skills

This agent has the following skills installed in `~/.claude/skills/`:

- `/verification` - Use with /verification command
- `/systematic-debugging` - Use with /systematic-debugging command
- `/policy-code-review` - Use with /policy-code-review command
```

This allows agents to answer "what skills do you have?" without scanning the filesystem.

### What This Means for Templates

**Templates should NOT include `.claude/skills/`** — skills are managed at the platform level and assigned per-agent. If your template includes skills in `.claude/skills/`, they will be seeded to the platform library on first deployment, then managed centrally.

### Syncing Skills

When skills are updated at the platform level, agents receive updates:
- **On next start**: Skills automatically injected
- **While running**: Use "Inject to Agent" button in Skills tab or MCP `sync_agent_skills` tool

### MCP Tools for Skills

Agents can interact with the skills system programmatically:

| Tool | Description |
|------|-------------|
| `list_skills` | List all platform skills |
| `get_skill` | Get skill details and content |
| `assign_skill_to_agent` | Assign a skill to an agent |
| `sync_agent_skills` | Re-inject skills to running agent |

### Skills vs. Slash Commands vs. CLAUDE.md

| Mechanism | Invoked By | Best For |
|-----------|------------|----------|
| **Skills** | Claude (automatic) | Reusable knowledge that applies across many situations |
| **Slash commands** | User (`/command`) | Specific actions the user explicitly requests |
| **CLAUDE.md** | Always loaded | Project-wide context and constraints |

**Use skills when**: The knowledge should apply automatically based on the task (e.g., always apply security review standards when reviewing code).

**Use slash commands when**: The user needs to explicitly trigger an action (e.g., `/deploy staging`).

---

## Custom Metrics

Agents can define custom KPIs displayed in the Trinity UI Metrics tab. This enables domain-specific observability beyond generic tool call counts.

### Metric Types

| Type | Description | Display | Example |
|------|-------------|---------|---------|
| `counter` | Monotonically increasing | Large number | "42 Messages" |
| `gauge` | Value that can go up/down | Number + optional unit | "12.5 Avg Words" |
| `percentage` | 0-100 with progress bar | Colored bar | "75% Success" |
| `status` | Enum/state value | Colored badge | "Active", "Idle" |
| `duration` | Time in seconds | Formatted time | "2h 15m" |
| `bytes` | Size in bytes | Formatted size | "1.2 MB" |

### How It Works

1. Define metrics in `template.yaml` under `metrics:`
2. Agent writes values to `metrics.json` in workspace
3. Trinity UI displays metrics in the Metrics tab (auto-refresh every 30 seconds)
4. **Agent must be running** for metrics to be visible

### File Locations

The agent server reads from the agent's working directory (`/home/developer/`):
- **Definitions**: `/home/developer/template.yaml`
- **Values**: `/home/developer/metrics.json`

### template.yaml Metric Definitions

```yaml
metrics:
  # Counter - monotonically increasing value
  - name: messages_processed        # Internal identifier (snake_case)
    type: counter
    label: "Messages"               # Display label
    description: "Total messages"   # Tooltip text

  # Gauge - value that goes up and down
  - name: avg_response_time
    type: gauge
    label: "Avg Response"
    unit: "ms"                      # Optional unit label

  # Percentage - with color thresholds
  - name: success_rate
    type: percentage
    label: "Success Rate"
    warning_threshold: 80           # Yellow if below 80%
    critical_threshold: 50          # Red if below 50%

  # Status - enum with colored badges
  - name: current_state
    type: status
    label: "State"
    values:                         # Required for status type
      - value: "active"             # The value in metrics.json
        color: "green"              # green, red, yellow, gray, blue, orange
        label: "Active"             # Display label
      - value: "idle"
        color: "gray"
        label: "Idle"
      - value: "error"
        color: "red"
        label: "Error"

  # Duration - time in seconds
  - name: last_cycle_duration
    type: duration
    label: "Last Cycle"
    description: "Duration of last processing cycle"

  # Bytes - size in bytes
  - name: cache_size
    type: bytes
    label: "Cache Size"
```

### metrics.json Format

Your agent writes current values to `metrics.json`:

```json
{
  "messages_processed": 42,
  "avg_response_time": 125.5,
  "success_rate": 87.5,
  "current_state": "active",
  "last_cycle_duration": 120,
  "cache_size": 1048576,
  "last_updated": "2025-12-10T10:30:00Z"
}
```

**Notes:**
- Keys must match the `name` field in template.yaml
- `last_updated` is optional but recommended (shown as "Updated X ago" in UI)
- Values are read when the Metrics tab is viewed or refreshed

### Complete Example

**template.yaml:**
```yaml
name: research-agent
display_name: Research Agent
description: Autonomous researcher

resources:
  cpu: "1"
  memory: "2g"

metrics:
  - name: research_cycles
    type: counter
    label: "Research Cycles"
    description: "Total research cycles completed"

  - name: findings_discovered
    type: counter
    label: "Findings"
    description: "Total findings discovered"

  - name: research_status
    type: status
    label: "Status"
    values:
      - value: "active"
        color: "green"
        label: "Researching"
      - value: "idle"
        color: "gray"
        label: "Idle"

  - name: last_cycle_duration
    type: duration
    label: "Last Cycle"
```

**Updating metrics in your agent:**
```bash
# In a script or via Claude Code
cat > /home/developer/metrics.json << 'EOF'
{
  "research_cycles": 5,
  "findings_discovered": 23,
  "research_status": "idle",
  "last_cycle_duration": 180,
  "last_updated": "2025-12-10T10:30:00Z"
}
EOF
```

**In CLAUDE.md instructions:**
```markdown
## Metrics Tracking

After each research cycle, update metrics.json:
- Increment `research_cycles`
- Update `findings_discovered` count
- Set `research_status` to "active" during work, "idle" when done
- Record `last_cycle_duration` in seconds
```

---

## Agent Dashboard

Agents can define a custom dashboard displayed in the Trinity UI Dashboard tab.

### File Location

Save the dashboard configuration to **`/home/developer/dashboard.yaml`** — the root of the agent's working directory.

If the file does not exist, no dashboard will be displayed.

### Basic Structure

```yaml
title: "My Agent Dashboard"
refresh: 30                    # Auto-refresh interval (seconds, min 5)

sections:
  - title: "Status"
    layout: grid               # 'grid' or 'list'
    columns: 3                 # 1-4 columns
    widgets:
      - type: metric
        label: "Total Tasks"
        value: 42
        trend: up

      - type: status
        label: "System"
        value: "Healthy"
        color: green           # green, red, yellow, gray, blue, orange

      - type: progress
        label: "Disk Usage"
        value: 75
        color: yellow
```

### Widget Types

| Type | Required Fields | Description |
|------|----------------|-------------|
| `metric` | label, value | Number with optional trend (up/down) |
| `status` | label, value, color | Colored badge |
| `progress` | label, value | Progress bar (0-100) |
| `text` | **content** | Plain text (NOT `text` or `value`) |
| `markdown` | **content** | Rendered markdown |
| `table` | columns, rows | Tabular data |
| `list` | **items** | Bullet/numbered list (NOT `values` or `list`) |
| `link` | label, **url** | Clickable link (NOT `href`) |
| `image` | src, alt | Image display |
| `divider` | - | Horizontal line |
| `spacer` | - | Vertical space |

### Widget Examples (All Types)

**IMPORTANT**: Use exact field names shown below. Common mistakes:
- `text` widget requires `content` (not `text`, `value`, or `label`)
- `list` widget requires `items` (not `values`, `list`, or `content`)
- `link` widget requires `url` (not `href` or `link`)

```yaml
widgets:
  # METRIC - numeric value with optional trend
  - type: metric
    label: "Total Tasks"        # Required
    value: 42                   # Required (number)
    trend: up                   # Optional: up, down
    trend_value: "+12%"         # Optional
    unit: "tasks"               # Optional
    description: "Since start"  # Optional

  # STATUS - colored badge
  - type: status
    label: "System Status"      # Required
    value: "Healthy"            # Required (string)
    color: green                # Required: green, red, yellow, gray, blue, orange, purple

  # PROGRESS - progress bar (0-100)
  - type: progress
    label: "Disk Usage"         # Required
    value: 75                   # Required (0-100)
    color: yellow               # Optional: green, red, yellow, blue

  # TEXT - plain text (NOT 'text' or 'value'!)
  - type: text
    content: "This is plain text"  # Required - MUST use 'content'
    size: md                       # Optional: xs, sm, md, lg
    color: gray                    # Optional
    align: center                  # Optional: left, center, right

  # MARKDOWN - rendered markdown
  - type: markdown
    content: "**Bold** and *italic* text"  # Required - MUST use 'content'

  # TABLE - tabular data
  - type: table
    title: "Recent Events"      # Optional
    columns:                    # Required
      - { key: date, label: Date }
      - { key: event, label: Event }
    rows:                       # Required
      - { date: "2024-01-01", event: "Started" }
      - { date: "2024-01-02", event: "Completed" }
    max_rows: 5                 # Optional

  # LIST - bullet or numbered list (NOT 'values'!)
  - type: list
    title: "Tasks"              # Optional
    items:                      # Required - MUST use 'items'
      - "Task 1"
      - "Task 2"
      - "Task 3"
    style: bullet               # Optional: bullet, number, none
    max_items: 10               # Optional

  # LINK - clickable link (NOT 'href'!)
  - type: link
    label: "Documentation"      # Required
    url: "https://example.com"  # Required - MUST use 'url'
    external: true              # Optional: opens in new tab
    style: button               # Optional: 'button' or omit for text link
    color: blue                 # Optional

  # IMAGE - image display
  - type: image
    src: "/files/chart.png"     # Required (or full URL)
    alt: "Chart description"    # Required
    caption: "Weekly metrics"   # Optional

  # DIVIDER - horizontal line
  - type: divider

  # SPACER - vertical space
  - type: spacer
    size: lg                    # Optional: sm (8px), md (16px), lg (32px)
```

### Updating Dashboard Data

Your agent updates the dashboard by rewriting `dashboard.yaml`. Use dynamic values:

```yaml
widgets:
  - type: metric
    label: "Processed"
    value: 127              # Update this value in your agent
    description: "Last run: 2 min ago"
```

**Note**: Agent must be running for dashboard to display. See `docs/memory/feature-flows/agent-dashboard.md` for complete schema.

---

## Agent-Defined Pipelines (#919)

If your agent runs a long-running, multi-stage pipeline (e.g. perception → incubation → synthesis → publish → measure), you can make its shape and live state **uniformly discoverable** by Trinity — **without** Trinity owning the DAG, the execution, or the recovery. You own all of that (via schedules, events, the operator queue, and the pre-check hook); Trinity only **reads** two files you publish.

### The file contract

Publish two kinds of file inside your container:

```
~/.trinity/pipelines/<pipeline_id>.yaml                  # definition (the DAG)
~/.trinity/pipeline-state/<pipeline_id>/<instance_id>.json  # live state per instance
```

- **`<pipeline_id>`** and **`<instance_id>`** must match `^[A-Za-z0-9._-]+$` (no `/`, no `..`). Trinity interpolates them into read paths and rejects anything else.
- **`<instance_id>` SHOULD be time-sortable.** Trinity selects the *latest* instance by the state file's mtime, tie-broken by `instance_id` lexically.
- Authoritative schemas (validate your files against these):
  - [`docs/schemas/agent-pipeline.schema.json`](schemas/agent-pipeline.schema.json) — the definition
  - [`docs/schemas/agent-pipeline-state.schema.json`](schemas/agent-pipeline-state.schema.json) — the state. **Required:** `instance_id`, `current_stage`, `health`, `updated_at` (ISO-8601), `escalations` (array). Trinity reads exactly these for its summary.

Minimal example:

```yaml
# ~/.trinity/pipelines/research.yaml
id: research
name: Daily Research Pipeline
stages:
  - id: collect
  - id: synthesize
  - id: publish
```

```json
// ~/.trinity/pipeline-state/research/2026-06-26T1200.json
{
  "instance_id": "2026-06-26T1200",
  "pipeline_id": "research",
  "current_stage": "synthesize",
  "health": "green",
  "updated_at": "2026-06-26T12:05:00Z",
  "escalations": []
}
```

### Reading it back (MCP tools)

Two thin, read-only MCP tools wrap the existing agent file-browser surface — no new backend endpoint, no Trinity-side DB:

- **`list_agent_pipelines(agent_name)`** — enumerates `pipelines/*.yaml`, each with a health summary from its latest instance (`current_stage`, `health`, `open_escalations`, `updated_at`). Returns `[]` when you publish no pipelines. A *stopped/unreachable* agent surfaces a real error, not an empty list (the files live in the container).
- **`get_agent_pipeline_state(agent_name, pipeline_id, instance_id?)`** — the full parsed state JSON for one instance; omit `instance_id` for the latest. A missing pipeline/instance returns a clean not-found (never a 500).

`open_escalations` counts `escalations[]` entries that are still open — an entry is open unless it carries `open: false`, `resolved: true`, or `status` in `{resolved, closed, done}`.

### Grouping escalations by pipeline (operator-queue convention)

When you escalate a stuck stage to the **operator queue**, put the pipeline coordinates in the queue item's free-form `context` JSON so escalations group by pipeline in the UI — **no Trinity schema change**:

```json
{ "pipeline_id": "research", "instance_id": "2026-06-26T1200", "stage": "synthesize" }
```

### Driving it (agent-side heartbeat — not Trinity)

Stage advancement, retry, and escalation are owned by your agent: run a single `pipeline-tick` skill on a cron schedule, gated by your `~/.trinity/pre-check` hook so it's near-free when nothing needs attention. That heartbeat ships with the **`agent-dev:add-pipeline`** plugin in [`abilityai/abilities`](https://github.com/abilityai/abilities), **not** with Trinity.

> **Adoption note:** these tools ship as MCP + docs only. Existing agents return `[]` until they adopt this file convention — that's by design, not a bug.

---

## Operator Communication Is Asynchronous (fire-and-park) — #1402

All human/operator communication on Trinity is **asynchronous**, mediated by the operator queue (`~/.trinity/operator-queue.json` ⇄ the Operating Room UI). Design your agent around this from day one:

**The contract: fire-and-park, never block-and-wait.**

1. **Park** the request (approval / question / alert) by appending an entry to the queue file.
2. **End the turn.** A turn must never wait, poll, or sleep for a human response — a human may answer in minutes or days, and a blocked turn burns its entire timeout budget while pinning platform capacity. Never assume a synchronous human answer is available mid-turn.
3. **Process responses in a later turn.** At the start of each autonomous run, check the queue file for `status: "responded"` items, act on them, then mark them `"acknowledged"`.

**Ask before irreversible actions.** Before an action the platform cannot undo or verify — payments, emails/messages through the agent's own credentials, public posts, destructive deletions — park an `approval` and end the turn when uncertain. This matters most under re-delivery: pull-mode coordination (#1081) re-runs a turn whose worker died, so a task you receive may have partially run before. Check your own records and the queue file before repeating an irreversible effect; do the reversible parts first and gate only the irreversible step.

**Request IDs must be globally unique.** Derive them from the current execution ID (`approval-{execution_id}-{short-slug}`, execution ID is in the Execution Context block of your system prompt). Date-serial IDs (`req-20260307-001`) collide across agents and a colliding request is silently swallowed; a derived ID also makes a re-park under re-delivery idempotent instead of duplicating the request.

**Plan for the response's return path.** The operator's answer is written back to your queue file within seconds, but only a *future turn* can act on it:

- An agent with a schedule or heartbeat picks it up on the next run — nothing extra needed.
- An agent with **no** future turn (one-shot webhook/chat tasks) must include resume instructions in the request itself ("after approving, re-trigger schedule X" / "send me a chat message with your decision") — otherwise an approved action never executes.
- Set `expires_at` on gating requests; an `expired` flip means "not approved — do not proceed."

**This is a compliance contract, not a security boundary.** The agent writes and reads its own queue file, so a misbehaving or prompt-injected agent can skip parking or forge a response. Operators must not treat "the agent asked for approval" as a guarantee; rails that need a hard guarantee belong behind confined Trinity-owned tools, not agent-side judgment. The queue's value here is disciplined recovery plus an audit trail.

The full queue-file protocol (JSON schema, request types, priorities, hygiene) is documented in the platform system prompt every agent receives; the escalation-grouping convention for pipelines is in [Agent-Defined Pipelines](#agent-defined-pipelines-919) above.

---

## Memory Management

Agents manage their own memory. Trinity provides storage, not strategy. Each agent can implement memory however it sees fit.

### Example Structure (Optional)

This is a suggested pattern, not a requirement:

```
memory/
├── context.md           # Long-term learned context
├── preferences.json     # User preferences
├── session_notes/       # Per-session working notes
│   └── 2025-12-05.md
└── summaries/           # Compressed old context
    └── 2025-11.md       # Monthly summary
```

---

## Content Folder Convention

For agents that generate large files (videos, audio, images, exports), Trinity provides a standard convention to prevent Git repository bloat.

### The Problem

Large generated files:
- Bloat Git repositories (video files can be 100s of MB)
- Slow down GitHub sync operations
- Risk accidental commits

### The Solution

Trinity automatically creates a `content/` directory structure:

```
/home/developer/content/
├── videos/      # Generated video files
├── audio/       # Generated audio files
├── images/      # Generated images
└── exports/     # Data exports, large files
```

**Key Properties:**
- ✅ **Persists** - Files survive container restarts (same Docker volume as workspace)
- ✅ **Excluded from Git** - Automatically added to `.gitignore`
- ✅ **Not synced** - Git sync ignores `content/` directory

### Usage in Your Agent

When generating large assets, save them to `content/`:

```python
# In your agent's scripts
output_path = "/home/developer/content/videos/my-video.mp4"
```

```markdown
# In your CLAUDE.md
When generating videos, save them to `content/videos/`.
When exporting data, save to `content/exports/`.
```

### outputs/ vs content/

| Directory | Synced to Git? | Use For |
|-----------|---------------|---------|
| `outputs/` | ✅ Yes | Small files you want versioned (reports, summaries) |
| `content/` | ❌ No | Large files that shouldn't be in Git (videos, audio) |

---

## Package Persistence

When agents install system packages (via `apt-get`, `npm install -g`, etc.), those packages are lost when the container is updated. Trinity provides a setup script convention to handle this.

### How It Works

1. When an agent installs a system package, it also appends the command to `~/.trinity/setup.sh`
2. On container start, Trinity runs this script automatically
3. Packages are reinstalled, surviving image updates

### Setup Script Location

```
/home/developer/.trinity/setup.sh
```

This file lives in the persistent workspace volume and survives container recreation.

### Usage Pattern

When installing packages, always add them to the setup script:

```bash
# Install the package
sudo apt-get install -y ffmpeg

# Remember it for future container starts
mkdir -p ~/.trinity
echo "sudo apt-get install -y ffmpeg" >> ~/.trinity/setup.sh
```

### Pre-configuring in Templates

Templates can ship with a pre-defined `setup.sh`:

```
my-agent/
├── .trinity/
│   └── setup.sh          # Pre-defined package installations
├── template.yaml
└── CLAUDE.md
```

Example `setup.sh` for a video processing agent:

```bash
#!/bin/bash
# Package persistence script - runs on every container start

# System packages
sudo apt-get update -qq
sudo apt-get install -y -qq ffmpeg imagemagick

# Global npm packages
npm install -g typescript ts-node

# Python packages (user-space)
pip install --user opencv-python moviepy
```

### What Goes Where

| Package Type | Persists Automatically? | Setup Script Needed? |
|--------------|------------------------|---------------------|
| `pip install --user` | ✅ Yes (in ~/.local) | No |
| `npm install` (local) | ✅ Yes (in node_modules/) | No |
| `go install` | ✅ Yes (in ~/go/) | No |
| `apt-get install` | ❌ No | Yes |
| `npm install -g` | ❌ No | Yes |
| System-level configs | ❌ No | Yes |

### Best Practices

1. **Prefer user-space installs**: `pip install --user`, local `npm install` when possible
2. **Keep setup.sh idempotent**: Use `-y` flags, check if already installed
3. **Minimize apt-get**: Each install adds startup time
4. **Document dependencies**: List required packages in README.md

---

## Compatibility Checklist

An agent is Trinity-compatible if:

### Required Files
- [ ] Has `template.yaml` with required fields (name, display_name, description, version, resources)
- [ ] Has `CLAUDE.md` with identity and domain-specific instructions
- [ ] Has `.mcp.json.template` with `${VAR}` placeholders (if using MCP servers)
- [ ] Has `.env.example` documenting required credentials
- [ ] Has `.gitignore` excluding secrets and large content

### Directory Structure
- [ ] (Optional) Has `docs/` directory for documentation

### Security
- [ ] No credentials stored in repository
- [ ] `.mcp.json` and `.env` are gitignored
- [ ] Sensitive files excluded from git sync paths

### Credentials (see "Declaring Credentials")
- [ ] Every `${VAR}` in `.mcp.json.template` is declared in `credentials:` (T-015 checks this)
- [ ] Every declared variable is documented in `.env.example` (K-001 checks this)
- [ ] `credentials: {}` is stated explicitly if the agent needs nothing
- [ ] No platform-injected variable (`GEMINI_API_KEY`, `GITHUB_PAT`, `TRINITY_*`, …) is
      declared as something an operator supplies
- [ ] (Optional) `credential_setup:` describes each variable, and every `name` in it is
      declared in `credentials:`
- [ ] No `${my-key}` or `${VAR:-default}` — neither form is visible to Trinity's readers

### Behavior
- [ ] Agent CLAUDE.md focuses on domain-specific instructions
- [ ] Can run both locally and on Trinity platform
- [ ] Starts and reports what it can do with NO credentials configured

---

## Migration Guide

To convert an existing agent to Trinity-compatible:

1. **Create repository structure** matching the directory layout above
2. **Extract CLAUDE.md** from current instructions (domain-specific only)
3. **Create template.yaml** with metadata and credentials
4. **Create .mcp.json.template** from current .mcp.json (replace values with ${VAR})
5. **Create .env.example** listing all required variables
6. **Add .gitignore** excluding secrets and platform directories
7. **Deploy to Trinity** and verify

---

## Best Practices

### Security
- Never commit secrets to the repository
- Use `.env.example` with placeholder values, not real credentials
- Add `.env` and `.mcp.json` to `.gitignore`
- Review git diffs before committing

### Credential Naming
Use descriptive names that indicate the service:
```
TWITTER_API_KEY          # Good
CLOUDINARY_API_SECRET    # Good
API_KEY                  # Bad - too generic
KEY1                     # Bad - meaningless
```

### Template Validation
Before publishing, verify:
- [ ] `template.yaml` has all required fields
- [ ] All `${VAR}` placeholders in `.mcp.json.template` are listed in `credentials`
- [ ] `.env.example` documents all variables
- [ ] No secrets are committed anywhere

### Domain Focus
- Keep CLAUDE.md focused on your agent's specialty
- Let Trinity handle collaboration and infrastructure
- Use outputs/ for generated content

### Documentation
- Keep documentation in a `docs/` folder
- Use README.md for quick-start and overview
- Document workflows, integrations, and constraints

---

## Autonomous Agent Design

Trinity agents achieve autonomy through a three-phase lifecycle:

```
DEVELOP → PACKAGE → SCHEDULE
```

1. **Develop** — Refine procedures interactively until they consistently produce good results
2. **Package** — Codify proven procedures as slash commands in `.claude/commands/`
3. **Schedule** — Run commands on cron via `schedules:` in template.yaml or the UI

### Autonomy Design Principles

| Principle | Description |
|-----------|-------------|
| **Self-contained** | No user input during execution |
| **Deterministic output** | Consistent format for parsing/alerts |
| **Graceful degradation** | Partial results better than failure |
| **Bounded scope** | Predictable runtime and cost |
| **Idempotent** | Safe to run multiple times |

### Quick Example

```yaml
# template.yaml
schedules:
  - name: Morning Health Check
    cron: "0 8 * * *"
    message: "/health-check"
    timezone: "UTC"
    enabled: true
```

```markdown
# .claude/commands/health-check.md
---
description: Automated fleet health check
allowed-tools: mcp__trinity__list_agents, mcp__trinity__get_agent
---

# Health Check
1. List all agents using `mcp__trinity__list_agents`
2. Evaluate context usage and last activity
3. Generate structured report
```

→ **Full guide**: [Autonomous Agent Design Guide](AUTONOMOUS_AGENT_DESIGN.md)

---

## Stop hook authoring — release inherited stdout

The Trinity platform reads the agent's `{"type":"result"}` JSON from Claude
Code's stdout pipe. Stop hooks inherit that pipe FD by default. If a hook
spawns a long-running subprocess that calls `setsid()` (`ssh` from
`git push` is the canonical case — see [#586](https://github.com/abilityai/trinity/issues/586) /
[#618](https://github.com/abilityai/trinity/pull/618)), the subprocess can
hold the pipe open during network I/O even after Claude exits. The
platform catches this via the `kill_cgroup_orphans()` sweep in
`drain_reader_threads` (SIGKILLs anything in the container cgroup outside
the allowlist) so your agent still completes, but the
slow path adds drain latency and shows up in `[METRIC] drain_outcome` log
lines with `outcome=natural` or `outcome=force_close`. Defensive hooks
avoid the slow path by releasing the inherited stdout FD before any
blocking I/O:

```bash
#!/bin/bash
exec 1>/dev/null    # release stdout pipe FD; keep stderr for diagnostics
set +e
git push origin HEAD
```

Closing stdout only (not stderr) preserves error messages from a failing
`git push`. Non-shell hook runtimes need the equivalent:

- **Python**: `sys.stdout = open(os.devnull, "w")` before spawning.
- **Node**: `process.stdout.destroy()` (or redirect with `stdio` when
  spawning child processes).

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-02-05 | **Credential System Refactor (CRED-002)**: Updated Credential Management section for new simplified system; Direct file injection replaces Redis-based assignments; Export/Import with encrypted `.credentials.enc` for git storage; Auto-import on agent startup |
| 2026-01-27 | **Advanced Skills & CLAUDE.md**: Added 8 new skill frontmatter fields (`disable-model-invocation`, `user-invocable`, `argument-hint`, `model`, `context`, `agent`, `hooks`); Added invocation control table; Added string substitutions (`$ARGUMENTS`, `$N`, `${CLAUDE_SESSION_ID}`); Added dynamic context injection (`!`command``); Added skill size guidelines; Added CLAUDE.md imports (`@path` syntax) and best practices table |
| 2026-01-26 | **Platform Skills Best Practices**: Expanded Platform Skills section with comprehensive skill writing guidance; Added SKILL.md format, frontmatter fields, description best practices, `allowed-tools` for restricting tool access, multi-file skill patterns, Skills vs Commands vs CLAUDE.md comparison table; Emphasized skills as the recommended way to encode reusable knowledge |
| 2026-01-25 | **Platform Skills**: Added new section documenting centralized Skills Library; Skills managed at platform level, mounted read-only into agents; Three skill types (policy, procedure, methodology); Updated directory structure and .gitignore notes |
| 2026-01-13 | **Dashboard widget examples**: Added complete examples for ALL 11 widget types with required field names highlighted; Added warning box about common field name mistakes (`content` not `text`, `items` not `values`, `url` not `href`) |
| 2026-01-13 | Added Agent Dashboard section with YAML schema and widget types reference |
| 2026-01-12 | Expanded Custom Metrics section: added file locations, complete template.yaml examples for all 6 metric types (counter, gauge, percentage, status, duration, bytes), metrics.json format with last_updated field, complete working example, and CLAUDE.md integration guidance |
| 2026-01-12 | Updated .gitignore: added instance-specific files (.npm, .ssh, .trinity, .cache, .claude.json, .sudo_as_admin_successful); clarified what to commit vs exclude from .claude/ directory |
| 2026-01-12 | Added Package Persistence section with setup.sh convention for surviving container updates |
| 2026-01-12 | Simplified guide: removed Platform Injection, Testing Locally, Troubleshooting, Registering with Trinity, Multi-Agent Systems sections; Made memory/ optional; Added docs/ best practice |
| 2026-01-01 | Added Autonomous Agent Design section with lifecycle overview; Reference to detailed guide |
| 2025-12-30 | Documented Source Mode (default) vs Working Branch Mode (legacy) in Git Configuration; Removed Task DAG/workplan content (feature removed 2025-12-23) |
| 2025-12-27 | Added Content Folder Convention for large generated assets (videos, audio, images) |
| 2025-12-24 | Removed Chroma MCP integration - templates should include their own vector memory if needed |
| 2025-12-18 | Added Multi-Agent Systems section with System Manifest deployment reference |
| 2025-12-14 | Consolidated from AGENT_TEMPLATE_SPEC.md and trinity-compatible-agent.md |
| 2025-12-13 | Added shared folders |
| 2025-12-10 | Added custom metrics specification |

---

*This document is the single source of truth for Trinity-compatible agent development.*
