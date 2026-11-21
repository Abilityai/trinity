"""abilityai/trinity-enterprise#1329 — registration terms at first-run setup.

A generic seam (``services/setup_registration``): an optional module may
register a provider of registration terms. With none registered, setup and
``GET /api/setup/status`` behave exactly as before and the terms route is 404.
With one registered:

* ``GET /api/setup/registration-terms`` answers the provider's current
  ``{version, title, text, url}`` while setup is open (cached 60 s), 503
  ``registration_terms_unavailable`` when the provider cannot answer, and 404
  once setup completes. It takes no claim code and forwards nothing to the
  provider.
* first-run setup from an emailed link (the email-bound claim) must carry the
  accepted version; it is re-checked against the provider's CURRENT version at
  submit: missing → 400 ``registration_terms_required``, stale → 409
  ``registration_terms_changed``, provider down → 503
  ``registration_terms_unavailable`` (retryable). In every refusal the claim
  file is untouched and nothing is written.
* on success the acceptance ``{version, accepted_at, ip}`` (ip ≤ 64 chars) is
  kept for the provider's module to read, and (P2) the setup admin is recorded
  as having accepted the instance's own terms for the version in force.

Import isolation: ``routers.setup`` is imported lazily (see
test_setup_operator_profile.py for why).
"""
import asyncio
import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException

pytestmark = pytest.mark.unit

PW = "Sup3rSecret!!"
EMAIL = "owner@acme.com"
SECRET = "a1" * 32
IP = "198.51.100.7"
V1 = "2026-11-21-reg-1"
V2 = "2026-11-22-reg-2"
CODE = hmac.new(bytes.fromhex(SECRET), EMAIL.encode(), hashlib.sha256).hexdigest()


def _setup():
    import routers.setup as m
    return m


def _seam():
    import services.setup_registration as m
    return m


class FakeDB:
    def __init__(self):
        self.settings = {"setup_completed": "false"}
        self.users = {}
        self.password_writes = []
        self.acceptances = []

    def get_user_by_username(self, username):
        return self.users.get(username)

    def get_setting_value(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value

    def update_user_password(self, username, hashed, email=None):
        self.password_writes.append((username, hashed))
        row = self.users.setdefault(username, {"username": username, "id": 1})
        row["password"] = hashed
        if email is not None:
            row["email"] = email
        return True

    def update_user(self, username, updates):
        self.users.setdefault(username, {"username": username, "id": 1}).update(updates)
        return self.users[username]

    # instance terms (terms_service)
    def insert_terms_acceptance(self, user_id, email, version, at, door=None, actor_ip=None):
        if any(a["user_id"] == user_id and a["version"] == version for a in self.acceptances):
            return None
        self.acceptances.append({"user_id": user_id, "email": email, "version": version,
                                 "accepted_at": at, "door": door, "actor_ip": actor_ip})
        return len(self.acceptances)

    def get_terms_acceptance(self, user_id, version):
        return next((a for a in self.acceptances
                     if a["user_id"] == user_id and a["version"] == version), None)

    def get_terms_acceptance_by_email(self, email, version):
        return next((a for a in self.acceptances
                     if a["email"] == email and a["version"] == version), None)


class FakeProvider:
    def __init__(self, version=V1):
        self.version = version
        self.down = False
        self.calls = []

    async def current_terms(self):
        self.calls.append(())
        if self.down:
            raise ConnectionError("unreachable")
        return {"version": self.version, "title": "Registration terms",
                "text": "You agree to ...", "url": "https://example.com/terms"}


@pytest.fixture
def env(monkeypatch, tmp_path):
    from services import terms_service

    def _apply(claim=f"email-bound:{SECRET}\n", provider=None, ip=IP):
        setup, seam = _setup(), _seam()
        db = FakeDB()
        claim_file = tmp_path / "setup-claim"
        if claim is not None:
            claim_file.write_text(claim)
        monkeypatch.setenv("ADMIN_PASSWORD_SOURCE", "instance-id")
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
        monkeypatch.delenv("ADMIN_USERNAME", raising=False)
        monkeypatch.setattr(setup, "db", db)
        monkeypatch.setattr(seam, "db", db)
        monkeypatch.setattr(terms_service, "db", db)

        async def _no_audit(*a, **k):
            return None

        monkeypatch.setattr(terms_service, "_audit", _no_audit)
        monkeypatch.setattr(setup, "_SETUP_CLAIM_PATH", claim_file)
        monkeypatch.setattr(setup, "validate_password_strength", lambda p: [])
        monkeypatch.setattr(setup, "hash_password", lambda p: "hashed:" + p)
        monkeypatch.setattr(setup, "_get_client_ip", lambda request: ip)
        monkeypatch.setattr(setup, "check_login_rate_limit", lambda ip, account=None: None)
        monkeypatch.setattr(setup, "record_login_attempt", lambda ip, success, account=None: None)
        monkeypatch.setattr(setup, "allow_list_admin_email", lambda e: None)
        monkeypatch.setattr(setup, "mark_backfill_done", lambda: None)
        seam.clear_provider()
        if provider is not None:
            seam.register_provider(provider)
        return SimpleNamespace(db=db, claim_file=claim_file, provider=provider)

    yield _apply
    _seam().clear_provider()


def _post(version=None, claim_code=CODE, email=EMAIL):
    setup = _setup()
    data = setup.SetAdminPasswordRequest(
        password=PW, confirm_password=PW, email=email, claim_code=claim_code,
        registration_terms_version=version,
    )
    return asyncio.run(setup.set_admin_password(data, object(), BackgroundTasks()))


def _get_terms():
    return asyncio.run(_setup().get_registration_terms())


def _refused(e, status, code, **kw):
    with pytest.raises(HTTPException) as ei:
        _post(**kw)
    assert ei.value.status_code == status
    assert ei.value.detail["code"] == code
    assert e.claim_file.read_text() == f"email-bound:{SECRET}\n", "the claim is untouched"
    assert e.db.password_writes == [] and e.db.settings["setup_completed"] == "false"
    assert "registration_terms_acceptance" not in e.db.settings
    assert e.db.acceptances == []
    return ei.value.detail


# --------------------------------------------------------------------------
# no provider: inert
# --------------------------------------------------------------------------

def test_no_provider_setup_is_unchanged_and_records_nothing(env):
    e = env()
    out = _post()
    assert out == {"success": True, "email_registered": True, "username": "admin"}
    assert e.db.settings["setup_completed"] == "true"
    assert "registration_terms_acceptance" not in e.db.settings
    assert e.db.acceptances == [], "P2 waiver never fires on the self-installed path"


def test_no_provider_a_given_version_is_ignored(env):
    e = env()
    assert _post(version="anything")["success"] is True
    assert "registration_terms_acceptance" not in e.db.settings


def test_no_provider_terms_route_is_404(env):
    env()
    with pytest.raises(HTTPException) as ei:
        _get_terms()
    assert ei.value.status_code == 404


def test_status_answer_is_unchanged_with_a_provider(env):
    env(provider=FakeProvider())
    assert set(asyncio.run(_setup().get_setup_status())) == {
        "setup_completed", "setup_available", "claim_required"}


# --------------------------------------------------------------------------
# the terms route
# --------------------------------------------------------------------------

def test_terms_route_answers_the_provider_terms(env):
    env(provider=FakeProvider())
    assert _get_terms() == {"version": V1, "title": "Registration terms",
                            "text": "You agree to ...", "url": "https://example.com/terms"}


def test_terms_route_is_cached_for_60_seconds(env, monkeypatch):
    p = FakeProvider()
    env(provider=p)
    seam = _seam()
    now = [1000.0]
    monkeypatch.setattr(seam, "_now", lambda: now[0])
    _get_terms(); _get_terms()
    assert len(p.calls) == 1
    now[0] += 61
    p.version = V2
    assert _get_terms()["version"] == V2
    assert len(p.calls) == 2


def test_terms_route_unreachable_is_503_named_and_retryable(env):
    p = FakeProvider()
    p.down = True
    env(provider=p)
    with pytest.raises(HTTPException) as ei:
        _get_terms()
    assert ei.value.status_code == 503
    assert ei.value.detail["code"] == "registration_terms_unavailable"
    assert ei.value.detail["retryable"] is True


def test_terms_route_closes_once_setup_completes(env):
    e = env(provider=FakeProvider())
    e.db.settings["setup_completed"] = "true"
    with pytest.raises(HTTPException) as ei:
        _get_terms()
    assert ei.value.status_code == 404


def test_terms_route_never_takes_a_claim_or_request():
    import inspect
    assert list(inspect.signature(_setup().get_registration_terms).parameters) == [], \
        "the route reads nothing from the caller, so it can neither read nor forward a claim code"


def test_malformed_provider_answer_is_unavailable(env):
    class Bad(FakeProvider):
        async def current_terms(self):
            return {"version": "", "text": "x"}
    env(provider=Bad())
    with pytest.raises(HTTPException) as ei:
        _get_terms()
    assert ei.value.detail["code"] == "registration_terms_unavailable"


# --------------------------------------------------------------------------
# the submit gate
# --------------------------------------------------------------------------

def test_missing_version_is_refused_claim_untouched(env):
    e = env(provider=FakeProvider())
    d = _refused(e, 400, "registration_terms_required")
    assert d["version"] == V1


def test_stale_version_is_refused_with_the_current_version(env):
    e = env(provider=FakeProvider(version=V2))
    d = _refused(e, 409, "registration_terms_changed", version=V1)
    assert d["version"] == V2


def test_stale_cache_does_not_pass_a_changed_version(env):
    p = FakeProvider()
    e = env(provider=p)
    _get_terms()  # caches V1
    p.version = V2
    _refused(e, 409, "registration_terms_changed", version=V1)


def test_unreachable_provider_is_503_retryable_claim_untouched(env):
    p = FakeProvider()
    p.down = True
    e = env(provider=p)
    d = _refused(e, 503, "registration_terms_unavailable", version=V1)
    assert d["retryable"] is True


def test_wrong_claim_is_still_the_generic_403_before_any_provider_call(env):
    p = FakeProvider()
    env(provider=p)
    with pytest.raises(HTTPException) as ei:
        _post(version=V1, claim_code="0" * 64)
    assert ei.value.status_code == 403
    assert p.calls == []


def test_current_version_completes_setup_and_records_acceptance(env):
    e = env(provider=FakeProvider())
    assert _post(version=V1)["success"] is True
    assert e.db.settings["setup_completed"] == "true"
    assert e.claim_file.exists() is False
    rec = _seam().recorded_acceptance()
    assert rec["version"] == V1 and rec["ip"] == IP and rec["accepted_at"].endswith("Z")
    assert json.loads(e.db.settings["registration_terms_acceptance"]) == rec


def test_recorded_ip_is_capped_at_64_chars(env):
    env(provider=FakeProvider(), ip="f" * 100)
    _post(version=V1)
    assert len(_seam().recorded_acceptance()["ip"]) == 64


def test_instance_id_claim_is_not_gated(env):
    """Only the emailed link carries registration terms; the EC2 instance-ID
    claim (PROV-018) is a self-installed path and stays as it was."""
    p = FakeProvider()
    e = env(claim="i-0123456789abcdef0\n", provider=p)
    assert _post(claim_code="i-0123456789abcdef0", email="")["success"] is True
    assert p.calls == []
    assert "registration_terms_acceptance" not in e.db.settings
    assert e.db.acceptances == []


# --------------------------------------------------------------------------
# P2: the setup admin has accepted the instance's own terms
# --------------------------------------------------------------------------

def test_setup_admin_has_accepted_instance_terms_in_force(env):
    from services import terms_service
    e = env(provider=FakeProvider())
    _post(version=V1)
    admin = SimpleNamespace(id=1, email=EMAIL)
    assert terms_service.decision(admin)["required"] is False
    assert e.db.acceptances[0]["door"] == "setup"


def test_invited_person_is_still_asked(env):
    from services import terms_service
    env(provider=FakeProvider())
    _post(version=V1)
    assert terms_service.decision(SimpleNamespace(id=2, email="invitee@acme.com"))["required"] is True


def test_a_later_instance_terms_version_asks_the_admin_again(env):
    from services import terms_service
    e = env(provider=FakeProvider())
    _post(version=V1)
    e.db.settings[terms_service.SETTING_VERSION] = "2027-01-01T00:00:00Z"
    assert terms_service.decision(SimpleNamespace(id=1, email=EMAIL))["required"] is True


# --------------------------------------------------------------------------
# provider load: single-flight + negative cache (review of #4698)
# --------------------------------------------------------------------------

class SlowProvider(FakeProvider):
    """Yields to the loop inside the fetch, so concurrent callers overlap."""

    async def current_terms(self):
        self.calls.append(())
        await asyncio.sleep(0.02)
        if self.down:
            raise ConnectionError("unreachable")
        return {"version": self.version, "title": None, "text": None, "url": None}


def _gather(n, fn):
    async def _run():
        return await asyncio.gather(*(fn() for _ in range(n)), return_exceptions=True)
    return asyncio.run(_run())


def test_concurrent_cold_calls_make_one_provider_call(env):
    p = SlowProvider()
    env(provider=p)
    setup = _setup()
    results = _gather(50, setup.get_registration_terms)
    assert all(isinstance(r, dict) and r["version"] == V1 for r in results)
    assert len(p.calls) == 1, f"50 concurrent cold calls made {len(p.calls)} provider calls"


def test_concurrent_cold_calls_on_a_down_provider_make_one_call(env):
    p = SlowProvider()
    p.down = True
    env(provider=p)
    results = _gather(20, _setup().get_registration_terms)
    assert all(isinstance(r, HTTPException) and r.status_code == 503 for r in results)
    assert len(p.calls) == 1


def test_a_down_provider_is_asked_at_most_once_per_negative_window(env, monkeypatch):
    p = FakeProvider()
    p.down = True
    env(provider=p)
    seam = _seam()
    assert 5 <= seam.NEGATIVE_TTL_S <= 10
    now = [1000.0]
    monkeypatch.setattr(seam, "_now", lambda: now[0])
    for _ in range(20):
        with pytest.raises(HTTPException) as ei:
            _get_terms()
        assert ei.value.status_code == 503
        assert ei.value.detail["code"] == "registration_terms_unavailable"
        assert ei.value.detail["retryable"] is True
    assert len(p.calls) == 1, "the unavailable answer is cached for the negative window"
    now[0] += seam.NEGATIVE_TTL_S + 0.1
    p.down = False
    assert _get_terms()["version"] == V1
    assert len(p.calls) == 2


def test_submit_fresh_read_bypasses_the_negative_cache(env, monkeypatch):
    p = FakeProvider()
    p.down = True
    e = env(provider=p)
    seam = _seam()
    monkeypatch.setattr(seam, "_now", lambda: 1000.0)
    with pytest.raises(HTTPException):
        _get_terms()  # negative-cached
    p.down = False
    _post(version=V1)  # the submit re-read asks the provider anyway
    assert e.db.settings["setup_completed"] == "true"
    assert len(p.calls) == 2


def test_submit_fresh_read_bypasses_a_warm_cache(env, monkeypatch):
    p = FakeProvider()
    env(provider=p)
    seam = _seam()
    monkeypatch.setattr(seam, "_now", lambda: 1000.0)
    _get_terms(); _get_terms()
    assert len(p.calls) == 1
    asyncio.run(seam.check_accepted(V1))
    assert len(p.calls) == 2, "fresh=True always asks the provider"


# --------------------------------------------------------------------------
# review warnings
# --------------------------------------------------------------------------

def test_p2_failure_never_fails_setup(env, monkeypatch):
    from services import terms_service
    e = env(provider=FakeProvider())

    async def _boom(*a, **k):
        raise terms_service.TermsError(409, "terms_version_stale", "stale")

    monkeypatch.setattr(terms_service, "accept", _boom)
    _post(version=V1)
    assert e.db.settings["setup_completed"] == "true"
    assert json.loads(e.db.settings["registration_terms_acceptance"])["version"] == V1


def test_p2_db_error_never_fails_setup(env, monkeypatch):
    from services import terms_service
    e = env(provider=FakeProvider())

    async def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(terms_service, "accept", _boom)
    _post(version=V1)
    assert e.db.settings["setup_completed"] == "true"


def test_acceptance_setting_is_reserved_while_a_provider_is_registered(env):
    from services import reserved_settings
    env(provider=FakeProvider())
    assert reserved_settings.managed_by("registration_terms_acceptance") is not None
    _seam().clear_provider()
    assert reserved_settings.managed_by("registration_terms_acceptance") is None


def test_failed_admin_write_leaves_no_acceptance_row(env):
    e = env(provider=FakeProvider())

    def _fail(*a, **k):
        raise RuntimeError("db down")

    e.db.update_user_password = _fail
    with pytest.raises(HTTPException) as ei:
        _post(version=V1)
    assert ei.value.status_code == 503
    assert "registration_terms_acceptance" not in e.db.settings, "nothing was changed"
    assert e.claim_file.read_text() == f"email-bound:{SECRET}\n"


# --------------------------------------------------------------------------
# a provider that says whether it applies. Eligibility that changes after the
# process started (another process writing a setting) left workers that had
# not registered a provider without the terms, so setup completed with none.
# The seam now asks the provider at request time; a provider without
# `applies()` behaves exactly as before.
# --------------------------------------------------------------------------

class GatedProvider(FakeProvider):
    def __init__(self, applies=False, **kw):
        super().__init__(**kw)
        self.applies_now = applies
        self.asked = 0

    def applies(self):
        self.asked += 1
        if isinstance(self.applies_now, Exception):
            raise self.applies_now
        return self.applies_now


def test_seam_advertises_that_it_asks_applies():
    assert _seam().SUPPORTS_APPLIES is True


def test_a_provider_that_does_not_apply_is_404_and_never_fetched(env):
    p = GatedProvider(applies=False)
    env(provider=p)
    with pytest.raises(HTTPException) as ei:
        _get_terms()
    assert ei.value.status_code == 404
    assert p.calls == [], "no provider fetch when it does not apply"
    assert p.asked == 1


def test_a_provider_that_starts_applying_after_registration_answers_the_terms(env):
    """Registered at start while it did not apply; a later change (made by
    another process) makes it apply. No re-registration."""
    p = GatedProvider(applies=False)
    env(provider=p)
    with pytest.raises(HTTPException):
        _get_terms()
    p.applies_now = True
    assert _get_terms()["version"] == V1


def test_a_provider_that_does_not_apply_leaves_setup_unchanged(env):
    p = GatedProvider(applies=False)
    e = env(provider=p)
    assert _post()["success"] is True
    assert "registration_terms_acceptance" not in e.db.settings
    assert p.calls == []


def test_a_provider_that_applies_gates_setup(env):
    e = env(provider=GatedProvider(applies=True))
    _refused(e, 400, "registration_terms_required")


def test_an_applies_that_raises_reads_as_not_applying(env):
    p = GatedProvider(applies=RuntimeError("db down"))
    env(provider=p)
    with pytest.raises(HTTPException) as ei:
        _get_terms()
    assert ei.value.status_code == 404
    assert p.calls == []


def test_active_provider_is_none_without_a_provider(env):
    env()
    assert _seam().active_provider() is None


def test_a_provider_without_applies_is_always_active(env):
    p = FakeProvider()
    env(provider=p)
    assert _seam().active_provider() is p
