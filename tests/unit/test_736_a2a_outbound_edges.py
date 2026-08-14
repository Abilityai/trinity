"""Edge cases of the #736 outbound A2A endpoint store — the id/name namespace collision (#2174).

`remove_endpoint` filtered out every record matching the ref on EITHER id or
name, while `resolve_endpoint` stopped at the first. Nothing enforces uniqueness
across those two namespaces and ids are visible in the admin GET, so one
`DELETE /api/settings/a2a-endpoints/{ref}` could destroy two endpoints — taking
an AES-256-GCM-encrypted partner credential the operator may hold no other copy
of — and still report a single success. Same class as #1631 (operator-queue ids
assumed globally unique, enforced per-agent).

The fix is first-match-wins, shared with `resolve_endpoint` via one predicate, so
a ref deletes precisely what it resolves to. These tests pin both halves: the
delete is bounded, and the two operations agree about which record a ref means.

True unit tests: no Docker, no backend. The store is a real encrypt/decrypt cycle
over an in-memory `system_settings` stand-in (same fixture shape as
`test_736_a2a_outbound_call.py`), because the record list surviving that cycle is
what the collision is about.
"""
from __future__ import annotations

import pytest

from services import a2a_outbound
from services.a2a_outbound import ResolvedEndpoint

PEER_URL = "https://peer.example.com/a2a"
PEER = ResolvedEndpoint(id="peer", name="peer", url=PEER_URL, credential=None)


@pytest.fixture
def oss_store(monkeypatch):
    """A real envelope round-trip over an in-memory settings row."""
    import secrets

    import database

    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))
    store = {}
    monkeypatch.setattr(database.db, "get_setting_value",
                        lambda key, default=None: store.get(key, default), raising=False)
    monkeypatch.setattr(database.db, "set_setting",
                        lambda key, value: store.__setitem__(key, value), raising=False)
    monkeypatch.setattr("utils.url_validation.validate_a2a_endpoint_url", lambda url: PEER)
    a2a_outbound.clear_provider()
    return store


def _names(records=None) -> list:
    return [r["name"] for r in (records if records is not None else a2a_outbound.list_oss_endpoints())]


# ---------------------------------------------------------------------------
# G7 — the reported defect
# ---------------------------------------------------------------------------

def test_G7_removing_one_endpoint_removes_exactly_one_endpoint(oss_store):
    """The repro from #2174, verbatim: three endpoints, one delete, two gone.

    `alpha`'s id is a legal name for a third endpoint (the admin GET shows ids,
    and `upsert_endpoint` only enforced uniqueness on name), so a delete by that
    id matched two records and destroyed both — including a credential.
    """
    alpha = a2a_outbound.upsert_endpoint("alpha", PEER_URL, "alpha-secret")
    a2a_outbound.upsert_endpoint("beta", PEER_URL, "beta-secret")
    # Pre-existing collision: stored before the #2174 create-time guard, or by any
    # writer that is not `upsert_endpoint`. The delete must be safe regardless.
    records = a2a_outbound._load_endpoint_records()
    records.append({"id": "a2aep_collider01", "name": alpha["id"], "url": PEER_URL,
                    "credential": "gamma-secret"})
    a2a_outbound._store_endpoint_records(records)

    assert len(a2a_outbound.list_oss_endpoints()) == 3

    assert a2a_outbound.remove_endpoint(alpha["id"]) is True

    remaining = a2a_outbound.list_oss_endpoints()
    assert len(remaining) == 2, "a single-target delete removed more than one endpoint"
    assert "beta" in _names(remaining), "an unrelated endpoint was destroyed"
    # First-match-wins: alpha was registered first, so the id-matched record goes
    # and the name-matched one survives — with its credential.
    assert alpha["id"] in _names(remaining)
    survivor = a2a_outbound.resolve_endpoint("bot", "a2aep_collider01")
    assert survivor is not None and survivor.credential == "gamma-secret"


def test_delete_agrees_with_resolve_about_which_record_a_ref_means(oss_store):
    """The invariant behind the fix: whatever a ref RESOLVES to is what it DELETES.

    Checked against the store's own order rather than an assumed one, so it holds
    whichever way a collision was created."""
    alpha = a2a_outbound.upsert_endpoint("alpha", PEER_URL, "alpha-secret")
    records = a2a_outbound._load_endpoint_records()
    records.append({"id": "a2aep_collider01", "name": alpha["id"], "url": PEER_URL,
                    "credential": "gamma-secret"})
    a2a_outbound._store_endpoint_records(records)

    ref = alpha["id"]
    resolved = a2a_outbound.resolve_endpoint("bot", ref)
    assert resolved is not None
    doomed_id = resolved.id

    assert a2a_outbound.remove_endpoint(ref) is True
    surviving_ids = {r["id"] for r in a2a_outbound.list_oss_endpoints()}
    assert doomed_id not in surviving_ids, "delete removed a different record than resolve returned"
    assert len(surviving_ids) == 1


def test_a_second_delete_of_the_same_ref_removes_the_survivor(oss_store):
    """Bounded per call, not per ref: the operator can still clear a collision —
    it just takes one deliberate delete each, which is the point."""
    alpha = a2a_outbound.upsert_endpoint("alpha", PEER_URL, "alpha-secret")
    records = a2a_outbound._load_endpoint_records()
    records.append({"id": "a2aep_collider01", "name": alpha["id"], "url": PEER_URL})
    a2a_outbound._store_endpoint_records(records)

    assert a2a_outbound.remove_endpoint(alpha["id"]) is True
    assert a2a_outbound.remove_endpoint(alpha["id"]) is True
    assert a2a_outbound.list_oss_endpoints() == []
    assert a2a_outbound.remove_endpoint(alpha["id"]) is False


# ---------------------------------------------------------------------------
# The ordinary paths the fix must not disturb
# ---------------------------------------------------------------------------

def test_removing_by_name_still_works(oss_store):
    a2a_outbound.upsert_endpoint("partner", PEER_URL, "the-secret")
    assert a2a_outbound.remove_endpoint("partner") is True
    assert a2a_outbound.resolve_endpoint("bot", "partner") is None


def test_removing_by_name_is_case_insensitive(oss_store):
    a2a_outbound.upsert_endpoint("Partner", PEER_URL)
    assert a2a_outbound.remove_endpoint("pArTnEr") is True
    assert a2a_outbound.list_oss_endpoints() == []


def test_removing_by_id_still_works(oss_store):
    record = a2a_outbound.upsert_endpoint("partner", PEER_URL)
    assert a2a_outbound.remove_endpoint(record["id"]) is True
    assert a2a_outbound.list_oss_endpoints() == []


def test_removing_leaves_every_other_endpoint_alone(oss_store):
    a2a_outbound.upsert_endpoint("one", PEER_URL, "s1")
    a2a_outbound.upsert_endpoint("two", PEER_URL, "s2")
    a2a_outbound.upsert_endpoint("three", PEER_URL, "s3")
    assert a2a_outbound.remove_endpoint("two") is True
    assert _names() == ["one", "three"]
    assert a2a_outbound.resolve_endpoint("bot", "one").credential == "s1"
    assert a2a_outbound.resolve_endpoint("bot", "three").credential == "s3"


@pytest.mark.parametrize("ref", ["", "   ", "\t\n", "nope", "a2aep_doesnotexist"])
def test_a_miss_or_an_empty_ref_returns_false_and_changes_nothing(oss_store, ref):
    a2a_outbound.upsert_endpoint("partner", PEER_URL, "keep-me")
    before = oss_store.get(a2a_outbound.A2A_ENDPOINTS_SETTING)

    assert a2a_outbound.remove_endpoint(ref) is False

    assert oss_store.get(a2a_outbound.A2A_ENDPOINTS_SETTING) == before, \
        "a no-op delete rewrote the stored envelope"
    assert a2a_outbound.resolve_endpoint("bot", "partner").credential == "keep-me"


def test_removing_from_an_empty_store_is_false_not_an_error(oss_store):
    assert a2a_outbound.remove_endpoint("anything") is False


# ---------------------------------------------------------------------------
# The create-time guard: stop new collisions at the source (#2174, additive)
# ---------------------------------------------------------------------------

def test_a_new_endpoint_may_not_be_named_after_an_existing_id(oss_store):
    alpha = a2a_outbound.upsert_endpoint("alpha", PEER_URL, "alpha-secret")
    with pytest.raises(a2a_outbound.EndpointValidationError) as e:
        a2a_outbound.upsert_endpoint(alpha["id"], PEER_URL, "gamma-secret")
    assert "id of another registered endpoint" in str(e.value)
    assert len(a2a_outbound.list_oss_endpoints()) == 1


def test_the_guard_is_case_insensitive(oss_store):
    alpha = a2a_outbound.upsert_endpoint("alpha", PEER_URL)
    with pytest.raises(a2a_outbound.EndpointValidationError):
        a2a_outbound.upsert_endpoint(alpha["id"].upper(), PEER_URL)


def test_an_already_stored_collision_stays_editable(oss_store):
    """The guard must not strand an operator in the state it exists to prevent:
    a collision written before it shipped is still updatable and removable.

    Note the deliberate asymmetry this pins. `upsert_endpoint` is **update-by-name**
    (its shipped contract, matching the enterprise registration path), so passing
    the colliding string edits the record NAMED that. `resolve_endpoint` /
    `remove_endpoint` are **by id or name**, so the same string reaches the
    id-owning record first. Both are documented and neither is destructive; the
    create-time guard above is what stops the state arising in the first place.
    """
    alpha = a2a_outbound.upsert_endpoint("alpha", PEER_URL, "alpha-secret")
    records = a2a_outbound._load_endpoint_records()
    records.append({"id": "a2aep_collider01", "name": alpha["id"], "url": PEER_URL,
                    "credential": "gamma-secret"})
    a2a_outbound._store_endpoint_records(records)

    updated = a2a_outbound.upsert_endpoint(alpha["id"], "https://peer.example.com/a2a/moved")

    assert updated["id"] == "a2aep_collider01", "update-by-name edited the record named that"
    assert len(a2a_outbound.list_oss_endpoints()) == 2, "an update minted a new record"
    # alpha is untouched: same url, credential intact.
    still_alpha = a2a_outbound.resolve_endpoint("bot", "alpha")
    assert still_alpha.url == PEER_URL and still_alpha.credential == "alpha-secret"
    # ...and the collision is removable, one deliberate delete at a time.
    assert a2a_outbound.remove_endpoint("a2aep_collider01") is True
    assert _names() == ["alpha"]


def test_an_ordinary_name_is_unaffected_by_the_guard(oss_store):
    a2a_outbound.upsert_endpoint("alpha", PEER_URL)
    a2a_outbound.upsert_endpoint("beta", PEER_URL)
    a2a_outbound.upsert_endpoint("a2aep-not-an-id", PEER_URL)
    assert _names() == ["alpha", "beta", "a2aep-not-an-id"]
