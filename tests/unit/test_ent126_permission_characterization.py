"""Characterization of `configure_permissions`' WRITE behaviour (ent#126).

Why this file exists
--------------------
trinity-enterprise#126 extracts the permission-topology computation out of
`configure_permissions` into a pure `resolve_permission_edges` so the dry-run
preview and the real deploy cannot drift. Refactoring a shipped writer needs
behaviour-preservation evidence, and the two candidates for that evidence do
not provide it:

* A parity test comparing `resolve_permission_edges()` against a writer that
  was *just refactored to loop that same resolver* is tautological — resolver
  and writer drift together and the test stays green.
* `test_ent125_resilient_system_deploy.py` monkeypatches `configure_permissions`
  (and `create_schedules`), so that suite never executes either function.

So this file pins the writer's observable behaviour — the exact ordered
sequence of `db.set_agent_permissions(source, targets, created_by)` calls and
the integer it returns — with expectations hand-derived from the SHIPPED code
and written down independently of the resolver. It was captured GREEN against
the pre-refactor writer; that run is the artifact. It must stay green after.

Every assertion below encodes a truthiness guard that is easy to "clean up"
into a behaviour change:

* full-mesh `if targets:` — a source with no targets is skipped entirely,
  never written as an empty list.
* orchestrator-workers `if workers:` guards the WHOLE body — a lone
  orchestrator with zero workers writes nothing and counts 0.
* orchestrator-workers counts `len(workers)` by ASSIGNMENT (not +=), and the
  worker-clearing calls are not counted.
* `explicit: {}` is falsy, so the entire branch is skipped and nothing is
  cleared.
* explicit targets are filtered by `if t in agent_names` — unknown targets are
  silently dropped, an unknown SOURCE is skipped with a log warning.

The odd branches are reachable in production even though `validate_manifest`
rejects unknown explicit sources/targets from a manifest: on the
partial-deploy path `configure_permissions` is called with `created_map`, a
SUBSET of the resolved names (system_service.py step 8).
"""
from __future__ import annotations

import asyncio

import pytest

import services.system_service as system_service
from models import SystemPermissions

pytestmark = pytest.mark.unit

CREATED_BY = "deployer"


class _RecordingDb:
    """Records set_agent_permissions calls in order."""

    def __init__(self):
        self.calls: list[tuple[str, list[str], str]] = []

    def set_agent_permissions(self, source, targets, created_by):
        # Copy the list: the writer may hand over a list it still holds.
        self.calls.append((source, list(targets), created_by))
        return True


@pytest.fixture
def rec(monkeypatch):
    db = _RecordingDb()
    monkeypatch.setattr(system_service, "db", db)
    return db


def _configure(permissions, agent_names):
    """Drive the async writer from a sync test.

    `tests/unit/pytest.ini` is the effective inifile for this directory, so
    pyproject's `asyncio_mode = "auto"` does not apply and a bare `async def
    test_*` would be silently unsupported. asyncio.run is the dominant
    tests/unit idiom for exactly this reason.
    """
    return asyncio.run(
        system_service.configure_permissions(
            agent_names=agent_names,
            permissions=permissions,
            created_by=CREATED_BY,
        )
    )


def _names(*shorts, system="sys"):
    return {s: f"{system}-{s}" for s in shorts}


# ---------------------------------------------------------------- no-op cases

def test_permissions_none_writes_nothing(rec):
    """`if not permissions: return 0` — the early return."""
    assert _configure(None, _names("a", "b")) == 0
    assert rec.calls == []


def test_permissions_empty_model_writes_nothing(rec):
    """A SystemPermissions with neither preset nor explicit falls off the chain."""
    assert _configure(SystemPermissions(), _names("a", "b")) == 0
    assert rec.calls == []


def test_explicit_empty_dict_clears_nothing(rec):
    """`explicit: {}` is FALSY, so the elif never runs.

    The tempting reading is "an empty matrix means no permissions, so clear
    everyone" — that is the `none` preset, not this. Shipped behaviour writes
    nothing at all.
    """
    assert _configure(SystemPermissions(explicit={}), _names("a", "b")) == 0
    assert rec.calls == []


# -------------------------------------------------------------- full-mesh

def test_full_mesh_three_agents(rec):
    count = _configure(
        SystemPermissions(preset="full-mesh"), _names("a", "b", "c")
    )
    assert rec.calls == [
        ("sys-a", ["sys-b", "sys-c"], CREATED_BY),
        ("sys-b", ["sys-a", "sys-c"], CREATED_BY),
        ("sys-c", ["sys-a", "sys-b"], CREATED_BY),
    ]
    # Summed over sources, NOT the number of calls.
    assert count == 6


def test_full_mesh_single_agent_writes_nothing(rec):
    """`if targets:` — a lone agent is skipped, never written as []."""
    count = _configure(SystemPermissions(preset="full-mesh"), _names("solo"))
    assert rec.calls == []
    assert count == 0


# ---------------------------------------------------- orchestrator-workers

def test_orchestrator_workers_normal(rec):
    count = _configure(
        SystemPermissions(preset="orchestrator-workers"),
        _names("orchestrator", "w1", "w2"),
    )
    assert rec.calls == [
        ("sys-orchestrator", ["sys-w1", "sys-w2"], CREATED_BY),
        ("sys-w1", [], CREATED_BY),
        ("sys-w2", [], CREATED_BY),
    ]
    # len(workers) by assignment; the two clearing calls are NOT counted.
    assert count == 2


def test_orchestrator_workers_without_orchestrator_writes_nothing(rec):
    """No `orchestrator` short name => the whole branch is skipped.

    Note the workers are NOT cleared — a headless fleet keeps whatever
    default permissions it had. validate_manifest emits a warning for this
    shape; the writer stays silent.
    """
    count = _configure(
        SystemPermissions(preset="orchestrator-workers"), _names("w1", "w2")
    )
    assert rec.calls == []
    assert count == 0


def test_orchestrator_workers_lone_orchestrator_writes_nothing(rec):
    """`if workers:` guards the WHOLE body, including the orchestrator's own set.

    A one-agent orchestrator-workers system writes nothing and counts 0 — the
    orchestrator is NOT granted an empty permission set.
    """
    count = _configure(
        SystemPermissions(preset="orchestrator-workers"), _names("orchestrator")
    )
    assert rec.calls == []
    assert count == 0


# ------------------------------------------------------------------- none

def test_preset_none_clears_every_agent_and_counts_zero(rec):
    count = _configure(SystemPermissions(preset="none"), _names("a", "b"))
    assert rec.calls == [
        ("sys-a", [], CREATED_BY),
        ("sys-b", [], CREATED_BY),
    ]
    # Clearing is not "configuring a permission".
    assert count == 0


# --------------------------------------------------------------- explicit

def test_explicit_clears_unlisted_then_sets_listed(rec):
    """Two phases, in this order: clear every short name absent from
    `explicit`, then apply each explicit source."""
    count = _configure(
        SystemPermissions(explicit={"a": ["b"]}), _names("a", "b", "c")
    )
    assert rec.calls == [
        ("sys-b", [], CREATED_BY),
        ("sys-c", [], CREATED_BY),
        ("sys-a", ["sys-b"], CREATED_BY),
    ]
    assert count == 1


def test_explicit_unknown_target_is_silently_filtered(rec):
    """`[agent_names[t] for t in target_shorts if t in agent_names]`.

    Reachable on the partial-deploy path, where the map is a subset: a target
    whose agent failed to create is dropped and the source is still written.

    Note the leading clear of `sys-b`: phase 1 clears every agent that is not
    an explicit SOURCE, so a target-only agent is cleared first and granted-to
    second. `ghost` never appears at all.
    """
    count = _configure(
        SystemPermissions(explicit={"a": ["b", "ghost"]}), _names("a", "b")
    )
    assert rec.calls == [
        ("sys-b", [], CREATED_BY),
        ("sys-a", ["sys-b"], CREATED_BY),
    ]
    assert count == 1


def test_explicit_unknown_source_is_skipped(rec):
    """An unknown source is skipped (logged) — the clear phase still ran."""
    count = _configure(
        SystemPermissions(explicit={"ghost": ["a"]}), _names("a", "b")
    )
    assert rec.calls == [
        ("sys-a", [], CREATED_BY),
        ("sys-b", [], CREATED_BY),
    ]
    assert count == 0


def test_explicit_empty_target_list_is_written_and_counts_zero(rec):
    """`explicit: {a: []}` DOES write an empty set for a (unlike the falsy
    `explicit: {}` case) and adds 0 to the count."""
    count = _configure(
        SystemPermissions(explicit={"a": []}), _names("a", "b")
    )
    assert rec.calls == [
        ("sys-b", [], CREATED_BY),
        ("sys-a", [], CREATED_BY),
    ]
    assert count == 0


def test_explicit_multiple_sources_sums_targets(rec):
    count = _configure(
        SystemPermissions(explicit={"a": ["b", "c"], "b": ["c"]}),
        _names("a", "b", "c"),
    )
    assert rec.calls == [
        ("sys-c", [], CREATED_BY),
        ("sys-a", ["sys-b", "sys-c"], CREATED_BY),
        ("sys-b", ["sys-c"], CREATED_BY),
    ]
    assert count == 3


# ------------------------------------------------------- preset precedence

def test_preset_wins_over_explicit_when_both_set(rec):
    """The if/elif chain tests `preset` first.

    validate_manifest raises 400 on preset+explicit, so a manifest cannot
    reach here — but the writer's own precedence is pinned so a refactor that
    reorders the chain is caught rather than being masked by the validator.
    """
    count = _configure(
        SystemPermissions(preset="none", explicit={"a": ["b"]}), _names("a", "b")
    )
    assert rec.calls == [
        ("sys-a", [], CREATED_BY),
        ("sys-b", [], CREATED_BY),
    ]
    assert count == 0


def test_unrecognized_preset_writes_nothing(rec):
    """An unknown preset string falls through every branch.

    `explicit` is None here, so nothing is written. validate_manifest rejects
    unknown presets, so this is defence-in-depth on the writer.
    """
    count = _configure(
        SystemPermissions(preset="mesh-ish"), _names("a", "b")
    )
    assert rec.calls == []
    assert count == 0
