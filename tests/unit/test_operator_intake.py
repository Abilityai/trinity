"""trinity-enterprise#38 — operator intake service.

The intake is an explicit opt-in, fire-and-forget, once-per-install submission
of the operator's email + company to a hosted endpoint. These tests pin the
guarantees that matter: it never fires unless enabled, never double-submits,
never raises, never fires without an email, and — #1593 — never silently drops a
delivery failure (a non-2xx logs at WARNING, a connect failure at INFO) nor
leaks the operator's email into a log line.
"""
import asyncio
import logging

import pytest

pytestmark = pytest.mark.unit

import services.operator_intake_service as ois

_LOGGER_NAME = "services.operator_intake_service"


class FakeDB:
    def __init__(self, settings=None):
        self.settings = dict(settings or {})

    def get_setting_value(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


class FakeResp:
    """httpx.Response stand-in with a controllable status + .json() behavior."""

    _SENTINEL = object()

    def __init__(self, status=200, json_body=_SENTINEL, json_raises=None):
        self.status_code = status
        self._json_body = json_body
        self._json_raises = json_raises

    def json(self):
        if self._json_raises is not None:
            raise self._json_raises
        if self._json_body is FakeResp._SENTINEL:
            # Mirror httpx: an empty/non-JSON body raises on .json().
            raise ValueError("no json body")
        return self._json_body


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


def _client_returning(resp):
    """Build an async-context client class whose .post() returns `resp`."""

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            return resp

    return _Client


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


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING]


def test_submits_on_consent_with_expected_payload(fake_db, monkeypatch, caplog):
    _enable(monkeypatch)
    monkeypatch.setattr(ois.httpx, "AsyncClient", RecordingClient)
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)

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
    # E4: a true 2xx must not emit a WARNING, and never the email.
    assert not _warnings(caplog)
    assert "me@acme.com" not in caplog.text


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


def test_4xx_logs_warning_with_status(fake_db, monkeypatch, caplog):
    """#1593 test 1 — a 404 surfaces at WARNING with the status; marker unchanged."""
    _enable(monkeypatch)
    monkeypatch.setattr(ois.httpx, "AsyncClient", _client_returning(FakeResp(404)))
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)

    asyncio.run(ois.submit_operator_intake(email="me@acme.com"))

    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "404" in caplog.text
    # Marker is deliberately NOT rolled back (at-most-once).
    assert fake_db.settings["operator_intake_submitted"] == "true"
    assert "me@acme.com" not in caplog.text


def test_3xx_is_warning_not_false_success(fake_db, monkeypatch, caplog):
    """#1593 test 2 (E2) — a 3xx redirect is a WARNING, not the INFO "submitted" line."""
    _enable(monkeypatch)
    monkeypatch.setattr(ois.httpx, "AsyncClient", _client_returning(FakeResp(302)))
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)

    asyncio.run(ois.submit_operator_intake(email="me@acme.com"))

    assert len(_warnings(caplog)) == 1
    assert "302" in caplog.text
    # Regression guard: the redirect must NOT hit the "submitted" success line.
    assert "submitted" not in caplog.text


def test_coded_error_surfaced_in_warning(fake_db, monkeypatch, caplog):
    """#1593 test 4 (Decision 1=C) — a coded {ok:false,error} is echoed."""
    _enable(monkeypatch)
    resp = FakeResp(400, json_body={"ok": False, "error": "rate_limited"})
    monkeypatch.setattr(ois.httpx, "AsyncClient", _client_returning(resp))
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)

    asyncio.run(ois.submit_operator_intake(email="me@acme.com"))

    assert len(_warnings(caplog)) == 1
    assert "400" in caplog.text
    assert "rate_limited" in caplog.text


def test_non_json_body_status_only_no_fallthrough(fake_db, monkeypatch, caplog):
    """#1593 test 5 — a body whose .json() raises → status-only WARNING, no fall-through."""
    _enable(monkeypatch)
    resp = FakeResp(404, json_raises=ValueError("not json"))
    monkeypatch.setattr(ois.httpx, "AsyncClient", _client_returning(resp))
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)

    asyncio.run(ois.submit_operator_intake(email="me@acme.com"))

    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "404" in caplog.text
    # Must NOT have fallen through to the outer-except INFO "skipped" line.
    assert "skipped (ignored)" not in caplog.text


@pytest.mark.parametrize("body", [[], "gateway error", 42])
def test_non_dict_json_body_status_only(fake_db, monkeypatch, caplog, body):
    """#1593 test 6 (E1) — a non-dict JSON body degrades to a status-only WARNING."""
    _enable(monkeypatch)
    resp = FakeResp(404, json_body=body)
    monkeypatch.setattr(ois.httpx, "AsyncClient", _client_returning(resp))
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)

    asyncio.run(ois.submit_operator_intake(email="me@acme.com"))

    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "404" in caplog.text
    # A string body must not be echoed into the log.
    assert "gateway error" not in caplog.text


def test_pii_shaped_error_dropped(fake_db, monkeypatch, caplog):
    """#1593 test 7 (Sec1) — an error carrying an email is dropped, not logged."""
    _enable(monkeypatch)
    resp = FakeResp(400, json_body={"error": "invalid email: me@acme.com"})
    monkeypatch.setattr(ois.httpx, "AsyncClient", _client_returning(resp))
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)

    asyncio.run(ois.submit_operator_intake(email="me@acme.com"))

    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "400" in caplog.text
    assert "invalid email" not in caplog.text
    assert "me@acme.com" not in caplog.text


def test_network_failure_swallowed_and_marker_still_claimed(fake_db, monkeypatch, caplog):
    """#1593 test 8 (E3) — a raised connect failure surfaces at INFO, not WARNING."""
    _enable(monkeypatch)
    monkeypatch.setattr(ois.httpx, "AsyncClient", BoomClient)
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)

    # Must not raise — fire-and-forget.
    asyncio.run(ois.submit_operator_intake(email="me@acme.com"))

    # Claimed before the POST → no re-send on a later attempt (at-most-once).
    assert fake_db.settings["operator_intake_submitted"] == "true"
    # The outage-that-raises class is visible once, at INFO (prod root=INFO).
    infos = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "skipped (ignored)" in r.getMessage()
    ]
    assert len(infos) == 1
    assert not _warnings(caplog)
    assert "me@acme.com" not in caplog.text


def test_installation_id_created_once_and_stable(fake_db):
    a = ois.get_or_create_installation_id()
    b = ois.get_or_create_installation_id()
    assert a == b
    assert fake_db.settings["installation_id"] == a
