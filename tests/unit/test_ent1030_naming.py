"""trinity-enterprise#1030: `naming:` — a manifest may name its members bare.

`naming: prefixed` (the default) is today: member `x` of system `acme` is the
agent `acme-x`. `naming: bare` makes the manifest key the agent's full name, so
a fleet whose agents were never prefixed can be described — and reconciled —
by the one format.

Every case runs over the REAL `db` on a full schema (`db_backend`); Docker is
faked at its read seams only (`_ent695_world.py`). The prefixed path is pinned
byte-for-byte by `test_ent695_create_mode_golden.py`, which this issue leaves
unedited.
"""
from __future__ import annotations

import ast
import asyncio
import sys
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
from services import system_service  # noqa: E402

pytestmark = pytest.mark.unit

SYSTEM = W.SYSTEM


@pytest.fixture
def world(db_backend, monkeypatch, tmp_path):
    return W.make_world(monkeypatch, tmp_path)


@pytest.fixture
def ew(world, monkeypatch):
    W.install_executor_seams(world, monkeypatch)
    return world


def _manifest(*shorts: str, naming: str | None = None, extra: str = "") -> str:
    head = f"name: {SYSTEM}\n" + (f"naming: {naming}\n" if naming is not None else "")
    agents = "\n".join(f"  {s}:\n    template: local:plain" for s in shorts)
    return f"{head}agents:\n{agents}\n{extra}"


def _parse(yaml_text: str):
    manifest = system_service.parse_manifest(yaml_text)
    warnings = system_service.validate_manifest(manifest)
    return manifest, warnings


# ===========================================================================
# CP0a — the key and the one name resolver
# ===========================================================================

class TestTheNamingKey:

    def test_absent_is_prefixed_which_is_today(self):
        manifest, warnings = _parse(_manifest("a"))
        assert manifest.naming == "prefixed"
        assert warnings == []

    def test_bare_is_read_and_is_not_an_unknown_key(self):
        manifest, warnings = _parse(_manifest("regent", naming="bare"))
        assert manifest.naming == "bare"
        assert "naming" not in manifest.unknown_keys
        assert warnings == []

    @pytest.mark.parametrize("value", ["flat", "Bare", "'yes'", "1", "null"])
    def test_any_other_value_is_a_named_refusal(self, value):
        manifest = system_service.parse_manifest(_manifest("a", naming=value))
        with pytest.raises(ValueError) as e:
            system_service.validate_manifest(manifest)
        assert "naming must be 'prefixed' or 'bare'" in str(e.value)

    def test_the_route_answers_the_refusal_as_a_named_400(self, world):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as e:
            asyncio.run(system_service.deploy_manifest(
                _manifest("a", naming="flat"), world.user(W.ALICE), dry_run=True))
        assert e.value.status_code == 400
        assert "naming must be 'prefixed' or 'bare'" in str(e.value.detail)

    def test_a_member_may_be_named_like_its_system_under_bare(self):
        manifest, _ = _parse(_manifest(SYSTEM, "regent", naming="bare"))
        assert system_service.declared_names(manifest) == {SYSTEM: SYSTEM, "regent": "regent"}


class TestOneNameResolver:

    def test_prefixed_names_carry_the_system(self):
        manifest, _ = _parse(_manifest("a", "b"))
        assert system_service.member_name(manifest, "a") == "acme-a"
        assert system_service.declared_names(manifest) == {"a": "acme-a", "b": "acme-b"}

    def test_bare_names_are_the_keys(self):
        manifest, _ = _parse(_manifest("regent", "canon", naming="bare"))
        assert system_service.member_name(manifest, "regent") == "regent"
        assert system_service.declared_names(manifest) == {"regent": "regent", "canon": "canon"}

    def test_no_member_name_is_spelled_outside_the_resolver(self):
        """Three f-strings of one rule is how #2373 became a bug in three
        places: a `{x}-{y}` join in `system_service.py` lives in the resolver
        and nowhere else."""
        src = (_BACKEND / "services" / "system_service.py").read_text()
        tree = ast.parse(src)
        owners = {}
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(fn):
                    owners.setdefault(id(node), fn.name)
        joins = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            v = node.values
            if any(
                isinstance(v[i], ast.FormattedValue)
                and isinstance(v[i + 1], ast.Constant) and v[i + 1].value == "-"
                and isinstance(v[i + 2], ast.FormattedValue)
                for i in range(len(v) - 2)
            ):
                joins.append((owners.get(id(node)), node.lineno))
        assert joins and {fn for fn, _ in joins} == {"_member_name"}, joins


class TestDeployUnderBare:
    """P8 (ruled yes): deploy creates bare names too."""

    def test_the_dry_run_previews_bare_names(self, world):
        res = asyncio.run(system_service.deploy_manifest(
            _manifest("regent", "canon", naming="bare"), world.user(W.ALICE), dry_run=True))
        assert [a["name"] for a in res.agents_to_create] == ["regent", "canon"]
        assert res.failed == []

    def test_a_taken_bare_name_still_suffixes_in_create_mode(self, world):
        world.seed_agent("regent", tags=())
        res = asyncio.run(system_service.deploy_manifest(
            _manifest("regent", naming="bare"), world.user(W.ALICE), dry_run=True))
        assert [a["name"] for a in res.agents_to_create] == ["regent_2"]
        assert any("'regent' already exists" in w for w in res.warnings)

    def test_a_real_deploy_creates_and_tags_the_bare_name(self, ew, monkeypatch):
        async def _no_start(names):
            return {n: "started" for n in names}
        monkeypatch.setattr(system_service, "start_all_agents", _no_start)
        res = asyncio.run(system_service.deploy_manifest(
            _manifest("regent", naming="bare"), ew.user(W.ALICE), create_agent_fn=ew.create))
        assert res.agents_created == ["regent"]
        assert [c.name for c in ew.create_calls] == ["regent"]
        # The system tag is the membership record a bare name cannot carry.
        assert SYSTEM in db.get_agent_tags("regent")

    def test_prefixed_deploy_is_unchanged(self, world):
        res = asyncio.run(system_service.deploy_manifest(
            _manifest("regent"), world.user(W.ALICE), dry_run=True))
        assert [a["name"] for a in res.agents_to_create] == ["acme-regent"]


class TestApplyUnderBare:

    def test_an_absent_bare_member_is_created_under_its_bare_name(self, ew):
        res = W.apply(ew, _manifest("regent", naming="bare"), dry_run=True)
        assert [(m.name, m.verdict) for m in res.members] == [("regent", "create")]

    def test_a_tagged_bare_member_is_reconciled_not_refused(self, world):
        world.seed_agent("regent")  # tagged `acme` by its deploy
        snap = world.snapshot(_manifest("regent", naming="bare"))
        assert snap.blockers == []
        assert snap.members["regent"].name == "regent"
        assert snap.members["regent"].status == "managed"
        assert snap.membership.evidence == {"regent": "tag"}


# ===========================================================================
# E5 — the executor's phase order is a pinned tuple, not dict-insertion luck
# ===========================================================================

def test_apply_op_phase_order():
    assert system_service._APPLY_OP_PHASES == (
        "tag", "edge", "folders", "resources", "created_resources",
        "schedule_create", "schedule_update", "created_schedules",
    )
