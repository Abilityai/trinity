"""#2696 — the `/stt` provider-error branch says WHY, instead of one opaque 422.

`transcribe_portal_audio` mapped every non-200 from ElevenLabs onto
`422 "Could not transcribe the audio"`. A missing endpoint permission, a rejected
key, exhausted credits, a provider rate limit and a bad audio container all read
identically, while the actionable status word sat in a backend WARNING. These
tests pin the replacement mapping — by EXECUTING `transcribe_portal_audio` with
the provider stubbed at `httpx.AsyncClient`, and by driving the pure classifier
over every documented status so an unrecognised one cannot regress to the old
string.

No real Redis, no real HTTP.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import httpx
import pytest

import services.stt_capability_service as stt
import services.tts_service as tts_service
from client_portal import service as portal_service
from client_portal.service import ClientPortalError

KEY = "sk_example_key_for_2696"
OPAQUE = "Could not transcribe the audio"


@pytest.fixture(autouse=True)
def _no_redis_fresh_cache(monkeypatch):
    monkeypatch.setattr(stt, "_redis", lambda: None)
    stt._local.clear(); stt._local_failures.clear(); stt._inflight.clear()
    yield
    stt._local.clear(); stt._local_failures.clear(); stt._inflight.clear()


def _body(status_word: str, message: str = "provider message") -> str:
    return json.dumps({"detail": {"status": status_word, "message": message}})


# ---- the mapping is total and named ----------------------------------------

@pytest.mark.parametrize("status,body,category,http_status", [
    (401, _body("missing_permissions"), stt.CATEGORY_PERMISSION, 503),
    (403, _body("missing_permissions"), stt.CATEGORY_PERMISSION, 503),
    (401, _body("invalid_api_key"), stt.CATEGORY_AUTH, 503),
    (401, "not json at all", stt.CATEGORY_AUTH, 503),
    (401, _body("quota_exceeded"), stt.CATEGORY_QUOTA, 503),
    (401, _body("free_users_not_allowed"), stt.CATEGORY_QUOTA, 503),
    (402, "", stt.CATEGORY_QUOTA, 503),
    (429, _body("too_many_concurrent_requests"), stt.CATEGORY_RATE_LIMIT, 429),
    (400, _body("invalid_audio", "File is corrupted"), stt.CATEGORY_AUDIO, 422),
    (415, "", stt.CATEGORY_AUDIO, 422),
    (422, json.dumps({"detail": [{"loc": ["file"], "msg": "x"}]}), stt.CATEGORY_AUDIO, 422),
    (500, "", stt.CATEGORY_PROVIDER, 502),
    (503, "", stt.CATEGORY_PROVIDER, 502),
    (418, "", stt.CATEGORY_UNKNOWN, 502),
    (301, "", stt.CATEGORY_UNKNOWN, 502),
])
def test_every_provider_status_lands_in_a_named_category(status, body, category, http_status):
    f = stt.classify_stt_failure(status, body)
    assert f.category == category
    assert f.http_status == http_status
    assert f.provider_status == status
    assert f.client_message and f.client_message != OPAQUE


def test_categories_are_distinguishable_by_their_client_sentence():
    """AC 1 + 2: permission ≠ audio, quota ≠ auth — in the words the user sees."""
    perm = stt.classify_stt_failure(401, _body("missing_permissions")).client_message
    auth = stt.classify_stt_failure(401, _body("invalid_api_key")).client_message
    quota = stt.classify_stt_failure(401, _body("quota_exceeded")).client_message
    audio = stt.classify_stt_failure(400, _body("invalid_audio")).client_message
    assert len({perm, auth, quota, audio}) == 4
    assert "permission" in perm
    assert "rejected" in auth
    assert "credits" in quota
    assert "recording" in audio


def test_rate_limit_maps_to_429_with_the_existing_retry_wording():
    f = stt.classify_stt_failure(429, "")
    assert f.http_status == 429
    assert f.client_message == "Too many voice messages just now — wait a moment and try again."


def test_client_message_never_carries_the_provider_body():
    secret_ish = _body("missing_permissions", "The API key you used (sk_live_abc) is missing speech_to_text")
    f = stt.classify_stt_failure(401, secret_ish)
    assert "sk_live_abc" not in f.client_message
    assert "speech_to_text" not in f.client_message
    assert f.detail == "missing_permissions"     # the operator half keeps the status word


def test_there_is_no_arm_that_returns_the_opaque_string():
    """AC 6, the regression pin: sweep every status the provider could answer
    and assert none reaches the old text. A new arm that defaulted to it would
    fail here before it failed in a user's hands."""
    for status in range(300, 600):
        for body in ("", "{}", _body("something_new")):
            assert stt.classify_stt_failure(status, body).client_message != OPAQUE


# ---- the endpoint executes the mapping -------------------------------------

def _stub_provider(monkeypatch, *, status: int, body: str):
    class _Resp:
        status_code = status
        text = body

        def json(self):
            return json.loads(body or "{}")

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return _Resp()

    # `transcribe_portal_audio` does `import httpx` inside the function, so the
    # module attribute is what it reads.
    monkeypatch.setattr(httpx, "AsyncClient", _Client)


def _call():
    return asyncio.run(portal_service.transcribe_portal_audio(
        "acme-bot", "client@example.com", "voice.webm", "audio/webm", b"x" * 4000))


@pytest.fixture
def past_the_gate(monkeypatch):
    """Roster hit, key present, capability already verified — the real call runs.
    (Seeding `capable` keeps the gate from probing the same stub the real call is
    about to hit; the probe's own behaviour is #2695's suite.)"""
    stt.store(KEY, stt.SttCapability(stt.VERDICT_CAPABLE))
    with patch.object(portal_service, "agent_on_roster", return_value=True), \
            patch.object(tts_service, "is_available", return_value=True), \
            patch("services.settings_service.settings_service.get_elevenlabs_api_key",
                  return_value=KEY):
        yield


@pytest.mark.parametrize("status,body,expect_http,expect_fragment", [
    (401, _body("missing_permissions"), 503, "speech-to-text permission"),
    (401, _body("quota_exceeded"), 503, "credits"),
    (401, _body("invalid_api_key"), 503, "rejected"),
    (429, "", 429, "wait a moment"),
    (400, _body("invalid_audio"), 422, "recording could not be read"),
    (500, "", 502, "please type instead"),
])
def test_endpoint_answers_with_the_category_message(monkeypatch, past_the_gate,
                                                    status, body, expect_http, expect_fragment):
    _stub_provider(monkeypatch, status=status, body=body)
    with pytest.raises(ClientPortalError) as exc:
        _call()
    assert exc.value.status_code == expect_http
    assert expect_fragment in exc.value.detail
    assert exc.value.detail != OPAQUE
    assert exc.value.status_code != 500       # AC 5: fail-soft, never a 500


def test_endpoint_remembers_the_failure_for_the_operator(monkeypatch, past_the_gate):
    _stub_provider(monkeypatch, status=401, body=_body("missing_permissions"))
    with pytest.raises(ClientPortalError):
        _call()
    last = stt.read_last_failure(KEY)
    assert last["category"] == stt.CATEGORY_PERMISSION
    assert last["provider_status"] == 401
    assert last["detail"] == "missing_permissions"
    assert last["at"] is not None
    # #2695's half still fires: a 401 teaches the capability cache.
    assert stt.read_cached(KEY).verdict == stt.VERDICT_REFUSED


def test_a_bad_recording_teaches_the_operator_but_not_the_capability_cache(monkeypatch, past_the_gate):
    _stub_provider(monkeypatch, status=400, body=_body("invalid_audio"))
    with pytest.raises(ClientPortalError) as exc:
        _call()
    assert exc.value.status_code == 422
    assert stt.read_last_failure(KEY)["category"] == stt.CATEGORY_AUDIO
    assert stt.read_cached(KEY).verdict == stt.VERDICT_CAPABLE   # the KEY is fine; the mic stays


def test_a_success_still_returns_the_transcript(monkeypatch, past_the_gate):
    _stub_provider(monkeypatch, status=200, body=json.dumps({"text": " hello "}))
    assert _call() == "hello"
    assert stt.read_last_failure(KEY) is None


# ---- the operator surface ----------------------------------------------------

def test_settings_state_carries_the_last_failure_for_admins_only_by_route(monkeypatch):
    from routers import settings as settings_router

    stt.record_live_failure(KEY, 401, _body("missing_permissions"))
    with patch("services.settings_service.settings_service.get_elevenlabs_api_key",
               return_value=KEY), \
            patch("services.settings_service.settings_service.elevenlabs_key_source",
                  return_value="override"), \
            patch("services.settings_service.settings_service.get_default_voice_id",
                  return_value=None):
        state = asyncio.run(settings_router._elevenlabs_settings_state_with_capability())

    assert state["stt_last_failure"]["category"] == stt.CATEGORY_PERMISSION
    assert state["stt_last_failure"]["detail"] == "missing_permissions"
    assert KEY not in json.dumps(state)


def test_the_portal_card_carries_no_operator_detail():
    """AC 4: the client payload never grows the provider's status word."""
    from client_portal.models import PortalAgentCard
    assert not any("stt_detail" in f or "last_failure" in f for f in PortalAgentCard.model_fields)
