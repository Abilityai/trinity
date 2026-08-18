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
  * DP-002/DP-004 are SOFT/INFO where the doc declared HARD/SOFT (#2137). The
    platform itself appends the `data/` ignore rule at creation
    (`git_service.materialize_data_paths`), so a DP-002 violation is a platform
    anomaly rather than an author defect — and HARD is reserved for "will break
    at runtime". DP-004 reports a *property* (instance-local data), which no
    author can "fix", so it can never be a defect-tier finding.

Retired ids (#2137) — never reissued, so old persisted `checks_json` rows in
`agent_compatibility_results` stay interpretable:
  F-008, F-012, F-013 — legacy layout/doc files the wizards no longer generate.
  T-012 → X-004, T-016 → X-007, K-002 → T-015, K-005 → S-010,
  I-003/I-004 → I-001, C-009 → C-006 — duplicates folded into their survivor.
  T-017, G-003, G-004, G-005 — the `template.yaml git:` block has NO backend
    reader and no bundled template declares it.
  D-006 — `template.yaml metrics:` has no backend reader (`dashboard.yaml` is
    the read surface; D-001..D-005/D-008 own it).
  I-005 — `.trinity/post-check` has no executor anywhere in the platform.
  G-002 — compared against the 58-entry fleet-wide `_GITIGNORE_PATTERNS` that
    Trinity itself injects at git-init. Most of that list is not authorable
    content at all (`.bashrc`, `.profile`, `.bash_history`, `.cache/`,
    `.local/`, `.npm/`, `.ssh/`, `.claude/plugins/`, `.claude/settings.json`),
    so a template author could not satisfy it and should not try; and every
    author-controllable line in it is ALREADY owned by S-001..S-008, so a
    narrowed G-002 would assert the empty set. The `.gitignore` auto-fix for
    F-003 still writes the canonical list — generating it is Trinity's job,
    demanding it of the author was not.
  DP-005 — `.trinity/pre-snapshot` has no executor (PR2 of #1169 never shipped).
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
    "DP": "Runtime Data Paths",
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
    _c("F-004", "soft", "static", "F", ".env.example exists when credentials are declared"),
    _c("F-005", "soft", "static", "F", ".mcp.json.template exists when MCP servers are declared"),
    _c("F-006", "info", "static", "F", "README.md exists"),
    _c("F-007", "info", "static", "F", ".trinity/setup.sh exists when system packages are referenced"),
    _c("F-009", "info", "static", "F", "at least one skill or command file exists", claude_only=True),
    _c("F-010", "soft", "static", "F", "dashboard.yaml exists"),
    _c("F-011", "info", "static", "F", "ARCHITECTURE.md (or docs/architecture.md) exists"),
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
    _c("T-007", "info", "static", "T", "version field present (semver)"),
    _c("T-008", "info", "static", "T", "author field present"),
    _c("T-009", "soft", "ai", "T", "description is substantive",
       prompt="Is the template description substantive — does it explain what the agent does AND who would use it, in at least two sentences? PASS only if both are clear."),
    _c("T-010", "info", "static", "T", "use_cases array present with 3–7 examples"),
    _c("T-011", "info", "static", "T", "capabilities array present"),
    _c("T-013", "soft", "ai", "T", "use_cases entries are realistic, specific prompts",
       prompt="If use_cases are present, are they realistic, specific user prompts (e.g. 'Analyze our Q3 pipeline and flag at-risk deals') rather than vague feature descriptions (e.g. 'Advanced analytics')? PASS if absent; FAIL only if present and they are buzzword/feature lists."),
    _c("T-014", "soft", "ai", "T", "tagline conveys unique value",
       prompt="If a tagline is present, does it state distinctive value rather than a generic phrase like 'AI-powered assistant'? PASS if absent or distinctive; FAIL only if present and generic."),
    # HARD, promoted from SOFT in #2137 when its duplicate K-002 was retired.
    # K-002 was declared HARD and its body was literally `return c_t015(snap)`,
    # so the *logic* was duplicated but the *severity* was not: retiring K-002 as
    # a pure duplicate would have silently downgraded the credential-declaration
    # gate from HARD to SOFT and quietly undone ent#128's deliberate choice
    # (`tests/unit/test_ent128b1_compat_gates.py` asserts `hard_count >= 1` on a
    # hostile declaration). An undeclared `${VAR}` is author-fixable and breaks
    # the agent at runtime, so HARD is the right home for it — on one check.
    _c("T-015", "hard", "static", "T", "credentials schema lists all MCP ${VAR} variables"),
    _c("T-018", "soft", "static", "T", "schedules block entries are well-formed"),
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
    # Absorbs retired C-009: one check owns "constraints exist AND are actionable".
    _c("C-006", "soft", "ai", "C", "contains explicit, actionable constraints/guardrails", claude_only=True,
       prompt="Does this CLAUDE.md have an explicit constraints or guardrails section limiting what the agent may do, AND are those constraints actionable (e.g. 'never email external addresses') rather than vague ('be safe', 'be helpful')? PASS only if constraints are present and actionable."),
    _c("C-007", "soft", "static", "C", "under 2000 lines", claude_only=True),
    _c("C-008", "soft", "ai", "C", "does not repeat standard Claude knowledge", claude_only=True,
       prompt="Does this CLAUDE.md waste context restating things the model already knows (e.g. 'write clean code', 'be helpful', generic library docs)? FAIL if there is notable generic filler; PASS if it's lean and specific."),
    _c("C-010", "info", "ai", "C", "critical rules are emphasized", claude_only=True,
       prompt="Are critical, must-never-violate rules emphasized (IMPORTANT:, bold, caps) so they survive context compression? PASS if emphasis is used for critical rules."),
    _c("C-011", "info", "ai", "C", "no stale references to unavailable tools", claude_only=True,
       prompt="Does this CLAUDE.md reference tools, MCP servers, or integrations that don't appear available to this agent (suggesting it was cloned and not updated)? FAIL if stale references exist."),
    _c("C-012", "info", "ai", "C", "identity conveys a coherent persona", claude_only=True,
       prompt="Does the agent's identity read as a coherent persona — name, tone, and area of expertise align rather than contradict? PASS if coherent."),
    # --- K: Credentials ----------------------------------------------------
    _c("K-001", "hard", "static", "K", "every ${VAR} in .mcp.json.template is in .env.example"),
    _c("K-003", "info", "static", "K", ".env.example comments explain each variable"),
    _c("K-004", "soft", "static", "K", ".env.example uses placeholder values"),
    # --- G: Git Config -----------------------------------------------------
    _c("G-001", "hard", "static", "G", ".claude/ is not excluded from .gitignore wholesale",
       auto_fixable=True, claude_only=True),
    # --- P: Skills & Playbooks (Claude runtime only) -----------------------
    _c("P-001", "soft", "static", "P", "each skill file has valid YAML frontmatter", claude_only=True),
    _c("P-002", "soft", "static", "P", "each skill frontmatter has name and description", claude_only=True),
    _c("P-003", "soft", "ai", "P", "skill descriptions enable correct auto-invocation", claude_only=True,
       prompt="For each skill, will its description trigger correct auto-invocation (says what it does AND gives trigger context) rather than being too vague/broad? FAIL if any description is too vague to invoke reliably."),
    _c("P-004", "soft", "static", "P", "each SKILL.md is under 500 lines", claude_only=True),
    _c("P-005", "soft", "ai", "P", "skills are domain-specific to this agent", claude_only=True,
       prompt="Are the skills domain-specific to this agent's purpose, rather than generic dev methodology (commit/review/test) that belongs in a shared plugin? FAIL if skills are mostly generic methodology."),
    _c("P-006", "hard", "static", "P", "autonomous/scheduled skills contain no approval gates "
       "(unless frontmatter declares automation: gated|manual)", claude_only=True),
    _c("P-007", "soft", "ai", "P", "autonomous skills include error handling/notification", claude_only=True,
       prompt="Do the autonomous/scheduled skills specify what to do on failure (log, notify, retry)? FAIL if they would fail silently."),
    _c("P-008", "soft", "ai", "P", "scheduled skills are self-contained", claude_only=True,
       prompt="Are scheduled/cron-triggered skills self-contained — do they avoid requiring a human to be present to complete? FAIL if they implicitly depend on user input."),
    _c("P-009", "info", "ai", "P", "complex skills use a multi-file layout", claude_only=True,
       prompt="Do any SKILL.md files exceed ~200 lines with detailed reference material that would be better split into SKILL.md + companion reference/examples files? FAIL (suggest split) if so. Companion files are expected and are never themselves a finding."),
    _c("P-010", "soft", "ai", "P", "skills are idempotent or document that they are not", claude_only=True,
       prompt="Are scheduled skills idempotent (same result if run repeatedly), or do they explicitly document non-idempotence? FAIL if a scheduled skill is non-idempotent and undocumented."),
    _c("P-011", "soft", "ai", "P", "allowed-tools is scoped appropriately", claude_only=True,
       prompt="Is each skill's allowed-tools scoped appropriately — read-only/analysis skills don't request write-capable tools? FAIL if a skill over-requests tools."),
    _c("P-012", "info", "ai", "P", "skills define an expected output format", claude_only=True,
       prompt="Do skills with structured output (reports, JSON, tables) specify the expected output format for consistency? PASS if structured skills document their output."),
    # --- A: Autonomy Design ------------------------------------------------
    _c("A-001", "info", "static", "A", "scheduled messages reference a slash command"),
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
    _c("I-006", "info", "static", "I",
       "Trinity plugin present, so the agent can onboard itself in place",
       claude_only=True),
    # --- DP: Runtime Data Paths (#1169) ------------------------------------
    # Documented in agent-validation-spec.md since #1169 but never implemented:
    # `TestSpecDocSync::test_ids_match_doc` matched a SINGLE-letter prefix
    # (`[A-Z]-\d{3}`), so the two-letter `DP-` ids silently never took part in
    # the "the two can't drift" guarantee. `data_paths` IS a real implemented
    # field (template_service surfaces it; git_service.materialize_data_paths
    # writes it), so these validate the platform as built.
    _c("DP-001", "hard", "static", "DP", "data_paths entries resolve under data/"),
    _c("DP-002", "soft", "static", "DP", "data/ root is excluded in .gitignore when data_paths is declared"),
    _c("DP-003", "soft", "static", "DP", "data_paths do not overlap separately-managed paths"),
    _c("DP-004", "info", "static", "DP", "data_paths make the agent instance-local (not replica-safe)"),
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
