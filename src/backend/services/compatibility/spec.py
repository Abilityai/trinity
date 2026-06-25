"""
Agent compatibility check catalog — the SINGLE source of truth (#668).

Every check is declared here exactly once with its severity, type, category,
runtime applicability, auto-fixability, and (for AI checks) the evaluation
prompt. The fix registry (`fixes.py`) and the static check registry
(`static_checks.py`) are validated against this catalog by a consistency test,
and the catalog's id set is kept in sync with `docs/agent-validation-spec.md`
by a sync test — so the three can never silently drift.

Catalog vs. emitted severity
-----------------------------
`severity` here is the catalog severity from the spec doc (hard | soft | info).
At evaluation time an **AI check's severity is capped at SOFT** (see
`__init__.py`): an LLM verdict is non-deterministic, so it must never drive the
HARD count. The catalog keeps the doc's declared severity; the cap is applied
when building the report.

Deviations from docs/agent-validation-spec.md (recorded in the doc's
"Implementation deviations" note):
  * P-006 implemented STATIC (doc marks AI) — the check has literal patterns to
    scan, and it is HARD, so it must not depend on an optional API key.
  * F-007, A-001, X-007 implemented STATIC (doc marks AI or hybrid) — the
    determinable signal is a deterministic file/pattern check.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Category code -> human-readable name.
CATEGORIES: Dict[str, str] = {
    "F": "File Structure",
    "S": "Security",
    "T": "template.yaml",
    "C": "CLAUDE.md",
    "K": "Credentials",
    "G": "Git Config",
    "P": "Skills & Playbooks",
    "A": "Autonomy Design",
    "D": "Dashboard & Metrics",
    "X": "Cross-File Consistency",
    "I": "Composability",
}

SEVERITIES = ("hard", "soft", "info")
TYPES = ("static", "ai")


@dataclass(frozen=True)
class CheckDef:
    id: str
    severity: str          # "hard" | "soft" | "info"  (catalog severity)
    type: str              # "static" | "ai"
    category: str          # category code (key of CATEGORIES)
    description: str        # short human description
    auto_fixable: bool = False
    claude_only: bool = False   # skip for non-Claude runtimes (CLAUDE.md / .claude/)
    prompt: Optional[str] = None  # AI evaluation question (AI checks only)

    @property
    def category_name(self) -> str:
        return CATEGORIES.get(self.category, self.category)


def _c(*args, **kwargs) -> CheckDef:
    return CheckDef(*args, **kwargs)


# ---------------------------------------------------------------------------
# The catalog. Order here is the canonical display order.
# ---------------------------------------------------------------------------
CHECKS: List[CheckDef] = [
    # --- F: File Structure -------------------------------------------------
    _c("F-001", "hard", "static", "F", "template.yaml exists"),
    _c("F-002", "hard", "static", "F", "CLAUDE.md exists", claude_only=True),
    _c("F-003", "soft", "static", "F", ".gitignore exists", auto_fixable=True),
    _c("F-004", "soft", "static", "F", ".env.example exists"),
    _c("F-005", "soft", "static", "F", ".mcp.json.template exists when MCP servers are declared"),
    _c("F-006", "info", "static", "F", "README.md exists"),
    _c("F-007", "info", "static", "F", ".trinity/setup.sh exists when system packages are referenced"),
    _c("F-008", "info", "static", "F", ".claude/commands/ directory exists", claude_only=True),
    _c("F-009", "info", "static", "F", "at least one skill or command file exists", claude_only=True),
    _c("F-010", "soft", "static", "F", "dashboard.yaml exists"),
    _c("F-011", "info", "static", "F", "ARCHITECTURE.md (or docs/architecture.md) exists"),
    _c("F-012", "info", "static", "F", "docs/memory/requirements.md (or REQUIREMENTS.md) exists"),
    _c("F-013", "info", "static", "F", "CHANGELOG.md exists"),
    # --- S: Security -------------------------------------------------------
    _c("S-001", "hard", "static", "S", ".env is excluded in .gitignore", auto_fixable=True),
    _c("S-002", "hard", "static", "S", ".mcp.json is excluded in .gitignore", auto_fixable=True),
    _c("S-003", "hard", "static", "S", "no hardcoded secrets in committed files"),
    _c("S-004", "hard", "static", "S", ".claude/projects/ is excluded in .gitignore", auto_fixable=True),
    _c("S-005", "hard", "static", "S", ".trinity/ is excluded in .gitignore", auto_fixable=True),
    _c("S-006", "soft", "static", "S", "Claude Code runtime dirs excluded in .gitignore", auto_fixable=True),
    _c("S-007", "soft", "static", "S", "content/ is excluded in .gitignore", auto_fixable=True),
    _c("S-008", "soft", "static", "S", "*.pem, *.key, credentials.json patterns in .gitignore", auto_fixable=True),
    _c("S-009", "hard", "static", "S", ".mcp.json.template uses ${VAR} placeholders (no literal secrets)"),
    _c("S-010", "soft", "static", "S", "credential variable names are service-specific"),
    # --- T: template.yaml --------------------------------------------------
    _c("T-001", "hard", "static", "T", "valid YAML syntax"),
    _c("T-002", "hard", "static", "T", "name field present and valid"),
    _c("T-003", "hard", "static", "T", "description field present and non-empty"),
    _c("T-004", "hard", "static", "T", "resources.cpu present and valid"),
    _c("T-005", "hard", "static", "T", "resources.memory present and valid"),
    _c("T-006", "soft", "static", "T", "display_name field present"),
    _c("T-007", "soft", "static", "T", "version field present (semver)"),
    _c("T-008", "soft", "static", "T", "author field present"),
    _c("T-009", "soft", "ai", "T", "description is substantive",
       prompt="Is the template description substantive — does it explain what the agent does AND who would use it, in at least two sentences? PASS only if both are clear."),
    _c("T-010", "soft", "static", "T", "use_cases array present with 3–7 examples"),
    _c("T-011", "soft", "static", "T", "capabilities array present"),
    _c("T-012", "soft", "static", "T", "mcp_servers descriptions match .mcp.json.template"),
    _c("T-013", "soft", "ai", "T", "use_cases entries are realistic, specific prompts",
       prompt="Are the use_cases realistic, specific user prompts (e.g. 'Analyze our Q3 pipeline and flag at-risk deals') rather than vague feature descriptions (e.g. 'Advanced analytics')? FAIL if they are buzzword/feature lists."),
    _c("T-014", "soft", "ai", "T", "tagline conveys unique value",
       prompt="If a tagline is present, does it state distinctive value rather than a generic phrase like 'AI-powered assistant'? PASS if absent or distinctive; FAIL only if present and generic."),
    _c("T-015", "soft", "static", "T", "credentials schema lists all MCP ${VAR} variables"),
    _c("T-016", "info", "static", "T", "schedule messages reference existing commands", claude_only=True),
    _c("T-017", "hard", "static", "T", "commit paths do not overwrite Trinity-injected files"),
    # --- C: CLAUDE.md (Claude runtime only) --------------------------------
    _c("C-001", "hard", "static", "C", "CLAUDE.md is valid UTF-8 and non-empty", claude_only=True),
    _c("C-002", "hard", "ai", "C", "has an identity/purpose section", claude_only=True,
       prompt="Does this CLAUDE.md contain a clear statement of who the agent is and what its primary purpose is? Answer PASS or FAIL."),
    _c("C-003", "soft", "ai", "C", "contains domain-specific instructions", claude_only=True,
       prompt="Does this CLAUDE.md contain instructions specific to the agent's domain (a real workflow, domain terms, unique constraints) rather than only generic guidance any assistant already follows? FAIL if it's mostly generic."),
    _c("C-004", "soft", "ai", "C", "lists available tools and MCP integrations", claude_only=True,
       prompt="Does this CLAUDE.md tell the agent what MCP servers / tools / capabilities are available to it? PASS if it does."),
    _c("C-005", "soft", "ai", "C", "contains at least one concrete workflow", claude_only=True,
       prompt="Does this CLAUDE.md contain at least one concrete step-by-step procedure or workflow (numbered/bulleted steps)? PASS if yes."),
    _c("C-006", "soft", "ai", "C", "contains explicit constraints/guardrails", claude_only=True,
       prompt="Does this CLAUDE.md have an explicit constraints or guardrails section limiting what the agent may do? PASS if present."),
    _c("C-007", "soft", "static", "C", "under 2000 lines", claude_only=True),
    _c("C-008", "soft", "ai", "C", "does not repeat standard Claude knowledge", claude_only=True,
       prompt="Does this CLAUDE.md waste context restating things the model already knows (e.g. 'write clean code', 'be helpful', generic library docs)? FAIL if there is notable generic filler; PASS if it's lean and specific."),
    _c("C-009", "soft", "ai", "C", "constraints are explicit and actionable", claude_only=True,
       prompt="Are the constraints explicit and actionable (e.g. 'never email external addresses') rather than vague ('be safe', 'be helpful')? FAIL if constraints are vague or absent."),
    _c("C-010", "info", "ai", "C", "critical rules are emphasized", claude_only=True,
       prompt="Are critical, must-never-violate rules emphasized (IMPORTANT:, bold, caps) so they survive context compression? PASS if emphasis is used for critical rules."),
    _c("C-011", "info", "ai", "C", "no stale references to unavailable tools", claude_only=True,
       prompt="Does this CLAUDE.md reference tools, MCP servers, or integrations that don't appear available to this agent (suggesting it was cloned and not updated)? FAIL if stale references exist."),
    _c("C-012", "soft", "ai", "C", "identity conveys a coherent persona", claude_only=True,
       prompt="Does the agent's identity read as a coherent persona — name, tone, and area of expertise align rather than contradict? PASS if coherent."),
    # --- K: Credentials ----------------------------------------------------
    _c("K-001", "hard", "static", "K", "every ${VAR} in .mcp.json.template is in .env.example"),
    _c("K-002", "hard", "static", "K", "every ${VAR} in .mcp.json.template is in template.yaml credentials"),
    _c("K-003", "soft", "static", "K", ".env.example comments explain each variable"),
    _c("K-004", "soft", "static", "K", ".env.example uses placeholder values"),
    _c("K-005", "soft", "ai", "K", "credential variable names follow SERVICE_FIELD convention",
       prompt="Do the credential variable names follow a SERVICE_FIELD convention (e.g. TWITTER_API_KEY) rather than ambiguous generic names (API_KEY, SECRET, TOKEN)? FAIL if names are generic/ambiguous."),
    # --- G: Git Config -----------------------------------------------------
    _c("G-001", "hard", "static", "G", ".claude/ is not excluded from .gitignore wholesale",
       auto_fixable=True, claude_only=True),
    _c("G-002", "soft", "static", "G", ".gitignore follows the canonical pattern list", auto_fixable=True),
    _c("G-003", "soft", "static", "G", "git.commit_paths do not include secrets or content/"),
    _c("G-004", "soft", "static", "G", "git.ignore_paths include .env and .mcp.json"),
    _c("G-005", "info", "static", "G", "git.push_enabled is explicitly set"),
    # --- P: Skills & Playbooks (Claude runtime only) -----------------------
    _c("P-001", "soft", "static", "P", "each skill file has valid YAML frontmatter", claude_only=True),
    _c("P-002", "soft", "static", "P", "each skill frontmatter has name and description", claude_only=True),
    _c("P-003", "soft", "ai", "P", "skill descriptions enable correct auto-invocation", claude_only=True,
       prompt="For each skill, will its description trigger correct auto-invocation (says what it does AND gives trigger context) rather than being too vague/broad? FAIL if any description is too vague to invoke reliably."),
    _c("P-004", "soft", "static", "P", "each skill file is under 500 lines", claude_only=True),
    _c("P-005", "soft", "ai", "P", "skills are domain-specific to this agent", claude_only=True,
       prompt="Are the skills domain-specific to this agent's purpose, rather than generic dev methodology (commit/review/test) that belongs in a shared plugin? FAIL if skills are mostly generic methodology."),
    _c("P-006", "hard", "static", "P", "autonomous/scheduled skills contain no approval gates", claude_only=True),
    _c("P-007", "soft", "ai", "P", "autonomous skills include error handling/notification", claude_only=True,
       prompt="Do the autonomous/scheduled skills specify what to do on failure (log, notify, retry)? FAIL if they would fail silently."),
    _c("P-008", "soft", "ai", "P", "scheduled skills are self-contained", claude_only=True,
       prompt="Are scheduled/cron-triggered skills self-contained — do they avoid requiring a human to be present to complete? FAIL if they implicitly depend on user input."),
    _c("P-009", "info", "ai", "P", "complex skills use a multi-file layout", claude_only=True,
       prompt="Do any skills exceed ~200 lines with detailed reference material that should be split into SKILL.md + reference/examples files? FAIL (suggest split) if so."),
    _c("P-010", "soft", "ai", "P", "skills are idempotent or document that they are not", claude_only=True,
       prompt="Are scheduled skills idempotent (same result if run repeatedly), or do they explicitly document non-idempotence? FAIL if a scheduled skill is non-idempotent and undocumented."),
    _c("P-011", "soft", "ai", "P", "allowed-tools is scoped appropriately", claude_only=True,
       prompt="Is each skill's allowed-tools scoped appropriately — read-only/analysis skills don't request write-capable tools? FAIL if a skill over-requests tools."),
    _c("P-012", "info", "ai", "P", "skills define an expected output format", claude_only=True,
       prompt="Do skills with structured output (reports, JSON, tables) specify the expected output format for consistency? PASS if structured skills document their output."),
    # --- A: Autonomy Design ------------------------------------------------
    _c("A-001", "soft", "static", "A", "scheduled messages reference slash commands"),
    _c("A-002", "soft", "static", "A", "cron expressions are valid"),
    _c("A-003", "soft", "ai", "A", "agent has a clear autonomy model",
       prompt="Is this agent clearly interactive-only, autonomous-only, or a hybrid with clear mode separation — rather than ambiguously mixing assumptions about user presence? FAIL if the autonomy model is ambiguous."),
    _c("A-004", "info", "static", "A", ".trinity/pre-check is executable with a shebang"),
    _c("A-005", "info", "ai", "A", "scheduled task prompts describe expected output",
       prompt="Do the scheduled task messages describe the expected output specifically (e.g. 'Produce the weekly pipeline report') rather than vaguely ('do the thing')? FAIL if vague."),
    # --- D: Dashboard & Metrics --------------------------------------------
    _c("D-001", "soft", "static", "D", "dashboard.yaml is valid YAML"),
    _c("D-002", "soft", "static", "D", "all widget types are supported"),
    _c("D-003", "hard", "static", "D", "widget required fields are present"),
    _c("D-004", "soft", "static", "D", "progress widget values are in 0–100 range"),
    _c("D-005", "soft", "static", "D", "status widget colors are from the allowed palette"),
    _c("D-006", "soft", "static", "D", "metric names are valid keys"),
    _c("D-007", "soft", "ai", "D", "metrics reflect meaningful domain KPIs",
       prompt="Are the declared metrics meaningful, actionable domain KPIs rather than generic vanity metrics (e.g. 'messages processed')? FAIL if mostly vanity metrics."),
    _c("D-008", "info", "static", "D", "dashboard refresh_interval is >= 5 seconds"),
    # --- X: Cross-File Consistency -----------------------------------------
    _c("X-001", "soft", "ai", "X", "name, display_name, description tell a coherent story",
       prompt="Do the agent's name, display_name, and description clearly refer to the same agent and purpose, with no signs of a partially-updated clone? FAIL on contradictions."),
    _c("X-002", "soft", "ai", "X", "CLAUDE.md identity is consistent with template.yaml", claude_only=True,
       prompt="Is the agent's self-description in CLAUDE.md consistent with what template.yaml promises (purpose, use cases)? FAIL on mismatch."),
    _c("X-003", "soft", "static", "X", "declared skills exist in .claude/skills/", claude_only=True),
    _c("X-004", "soft", "static", "X", "MCP servers are consistent across template.yaml and .mcp.json.template"),
    _c("X-005", "soft", "ai", "X", ".env.example and CLAUDE.md credential references are consistent", claude_only=True,
       prompt="If CLAUDE.md references specific APIs/services, do corresponding credentials exist in .env.example (and vice versa)? FAIL on notable mismatch."),
    _c("X-006", "info", "ai", "X", "use cases are achievable with declared tools",
       prompt="Given the MCP servers and tools declared in template.yaml, are the stated use_cases achievable? FAIL any use case that needs a tool/integration not listed."),
    _c("X-007", "soft", "static", "X", "scheduled messages match existing skills/commands", claude_only=True),
    _c("X-008", "info", "ai", "X", "resource allocation is appropriate for the workload",
       prompt="Given the agent's purpose and use cases, is the cpu/memory allocation appropriate? FAIL obvious mismatches (e.g. video processing with 512m, or trivial Q&A with 16 cpu)."),
    # --- I: Composability --------------------------------------------------
    _c("I-001", "soft", "ai", "I", "callable agents declare their output format",
       prompt="If this agent is intended to be called by other agents (references Trinity MCP, agent permissions, or describes itself as a worker/specialist), does it document the format/schema of its output? FAIL if it describes only what it does, not what it returns. PASS if not a callable agent."),
    _c("I-002", "soft", "ai", "I", "scheduled tasks produce structured, consumable output",
       prompt="Do the agent's scheduled/autonomous tasks write structured, file-based output (JSON/CSV/markdown to a known path or shared folder) that another system could consume without parsing a conversation? FAIL if they only produce chat responses. PASS if no autonomous tasks."),
    _c("I-003", "soft", "ai", "I", "output schema documented for data-producing agents",
       prompt="If this agent produces reports/datasets/structured content for downstream use, is the output schema or format documented somewhere (CLAUDE.md, schemas/, template.yaml)? PASS if documented or not a data producer."),
    _c("I-004", "soft", "ai", "I", "agent has a clear interface declaration",
       prompt="Does this agent document its interface — what input it expects and what output callers receive — separate from a general description of what it does? PASS if the boundary contract is clear."),
    _c("I-005", "info", "static", "I", "post-check hook exists when output contracts are declared"),
]

# ---------------------------------------------------------------------------
# Derived lookups (computed once).
# ---------------------------------------------------------------------------
BY_ID: Dict[str, CheckDef] = {c.id: c for c in CHECKS}
ALL_IDS: Tuple[str, ...] = tuple(c.id for c in CHECKS)
STATIC_IDS: Tuple[str, ...] = tuple(c.id for c in CHECKS if c.type == "static")
AI_IDS: Tuple[str, ...] = tuple(c.id for c in CHECKS if c.type == "ai")
AUTO_FIXABLE_IDS: Tuple[str, ...] = tuple(c.id for c in CHECKS if c.auto_fixable)


def effective_severity(check: CheckDef) -> str:
    """AI checks are capped at SOFT — an LLM verdict never drives the HARD count."""
    if check.type == "ai" and check.severity == "hard":
        return "soft"
    return check.severity


def applies_to_runtime(check: CheckDef, runtime: Optional[str]) -> bool:
    """A claude_only check is skipped for non-Claude runtimes (#1187)."""
    if not check.claude_only:
        return True
    from services.agent_service.helpers import is_claude_runtime
    return is_claude_runtime(runtime)
