"""A blank credential leaves the stored secret alone — every spelling (#2175 F5b).

`upsert_endpoint` documents three paths: omit (leave), set, `clear_credential`
(remove). A whitespace-only value was a fourth: it skipped the header-safety
check (`if credential.strip() and …`) and then took the `elif credential:` branch,
because `"   "` is truthy, writing `""` — so `None` and `""` preserved the secret
while `"   "` destroyed it.

Not reachable over HTTP — `A2AOutboundEndpointUpsert._validate_credential`
normalises a blank `SecretStr` to `None`, so the API surface is honest — which is
what makes this a public-module-function defect rather than an API one, and why
the test drives the function rather than the route. The value at stake is a
partner secret the caller may hold no other copy of.

True unit tests: no Docker, no backend. The store is a real encrypt/decrypt cycle
over an in-memory `system_settings` stand-in, for the reason the sibling fixtures
record — stubbing `CredentialEncryptionService` by string target passes alone and
fails inside the full suite once `conftest.py` restores `sys.modules`.
"""
from __future__ import annotations

import pytest

PEER_URL = "https://peer.example.com/a2a"


@pytest.fixture
def store(monkeypatch):
    import secrets

    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))

    import database
    from services import a2a_outbound

    backing = {}
    monkeypatch.setattr(database.db, "get_setting_value",
                        lambda key, default=None: backing.get(key, default), raising=False)
    monkeypatch.setattr(database.db, "set_setting",
                        lambda key, value: backing.__setitem__(key, value), raising=False)
    monkeypatch.setattr("utils.url_validation.validate_a2a_endpoint_url", lambda url: None)
    a2a_outbound.clear_provider()
    return a2a_outbound


def _credential(store, name="partner"):
    resolved = store.resolve_endpoint("bot", name)
    return resolved.credential if resolved else None


# --------------------------------------------------------------------------- #
# The defect
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("blank", ["   ", "\t", "\n", " \t\n ", ""])
def test_a_blank_credential_never_overwrites_a_stored_one(store, blank):
    """`None` and `""` already preserved; `"   "` did not. All spellings now do."""
    store.upsert_endpoint("partner", PEER_URL, "real-secret")
    store.upsert_endpoint("partner", PEER_URL, blank)
    assert _credential(store) == "real-secret", f"a blank {blank!r} destroyed the secret"


def test_omitting_the_credential_still_preserves_it(store):
    store.upsert_endpoint("partner", PEER_URL, "real-secret")
    store.upsert_endpoint("partner", "https://peer.example.com/a2a/moved")
    assert _credential(store) == "real-secret"
    assert store.resolve_endpoint("bot", "partner").url.endswith("/moved")


def test_clear_credential_remains_the_only_removal_path(store):
    store.upsert_endpoint("partner", PEER_URL, "real-secret")
    store.upsert_endpoint("partner", PEER_URL, "   ")
    assert _credential(store) == "real-secret"

    store.upsert_endpoint("partner", PEER_URL, clear_credential=True)
    assert _credential(store) is None


def test_a_blank_credential_on_creation_stores_no_credential(store):
    """The create path takes the same normalisation: blank means "none given",
    never an empty-string credential that `has_credentials` then reports False
    for while the key exists."""
    record = store.upsert_endpoint("partner", PEER_URL, "   ")
    assert record["has_credentials"] is False
    assert _credential(store) is None
    stored = store._load_endpoint_records()[0]
    assert "credential" not in stored, "a blank credential was persisted as a key"


# --------------------------------------------------------------------------- #
# ...without disturbing a real one
# --------------------------------------------------------------------------- #
def test_a_real_credential_is_still_set_and_stripped(store):
    store.upsert_endpoint("partner", PEER_URL, "  real-secret  ")
    assert _credential(store) == "real-secret"


def test_a_real_credential_still_replaces_an_existing_one(store):
    store.upsert_endpoint("partner", PEER_URL, "old-secret")
    store.upsert_endpoint("partner", PEER_URL, "new-secret")
    assert _credential(store) == "new-secret"


def test_clear_credential_wins_over_a_supplied_one(store):
    """Unchanged precedence: `clear_credential` is checked first on both paths."""
    store.upsert_endpoint("partner", PEER_URL, "old-secret")
    store.upsert_endpoint("partner", PEER_URL, "new-secret", clear_credential=True)
    assert _credential(store) is None


def test_the_header_safety_check_still_refuses_an_unsafe_credential(store):
    """The guard the blank case used to skip past. A credential with an interior
    space is still refused — only an ENTIRELY blank one is now "leave it alone"."""
    with pytest.raises(store.EndpointValidationError):
        store.upsert_endpoint("partner", PEER_URL, "has a space")
    with pytest.raises(store.EndpointValidationError):
        store.upsert_endpoint("partner", PEER_URL, "line\nbreak")


def test_the_length_cap_is_still_measured_on_the_raw_value(store):
    """The bound an operator is told about must not move because their secret has
    surrounding whitespace."""
    too_long = " " * 10 + "x" * store.MAX_ENDPOINT_CREDENTIAL_LEN
    with pytest.raises(store.EndpointValidationError) as exc:
        store.upsert_endpoint("partner", PEER_URL, too_long)
    assert "too long" in str(exc.value)


def test_a_blank_credential_never_reaches_the_wire_as_an_empty_bearer(store):
    """The end the defect is measured at: an endpoint that looks credentialed but
    resolves to `""` would send `Authorization: Bearer ` with nothing after it."""
    store.upsert_endpoint("partner", PEER_URL, "   ")
    resolved = store.resolve_endpoint("bot", "partner")
    assert resolved.credential is None, "an empty-string credential survived to the caller"
