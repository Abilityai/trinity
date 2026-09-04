"""A skills source cannot be granted through the settings back door (ent#346).

ent#237 gates every `/skills/sources` route with `reject_agent_principal`, and
says why at the site: adding a source is the GRANT action, and a prompt-injected
agent that could register its own repo gets unattended, fleet-wide, persistent
prompt injection — skills are instructions Claude follows and they ship
executable `scripts/`.

That gate worked. The same grant was reachable through `PUT
/api/settings/skills_library_url`, which is `assert_admin`-gated but not
`reject_agent_principal`-gated, and an agent-scoped key resolves to its owner
carrying the owner's role — so on the default admin-owned install it passed.
`_adopt_legacy_clone` then turned that string into a `skill_sources` row with no
validation at all, at CUSTOM priority (which outranks the bundled community
catalog), and deleted the setting afterwards, erasing where the row came from.

Four things are pinned here, because the chain has four independent links and
closing any one of them alone leaves a hole:

  1. the settings write is refused (the front door on the back door);
  2. the sink validates, so a row can never be created from an unvalidated
     string even if some future writer reaches it;
  3. adoption is genuinely gated to an unmigrated install — the guard it
     replaces was `if <two db calls>: pass`, so adoption ran unconditionally on
     every sync of every install, forever;
  4. a refusal is OBSERVABLE, because a silent refusal is how this gets
     rediscovered as "skills stopped working".
"""
from __future__ import annotations

import ast
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)
os.environ.setdefault(
    "TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent346.db")
)
os.environ.setdefault(
    "LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent346-logs")
)

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

ATTACKER_URL = "https://github.com/attacker/skills"


# ---------------------------------------------------------------------------
# 1. The settings write is refused
# ---------------------------------------------------------------------------

def test_the_legacy_keys_are_blocked_on_the_generic_settings_put():
    """Both keys, not just the URL.

    A source is `(url, ref)`. Re-pointing the ref alone changes which commit the
    fleet executes, so blocking only the URL would leave half the grant open.
    """
    from routers.settings import LEGACY_SKILLS_LIBRARY_KEYS

    assert "skills_library_url" in LEGACY_SKILLS_LIBRARY_KEYS
    assert "skills_library_branch" in LEGACY_SKILLS_LIBRARY_KEYS


def test_the_put_refuses_before_it_reaches_set_setting():
    """The block must precede the write, not merely accompany it.

    Asserted over the AST rather than by grep: the ordering is the property, and
    a block placed *after* `db.set_setting` would satisfy any text search while
    the value had already landed.
    """
    # #1028: the catch-all PUT lives in the package's `generic` module.
    src = (_BACKEND / "routers" / "settings" / "generic.py").read_text()
    tree = ast.parse(src)

    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(d, ast.Call)
            and isinstance(d.func, ast.Attribute)
            and d.func.attr == "put"
            and d.args
            and isinstance(d.args[0], ast.Constant)
            and d.args[0].value == "/{key}"
            for d in n.decorator_list
        )
    )
    body_src = ast.unparse(fn)
    guard_at = body_src.index("LEGACY_SKILLS_LIBRARY_KEYS")
    write_at = body_src.index("db.set_setting")
    assert guard_at < write_at, (
        "the legacy-skills-key guard runs AFTER db.set_setting — the value is "
        "already persisted by then"
    )


def test_the_refusal_points_at_the_gated_route():
    """A 422 that does not say where to go turns a security control into a
    mystery, and the next person routes around it."""
    # #1028: the catch-all PUT lives in the package's `generic` module.
    src = (_BACKEND / "routers" / "settings" / "generic.py").read_text()
    block = src[src.index("if key in LEGACY_SKILLS_LIBRARY_KEYS"):][:900]
    assert "POST /api/skills/sources" in block
    assert "422" in block


# ---------------------------------------------------------------------------
# 2. The sink validates
# ---------------------------------------------------------------------------

def test_adoption_applies_the_same_validators_as_the_write_routes():
    """`routers/skills.py` applies both on every source write. The sink applied
    neither, which is what made a settings string a source-creation path."""
    src = (_BACKEND / "services" / "skill_service.py").read_text()
    fn_src = src[src.index("def _adopt_legacy_clone"):]
    fn_src = fn_src[: fn_src.index("\n    def ", 10)]

    assert "validate_skills_library_url" in fn_src
    assert "reject_embedded_credentials" in fn_src

    create_at = fn_src.index("db.create_skill_source")
    assert fn_src.index("validate_skills_library_url") < create_at, (
        "validation runs after the row is created"
    )
    assert fn_src.index("reject_embedded_credentials") < create_at


@pytest.mark.parametrize("bad_url", [
    "https://evil.example.com/skills",              # not github.com
    "https://x@github.com/attacker/skills",         # embedded credential
    "https://ghp_deadbeef@github.com/a/b",          # laundered PAT (ent#334)
    "file:///etc/passwd",
    "http://169.254.169.254/latest/meta-data",      # SSRF classic
])
def test_a_row_is_never_created_from_a_rejected_url(monkeypatch, bad_url):
    """The property the AC states: a `skill_sources` row can never be created
    from an unvalidated string."""
    import services.skill_service as svc_mod

    created = []
    monkeypatch.setattr(
        svc_mod, "get_skills_library_url", lambda: bad_url, raising=False
    )
    monkeypatch.setattr(
        svc_mod, "get_skills_library_branch", lambda: "main", raising=False
    )
    monkeypatch.setattr(
        svc_mod.db, "create_skill_source",
        lambda **kw: created.append(kw) or pytest.fail("row created from a rejected URL"),
    )
    monkeypatch.setattr(svc_mod.db, "list_skill_sources", lambda: [])
    monkeypatch.setattr(svc_mod.db, "count_skill_sources", lambda: 0)
    monkeypatch.setattr(svc_mod.db, "create_operator_queue_item", lambda *a, **k: None)

    service = svc_mod.SkillService.__new__(svc_mod.SkillService)
    service.library_root = Path(tempfile.mkdtemp())

    assert service._adopt_legacy_clone() is None
    assert created == []


# ---------------------------------------------------------------------------
# 3. Adoption is gated to an unmigrated install
# ---------------------------------------------------------------------------

def test_the_dead_guard_is_gone():
    """`if <two db calls>: pass` — the calls were made and the result discarded.

    A discarded guard is worse than no guard: it reads as protection in review,
    which is exactly why this one survived. Pinned by AST so a future edit
    cannot reintroduce a condition whose body is `pass`.
    """
    src = (_BACKEND / "services" / "skill_service.py").read_text()
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "_adopt_legacy_clone"
    )
    for node in ast.walk(fn):
        if isinstance(node, ast.If):
            assert not all(isinstance(b, ast.Pass) for b in node.body), (
                "an `if ...: pass` is back in _adopt_legacy_clone — a guard whose "
                "body does nothing is the ent#346 defect verbatim"
            )


def test_adoption_is_refused_once_the_install_has_sources(monkeypatch):
    """The exploit needs adoption to run on a CONFIGURED fleet. It no longer does.

    Setting a URL after ent#237 on an install that already has sources is not a
    migration input — it is an unvalidated back door.
    """
    import services.skill_service as svc_mod

    created = []
    monkeypatch.setattr(svc_mod, "get_skills_library_url", lambda: ATTACKER_URL, raising=False)
    monkeypatch.setattr(svc_mod, "get_skills_library_branch", lambda: "main", raising=False)
    monkeypatch.setattr(svc_mod.db, "create_skill_source", lambda **kw: created.append(kw))
    monkeypatch.setattr(svc_mod.db, "list_skill_sources", lambda: [])
    monkeypatch.setattr(svc_mod.db, "count_skill_sources", lambda: 3)   # configured
    alarms = []
    monkeypatch.setattr(
        svc_mod.db, "create_operator_queue_item", lambda agent, item: alarms.append(item)
    )

    service = svc_mod.SkillService.__new__(svc_mod.SkillService)
    service.library_root = Path(tempfile.mkdtemp())

    assert service._adopt_legacy_clone() is None
    assert created == [], "adopted a source on an already-migrated install"
    assert alarms, "refusal must be observable"


def test_a_genuine_unmigrated_install_still_migrates(monkeypatch):
    """The fix must not break the migration it is guarding.

    ent#237 AC#6: an existing single-repo install keeps working with zero admin
    action. A gate that also blocks the legitimate path is a regression, not a fix.
    """
    import services.skill_service as svc_mod

    created = []

    class _Src:
        id = "src-1"

    monkeypatch.setattr(
        svc_mod, "get_skills_library_url",
        lambda: "https://github.com/abilityai/abilities", raising=False,
    )
    monkeypatch.setattr(svc_mod, "get_skills_library_branch", lambda: "main", raising=False)
    monkeypatch.setattr(
        svc_mod.db, "create_skill_source", lambda **kw: (created.append(kw), _Src())[1]
    )
    monkeypatch.setattr(svc_mod.db, "list_skill_sources", lambda: [])
    monkeypatch.setattr(svc_mod.db, "count_skill_sources", lambda: 0)   # unmigrated
    monkeypatch.setattr(svc_mod.db, "create_operator_queue_item", lambda *a, **k: None)

    service = svc_mod.SkillService.__new__(svc_mod.SkillService)
    service.library_root = Path(tempfile.mkdtemp())

    assert service._adopt_legacy_clone() == "src-1"
    assert len(created) == 1
    assert created[0]["url"] == "https://github.com/abilityai/abilities"


# ---------------------------------------------------------------------------
# 4. Refusals are observable
# ---------------------------------------------------------------------------

def test_a_refusal_raises_an_operator_alarm_not_just_a_log_line(monkeypatch):
    """AC: never a bare `logger.warning`.

    Before, a rejected URL and a successful adoption both produced a sync that
    reported success — so an operator whose library stopped migrating had
    nothing to look at.
    """
    import services.skill_service as svc_mod

    alarms = []
    monkeypatch.setattr(svc_mod, "get_skills_library_url", lambda: "https://evil.example.com/x", raising=False)
    monkeypatch.setattr(svc_mod, "get_skills_library_branch", lambda: "main", raising=False)
    monkeypatch.setattr(svc_mod.db, "list_skill_sources", lambda: [])
    monkeypatch.setattr(svc_mod.db, "count_skill_sources", lambda: 0)
    monkeypatch.setattr(
        svc_mod.db, "create_operator_queue_item", lambda agent, item: alarms.append(item)
    )

    service = svc_mod.SkillService.__new__(svc_mod.SkillService)
    service.library_root = Path(tempfile.mkdtemp())
    service._adopt_legacy_clone()

    assert alarms, "a refused adoption raised no operator alarm"
    assert alarms[0]["type"] == "alert"
    assert alarms[0]["context"]["alert_type"] == "skills_legacy_adoption_refused"


def test_the_alarm_never_blocks_a_sync(monkeypatch):
    """Adoption is fail-soft: a broken alarm must not take the sync with it."""
    import services.skill_service as svc_mod

    monkeypatch.setattr(svc_mod, "get_skills_library_url", lambda: "https://evil.example.com/x", raising=False)
    monkeypatch.setattr(svc_mod, "get_skills_library_branch", lambda: "main", raising=False)
    monkeypatch.setattr(svc_mod.db, "list_skill_sources", lambda: [])
    monkeypatch.setattr(svc_mod.db, "count_skill_sources", lambda: 0)

    def boom(*a, **k):
        raise RuntimeError("operator queue down")

    monkeypatch.setattr(svc_mod.db, "create_operator_queue_item", boom)

    service = svc_mod.SkillService.__new__(svc_mod.SkillService)
    service.library_root = Path(tempfile.mkdtemp())
    assert service._adopt_legacy_clone() is None      # returns, does not raise
