"""The platform side of abilityai/trinity-enterprise#1176: an agent that is not
applying Trinity's managed hooks is named in one Operations alert, its refused
turns are not mistaken for an auth failure, and the Skills tab can say so.

* `services/managed_hooks_check_service.py` — reads each running agent's
  `/health` (`guardrails_registration == "skipped"`) inside the fleet-image
  cycle (#4483: same lease, same cadence) and files or ends ONE alert,
  `managed_hooks_skipped:fleet`, on the system agent's queue.
* `services/execution_classification.classify_switch_failure` — a typed
  `turn_refused` 403 is not a credential failure, so no push path walks the
  agent's subscriptions over a refusal.
* `services/public_chat_service.is_turn_refused` — the async status read knows
  the new refusal sentences.
* `services/skill_gate_map_service.HOOK_STATES` — carries `skipped`.
Related flow: docs/memory/feature-flows/agent-lifecycle.md (Managed-hooks report)
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit

from services import fleet_image_service as fis  # noqa: E402
from services import managed_hooks_check_service as mhc  # noqa: E402
from services import platform_alerts  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `_managed_hooks_fleet_world`, as `_route_census`
from _managed_hooks_fleet_world import Fleet, REAL_ALERT_AGENT_NAME  # noqa: E402

SYS = "trinity-system"


# ---------------------------------------------------------------------------
# The fleet check. `Fleet` stubs the reads of the fleet and runs the alert for
# real, on a queue of the test's own (`fleet.queue`): which silent agent holds
# the alert is read from the alert's pending row (#4778).
# ---------------------------------------------------------------------------

@pytest.fixture
def fleet(monkeypatch):
    return Fleet().install(monkeypatch)


def test_skipped_agents_are_named_in_one_alert(fleet):
    fleet.agent("alpha", "skipped").agent("beta", "ok").agent("gamma", "skipped")
    out = fleet.run()
    assert out["status"] == "skipped" and out["skipped"] == ["alpha", "gamma"]
    fleet.observe.assert_called_once()
    args, kwargs = fleet.observe.call_args
    assert args == (fleet.queue, mhc.ALERT_KIND, mhc.ALERT_KEY)
    assert kwargs["priority"] == "high"
    assert kwargs["context"]["agents"] == ["alpha", "gamma"]
    assert kwargs["title"] == "Guardrails are not running on 2 agents"
    assert kwargs["question"] == (
        "Guardrails are not running on 2 agents: alpha, gamma. Claude Code on these agents is "
        "using cached organization settings, so it skips Trinity's guardrails. If their Claude "
        "organization has managed settings, ask an Owner to add \"managedSourcesBehavior\": "
        "\"merge\", then have each agent's owner run one turn on it. If it has none, the file "
        "was written inside the agent: treat the agent as tampered with.")
    fleet.clear.assert_not_called()


def test_one_agent_reads_in_the_singular(fleet):
    fleet.agent("alpha", "skipped")
    fleet.run()
    kwargs = fleet.observe.call_args.kwargs
    assert kwargs["title"] == "Guardrails are not running on 1 agent"
    assert kwargs["question"].startswith(
        "Guardrails are not running on 1 agent: alpha. Claude Code on this agent is using cached "
        "organization settings")


def test_the_system_agent_is_checked_and_stopped_or_ephemeral_agents_are_not(fleet):
    fleet.agent(SYS, "skipped").agent("ghost", "skipped", ephemeral=True)
    fleet.agent("asleep", "skipped", state="stopped")
    out = fleet.run()
    assert out["skipped"] == [SYS]


def test_the_alert_ends_when_no_agent_is_skipped(fleet):
    fleet.agent("alpha", "ok").agent("beta", "missing")
    assert fleet.run()["status"] == "clear"
    fleet.clear.assert_called_once_with(fleet.queue, mhc.ALERT_KIND, mhc.ALERT_KEY)
    fleet.observe.assert_not_called()


def test_docker_unreadable_leaves_the_alert(fleet):
    fleet.states = None
    assert fleet.run()["status"] == "could_not_check"
    fleet.observe.assert_not_called()
    fleet.clear.assert_not_called()


def test_the_alert_lives_on_the_system_agents_queue():
    from db.agents import SYSTEM_AGENT_NAME
    assert REAL_ALERT_AGENT_NAME == SYSTEM_AGENT_NAME == SYS


def test_with_no_pending_alert_an_unreadable_agent_holds_nothing(fleet):
    """#4778: a fresh process used to let any silent agent hold the alert."""
    fleet.agent("alpha", "ok").agent("beta", None)
    out = fleet.run()
    assert out["status"] == "clear" and out["unchecked"] == ["beta"]


def test_an_unreadable_agent_the_alert_named_holds_it(fleet):
    fleet.agent("alpha", "skipped").agent("beta", "ok")
    fleet.run()
    fleet.health["alpha"] = None
    assert fleet.run()["status"] == "could_not_check"
    fleet.clear.assert_not_called()


def test_a_silent_agent_the_alert_named_stays_named_until_it_reads_clean(fleet):
    """X and Y named; X goes silent while Y still reads skipped: the alert keeps
    naming X. Then Y is fixed while X is still silent: the alert is not ended."""
    fleet.agent("x-agent", "skipped").agent("y-agent", "skipped")
    fleet.run()
    fleet.health["x-agent"] = None
    assert fleet.run()["status"] == "skipped"
    assert fleet.observe.call_args.kwargs["context"]["agents"] == ["x-agent", "y-agent"]
    fleet.health["y-agent"] = "ok"
    assert fleet.run()["status"] == "could_not_check"
    fleet.clear.assert_not_called()
    fleet.health["x-agent"] = "ok"
    assert fleet.run()["status"] == "clear"


def test_an_unreadable_agent_the_alert_never_named_does_not_hold_it(fleet):
    fleet.agent("alpha", "skipped").agent("beta", None)
    fleet.run()
    fleet.health["alpha"] = "ok"
    assert fleet.run()["status"] == "clear"
    fleet.clear.assert_called_once()


def test_run_once_never_raises(fleet, monkeypatch):
    async def boom():
        raise RuntimeError("docker exploded")
    monkeypatch.setattr(mhc, "_container_states", boom)
    assert fleet.run()["status"] == "could_not_check"


def test_the_health_read_maps_each_answer(monkeypatch):
    class _Resp:
        def __init__(self, status, body):
            self.status_code, self._body = status, body

        def json(self):
            if isinstance(self._body, Exception):
                raise self._body
            return self._body

    answers = {
        "skipped": _Resp(200, {"guardrails_registration": "skipped"}),
        "clean": _Resp(200, {"guardrails_registration": "ok"}),
        "old-image": _Resp(200, {"status": "healthy"}),          # predates the field: clean (G5)
        "down": _Resp(503, {}),
        "garbled": _Resp(200, ValueError("not json")),
        "weird": _Resp(200, {"guardrails_registration": 7}),
    }

    async def fake_get(name):
        return answers[name]
    monkeypatch.setattr(mhc, "_get_health", fake_get)
    read = {name: asyncio.run(mhc._read_registration(name)) for name in answers}
    assert read == {"skipped": "skipped", "clean": "ok", "old-image": "ok", "down": None,
                    "garbled": None, "weird": None}


def test_the_fleet_image_cycle_runs_the_check(monkeypatch):
    """The production wiring: the leased fleet cycle calls this check (and a
    failing check never breaks the image check)."""
    ran = []

    async def image_once():
        ran.append("image")
        return {"status": "current"}

    async def hooks_once():
        ran.append("hooks")
        raise RuntimeError("must not escape")
    monkeypatch.setattr(fis, "run_once", image_once)
    monkeypatch.setattr(mhc, "run_once", hooks_once)
    monkeypatch.setattr(fis, "_redis", lambda: None)
    result = asyncio.run(fis.FleetImageCheckService().run_cycle())
    assert ran == ["image", "hooks"] and result == {"status": "current"}


def test_the_kind_is_registered_under_a_reserved_prefix():
    from services import operator_queue_service as oqs
    kind = platform_alerts._kind(mhc.ALERT_KIND)
    assert kind.clears == platform_alerts.CLEARS_HOOK
    assert any(kind.prefix.startswith(p) for p in oqs.reserved_id_prefixes())
    # a new agent going skipped breaks a person's snooze
    assert kind.escalates({"agents": ["a", "b"]}, {"agents": ["a"]})
    assert not kind.escalates({"agents": ["a"]}, {"agents": ["a", "b"]})


# ---------------------------------------------------------------------------
# A refusal is not a credential failure
# ---------------------------------------------------------------------------

def _resp(status, body):
    r = MagicMock()
    r.status_code = status
    r.text = json.dumps(body)
    r.json.return_value = body
    return r


def test_a_typed_turn_refusal_never_walks_the_subscriptions():
    from services.execution_classification import classify_switch_failure
    from agent_server.services import managed_hooks
    body = {"detail": {"message": managed_hooks.REFUSED, "error_code": "turn_refused"}}
    assert classify_switch_failure(_resp(403, body)) is None


@pytest.mark.parametrize("body", [
    {"detail": "Forbidden"},
    {"detail": {"message": "invalid x-api-key", "error_code": "auth"}},
])
def test_any_other_403_is_still_an_auth_failure(body):
    from services.execution_classification import classify_switch_failure
    assert classify_switch_failure(_resp(403, body)) == "auth"


def _agent_texts():
    from agent_server.services import managed_hooks, turn_scope
    refusals = [
        managed_hooks.REFUSED,
        managed_hooks.schedule_refusal({"cause": "creator_not_owner_tier", "schedule": "Daily",
                                        "creator": "pat", "remedy": None}),
        managed_hooks.schedule_refusal({"cause": "creator_missing", "schedule": None,
                                        "creator": None, "remedy": None}),
        managed_hooks.schedule_refusal({"cause": "schedule_unreadable", "schedule": None,
                                        "creator": None, "remedy": None}),
    ]
    return refusals, turn_scope.COULD_NOT_CHECK.format(runtime=managed_hooks.RUNTIME_LABEL)


def test_no_new_text_reads_as_an_auth_failure():
    """SUB-003 matches auth words in the agent's text on ANY status."""
    from services.failure_classifier import is_auth_failure
    refusals, could_not_check = _agent_texts()
    for text in (*refusals, could_not_check):
        assert not is_auth_failure(text), text


def test_the_public_status_read_knows_every_refusal():
    from services import public_chat_service as pcs
    refusals, could_not_check = _agent_texts()
    for text in refusals:
        assert pcs.is_turn_refused(None, text), text
    assert not pcs.is_turn_refused(None, could_not_check)


# ---------------------------------------------------------------------------
# The Skills tab's hook state
# ---------------------------------------------------------------------------

def test_the_hook_states_carry_skipped():
    from services import skill_gate_map_service as sgm
    from agent_server.services import managed_hooks
    assert managed_hooks.SKIPPED in sgm.HOOK_STATES
