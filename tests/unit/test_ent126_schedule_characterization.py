"""Characterization of `create_schedules`' WRITE behaviour (ent#126).

The twin of `test_ent126_permission_characterization.py`, and it exists for the
same reason: trinity-enterprise#126 extracts the schedule-iteration logic into a
pure `resolve_schedule_previews` so the dry-run preview and the real deploy
cannot drift, and refactoring a shipped writer needs behaviour-preservation
evidence that a resolver-vs-refactored-writer parity test cannot supply.
`test_ent125_resilient_system_deploy.py` monkeypatches `create_schedules`, so it
proves nothing about this function either.

Pinned here, hand-derived from the SHIPPED code and captured GREEN before the
refactor:

* the skip predicate `if not final_name or not config.schedules` — an agent
  absent from the name map (the partial-deploy `created_map` case) and an agent
  with no `schedules:` key are both skipped, silently;
* the `ScheduleCreate` field mapping, in particular `cron` -> `cron_expression`
  and the `.get()` defaults `enabled=True` / `timezone="UTC"` /
  `description=None`. `enabled` defaulting to TRUE is why a manifest with
  schedules starts autonomous executions the moment it deploys, which is what
  the ent#126 preview has to warn about;
* count-only-on-success: a falsy `db.create_schedule` return is logged and NOT
  counted, while the call still happened;
* no internal try/except — an exception propagates out of `create_schedules`
  (`deploy_manifest` step 9 is what degrades it to a warning), and schedules
  already written before the raise stay written.
"""
from __future__ import annotations

import pytest

import services.system_service as system_service
from models import SystemAgentConfig

pytestmark = pytest.mark.unit

OWNER = "deployer"


class _RecordingDb:
    """Records create_schedule calls; `returns` drives success/failure."""

    def __init__(self, returns=None, raise_on=None):
        self.calls: list[tuple[str, str, object]] = []
        self._returns = list(returns) if returns is not None else None
        self._raise_on = raise_on

    def create_schedule(self, agent_name, username, schedule_data):
        if self._raise_on is not None and schedule_data.name == self._raise_on:
            raise RuntimeError(f"db exploded on {schedule_data.name}")
        self.calls.append((agent_name, username, schedule_data))
        if self._returns is None:
            return {"id": f"sched_{len(self.calls)}"}
        return self._returns.pop(0)


def _install(monkeypatch, db):
    monkeypatch.setattr(system_service, "db", db)
    return db


def _sched(name, **over):
    base = {"name": name, "cron": "0 9 * * *", "message": f"/{name}"}
    base.update(over)
    return base


def _create(agent_names, agents_config):
    return system_service.create_schedules(
        agent_names=agent_names,
        agents_config=agents_config,
        owner_username=OWNER,
    )


# ------------------------------------------------------------- skip predicate

def test_agent_without_schedules_is_skipped(monkeypatch):
    db = _install(monkeypatch, _RecordingDb())
    count = _create({"a": "sys-a"}, {"a": SystemAgentConfig(template="local:x")})
    assert db.calls == []
    assert count == 0


def test_agent_with_empty_schedule_list_is_skipped(monkeypatch):
    """`not config.schedules` — an empty list is falsy, same as absent."""
    db = _install(monkeypatch, _RecordingDb())
    count = _create(
        {"a": "sys-a"}, {"a": SystemAgentConfig(template="local:x", schedules=[])}
    )
    assert db.calls == []
    assert count == 0


def test_agent_missing_from_name_map_is_skipped(monkeypatch):
    """The partial-deploy case: `created_map` omits agents that failed to create.

    `agents_config` still carries every agent from the manifest, so the skip is
    what keeps a failed agent's schedules from being written against a name
    that does not exist.
    """
    db = _install(monkeypatch, _RecordingDb())
    count = _create(
        {"a": "sys-a"},
        {
            "a": SystemAgentConfig(template="local:x", schedules=[_sched("keep")]),
            "gone": SystemAgentConfig(
                template="local:x", schedules=[_sched("dropped")]
            ),
        },
    )
    assert [c[2].name for c in db.calls] == ["keep"]
    assert count == 1


# --------------------------------------------------------- field construction

def test_schedule_create_field_mapping_and_defaults(monkeypatch):
    db = _install(monkeypatch, _RecordingDb())
    count = _create(
        {"a": "sys-a"},
        {"a": SystemAgentConfig(template="local:x", schedules=[_sched("nightly")])},
    )
    assert count == 1
    agent_name, username, payload = db.calls[0]
    assert agent_name == "sys-a"
    assert username == OWNER
    assert payload.name == "nightly"
    # The manifest key is `cron`; the model field is `cron_expression`.
    assert payload.cron_expression == "0 9 * * *"
    assert payload.message == "/nightly"
    # The three .get() defaults. `enabled=True` is the load-bearing one: a
    # manifest that merely lists a schedule starts autonomous executions.
    assert payload.enabled is True
    assert payload.timezone == "UTC"
    assert payload.description is None


def test_schedule_create_honours_explicit_optional_fields(monkeypatch):
    db = _install(monkeypatch, _RecordingDb())
    _create(
        {"a": "sys-a"},
        {
            "a": SystemAgentConfig(
                template="local:x",
                schedules=[
                    _sched(
                        "quiet",
                        enabled=False,
                        timezone="Europe/London",
                        description="a description",
                    )
                ],
            )
        },
    )
    payload = db.calls[0][2]
    assert payload.enabled is False
    assert payload.timezone == "Europe/London"
    assert payload.description == "a description"


def test_unknown_schedule_keys_are_ignored(monkeypatch):
    """Keys `create_schedules` does not read are dropped, not forwarded.

    ScheduleCreate has fields (timeout_seconds, model, allowed_tools,
    max_retries, ...) that the manifest path never populates, so they keep
    their model defaults regardless of what the manifest said.
    """
    db = _install(monkeypatch, _RecordingDb())
    _create(
        {"a": "sys-a"},
        {
            "a": SystemAgentConfig(
                template="local:x",
                schedules=[_sched("x", model="opus", timeout_seconds=99)],
            )
        },
    )
    payload = db.calls[0][2]
    assert payload.model is None
    assert payload.timeout_seconds is None


# ------------------------------------------------------- counting + ordering

def test_multiple_schedules_across_agents_count_and_order(monkeypatch):
    db = _install(monkeypatch, _RecordingDb())
    count = _create(
        {"a": "sys-a", "b": "sys-b"},
        {
            "a": SystemAgentConfig(
                template="local:x", schedules=[_sched("a1"), _sched("a2")]
            ),
            "b": SystemAgentConfig(template="local:x", schedules=[_sched("b1")]),
        },
    )
    assert [(c[0], c[2].name) for c in db.calls] == [
        ("sys-a", "a1"),
        ("sys-a", "a2"),
        ("sys-b", "b1"),
    ]
    assert count == 3


def test_falsy_db_return_is_not_counted_but_call_happened(monkeypatch):
    """`if schedule:` — a falsy return is logged and skipped, not raised.

    The count is therefore "schedules the DB confirmed", not "schedules the
    manifest asked for" — so a deploy can report fewer schedules than the
    manifest declares with no failure anywhere.
    """
    db = _install(monkeypatch, _RecordingDb(returns=[None, {"id": "ok"}]))
    count = _create(
        {"a": "sys-a"},
        {
            "a": SystemAgentConfig(
                template="local:x", schedules=[_sched("lost"), _sched("kept")]
            )
        },
    )
    assert [c[2].name for c in db.calls] == ["lost", "kept"]
    assert count == 1


def test_exception_propagates_and_prior_writes_stand(monkeypatch):
    """No internal try/except: the raise escapes to deploy_manifest step 9,
    which turns it into a warning. Anything already written stays written —
    there is no rollback of schedules."""
    db = _install(monkeypatch, _RecordingDb(raise_on="boom"))
    with pytest.raises(RuntimeError, match="db exploded on boom"):
        _create(
            {"a": "sys-a"},
            {
                "a": SystemAgentConfig(
                    template="local:x",
                    schedules=[_sched("first"), _sched("boom"), _sched("never")],
                )
            },
        )
    assert [c[2].name for c in db.calls] == ["first"]
