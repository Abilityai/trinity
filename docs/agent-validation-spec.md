# Agent Validation Specification

> **Purpose**: Canonical list of checks run by `GET /api/agents/{name}/compatibility` and the `get_agent_compatibility_report` MCP tool.  
> **Evaluation model**: Checks marked `[AI]` are evaluated by an LLM reading the relevant file content. Checks marked `[STATIC]` use deterministic file/pattern analysis.  
> **Severity**: `HARD` = will likely break Trinity at runtime. `SOFT` = best-practice recommendation. `INFO` = improvement suggestion.

---

## Check Index

| ID | Severity | Type | Category | Description |
|----|----------|------|----------|-------------|
| F-001 | HARD | STATIC | File Structure | `template.yaml` exists |
| F-002 | HARD | STATIC | File Structure | `CLAUDE.md` exists |
| F-003 | SOFT | STATIC | File Structure | `.gitignore` exists |
| F-004 | SOFT | STATIC | File Structure | `.env.example` exists |
| F-005 | SOFT | STATIC | File Structure | `.mcp.json.template` exists (if MCP servers declared) |
| F-006 | INFO | STATIC | File Structure | `README.md` exists |
| F-007 | INFO | STATIC | File Structure | `.trinity/setup.sh` exists (if system packages are needed) |
| F-008 | INFO | STATIC | File Structure | `.claude/commands/` directory exists |
| F-009 | INFO | STATIC | File Structure | At least one `.claude/skills/` or `.claude/commands/` file exists |
| F-010 | SOFT | STATIC | File Structure | `dashboard.yaml` exists |
| F-011 | INFO | STATIC | File Structure | `ARCHITECTURE.md` (or `docs/architecture.md`) exists |
| F-012 | INFO | STATIC | File Structure | `docs/memory/requirements.md` (or `REQUIREMENTS.md`) exists |
| F-013 | INFO | STATIC | File Structure | `CHANGELOG.md` exists |
| S-001 | HARD | STATIC | Security | `.env` is excluded in `.gitignore` |
| S-002 | HARD | STATIC | Security | `.mcp.json` is excluded in `.gitignore` |
| S-003 | HARD | STATIC | Security | No hardcoded secrets in any committed file |
| S-004 | HARD | STATIC | Security | `.claude/projects/` is excluded in `.gitignore` |
| S-005 | HARD | STATIC | Security | `.trinity/` is excluded in `.gitignore` |
| S-006 | SOFT | STATIC | Security | `.claude/statsig/`, `.claude/todos/`, `.claude/debug/`, `.claude/sessions/`, `.claude/shell-snapshots/` excluded in `.gitignore` |
| S-007 | SOFT | STATIC | Security | `content/` is excluded in `.gitignore` |
| S-008 | SOFT | STATIC | Security | `*.pem`, `*.key`, `credentials.json` patterns in `.gitignore` |
| S-009 | HARD | STATIC | Security | `.mcp.json.template` uses `${VAR}` placeholders (no literal secrets) |
| S-010 | SOFT | STATIC | Security | Credential variable names are service-specific (not generic `API_KEY`) |
| T-001 | HARD | STATIC | template.yaml | Valid YAML syntax |
| T-002 | HARD | STATIC | template.yaml | `name` field present and valid (lowercase alphanumeric + hyphens, ≤64 chars) |
| T-003 | HARD | STATIC | template.yaml | `description` field present and non-empty |
| T-004 | HARD | STATIC | template.yaml | `resources.cpu` present and valid Docker CPU string |
| T-005 | HARD | STATIC | template.yaml | `resources.memory` present and valid Docker memory string |
| T-006 | SOFT | STATIC | template.yaml | `display_name` field present |
| T-007 | SOFT | STATIC | template.yaml | `version` field present (semantic version format) |
| T-008 | SOFT | STATIC | template.yaml | `author` field present |
| T-009 | SOFT | AI | template.yaml | `description` is substantive (2+ sentences, explains purpose clearly) |
| T-010 | SOFT | STATIC | template.yaml | `use_cases` array present with 3–7 examples |
| T-011 | SOFT | STATIC | template.yaml | `capabilities` array present |
| T-012 | SOFT | STATIC | template.yaml | `mcp_servers` descriptions match actual servers in `.mcp.json.template` |
| T-013 | SOFT | AI | template.yaml | `use_cases` entries are realistic, specific, actionable prompts (not buzzword lists) |
| T-014 | SOFT | AI | template.yaml | `tagline` (if present) is concise and explains unique value (not generic "AI assistant") |
| T-015 | SOFT | STATIC | template.yaml | `credentials` schema lists all variables referenced in `.mcp.json.template` |
| T-016 | INFO | STATIC | template.yaml | `schedules` entries (if any) reference existing `.claude/commands/` files |
| T-017 | HARD | STATIC | template.yaml | No conflicting Trinity-injected files named in commit paths (`.env`, `.mcp.json`) |
| C-001 | HARD | STATIC | CLAUDE.md | Valid UTF-8 markdown, non-empty |
| C-002 | HARD | AI | CLAUDE.md | Has an identity/purpose section (who the agent is and what it does) |
| C-003 | SOFT | AI | CLAUDE.md | Contains domain-specific instructions (not just generic Claude guidance) |
| C-004 | SOFT | AI | CLAUDE.md | Lists available tools and MCP integrations |
| C-005 | SOFT | AI | CLAUDE.md | Contains at least one concrete workflow or step-by-step procedure |
| C-006 | SOFT | AI | CLAUDE.md | Contains explicit constraints or guardrails section |
| C-007 | SOFT | STATIC | CLAUDE.md | Under 2000 lines (beyond this, Claude ignores trailing instructions) |
| C-008 | SOFT | AI | CLAUDE.md | Does not repeat standard Claude knowledge (generic best practices, library docs) |
| C-009 | SOFT | AI | CLAUDE.md | Constraints are explicit and actionable, not vague ("be safe", "be helpful") |
| C-010 | INFO | AI | CLAUDE.md | Critical rules are emphasized (uses IMPORTANT:, **bold**, or similar) |
| C-011 | INFO | AI | CLAUDE.md | No stale references to tools/services not available in this agent |
| C-012 | SOFT | AI | CLAUDE.md | Identity section conveys a coherent persona aligned with the agent's purpose |
| K-001 | HARD | STATIC | Credentials | Every `${VAR}` in `.mcp.json.template` has a corresponding entry in `.env.example` |
| K-002 | HARD | STATIC | Credentials | Every `${VAR}` in `.mcp.json.template` is listed in `template.yaml` credentials schema |
| K-003 | SOFT | STATIC | Credentials | `.env.example` comments explain what each variable is for |
| K-004 | SOFT | STATIC | Credentials | `.env.example` uses placeholder values (not empty or real values) |
| K-005 | SOFT | AI | Credentials | Credential variable names follow the `SERVICE_FIELD` convention (e.g., `TWITTER_API_KEY`) |
| G-001 | HARD | STATIC | Git Config | `.claude/` is NOT excluded from `.gitignore` wholesale (must stay committed for Claude Code) |
| G-002 | SOFT | STATIC | Git Config | `.gitignore` follows the canonical pattern list from `git_service.py` |
| G-003 | SOFT | STATIC | Git Config | If `git.commit_paths` in `template.yaml`, paths do not include `.env`, `.mcp.json`, `content/` |
| G-004 | SOFT | STATIC | Git Config | If `git.ignore_paths` in `template.yaml`, `.env` and `.mcp.json` are listed |
| G-005 | INFO | STATIC | Git Config | `git.push_enabled` is explicitly set (not left to platform default) |
| P-001 | SOFT | STATIC | Skills/Playbooks | Each skill file has valid YAML frontmatter |
| P-002 | SOFT | STATIC | Skills/Playbooks | Each skill frontmatter has `name` and `description` fields |
| P-003 | SOFT | AI | Skills/Playbooks | Skill `description` is specific enough to trigger correct auto-invocation (not vague) |
| P-004 | SOFT | STATIC | Skills/Playbooks | Each skill file is under 500 lines |
| P-005 | SOFT | AI | Skills/Playbooks | Skills are domain-specific to this agent's purpose (not generic dev methodology) |
| P-006 | HARD | AI | Skills/Playbooks | Autonomous/scheduled skills contain NO approval gates or human decision points |
| P-007 | SOFT | AI | Skills/Playbooks | Autonomous skills include error handling and notification on failure |
| P-008 | SOFT | AI | Skills/Playbooks | Playbooks that run on schedule are self-contained (no required user input) |
| P-009 | INFO | AI | Skills/Playbooks | Complex skills use multi-file layout (SKILL.md + reference.md / examples.md) |
| P-010 | SOFT | AI | Skills/Playbooks | Skills are idempotent or clearly document that they are not |
| P-011 | SOFT | AI | Skills/Playbooks | `allowed-tools` is scoped appropriately (read-only skills don't request write tools) |
| P-012 | INFO | AI | Skills/Playbooks | Skills include a completion checklist or explicit output format |
| A-001 | SOFT | AI | Autonomy Design | Scheduled messages reference slash commands, not raw prose prompts |
| A-002 | SOFT | STATIC | Autonomy Design | Cron expressions in `template.yaml` schedules are valid cron syntax |
| A-003 | SOFT | AI | Autonomy Design | Agent has a clear autonomy model: either interactive or autonomous, not ambiguous |
| A-004 | INFO | STATIC | Autonomy Design | `.trinity/pre-check` (if present) is executable and has a valid shebang |
| A-005 | INFO | AI | Autonomy Design | Scheduled task descriptions are specific about expected output format |
| D-001 | SOFT | STATIC | Dashboard/Metrics | `dashboard.yaml` (if present) is valid YAML |
| D-002 | SOFT | STATIC | Dashboard/Metrics | All widget types are from the supported list |
| D-003 | HARD | STATIC | Dashboard/Metrics | Widget required fields are present (e.g., `content` not `text` for text widgets, `items` not `values` for list widgets, `url` not `href` for link widgets) |
| D-004 | SOFT | STATIC | Dashboard/Metrics | Progress widget values are in 0–100 range |
| D-005 | SOFT | STATIC | Dashboard/Metrics | Status widget colors are from allowed palette (green/red/yellow/gray/blue/orange/purple) |
| D-006 | SOFT | STATIC | Dashboard/Metrics | `metrics:` in `template.yaml` (if present) — all metric names match keys in `metrics.json` pattern |
| D-007 | SOFT | AI | Dashboard/Metrics | Metrics definitions reflect meaningful domain KPIs (not just generic "messages processed") |
| D-008 | INFO | STATIC | Dashboard/Metrics | Dashboard `refresh_interval` is >= 5 seconds |
| X-001 | SOFT | AI | Consistency | Agent name, `display_name`, and `description` tell a coherent story about the same agent |
| X-002 | SOFT | AI | Consistency | CLAUDE.md identity is consistent with `template.yaml` description and use cases |
| X-003 | SOFT | AI | Consistency | Skills/playbooks described in `template.yaml` match skills that actually exist in `.claude/skills/` |
| X-004 | SOFT | AI | Consistency | MCP servers listed in `template.yaml` match servers in `.mcp.json.template` |
| X-005 | SOFT | AI | Consistency | Credentials in `.env.example` are consistent with those documented in CLAUDE.md |
| X-006 | INFO | AI | Consistency | The agent's stated use cases are achievable given its declared tools and MCP servers |
| X-007 | SOFT | AI | Consistency | Scheduled task messages (cron prompts) align with skills that exist in this agent |
| X-008 | INFO | AI | Consistency | Resource allocation (`cpu`/`memory`) is appropriate for the agent's stated workload |
| I-001 | SOFT | AI | Composability | If the agent is callable by others (declares Trinity MCP or `permissions`), it documents its output format in `template.yaml` or `CLAUDE.md` |
| I-002 | SOFT | AI | Composability | Scheduled/autonomous tasks write structured output to a file or shared folder, not only as a chat response |
| I-003 | SOFT | AI | Composability | If the agent produces data for downstream consumers, an output schema or format is documented |
| I-004 | SOFT | AI | Composability | Agent has a clear "interface" — what goes in, what comes out — not just a description of what it does |
| I-005 | INFO | STATIC | Composability | `~/.trinity/post-check` exists if the agent declares output contracts (validates own output before delivery) |
| DP-001 | HARD | STATIC | Runtime Data | Every `data_paths` entry in `template.yaml` resolves under `data/` (relative to `/home/developer`) — no `../`, no absolute paths, no escape from the data root |
| DP-002 | HARD | STATIC | Runtime Data | If `data_paths` is declared, the `data/` root is excluded in `.gitignore` (Trinity appends it at creation; a template that ships `.gitignore` should pre-include it) |
| DP-003 | SOFT | STATIC | Runtime Data | `data_paths` entries do not overlap `.trinity/`, `.claude/`, `.env`, `.mcp.json`, `git.commit_paths`, or `persistent_state` (those are managed separately) |
| DP-004 | SOFT | STATIC | Runtime Data | If `data_paths` is declared, the agent is NOT replica-safe — its runtime data is instance-local and must travel via export/import, not template clone (feeds #927) |
| DP-005 | INFO | STATIC | Runtime Data | `~/.trinity/pre-snapshot` (if present) is executable and has a valid shebang (PR2 SQLite-quiesce hook; A-004 analog) |

---

## Detailed Check Definitions

### Category: File Structure

**F-001** — `template.yaml` exists  
Severity: HARD | Type: STATIC  
Check: File exists at agent root. Auto-fixable: No.

**F-002** — `CLAUDE.md` exists  
Severity: HARD | Type: STATIC  
Check: File exists at agent root. Without it, Claude Code has no instructions and the agent is effectively inert. Auto-fixable: No.

**F-003** — `.gitignore` exists  
Severity: SOFT | Type: STATIC  
Check: File exists at agent root. Missing `.gitignore` will cause secrets to be committed on first sync. Auto-fixable: Yes (generate canonical template).

**F-004** — `.env.example` exists  
Severity: SOFT | Type: STATIC  
Check: File exists. Without it, users have no way to know what credentials to inject. Auto-fixable: No.

**F-005** — `.mcp.json.template` exists when MCP servers are declared  
Severity: SOFT | Type: STATIC  
Check: If `template.yaml` has an `mcp_servers:` block, `.mcp.json.template` must exist. Without it, MCP tools will not be available at runtime. Auto-fixable: No.

**F-006** — `README.md` exists  
Severity: INFO | Type: STATIC  
Human-facing documentation. Not required for Trinity runtime, but expected for any published agent template.

**F-007** — `.trinity/setup.sh` exists if apt/npm-g installs are used in CLAUDE.md  
Severity: INFO | Type: AI+STATIC  
Check: If CLAUDE.md references system packages (ffmpeg, imagemagick, etc.) or global npm packages, a setup.sh must exist to persist them across container restarts.

**F-008** — `.claude/commands/` exists  
Severity: INFO | Type: STATIC  
At least one slash command is expected for any agent that has scheduled tasks.

**F-009** — At least one skill or command file exists  
Severity: INFO | Type: STATIC  
An agent with no skills or commands is unlikely to be useful autonomously.

**F-010** — `dashboard.yaml` exists  
Severity: SOFT | Type: STATIC  
Without a dashboard, the Trinity Dashboard tab shows nothing. Not required, but strongly recommended.

**F-011** — `ARCHITECTURE.md` (or `docs/architecture.md`) exists  
Severity: INFO | Type: STATIC  
Describes the agent's design, data flows, key components, and how it fits into a broader agentic system. Especially valuable for multi-agent or complex agents. CLAUDE.md can `@import` this file to keep the system prompt concise while giving Claude full architectural context.

**F-012** — `docs/memory/requirements.md` (or `REQUIREMENTS.md`) exists  
Severity: INFO | Type: STATIC  
Captures the agent's goals, use cases, and acceptance criteria in a durable document. Helps future maintainers understand scope and prevents feature drift. Recommended location: `docs/memory/requirements.md` to mirror the Trinity development convention.

**F-013** — `CHANGELOG.md` exists  
Severity: INFO | Type: STATIC  
Tracks changes between versions. Once an agent is published and iterated on, a changelog helps operators understand what changed and whether an upgrade is safe. Not required for initial versions but expected for any mature published template.

---

### Category: Security

**S-001** — `.env` excluded in `.gitignore`  
Severity: HARD | Type: STATIC  
Check: `.gitignore` contains `.env` or `.env.*` pattern. Auto-fixable: Yes (append pattern).

**S-002** — `.mcp.json` excluded in `.gitignore`  
Severity: HARD | Type: STATIC  
The generated `.mcp.json` contains live credentials injected by Trinity. Must never be committed. Auto-fixable: Yes.

**S-003** — No hardcoded secrets in committed files  
Severity: HARD | Type: STATIC  
Pattern scan across all committed files for: `sk-`, `ghp_`, `xoxb-`, `AIza`, `AKIA` prefixes; any key that matches `[A-Za-z_]+(KEY|SECRET|TOKEN|PASSWORD)\s*=\s*[^\$\{][^\s]{8,}`. Flags matches for human review. Auto-fixable: No.

**S-004** — `.claude/projects/` excluded  
Severity: HARD | Type: STATIC  
Contains Claude Code session history and JSONL files — never intended for git. Auto-fixable: Yes.

**S-005** — `.trinity/` excluded  
Severity: HARD | Type: STATIC  
Contains platform runtime state, operator queue files, and persistent-state config. Not part of the agent source. Auto-fixable: Yes.

**S-006** — Claude Code runtime dirs excluded  
Severity: SOFT | Type: STATIC  
Checks for `.claude/statsig/`, `.claude/todos/`, `.claude/debug/`, `.claude/sessions/`, `.claude/shell-snapshots/` in `.gitignore`. These are instance-local and not part of agent source. Auto-fixable: Yes.

**S-007** — `content/` excluded  
Severity: SOFT | Type: STATIC  
The `content/` directory is auto-created by the base image for large generated assets (video, audio). Committing it bloats the repository. Auto-fixable: Yes.

**S-008** — Wildcard secret file patterns in `.gitignore`  
Severity: SOFT | Type: STATIC  
Checks for `*.pem`, `*.key`, `credentials.json` patterns. Auto-fixable: Yes.

**S-009** — `.mcp.json.template` uses only `${VAR}` placeholders  
Severity: HARD | Type: STATIC  
Scans `.mcp.json.template` for any value that looks like a real credential (matching S-003 patterns). A template with literal API keys will leak secrets to anyone who clones the repo. Auto-fixable: No.

**S-010** — Credential variable names are service-specific  
Severity: SOFT | Type: STATIC  
Flags generic names: `API_KEY`, `SECRET`, `TOKEN`, `PASSWORD`, `KEY1`, `KEY2` (without a service prefix). Good: `TWITTER_API_KEY`, `OPENAI_API_KEY`. Bad: `API_KEY`. Auto-fixable: No.

---

### Category: template.yaml

**T-001** — Valid YAML syntax  
Severity: HARD | Type: STATIC  
Parse the file; any syntax error is a hard failure. Auto-fixable: No.

**T-002** — `name` field valid  
Severity: HARD | Type: STATIC  
Must match `/^[a-z0-9][a-z0-9\-]*$/`, max 64 chars. Used as Docker container name and internal identifier.

**T-003** — `description` present and non-empty  
Severity: HARD | Type: STATIC  
Required for template gallery display.

**T-004/T-005** — `resources.cpu` and `resources.memory` valid  
Severity: HARD | Type: STATIC  
CPU must be a numeric string ("1", "2", "4", "8", "16"). Memory must match `/^\d+[gm]$/` (e.g., "2g", "512m").

**T-006** — `display_name` present  
Severity: SOFT | Type: STATIC  
Without it, the UI falls back to `name` (lowercase, hyphens visible).

**T-007** — `version` present (semver)  
Severity: SOFT | Type: STATIC  
Must match `/^\d+\.\d+(\.\d+)?$/`. Enables upgrade tracking.

**T-008** — `author` present  
Severity: SOFT | Type: STATIC  
Required for template marketplace attribution.

**T-009** — `description` is substantive  
Severity: SOFT | Type: AI  
The description must explain what the agent does and for whom in at least 2 sentences. Evaluate: does it answer "what does this agent do?" and "who would use it?"

**T-010** — `use_cases` array with 3–7 entries  
Severity: SOFT | Type: STATIC  
Fewer than 3 gives users no guidance. More than 7 clutters the UI.

**T-011** — `capabilities` array present  
Severity: SOFT | Type: STATIC  
These appear as feature chips in the template gallery.

**T-012** — `mcp_servers` entries match `.mcp.json.template`  
Severity: SOFT | Type: STATIC  
Extract server names from `.mcp.json.template` `mcpServers` keys; verify all are listed in `template.yaml mcp_servers[].name`.

**T-013** — `use_cases` entries are realistic and specific  
Severity: SOFT | Type: AI  
Each use case should be a plausible user prompt, not a feature description. Bad: "Advanced analytics capabilities". Good: "Analyze our Q3 pipeline and flag deals at risk of slipping."

**T-014** — `tagline` conveys unique value  
Severity: SOFT | Type: AI  
If present, must not be generic ("AI-powered assistant", "Smart agent"). Should state what makes this agent distinctive in ≤60 chars.

**T-015** — All MCP credential variables listed in `credentials` schema  
Severity: SOFT | Type: STATIC  
Extract all `${VAR}` from `.mcp.json.template`; verify each appears in `template.yaml credentials`.

**T-016** — Schedule messages reference existing commands  
Severity: SOFT | Type: STATIC  
If `schedules[].message` starts with `/`, verify a corresponding file exists in `.claude/commands/`.

**T-017** — Commit paths don't overwrite Trinity-injected files  
Severity: HARD | Type: STATIC  
`git.commit_paths` must not include `.env`, `.mcp.json`, `.mcp.json.template`. These are managed by Trinity.

---

### Category: CLAUDE.md

**C-001** — Valid UTF-8, non-empty  
Severity: HARD | Type: STATIC  
File must be readable and contain meaningful content.

**C-002** — Has identity/purpose section  
Severity: HARD | Type: AI  
Prompt: "Does this CLAUDE.md contain a clear statement of who this agent is and what its primary purpose is? Answer YES or NO and explain."

**C-003** — Contains domain-specific instructions  
Severity: SOFT | Type: AI  
Prompt: "Does this CLAUDE.md contain instructions specific to this agent's domain (not generic Claude guidance anyone would already follow)? Examples of generic: 'be helpful', 'write clean code'. Examples of specific: step-by-step workflow for a business process, domain terminology, constraint unique to this agent's use case."

**C-004** — Lists available tools and integrations  
Severity: SOFT | Type: AI  
The agent should tell Claude what MCP servers and capabilities are available so it knows to use them.

**C-005** — Contains at least one concrete workflow  
Severity: SOFT | Type: AI  
At least one numbered or bulleted step-by-step procedure. An agent with only high-level instructions will produce inconsistent results.

**C-006** — Contains explicit constraints  
Severity: SOFT | Type: AI  
A constraints section limits scope creep and prevents the agent from doing things it shouldn't.

**C-007** — Under 2000 lines  
Severity: SOFT | Type: STATIC  
Claude's instruction-following degrades for content past ~2000 lines. Move reference material to separate files and `@import` them.

**C-008** — No generic Claude guidance  
Severity: SOFT | Type: AI  
Prompt: "Does this CLAUDE.md contain instructions that Claude already knows without being told (e.g., 'write clean code', 'be helpful', 'use best practices')? If so, list them. These waste context and should be removed."

**C-009** — Constraints are explicit and actionable  
Severity: SOFT | Type: AI  
Bad: "Be safe." Good: "Never send emails to external addresses. Only write files within /home/developer/outputs/. Do not access URLs outside *.company.com."

**C-010** — Critical rules are emphasized  
Severity: INFO | Type: AI  
Rules that must never be violated should use `IMPORTANT:`, `**bold**`, or similar emphasis to survive context compression.

**C-011** — No stale tool references  
Severity: INFO | Type: AI  
References to MCP tools or integrations not available in this agent's `.mcp.json.template` suggest the CLAUDE.md was cloned from another agent and not updated.

**C-012** — Coherent persona  
Severity: SOFT | Type: AI  
The agent's identity should feel like a consistent character: name (if any), tone, and area of expertise should align rather than contradict.

---

### Category: Credentials

**K-001/K-002** — All `${VAR}` placeholders documented  
Severity: HARD | Type: STATIC  
Extract all `${VAR_NAME}` references from `.mcp.json.template`. Verify each appears in `.env.example` AND in `template.yaml credentials`. Missing entries mean users can't know what credentials to provide. Auto-fixable: No.

**K-003** — `.env.example` entries have comments  
Severity: SOFT | Type: STATIC  
Each variable should have a `# comment` explaining what it is and where to get it.

**K-004** — `.env.example` uses placeholder values  
Severity: SOFT | Type: STATIC  
Values must look like placeholders (`your-api-key-here`, `PLACEHOLDER`). Flag any value that matches the secret patterns from S-003.

**K-005** — Credential naming convention  
Severity: SOFT | Type: AI  
Variable names should follow `SERVICE_FIELD` (e.g., `OPENAI_API_KEY`, `STRIPE_SECRET_KEY`). Ambiguous names like `API_KEY`, `SECRET`, `TOKEN` without a service prefix are flagged.

---

### Category: Git Configuration

**G-001** — `.claude/` not wholesale excluded  
Severity: HARD | Type: STATIC  
Check: `.gitignore` must NOT contain `.claude/` as a standalone pattern. The `.claude/commands/`, `.claude/skills/`, and `.claude/agents/` directories must be committed for Claude Code to work on Trinity. Auto-fixable: Yes (remove the overly broad exclusion, add specific subdirectory exclusions).

**G-002** — `.gitignore` follows canonical pattern list  
Severity: SOFT | Type: STATIC  
Compare against the canonical `_GITIGNORE_PATTERNS` list in `git_service.py`. Missing entries may cause secrets to be committed.

**G-003** — Commit paths don't include secrets or content  
Severity: SOFT | Type: STATIC  
If `git.commit_paths` is set in `template.yaml`, verify `.env`, `.mcp.json`, and `content/` are not included.

**G-004** — Ignore paths include credential files  
Severity: SOFT | Type: STATIC  
If `git.ignore_paths` is set, `.env` and `.mcp.json` must be present.

**G-005** — `git.push_enabled` explicitly declared  
Severity: INFO | Type: STATIC  
Leaving it to platform default makes the sync behavior implicit. Explicit declaration makes the agent's intent clear.

---

### Category: Skills and Playbooks

**P-001** — Each skill has valid YAML frontmatter  
Severity: SOFT | Type: STATIC  
SKILL.md frontmatter block (`---` ... `---`) must be valid YAML.

**P-002** — Frontmatter has `name` and `description`  
Severity: SOFT | Type: STATIC  
Both are required. `name` is the skill identifier; `description` is what Claude reads to decide when to invoke it.

**P-003** — Skill descriptions enable correct auto-invocation  
Severity: SOFT | Type: AI  
Prompt: "Will this skill description cause Claude to invoke this skill at the right times and not invoke it at wrong times? A good description says what the skill does AND gives trigger context. A bad description is too vague or too broad."

**P-004** — Skill files under 500 lines  
Severity: SOFT | Type: STATIC  
Beyond 500 lines, Claude's attention degrades. Move reference material to companion files.

**P-005** — Skills are domain-specific  
Severity: SOFT | Type: AI  
Skills should encode knowledge unique to this agent's domain. Skills that are generic development methodology (commit, review, test) likely belong in a separate plugin, not in this agent's skill library.

**P-006** — Autonomous skills have no approval gates  
Severity: HARD | Type: AI  
Scan scheduled/autonomous skills for: `[APPROVAL GATE]`, "wait for", "ask user", "confirm with", "present options to", "get user input". An approval gate in an autonomous playbook causes the scheduled execution to hang indefinitely. Flag any match. Auto-fixable: No.

**P-007** — Autonomous skills have error handling  
Severity: SOFT | Type: AI  
Autonomous skills should specify what to do on failure (log, notify via Slack/email, retry). Skills that don't handle errors will fail silently.

**P-008** — Scheduled skills are self-contained  
Severity: SOFT | Type: AI  
A skill triggered by a cron schedule must not require human input to complete. Review for implicit dependencies on a human being present.

**P-009** — Complex skills use multi-file layout  
Severity: INFO | Type: AI  
If a skill's SKILL.md exceeds 200 lines but contains detailed reference material, suggest splitting into SKILL.md + reference.md + examples.md.

**P-010** — Skills are idempotent  
Severity: SOFT | Type: AI  
Skills that run on a schedule should produce the same result if run multiple times. Non-idempotent skills should document this explicitly.

**P-011** — `allowed-tools` is appropriately scoped  
Severity: SOFT | Type: AI  
Read-only analysis skills should not request write-capable tools. Overly permissive `allowed-tools` increases blast radius.

**P-012** — Skills define expected output format  
Severity: INFO | Type: AI  
Skills with structured output (reports, JSON, tables) should specify the expected format to ensure consistency across scheduled runs.

---

### Category: Autonomy Design

**A-001** — Scheduled messages use slash commands  
Severity: SOFT | Type: AI  
`template.yaml schedules[].message` should start with `/` referencing a `.claude/commands/` file, not be a raw prose prompt. Raw prompts produce inconsistent autonomous behavior.

**A-002** — Cron expressions are valid  
Severity: SOFT | Type: STATIC  
Validate against standard 5-field cron syntax. Flag invalid expressions.

**A-003** — Agent has a clear autonomy model  
Severity: SOFT | Type: AI  
The agent should be clearly one of: (a) interactive-only, (b) autonomous-only, (c) hybrid with clear mode separation. An agent that mixes assumptions about user presence is likely to behave inconsistently.

**A-004** — `.trinity/pre-check` is executable with a shebang  
Severity: INFO | Type: STATIC  
If present, verify the file has a `#!` shebang on line 1. Without it, `docker exec` will fail to run it.

**A-005** — Scheduled task prompts describe expected output  
Severity: INFO | Type: AI  
A schedule that says "Run the weekly report" is better than one that says "Do the thing". Specific output expectations help Claude produce consistent results.

---

### Category: Dashboard and Metrics

**D-001** — `dashboard.yaml` is valid YAML  
Severity: SOFT | Type: STATIC  
Parse the file; syntax errors prevent the dashboard from rendering.

**D-002** — All widget types are supported  
Severity: SOFT | Type: STATIC  
Allowed types: `metric`, `status`, `progress`, `text`, `markdown`, `table`, `list`, `link`, `image`, `divider`, `spacer`. Unknown types are silently ignored by the UI.

**D-003** — Widget required fields present  
Severity: HARD | Type: STATIC  
Common mistakes that break rendering:
- `text` widget: must use `content` (not `text`, `value`, or `label`)
- `list` widget: must use `items` (not `values`, `list`, or `content`)
- `link` widget: must use `url` (not `href` or `link`)
- `metric` widget: must have `label` and `value`
- `status` widget: must have `label`, `value`, and `color`
- `progress` widget: must have `label` and `value`

**D-004** — Progress values in range  
Severity: SOFT | Type: STATIC  
Values for `progress` widgets must be 0–100. Values outside this range are clamped but indicate a calculation error.

**D-005** — Status colors from allowed palette  
Severity: SOFT | Type: STATIC  
Only `green`, `red`, `yellow`, `gray`, `blue`, `orange`, `purple` are rendered correctly.

**D-006** — Metric names consistent with metrics.json pattern  
Severity: SOFT | Type: STATIC  
If `template.yaml` declares metrics, the `name` fields should be valid JSON keys (no spaces, no special characters).

**D-007** — Metrics reflect meaningful KPIs  
Severity: SOFT | Type: AI  
Prompt: "Are these metrics meaningful domain KPIs, or are they generic vanity metrics? A meaningful metric tells the operator something actionable about agent health or output quality."

**D-008** — Dashboard refresh interval >= 5s  
Severity: INFO | Type: STATIC  
Faster refresh rates put unnecessary load on the agent container.

---

### Category: Cross-File Consistency

**X-001** — Name, display_name, description tell a coherent story  
Severity: SOFT | Type: AI  
All three should clearly refer to the same agent and the same purpose. Discrepancies suggest the agent was cloned and partially updated.

**X-002** — CLAUDE.md identity consistent with template.yaml  
Severity: SOFT | Type: AI  
The agent's self-description in CLAUDE.md should match what's promised in template.yaml. A mismatch means users get different behavior than they were promised by the template.

**X-003** — Declared skills exist in `.claude/skills/`  
Severity: SOFT | Type: STATIC  
If `template.yaml` lists `skills:`, verify each has a corresponding SKILL.md file.

**X-004** — MCP servers consistent across files  
Severity: SOFT | Type: STATIC  
Server names in `template.yaml mcp_servers[]` must match keys in `.mcp.json.template mcpServers{}`. Mismatches mean the UI shows capabilities the agent can't actually use.

**X-005** — `.env.example` and CLAUDE.md credential references consistent  
Severity: SOFT | Type: AI  
If CLAUDE.md references specific APIs or services, the corresponding credentials should exist in `.env.example`.

**X-006** — Use cases achievable with declared tools  
Severity: INFO | Type: AI  
Prompt: "Given the MCP servers and tools declared in template.yaml, are the stated use_cases actually achievable? Flag any use case that requires a tool or integration not listed."

**X-007** — Scheduled messages match existing skills  
Severity: SOFT | Type: STATIC + AI  
If `schedules[].message` is `/some-command`, verify `.claude/commands/some-command.md` exists. Additionally (AI): verify the command's purpose aligns with the schedule's declared intent.

**X-008** — Resource allocation appropriate for workload  
Severity: INFO | Type: AI  
Prompt: "Given this agent's stated purpose and use cases, is the resource allocation (cpu: X, memory: Yg) appropriate? Flag obvious mismatches: a video-processing agent with 512m memory, or a simple Q&A agent over-provisioned with 16 CPUs."

---

### Category: Composability

These checks evaluate whether an agent is designed to participate in a multi-agent system reliably. The guiding principle: agents should exchange data (structured files, queues, typed outputs) rather than chain conversations. An agent with no declared output contract is a black box to any system that depends on it.

**I-001** — Callable agents declare their output format  
Severity: SOFT | Type: AI  
If the agent's `template.yaml` or `CLAUDE.md` indicates it is intended to be called by other agents (references Trinity MCP, `agent_permissions`, or describes itself as a "worker" or "specialist"), it must document what format its responses take. Prompt: "Does this agent document what format or schema callers should expect in its output? A passing answer includes an explicit output format, schema reference, or structured example. A failing answer describes only what the agent *does*, not what it *returns*."

**I-002** — Scheduled tasks produce structured, consumable output  
Severity: SOFT | Type: AI  
Autonomous tasks that feed downstream agents or systems should write structured output (JSON file, CSV, markdown report to a known path, shared folder write) rather than relying solely on the chat response text. Prompt: "Do this agent's scheduled tasks or autonomous skills produce output in a structured, file-based form that another agent or system could consume without parsing a conversation? Flag skills that only produce chat responses with no file or structured output."

**I-003** — Output schema documented for data-producing agents  
Severity: SOFT | Type: AI  
If the agent's purpose includes producing reports, datasets, or structured content for downstream use, the output schema or format must be documented somewhere in the agent (CLAUDE.md, a `schemas/` directory, or `template.yaml`). Prompt: "If this agent produces structured data or reports intended for downstream consumption, is the output schema or format documented? Answer YES/NO and cite where."

**I-004** — Agent has a clear interface declaration  
Severity: SOFT | Type: AI  
An agent designed for composition should explicitly state what it accepts as input and what it produces as output — not just its general purpose. Prompt: "Does this agent clearly document its 'interface': what input it expects (message format, required context, parameters) and what output callers will receive? This is separate from what the agent does — it's about the contract at the boundary. Answer YES/NO."

**I-005** — `post-check` hook exists when output contracts are declared  
Severity: INFO | Type: STATIC  
If the agent documents an output format or schema (detected by presence of `schemas/` directory, `output_format:` key in `template.yaml`, or `output contract` / `output format` in CLAUDE.md), a `~/.trinity/post-check` hook should exist to validate outputs before delivery. Without it, the declared contract is aspirational rather than enforced.

---

### Category: Runtime Data Paths (#1169)

`data_paths` in `template.yaml` declares the agent's runtime data (SQLite DBs, datasets) that live under `/home/developer/data` on the already-durable home volume — kept out of git, but exportable/importable for portability. These checks fire only when `data_paths` is declared (it is opt-in).

**DP-001** — `data_paths` entries resolve under `data/`  
Severity: HARD | Type: STATIC  
Each entry, after normalization, must stay under the `data/` root (relative to `/home/developer`). Reject absolute paths, `../` traversal, or any entry that escapes the data root. The export/import primitive only captures and restores `data/**`, so an out-of-root declaration is silently never snapshotted.

**DP-002** — `data/` root is gitignored  
Severity: HARD | Type: STATIC  
Trinity appends `data/` (and each declared entry) to the agent's `.gitignore` at creation, but a template shipping its own `.gitignore` should pre-include `data/`. Without it, runtime data risks being committed on the first auto-sync.

**DP-003** — `data_paths` don't overlap managed paths  
Severity: SOFT | Type: STATIC  
Entries must not overlap `.trinity/`, `.claude/`, `.env`, `.mcp.json`, `git.commit_paths`, or `persistent_state`. Those surfaces are materialized and managed separately; overlapping declarations create ambiguous ownership and double-handling.

**DP-004** — `data_paths` ⇒ not replica-safe  
Severity: SOFT | Type: STATIC  
A `data_paths` declaration means the agent carries instance-local runtime state. Such an agent cannot be replicated by cloning its template alone — its data must travel via `data/export` → `data/import`. Flag for replica-safety tooling (#927).

**DP-005** — `.trinity/pre-snapshot` is executable with a shebang  
Severity: INFO | Type: STATIC  
If present (the PR2 SQLite-quiesce hook that stages a consistent `.backup` copy before snapshot), verify a `#!` shebang on line 1 — `docker exec` fails to run it otherwise. Analogous to A-004 for `pre-check`.

---

## Severity Summary

| Severity | Meaning | Effect on Deployment |
|----------|---------|---------------------|
| **HARD** | Will break Trinity at runtime | Deployment proceeds, prominent warning shown |
| **SOFT** | Best practice; agent may behave incorrectly | Yellow recommendation in Info tab |
| **INFO** | Improvement suggestion | Gray suggestion in Info tab |

No severity level blocks deployment. All checks are informational.

---

## Auto-Fixable Checks

The following checks can be resolved automatically via `POST /api/agents/{name}/compatibility/fix`:

| Check ID | Auto-Fix Action |
|----------|----------------|
| F-003 | Generate canonical `.gitignore` from template |
| S-001 | Append `.env` to `.gitignore` |
| S-002 | Append `.mcp.json` to `.gitignore` |
| S-004 | Append `.claude/projects/` to `.gitignore` |
| S-005 | Append `.trinity/` to `.gitignore` |
| S-006 | Append Claude Code runtime dirs to `.gitignore` |
| S-007 | Append `content/` to `.gitignore` |
| S-008 | Append `*.pem`, `*.key`, `credentials.json` to `.gitignore` |
| G-001 | Remove `.claude/` blanket exclusion; add specific subdirectory exclusions |
| G-002 | Append missing canonical patterns to `.gitignore` |

All other checks require manual intervention.

---

## Implementation Notes

- Checks in this spec map to the validation service package at `src/backend/services/compatibility/` (`spec.py` is the single source of truth; `collector.py`, `static_checks.py`, `ai_checks.py`, `fixes.py`) — issue #668.
- AI-evaluated checks call the Claude API (`claude-haiku-4-5`) with the relevant file contents, batched by category; results include a confidence score and explanation.
- The full check list is versioned here; bump this file when adding/removing checks. A unit test (`tests/unit/test_compatibility_checks.py::TestSpecDocSync`) asserts the check-id set here matches `spec.py` exactly, so the two can't drift.
- Check IDs are stable — do not renumber existing checks; append new ones.

### Implementation deviations (#668)

The shipped validator differs from the `Type` column above in a few principled,
test-locked ways:

- **AI severity is capped at SOFT.** An LLM verdict is non-deterministic, so an
  AI check never drives the HARD count. The catalog keeps each check's declared
  severity; the report downgrades any `AI` + `HARD` check (only **C-002**) to
  SOFT at emit time. HARD remains reserved for deterministic STATIC checks.
- **P-006 is implemented STATIC** (doc marks it AI). The check has literal
  approval-gate patterns to scan and is HARD, so it must not depend on an
  optional API key; it scans the command files referenced by `template.yaml`
  schedules (the actual autonomous path).
- **F-007, A-001, X-007 are implemented STATIC** (doc marks them AI or hybrid).
  The determinable signal is a deterministic file/pattern check (system-package
  references; schedule message starts with `/`; scheduled command exists).
- **Runtime-aware.** Claude-specific checks (`CLAUDE.md` content, `.claude/`
  skills/commands) are **omitted** for non-Claude runtimes (Codex/Gemini, #1187)
  so those agents aren't flagged with false HARDs.
- **Persistence (departs from the issue's "no DB table" note).** The latest
  report per agent is persisted in `agent_compatibility_results` (one row,
  upserted) so AI verdicts show on every Overview load without re-spending
  tokens, and the fleet can aggregate "N agents have HARD findings". STATIC
  checks recompute live on each read; persisted AI verdicts are merged in until
  a re-run.
