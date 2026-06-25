"""trinity-enterprise#38 — operator intake service.

The intake is an explicit opt-in, fire-and-forget, once-per-install submission
of the operator's email + company to a hosted endpoint. These tests pin the
guarantees that matter: it never fires unless enabled, never double-submits,
never raises, and never fires without an email.
"""
import asyncio

import pytest

pytestmark = pytest.mark.unit

import services.operator_intake_service as ois


class FakeDB:
    def __init__(self, settings=None):
        self.settings = dict(settings or {})

    def get_setting_value(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


class FakeResp:
    def __init__(self, status=200):
        self.status_code = status


class RecordingClient:
    """Async-context httpx.AsyncClient stand-in that records POSTs."""
    posted = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        RecordingClient.posted.append((url, json))
        return FakeResp(200)


class BoomClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        raise RuntimeError("network down")


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(ois, "db", db)
    return db


@pytest.fixture(autouse=True)
def reset_recording():
    RecordingClient.posted = []
    yield
    RecordingClient.posted = []


def _enable(monkeypatch, on=True):
    monkeypatch.setattr(ois, "OPERATOR_INTAKE_ENABLED", on)
    monkeypatch.setattr(ois, "OPERATOR_INTAKE_URL", "https://intake.test/v1/operator-intake")


def test_submits_on_consent_with_expected_payload(fake_db, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(ois.httpx, "AsyncClient", RecordingClient)

    asyncio.run(ois.submit_operator_intake(email="me@acme.com", company="Acme"))

    assert len(RecordingClient.posted) == 1
    url, payload = RecordingClient.posted[0]
    assert url.endswith("/operator-intake")
    assert payload["email"] == "me@acme.com"
    assert payload["company"] == "Acme"
    assert payload["consent"] == "security_and_product_updates"
    assert payload["installation_id"]  # generated + non-empty
    # once-per-install marker claimed
    assert fake_db.settings["operator_intake_submitted"] == "true"


def test_disabled_never_submits(fake_db, monkeypatch):
    _enable(monkeypatch, on=False)
    monkeypatch.setattr(ois.httpx, "AsyncClient", RecordingClient)

    asyncio.run(ois.submit_operator_intake(email="me@acme.com"))

    assert RecordingClient.posted == []
    # No marker written — a later enable can still submit.
    assert "operator_intake_submitted" not in fake_db.settings


def test_idempotent_when_already_submitted(fake_db, monkeypatch):
    _enable(monkeypatch)
    fake_db.settings["operator_intake_submitted"] = "true"
    monkeypatch.setattr(ois.httpx, "AsyncClient", RecordingClient)

    asyncio.run(ois.submit_operator_intake(email="me@acme.com"))

    assert RecordingClient.posted == []


def test_no_email_no_submit(fake_db, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(ois.httpx, "AsyncClient", RecordingClient)

    asyncio.run(ois.submit_operator_intake(email=""))

    assert RecordingClient.posted == []


def test_network_failure_swallowed_and_marker_still_claimed(fake_db, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(ois.httpx, "AsyncClient", BoomClient)

    # Must not raise — fire-and-forget.
    asyncio.run(ois.submit_operator_intake(email="me@acme.com"))

    # Claimed before the POST → no re-send on a later attempt (at-most-once).
    assert fake_db.settings["operator_intake_submitted"] == "true"


def test_installation_id_created_once_and_stable(fake_db):
    a = ois.get_or_create_installation_id()
    b = ois.get_or_create_installation_id()
    assert a == b
    assert fake_db.settings["installation_id"] == a
