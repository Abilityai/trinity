"""
Bundled Cornelius template declares only what it ships (#1646).

The bundle is a vendored snapshot of the public `Abilityai/cornelius` repo, and
the snapshot took `template.yaml` + `CLAUDE.md` + `.gitignore` but not
`.claude/` or `resources/local-brain-search/`. The metadata therefore advertised
~24 skills, 9 sub-agents and a FAISS semantic tier that the bundle cannot serve,
while `CLAUDE.md` instructed the agent to invoke those skills and shell out to
`resources/local-brain-search/run_*.sh`.

Nothing detected the mismatch: `template.yaml` has no CI validation, and the
runtime compatibility checks (X-003/X-004) only run against a *live* agent. These
tests are the missing guard, and they run against the REAL bundle on disk — a
fixture would only prove a fixture.

They are deliberately GENERIC (assert every declaration resolves to a file)
rather than pinning today's list, so a future re-vendor that reintroduces the
same class of drift fails here.

The semantic tier returns via trinity-enterprise#173; when it lands, the
declarations come back WITH the files and these tests keep passing unchanged.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

BUNDLE = Path(__file__).resolve().parents[2] / "config" / "agent-templates" / "cornelius"

# Capability tokens that require resources/local-brain-search/ (FAISS + embeddings).
_SEMANTIC_TOKENS = {"semantic_search", "spreading_activation", "usage_based_learning"}

# The single load-bearing capability token in the repo: the frontend route guard
# (src/frontend/src/router/index.js) reads it from /info to gate the Brain tab.
_LOAD_BEARING_TOKEN = "brain-orb"


@pytest.fixture(scope="module")
def template() -> dict:
    data = yaml.safe_load((BUNDLE / "template.yaml").read_text())
    assert isinstance(data, dict), "template.yaml must parse to a mapping"
    return data


@pytest.fixture(scope="module")
def claude_md() -> str:
    return (BUNDLE / "CLAUDE.md").read_text()


# ---------------------------------------------------------------------------
# template.yaml — declarations must resolve to files (AC#1, AC#2)
# ---------------------------------------------------------------------------

def test_every_declared_skill_has_a_skill_file(template):
    """A declared skill must exist as .claude/skills/<name>/SKILL.md."""
    declared = template.get("skills") or []
    names = [s.get("name") if isinstance(s, dict) else s for s in declared]
    missing = [n for n in names if n and not (BUNDLE / ".claude" / "skills" / n / "SKILL.md").is_file()]
    assert not missing, (
        f"template.yaml declares skills with no SKILL.md in the bundle: {missing}. "
        "Either ship the skill files or drop the declaration (#1646)."
    )


def test_every_declared_sub_agent_has_an_agent_file(template):
    """A declared sub-agent must exist as .claude/agents/<name>.md.

    InfoPanel.vue renders sub_agents as a CLICKABLE delegation prompt, so a
    declaration with no file is a button that delegates to nothing.
    """
    declared = template.get("sub_agents") or []
    names = [a.get("name") if isinstance(a, dict) else a for a in declared]
    missing = [n for n in names if n and not (BUNDLE / ".claude" / "agents" / f"{n}.md").is_file()]
    assert not missing, (
        f"template.yaml declares sub-agents with no .md in the bundle: {missing} (#1646)."
    )


def test_semantic_capabilities_require_the_search_stack(template):
    """The semantic tier may only be claimed when local-brain-search ships.

    This is the exact bug: the tokens were declared while `try_semantic()` could
    never resolve, so every install served the keyword floor while advertising
    FAISS. Written as an implication, not a denylist — trinity-enterprise#173
    ships the stack and flips this green with the tokens restored.
    """
    claimed = _SEMANTIC_TOKENS & set(template.get("capabilities") or [])
    stack_ships = (BUNDLE / "resources" / "local-brain-search" / "run_search.sh").is_file()
    assert not claimed or stack_ships, (
        f"template.yaml claims {sorted(claimed)} but resources/local-brain-search/ "
        "is not bundled, so the search hook can only serve its keyword floor (#1646)."
    )


def test_brain_orb_capability_is_preserved(template):
    """Regression guard: `brain-orb` is load-bearing, not cosmetic.

    Trimming capabilities to "only what ships" must never drop this token — the
    frontend route guard reads it from /info, so losing it silently removes the
    Brain tab, which is the entire point of the auto-seeded agent (ent#107).
    """
    assert _LOAD_BEARING_TOKEN in (template.get("capabilities") or []), (
        "`brain-orb` gates the Brain Orb route (src/frontend/src/router/index.js) — "
        "removing it disables the orb for the default agent."
    )


def test_declared_mcp_servers_match_the_mcp_template(template):
    """template.yaml mcp_servers must equal .mcp.json.template (compat check X-004).

    `trinity` is injected into .mcp.json by the platform at start and is
    deliberately NOT declared here — X-004 compares against the *template* file.
    """
    declared = {s.get("name") if isinstance(s, dict) else s
                for s in (template.get("mcp_servers") or [])}
    configured = set(json.loads((BUNDLE / ".mcp.json.template").read_text())["mcpServers"])
    assert declared == configured, (
        f"only in template.yaml: {sorted(declared - configured)}; "
        f"only in .mcp.json.template: {sorted(configured - declared)} (#1646, X-004)."
    )


def test_use_cases_do_not_promise_semantic_search(template):
    """use_cases render above the fold as CLICKABLE prompts (InfoPanel.vue).

    A use_case is the only template field that is both always-visible and
    actionable, so a false one prefills a task the agent cannot serve.
    """
    stack_ships = (BUNDLE / "resources" / "local-brain-search" / "run_search.sh").is_file()
    if stack_ships:
        pytest.skip("search stack bundled (trinity-enterprise#173) — semantic use_cases are honest")
    offenders = [u for u in (template.get("use_cases") or [])
                 if re.search(r"semantic|spreading activation|3-layer", u, re.I)]
    assert not offenders, (
        f"use_cases promise semantic search the keyword floor cannot serve: {offenders} (#1646)."
    )


# ---------------------------------------------------------------------------
# CLAUDE.md — the agent's instructions must not point at absent machinery
# ---------------------------------------------------------------------------

def test_claude_md_does_not_invoke_the_absent_search_stack(claude_md):
    """CLAUDE.md drove runtime behavior: it told the agent to run run_*.sh.

    template.yaml misleads a human reading the Info tab; CLAUDE.md misleads the
    agent itself, which is why it is in scope for this fix.
    """
    stack_ships = (BUNDLE / "resources" / "local-brain-search" / "run_search.sh").is_file()
    if stack_ships:
        pytest.skip("search stack bundled (trinity-enterprise#173)")
    hits = [ln.strip() for ln in claude_md.splitlines()
            if re.search(r"resources/local-brain-search/run_[a-z]+\.sh", ln)]
    assert not hits, f"CLAUDE.md instructs the agent to run absent scripts: {hits[:3]} (#1646)."


def test_claude_md_has_no_dangling_at_imports(claude_md):
    """Every @-import in CLAUDE.md must resolve inside the bundle.

    The vendored file carried `@.claude/settings.md` and
    `@knowledge-base-analysis.md`; neither ships, so Claude Code resolved both to
    nothing on every fresh install.
    """
    imports = re.findall(r"^@([^\s]+)$", claude_md, re.M)
    dangling = [i for i in imports if not (BUNDLE / i).exists()]
    assert not dangling, f"CLAUDE.md @-imports do not resolve in the bundle: {dangling} (#1646)."


def test_claude_md_does_not_reference_unshipped_slash_commands(claude_md):
    """A /skill instruction is only honest if the skill file ships.

    Only *agent-local* commands are in scope. `/trinity:onboard` comes from the
    trinity plugin the operator installs, so the `(?!:)` lookahead skips the
    `/namespace:command` form. `_EXTERNAL` covers the rest: `plugin` is a Claude
    Code builtin, and the others are POSIX path segments that suppress false
    positives from prose paths like `open /path/to/folder` (CLAUDE.md:30) — not
    builtins. Extend it when CLAUDE.md gains a new prose path (`scripts`
    entered with the `./scripts/deploy/start.sh` install command, #1788).
    """
    _EXTERNAL = {"plugin", "home", "api", "data", "path", "opt", "usr", "var", "tmp", "mcp", "docs",
                     "scripts"}
    # (?!:) skips plugin-namespaced commands like /trinity:onboard
    referenced = set(re.findall(r"(?<![\w/])/([a-z][a-z0-9-]{2,})\b(?!:)", claude_md))
    referenced -= _EXTERNAL
    unshipped = sorted(n for n in referenced
                       if not (BUNDLE / ".claude" / "skills" / n / "SKILL.md").is_file()
                       and not (BUNDLE / ".claude" / "commands" / f"{n}.md").is_file())
    assert not unshipped, (
        f"CLAUDE.md references slash commands with no file in the bundle: {unshipped} (#1646)."
    )


# ---------------------------------------------------------------------------
# The seed vault is shipped content and makes the same claims
# ---------------------------------------------------------------------------

def test_brain_notes_do_not_reference_unshipped_commands():
    """The seed vault's own notes must not advertise commands that don't ship.

    Found by review: the first pass trimmed `template.yaml` + `CLAUDE.md` and
    stopped, but `Brain/README.md` still said "Start here ... try /advise" and
    `02-Permanent/README.md` still documented `/recall` as "3-layer semantic
    search". Those notes render in the Brain tab and are the literal first thing
    a fresh install is told to read — a *more* visible surface than the Info tab
    this issue started from. Same bug, one directory over.
    """
    offenders = {}
    for note in (BUNDLE / "Brain").rglob("*.md"):
        found = set()
        for cmd in re.findall(r"(?<![\w/])/([a-z][a-z0-9-]{2,})\b(?!:)", note.read_text()):
            if (BUNDLE / ".claude" / "skills" / cmd / "SKILL.md").is_file():
                continue
            if (BUNDLE / ".claude" / "commands" / f"{cmd}.md").is_file():
                continue
            found.add(cmd)
        if found:
            offenders[str(note.relative_to(BUNDLE))] = sorted(found)
    assert not offenders, (
        f"seed vault notes reference slash commands the bundle does not ship: {offenders} (#1646)."
    )


# ---------------------------------------------------------------------------
# The upgrade ladder must survive the trim (AC#3)
# ---------------------------------------------------------------------------

def test_search_hook_keyword_to_semantic_ladder_is_intact():
    """AC#3: the hook still tries the stack first, then falls back to keyword.

    The fix trims *claims*; it must not touch the coded ladder, or
    trinity-enterprise#173 would have nothing to upgrade into.
    """
    hook = (BUNDLE / ".trinity" / "brain-orb" / "search").read_text()
    assert "run_search.sh" in hook, "hook must still attempt the semantic backend"
    assert "def try_semantic" in hook, "semantic attempt must remain"
    assert re.search(r'"?backend"?\s*[:=]', hook), "hook must still report its backend honestly"
