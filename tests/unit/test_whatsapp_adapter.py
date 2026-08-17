"""
WhatsApp (Twilio) adapter unit tests (WHATSAPP-001, issue #299).

Covers:
- Phone masking in logs
- SSRF allowlist for media URLs
- parse_message edge cases (text, media, empty, non-Twilio media)
- Message splitting at Twilio's 1600-char WhatsApp limit
- Webhook URL computation
- MessageSid dedup ring
- URL reconstruction with X-Forwarded-Proto
- Twilio RequestValidator round-trip (signature match + empty-param case)
- Webhook transport: unknown secret returns 200; bad signature returns 403
"""

import asyncio
import os
import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Backend path setup (same pattern as test_telegram_login_gate.py)
_backend_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "src", "backend")
)
if _backend_path not in sys.path:
    sys.path.insert(0, _backend_path)

# Stub utils.helpers to avoid loading full backend utils
if "utils.helpers" not in sys.modules:
    _helpers = types.ModuleType("utils.helpers")
    _helpers.utc_now = lambda: datetime.utcnow()
    _helpers.utc_now_iso = lambda: datetime.utcnow().isoformat() + "Z"
    _helpers.to_utc_iso = lambda v: str(v)
    _helpers.parse_iso_timestamp = lambda s: datetime.fromisoformat(s.rstrip("Z"))
    sys.modules["utils.helpers"] = _helpers

# Stub `database` so adapter imports don't touch real SQLite
if "database" not in sys.modules:
    _database_stub = types.ModuleType("database")
    _database_stub.db = MagicMock()
    sys.modules["database"] = _database_stub


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# Override the autouse cleanup fixture from conftest.py so these pure unit tests
# don't require a live backend (conftest's cleanup_after_test pulls in api_client
# which authenticates on fixture setup).
@pytest.fixture(autouse=True)
def cleanup_after_test():  # noqa: F811 — intentional override
    yield


@pytest.fixture(scope="session")
def api_client():  # noqa: F811 — intentional override, never used here
    yield None


@pytest.fixture(scope="session")
def resource_tracker():  # noqa: F811 — intentional override, never used here
    class _NoopTracker:
        def cleanup(self, *args, **kwargs):
            pass
    yield _NoopTracker()


# =============================================================================
# Pure helpers — don't need DB / network stubbing
# =============================================================================

class TestPhoneMasking:
    @pytest.mark.unit
    def test_mask_whatsapp_e164(self):
        from adapters.whatsapp_adapter import _mask_phone
        masked = _mask_phone("whatsapp:+14155551234")
        assert "+14155551234" not in masked
        assert masked.endswith("1234")
        assert masked.startswith("whatsapp:+")

    @pytest.mark.unit
    def test_mask_raw_e164(self):
        from adapters.whatsapp_adapter import _mask_phone
        masked = _mask_phone("+14155551234")
        assert masked.endswith("1234")
        assert "+14155551234" not in masked

    @pytest.mark.unit
    def test_mask_empty(self):
        from adapters.whatsapp_adapter import _mask_phone
        assert _mask_phone("") == "<empty>"

    @pytest.mark.unit
    def test_mask_short_number(self):
        """Short numbers still get partial masking."""
        from adapters.whatsapp_adapter import _mask_phone
        masked = _mask_phone("+12345")
        assert "12345" not in masked


class TestTwilioMediaUrlAllowlist:
    """SSRF defense: CDN hosts allowed as redirect targets; credentialed/source
    hosts stay *.twilio.com only (#1932 two-tier allowlist)."""

    @pytest.mark.unit
    def test_accepts_api_twilio_com(self):
        from adapters.whatsapp_adapter import _is_twilio_media_url
        assert _is_twilio_media_url(
            "https://api.twilio.com/2010-04-01/Accounts/AC00/Messages/MM00/Media/ME00"
        )

    @pytest.mark.unit
    def test_accepts_subdomain_twilio_com(self):
        from adapters.whatsapp_adapter import _is_twilio_media_url
        assert _is_twilio_media_url("https://media.twilio.com/some/path")

    @pytest.mark.unit
    def test_rejects_non_twilio_host(self):
        from adapters.whatsapp_adapter import _is_twilio_media_url
        assert not _is_twilio_media_url("https://attacker.com/evil")

    @pytest.mark.unit
    def test_rejects_domain_suffix_spoof(self):
        """'eviltwilio.com' must NOT match '.twilio.com'."""
        from adapters.whatsapp_adapter import _is_twilio_media_url
        assert not _is_twilio_media_url("https://eviltwilio.com/evil")
        # And the 'api.twilio.com.evil.com' subdomain trick
        assert not _is_twilio_media_url("https://api.twilio.com.evil.com/evil")

    @pytest.mark.unit
    def test_rejects_http_scheme(self):
        """Only https allowed (defense-in-depth — Twilio media is always https)."""
        from adapters.whatsapp_adapter import _is_twilio_media_url
        assert not _is_twilio_media_url("http://api.twilio.com/path")

    @pytest.mark.unit
    def test_rejects_garbage_url(self):
        from adapters.whatsapp_adapter import _is_twilio_media_url
        assert not _is_twilio_media_url("not-a-url")
        assert not _is_twilio_media_url("")

    # --- #1932: the media CDN is a legitimate redirect target ---------------

    @pytest.mark.unit
    def test_accepts_mms_twiliocdn_com(self):
        """The host api.twilio.com actually 302s to — refused before #1932."""
        from adapters.whatsapp_adapter import _is_twilio_media_url
        assert _is_twilio_media_url(
            "https://mms.twiliocdn.com/Accounts/AC00/Messages/MM00/Media/ME00"
        )

    @pytest.mark.unit
    def test_accepts_media_twiliocdn_com(self):
        from adapters.whatsapp_adapter import _is_twilio_media_url
        assert _is_twilio_media_url("https://media.twiliocdn.com/x")

    @pytest.mark.unit
    def test_accepts_bare_apex(self):
        """Pins the `host == suffix.lstrip('.')` branch, not the endswith one."""
        from adapters.whatsapp_adapter import _is_twilio_media_url
        assert _is_twilio_media_url("https://twiliocdn.com/x")
        assert _is_twilio_media_url("https://twilio.com/x")

    @pytest.mark.unit
    def test_accepts_cdn_host_case_insensitively(self):
        from adapters.whatsapp_adapter import _is_twilio_media_url
        assert _is_twilio_media_url("https://MMS.TWILIOCDN.COM/x")

    # --- #1932: CDN-shaped spoofs must still be refused ---------------------

    @pytest.mark.unit
    def test_rejects_cdn_domain_suffix_spoof(self):
        """The leading dot anchors the suffix — no lookalike registrable domain."""
        from adapters.whatsapp_adapter import _is_twilio_media_url
        assert not _is_twilio_media_url("https://eviltwiliocdn.com/evil")
        assert not _is_twilio_media_url("https://twiliocdn.com.evil.com/evil")
        assert not _is_twilio_media_url("https://mms.twiliocdn.com.evil.com/evil")

    @pytest.mark.unit
    def test_rejects_http_scheme_on_cdn(self):
        from adapters.whatsapp_adapter import _is_twilio_media_url
        assert not _is_twilio_media_url("http://mms.twiliocdn.com/x")

    @pytest.mark.unit
    def test_rejects_userinfo_host_spoof(self):
        """`parsed.hostname` returns the real host, not the userinfo segment."""
        from adapters.whatsapp_adapter import _is_twilio_media_url
        assert not _is_twilio_media_url("https://mms.twiliocdn.com@evil.com/x")
        assert not _is_twilio_media_url("https://api.twilio.com@evil.com/x")

    @pytest.mark.unit
    def test_rejects_trailing_dot_fqdn(self):
        """Fail-closed: the absolute-FQDN form doesn't match the suffix."""
        from adapters.whatsapp_adapter import _is_twilio_media_url
        assert not _is_twilio_media_url("https://mms.twiliocdn.com./x")

    @pytest.mark.unit
    def test_rejects_punycode_lookalike(self):
        from adapters.whatsapp_adapter import _is_twilio_media_url
        assert not _is_twilio_media_url("https://xn--mms-twiliocdn.com/x")

    @pytest.mark.unit
    def test_rejects_scheme_relative_url(self):
        from adapters.whatsapp_adapter import _is_twilio_media_url
        assert not _is_twilio_media_url("//mms.twiliocdn.com/x")
        assert not _is_twilio_media_url("/relative/path")

    @pytest.mark.unit
    def test_rejects_s3_redirect_target(self):
        """D2 (#1932): S3 is deliberately NOT allowlisted.

        `s3-external-1.amazonaws.com` is path-style — allowlisting it, even as a
        single exact host, admits arbitrary buckets under an allowlisted name.
        Accounts without "HTTP Basic Authentication for media" enabled redirect
        there and are an unsupported configuration by decision, not oversight.
        """
        from adapters.whatsapp_adapter import _is_twilio_media_url
        assert not _is_twilio_media_url("https://s3-external-1.amazonaws.com/bucket/key")
        assert not _is_twilio_media_url("https://mybucket.s3.amazonaws.com/key")

    # --- #1932: the two tiers are genuinely different -----------------------

    @pytest.mark.unit
    def test_source_predicate_excludes_cdn(self):
        """D3: the CDN is fetchable as a redirect target but never credentialed."""
        from adapters.whatsapp_adapter import (
            _is_twilio_media_source_url,
            _is_twilio_media_url,
        )
        assert _is_twilio_media_url("https://mms.twiliocdn.com/x") is True
        assert _is_twilio_media_source_url("https://mms.twiliocdn.com/x") is False

    @pytest.mark.unit
    def test_source_predicate_accepts_api_host(self):
        from adapters.whatsapp_adapter import _is_twilio_media_source_url
        assert _is_twilio_media_source_url("https://api.twilio.com/x") is True

    @pytest.mark.unit
    def test_source_predicate_keeps_the_spoof_properties(self):
        """The narrow tier shares the matcher, so it inherits every guard."""
        from adapters.whatsapp_adapter import _is_twilio_media_source_url
        assert not _is_twilio_media_source_url("https://eviltwilio.com/evil")
        assert not _is_twilio_media_source_url("https://api.twilio.com.evil.com/evil")
        assert not _is_twilio_media_source_url("http://api.twilio.com/x")
        assert not _is_twilio_media_source_url("https://api.twilio.com@evil.com/x")
        assert not _is_twilio_media_source_url("")

    @pytest.mark.unit
    def test_allowlist_constants_are_the_documented_tiers(self):
        """Pins the named constants AC #2 and D3 both refer to."""
        from adapters.whatsapp_adapter import (
            _TWILIO_MEDIA_ALLOWED_HOST_SUFFIXES,
            _TWILIO_MEDIA_SOURCE_HOST_SUFFIXES,
        )
        assert _TWILIO_MEDIA_SOURCE_HOST_SUFFIXES == (".twilio.com",)
        assert _TWILIO_MEDIA_ALLOWED_HOST_SUFFIXES == (".twilio.com", ".twiliocdn.com")
        # The source tier must never grow past the redirect tier.
        assert set(_TWILIO_MEDIA_SOURCE_HOST_SUFFIXES) <= set(
            _TWILIO_MEDIA_ALLOWED_HOST_SUFFIXES
        )


class TestMessageSplit:
    @pytest.mark.unit
    def test_short_message_single_chunk(self):
        from adapters.whatsapp_adapter import WhatsAppAdapter
        chunks = WhatsAppAdapter._split_message("hello")
        assert chunks == ["hello"]

    @pytest.mark.unit
    def test_exactly_limit_single_chunk(self):
        from adapters.whatsapp_adapter import WhatsAppAdapter, TWILIO_WHATSAPP_MAX_LENGTH
        text = "x" * TWILIO_WHATSAPP_MAX_LENGTH
        chunks = WhatsAppAdapter._split_message(text)
        assert chunks == [text]

    @pytest.mark.unit
    def test_long_message_splits_at_paragraph(self):
        from adapters.whatsapp_adapter import WhatsAppAdapter, TWILIO_WHATSAPP_MAX_LENGTH
        para1 = "a" * (TWILIO_WHATSAPP_MAX_LENGTH - 100)
        para2 = "b" * 500
        text = f"{para1}\n\n{para2}"
        chunks = WhatsAppAdapter._split_message(text)
        assert len(chunks) == 2
        assert all(len(c) <= TWILIO_WHATSAPP_MAX_LENGTH for c in chunks)

    @pytest.mark.unit
    def test_worst_case_no_separators(self):
        """Single giant unbroken string still splits without losing content."""
        from adapters.whatsapp_adapter import WhatsAppAdapter, TWILIO_WHATSAPP_MAX_LENGTH
        text = "z" * (TWILIO_WHATSAPP_MAX_LENGTH * 3)
        chunks = WhatsAppAdapter._split_message(text)
        assert "".join(chunks) == text
        assert all(len(c) <= TWILIO_WHATSAPP_MAX_LENGTH for c in chunks)


class TestWebhookUrlCompute:
    @pytest.mark.unit
    def test_basic(self):
        from adapters.transports.twilio_webhook import compute_webhook_url
        assert (
            compute_webhook_url("https://public.example.com", "abc123")
            == "https://public.example.com/api/whatsapp/webhook/abc123"
        )

    @pytest.mark.unit
    def test_trims_trailing_slash(self):
        from adapters.transports.twilio_webhook import compute_webhook_url
        assert (
            compute_webhook_url("https://public.example.com/", "abc123")
            == "https://public.example.com/api/whatsapp/webhook/abc123"
        )

    @pytest.mark.unit
    def test_empty_public_url_returns_empty(self):
        from adapters.transports.twilio_webhook import compute_webhook_url
        assert compute_webhook_url("", "abc123") == ""


class TestDedupRing:
    @pytest.mark.unit
    def test_first_seen_returns_false(self):
        from adapters.transports.twilio_webhook import _seen_recently, _SEEN_MESSAGE_SIDS
        _SEEN_MESSAGE_SIDS.clear()
        assert _seen_recently("SM_new_msg_1") is False

    @pytest.mark.unit
    def test_second_time_returns_true(self):
        from adapters.transports.twilio_webhook import _seen_recently, _SEEN_MESSAGE_SIDS
        _SEEN_MESSAGE_SIDS.clear()
        _seen_recently("SM_dup_msg")
        assert _seen_recently("SM_dup_msg") is True

    @pytest.mark.unit
    def test_empty_sid_not_recorded(self):
        from adapters.transports.twilio_webhook import _seen_recently, _SEEN_MESSAGE_SIDS
        _SEEN_MESSAGE_SIDS.clear()
        assert _seen_recently("") is False
        assert "" not in _SEEN_MESSAGE_SIDS

    @pytest.mark.unit
    def test_eviction_when_over_cap(self):
        from adapters.transports.twilio_webhook import _seen_recently, _SEEN_MESSAGE_SIDS, _SEEN_MAX
        _SEEN_MESSAGE_SIDS.clear()
        # Fill to cap
        for i in range(_SEEN_MAX):
            _SEEN_MESSAGE_SIDS[f"SM{i}"] = 0.0
        # Insert one more → eviction kicks in
        _seen_recently("SM_new_after_cap")
        assert len(_SEEN_MESSAGE_SIDS) < _SEEN_MAX + 1
        assert "SM_new_after_cap" in _SEEN_MESSAGE_SIDS


class TestUrlReconstruction:
    """Ensures uvicorn --proxy-headers + nginx X-Forwarded-Proto handling."""

    @pytest.mark.unit
    def test_upgrades_http_to_https_when_forwarded_proto_set(self):
        from adapters.transports.twilio_webhook import _reconstruct_url
        req = MagicMock()
        req.url = "http://backend:8000/api/whatsapp/webhook/abc"
        req.headers = {"x-forwarded-proto": "https"}
        # MagicMock for url returns the string in str() — but fastapi.Request.url is a URL obj
        # that `str()`-ifies to the URL. Simulate via plain str:
        req.__class__ = type(req)
        req.url = "http://backend:8000/api/whatsapp/webhook/abc"
        # str(request.url) is what our code calls
        req.url = types.SimpleNamespace(__str__=lambda self="http://backend:8000/api/whatsapp/webhook/abc": "http://backend:8000/api/whatsapp/webhook/abc")
        # Replace with a plain string-like
        class FakeReq:
            url = "http://backend:8000/api/whatsapp/webhook/abc"
            headers = {"x-forwarded-proto": "https"}
        url = _reconstruct_url(FakeReq())
        assert url.startswith("https://")

    @pytest.mark.unit
    def test_preserves_https_when_already_https(self):
        from adapters.transports.twilio_webhook import _reconstruct_url
        class FakeReq:
            url = "https://public.example.com/api/whatsapp/webhook/abc"
            headers = {}
        assert _reconstruct_url(FakeReq()) == "https://public.example.com/api/whatsapp/webhook/abc"


# =============================================================================
# parse_message — no DB interaction needed
# =============================================================================

class TestParseMessage:
    def _adapter(self):
        from adapters.whatsapp_adapter import WhatsAppAdapter
        return WhatsAppAdapter()

    @pytest.mark.unit
    def test_text_only(self):
        raw = {
            "From": "whatsapp:+14155551234",
            "To": "whatsapp:+14155238886",
            "Body": "Hello Trinity",
            "MessageSid": "SM_abc123",
            "NumMedia": "0",
            "_agent_name": "my-agent",
            "_binding_id": 7,
        }
        msg = self._adapter().parse_message(raw)
        assert msg is not None
        assert msg.text == "Hello Trinity"
        assert msg.sender_id == "whatsapp:+14155551234"
        assert msg.channel_id == "whatsapp:+14155551234"
        assert msg.metadata["agent_name"] == "my-agent"
        assert msg.metadata["binding_id"] == 7
        assert msg.metadata["message_sid"] == "SM_abc123"
        assert msg.metadata["is_group"] is False
        assert msg.files == []

    @pytest.mark.unit
    def test_empty_body_and_no_media_returns_none(self):
        raw = {"From": "whatsapp:+14155551234", "To": "whatsapp:+14155238886", "Body": "", "NumMedia": "0"}
        assert self._adapter().parse_message(raw) is None

    @pytest.mark.unit
    def test_missing_from_returns_none(self):
        raw = {"Body": "hi", "NumMedia": "0"}
        assert self._adapter().parse_message(raw) is None

    @pytest.mark.unit
    def test_media_attached(self):
        raw = {
            "From": "whatsapp:+14155551234",
            "To": "whatsapp:+14155238886",
            "Body": "Look at this",
            "MessageSid": "SM_img1",
            "NumMedia": "1",
            "MediaUrl0": "https://api.twilio.com/2010-04-01/Accounts/AC00/Messages/MM00/Media/ME00",
            "MediaContentType0": "image/jpeg",
            "_agent_name": "my-agent",
            "_binding_id": 7,
        }
        msg = self._adapter().parse_message(raw)
        assert msg is not None
        assert len(msg.files) == 1
        assert msg.files[0].mimetype == "image/jpeg"
        assert msg.files[0].name.startswith("media_0")
        assert msg.files[0].url.startswith("https://api.twilio.com/")

    @pytest.mark.unit
    def test_non_twilio_media_url_rejected(self):
        """SSRF defense at parse time — non-Twilio media URLs are dropped."""
        raw = {
            "From": "whatsapp:+14155551234",
            "To": "whatsapp:+14155238886",
            "Body": "check this",
            "NumMedia": "1",
            "MediaUrl0": "https://attacker.com/exfil",
            "MediaContentType0": "image/jpeg",
        }
        msg = self._adapter().parse_message(raw)
        # Body survived, but attacker URL was dropped
        assert msg is not None
        assert msg.files == []

    @pytest.mark.unit
    def test_twiliocdn_media_url_rejected_at_parse_time(self):
        """#1932 D3: the parse gate is the NARROW tier.

        A webhook `MediaUrl{N}` is always the api.twilio.com form; the CDN is
        only ever a redirect target. Widening the redirect tier must not widen
        what we accept straight off the wire.
        """
        raw = {
            "From": "whatsapp:+14155551234",
            "To": "whatsapp:+14155238886",
            "Body": "check this",
            "NumMedia": "1",
            "MediaUrl0": "https://mms.twiliocdn.com/Accounts/AC00/Media/ME00",
            "MediaContentType0": "image/jpeg",
        }
        msg = self._adapter().parse_message(raw)
        assert msg is not None
        assert msg.files == []

    @pytest.mark.unit
    def test_media_only_gets_placeholder_text(self):
        raw = {
            "From": "whatsapp:+14155551234",
            "To": "whatsapp:+14155238886",
            "Body": "",
            "MessageSid": "SM_media_only",
            "NumMedia": "1",
            "MediaUrl0": "https://api.twilio.com/media/ME00",
            "MediaContentType0": "audio/ogg",
        }
        msg = self._adapter().parse_message(raw)
        assert msg is not None
        assert msg.text == "(media upload)"
        assert len(msg.files) == 1

    @pytest.mark.unit
    def test_bad_num_media_string_defaults_to_zero(self):
        raw = {
            "From": "whatsapp:+14155551234",
            "To": "whatsapp:+14155238886",
            "Body": "hi",
            "NumMedia": "not-a-number",
        }
        msg = self._adapter().parse_message(raw)
        assert msg is not None
        assert msg.files == []


# =============================================================================
# Twilio RequestValidator — signature round-trip
# =============================================================================

class TestRequestValidatorRoundTrip:
    """Canonical Twilio signature verification tests using the vendored validator."""

    @pytest.mark.unit
    def test_valid_signature_passes(self):
        from twilio.request_validator import RequestValidator
        auth_token = "test_auth_token_a" * 2  # 34 chars — realistic length
        validator = RequestValidator(auth_token)

        url = "https://public.example.com/api/whatsapp/webhook/abc123"
        params = {
            "From": "whatsapp:+14155551234",
            "To": "whatsapp:+14155238886",
            "Body": "Hello",
            "MessageSid": "SM_abc",
            "NumMedia": "0",
            "AccountSid": "AC00000000000000000000000000000000",
        }
        sig = validator.compute_signature(url, params)
        assert validator.validate(url, params, sig) is True

    @pytest.mark.unit
    def test_invalid_signature_fails(self):
        from twilio.request_validator import RequestValidator
        validator = RequestValidator("token_a" * 4)
        url = "https://public.example.com/api/whatsapp/webhook/abc"
        params = {"Body": "Hello", "AccountSid": "AC00"}
        assert validator.validate(url, params, "bogus-signature") is False

    @pytest.mark.unit
    def test_tampered_body_fails(self):
        from twilio.request_validator import RequestValidator
        auth_token = "test_token" * 4
        validator = RequestValidator(auth_token)
        url = "https://public.example.com/api/whatsapp/webhook/abc"
        params = {"Body": "original", "AccountSid": "AC00"}
        sig = validator.compute_signature(url, params)
        tampered = {"Body": "MODIFIED", "AccountSid": "AC00"}
        assert validator.validate(url, tampered, sig) is False

    @pytest.mark.unit
    def test_empty_param_value_preserved(self):
        """Regression: Twilio includes empty-value params in the HMAC, and
        RequestValidator must keep them too. This is the primary reason we
        use `twilio.request_validator` instead of rolling our own HMAC."""
        from twilio.request_validator import RequestValidator
        validator = RequestValidator("token_a" * 4)
        url = "https://public.example.com/api/whatsapp/webhook/abc"
        params_with_empty = {
            "Body": "hi",
            "ProfileName": "",  # Twilio sends this empty for users without a WhatsApp profile
            "AccountSid": "AC00",
        }
        sig = validator.compute_signature(url, params_with_empty)
        assert validator.validate(url, params_with_empty, sig) is True


# =============================================================================
# Webhook transport — integration-ish with mocked db and request
# =============================================================================

class TestWebhookTransport:
    def _transport(self):
        from adapters.transports.twilio_webhook import TwilioWebhookTransport
        from adapters.whatsapp_adapter import WhatsAppAdapter
        # Stub router — we only care about auth/dedup here
        router = MagicMock()
        router.handle_message = AsyncMock()
        return TwilioWebhookTransport(WhatsAppAdapter(), router)

    @pytest.mark.unit
    def test_unknown_webhook_secret_returns_ok_no_work(self):
        transport = self._transport()
        req = MagicMock()
        with patch("adapters.transports.twilio_webhook.db") as mock_db:
            mock_db.get_whatsapp_binding_by_webhook_secret.return_value = None
            result = _run(transport.handle_webhook(req, "unknown_secret"))
        assert result == {"ok": True}

    @pytest.mark.unit
    def test_invalid_signature_returns_403_marker(self):
        from adapters.transports.twilio_webhook import TwilioWebhookTransport
        transport = self._transport()

        # Build a fake request that yields a form body and bogus signature
        class FakeForm:
            def items(self):
                return [("Body", "hi"), ("AccountSid", "AC00"), ("MessageSid", "SM_x")]

        req = MagicMock()
        req.form = AsyncMock(return_value=FakeForm())
        req.headers = {"x-twilio-signature": "bogus", "x-forwarded-proto": "https"}
        req.url = "https://public.example.com/api/whatsapp/webhook/abc"

        with patch("adapters.transports.twilio_webhook.db") as mock_db:
            mock_db.get_whatsapp_binding_by_webhook_secret.return_value = {
                "id": 1, "agent_name": "my-agent", "webhook_secret": "abc",
            }
            mock_db.get_whatsapp_auth_token.return_value = "auth_token_" + "a" * 20
            result = _run(transport.handle_webhook(req, "abc"))
        assert result == {"ok": False, "status": 403}

    @pytest.mark.unit
    def test_valid_signature_dispatches(self):
        """End-to-end: valid sig → dedup miss → asyncio task scheduled, returns 200."""
        from twilio.request_validator import RequestValidator
        from adapters.transports.twilio_webhook import _SEEN_MESSAGE_SIDS
        _SEEN_MESSAGE_SIDS.clear()

        auth_token = "real_auth_token_" + "x" * 20
        url = "https://public.example.com/api/whatsapp/webhook/abc"
        params = {
            "From": "whatsapp:+14155551234",
            "To": "whatsapp:+14155238886",
            "Body": "hello",
            "AccountSid": "AC00000000000000000000000000000000",
            "MessageSid": "SM_valid_one",
            "NumMedia": "0",
        }
        sig = RequestValidator(auth_token).compute_signature(url, params)

        class FakeForm:
            def __init__(self, params): self._p = list(params.items())
            def items(self): return self._p

        req = MagicMock()
        req.form = AsyncMock(return_value=FakeForm(params))
        req.headers = {"x-twilio-signature": sig, "x-forwarded-proto": "https"}
        req.url = url

        transport = self._transport()
        with patch("adapters.transports.twilio_webhook.db") as mock_db:
            mock_db.get_whatsapp_binding_by_webhook_secret.return_value = {
                "id": 1, "agent_name": "my-agent", "webhook_secret": "abc",
            }
            mock_db.get_whatsapp_auth_token.return_value = auth_token
            result = _run(transport.handle_webhook(req, "abc"))
        assert result == {"ok": True}

    @pytest.mark.unit
    def test_duplicate_message_sid_skipped(self):
        """Second webhook with same MessageSid is dropped pre-dispatch."""
        from twilio.request_validator import RequestValidator
        from adapters.transports.twilio_webhook import _SEEN_MESSAGE_SIDS
        _SEEN_MESSAGE_SIDS.clear()

        auth_token = "real_auth_token_" + "x" * 20
        url = "https://public.example.com/api/whatsapp/webhook/abc"
        params = {
            "From": "whatsapp:+14155551234",
            "To": "whatsapp:+14155238886",
            "Body": "dup",
            "AccountSid": "AC00000000000000000000000000000000",
            "MessageSid": "SM_duplicated",
            "NumMedia": "0",
        }
        sig = RequestValidator(auth_token).compute_signature(url, params)

        class FakeForm:
            def __init__(self, params): self._p = list(params.items())
            def items(self): return self._p

        req = MagicMock()
        req.form = AsyncMock(return_value=FakeForm(params))
        req.headers = {"x-twilio-signature": sig, "x-forwarded-proto": "https"}
        req.url = url

        transport = self._transport()
        with patch("adapters.transports.twilio_webhook.db") as mock_db:
            mock_db.get_whatsapp_binding_by_webhook_secret.return_value = {
                "id": 1, "agent_name": "my-agent", "webhook_secret": "abc",
            }
            mock_db.get_whatsapp_auth_token.return_value = auth_token
            # First call — accepted
            first = _run(transport.handle_webhook(req, "abc"))
            # Same SID again — deduped (still returns 200 ok)
            second = _run(transport.handle_webhook(req, "abc"))
        assert first == {"ok": True}
        assert second == {"ok": True}
        # Dispatch was scheduled only once — check by inspecting the dedup ring
        assert "SM_duplicated" in _SEEN_MESSAGE_SIDS


# =============================================================================
# Phase 2 (#467) — Markdown conversion
# =============================================================================

class TestMarkdownConversion:
    @pytest.mark.unit
    def test_bold_double_asterisk_to_single(self):
        from adapters.whatsapp_adapter import WhatsAppAdapter
        assert WhatsAppAdapter._markdown_to_whatsapp("**bold**") == "*bold*"

    @pytest.mark.unit
    def test_bold_double_underscore_to_single_asterisk(self):
        from adapters.whatsapp_adapter import WhatsAppAdapter
        assert WhatsAppAdapter._markdown_to_whatsapp("__bold__") == "*bold*"

    @pytest.mark.unit
    def test_strikethrough_double_tilde_to_single(self):
        from adapters.whatsapp_adapter import WhatsAppAdapter
        assert WhatsAppAdapter._markdown_to_whatsapp("~~gone~~") == "~gone~"

    @pytest.mark.unit
    def test_markdown_link_to_text_and_url(self):
        from adapters.whatsapp_adapter import WhatsAppAdapter
        out = WhatsAppAdapter._markdown_to_whatsapp("Visit [docs](https://x.com/docs)")
        assert out == "Visit docs (https://x.com/docs)"

    @pytest.mark.unit
    def test_headers_become_bold(self):
        from adapters.whatsapp_adapter import WhatsAppAdapter
        out = WhatsAppAdapter._markdown_to_whatsapp("## Section")
        assert out == "*Section*"

    @pytest.mark.unit
    def test_code_span_passes_through(self):
        from adapters.whatsapp_adapter import WhatsAppAdapter
        # WhatsApp's `mono` syntax matches markdown — no transformation needed
        assert WhatsAppAdapter._markdown_to_whatsapp("use `cmd -x`") == "use `cmd -x`"

    @pytest.mark.unit
    def test_empty_string_passes(self):
        from adapters.whatsapp_adapter import WhatsAppAdapter
        assert WhatsAppAdapter._markdown_to_whatsapp("") == ""


# =============================================================================
# Phase 2 (#467) — Command detection (is_command)
# =============================================================================

class TestIsCommand:
    @pytest.mark.unit
    def test_slash_prefix_is_command(self):
        from adapters.base import NormalizedMessage
        from adapters.whatsapp_adapter import WhatsAppAdapter
        msg = NormalizedMessage(sender_id="whatsapp:+1", text="/login",
                                channel_id="whatsapp:+1", timestamp="")
        assert WhatsAppAdapter().is_command(msg) is True

    @pytest.mark.unit
    def test_no_slash_is_not_command(self):
        from adapters.base import NormalizedMessage
        from adapters.whatsapp_adapter import WhatsAppAdapter
        msg = NormalizedMessage(sender_id="whatsapp:+1", text="hello",
                                channel_id="whatsapp:+1", timestamp="")
        assert WhatsAppAdapter().is_command(msg) is False


# =============================================================================
# Phase 2 (#467) — /login flow
# =============================================================================

def _msg_with_text(text: str, sender: str = "whatsapp:+14155551234",
                   agent: str = "my-agent"):
    from adapters.base import NormalizedMessage
    return NormalizedMessage(
        sender_id=sender,
        text=text,
        channel_id=sender,
        thread_id="SM_test",
        timestamp="",
        metadata={"agent_name": agent, "binding_id": 1, "message_sid": "SM_test"},
    )


class TestLoginFlow:
    @pytest.mark.unit
    def test_login_no_arg_shows_usage(self):
        from adapters.whatsapp_adapter import WhatsAppAdapter
        with patch("adapters.whatsapp_adapter.db") as mock_db:
            mock_db.get_whatsapp_binding.return_value = {"id": 1, "agent_name": "my-agent"}
            reply = _run(WhatsAppAdapter().handle_command(_msg_with_text("/login")))
        assert "Usage" in reply
        assert "/login your@email.com" in reply

    @pytest.mark.unit
    def test_login_no_binding_returns_unavailable(self):
        from adapters.whatsapp_adapter import WhatsAppAdapter
        with patch("adapters.whatsapp_adapter.db") as mock_db:
            mock_db.get_whatsapp_binding.return_value = None
            reply = _run(WhatsAppAdapter().handle_command(_msg_with_text("/login foo@bar.com")))
        assert reply == "Login is unavailable for this chat."

    @pytest.mark.unit
    def test_login_malformed_email(self):
        from adapters.whatsapp_adapter import WhatsAppAdapter
        with patch("adapters.whatsapp_adapter.db") as mock_db:
            mock_db.get_whatsapp_binding.return_value = {"id": 1, "agent_name": "my-agent"}
            reply = _run(WhatsAppAdapter().handle_command(_msg_with_text("/login not-an-email")))
        assert "doesn't look like an email" in reply

    @pytest.mark.unit
    def test_login_email_sends_code_and_stores_pending(self):
        from adapters.whatsapp_adapter import WhatsAppAdapter
        with patch("adapters.whatsapp_adapter.db") as mock_db, \
             patch("adapters.whatsapp_adapter._set_pending_login") as mock_set, \
             patch("adapters.whatsapp_adapter.EmailService") as mock_email_cls:
            mock_db.get_whatsapp_binding.return_value = {"id": 1, "agent_name": "my-agent"}
            mock_db.create_login_code.return_value = {"code": "123456"}
            mock_email_instance = MagicMock()
            mock_email_instance.send_verification_code = AsyncMock(return_value=True)
            mock_email_cls.return_value = mock_email_instance

            reply = _run(WhatsAppAdapter().handle_command(_msg_with_text("/login user@example.com")))
            assert "Sent a 6-digit code" in reply
            mock_db.create_login_code.assert_called_once_with("user@example.com", expiry_minutes=10)
            mock_set.assert_called_once_with(1, "whatsapp:+14155551234", "user@example.com")

    @pytest.mark.unit
    def test_login_email_send_failure(self):
        from adapters.whatsapp_adapter import WhatsAppAdapter
        with patch("adapters.whatsapp_adapter.db") as mock_db, \
             patch("adapters.whatsapp_adapter._set_pending_login"), \
             patch("adapters.whatsapp_adapter.EmailService") as mock_email_cls:
            mock_db.get_whatsapp_binding.return_value = {"id": 1, "agent_name": "my-agent"}
            mock_db.create_login_code.return_value = {"code": "123456"}
            mock_email_instance = MagicMock()
            mock_email_instance.send_verification_code = AsyncMock(return_value=False)
            mock_email_cls.return_value = mock_email_instance

            reply = _run(WhatsAppAdapter().handle_command(_msg_with_text("/login user@example.com")))
            assert "couldn't send the email" in reply

    @pytest.mark.unit
    def test_login_code_without_pending(self):
        from adapters.whatsapp_adapter import WhatsAppAdapter
        with patch("adapters.whatsapp_adapter.db") as mock_db, \
             patch("adapters.whatsapp_adapter._get_pending_login", return_value=None):
            mock_db.get_whatsapp_binding.return_value = {"id": 1, "agent_name": "my-agent"}
            reply = _run(WhatsAppAdapter().handle_command(_msg_with_text("/login 123456")))
        assert "don't have a pending login" in reply

    @pytest.mark.unit
    def test_login_code_invalid(self):
        from adapters.whatsapp_adapter import WhatsAppAdapter
        with patch("adapters.whatsapp_adapter.db") as mock_db, \
             patch("adapters.whatsapp_adapter._get_pending_login", return_value="user@example.com"):
            mock_db.get_whatsapp_binding.return_value = {"id": 1, "agent_name": "my-agent"}
            mock_db.verify_login_code.return_value = None
            reply = _run(WhatsAppAdapter().handle_command(_msg_with_text("/login 000000")))
        assert "Invalid or expired" in reply


# =============================================================================
# Phase 2 (#467) — Post-verification access gate
# =============================================================================

class TestAccessGate:
    @pytest.mark.unit
    def test_verified_shared_user_gets_all_clear(self):
        from adapters.whatsapp_adapter import WhatsAppAdapter
        with patch("adapters.whatsapp_adapter.db") as mock_db, \
             patch("adapters.whatsapp_adapter._get_pending_login", return_value="user@example.com"), \
             patch("adapters.whatsapp_adapter._clear_pending_login") as mock_clear:
            mock_db.get_whatsapp_binding.return_value = {"id": 1, "agent_name": "my-agent"}
            mock_db.verify_login_code.return_value = {"email": "user@example.com"}
            mock_db.get_access_policy.return_value = {"require_email": True, "open_access": False}
            mock_db.email_has_agent_access.return_value = True

            reply = _run(WhatsAppAdapter().handle_command(_msg_with_text("/login 123456")))
            assert "✅ Verified" in reply
            assert "You can chat normally" in reply
            mock_db.set_whatsapp_verified_email.assert_called_once_with(
                1, "whatsapp:+14155551234", "user@example.com"
            )
            mock_clear.assert_called_once()
            # No access request for shared user
            mock_db.upsert_access_request.assert_not_called()

    @pytest.mark.unit
    def test_verified_open_access_gets_all_clear(self):
        from adapters.whatsapp_adapter import WhatsAppAdapter
        with patch("adapters.whatsapp_adapter.db") as mock_db, \
             patch("adapters.whatsapp_adapter._get_pending_login", return_value="user@example.com"), \
             patch("adapters.whatsapp_adapter._clear_pending_login"):
            mock_db.get_whatsapp_binding.return_value = {"id": 1, "agent_name": "my-agent"}
            mock_db.verify_login_code.return_value = {"email": "user@example.com"}
            mock_db.get_access_policy.return_value = {"require_email": False, "open_access": True}
            mock_db.email_has_agent_access.return_value = False

            reply = _run(WhatsAppAdapter().handle_command(_msg_with_text("/login 123456")))
            assert "You can chat normally" in reply
            mock_db.upsert_access_request.assert_not_called()

    @pytest.mark.unit
    def test_verified_restricted_creates_access_request(self):
        from adapters.whatsapp_adapter import WhatsAppAdapter
        with patch("adapters.whatsapp_adapter.db") as mock_db, \
             patch("adapters.whatsapp_adapter._get_pending_login", return_value="user@example.com"), \
             patch("adapters.whatsapp_adapter._clear_pending_login"):
            mock_db.get_whatsapp_binding.return_value = {"id": 1, "agent_name": "my-agent"}
            mock_db.verify_login_code.return_value = {"email": "user@example.com"}
            mock_db.get_access_policy.return_value = {"require_email": True, "open_access": False}
            mock_db.email_has_agent_access.return_value = False

            reply = _run(WhatsAppAdapter().handle_command(_msg_with_text("/login 123456")))
            assert "pending approval" in reply
            mock_db.upsert_access_request.assert_called_once_with(
                "my-agent", "user@example.com", "whatsapp"
            )


# =============================================================================
# Phase 2 (#467) — /logout and /whoami
# =============================================================================

class TestLogoutWhoami:
    @pytest.mark.unit
    def test_logout_clears_verified_email(self):
        from adapters.whatsapp_adapter import WhatsAppAdapter
        with patch("adapters.whatsapp_adapter.db") as mock_db, \
             patch("adapters.whatsapp_adapter._clear_pending_login") as mock_clear:
            mock_db.get_whatsapp_binding.return_value = {"id": 1, "agent_name": "my-agent"}
            reply = _run(WhatsAppAdapter().handle_command(_msg_with_text("/logout")))
            assert "Logged out" in reply
            mock_db.clear_whatsapp_verified_email.assert_called_once_with(
                1, "whatsapp:+14155551234"
            )
            mock_clear.assert_called_once()

    @pytest.mark.unit
    def test_whoami_verified(self):
        from adapters.whatsapp_adapter import WhatsAppAdapter
        with patch("adapters.whatsapp_adapter.db") as mock_db:
            mock_db.get_whatsapp_binding.return_value = {"id": 1, "agent_name": "my-agent"}
            mock_db.get_whatsapp_verified_email.return_value = "user@example.com"
            reply = _run(WhatsAppAdapter().handle_command(_msg_with_text("/whoami")))
        assert "user@example.com" in reply
        assert "verified" in reply.lower()

    @pytest.mark.unit
    def test_whoami_unverified(self):
        from adapters.whatsapp_adapter import WhatsAppAdapter
        with patch("adapters.whatsapp_adapter.db") as mock_db:
            mock_db.get_whatsapp_binding.return_value = {"id": 1, "agent_name": "my-agent"}
            mock_db.get_whatsapp_verified_email.return_value = None
            reply = _run(WhatsAppAdapter().handle_command(_msg_with_text("/whoami")))
        assert "not verified" in reply
        assert "/login" in reply


# =============================================================================
# Phase 2 (#467) — prompt_auth
# =============================================================================

class TestPromptAuth:
    @pytest.mark.unit
    def test_prompt_auth_sends_instructions(self):
        from adapters.whatsapp_adapter import WhatsAppAdapter
        adapter = WhatsAppAdapter()
        adapter.send_response = AsyncMock()
        msg = _msg_with_text("blocked message")
        _run(adapter.prompt_auth(msg, "my-agent", bot_token="AC_sid:token"))
        adapter.send_response.assert_awaited_once()
        call_kwargs = adapter.send_response.await_args
        response = call_kwargs.args[1]
        assert "verified email" in response.text
        assert "/login your@email.com" in response.text


# =============================================================================
# Phase 2 (#467) — Proactive delivery via WhatsApp
# =============================================================================

class TestProactiveWhatsApp:
    @pytest.mark.unit
    def test_deliver_whatsapp_no_binding_raises(self):
        import importlib
        pms = importlib.import_module("services.proactive_message_service")
        with patch.object(pms, "db") as mock_db:
            mock_db.get_whatsapp_binding.return_value = None
            # Bypass rate + auth checks by calling _deliver_whatsapp directly
            svc = pms.ProactiveMessageService()
            with pytest.raises(pms.RecipientNotFoundError):
                _run(svc._deliver_whatsapp("my-agent", "user@example.com", "hi"))

    @pytest.mark.unit
    def test_deliver_whatsapp_no_chat_link_raises(self):
        import importlib
        pms = importlib.import_module("services.proactive_message_service")
        with patch.object(pms, "db") as mock_db:
            mock_db.get_whatsapp_binding.return_value = {
                "id": 1, "agent_name": "my-agent", "account_sid": "AC_sid",
                "from_number": "whatsapp:+14155238886", "messaging_service_sid": None,
            }
            mock_db.get_whatsapp_chat_link_by_verified_email.return_value = None
            svc = pms.ProactiveMessageService()
            with pytest.raises(pms.RecipientNotFoundError):
                _run(svc._deliver_whatsapp("my-agent", "user@example.com", "hi"))

    @pytest.mark.unit
    def test_deliver_whatsapp_happy_path(self):
        import importlib
        pms = importlib.import_module("services.proactive_message_service")
        with patch.object(pms, "db") as mock_db:
            mock_db.get_whatsapp_binding.return_value = {
                "id": 1, "agent_name": "my-agent", "account_sid": "AC_sid",
                "from_number": "whatsapp:+14155238886", "messaging_service_sid": None,
            }
            mock_db.get_whatsapp_chat_link_by_verified_email.return_value = {
                "id": 10, "binding_id": 1, "wa_user_phone": "whatsapp:+14155551234",
                "verified_email": "user@example.com",
            }
            mock_db.get_whatsapp_auth_token.return_value = "tok_abc"

            from adapters import whatsapp_adapter as wa_mod
            send_mock = AsyncMock(return_value={"sid": "SM_outbound_xyz"})
            with patch.object(wa_mod.WhatsAppAdapter, "_send_message", send_mock):
                svc = pms.ProactiveMessageService()
                result = _run(svc._deliver_whatsapp("my-agent", "user@example.com", "**ping**"))

            assert result.success is True
            assert result.channel == "whatsapp"
            assert result.message_id == "SM_outbound_xyz"
            # Verify markdown was converted before sending
            kwargs = send_mock.await_args.kwargs
            assert kwargs["body"] == "*ping*"
            assert kwargs["to_number"] == "whatsapp:+14155551234"

    @pytest.mark.unit
    def test_deliver_whatsapp_chunks_long_messages(self):
        """I1 fix — proactive messages >1600 chars must be split before send."""
        import importlib
        pms = importlib.import_module("services.proactive_message_service")
        with patch.object(pms, "db") as mock_db:
            mock_db.get_whatsapp_binding.return_value = {
                "id": 1, "agent_name": "my-agent", "account_sid": "AC_sid",
                "from_number": "whatsapp:+14155238886", "messaging_service_sid": None,
            }
            mock_db.get_whatsapp_chat_link_by_verified_email.return_value = {
                "id": 10, "binding_id": 1, "wa_user_phone": "whatsapp:+14155551234",
                "verified_email": "user@example.com",
            }
            mock_db.get_whatsapp_auth_token.return_value = "tok_abc"

            # Build text well over Twilio's 1600-char WhatsApp body limit
            long_text = ("Paragraph. " * 300).strip()  # ~3300 chars
            assert len(long_text) > 1600

            from adapters import whatsapp_adapter as wa_mod
            send_mock = AsyncMock(side_effect=[
                {"sid": "SM_chunk_1"},
                {"sid": "SM_chunk_2"},
                {"sid": "SM_chunk_3"},
                {"sid": "SM_chunk_4"},
            ])
            with patch.object(wa_mod.WhatsAppAdapter, "_send_message", send_mock):
                svc = pms.ProactiveMessageService()
                result = _run(svc._deliver_whatsapp("my-agent", "user@example.com", long_text))

            # Must have invoked the sender at least twice
            assert send_mock.await_count >= 2
            # Every chunk must respect Twilio's WhatsApp body limit
            for call in send_mock.await_args_list:
                assert len(call.kwargs["body"]) <= wa_mod.TWILIO_WHATSAPP_MAX_LENGTH
            assert result.success is True
            assert result.channel == "whatsapp"
            # message_id should be the *last* chunk's sid
            assert result.message_id == f"SM_chunk_{send_mock.await_count}"

    @pytest.mark.unit
    def test_deliver_whatsapp_partial_send_failure(self):
        """If any chunk fails mid-send, return failure — don't silently report success."""
        import importlib
        pms = importlib.import_module("services.proactive_message_service")
        with patch.object(pms, "db") as mock_db:
            mock_db.get_whatsapp_binding.return_value = {
                "id": 1, "agent_name": "my-agent", "account_sid": "AC_sid",
                "from_number": "whatsapp:+14155238886", "messaging_service_sid": None,
            }
            mock_db.get_whatsapp_chat_link_by_verified_email.return_value = {
                "id": 10, "binding_id": 1, "wa_user_phone": "whatsapp:+14155551234",
                "verified_email": "user@example.com",
            }
            mock_db.get_whatsapp_auth_token.return_value = "tok_abc"
            long_text = ("Paragraph. " * 300).strip()

            from adapters import whatsapp_adapter as wa_mod
            # First chunk sends, second fails (Twilio error → None)
            send_mock = AsyncMock(side_effect=[{"sid": "SM_chunk_1"}, None])
            with patch.object(wa_mod.WhatsAppAdapter, "_send_message", send_mock):
                svc = pms.ProactiveMessageService()
                result = _run(svc._deliver_whatsapp("my-agent", "user@example.com", long_text))

            assert result.success is False
            assert "Send failed" in (result.error or "")


# =============================================================================
# Phase 2 (#467) — send_response applies markdown conversion
# =============================================================================

class TestSendResponseMarkdown:
    @pytest.mark.unit
    def test_send_response_converts_markdown(self):
        from adapters.whatsapp_adapter import WhatsAppAdapter
        from adapters.base import ChannelResponse

        with patch("adapters.whatsapp_adapter.db") as mock_db:
            mock_db.get_whatsapp_binding.return_value = {
                "id": 1, "agent_name": "my-agent", "account_sid": "AC_sid",
                "from_number": "whatsapp:+14155238886", "messaging_service_sid": None,
            }
            adapter = WhatsAppAdapter()
            send_mock = AsyncMock(return_value={"sid": "SM_ok"})
            with patch.object(WhatsAppAdapter, "_send_message", send_mock):
                response = ChannelResponse(
                    text="**hello** [world](https://x.com)",
                    metadata={"bot_token": "AC_sid:tok_abc", "agent_name": "my-agent"},
                )
                _run(adapter.send_response("whatsapp:+14155551234", response))

            kwargs = send_mock.await_args.kwargs
            assert kwargs["body"] == "*hello* world (https://x.com)"
