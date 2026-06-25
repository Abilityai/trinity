"""
Unit tests for WhatsApp outbound media attachments (#1315).

The WhatsApp adapter delivers `ChannelResponse.files` to users by persisting
each file into FILES-001 storage (`create_share_from_bytes`) and handing Twilio
the public `?sig=` URL as a `MediaUrl`. Text is always sent first; files that
can't be delivered as media degrade to a download link appended to the text.

Covers:
- `_outbound_media_cap_for_mime` MIME→cap classification (pure logic)
- `_prepare_outbound_media`: gate-off, happy path, no-public-URL / unsupported /
  oversized text-link fallback, per-file isolation on HTTPException
- `send_response`: text-first ordering, empty-text + file, persist-failure keeps
  text, multi-file fan-out, gate-off keeps text
- `_send_message`: MediaUrl payload construction (body omitted when empty)
- `create_share_from_bytes` service entry: flag gate (403), expiry bounds (400),
  delegation to the shared persist helper

Modules:
- src/backend/adapters/whatsapp_adapter.py
- src/backend/services/agent_shared_files_service.py
Issue: https://github.com/abilityai/trinity/issues/1315

Env + sys.path are configured by tests/unit/conftest.py.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

import adapters.whatsapp_adapter as wa_mod
from adapters.whatsapp_adapter import (
    WhatsAppAdapter,
    _WA_DOC_CAP_BYTES,
    _WA_MEDIA_CAP_BYTES,
    _OUTBOUND_MEDIA_EXPIRES_IN,
    _outbound_media_cap_for_mime,
)
from adapters.base import ChannelResponse, OutboundFile

AGENT = "wa-agent"
_PUBLIC_URL = "https://trinity.example.com/api/files/abc-123?sig=tok"


def _binding():
    return {"from_number": "whatsapp:+14155238886", "messaging_service_sid": None}


def _share(url=_PUBLIC_URL, mime="text/csv", size=1234):
    """Mimic the create_share_from_bytes return dict."""
    return {"file_id": "abc-123", "url": url, "mime_type": mime, "size_bytes": size,
            "expires_at": "2026-01-01T00:00:00+00:00"}


def _file(name="response_1.csv", content=b"a,b,c\n1,2,3\n", lang="csv"):
    return OutboundFile(filename=name, content=content, language=lang)


def _response(text="here you go", files=None):
    return ChannelResponse(
        text=text,
        files=files or [],
        metadata={"bot_token": "AC0000:authtoken", "agent_name": AGENT},
    )


# ---------------------------------------------------------------------------
# _outbound_media_cap_for_mime — pure classification
# ---------------------------------------------------------------------------

class TestMediaCapForMime:
    @pytest.mark.parametrize("mime,expected", [
        ("text/csv", _WA_DOC_CAP_BYTES),
        ("text/plain", _WA_DOC_CAP_BYTES),
        ("text/plain; charset=utf-8", _WA_DOC_CAP_BYTES),  # params stripped
        ("TEXT/CSV", _WA_DOC_CAP_BYTES),                    # case-insensitive
        ("application/json", _WA_DOC_CAP_BYTES),
        ("application/xml", _WA_DOC_CAP_BYTES),
        ("application/pdf", _WA_DOC_CAP_BYTES),
        ("application/x-yaml", _WA_DOC_CAP_BYTES),
        ("image/png", _WA_MEDIA_CAP_BYTES),
        ("image/jpeg", _WA_MEDIA_CAP_BYTES),
        ("audio/mpeg", _WA_MEDIA_CAP_BYTES),
        ("video/mp4", _WA_MEDIA_CAP_BYTES),
    ])
    def test_supported_types(self, mime, expected):
        assert _outbound_media_cap_for_mime(mime) == expected

    @pytest.mark.parametrize("mime", [
        "application/octet-stream",  # unknown / magic failed
        "",
        None,
        "application/zip",
        "application/x-tar",
    ])
    def test_unsupported_types(self, mime):
        assert _outbound_media_cap_for_mime(mime) is None

    def test_media_cap_below_doc_cap(self):
        # image/audio/video (~5MB) are stricter than documents (~16MB)
        assert _WA_MEDIA_CAP_BYTES < _WA_DOC_CAP_BYTES


# ---------------------------------------------------------------------------
# _prepare_outbound_media — persist + classify
# ---------------------------------------------------------------------------

class TestPrepareOutboundMedia:
    def setup_method(self):
        self.adapter = WhatsAppAdapter()

    def test_no_files_short_circuits(self):
        with patch.object(wa_mod.db, "get_file_sharing_enabled") as gate:
            media, fallback = self.adapter._prepare_outbound_media(AGENT, [])
        assert media == [] and fallback == []
        gate.assert_not_called()

    def test_gate_off_skips_all(self):
        with patch.object(wa_mod.db, "get_file_sharing_enabled", return_value=False), \
             patch.object(wa_mod, "create_share_from_bytes") as mint:
            media, fallback = self.adapter._prepare_outbound_media(AGENT, [_file()])
        assert media == [] and fallback == []
        mint.assert_not_called()  # never persist when file sharing is disabled

    def test_happy_path_media_url(self):
        with patch.object(wa_mod.db, "get_file_sharing_enabled", return_value=True), \
             patch.object(wa_mod, "create_share_from_bytes", return_value=_share()) as mint:
            media, fallback = self.adapter._prepare_outbound_media(AGENT, [_file()])
        assert media == [_PUBLIC_URL]
        assert fallback == []
        # minted with the short outbound TTL and agent-as-creator
        _, kwargs = mint.call_args
        assert kwargs["expires_in"] == _OUTBOUND_MEDIA_EXPIRES_IN
        assert kwargs["created_by"] == AGENT
        assert kwargs["display_name"] == "response_1.csv"

    def test_relative_url_falls_back_to_link(self):
        rel = _share(url="/api/files/abc-123?sig=tok")  # public_chat_url unset
        with patch.object(wa_mod.db, "get_file_sharing_enabled", return_value=True), \
             patch.object(wa_mod, "create_share_from_bytes", return_value=rel):
            media, fallback = self.adapter._prepare_outbound_media(AGENT, [_file()])
        assert media == []
        assert fallback == [("response_1.csv", "/api/files/abc-123?sig=tok")]

    def test_non_https_url_falls_back_to_link(self):
        http = _share(url="http://trinity.example.com/api/files/x?sig=tok")
        with patch.object(wa_mod.db, "get_file_sharing_enabled", return_value=True), \
             patch.object(wa_mod, "create_share_from_bytes", return_value=http):
            media, fallback = self.adapter._prepare_outbound_media(AGENT, [_file()])
        assert media == []
        assert fallback and fallback[0][0] == "response_1.csv"

    def test_unsupported_mime_falls_back_to_link(self):
        zip_share = _share(mime="application/octet-stream")
        with patch.object(wa_mod.db, "get_file_sharing_enabled", return_value=True), \
             patch.object(wa_mod, "create_share_from_bytes", return_value=zip_share):
            media, fallback = self.adapter._prepare_outbound_media(AGENT, [_file()])
        assert media == []
        assert fallback == [("response_1.csv", _PUBLIC_URL)]

    def test_oversized_falls_back_to_link(self):
        big = _share(mime="image/png", size=_WA_MEDIA_CAP_BYTES + 1)
        with patch.object(wa_mod.db, "get_file_sharing_enabled", return_value=True), \
             patch.object(wa_mod, "create_share_from_bytes", return_value=big):
            media, fallback = self.adapter._prepare_outbound_media(AGENT, [_file()])
        assert media == []
        assert fallback == [("response_1.csv", _PUBLIC_URL)]

    def test_httpexception_isolated_per_file(self):
        # File 1 raises (quota), file 2 succeeds — file 2 must still be delivered.
        def mint(agent, content, **kwargs):
            if kwargs["display_name"] == "bad.csv":
                raise HTTPException(status_code=413, detail="QUOTA_EXCEEDED")
            return _share()

        with patch.object(wa_mod.db, "get_file_sharing_enabled", return_value=True), \
             patch.object(wa_mod, "create_share_from_bytes", side_effect=mint):
            media, fallback = self.adapter._prepare_outbound_media(
                AGENT, [_file(name="bad.csv"), _file(name="good.csv")]
            )
        assert media == [_PUBLIC_URL]   # the good file survived
        assert fallback == []           # the rejected file is dropped, not linked

    def test_unexpected_exception_isolated(self):
        with patch.object(wa_mod.db, "get_file_sharing_enabled", return_value=True), \
             patch.object(wa_mod, "create_share_from_bytes", side_effect=RuntimeError("boom")):
            media, fallback = self.adapter._prepare_outbound_media(AGENT, [_file()])
        assert media == [] and fallback == []  # swallowed, never raises

    def test_multi_file(self):
        with patch.object(wa_mod.db, "get_file_sharing_enabled", return_value=True), \
             patch.object(wa_mod, "create_share_from_bytes",
                          side_effect=[_share(url="https://h/1?sig=a"),
                                       _share(url="https://h/2?sig=b")]):
            media, fallback = self.adapter._prepare_outbound_media(
                AGENT, [_file(name="a.csv"), _file(name="b.csv")]
            )
        assert media == ["https://h/1?sig=a", "https://h/2?sig=b"]
        assert fallback == []


# ---------------------------------------------------------------------------
# send_response — orchestration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSendResponse:
    def setup_method(self):
        self.adapter = WhatsAppAdapter()

    async def _run(self, response, *, sharing=True, share_ret=None, share_exc=None):
        send = AsyncMock(return_value={"sid": "SM1"})
        mint_kwargs = {}
        if share_exc is not None:
            mint_kwargs["side_effect"] = share_exc
        else:
            mint_kwargs["return_value"] = share_ret if share_ret is not None else _share()
        with patch.object(wa_mod.db, "get_whatsapp_binding", return_value=_binding()), \
             patch.object(wa_mod.db, "get_file_sharing_enabled", return_value=sharing), \
             patch.object(wa_mod, "create_share_from_bytes", **mint_kwargs), \
             patch.object(WhatsAppAdapter, "_send_message", send):
            await self.adapter.send_response("whatsapp:+15551234567", response)
        return send

    async def test_text_then_media_ordering(self):
        send = await self._run(_response(text="hi", files=[_file()]))
        assert send.await_count == 2
        # first call = text (Body, no MediaUrl); second = media (MediaUrl, empty body)
        first = send.await_args_list[0].kwargs
        second = send.await_args_list[1].kwargs
        assert first["body"] == "hi" and first.get("media_url") is None
        assert second["media_url"] == _PUBLIC_URL and second["body"] == ""

    async def test_empty_text_with_file_sends_only_media(self):
        send = await self._run(_response(text="", files=[_file()]))
        assert send.await_count == 1
        assert send.await_args_list[0].kwargs["media_url"] == _PUBLIC_URL

    async def test_no_text_no_deliverable_files_sends_nothing(self):
        # gate off + empty text → nothing to send
        send = await self._run(_response(text="", files=[_file()]), sharing=False)
        send.assert_not_awaited()

    async def test_persist_failure_keeps_text(self):
        send = await self._run(
            _response(text="still here", files=[_file()]),
            share_exc=HTTPException(status_code=507, detail="INSUFFICIENT_STORAGE"),
        )
        assert send.await_count == 1
        assert send.await_args_list[0].kwargs["body"] == "still here"
        assert send.await_args_list[0].kwargs.get("media_url") is None

    async def test_no_public_url_appends_link_to_text(self):
        rel = _share(url="/api/files/x?sig=tok")
        send = await self._run(_response(text="report:", files=[_file()]), share_ret=rel)
        # only a text message — relative URL can't be a MediaUrl
        assert send.await_count == 1
        body = send.await_args_list[0].kwargs["body"]
        assert "/api/files/x?sig=tok" in body
        assert "report:" in body
        assert send.await_args_list[0].kwargs.get("media_url") is None

    async def test_gate_off_sends_text_only(self):
        send = await self._run(_response(text="just text", files=[_file()]), sharing=False)
        assert send.await_count == 1
        assert send.await_args_list[0].kwargs["body"] == "just text"

    async def test_multi_file_fan_out(self):
        share_ret = _share(url="https://h/f?sig=z")
        send = await self._run(
            _response(text="two files", files=[_file(name="a.csv"), _file(name="b.csv")]),
            share_ret=share_ret,
        )
        # 1 text + 2 media messages
        assert send.await_count == 3
        assert send.await_args_list[0].kwargs["body"] == "two files"
        assert all(c.kwargs["media_url"] == "https://h/f?sig=z"
                   for c in send.await_args_list[1:])

    async def test_missing_credentials_no_send(self):
        send = AsyncMock()
        resp = ChannelResponse(text="hi", metadata={"bot_token": "", "agent_name": AGENT})
        with patch.object(WhatsAppAdapter, "_send_message", send):
            await self.adapter.send_response("whatsapp:+1555", resp)
        send.assert_not_awaited()


# ---------------------------------------------------------------------------
# _send_message — Twilio payload construction
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSendMessagePayload:
    async def _post_data(self, **kwargs):
        captured = {}

        class _Resp:
            status_code = 201
            text = "{}"
            def json(self):
                return {"sid": "SM1"}

        class _Client:
            def __init__(self, *a, **k):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def post(self, url, data=None, auth=None):
                captured["url"] = url
                captured["data"] = data
                captured["auth"] = auth
                return _Resp()

        with patch.object(wa_mod.httpx, "AsyncClient", _Client):
            await WhatsAppAdapter._send_message(
                account_sid="AC1", auth_token="tok",
                from_number="whatsapp:+1444", messaging_service_sid=None,
                to_number="whatsapp:+1555", **kwargs,
            )
        return captured["data"], captured["auth"]

    async def test_text_only_no_media_url(self):
        data, auth = await self._post_data(body="hello")
        assert data["Body"] == "hello"
        assert "MediaUrl" not in data
        assert data["From"] == "whatsapp:+1444"
        assert auth == ("AC1", "tok")

    async def test_media_only_omits_empty_body(self):
        data, _ = await self._post_data(body="", media_url="https://h/x?sig=t")
        assert data["MediaUrl"] == "https://h/x?sig=t"
        assert "Body" not in data  # empty body omitted so media-only is valid

    async def test_messaging_service_sid_prefers_over_from(self):
        captured = {}

        class _Resp:
            status_code = 201
            text = "{}"
            def json(self):
                return {}

        class _Client:
            def __init__(self, *a, **k):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def post(self, url, data=None, auth=None):
                captured["data"] = data
                return _Resp()

        with patch.object(wa_mod.httpx, "AsyncClient", _Client):
            await WhatsAppAdapter._send_message(
                account_sid="AC1", auth_token="tok",
                from_number="whatsapp:+1444", messaging_service_sid="MG9",
                to_number="whatsapp:+1555", body="hi", media_url="https://h/x?sig=t",
            )
        assert captured["data"]["MessagingServiceSid"] == "MG9"
        assert "From" not in captured["data"]
        assert captured["data"]["MediaUrl"] == "https://h/x?sig=t"


# ---------------------------------------------------------------------------
# create_share_from_bytes — service entry point
# ---------------------------------------------------------------------------

class TestCreateShareFromBytes:
    def setup_method(self):
        import services.agent_shared_files_service as svc
        self.svc = svc

    def test_gate_off_raises_403(self):
        with patch.object(self.svc.db, "get_file_sharing_enabled", return_value=False), \
             patch.object(self.svc, "_persist_and_register") as persist:
            with pytest.raises(HTTPException) as ei:
                self.svc.create_share_from_bytes(AGENT, b"data", display_name="x.csv")
        assert ei.value.status_code == 403
        persist.assert_not_called()

    def test_bad_expiry_raises_400(self):
        with patch.object(self.svc.db, "get_file_sharing_enabled", return_value=True), \
             patch.object(self.svc, "_persist_and_register") as persist:
            with pytest.raises(HTTPException) as ei:
                self.svc.create_share_from_bytes(
                    AGENT, b"data", display_name="x.csv", expires_in=10**9,
                )
        assert ei.value.status_code == 400
        persist.assert_not_called()

    def test_happy_path_delegates_to_persist(self):
        sentinel = {"file_id": "f1", "url": _PUBLIC_URL}
        with patch.object(self.svc.db, "get_file_sharing_enabled", return_value=True), \
             patch.object(self.svc, "_persist_and_register", return_value=sentinel) as persist:
            out = self.svc.create_share_from_bytes(
                AGENT, b"a,b\n1,2\n", display_name="data.csv",
                expires_in=3600, created_by=AGENT,
            )
        assert out is sentinel
        _, kwargs = persist.call_args
        assert kwargs["basename"] == "data.csv"
        assert kwargs["display_name"] == "data.csv"
        assert kwargs["expires_in"] == 3600
        assert kwargs["created_by"] == AGENT
