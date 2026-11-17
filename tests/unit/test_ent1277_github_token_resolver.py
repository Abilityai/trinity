"""trinity-enterprise#1277 — repo-scoped GitHub token seam and resolver.

Pins the open-core half:

* `resolve_github_token` walks per-agent PAT, per-user PAT, the registered
  repo-token provider, then the platform PAT;
* with no provider registered the answer equals `resolve_github_pat`'s;
* a provider answering "not covered" falls through to the platform PAT;
* a provider failing on a covered repo raises and never yields the platform PAT;
* `resolve_github_pat` (the create-path resolver that bakes its answer into a
  container) never consults the provider;
* the app credential setting is a registered secret setting: encrypted at rest,
  refused in cleartext, covered by key rotation.

DB-level unit tests against the conftest's throwaway SQLite. No network.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("cryptography")

os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", "ab" * 32)

from database import db  # noqa: E402
from services import github_token_provider as gtp  # noqa: E402
from services import settings_service as ss  # noqa: E402
from services.secret_settings import (  # noqa: E402
    SECRET_SETTING_KEYS,
    SecretSettingWriteError,
    encrypted_key_for,
    looks_like_envelope,
)

pytestmark = pytest.mark.unit

KEY = "github_app_credentials"


class _Provider:
    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.calls = answer, error, []

    async def token_for_repo(self, repo, permissions=None):
        self.calls.append((repo, permissions))
        if self.error:
            raise self.error
        return self.answer


@pytest.fixture(autouse=True)
def _tiers(monkeypatch):
    """Platform PAT only, no per-agent or per-user PAT, no provider."""
    monkeypatch.setattr(ss.db, "get_agent_github_pat", lambda name: None)
    monkeypatch.setattr(ss.db, "get_user_github_pat", lambda uid: None)
    monkeypatch.setattr(ss.settings_service, "get_github_pat", lambda: "ghp_platform")
    gtp.clear_provider()
    yield
    gtp.clear_provider()
    db.delete_setting(KEY)
    db.delete_setting(encrypted_key_for(KEY))


def _resolve(repo, **kw):
    return asyncio.run(ss.resolve_github_token(repo, **kw))


def test_no_provider_matches_the_pat_resolver():
    assert _resolve("acme/app") == ("ghp_platform", "global", None)


def test_nothing_configured(monkeypatch):
    monkeypatch.setattr(ss.settings_service, "get_github_pat", lambda: "")
    assert _resolve("acme/app") == ("", "none", None)


def test_covered_repo_gets_the_provider_token():
    provider = _Provider(gtp.RepoToken("ghs_scoped", "2026-11-17T12:00:00Z"))
    gtp.register_provider(provider)
    assert _resolve("Acme/App", permissions={"contents": "read"}) == (
        "ghs_scoped", "app", "2026-11-17T12:00:00Z")
    assert provider.calls == [("Acme/App", {"contents": "read"})]


def test_uncovered_repo_falls_through_to_the_platform_pat():
    gtp.register_provider(_Provider(None))
    assert _resolve("acme/app") == ("ghp_platform", "global", None)


@pytest.mark.parametrize("tier,patch", [
    ("per_agent", "get_agent_github_pat"),
    ("per_user", "get_user_github_pat"),
])
def test_a_pat_tier_outranks_the_provider(monkeypatch, tier, patch):
    monkeypatch.setattr(ss.db, patch, lambda _: "ghp_own")
    provider = _Provider(gtp.RepoToken("ghs_scoped", "x"))
    gtp.register_provider(provider)
    assert _resolve("acme/app", agent_name="a", owner_id=1) == ("ghp_own", tier, None)
    assert provider.calls == []


@pytest.mark.parametrize("error", [
    gtp.GithubTokenUnavailable("installation suspended"),
    RuntimeError("boom ghs_secret"),
])
def test_a_failed_mint_raises_and_never_yields_the_platform_pat(error):
    gtp.register_provider(_Provider(error=error))
    with pytest.raises(gtp.GithubTokenUnavailable) as raised:
        _resolve("acme/app")
    assert "ghs_secret" not in str(raised.value)


@pytest.mark.parametrize("repo", [
    "", "acme", "acme/app/extra", "https://github.com/acme/app", "acme/../x", "acme/app?x=1",
    "acme/.", "../app", "acme/.git",
])
def test_a_malformed_repo_never_reaches_the_provider(repo):
    provider = _Provider(gtp.RepoToken("ghs_scoped", "x"))
    gtp.register_provider(provider)
    assert _resolve(repo) == ("ghp_platform", "global", None)
    assert provider.calls == []


def test_a_trailing_dot_git_is_removed_before_the_provider_sees_the_repo():
    provider = _Provider(gtp.RepoToken("ghs_scoped", "x"))
    gtp.register_provider(provider)
    _resolve("acme/app.git")
    assert provider.calls == [("acme/app", None)]


def test_the_create_path_resolver_never_consults_the_provider():
    provider = _Provider(gtp.RepoToken("ghs_scoped", "x"))
    gtp.register_provider(provider)
    assert ss.resolve_github_pat(agent_name="a", owner_id=1) == ("ghp_platform", "global")
    assert ss.resolve_github_pat_candidates(agent_name="a", owner_id=1) == [
        ("ghp_platform", "global")]
    assert provider.calls == []


def test_repo_token_repr_hides_the_token():
    assert "ghs_scoped" not in repr(gtp.RepoToken("ghs_scoped", "x"))


# --- the credential setting ---------------------------------------------------

def test_app_credentials_are_a_registered_secret_setting():
    assert KEY in SECRET_SETTING_KEYS


def test_app_credentials_round_trip_encrypted_with_no_env_leg(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_CREDENTIALS", "from-env")
    assert ss.get_stored_secret_setting(KEY) == ""
    ss.set_secret_setting(KEY, '{"private_key": "pem"}')
    assert db.get_setting_value(KEY, None) is None
    assert looks_like_envelope(db.get_setting_value(encrypted_key_for(KEY), None))
    assert ss.get_stored_secret_setting(KEY) == '{"private_key": "pem"}'


def test_stored_secret_read_refuses_a_key_that_is_not_a_secret_setting():
    """The read lazily migrates a legacy cleartext row, so it must not be
    reachable for an ordinary setting."""
    db.set_setting("some_ordinary_setting", "plain")
    with pytest.raises(ValueError):
        ss.get_stored_secret_setting("some_ordinary_setting")
    assert db.get_setting_value("some_ordinary_setting", None) == "plain"
    assert db.get_setting_value(encrypted_key_for("some_ordinary_setting"), None) is None


def test_app_credentials_are_refused_in_cleartext():
    with pytest.raises(SecretSettingWriteError):
        db.set_setting(KEY, "pem")


def test_key_rotation_covers_the_app_credentials():
    path = Path(__file__).resolve().parents[2] / "scripts/deploy/rotate-credential-key.py"
    spec = importlib.util.spec_from_file_location("rotate_credential_key", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert encrypted_key_for(KEY) in module._envelope_setting_keys()
