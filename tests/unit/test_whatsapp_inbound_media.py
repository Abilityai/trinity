"""
Unit tests for WhatsApp INBOUND media download (#1932).

Twilio serves message media from its CDN: `api.twilio.com` answers the
authenticated media request with a **302** to `mms.twiliocdn.com` (a short-lived
signed URL). Before #1932 the SSRF allowlist accepted only `.twilio.com`, so the
redirect target was refused and `WhatsAppAdapter.download_file` returned `None`
for *every* attachment — inbound WhatsApp media had never worked (since #463).

The allowlist is now two tiers:

- `_TWILIO_MEDIA_SOURCE_HOST_SUFFIXES` — `*.twilio.com`. Webhook `MediaUrl{N}`
  parse gate and hop 1, the hop that carries the tenant's Basic auth.
- `_TWILIO_MEDIA_ALLOWED_HOST_SUFFIXES` — adds `*.twiliocdn.com`. Validated
  redirect targets only, always fetched WITHOUT auth.

Covers:
- the real redirect chain end-to-end (302 → CDN → 200 → bytes)
- the redirect hop carries no credentials
- every refusal path, each asserted by HOP COUNT (see below)
- bounded redirect budget, transport size cap, timeout, non-200

WHY EVERY TEST ASSERTS THE CALL LOG
-----------------------------------
`download_file` returns `None` on *every* failure path, and its bare
`except Exception` swallows `AssertionError` too. A negative test that only
checks `result is None` therefore passes even when the harness is broken
(DB seam unpatched → 0 hops; capital-`L` `Location` key → redirect silently
missed; assert inside the fake → swallowed). So each test asserts the recorded
`(url, auth)` call list, never `result is None` alone.

Harness rules (all three are load-bearing):
1. `patch.object(wa_mod, "db", ...)` is the ONLY correct DB seam —
   `whatsapp_adapter.py` does `from database import db`, a binding captured at
   import, so patching `sys.modules["database"]` afterwards is a no-op. (This is
   also why these tests do NOT live in `tests/test_whatsapp_adapter.py`, whose
   module-level `db = MagicMock()` makes the credentials guard vacuous.)
2. Never assert inside the fake client — record, then assert after the await.
3. Use `httpx.Headers({...})`, not a plain dict: the adapter reads
   `resp.headers.get("location")` lowercase and real httpx headers are
   case-insensitive.

Modules:
- src/backend/adapters/whatsapp_adapter.py
Issue: https://github.com/abilityai/trinity/issues/1932

Env + sys.path are configured by tests/unit/conftest.py.
"""

from unittest.mock import patch

import httpx
import pytest

import adapters.whatsapp_adapter as wa_mod
from adapters.base import FileAttachment, NormalizedMessage
from adapters.whatsapp_adapter import (
    WhatsAppAdapter,
    _MAX_MEDIA_REDIRECTS,
    _WA_MEDIA_DOWNLOAD_MAX_BYTES,
)

_SOURCE_URL = "https://api.twilio.com/2010-04-01/Accounts/AC00/Messages/MM00/Media/ME00"
_CDN_URL = "https://mms.twiliocdn.com/Accounts/AC00/Messages/MM00/Media/ME00?x=sig"
_PNG = b"\x89PNG\r\n\x1a\nFAKEBYTES"


class _Resp:
    """Minimal httpx.Response stand-in: status, case-insensitive headers, bytes."""

    def __init__(self, status_code, headers=None, content=b""):
        self.status_code = status_code
        self.headers = httpx.Headers(headers or {})
        self.content = content


class _Raise:
    """Sentinel: the fake client raises this exception instead of responding."""

    def __init__(self, exc):
        self.exc = exc


def _redirect(location, status=302):
    return _Resp(status, headers={"Location": location})


def _file(url=_SOURCE_URL, name="media_0.jpeg"):
    return FileAttachment(id="MM00-0", name=name, mimetype="image/jpeg", size=0, url=url)


def _msg(metadata=None):
    return NormalizedMessage(
        sender_id="whatsapp:+14155551234",
        text="",
        channel_id="whatsapp:+14155551234",
        timestamp="2026-08-01T00:00:00Z",
        metadata={"agent_name": "my-agent"} if metadata is None else metadata,
    )


async def _download(
    *,
    responses,
    url=_SOURCE_URL,
    binding={"account_sid": "AC1"},
    token="tok",
    metadata=None,
):
    """Drive `download_file` against a scripted response sequence.

    Returns `(result, calls)` where `calls` is the ordered list of
    `(url, auth)` tuples the adapter actually issued.
    """
    calls = []
    scripted = list(responses)

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, auth=None):
            calls.append((url, auth))  # RECORD ONLY — never assert in here
            nxt = scripted.pop(0)
            if isinstance(nxt, _Raise):
                raise nxt.exc
            return nxt

    with patch.object(wa_mod, "db") as fake_db:
        fake_db.get_whatsapp_binding.return_value = binding
        fake_db.get_whatsapp_auth_token.return_value = token
        with patch.object(wa_mod.httpx, "AsyncClient", _Client):
            result = await WhatsAppAdapter().download_file(_file(url), _msg(metadata))

    return result, calls


@pytest.mark.asyncio
class TestDownloadFileRedirectChain:
    """The real Twilio path and every way it can be refused (#1932)."""

    # --- positive controls -------------------------------------------------

    @pytest.mark.unit
    async def test_redirect_to_cdn_returns_bytes(self):
        """AC #5 headline: api.twilio.com -> 302 -> mms.twiliocdn.com -> 200."""
        result, calls = await _download(
            responses=[_redirect(_CDN_URL), _Resp(200, content=_PNG)]
        )
        assert result == _PNG
        assert len(calls) == 2, f"expected both hops to be issued, got {calls}"
        assert calls[1][0] == _CDN_URL

    @pytest.mark.unit
    async def test_redirect_hop_carries_no_auth(self):
        """Hop 1 sends the tenant's Basic auth; the CDN hop must send none."""
        result, calls = await _download(
            responses=[_redirect(_CDN_URL), _Resp(200, content=_PNG)]
        )
        assert result == _PNG
        assert calls == [(_SOURCE_URL, ("AC1", "tok")), (_CDN_URL, None)]

    @pytest.mark.unit
    async def test_direct_200_returns_bytes(self):
        """An account that answers 200 without redirecting still works."""
        result, calls = await _download(responses=[_Resp(200, content=_PNG)])
        assert result == _PNG
        assert len(calls) == 1

    @pytest.mark.unit
    async def test_media_twiliocdn_redirect_accepted(self):
        """The suffix covers media.twiliocdn.com as well as mms.*."""
        target = "https://media.twiliocdn.com/x"
        result, calls = await _download(
            responses=[_redirect(target), _Resp(200, content=_PNG)]
        )
        assert result == _PNG
        assert [c[0] for c in calls] == [_SOURCE_URL, target]

    # --- redirect-target refusals (each pinned by hop count) ---------------

    @pytest.mark.unit
    async def test_off_domain_redirect_refused(self):
        result, calls = await _download(
            responses=[_redirect("https://attacker.com/x"), _Resp(200, content=_PNG)]
        )
        assert result is None
        assert len(calls) == 1, "the off-domain target must never be fetched"

    @pytest.mark.unit
    async def test_cdn_spoof_redirect_refused(self):
        for target in (
            "https://eviltwiliocdn.com/x",
            "https://twiliocdn.com.evil.com/x",
            "https://mms.twiliocdn.com.evil.com/x",
            "https://mms.twiliocdn.com@evil.com/x",
        ):
            result, calls = await _download(
                responses=[_redirect(target), _Resp(200, content=_PNG)]
            )
            assert result is None, target
            assert len(calls) == 1, f"{target} must not be fetched (got {calls})"

    @pytest.mark.unit
    async def test_s3_redirect_refused(self):
        """D2: accounts without media auth 302 to path-style S3 — refused.

        `s3-external-1.amazonaws.com/<bucket>/<key>` is path-style, so
        allowlisting it (even as one exact host) would admit arbitrary buckets
        under an allowlisted name. This is a decision, pinned as a property.
        """
        for target in (
            "https://s3-external-1.amazonaws.com/bucket/key",
            "https://mybucket.s3.amazonaws.com/key",
        ):
            result, calls = await _download(
                responses=[_redirect(target), _Resp(200, content=_PNG)]
            )
            assert result is None, target
            assert len(calls) == 1, f"{target} must not be fetched (got {calls})"

    @pytest.mark.unit
    async def test_http_redirect_refused(self):
        result, calls = await _download(
            responses=[
                _redirect("http://mms.twiliocdn.com/x"),
                _Resp(200, content=_PNG),
            ]
        )
        assert result is None
        assert len(calls) == 1

    @pytest.mark.unit
    async def test_relative_location_refused(self):
        """Documented fail-closed: a relative Location has no https scheme."""
        result, calls = await _download(
            responses=[_redirect("/foo/bar"), _Resp(200, content=_PNG)]
        )
        assert result is None
        assert len(calls) == 1

    @pytest.mark.unit
    async def test_missing_location_header_refused(self):
        """A 302 with no Location fails closed (empty string -> no scheme)."""
        result, calls = await _download(
            responses=[_Resp(302), _Resp(200, content=_PNG)]
        )
        assert result is None
        assert len(calls) == 1

    @pytest.mark.unit
    async def test_all_redirect_statuses_are_validated(self):
        """301/303/307/308 go through the same gate as 302."""
        for status in (301, 302, 303, 307, 308):
            result, calls = await _download(
                responses=[
                    _redirect("https://attacker.com/x", status=status),
                    _Resp(200, content=_PNG),
                ]
            )
            assert result is None, status
            assert len(calls) == 1, status

    # --- source-tier refusals (D3: the CDN is never a credentialed hop) ----

    @pytest.mark.unit
    async def test_cdn_source_url_never_credentialed(self):
        """D3: a CDN host in `file.url` is refused BEFORE any request.

        The credentialed hop stays *.twilio.com only, so the tenant's AuthToken
        can never be sent to a CDN host even if one reached `FileAttachment.url`.
        """
        result, calls = await _download(
            responses=[_Resp(200, content=_PNG)], url=_CDN_URL
        )
        assert result is None
        assert calls == [], "the CDN must never be a credentialed hop-1 target"

    @pytest.mark.unit
    async def test_non_twilio_source_url_refused_before_fetch(self):
        result, calls = await _download(
            responses=[_Resp(200, content=_PNG)], url="https://attacker.com/x"
        )
        assert result is None
        assert calls == []

    # --- credential / transport failure paths ------------------------------

    @pytest.mark.unit
    async def test_missing_credentials_returns_none(self):
        """No binding, no token, or no agent_name — all refuse before fetching."""
        for kwargs in (
            {"binding": None},
            {"token": None},
            {"metadata": {}},  # no agent_name in message.metadata
        ):
            result, calls = await _download(
                responses=[_Resp(200, content=_PNG)], **kwargs
            )
            assert result is None, kwargs
            assert calls == [], f"{kwargs} must not reach the network (got {calls})"

    @pytest.mark.unit
    async def test_timeout_returns_none(self):
        result, calls = await _download(
            responses=[_Raise(httpx.TimeoutException("boom"))]
        )
        assert result is None
        assert len(calls) == 1

    @pytest.mark.unit
    async def test_non_200_returns_none(self):
        result, calls = await _download(responses=[_Resp(404)])
        assert result is None
        assert len(calls) == 1

    @pytest.mark.unit
    async def test_non_200_after_redirect_returns_none(self):
        result, calls = await _download(responses=[_redirect(_CDN_URL), _Resp(403)])
        assert result is None
        assert len(calls) == 2

    # --- bounded budget + size cap -----------------------------------------

    @pytest.mark.unit
    async def test_redirect_budget_exhausted(self):
        """A 302 chain longer than the budget stops, it does not loop."""
        responses = [_redirect(_CDN_URL) for _ in range(_MAX_MEDIA_REDIRECTS + 1)]
        responses.append(_Resp(200, content=_PNG))
        result, calls = await _download(responses=responses)
        assert result is None
        # hop 1 + exactly _MAX_MEDIA_REDIRECTS follows, then refusal.
        assert len(calls) == _MAX_MEDIA_REDIRECTS + 1

    @pytest.mark.unit
    async def test_chained_redirect_within_budget_succeeds(self):
        """302 -> 302 -> 200 works: the budget is a ceiling, not a single hop."""
        result, calls = await _download(
            responses=[
                _redirect(_CDN_URL),
                _redirect("https://media.twiliocdn.com/second"),
                _Resp(200, content=_PNG),
            ]
        )
        assert result == _PNG
        assert len(calls) == 3
        assert calls[1][1] is None and calls[2][1] is None, "no hop after 1 is authed"

    @pytest.mark.unit
    async def test_oversize_body_rejected(self):
        big = b"a" * (_WA_MEDIA_DOWNLOAD_MAX_BYTES + 1)
        result, calls = await _download(responses=[_Resp(200, content=big)])
        assert result is None
        assert len(calls) == 1

    @pytest.mark.unit
    async def test_body_at_cap_accepted(self):
        """The cap is inclusive — exactly-at-limit must still be delivered."""
        exact = b"a" * _WA_MEDIA_DOWNLOAD_MAX_BYTES
        result, calls = await _download(responses=[_Resp(200, content=exact)])
        assert result == exact
        assert len(calls) == 1


@pytest.mark.asyncio
class TestInboundMediaHarnessIntegrity:
    """Guards against the vacuous-test failure modes this suite is shaped around."""

    @pytest.mark.unit
    async def test_db_seam_is_actually_patched(self):
        """If `patch.object(wa_mod, "db")` stopped working, hop 1 would never fire.

        `test_redirect_to_cdn_returns_bytes` would then still return None and
        every negative test would pass for the wrong reason. Assert the seam.
        """
        result, calls = await _download(responses=[_Resp(200, content=_PNG)])
        assert result == _PNG, "the credentials guard must pass under the fake db"
        assert calls[0][1] == ("AC1", "tok"), "hop 1 must carry the patched creds"

    @pytest.mark.unit
    async def test_location_header_is_read_case_insensitively(self):
        """The fake must use httpx.Headers, or every redirect test is vacuous."""
        resp = _redirect(_CDN_URL)
        assert resp.headers.get("location") == _CDN_URL
