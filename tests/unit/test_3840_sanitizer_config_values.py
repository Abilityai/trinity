"""#3840 — non-secret config values must not enter the exact-value redaction set.

`_load_credential_values()` added the VALUE of every variable whose name
matched a vendor prefix (`FIBERY_.*`, `GITHUB_.*`, `TRINITY_MCP.*`, …). So
`FIBERY_HOST=acme.fibery.io` — a hostname that merely sits next to
`FIBERY_API_TOKEN` — was erased from every reply, and the link
`https://acme.fibery.io/x/y/1` reached the user as
`https://***REDACTED***/x/y/1`.

These tests pin both directions: config values with a non-secret suffix are
delivered intact, and anything whose name still denotes a secret (including a
secret-bearing URL such as a webhook) is redacted exactly as before.
"""
import os
import sys
from pathlib import Path

import pytest

_agent_utils = str(
    Path(__file__).resolve().parents[2] / "docker" / "base-image" / "agent_server" / "utils"
)
if _agent_utils not in sys.path:
    sys.path.insert(0, _agent_utils)

import credential_sanitizer as agent_sanitizer  # noqa: E402
from credential_sanitizer import (  # noqa: E402
    REDACTION_PLACEHOLDER,
    get_credential_values,
    refresh_credential_values,
    sanitize_text,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """No real `~/.env`, and a cache rebuilt from this test's env only."""
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / ".env"))
    yield
    agent_sanitizer._credential_values = None


@pytest.mark.parametrize("name, value", [
    ("FIBERY_HOST", "acme.fibery.io"),
    ("GITHUB_REPO", "acme-org/ops-agent"),
    ("TRINITY_MCP_URL", "http://mcp-server:8080/mcp"),
    ("SLACK_TEAM_ID", "T0123456789"),
    ("AWS_REGION", "eu-central-1"),
    ("GITHUB_ORG", "acme-organisation"),
    ("GOOGLE_PROJECT_ID", "acme-prod-123456"),
    ("FIBERY_WORKSPACE", "acme-workspace"),
])
def test_non_secret_config_values_are_delivered(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    refresh_credential_values()

    assert value not in get_credential_values()
    text = f"See https://{value}/Client_Ops/Client_Tasks/3103 for {value}"
    assert sanitize_text(text) == text


def test_the_reported_fibery_link_survives(monkeypatch):
    monkeypatch.setenv("FIBERY_HOST", "acme.fibery.io")
    monkeypatch.setenv("FIBERY_API_TOKEN", "fibery-secret-token-value")
    refresh_credential_values()

    link = "https://acme.fibery.io/Client_Ops/Client_Tasks/3103"
    assert sanitize_text(link) == link
    # ...while the token beside it is still redacted wherever it appears.
    assert sanitize_text("tok fibery-secret-token-value") == f"tok {REDACTION_PLACEHOLDER}"


def test_env_file_config_values_are_delivered(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "FIBERY_HOST=acme.fibery.io\nFIBERY_API_TOKEN=fibery-secret-token-value\n"
    )
    monkeypatch.delenv("FIBERY_HOST", raising=False)
    monkeypatch.delenv("FIBERY_API_TOKEN", raising=False)
    refresh_credential_values()

    values = get_credential_values()
    assert "acme.fibery.io" not in values
    assert "fibery-secret-token-value" in values


@pytest.mark.parametrize("name, value", [
    # A secret word in the name wins over a non-secret suffix.
    ("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T000/B000/XXXXXXXX"),
    ("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/abcdefgh"),
    ("FIBERY_TOKEN_URL", "https://acme.fibery.io/token/abcdefgh"),
    ("GITHUB_APP_PRIVATE_KEY_URL", "https://vault.example.com/pk"),
    ("SENTRY_AUTH_HOST", "auth-host-value-123"),
    # Vendor-prefixed names without a non-secret suffix are unchanged.
    ("FIBERY_API_TOKEN", "fibery-secret-token-value"),
    ("GITHUB_PAT", "github-pat-like-value"),
    ("DB_PASS", "database-password-value"),
    ("STRIPE_SK", "stripe-secret-value-1"),
    ("TRINITY_MCP_API_KEY", "mcp-key-value-12345"),
    ("DATABASE_DSN", "dsn-with-secret-123"),
])
def test_secret_names_still_enter_the_value_set(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    refresh_credential_values()

    assert value in get_credential_values()
    assert value not in sanitize_text(f"value: {value}")


def test_key_value_redaction_still_uses_vendor_prefixes(monkeypatch):
    """The fix narrows the EXACT-VALUE set only; `KEY=value` text is unchanged."""
    refresh_credential_values()
    assert sanitize_text("FIBERY_HOST=acme.fibery.io") == f"FIBERY_HOST={REDACTION_PLACEHOLDER}"


def test_credentials_inside_a_config_url_are_still_stripped(monkeypatch):
    """A `*_URL` leaves the value set, but userinfo is redacted by shape."""
    monkeypatch.setenv("DATABASE_URL", "postgres://app:s3cretpass@db:5432/app")
    refresh_credential_values()

    out = sanitize_text("connecting to postgres://app:s3cretpass@db:5432/app")
    assert "s3cretpass" not in out


def test_the_name_check_is_linear():
    """#2398 class: a check reached with an unbounded input must stay linear.

    Names come from `os.environ` and `~/.env`, so they are not attacker-sized
    in practice, but nothing bounds them either. ~7ms at 64 KB measured; a
    second is far above that and far below any quadratic regression.
    """
    import time

    for name in ("A" * 65536, "FIBERY_" + "_HOS" * 16384 + "_HOST", "API_KE" * 10922):
        started = time.perf_counter()
        agent_sanitizer._is_credential_value_name(name)
        assert time.perf_counter() - started < 1.0, name[:32]


def test_sanitize_text_never_runs_the_name_check(monkeypatch):
    """The name check belongs to the cached value LOADER, never to the per-text
    hot path that #1661/#2398 had to make linear."""
    monkeypatch.setenv("FIBERY_HOST", "acme.fibery.io")
    refresh_credential_values()

    def boom(_name):
        raise AssertionError("name check reached from sanitize_text")

    monkeypatch.setattr(agent_sanitizer, "_is_credential_value_name", boom)
    sanitize_text("x " * 50000 + "FIBERY_HOST=acme.fibery.io https://acme.fibery.io/a")
