"""Unit tests for Slack INBOUND file download SSRF hardening (#1951).

`SlackService.download_file` fetched `url_private_download` with no host
allowlist, no scheme check, and `follow_redirects=True` — so a redirect was
blind-followed to any host. Both sibling channels already validated (Telegram
exact-match since its introduction, WhatsApp two-tier since #1932); Slack was
the one that never did. Third consecutive CSO audit to report it.

The gate is the #1932 shape, two tiers:

- `_SLACK_FILE_SOURCE_HOST_SUFFIXES` — `*.slack.com`. Hop 1, the only request
  that carries `Bearer {bot_token}`.
- `_SLACK_FILE_ALLOWED_HOST_SUFFIXES` — adds `*.slack-edge.com` /
  `*.slack-files.com`. Validated redirect targets only, fetched WITHOUT the
  token.

WHY EVERY TEST ASSERTS THE CALL LOG
-----------------------------------
`download_file` returns `None` on *every* failure path and wraps its body in a
bare `except Exception` that swallows `AssertionError` too. A negative test
asserting only `result is None` therefore passes against a **completely
unpatched** seam — it cannot tell "refused before the request" from "the fake
client was never wired up". So each test asserts the recorded `(url, headers)`
call list. This is the lesson `tests/unit/test_whatsapp_inbound_media.py`
records, applied to the third channel.

Issue: https://github.com/abilityai/trinity/issues/1951
Env + sys.path are configured by tests/unit/conftest.py.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import httpx
import pytest

from services.slack_service import (
    SlackService,
    _MAX_FILE_REDIRECTS,
    _is_slack_file_source_url,
    _is_slack_file_url,
)

TOKEN = "xoxb-test-token"                     # not a real credential
SLACK_URL = "https://files.slack.com/files-pri/T1-F1/report.pdf"


class _FakeClient:
    """Records every request; replays a scripted response per call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def get(self, url, headers=None, **kwargs):
        self.calls.append((url, dict(headers or {})))
        if not self._responses:
            raise AssertionError("more requests than scripted responses")
        return self._responses.pop(0)


def _resp(status=200, *, content=b"bytes", location=None):
    headers = httpx.Headers({"location": location} if location else {})
    return httpx.Response(status_code=status, content=content, headers=headers)


def _service(responses):
    svc = SlackService()
    fake = _FakeClient(responses)
    svc._client = fake
    return svc, fake


# ---------------------------------------------------------------------------
# The legitimate path must still work — #1932's real lesson
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_normal_slack_file_download_succeeds():
    """An allowlist that rejects the legitimate host scores as "allowlist
    present" in an audit while being 100% broken. That is how WhatsApp media
    stayed dead for three months."""
    svc, fake = _service([_resp(content=b"PDF-BYTES")])

    result = await svc.download_file(TOKEN, SLACK_URL)

    assert result == b"PDF-BYTES"
    assert len(fake.calls) == 1
    url, headers = fake.calls[0]
    assert url == SLACK_URL
    assert headers.get("Authorization") == f"Bearer {TOKEN}"


@pytest.mark.asyncio
async def test_a_validated_redirect_to_the_cdn_is_followed_without_the_token():
    """Hop 1 carries the bot token; the CDN hop must not."""
    cdn = "https://files-origin.slack-edge.com/signed/abc"
    svc, fake = _service([_resp(302, location=cdn), _resp(content=b"CDN-BYTES")])

    result = await svc.download_file(TOKEN, SLACK_URL)

    assert result == b"CDN-BYTES"
    assert [c[0] for c in fake.calls] == [SLACK_URL, cdn]
    assert fake.calls[0][1].get("Authorization") == f"Bearer {TOKEN}"
    assert "Authorization" not in fake.calls[1][1], (
        "the redirect hop must not re-send the bot token — that widens the "
        "credential's blast radius to a CDN host"
    )


# ---------------------------------------------------------------------------
# Hop 1 refusals — asserted by hop count, never by `is None` alone
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/x",                  # unrelated host
        "https://files.slack.com.evil.example/x",      # suffix-lookalike
        "https://evilslack.com/x",                     # apex-lookalike
        "https://evil-slack-files.com/x",              # the dotless-suffix bypass
        "http://files.slack.com/x",                    # non-https
        "file:///etc/passwd",                          # non-http scheme
        "https://127.0.0.1/x",                         # loopback
        "https://172.29.0.2:5432/x",                   # a platform-network service
        "",                                            # empty
    ],
)
async def test_hop1_refusals_issue_no_request(url):
    svc, fake = _service([])   # any request at all raises → also proves refusal

    result = await svc.download_file(TOKEN, url)

    assert result is None
    assert fake.calls == [], f"a request was issued for {url!r}: {fake.calls}"


@pytest.mark.asyncio
async def test_refusal_logs_at_error_not_warning(caplog):
    """A fail-closed media gate that goes quiet is the #1932 root cause: the
    outage sat at WARNING and nobody saw it for three months."""
    svc, _ = _service([])
    with caplog.at_level(logging.WARNING, logger="services.slack_service"):
        await svc.download_file(TOKEN, "https://evil.example.com/x")

    records = [r for r in caplog.records if "Refusing to download" in r.message]
    assert records, "the refusal was not logged at all"
    assert all(r.levelno == logging.ERROR for r in records), (
        f"refusal logged at {[r.levelname for r in records]}, must be ERROR"
    )


# ---------------------------------------------------------------------------
# Redirect refusals — the blind-follow this issue is about
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example.com/x",
        "http://files.slack.com/x",                 # downgrade to plaintext
        "https://169.254.169.254/latest/meta-data/",  # cloud metadata
        "https://files.slack.com.evil.example/x",
        "",                                          # 302 with no Location
    ],
)
async def test_off_domain_redirect_is_refused_after_exactly_one_hop(location):
    svc, fake = _service([_resp(302, location=location)])

    result = await svc.download_file(TOKEN, SLACK_URL)

    assert result is None
    assert len(fake.calls) == 1, (
        f"the redirect to {location!r} was followed: {[c[0] for c in fake.calls]}"
    )


@pytest.mark.asyncio
async def test_redirect_budget_is_bounded():
    """A validated-but-endless chain stops at the budget instead of looping."""
    hop = "https://files.slack.com/next"
    svc, fake = _service([_resp(302, location=hop)] * (_MAX_FILE_REDIRECTS + 2))

    result = await svc.download_file(TOKEN, SLACK_URL)

    assert result is None
    assert len(fake.calls) == _MAX_FILE_REDIRECTS + 1, (
        f"expected {_MAX_FILE_REDIRECTS} follows after the first request, "
        f"got {len(fake.calls)} requests"
    )


# ---------------------------------------------------------------------------
# The matcher itself
# ---------------------------------------------------------------------------

def test_source_tier_is_narrower_than_the_redirect_tier():
    """The token-carrying hop must not accept the CDN hosts."""
    cdn = "https://files-origin.slack-edge.com/x"
    assert _is_slack_file_url(cdn) is True
    assert _is_slack_file_source_url(cdn) is False


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://files.slack.com/x", True),
        ("https://slack.com/x", True),                  # apex, via lstrip('.')
        ("https://FILES.SLACK.COM/x", True),            # case-insensitive host
        ("https://notslack.com/x", False),
        ("https://slack.com.evil.example/x", False),
    ],
)
def test_source_matcher(url, expected):
    assert _is_slack_file_source_url(url) is expected


def test_every_allowlist_entry_starts_with_a_dot():
    """A dotless entry degrades to `endswith("slack-files.com")`, which
    `evil-slack-files.com` satisfies — a bypass, not a widening."""
    from services import slack_service as mod

    for tier in (
        mod._SLACK_FILE_SOURCE_HOST_SUFFIXES,
        mod._SLACK_FILE_ALLOWED_HOST_SUFFIXES,
    ):
        for entry in tier:
            assert entry.startswith("."), f"{entry!r} must start with a dot"


def test_download_never_blind_follows():
    """Static backstop: `follow_redirects=True` anywhere in this download path
    reinstates the defect regardless of what the allowlist says."""
    import inspect
    from services import slack_service as mod

    src = inspect.getsource(mod.SlackService.download_file)
    assert "follow_redirects=False" in src
    assert "follow_redirects=True" not in src
