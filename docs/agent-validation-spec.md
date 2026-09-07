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
| F-004 | SOFT | STATIC | File Structure | `.env.example` exists (only when the agent declares credentials — K-001 owns the HARD case) |
| F-005 | SOFT | STATIC | File Structure | `.mcp.json.template` exists (if MCP servers declared) |
| F-006 | INFO | STATIC | File Structure | `README.md` exists |
| F-007 | INFO | STATIC | File Structure | `.trinity/setup.sh` exists (if system packages are needed) |
| F-009 | INFO | STATIC | File Structure | At least one `.claude/skills/` or `.claude/commands/` file exists |
| F-010 | SOFT | STATIC | File Structure | `dashboard.yaml` exists |
| F-011 | INFO | STATIC | File Structure | `ARCHITECTURE.md` (or `docs/architecture.md`) exists |
| S-001 | HARD | STATIC | Security | `.env` is excluded in `.gitignore` |
| S-002 | HARD | STATIC | Security | `.mcp.json` is excluded in `.gitignore` |
| S-003 | HARD | STATIC | Security | No hardcoded secrets in any committed file |
| S-004 | HARD | STATIC | Security | `.claude/projects/` is excluded in `.gitignore` |
| S-005 | HARD | STATIC | Security | `.trinity/` runtime state is excluded in `.gitignore` (canonical shape is `.trinity/*` plus `!` re-includes for the authored hooks — see #2070; the dir-forms are still accepted) |
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
| T-007 | INFO | STATIC | template.yaml | `version` field present (semantic version format) |
| T-008 | INFO | STATIC | template.yaml | `author` field present |
| T-009 | SOFT | AI | template.yaml | `description` is substantive (2+ sentences, explains purpose clearly) |
| T-010 | INFO | STATIC | template.yaml | `use_cases` array present with 3–7 examples |
| T-011 | INFO | STATIC | template.yaml | `capabilities` array present |
| T-013 | SOFT | AI | template.yaml | `use_cases` entries are realistic, specific, actionable prompts (not buzzword lists) |
| T-014 | SOFT | AI | template.yaml | `tagline` (if present) is concise and explains unique value (not generic "AI assistant") |
| T-015 | HARD | STATIC | template.yaml | `credentials` schema lists all variables referenced in `.mcp.json.template` |
| T-018 | SOFT | STATIC | template.yaml | `schedules` block entries are well-formed (structure only — A-002 owns cron syntax) |
| C-001 | HARD | STATIC | CLAUDE.md | Valid UTF-8 markdown, non-empty |
| C-002 | HARD | AI | CLAUDE.md | Has an identity/purpose section (who the agent is and what it does) |
| C-003 | SOFT | AI | CLAUDE.md | Contains domain-specific instructions (not just generic Claude guidance) |
| C-004 | SOFT | AI | CLAUDE.md | Lists available tools and MCP integrations |
| C-005 | SOFT | AI | CLAUDE.md | Contains at least one concrete workflow or step-by-step procedure |
| C-006 | SOFT | AI | CLAUDE.md | Contains explicit, **actionable** constraints or guardrails (absorbs retired C-009) |
| C-007 | SOFT | STATIC | CLAUDE.md | Under 2000 lines (beyond this, Claude ignores trailing instructions) |
| C-008 | SOFT | AI | CLAUDE.md | Does not repeat standard Claude knowledge (generic best practices, library docs) |
| C-010 | INFO | AI | CLAUDE.md | Critical rules are emphasized (uses IMPORTANT:, **bold**, or similar) |
| C-011 | INFO | AI | CLAUDE.md | No stale references to tools/services not available in this agent |
| C-012 | INFO | AI | CLAUDE.md | Identity section conveys a coherent persona aligned with the agent's purpose |
| K-001 | HARD | STATIC | Credentials | Every `${VAR}` in `.mcp.json.template` has a corresponding entry in `.env.example` |
| K-003 | INFO | STATIC | Credentials | `.env.example` comments explain what each variable is for |
| K-004 | SOFT | STATIC | Credentials | `.env.example` uses placeholder values (not empty or real values) |
| G-001 | HARD | STATIC | Git Config | `.claude/` is NOT excluded from `.gitignore` wholesale (must stay committed for Claude Code) |
| P-001 | SOFT | STATIC | Skills/Playbooks | Each skill file has valid YAML frontmatter |
| P-002 | SOFT | STATIC | Skills/Playbooks | Each skill frontmatter has `name` and `description` fields |
| P-003 | SOFT | AI | Skills/Playbooks | Skill `description` is specific enough to trigger correct auto-invocation (not vague) |
| P-004 | SOFT | STATIC | Skills/Playbooks | Each `SKILL.md` is under 500 lines (companion `reference.md`/`examples.md` are exempt — they are P-009's output) |
| P-005 | SOFT | AI | Skills/Playbooks | Skills are domain-specific to this agent's purpose (not generic dev methodology) |
| P-006 | HARD | STATIC | Skills/Playbooks | Autonomous/scheduled skills contain NO approval gates, unless frontmatter declares `automation: gated` or `manual` |
| P-007 | SOFT | AI | Skills/Playbooks | Autonomous skills include error handling and notification on failure |
| P-008 | SOFT | AI | Skills/Playbooks | Playbooks that run on schedule are self-contained (no required user input) |
| P-009 | INFO | AI | Skills/Playbooks | Complex skills use multi-file layout (SKILL.md + reference.md / examples.md) |
| P-010 | SOFT | AI | Skills/Playbooks | Skills are idempotent or clearly document that they are not |
| P-011 | SOFT | AI | Skills/Playbooks | `allowed-tools` is scoped appropriately (read-only skills don't request write tools) |
| P-012 | INFO | AI | Skills/Playbooks | Skills include a completion checklist or explicit output format |
| A-001 | INFO | STATIC | Autonomy Design | Scheduled messages name a slash command *anywhere* in the message (prose is valid; this is a determinism suggestion) |
| A-002 | SOFT | STATIC | Autonomy Design | Cron expressions in `template.yaml` schedules are valid cron syntax |
| A-003 | SOFT | AI | Autonomy Design | Agent has a clear autonomy model: either interactive or autonomous, not ambiguous |
| A-004 | INFO | STATIC | Autonomy Design | `.trinity/pre-check` (if present) is executable and has a valid shebang |
| A-005 | INFO | AI | Autonomy Design | Scheduled task descriptions are specific about expected output format |
| D-001 | SOFT | STATIC | Dashboard/Metrics | `dashboard.yaml` (if present) is valid YAML |
| D-002 | SOFT | STATIC | Dashboard/Metrics | All widget types are from the supported list |
| D-003 | HARD | STATIC | Dashboard/Metrics | Widget required fields are present (e.g., `content` not `text` for text widgets, `items` not `values` for list widgets, `url` not `href` for link widgets) |
| D-004 | SOFT | STATIC | Dashboard/Metrics | Progress widget values are in 0–100 range |
| D-005 | SOFT | STATIC | Dashboard/Metrics | Status widget colors are from allowed palette (green/red/yellow/gray/blue/orange/purple) |
| D-007 | SOFT | AI | Dashboard/Metrics | Metrics definitions reflect meaningful domain KPIs (not just generic "messages processed") |
| D-008 | INFO | STATIC | Dashboard/Metrics | Dashboard `refresh_interval` is >= 5 seconds |
| X-001 | SOFT | AI | Consistency | Agent name, `display_name`, and `description` tell a coherent story about the same agent |
| X-002 | SOFT | AI | Consistency | CLAUDE.md identity is consistent with `template.yaml` description and use cases |
| X-003 | SOFT | AI | Consistency | Skills/playbooks described in `template.yaml` match skills that actually exist in `.claude/skills/` |
| X-004 | SOFT | AI | Consistency | MCP servers listed in `template.yaml` match servers in `.mcp.json.template` |
| X-005 | SOFT | AI | Consistency | Credentials in `.env.example` are consistent with those documented in CLAUDE.md |
| X-006 | INFO | AI | Consistency | The agent's stated use cases are achievable given its declared tools and MCP servers |
| X-007 | SOFT | STATIC | Consistency | Scheduled messages resolve to an existing `.claude/skills/<name>/SKILL.md` **or** `.claude/commands/<name>.md` |
| X-008 | INFO | AI | Consistency | Resource allocation (`cpu`/`memory`) is appropriate for the agent's stated workload |
| I-001 | SOFT | AI | Composability | If the agent is callable by others (declares Trinity MCP or `permissions`), it documents its output format in `template.yaml` or `CLAUDE.md` |
| I-002 | SOFT | AI | Composability | Scheduled/autonomous tasks write structured output to a file or shared folder, not only as a chat response |
| I-006 | INFO | STATIC | Composability | The Trinity plugin (`trinity@abilityai`) is installed, so the agent can run `/trinity:onboard` in place and make itself compatible without a human checkout |
| DP-001 | HARD | STATIC | Runtime Data | Every `data_paths` entry in `template.yaml` resolves under `data/` (relative to `/home/developer`) — no `../`, no absolute paths, no escape from the data root |
| DP-002 | SOFT | STATIC | Runtime Data | If `data_paths` is declared, the `data/` root is excluded in `.gitignore` (Trinity appends it at creation; a template that ships `.gitignore` should pre-include it) |
| DP-003 | SOFT | STATIC | Runtime Data | `data_paths` entries do not overlap `.trinity/`, `.claude/`, `.env`, `.mcp.json`, `git.commit_paths`, or `persistent_state` (those are managed separately) |
| DP-004 | INFO | STATIC | Runtime Data | If `data_paths` is declared, the agent is NOT replica-safe — its runtime data is instance-local and must travel via export/import, not template clone (feeds #927) |

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

**F-004** — `.env.example` exists when credentials are declared  
Severity: SOFT | Type: STATIC  
Check: File exists — but only demanded when the agent actually declares credentials (a `${VAR}` in `.mcp.json.template`, or a `credentials:`/`mcp_servers:` block in `template.yaml`). A credential-free agent is PASSed: an `.env.example` there would document nothing, and unconditionally requiring it SOFT-failed every such agent for an unactionable reason (#2137). K-001 still owns the case that matters at HARD — a `${VAR}` with no matching `.env.example` entry. Auto-fixable: No.

**F-005** — `.mcp.json.template` exists when MCP servers are declared  
Severity: SOFT | Type: STATIC  
Check: If `template.yaml` has an `mcp_servers:` block, `.mcp.json.template` must exist. Without it, MCP tools will not be available at runtime. Auto-fixable: No.

**F-006** — `README.md` exists  
Severity: INFO | Type: STATIC  
Human-facing documentation. Not required for Trinity runtime, but expected for any published agent template.

**F-007** — `.trinity/setup.sh` exists if apt/npm-g installs are used in CLAUDE.md  
Severity: INFO | Type: AI+STATIC  
Check: If CLAUDE.md references system packages (ffmpeg, imagemagick, etc.) or global npm packages, a setup.sh must exist to persist them across container restarts.

**F-009** — At least one skill or command file exists  
Severity: INFO | Type: STATIC  
An agent with no skills or commands is unlikely to be useful autonomously.

**F-010** — `dashboard.yaml` exists  
Severity: SOFT | Type: STATIC  
Without a dashboard, the Trinity Dashboard tab shows nothing. Not required, but strongly recommended.

**F-011** — `ARCHITECTURE.md` (or `docs/architecture.md`) exists  
Severity: INFO | Type: STATIC  
Describes the agent's design, data flows, key components, and how it fits into a broader agentic system. Especially valuable for multi-agent or complex agents. CLAUDE.md can `@import` this file to keep the system prompt concise while giving Claude full architectural context.

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
Severity: INFO | Type: STATIC  
Must match `/^\d+\.\d+(\.\d+)?$/`. Enables upgrade tracking.

**T-008** — `author` present  
Severity: INFO | Type: STATIC  
Required for template marketplace attribution.

**T-009** — `description` is substantive  
Severity: SOFT | Type: AI  
The description must explain what the agent does and for whom in at least 2 sentences. Evaluate: does it answer "what does this agent do?" and "who would use it?"

**T-010** — `use_cases` array with 3–7 entries  
Severity: INFO | Type: STATIC  
Fewer than 3 gives users no guidance. More than 7 clutters the UI.

**T-011** — `capabilities` array present  
Severity: INFO | Type: STATIC  
These appear as feature chips in the template gallery.

**T-013** — `use_cases` entries are realistic and specific  
Severity: SOFT | Type: AI  
Each use case should be a plausible user prompt, not a feature description. Bad: "Advanced analytics capabilities". Good: "Analyze our Q3 pipeline and flag deals at risk of slipping." **PASSes when `use_cases` is absent** (#2137), mirroring T-014's "PASS if absent; FAIL only if present and generic" — T-010 already owns presence, at INFO.

**T-014** — `tagline` conveys unique value  
Severity: SOFT | Type: AI  
If present, must not be generic ("AI-powered assistant", "Smart agent"). Should state what makes this agent distinctive in ≤60 chars.

**T-015** — All MCP credential variables listed in `credentials` schema  
Severity: HARD | Type: STATIC  
Extract all `${VAR}` from `.mcp.json.template`; verify each appears in `template.yaml credentials`. **Promoted SOFT → HARD in #2137** when its duplicate K-002 was retired: K-002 was declared HARD and its body was literally `return c_t015(snap)`, so the two shared logic but *not* severity. Retiring it as a plain duplicate would have silently downgraded the credential-declaration gate and undone the deliberate ent#128 choice that a hostile `credentials:` declaration must reach `hard_count`. An undeclared `${VAR}` is author-fixable and breaks the agent at runtime — HARD, on one check instead of two.

**T-018** — `schedules` block entries are well-formed  
Severity: SOFT | Type: STATIC  
Trinity materializes a template's declared `schedules:` at agent creation
(trinity-enterprise#89), so a malformed block silently costs the agent its recurring tasks.
Validates **structure**: the block is a list; each entry is a mapping carrying non-empty string
`name`, `cron` and `message`; `timezone`/`description` are strings when present; `name` and
`description` are within their length bounds; names are unique within the block; and the block is
within the 20-entry cap. Shares one reader — `services/template_schedules.py` — with the
materializer and the catalog surface, so the report cannot drift from what creation actually does.

**Cron syntax is deliberately NOT reported here — A-002 owns it.** Two checks disagreeing about
the same field is worse than either alone. (The reader *does* validate cron strictly, but that is
a materialization gate — drop the entry — not a report verdict.)

SOFT, not HARD: a malformed block costs the agent its declared schedules, not its usability.

**Fails closed.** Unlike every other check, T-018 catches its own exception and returns `fail`.
`run_static` converts a raise into `skipped`, and the report counts only `fail` — so a raising
SOFT check drops `soft_count` 1→0 and flips `overall_status` from `issues` to `compatible`
precisely when this check's finding was the only failure, then persists that clean bill of health
into every degraded report. A check whose whole purpose is malformed-input tolerance must not rely
on the fail-open outer net.

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

**C-006** — Contains explicit, actionable constraints  
Severity: SOFT | Type: AI  
A constraints section limits scope creep and prevents the agent from doing things it shouldn't — and those constraints must be actionable ("never email external addresses"), not vague ("be safe"). Absorbs retired C-009 (#2137): asking "are there constraints?" and "are the constraints actionable?" as two AI checks charged two LLM verdicts and produced two findings for one edit.

**C-007** — Under 2000 lines  
Severity: SOFT | Type: STATIC  
Claude's instruction-following degrades for content past ~2000 lines. Move reference material to separate files and `@import` them.

**C-008** — No generic Claude guidance  
Severity: SOFT | Type: AI  
Prompt: "Does this CLAUDE.md contain instructions that Claude already knows without being told (e.g., 'write clean code', 'be helpful', 'use best practices')? If so, list them. These waste context and should be removed."

**C-010** — Critical rules are emphasized  
Severity: INFO | Type: AI  
Rules that must never be violated should use `IMPORTANT:`, `**bold**`, or similar emphasis to survive context compression.

**C-011** — No stale tool references  
Severity: INFO | Type: AI  
References to MCP tools or integrations not available in this agent's `.mcp.json.template` suggest the CLAUDE.md was cloned from another agent and not updated.

**C-012** — Coherent persona  
Severity: INFO | Type: AI  
The agent's identity should feel like a consistent character: name (if any), tone, and area of expertise should align rather than contradict.

---

### Category: Credentials

**K-001** — All `${VAR}` placeholders documented in `.env.example`  
Severity: HARD | Type: STATIC  
Extract all `${VAR_NAME}` references from `.mcp.json.template`; verify each appears in `.env.example`. Missing entries mean users can't know what credentials to provide. The `template.yaml credentials` half of this pair is **T-015** — it was duplicated as K-002 until #2137. Auto-fixable: No.

**K-003** — `.env.example` entries have comments  
Severity: INFO | Type: STATIC  
Each variable should have a `# comment` explaining what it is and where to get it.

**K-004** — `.env.example` uses placeholder values  
Severity: SOFT | Type: STATIC  
Values must look like placeholders (`your-api-key-here`, `PLACEHOLDER`). Flag any value that matches the secret patterns from S-003.

---

### Category: Git Configuration

**G-001** — `.claude/` not wholesale excluded  
Severity: HARD | Type: STATIC  
Check: `.gitignore` must NOT contain `.claude/` as a standalone pattern. The `.claude/commands/`, `.claude/skills/`, and `.claude/agents/` directories must be committed for Claude Code to work on Trinity. Auto-fixable: Yes (remove the overly broad exclusion, add specific subdirectory exclusions).

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

**P-004** — `SKILL.md` files under 500 lines  
Severity: SOFT | Type: STATIC  
Beyond 500 lines, Claude's attention degrades. Move reference material to companion files. **Scoped to `SKILL.md` and `.claude/commands/*.md` only** (#2137) — it previously walked every `.md` under `.claude/skills/`, so it flagged the very `reference.md`/`examples.md` companions P-009 tells authors to create. With the scope fixed the two form a ladder: P-009 suggests splitting past ~200 lines, P-004 fails past 500.

**P-005** — Skills are domain-specific  
Severity: SOFT | Type: AI  
Skills should encode knowledge unique to this agent's domain. Skills that are generic development methodology (commit, review, test) likely belong in a separate plugin, not in this agent's skill library.

**P-006** — Autonomous skills have no approval gates  
Severity: HARD | Type: STATIC  
Scan scheduled/autonomous skills for: `[APPROVAL GATE]`, "wait for", "ask user", "confirm with", "present options to", "get user input". An approval gate in an autonomous playbook causes the scheduled execution to hang indefinitely. Flag any match. Auto-fixable: No.

Two #2137 corrections. (1) The check resolves its target skills through `_slash_command()`, which anchored at position 0 (`^\s*/`) — so the marketplace's own generated schedules (`"Run /pipeline-tick"`, `"Run /project-steward"`) matched nothing, the scheduled-command set came back empty, and this HARD check returned "no scheduled/autonomous skills declared" on exactly the agents it exists to guard. It has been inert since it shipped. The matcher now finds a slash command anywhere a token starts, with two guards so a filesystem path is never read as one: the `/` must begin a token, and the token must not be followed by another `/` (a command is one segment). `"Check /var/log/app.log for errors"` therefore yields no command — X-007 turns a resolved name into a SOFT finding, so a false positive there would manufacture the exact unactionable failure this pass removed. (2) A skill may **opt out** by declaring `automation: gated` or `automation: manual` in its YAML frontmatter (the convention already used across the `abilityai/abilities` marketplace). An intentional human pause is a design choice, not a defect; only the default (frontmatter absent, or `automation: autonomous`) is held to "must not hang".

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

**A-001** — Scheduled messages name a slash command  
Severity: INFO | Type: STATIC  
`template.yaml schedules[].message` naming a `/skill` dispatches more deterministically than a prose prompt. **INFO, not SOFT** (#2137): prose messages are entirely valid Trinity input — a schedule message is a chat prompt — and the `create-agent` wizards deliberately generate prose for all 13 of their scheduled tasks, so a SOFT here failed every wizard-scaffolded agent for a decision its author never made. The slash command may appear **anywhere** in the message, not only at position 0.

**A-002** — Cron expressions are valid  
Severity: SOFT | Type: STATIC  
Validated with `services/schedule_validation.validate_cron_expression` — the **same** parser the
dedicated scheduler registers the job with (#1472): exactly 5 fields, Unix→APScheduler day-name
translation, real range checking, and the declared `timezone`. Flags anything `_add_job` would
reject. Until trinity-enterprise#89 this was a per-field `^[\d*/,\-]+$` regex that was wrong in
both directions — it rejected `0 9 * * MON` and accepted `99 99 * * *`. A-002 is the single cron
authority in this catalog; T-018 reports the `schedules:` block's structure only.

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
Allowed types: `metric`, `status`, `progress`, `text`, `markdown`, `table`, `list`, `link`, `image`, `divider`, `spacer`. An unknown type is never rendered: the agent server strips the widget before the dashboard reaches the UI and lists it in the Dashboard tab's warning banner (a file with no `sections:` is rejected whole), and D-002 names each offending type with its count, e.g. `unsupported dashboard widget type(s): 'chart' ×5 — not rendered; supported: …`. The list above is closed — there is no chart, badge, or countdown type; trend lines come from the platform's metric history (DASH-001), not from a widget.

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
Severity: SOFT | Type: STATIC  
If `schedules[].message` names `/some-command`, verify a target exists. **Both layouts resolve** (#2137): `.claude/commands/some-command.md` *or* `.claude/skills/some-command/SKILL.md`. The name resolver previously globbed only `.claude/commands/`, which is the one layout the `create-agent` wizards never produce — so a skill-based agent naming a real skill was reported as referencing a missing command.

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

**I-006** — Trinity plugin present (deploy-as-is bootstrap)  
Severity: INFO | Type: STATIC  
Reports whether `trinity@abilityai` is installed in the agent's container, from `.trinity/plugins-state.json` (written by the boot reconciler, ent#411). The plugin is what lets a **deployed** agent make itself compatible — `/trinity:onboard` run in place writes `template.yaml`, `.env.example`, `.gitignore` and `.mcp.json.template` and pushes them back — so its absence is the difference between an agent that can fix its own findings and one that needs a human with a local checkout. INFO, never a defect tier: an operator may switch platform plugins off (`TRINITY_PLATFORM_PLUGINS=0`), and a bare repo is not at fault for an install the platform failed to make. When the plugin is missing the check reports the reconciler's own withheld reason, so "the marketplace was unreachable" is distinguishable from "it was never wanted" — a bare presence flag cannot tell those apart. A missing state file is a SKIP (image or boot predates the mechanism), not a failure.

---

### Category: Runtime Data Paths (#1169)

`data_paths` in `template.yaml` declares the agent's runtime data (SQLite DBs, datasets) that live under `/home/developer/data` on the already-durable home volume — kept out of git, but exportable/importable for portability. These checks fire only when `data_paths` is declared (it is opt-in).

> **Implemented in #2137.** DP-001..DP-004 were documented and indexed here from #1169 but never existed in `spec.py`. `TestSpecDocSync::test_ids_match_doc` matched a **single**-letter prefix (`^\|\s*([A-Z]-\d{3})\s*\|`), so the two-letter `DP-` ids never took part in the "the two can't drift" guarantee. The regex is now `[A-Z]{1,2}-\d{3}`. `DP-005` (`.trinity/pre-snapshot` shebang) was **retired instead of implemented** — PR2 of #1169 never shipped, so no executor reads that hook.

**DP-001** — `data_paths` entries resolve under `data/`  
Severity: HARD | Type: STATIC  
Each entry, after normalization, must stay under the `data/` root (relative to `/home/developer`) — reported as `absolute`, `escapes_data_root`, `outside_data_root`, or `shell_metacharacters`. Absolute paths and `../` are only two of the ways to be outside the root: `POST /api/agents/{name}/data/export` archives `/home/developer/data` and nothing else, so a plain relative entry like `outputs/*.csv` is just as unsnapshotted — it merely fails quietly instead of loudly. The export/import primitive only captures and restores `data/**`, so an out-of-root declaration is silently never snapshotted — which is why this is the category's only HARD. Shell-safety reuses `git_service._is_safe_data_path`, the **same** predicate `materialize_data_paths` uses to decide what to drop (the A-002 discipline: validate with the parser the executor uses). Containment is checked *here* because the materializer's own regex deliberately does not — it admits `..` and `/` — so this is new coverage rather than a mirror.

**DP-002** — `data/` root is gitignored  
Severity: SOFT | Type: STATIC  
Trinity appends `data/` (and each declared entry) to the agent's `.gitignore` at creation, but a template shipping its own `.gitignore` should pre-include `data/`. Without it, runtime data risks being committed on the first auto-sync. **SOFT, not the HARD originally documented** (#2137): because `materialize_data_paths` appends the rule itself, a violation is a platform anomaly rather than an author defect, and the consequence (data committed to git) is bloat/leak — not the runtime breakage HARD denotes.

**DP-003** — `data_paths` don't overlap managed paths  
Severity: SOFT | Type: STATIC  
Entries must not overlap `.trinity/`, `.claude/`, `.env`, `.mcp.json`, `git.commit_paths`, or `persistent_state`. Those surfaces are materialized and managed separately; overlapping declarations create ambiguous ownership and double-handling.

**DP-004** — `data_paths` ⇒ not replica-safe  
Severity: INFO | Type: STATIC  
A `data_paths` declaration means the agent carries instance-local runtime state. Such an agent cannot be replicated by cloning its template alone — its data must travel via `data/export` → `data/import`. Flag for replica-safety tooling (#927). **INFO, not the SOFT originally documented** (#2137): this reports a *property* of the agent, not a defect. There is no edit that "fixes" it, and an unactionable SOFT is precisely the finding class #2137 removed.

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

All other checks require manual intervention.

---

## Implementation Notes

- Checks in this spec map to the validation service package at `src/backend/services/compatibility/` (`spec.py` is the single source of truth; `collector.py`, `static_checks.py`, `ai_checks.py`, `fixes.py`) — issue #668.
- AI-evaluated checks call the Claude API (`claude-haiku-4-5`) with the relevant file contents, batched by category; results include a confidence score and explanation.
- The full check list is versioned here; bump this file when adding/removing checks. A unit test (`tests/unit/test_compatibility_checks.py::TestSpecDocSync`) asserts the check-id set **and the Severity column** here match `spec.py` exactly, so the two can't drift. The id regex is `[A-Z]{1,2}-\d{3}` — it was `[A-Z]-\d{3}` until #2137, which is how `DP-001`..`DP-005` stayed documented-but-unimplemented for so long. Any future two-letter category is covered.
- Check IDs are stable — do not renumber existing checks; append new ones. **Retired ids are never reissued** (see below), so a persisted `agent_compatibility_results.checks_json` row written before a retirement stays interpretable.

### Retired checks (#2137)

Removed from the catalog; their ids are permanently retired.

| Retired | Reason | Successor |
|---------|--------|-----------|
| `T-017`, `G-003`, `G-004`, `G-005` | The `template.yaml git:` block has **no backend reader** anywhere in the platform, and no bundled template declares it. It is documented in `TRINITY_COMPATIBLE_AGENT_GUIDE.md` as legacy Working Branch Mode config. | — |
| `D-006` | `template.yaml metrics:` has no backend reader — `dashboard.yaml` is the read surface. | D-001..D-005, D-008 |
| `I-005` | `.trinity/post-check` has no executor. Its only other mention was a `git_service` comment pointing back at this check. | — |
| `F-008` | Required `.claude/commands/`; the `create-agent` wizards emit `.claude/skills/<name>/SKILL.md` and never `.claude/commands/`, so this was a guaranteed INFO failure. | F-009 |
| `F-012`, `F-013` | `docs/memory/requirements.md` / `CHANGELOG.md` are Trinity-repo conventions, not agent conventions. | — |
| `T-012` | Same MCP-server comparison, one direction only. | X-004 |
| `T-016` | Byte-identical logic to X-007 under a second id. | X-007 |
| `K-002` | Literally `return c_t015(snap)`. | T-015 |
| `K-005` | AI restatement of a STATIC check. | S-010 |
| `I-003`, `I-004` | Two more ways of asking "is the output contract documented?". | I-001 |
| `C-009` | "Are constraints actionable?" split from "do constraints exist?". | C-006 |
| `G-002` | Compared against the 58-entry fleet-wide `_GITIGNORE_PATTERNS` that **Trinity injects at git-init**; most of it (`.bashrc`, `.profile`, `.cache/`, `.ssh/`, `.claude/plugins/`, …) is not authorable content, and every author-controllable line is already owned by S-001..S-008. | S-001..S-008, G-001 |
| `DP-005` | `.trinity/pre-snapshot` has no executor — PR2 of #1169 never shipped. | — |

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
- **T-018 fails closed** (trinity-enterprise#89) — the only check that catches
  its own exception and returns `fail`. `run_static`'s swallow turns a raise
  into `skipped`, which the report's counts ignore, so a broken validator
  reports "healthy" and that verdict persists into every degraded report. A
  check whose entire purpose is malformed-input tolerance cannot rest on that
  net. The swallow itself is now logged (`logger.error`), which is the
  instrument for deciding later whether to flip it to `fail` for all checks.
- **Persistence (departs from the issue's "no DB table" note).** The latest
  report per agent is persisted in `agent_compatibility_results` (one row,
  upserted) so AI verdicts show on every Overview load without re-spending
  tokens, and the fleet can aggregate "N agents have HARD findings". STATIC
  checks recompute live on each read; persisted AI verdicts are merged in until
  a re-run.
