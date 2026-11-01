"""#3533 — a chat is never titled with the title generator's own failure text.

An opener with no topic ("Run sleep 40, then say done.", "yo") made the model
answer about its own inability, and that answer was stored as the title. The
prompt now gives the model a defined reply for "no title" (`NONE`), and a
generation that is the sentinel or a known failure phrasing is dropped so the
derived fallback stands.

The model is stubbed throughout: these tests pin what the generator does with
a reply, never what a model picks for a given opener.
"""
from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def svc(monkeypatch):
    from client_portal import service
    monkeypatch.setattr(service, "_title_health", {
        "state": service.TITLE_HEALTH_UNKNOWN, "consecutive_failures": 0,
        "last_ok_at": None, "last_failure_at": None, "last_failure": None,
    })
    from services import model_gateway
    monkeypatch.setattr(service, "_resolve_title_auth",
                        lambda agent: model_gateway.Credential("api_key", "auto", model_gateway.MODEL_ON_API_KEY, "k"))
    return service


def _model_replies(monkeypatch, text):
    import httpx

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"content": [{"text": text}]}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


@pytest.mark.parametrize("opener, generation", [
    ("Run sleep 40, then say done.", "Unable to generate title from the provided instruction"),
    ("Run sleep 40, then say done.", "NONE"),
    ("yo", "Unclear Initial Contact"),
    ("yo", "NONE"),
])
def test_a_topicless_opener_keeps_its_fallback_title(svc, monkeypatch, opener, generation):
    _model_replies(monkeypatch, generation)
    assert asyncio.run(svc._generate_thread_title("scribe", opener, "")) is None
    # The generator answered; it is healthy. Three greetings in a row must not
    # read as a failing generator on the Settings panel.
    h = svc.title_generation_health()
    assert h["state"] == "ok" and h["consecutive_failures"] == 0


@pytest.mark.parametrize("title", [
    "NONE", "None", "none", "(NONE)", "NONE!", "<NONE",
    "NONE - no topic named",
    "Unable to generate title",
    "I can't generate a title for this",
    "Cannot create a title for this message",
    "Can't determine topic",
    "Could not identify a topic",
    "Unclear initial contact",
    "Unclear request",
    "No topic provided",
    "No clear topic",
    "No title",
    "Untitled conversation",
    "Untitled",
])
def test_failure_text_is_not_a_title(svc, title):
    assert svc._is_no_title(title) is True


@pytest.mark.parametrize("title", [
    "Q3 invoice discrepancy",
    "None of the invoices match",
    "Unclear invoice charges",
    "No topic modeling results",
    "Untitled Goose Game bug",
    "Nonexistent user login error",
    "Unable to log in after reset",
    "Cannot export report to PDF",
    "Cannot update the page title",
    "Could not load topic list",
    "Unable to access topic modeling results",
])
def test_a_real_title_is_kept(svc, monkeypatch, title):
    assert svc._is_no_title(title) is False
    _model_replies(monkeypatch, title)
    assert asyncio.run(svc._generate_thread_title("scribe", "MSG", "")) == title


def test_failure_text_cut_at_the_title_width_is_still_caught(svc):
    cut = svc._sanitize_title("Unable to generate title from the provided instruction, then say done.")
    assert len(cut) == svc._TITLE_MAX_CHARS and cut.endswith("…")
    assert svc._is_no_title(cut) is True


def test_an_empty_generation_is_still_a_failure(svc, monkeypatch):
    _model_replies(monkeypatch, "  **  ")
    assert asyncio.run(svc._generate_thread_title("scribe", "MSG", "")) is None
    assert svc.title_generation_health()["last_failure"] == "unusable generation"


def test_nothing_is_stored_and_the_one_retry_still_runs(svc, monkeypatch):
    _model_replies(monkeypatch, "NONE")
    writes = []
    monkeypatch.setattr(svc.db, "set_portal_session_title", lambda *a: writes.append(a) or True)
    asyncio.run(svc._title_thread_background("scribe", "sess-1", "yo", ""))
    assert writes == []
    # The row is as the first turn left it: derived title, no generated source.
    row = {"title": "yo", "title_source": None, "message_count": 2}
    history = [{"role": "user", "content": "yo"}, {"role": "assistant", "content": "Hello"}]
    assert svc._title_plan(row, history) == svc.TITLE_ATTEMPT_RETRY
    # One more exchange on record and the window is closed for good.
    assert svc._title_plan({**row, "message_count": 4}, history) is None


def test_both_prompts_define_the_no_title_reply(svc):
    for tpl in (svc._TITLE_PROMPT, svc._TITLE_PROMPT_OPENER):
        assert f"output exactly {svc._NO_TITLE_SENTINEL}" in tpl
