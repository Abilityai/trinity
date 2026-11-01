"""#4131: `naming: bare` edge cases — re-parse, name collisions, restart scope.

1. A member the platform suffixed on creation (`writer_2`, `writer_2_2`) is
   exported under that key, and the key must parse again. The manifest key
   accepts what the platform's own name producers write (the create route's
   `[a-z0-9_.-]` charset), in both naming modes.
2. Under `naming: bare` a prefix is no evidence of anything: a declared
   untagged name is adopted whatever its shape, and an undeclared untagged
   `<system>-x` is neither member, drift nor blocker. Restart, get and export
   have no stored naming, so `system_membership(naming=None)` infers bare the
   way export does (tags readable, every tagged member unprefixed) and then
   drops the prefix step; a prefixed system keeps the #2373 union.
3. A `deploy: false` member whose live agent carries the system tag is still
   reached by restart and teardown (membership is the tag); apply says so.

Every apply/route case runs the REAL route or service over the REAL `db`
facade on a full schema (`db_backend`); Docker is faked at its read seams only
(`_ent695_world.py`), plus the roster seam the #4129 tests use.
"""
from __future__ import annotations

import ast
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend  # noqa: E402,F401

pytest.importorskip("docker", reason="backend venv required (system_service chain)")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _ent695_world as W  # noqa: E402
from database import db  # noqa: E402
from models import AgentStatus  # noqa: E402
from services import system_service  # noqa: E402

pytestmark = pytest.mark.unit

SYSTEM = W.SYSTEM
T0 = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def world(db_backend, monkeypatch, tmp_path):
    return W.make_world(monkeypatch, tmp_path)


@pytest.fixture
def ew(world, monkeypatch):
    W.install_executor_seams(world, monkeypatch)
    return world


def _row(name: str) -> dict:
    """One roster row exactly as `get_accessible_agents` returns it."""
    return AgentStatus(name=name, status="running", port=2222, created=T0,
                       resources={}, template="local:plain").dict()


def _client(world, monkeypatch, rows):
    """The real systems router; the Docker-backed roster is the only seam
    (copied from #4129's tests rather than imported, to keep the stack
    decoupled)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routers import agents as agents_router
    from routers import systems

    monkeypatch.setattr(agents_router, "get_accessible_agents", lambda user: list(rows))
    app = FastAPI()
    app.include_router(systems.router)
    app.dependency_overrides[systems.get_current_user] = lambda: world.user(W.ALICE)
    return TestClient(app, raise_server_exceptions=False)


def _manifest(*members, naming: str | None = None) -> str:
    """`members` are keys, or `(key, "deploy: false")` pairs."""
    head = f"name: {SYSTEM}\n" + (f"naming: {naming}\n" if naming is not None else "")
    lines = []
    for m in members:
        key, extra = (m, None) if isinstance(m, str) else m
        lines += [f"  {key}:", "    template: local:plain"] + ([f"    {extra}"] if extra else [])
    return head + "agents:\n" + "\n".join(lines) + "\n"


def _parse(yaml_text: str):
    manifest = system_service.parse_manifest(yaml_text)
    system_service.validate_manifest(manifest)
    return manifest


def _codes(snapshot):
    return [(b.code, b.member) for b in snapshot.blockers]


# ===========================================================================
# Case 1 — a suffixed member's key parses again (both modes, one line)
# ===========================================================================

class TestCase1SuffixedKeysReparse:

    def test_a_bare_export_with_a_suffixed_member_parses_again(self, world, monkeypatch):
        world.seed_agent("writer")
        world.seed_agent("writer_2")
        client = _client(world, monkeypatch, [_row("writer"), _row("writer_2")])
        r = client.get(f"/api/systems/{SYSTEM}/manifest")
        assert r.status_code == 200, r.text
        manifest = _parse(r.text)
        assert manifest.naming == "bare"
        assert sorted(manifest.agents) == ["writer", "writer_2"]

    def test_a_prefixed_export_with_a_suffixed_member_parses_again(self, world, monkeypatch):
        world.seed_agent("acme-writer")
        world.seed_agent("acme-writer_2")
        client = _client(world, monkeypatch, [_row("acme-writer"), _row("acme-writer_2")])
        r = client.get(f"/api/systems/{SYSTEM}/manifest")
        assert r.status_code == 200, r.text
        manifest = _parse(r.text)
        assert manifest.naming == "prefixed"
        assert sorted(manifest.agents) == ["writer", "writer_2"]

    @pytest.mark.parametrize("naming", ["prefixed", "bare"])
    def test_what_the_name_producer_writes_is_a_valid_key(self, monkeypatch, naming):
        """Producer parity: `get_next_agent_name` chains its suffix when the
        re-applied copy is itself taken."""
        taken = {"writer", "writer_2"}
        monkeypatch.setattr(system_service, "agent_exists", lambda n: n in taken)
        produced = [system_service.get_next_agent_name("writer"),
                    system_service.get_next_agent_name("writer_2")]
        assert produced == ["writer_3", "writer_2_2"]
        manifest = _parse(_manifest(*produced, "a.b", "a_b", naming=naming))
        assert sorted(manifest.agents) == ["a.b", "a_b", "writer_2_2", "writer_3"]

    @pytest.mark.parametrize("key", ["Writer", "_x", ".x", "-x", "x-", "a b", "a/b"])
    @pytest.mark.parametrize("naming", ["prefixed", "bare"])
    def test_every_other_bad_key_is_still_the_named_refusal(self, key, naming):
        with pytest.raises(ValueError, match="Invalid agent name"):
            _parse(f"name: {SYSTEM}\nnaming: {naming}\nagents:\n  '{key}':\n    template: local:plain\n")

    def test_the_drift_note_still_names_a_chained_copy(self, ew):
        ew.seed_agent("writer")
        ew.seed_agent("writer_2_2")
        res = W.apply(ew, _manifest("writer", naming="bare"), dry_run=True)
        (d,) = res.drift
        assert d.name == "writer_2_2" and d.note and "suffixed copy" in d.note


# ===========================================================================
# Case 2 — under bare a prefix is no evidence of anything
# ===========================================================================

class TestCase2BareAdoptsWhateverTheShape:

    def _roster(self, world):
        world.seed_agent("acme-writer", tags=())   # declared, untagged, prefix-shaped
        world.seed_agent("acme-foo", tags=())      # undeclared, untagged, prefix-shaped
        world.seed_agent("writer")                 # declared, tagged

    def test_the_dry_run_adopts_the_declared_name_and_ignores_the_lookalike(self, ew):
        self._roster(ew)
        res = W.apply(ew, _manifest("acme-writer", "writer", naming="bare"), dry_run=True)
        assert res.status == "valid", res
        assert [a.name for a in res.adopted] == ["acme-writer"]
        mw = next(m for m in res.members if m.name == "acme-writer")
        assert "member_adopted" in [c.code for c in mw.changes]
        assert "acme-foo" not in [d.name for d in res.drift]
        assert "acme-foo" not in [a.name for a in res.adopted]

    def test_the_snapshot_has_no_blocker_and_no_prefix_member(self, world):
        self._roster(world)
        snap = world.snapshot(_manifest("acme-writer", "writer", naming="bare"))
        assert _codes(snap) == []
        assert snap.members["acme-writer"].untagged is True
        assert snap.membership.members == ["writer"]

    def test_the_real_run_tags_the_declared_name_only(self, ew):
        self._roster(ew)
        res = W.apply(ew, _manifest("acme-writer", "writer", naming="bare"), dry_run=False)
        assert res.status == "applied", res
        assert SYSTEM in db.get_agent_tags("acme-writer")
        assert SYSTEM not in (db.get_agent_tags("acme-foo") or [])
        assert ew.create_calls == []

    def test_a_bare_name_tagged_with_another_system_is_adopted_and_shown(self, ew):
        ew.seed_agent("acme-writer", tags=("other",))
        res = W.apply(ew, _manifest("acme-writer", naming="bare"), dry_run=True)
        assert res.status == "valid", res
        assert [(a.name, a.tags) for a in res.adopted] == [("acme-writer", ["other"])]
        W.apply(ew, _manifest("acme-writer", naming="bare"), dry_run=False)
        again = W.apply(ew, _manifest("acme-writer", naming="bare"), dry_run=True)
        assert {m.verdict for m in again.members} == {"unchanged"} and again.adopted == []

    def test_prefixed_mode_keeps_todays_refusal_for_the_lookalike(self, world):
        self._roster(world)
        snap = world.snapshot(_manifest("writer"))  # prefixed: declares acme-writer
        assert ("membership_evidence_prefix_only", "acme-foo") in _codes(snap)


# ===========================================================================
# The predicate: naming known (apply) and inferred (restart/get/export, D)
# ===========================================================================

class _TagRead:
    def __init__(self, tags, readable=True):
        self.tags, self.readable, self.calls = tags, readable, 0

    def __call__(self, names):
        self.calls += 1
        if not self.readable:
            raise RuntimeError("tags table unavailable")
        return {n: self.tags.get(n, []) for n in names}


class TestMembershipNaming:

    def _m(self, monkeypatch, roster, tags, readable=True, **kw):
        read = _TagRead(tags, readable)
        monkeypatch.setattr(system_service.db, "get_tags_for_agents", read)
        return system_service.system_membership(SYSTEM, roster, **kw), read

    def test_bare_is_the_tag_only(self, monkeypatch):
        res, _ = self._m(monkeypatch, ["writer", "acme-x", "acme-web"],
                         {"writer": ["acme"], "acme-web": ["acme"]}, naming="bare")
        assert res.members == ["writer", "acme-web"]
        assert res.evidence == {"writer": "tag", "acme-web": "both"}

    def test_bare_still_reads_the_tags_and_refuses_on_an_outage(self, monkeypatch):
        res, read = self._m(monkeypatch, ["writer", "acme-x"], {}, readable=False, naming="bare")
        assert read.calls == 1
        assert res.members == [] and res.tags_readable is False

    def test_an_empty_roster_never_reads(self, monkeypatch):
        res, read = self._m(monkeypatch, [], {}, naming="bare")
        assert res.members == [] and read.calls == 0

    def test_unknown_naming_is_refused(self, monkeypatch):
        with pytest.raises(ValueError):
            self._m(monkeypatch, ["writer"], {}, naming="loose")

    def test_inferred_bare_drops_the_untagged_lookalike(self, monkeypatch):
        """D: every tagged member unprefixed -> the system is bare."""
        res, _ = self._m(monkeypatch, ["writer", "acme-x"], {"writer": ["acme"]})
        assert res.members == ["writer"]

    def test_a_prefixed_system_keeps_the_union(self, monkeypatch):
        """#2373: one tagged prefixed member keeps every untagged one."""
        res, _ = self._m(monkeypatch, ["acme-web", "acme-db", "helper"],
                         {"acme-web": ["acme"], "helper": ["acme"]})
        assert res.members == ["acme-web", "acme-db", "helper"]
        assert res.evidence["acme-db"] == "prefix"

    def test_no_tagged_member_keeps_the_raw_prefix(self, monkeypatch):
        res, _ = self._m(monkeypatch, ["acme-web", "writer"], {})
        assert res.members == ["acme-web"]

    def test_an_outage_keeps_the_raw_prefix_on_the_default(self, monkeypatch):
        res, _ = self._m(monkeypatch, ["writer", "acme-x"], {"writer": ["acme"]}, readable=False)
        assert res.members == ["acme-x"] and res.tags_readable is False

    def test_explicit_prefixed_is_todays_union(self, monkeypatch):
        res, _ = self._m(monkeypatch, ["writer", "acme-x"], {"writer": ["acme"]}, naming="prefixed")
        assert res.members == ["writer", "acme-x"]


try:
    from hypothesis import given, settings, strategies as st

    _NAMES = st.lists(st.sampled_from(
        ["acme", "acme-a", "acme-b", "acme-extra-w", "writer", "w_2", "other"]),
        unique=True, max_size=6)
    _TAGS = st.dictionaries(st.sampled_from(
        ["acme", "acme-a", "acme-b", "acme-extra-w", "writer", "w_2", "other"]),
        st.lists(st.sampled_from(["acme", "acme-extra", "other"]), unique=True, max_size=2))

    @settings(max_examples=150, deadline=None)
    @given(roster=_NAMES, tags=_TAGS)
    def test_bare_membership_is_exactly_the_tag_carriers(roster, tags):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(system_service.db, "get_tags_for_agents", _TagRead(tags))
            bare = system_service.system_membership(SYSTEM, roster, naming="bare")
            inferred = system_service.system_membership(SYSTEM, roster)
        carriers = [n for n in roster if SYSTEM in tags.get(n, [])]
        assert bare.members == carriers
        assert set(bare.evidence.values()) <= {"tag", "both"}
        if carriers and not any(n.startswith(f"{SYSTEM}-") for n in carriers):
            assert inferred.members == bare.members  # D: the inference agrees
        assert set(carriers) <= set(inferred.members)
except ImportError:  # pragma: no cover
    pass


def test_only_the_apply_snapshot_passes_a_naming():
    """The naming kwarg is apply's knowledge; every other caller infers."""
    tree = ast.parse((_BACKEND / "services" / "system_service.py").read_text())
    callers = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "system_membership"
                    and any(k.arg == "naming" for k in node.keywords)):
                callers.append(fn.name)
    assert callers == ["read_apply_snapshot"]


# ===========================================================================
# Declared-only names are excluded from the evidence gate (both modes)
# ===========================================================================

class TestDeclaredOnlyOutOfTheEvidenceGate:

    @pytest.mark.parametrize("naming,key", [(None, "canon"), ("bare", "acme-canon")])
    def test_an_untagged_prefix_shaped_declared_only_member_is_not_refused(self, world, naming, key):
        world.seed_agent("acme-canon", tags=())
        world.seed_agent("acme-web" if naming is None else "web")
        members = (("web" if naming is None else "web"), (key, "deploy: false"))
        snap = world.snapshot(_manifest(*members, naming=naming))
        assert not [c for c in _codes(snap) if c[0] == "membership_evidence_prefix_only"], _codes(snap)
        assert snap.members[key].declared_only is True

    def test_the_dry_run_verdict_is_declared_only(self, ew):
        ew.seed_agent("acme-canon", tags=())
        ew.seed_agent("acme-web")
        res = W.apply(ew, _manifest("web", ("canon", "deploy: false")), dry_run=True)
        assert res.status == "valid", res
        assert {m.name: m.verdict for m in res.members}["acme-canon"] == "declared_only"
