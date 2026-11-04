"""URL-userinfo redaction must not swallow compact JSON across keys (#3769).

`redact_url_userinfo` matched `://` up to the next `@` excluding only `/`, `@`
and whitespace. Compact JSON has none of those between a URL value and the next
key, so a body like the AAuth reference verifier's reply

    {"iss":"https://agent.example","sub":"aauth:assistant@agent.example"}

came out as ``{"iss":"https://***@agent.example"}`` — the `sub` key and the
agent's own identity gone, and the result no longer valid JSON. The same rule
runs on every surface that cleans foreign text (A2A replies, AAuth resource
bodies, git and docker errors), on both sides of the backend/agent boundary.

The fix stops the userinfo span at `"`, `<`, `>` and an escaped quote `\\"` —
characters that cannot appear unencoded in a URL's userinfo and that delimit a
JSON string, an escaped nested JSON string, or an HTML/XML attribute. A bare
backslash still matches, so `CORP\\user:pw@proxy` stays redacted. Both copies are pinned
against one shared table.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "src" / "backend"
_AGENT_UTILS = _ROOT / "docker" / "base-image" / "agent_server" / "utils"

if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def _load(name: str, path: Path):
    # Loaded by path without registering in sys.modules: both trees ship a
    # `utils/credential_sanitizer.py`, and the sanitizer imports only stdlib.
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_COPIES = {
    "backend": _load("_cs_3769_backend", _BACKEND / "utils" / "credential_sanitizer.py"),
    "agent": _load("_cs_3769_agent", _AGENT_UTILS / "credential_sanitizer.py"),
}


@pytest.fixture(params=sorted(_COPIES))
def redact(request):
    return _COPIES[request.param].redact_url_userinfo


WHOAMI = '{"iss":"https://agent.example","sub":"aauth:assistant@agent.example"}'

# Bodies with a URL followed later by an `@`-bearing value and NO userinfo:
# must pass through byte-identical.
UNCHANGED = [
    WHOAMI,
    '{"site":"https://example.com","email":"user@example.com"}',
    '[{"url":"https://example.com/a"},{"owner":"ops@example.com"}]',
    # JSON nested inside a JSON string (escaped quotes).
    '{"body":"{\\"iss\\":\\"https://agent.example\\",\\"sub\\":\\"a@b.example\\"}"}',
    # HTML attribute followed by an email.
    '<a href="https://example.com">me@example.com</a>',
    '<link href=https://example.com>me@example.com',
]


@pytest.mark.parametrize("text", UNCHANGED, ids=range(len(UNCHANGED)))
def test_url_then_at_value_without_userinfo_is_untouched(redact, text):
    assert redact(text) == text


def test_whoami_identity_survives_and_stays_valid_json(redact):
    out = redact(WHOAMI)
    assert json.loads(out) == {
        "iss": "https://agent.example",
        "sub": "aauth:assistant@agent.example",
    }


# Real userinfo is still redacted — inside JSON strings too.
REDACTED = [
    ('{"remote":"https://user:token@github.com/o/r.git"}',
     '{"remote":"https://***@github.com/o/r.git"}'),
    ('{"remote":"https://x-access-token:ghp_abc123@github.com/o/r.git","by":"me@example.com"}',
     '{"remote":"https://***@github.com/o/r.git","by":"me@example.com"}'),
    ('{"site":"https://example.com","remote":"https://oauth2:tok@h.example/r"}',
     '{"site":"https://example.com","remote":"https://***@h.example/r"}'),
    ('{"body":"{\\"r\\":\\"https://u:p@h.example/x\\"}"}',
     '{"body":"{\\"r\\":\\"https://***@h.example/x\\"}"}'),
    ("fatal: unable to access 'https://oauth2:tok@github.com/o/r.git/': 403",
     "fatal: unable to access 'https://***@github.com/o/r.git/': 403"),
    ("https://u:p@h/x and git://t@h2/y", "https://***@h/x and git://***@h2/y"),
    ("ssh://git@github.com:22/a", "ssh://***@github.com:22/a"),
    ('<a href="https://u:p@h.example/">x</a>', '<a href="https://***@h.example/">x</a>'),
    # A bare backslash is not a JSON escape: a Windows domain proxy stays redacted.
    ("proxy=http://CORP\\svc:pw@proxy.example:8080", "proxy=http://***@proxy.example:8080"),
]


@pytest.mark.parametrize("text,expected", REDACTED, ids=range(len(REDACTED)))
def test_real_userinfo_is_still_redacted(redact, text, expected):
    out = redact(text)
    assert out == expected
    assert redact(out) == out


def test_redacted_json_stays_valid(redact):
    out = redact('{"remote":"https://user:token@github.com/o/r.git","by":"me@example.com"}')
    assert json.loads(out) == {
        "remote": "https://***@github.com/o/r.git",
        "by": "me@example.com",
    }


def test_copies_agree_on_the_shared_table():
    for text in UNCHANGED + [t for t, _ in REDACTED]:
        assert (_COPIES["backend"].redact_url_userinfo(text)
                == _COPIES["agent"].redact_url_userinfo(text)), text


def test_aauth_body_reaches_the_agent_intact():
    """The exact path the AAuth resource client uses for a response body."""
    from services import a2a_client

    clean, truncated = a2a_client.sanitize_outbound_text(WHOAMI, None)
    assert not truncated
    assert json.loads(clean)["sub"] == "aauth:assistant@agent.example"


def test_outbound_text_still_redacts_userinfo():
    from services import a2a_client

    clean, _ = a2a_client.sanitize_outbound_text(
        '{"site":"https://example.com","remote":"https://u:secretval@h.example/r"}', None)
    assert "secretval" not in clean
    assert json.loads(clean)["site"] == "https://example.com"
