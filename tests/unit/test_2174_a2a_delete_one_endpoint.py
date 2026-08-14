"""Bounding `remove_endpoint` — one ref, one record (#2174).

The defect itself is pinned by `test_736_a2a_outbound_edges.py::G7` (the
`/edge-cases` sweep's own repro, un-xfailed by this change). These are the
surrounding cases that fix left open: that delete and resolve agree about which
record a ref means, that a collision is still clearable one deliberate call at a
time, and that the ordinary and no-op paths are untouched — a no-op delete in
particular must not rewrite the stored envelope.

True unit tests: no Docker, no backend. The store is a real encrypt/decrypt cycle
over an in-memory `system_settings` stand-in, for the reason the sweep's fixture
records — stubbing `CredentialEncryptionService` by string target passes alone and
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
    a2a_outbound._backing = backing          # for the envelope-untouched assertion
    return a2a_outbound


def _names(store) -> list:
    return [r["name"] for r in store.list_oss_endpoints()]


def test_delete_agrees_with_resolve_about_which_record_a_ref_means(store):
    """The invariant behind the fix: whatever a ref RESOLVES to is what it DELETES.

    Read from the store's own order rather than an assumed one, so it holds
    whichever way the collision was created."""
    alpha = store.upsert_endpoint("alpha", PEER_URL, "alpha-secret")
    store.upsert_endpoint(alpha["id"], PEER_URL, "gamma-secret")

    resolved = store.resolve_endpoint("bot", alpha["id"])
    assert resolved is not None
    doomed = resolved.id

    assert store.remove_endpoint(alpha["id"]) is True

    surviving = {r["id"] for r in store.list_oss_endpoints()}
    assert doomed not in surviving, "delete removed a different record than resolve returned"
    assert len(surviving) == 1


def test_the_collided_record_keeps_its_credential(store):
    """What the bug destroyed: a partner secret the operator may hold no copy of."""
    alpha = store.upsert_endpoint("alpha", PEER_URL, "alpha-secret")
    collider = store.upsert_endpoint(alpha["id"], PEER_URL, "gamma-secret")

    store.remove_endpoint(alpha["id"])

    survivor = store.resolve_endpoint("bot", collider["id"])
    assert survivor is not None and survivor.credential == "gamma-secret"


def test_a_second_delete_of_the_same_ref_removes_the_survivor(store):
    """Bounded per call, not per ref: an operator can still clear a collision —
    it just takes one deliberate delete each, which is the point."""
    alpha = store.upsert_endpoint("alpha", PEER_URL, "alpha-secret")
    store.upsert_endpoint(alpha["id"], PEER_URL, "gamma-secret")

    assert store.remove_endpoint(alpha["id"]) is True
    assert store.remove_endpoint(alpha["id"]) is True
    assert store.list_oss_endpoints() == []
    assert store.remove_endpoint(alpha["id"]) is False


def test_removing_by_name_is_case_insensitive(store):
    store.upsert_endpoint("Partner", PEER_URL)
    assert store.remove_endpoint("pArTnEr") is True
    assert store.list_oss_endpoints() == []


def test_removing_leaves_every_other_endpoint_and_credential_alone(store):
    store.upsert_endpoint("one", PEER_URL, "s1")
    store.upsert_endpoint("two", PEER_URL, "s2")
    store.upsert_endpoint("three", PEER_URL, "s3")

    assert store.remove_endpoint("two") is True

    assert _names(store) == ["one", "three"]
    assert store.resolve_endpoint("bot", "one").credential == "s1"
    assert store.resolve_endpoint("bot", "three").credential == "s3"


@pytest.mark.parametrize("ref", ["", "   ", "\t\n", "nope", "a2aep_doesnotexist"])
def test_a_miss_or_an_empty_ref_returns_false_and_rewrites_nothing(store, ref):
    """A no-op delete must not touch the envelope: re-encrypting on every miss
    would churn a row holding every partner credential for no reason."""
    store.upsert_endpoint("partner", PEER_URL, "keep-me")
    before = store._backing.get(store.A2A_ENDPOINTS_SETTING)

    assert store.remove_endpoint(ref) is False

    assert store._backing.get(store.A2A_ENDPOINTS_SETTING) == before
    assert store.resolve_endpoint("bot", "partner").credential == "keep-me"


def test_removing_from_an_empty_store_is_false_not_an_error(store):
    assert store.remove_endpoint("anything") is False
