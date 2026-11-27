"""abilityai/trinity-enterprise#1403 — accepting the registration terms at
first-run setup switches usage sharing on, when those terms say so.

The self-serve registration terms say: "Usage sharing is on by default, and
accepting these terms switches it on, unless you have already switched it off."
Setup recorded the acceptance and did nothing else, so sharing stayed off.

The rule driven here, through ``POST /api/setup/admin-password`` itself:

* the terms a provider serves may carry ``usage_sharing_on_accept: true``;
  only then does completing setup with them accepted switch sharing on;
* it goes through the platform's own switch (``set_consent``), so Settings →
  Usage sharing shows it on, the first-run step stops asking (it asks only
  while sharing is off), a share with the disclosed backfill is scheduled, and
  the change is audit-logged like the Settings route's, naming the door;
* never when the person or the server has said no (ent#1397's rule): switched
  off, "Don't ask again", or the ``DO_NOT_TRACK`` /
  ``TELEMETRY_SHARING_ENABLED=false`` kill switch;
* an instance with no provider, terms that do not say so, or the self-installed
  instance-ID form, is unchanged;
* a failure to switch it on never fails setup.

The telemetry service is the real one, on the same fake settings store setup
writes, so "on" here is what Settings reads.
"""
import asyncio
import sys
from pathlib import Path

import pytest

# Sibling import (the unit dir is not implicitly importable): the ent#1329 setup
# harness, reused so the two suites drive one fake of the same route.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ent1329_setup_registration_terms import (  # noqa: F401 — `env` is a fixture
    CODE, EMAIL, PW, SECRET, V1, FakeDB, FakeProvider, _post, _seam, _setup, env,
)

pytestmark = pytest.mark.unit


class SharingDB(FakeDB):
    """The setup fake plus what the telemetry service writes."""

    def delete_setting(self, key):
        self.settings.pop(key, None)

    def insert_setting_if_absent(self, key, value):
        if key in self.settings:
            return False
        self.settings[key] = value
        return True

    def first_autonomous_success_at(self):
        return None


class SaysSharingProvider(FakeProvider):
    def __init__(self, flag=True, **kw):
        super().__init__(**kw)
        self.flag = flag

    async def current_terms(self):
        terms = await super().current_terms()
        terms["usage_sharing_on_accept"] = self.flag
        return terms


@pytest.fixture
def sharing_env(env, monkeypatch):
    """`env` with one settings store shared by setup, the seam and the sharing
    service, the audit captured and the share spawn recorded."""
    import services.platform_audit_service as audit_mod
    import services.telemetry_sharing_service as sharing

    audits, spawns = [], []

    async def _log(**kw):
        audits.append(kw)

    monkeypatch.setattr(audit_mod.platform_audit_service, "log", _log)
    monkeypatch.setattr(sharing, "spawn_share", lambda **kw: spawns.append(kw))
    monkeypatch.setattr(sharing, "TELEMETRY_SHARING_ENABLED", True)

    def _apply(provider=None, claim=f"email-bound:{SECRET}\n", before=None):
        e = env(provider=provider, claim=claim)
        db = SharingDB()
        db.settings.update(e.db.settings)
        for mod in (_setup(), _seam(), sharing):
            monkeypatch.setattr(mod, "db", db)
        from services import terms_service
        monkeypatch.setattr(terms_service, "db", db)
        e.db = db
        if before is not None:
            before(sharing)
        e.sharing, e.audits, e.spawns = sharing, audits, spawns
        return e

    return _apply


def _consent_audits(e):
    return [a for a in e.audits if a.get("event_action") == "telemetry_sharing_consent"]


# --------------------------------------------------------------------------
# the terms say so, the person never chose: on
# --------------------------------------------------------------------------

def test_accepting_terms_that_say_so_leaves_sharing_on(sharing_env):
    e = sharing_env(provider=SaysSharingProvider())
    assert _post(version=V1)["success"] is True
    status = e.sharing.get_status()
    assert status["enabled"] is True, "Settings → Usage sharing reads it on straight after setup"
    assert status["consent_at"] and status["sharing_id"], "the platform's own switch ran"
    assert e.spawns == [{"backfill": True}], "the first share, with the disclosed history"


def test_the_change_is_audit_logged_naming_the_door(sharing_env):
    e = sharing_env(provider=SaysSharingProvider())
    _post(version=V1)
    [line] = _consent_audits(e)
    assert line["details"]["enabled"] is True
    assert line["details"]["via"] == "registration_terms"
    assert line["actor_user"].username == "admin"
    assert "sharing_id" not in line["details"], "the share id never enters the audit"


def test_the_first_run_step_does_not_ask_again(sharing_env):
    e = sharing_env(provider=SaysSharingProvider())
    _post(version=V1)
    flags = e.sharing.public_flags()
    # firstRunSteps.js: pending only while `!telemetryEnabled && !telemetryDismissed`.
    assert flags["telemetry_sharing_enabled"] is True


def test_switching_it_off_afterwards_works_as_today(sharing_env):
    e = sharing_env(provider=SaysSharingProvider())
    _post(version=V1)
    e.sharing.set_consent(False)
    assert e.sharing.get_status()["enabled"] is False
    assert e.sharing.get_status()["sharing_id"] is None


# --------------------------------------------------------------------------
# a person or the server said no: off, nothing written or audited
# --------------------------------------------------------------------------

@pytest.mark.parametrize("before", [
    pytest.param(lambda s: s.set_consent(False), id="switched-off"),
    pytest.param(lambda s: (s.set_consent(True), s.set_consent(False)), id="on-then-off"),
    pytest.param(lambda s: s.mark_ask_dismissed(), id="dont-ask-again"),
])
def test_a_refusal_on_record_keeps_sharing_off(sharing_env, before):
    e = sharing_env(provider=SaysSharingProvider(), before=before)
    assert _post(version=V1)["success"] is True
    assert e.sharing.get_status()["enabled"] is False
    assert e.sharing.get_status()["sharing_id"] is None, "no share id minted"
    assert _consent_audits(e) == [] and e.spawns == []


def test_the_kill_switch_wins(sharing_env, monkeypatch):
    e = sharing_env(provider=SaysSharingProvider())
    monkeypatch.setattr(e.sharing, "TELEMETRY_SHARING_ENABLED", False)
    assert _post(version=V1)["success"] is True
    assert e.db.settings.get("telemetry_sharing_enabled") is None, "nothing written"
    assert _consent_audits(e) == [] and e.spawns == []


def test_already_on_is_left_alone(sharing_env):
    e = sharing_env(provider=SaysSharingProvider(), before=lambda s: s.set_consent(True))
    consent_at = e.db.settings["telemetry_sharing_consent_at"]
    _post(version=V1)
    assert e.db.settings["telemetry_sharing_consent_at"] == consent_at
    assert _consent_audits(e) == [] and e.spawns == []


# --------------------------------------------------------------------------
# terms that do not say so, or no terms: unchanged
# --------------------------------------------------------------------------

@pytest.mark.parametrize("flag", [False, None, "true", 1], ids=["false", "absent", "string", "int"])
def test_terms_that_do_not_say_so_leave_sharing_as_it_was(sharing_env, flag):
    provider = SaysSharingProvider(flag=flag) if flag is not None else FakeProvider()
    e = sharing_env(provider=provider)
    assert _post(version=V1)["success"] is True, "a malformed flag never blocks setup"
    assert e.sharing.get_status()["enabled"] is False
    assert _consent_audits(e) == [] and e.spawns == []


def test_no_provider_leaves_sharing_as_it_was(sharing_env):
    e = sharing_env()
    assert _post()["success"] is True
    assert e.sharing.get_status()["enabled"] is False
    assert _consent_audits(e) == []


def test_the_instance_id_form_is_unchanged(sharing_env):
    e = sharing_env(provider=SaysSharingProvider(), claim="instance-id-xyz\n")
    assert _post(claim_code="instance-id-xyz", email="")["success"] is True
    assert e.sharing.get_status()["enabled"] is False
    assert _consent_audits(e) == []


def test_the_terms_route_says_whether_accepting_switches_sharing_on(sharing_env):
    sharing_env(provider=SaysSharingProvider())
    assert asyncio.run(_setup().get_registration_terms())["usage_sharing_on_accept"] is True


def test_terms_that_do_not_say_so_read_false_on_the_route(sharing_env):
    sharing_env(provider=SaysSharingProvider(flag="yes"))
    assert asyncio.run(_setup().get_registration_terms())["usage_sharing_on_accept"] is False


# --------------------------------------------------------------------------
# a failure never blocks setup
# --------------------------------------------------------------------------

def test_a_failing_switch_never_fails_setup(sharing_env, monkeypatch):
    e = sharing_env(provider=SaysSharingProvider())

    def _boom(*a, **k):
        raise RuntimeError("settings store down")

    monkeypatch.setattr(e.sharing, "set_consent", _boom)
    out = _post(version=V1)
    assert out["success"] is True
    assert e.db.settings["setup_completed"] == "true"


def test_a_failing_audit_never_fails_setup_and_sharing_stays_on(sharing_env, monkeypatch):
    import services.platform_audit_service as audit_mod
    e = sharing_env(provider=SaysSharingProvider())

    async def _boom(**kw):
        raise RuntimeError("audit down")

    monkeypatch.setattr(audit_mod.platform_audit_service, "log", _boom)
    assert _post(version=V1)["success"] is True
    assert e.sharing.get_status()["enabled"] is True
