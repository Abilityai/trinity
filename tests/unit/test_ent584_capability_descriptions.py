"""trinity-enterprise#584 — a capability is described once, in its SKILL.md.

Requirement: docs/memory/requirements/skills.md §23 (SKILL-DESC-001).
Related flows: workspace-agent-page.md, skills-tab.md.

What these tests pin, each at the layer that owns it:

1. One blank rule (`services/skill_packaging.clean_description`): missing,
   empty, whitespace-only or non-text is `None`; a YAML scalar is its string
   form, like the agent server's `_coerce_optional_str`.
2. The rule runs at the chokepoints where descriptions enter the backend:
   the connector/Workspace playbook resolution, and the agent skills listing
   (live answer, kept copy and the last-known answer built from it).
3. The Workspace briefing tells its two tiers apart: a use case is
   `kind: "use_case"`, a playbook `kind: "playbook"`, in the hint cards AND the
   typeahead copy of them.
4. The Workspace agent page says when the briefing never got an answer
   (`capabilities_state`), so an unreachable agent is not "nothing published".
5. Template checks: T-011 ("capabilities array present") is retired, never
   reissued; X-010 (`info`) notes a `capabilities:` tag with no described
   skill, reading only what the snapshot actually holds.
"""
from __future__ import annotations

import asyncio
import datetime
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 1. The one blank rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("\n\t ", None),
        ("Writes the weekly report.", "Writes the weekly report."),
        ("  Folded text\n", "Folded text"),
        (True, "True"),            # YAML 1.1 `yes` reaches JSON as a bool
        (3, "3"),
        (1.5, "1.5"),
        (datetime.date(2026, 1, 2), "2026-01-02"),
        (["a", "b"], None),        # a collection cannot be text
        ({"a": 1}, None),
    ],
)
def test_clean_description(raw, expected):
    from services.skill_packaging import clean_description

    assert clean_description(raw) == expected


# ---------------------------------------------------------------------------
# 2a. Connector / Workspace playbook resolution
# ---------------------------------------------------------------------------


def test_resolve_exposed_playbooks_cleans_blank_descriptions():
    from services.connector_service import resolve_exposed_playbooks

    out = resolve_exposed_playbooks(
        [
            {"name": "weekly-report", "description": "Writes the weekly report."},
            {"name": "blank", "description": ""},
            {"name": "spaces", "description": "   "},
            {"name": "missing"},
        ],
        None,
    )
    assert [(p.name, p.description) for p in out] == [
        ("weekly-report", "Writes the weekly report."),
        ("blank", None),
        ("spaces", None),
        ("missing", None),
    ]


def test_resolve_exposed_playbooks_survives_a_non_text_description():
    """An older agent image passes a list straight through. It used to raise in
    `ConnectorPlaybook` and take every playbook of the agent down with it."""
    from services.connector_service import resolve_exposed_playbooks

    out = resolve_exposed_playbooks(
        [
            {"name": "listy", "description": ["one", "two"]},
            {"name": "fine", "description": "Fine."},
        ],
        None,
    )
    assert [(p.name, p.description) for p in out] == [("listy", None), ("fine", "Fine.")]


# ---------------------------------------------------------------------------
# 2b. The agent skills listing (Skills tab, chat `/` menu, public link)
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value

    def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)


def _listing_env(monkeypatch, *, state="running", body=None):
    import redis_breaker_util
    from services import agent_skills_listing as listing

    redis = _FakeRedis()

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return body

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            return _Resp()

    monkeypatch.setattr(redis_breaker_util, "get_breaker_redis", lambda: redis)
    monkeypatch.setattr(listing, "agent_container_state", lambda name: state)
    monkeypatch.setattr(listing, "agent_httpx_client", lambda name, **kw: _Client())
    return listing, redis


def test_live_listing_serves_and_keeps_clean_descriptions(monkeypatch):
    body = {
        "skills": [
            {"name": "a", "description": "  Does A.  "},
            {"name": "b", "description": "   "},
            {"name": "c", "description": None},
        ],
        "count": 3,
        "skill_paths": [".claude/skills"],
    }
    listing, redis = _listing_env(monkeypatch, body=body)

    served = asyncio.run(listing.list_skills("atlas"))

    assert [s["description"] for s in served["skills"]] == ["Does A.", None, None]
    kept = json.loads(redis.store["agent:skills_list:atlas"])
    assert [s["description"] for s in kept["skills"]] == ["Does A.", None, None]


def test_public_view_of_a_live_listing_is_clean(monkeypatch):
    body = {"skills": [{"name": "a", "description": "\n"}], "count": 1, "skill_paths": []}
    listing, _ = _listing_env(monkeypatch, body=body)

    view = listing.public_view(asyncio.run(listing.fetch_live("atlas")))

    assert view["skills"] == [{"name": "a", "description": None}]


def test_last_known_copy_written_before_the_rule_is_served_clean(monkeypatch):
    """A copy kept by an older backend still holds the blank."""
    listing, redis = _listing_env(monkeypatch, state="stopped")
    redis.store["agent:skills_list:atlas"] = json.dumps({
        "skills": [{"name": "a", "description": "  "}, {"name": "b", "description": "Does B."}],
        "skill_paths": [],
        "captured_at": "2026-11-01T00:00:00Z",
    })

    served = asyncio.run(listing.list_skills("atlas", last_known=True))

    assert served["last_known"]["reason"] == "stopped"
    assert [s["description"] for s in served["skills"]] == [None, "Does B."]


# ---------------------------------------------------------------------------
# 3. The Workspace briefing's two tiers
# ---------------------------------------------------------------------------


def _briefing(skills, *, use_cases=()):
    """Run the real `_agent_briefing` with the agent HTTP + connector reads stubbed."""
    from client_portal import service

    info = MagicMock(status_code=200)
    info.json.return_value = {"description": "d", "use_cases": list(use_cases)}
    listing = MagicMock(status_code=200)
    listing.json.return_value = {"skills": skills}

    client = MagicMock()
    client.get = AsyncMock(side_effect=lambda url, **kw: info if "template/info" in url else listing)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    core_db = MagicMock()
    core_db.get_connector_config.return_value = None

    with patch("services.agent_auth.agent_httpx_client", return_value=ctx), \
         patch("database.db", core_db):
        return asyncio.run(service._agent_briefing("atlas", "ready"))


def test_playbook_tier_is_kind_playbook_with_clean_descriptions():
    _desc, cards, searchable, total = _briefing([
        {"name": "weekly-report", "description": "Writes the weekly report.", "user_invocable": True},
        {"name": "blank", "description": "   ", "user_invocable": True},
        {"name": "listy", "description": ["x"], "user_invocable": True},
    ])

    assert total == 3
    assert [(c.title, c.description, c.kind) for c in cards] == [
        ("Weekly report", "Writes the weekly report.", "playbook"),
        ("Blank", None, "playbook"),
        ("Listy", None, "playbook"),
    ]
    assert {s.kind for s in searchable} == {"playbook"}


def test_use_case_tier_is_kind_use_case_in_cards_and_typeahead():
    _desc, cards, searchable, total = _briefing([], use_cases=["Summarise this week", "Draft a reply"])

    assert total == 2
    assert [(c.title, c.description, c.kind) for c in cards] == [
        ("Summarise this week", None, "use_case"),
        ("Draft a reply", None, "use_case"),
    ]
    # #2213's typeahead copy is rebuilt from the cards: it must not relabel a
    # use case as a playbook on the wire.
    assert [s.kind for s in searchable] == ["use_case", "use_case"]


def test_portal_playbook_kind_defaults_to_playbook_and_is_closed():
    from pydantic import ValidationError

    from client_portal.models import PortalPlaybook

    assert PortalPlaybook(title="t", starter_prompt="/t ").kind == "playbook"
    with pytest.raises(ValidationError):
        PortalPlaybook(title="t", starter_prompt="/t ", kind="tag")


# ---------------------------------------------------------------------------
# 4. The agent page says when the briefing never answered
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "briefing_state, expected",
    [("ready", "ready"), ("unavailable", "unavailable"), (None, "ready")],
)
def test_agent_page_carries_capabilities_state(monkeypatch, briefing_state, expected):
    from client_portal import agent_page
    from client_portal.models import PortalAgentPage

    for name in ("_health", "_last_active", "_stats", "_rating_tally", "_recent_work"):
        monkeypatch.setattr(agent_page, name, lambda *a, **k: None)
    monkeypatch.setattr(agent_page, "_health", lambda *a, **k: {"status": "unknown"})
    monkeypatch.setattr(agent_page, "_stats", lambda *a, **k: {})
    monkeypatch.setattr(agent_page, "_rating_tally", lambda *a, **k: {"up": 0, "down": 0, "total": 0, "unavailable": False})
    monkeypatch.setattr(agent_page, "_recent_work", lambda *a, **k: [])

    card = {"playbooks": []}
    if briefing_state is not None:
        card["briefing_state"] = briefing_state
    page = agent_page.build_page("viewer@example.com", "atlas", card)

    assert page["capabilities_state"] == expected
    assert "capabilities_state" in PortalAgentPage.model_fields


# ---------------------------------------------------------------------------
# 5. Template checks
# ---------------------------------------------------------------------------


def _f(content):
    return {"exists": True, "is_file": True, "size": len(content), "mode_exec": False,
            "binary": False, "truncated": False, "content": content}


def _skill(description=None, *, name=None, body="Do the thing.\n"):
    fm = []
    if name is not None:
        fm.append(f"name: {name}")
    if description is not None:
        fm.append(f"description: {description}")
    return _f("---\n" + "\n".join(fm) + "\n---\n" + body)


def _snap(capabilities_yaml, skills=None, *, dirs=None):
    template = "name: acme\ndescription: Acme triages leads.\n" + capabilities_yaml
    skills = skills or {}
    listed = dirs if dirs is not None else sorted(
        {rel.split("/")[2] for rel in skills if rel.startswith(".claude/skills/")}
    )
    return {
        "schema": 1, "root": "/home/developer",
        "files": {"template.yaml": _f(template)},
        "dirs": {".claude/skills": listed, ".claude/commands": [], ".claude/agents": [], "schemas": None},
        "skills": skills,
        "hit_total_cap": False,
    }


def _x010(snap):
    from services.compatibility.static_checks import run_static

    return run_static(snap, ["X-010"])["X-010"]


def test_x010_is_info_static_claude_only_and_t011_is_retired():
    from services.compatibility import spec
    from services.compatibility.static_checks import STATIC_CHECKS

    c = spec.BY_ID["X-010"]
    assert (c.severity, c.type, c.claude_only) == ("info", "static", True)
    assert "X-010" in STATIC_CHECKS
    assert "T-011" not in spec.ALL_IDS
    assert "T-011" not in STATIC_CHECKS


def test_x010_passes_without_tags():
    assert _x010(_snap(""))[0] == "pass"
    assert _x010(_snap("capabilities: []\n"))[0] == "pass"


def test_x010_passes_when_each_tag_names_a_described_skill():
    snap = _snap(
        "capabilities:\n  - lead-triage\n  - pipeline_summary\n  - draft\n",
        {
            ".claude/skills/lead-triage/SKILL.md": _skill("Triage today's leads."),
            # a snake_case tag matches a kebab-case directory
            ".claude/skills/pipeline-summary/SKILL.md": _skill("Summarise the pipeline."),
            # a tag can match the frontmatter `name` when the directory differs
            ".claude/skills/drafting/SKILL.md": _skill("Draft a reply.", name="draft"),
        },
    )
    assert _x010(snap)[0] == "pass"


def test_x010_flags_unmatched_and_undescribed_tags():
    snap = _snap(
        "capabilities:\n  - lead-triage\n  - quiet\n  - spaces\n  - nowhere\n",
        {
            ".claude/skills/lead-triage/SKILL.md": _skill("Triage today's leads."),
            ".claude/skills/quiet/SKILL.md": _skill(None, name="quiet"),
            ".claude/skills/spaces/SKILL.md": _skill('"   "'),
        },
    )
    status, _msg, detail = _x010(snap)
    assert status == "fail"
    assert detail["unmatched"] == ["nowhere"]
    assert detail["undescribed"] == ["quiet", "spaces"]


def test_x010_exempts_exactly_the_brain_orb_flag():
    assert _x010(_snap("capabilities:\n  - brain-orb\n"))[0] == "pass"
    status, _m, detail = _x010(_snap("capabilities:\n  - brain_orb\n"))
    assert status == "fail" and detail["unmatched"] == ["brain_orb"]


def test_x010_reads_object_entries_by_name():
    snap = _snap(
        "capabilities:\n  - name: lead-triage\n    description: legacy text\n",
        {".claude/skills/lead-triage/SKILL.md": _skill("Triage today's leads.")},
    )
    assert _x010(snap)[0] == "pass"


def test_x010_does_not_flag_what_the_snapshot_could_not_read():
    """The collector caps files and bytes: a listed skill whose SKILL.md is
    missing from the snapshot, unread or truncated is unknown, not a finding."""
    unread = dict(_skill("x"), content=None)
    truncated = dict(_skill("x"), truncated=True)
    snap = _snap(
        "capabilities:\n  - capped\n  - unread\n  - cut\n",
        {".claude/skills/unread/SKILL.md": unread, ".claude/skills/cut/SKILL.md": truncated},
        dirs=["capped", "cut", "unread"],
    )
    assert _x010(snap)[0] == "pass"


def test_x010_ignores_commands_and_nested_markdown():
    snap = _snap(
        "capabilities:\n  - triage\n",
        {
            ".claude/commands/triage.md": _f("---\ndescription: A command.\n---\n"),
            ".claude/skills/other/reference/triage/SKILL.md": _skill("Nested, not a skill."),
            ".claude/skills/other/SKILL.md": _skill("Other."),
        },
        dirs=["other"],
    )
    status, _m, detail = _x010(snap)
    assert status == "fail" and detail["unmatched"] == ["triage"]


def test_x010_detail_is_bounded():
    tags = "".join(f"  - {'t' * 200}{i}\n" for i in range(40))
    status, _m, detail = _x010(_snap("capabilities:\n" + tags))
    assert status == "fail"
    assert len(detail["unmatched"]) == 25
    assert all(len(t) <= 80 for t in detail["unmatched"])


def test_p002_treats_a_whitespace_description_as_missing():
    from services.compatibility.static_checks import run_static

    snap = _snap("", {".claude/skills/spaces/SKILL.md": _skill('"   "', name="spaces")})
    assert run_static(snap, ["P-002"])["P-002"][0] == "fail"


# ---------------------------------------------------------------------------
# 6. The skills library: no description is guessed from the body
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("---\nname: x\ndescription: Does X.\n---\n\n# X\n\nBody.\n", "Does X."),
        # The old fallback took the first non-header line, which for this file
        # is the frontmatter's own `name: x`.
        ("---\nname: x\n---\n\n# X\n\nBody line.\n", None),
        ("---\nname: x\ndescription: '  '\n---\nBody.\n", None),
        ("# X\n\nA legacy body line.\n", None),
        ("---\nname: x\ndescription: [a, b]\n---\nBody.\n", None),
    ],
)
def test_library_parse_never_guesses_a_description(tmp_path, monkeypatch, text, expected):
    from services.skill_service import SkillService

    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text(text)
    svc = SkillService()
    monkeypatch.setattr(svc, "_skill_files", lambda clone, name: [])

    info = svc._parse_skill_info(None, "x", skill_md)

    assert info["description"] == expected
    # No new warning code: a library whose CI runs this parser fails on
    # `frontmatter_invalid*`, and a list description was never one.
    assert not any(w.startswith("frontmatter_invalid:description") for w in info["contract_warnings"])


def test_skill_packaging_imports_only_what_the_library_ci_downloads():
    """The community skills library validates its SKILL.md files with this
    parser by downloading exactly `services/skill_packaging.py` and
    `utils/safe_yaml.py` at a pinned commit (its `.github/workflows/
    validate.yml`). An import of any other backend module breaks that CI at
    its next pin bump, which is why the blank rule lives in this file. Its
    live consumer is that workflow, so this reads the source on purpose."""
    import ast
    import sys
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src" / "backend" / "services" / "skill_packaging.py"
    allowed_local = {"utils.safe_yaml"}
    imported = set()
    for node in ast.walk(ast.parse(src.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    foreign = {
        m for m in imported
        if m.split(".")[0] not in sys.stdlib_module_names and m.split(".")[0] != "yaml" and m not in allowed_local
    }
    assert foreign == set(), f"skill_packaging.py imports {sorted(foreign)}; the library CI downloads only it and utils/safe_yaml.py"


# ---------------------------------------------------------------------------
# 7. The finding names what it found (the Compatibility card shows `message`)
# ---------------------------------------------------------------------------


def test_x010_message_names_the_tags_and_caps_the_list():
    status, msg, detail = _x010(_snap("capabilities:\n  - lead_triage\n  - obj-tag\n"))
    assert status == "fail"
    assert msg == "capabilities tags with no described skill: lead_triage, obj-tag"
    assert detail == {"unmatched": ["lead_triage", "obj-tag"], "undescribed": []}

    tags = "".join(f"  - tag{i}\n" for i in range(8))
    _s, msg, _d = _x010(_snap("capabilities:\n" + tags))
    assert msg == "capabilities tags with no described skill: tag0, tag1, tag2, tag3, tag4, +3 more"


def test_x010_message_keeps_tag_order_across_unmatched_and_undescribed():
    snap = _snap(
        "capabilities:\n  - quiet\n  - nowhere\n",
        {".claude/skills/quiet/SKILL.md": _skill(None, name="quiet")},
    )
    assert _x010(snap)[1] == "capabilities tags with no described skill: quiet, nowhere"


def test_x003_message_names_the_missing_skills():
    from services.compatibility.static_checks import run_static

    snap = _snap("skills:\n  - alpha\n  - beta\n", {".claude/skills/alpha/SKILL.md": _skill("A.")})
    status, msg, detail = run_static(snap, ["X-003"])["X-003"]
    assert status == "fail"
    assert msg == "template.yaml declares skills with no SKILL.md: beta"
    assert detail == {"missing": ["beta"]}
