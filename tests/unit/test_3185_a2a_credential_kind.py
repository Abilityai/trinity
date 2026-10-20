"""#3185 Checkpoint B — the credential KIND on the outbound endpoint store.

Checkpoint A taught the client to read a 402 and to attach an x402 payment token
when the resolved endpoint says its credential is one. This file covers the half
that decides *whether it says so*: the store, the request model, the settings
route, and the audit row.

Two properties are the point of the whole checkpoint:

* **The label describes a secret, never stands alone.** `credential_kind` rides
  the credential's existing three write paths (set / leave alone / clear) rather
  than adding a fourth. A kind with no credential under it would read back as
  "a payment token is registered here" to the one operator who most needs the
  truth — the person who has just been handed a 402.
* **The label and the transport agree.** The store infers an omitted kind with
  `a2a_protocol.decode_payment_token`, which is the same predicate the client
  uses to decide whether it may announce the token in-band. A second spelling
  would produce a credential the store calls `payment_token` and the client then
  declines to send as one — a disagreement that reads as a platform bug rather
  than as the remote's refusal.

The transport half (what actually goes on the wire for each kind) is proven in
`test_736_a2a_outbound_transport.py`; the outcome vocabulary in
`test_3185_a2a_payment_outcome.py`. Nothing here re-tests either.

Sync throughout with `TestClient`: `tests/unit/pytest.ini` overrides
`pyproject.toml`, so `asyncio_mode = auto` does not apply.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

import pytest

_BACKEND = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend")
)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import models  # noqa: E402
from services import a2a_client, a2a_outbound, a2a_protocol  # noqa: E402

# Sibling imports — the unit dir is not implicitly importable. The store fixture
# and the admin-principal settings app already exist; a second copy of either is
# how two files come to disagree about what "the store" is.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_736_a2a_outbound_edges import oss_store  # noqa: E402,F401
from test_ent761_outbound_control_oss import app_client  # noqa: E402,F401

URL = "https://peer.example.com/a2a"


def _token(*, nonce: str | None = None, version: int = 2) -> str:
    """A base64url x402 access token, the shape `decode_payment_token` accepts.

    `nonce` under `payload.authorization` is what makes a v3 token single-use —
    one settlement spends it.
    """
    authorization: dict = {"from": "0xabc", "to": "0xdef", "value": "1000"}
    if nonce is not None:
        authorization["nonce"] = nonce
    payload = {
        "x402Version": version,
        "scheme": "exact",
        "network": "base-sepolia",
        "payload": {"signature": "0x" + "ab" * 32, "authorization": authorization},
    }
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return raw


PAYMENT_TOKEN = _token()
SINGLE_USE_TOKEN = _token(nonce="0x" + "11" * 32)
API_KEY = "sk-an-ordinary-looking-api-key-0001"


# =========================================================================== #
# 1. The store
# =========================================================================== #

def test_an_explicit_payment_token_kind_round_trips(oss_store):
    record = a2a_outbound.upsert_endpoint(
        "partner", URL, PAYMENT_TOKEN, credential_kind="payment_token"
    )
    assert record["credential_kind"] == "payment_token"

    resolved = a2a_outbound.resolve_endpoint("bot", "partner")
    assert resolved.credential_kind == "payment_token"
    assert resolved.credential == PAYMENT_TOKEN


def test_a_record_written_before_the_field_existed_resolves_as_an_api_key(oss_store):
    """Additive-safe: the key is simply ABSENT on every pre-#3185 record, and
    absent must mean today's behaviour exactly — Bearer and nothing else."""
    records = [{"id": "a2aep_legacy01", "name": "legacy", "url": URL,
                "credential": PAYMENT_TOKEN}]
    a2a_outbound._store_endpoint_records(records)

    resolved = a2a_outbound.resolve_endpoint("bot", "legacy")
    assert resolved.credential_kind == "api_key"
    # And the read reports the default explicitly rather than a missing key, so
    # an operator can see which slot they filled.
    assert a2a_outbound.list_oss_endpoints()[0]["credential_kind"] == "api_key"


def test_an_omitted_kind_is_inferred_from_the_value(oss_store):
    """T6. The failure this removes: an operator pastes a token bought after a
    402, the slot defaults to `api_key`, the token rides as a Bearer header and
    the remote answers 402 again — with nothing on either side saying why."""
    paid = a2a_outbound.upsert_endpoint("paid", URL, PAYMENT_TOKEN)
    assert paid["credential_kind"] == "payment_token"

    plain = a2a_outbound.upsert_endpoint("plain", URL, API_KEY)
    assert plain["credential_kind"] == "api_key"


def test_an_explicit_kind_beats_the_inference(oss_store):
    """The operator is allowed to be right about their own provider: a token that
    LOOKS like an x402 payload but is used as an ordinary API key stays one."""
    record = a2a_outbound.upsert_endpoint(
        "partner", URL, PAYMENT_TOKEN, credential_kind="api_key"
    )
    assert record["credential_kind"] == "api_key"
    assert a2a_outbound.resolve_endpoint("bot", "partner").credential_kind == "api_key"


def test_the_inferred_kind_is_the_predicate_the_client_sends_on(oss_store):
    """The cross-layer property: a credential the store labels `payment_token`
    is one the client will actually announce in-band.

    Two spellings of "is this an x402 token" would give a label the transport
    silently disagrees with — the endpoint reads as configured for payment and
    keeps sending a Bearer header.
    """
    record = a2a_outbound.upsert_endpoint("paid", URL, PAYMENT_TOKEN)
    assert record["credential_kind"] == "payment_token"
    assert a2a_client._decode_payment_token(PAYMENT_TOKEN) is not None

    plain = a2a_outbound.upsert_endpoint("plain", URL, API_KEY)
    assert plain["credential_kind"] == "api_key"
    assert a2a_client._decode_payment_token(API_KEY) is None


def test_a_kind_only_update_relabels_the_stored_secret(oss_store):
    """The repair path for an operator who pasted a payment token before the
    field existed: re-label without re-typing a secret they may not have."""
    a2a_outbound.upsert_endpoint("partner", URL, API_KEY, credential_kind="api_key")

    record = a2a_outbound.upsert_endpoint("partner", URL, credential_kind="payment_token")
    assert record["credential_kind"] == "payment_token"
    resolved = a2a_outbound.resolve_endpoint("bot", "partner")
    assert resolved.credential == API_KEY, "the relabel destroyed the secret"
    assert resolved.credential_kind == "payment_token"


def test_a_kind_with_no_credential_to_describe_is_refused(oss_store):
    """On create and on an update of an empty slot alike. A kind with nothing
    under it would report `credential_kind: payment_token` for an endpoint that
    sends no credential at all."""
    with pytest.raises(a2a_outbound.EndpointValidationError) as exc:
        a2a_outbound.upsert_endpoint("fresh", URL, credential_kind="payment_token")
    assert "no stored credential" in str(exc.value)

    a2a_outbound.upsert_endpoint("bare", URL)
    with pytest.raises(a2a_outbound.EndpointValidationError):
        a2a_outbound.upsert_endpoint("bare", URL, credential_kind="payment_token")


def test_clearing_the_credential_drops_the_kind_with_it(oss_store):
    a2a_outbound.upsert_endpoint("partner", URL, SINGLE_USE_TOKEN)
    a2a_outbound.upsert_endpoint("partner", URL, clear_credential=True)

    resolved = a2a_outbound.resolve_endpoint("bot", "partner")
    assert resolved.credential is None
    assert resolved.credential_kind == "api_key"
    public = a2a_outbound.list_oss_endpoints()[0]
    # Not merely false — ABSENT. A kind reported for an empty slot is the one
    # wrong answer this read can give about a 402.
    assert "credential_kind" not in public
    assert "credential_single_use" not in public
    stored = a2a_outbound._load_endpoint_records()[0]
    assert "credential_kind" not in stored
    assert "credential_single_use" not in stored


def test_a_kind_together_with_a_clear_is_refused_at_the_store(oss_store):
    """Contradictory instructions about one slot. Whichever won, the caller
    would be told their write succeeded while believing the other happened."""
    a2a_outbound.upsert_endpoint("partner", URL, PAYMENT_TOKEN)
    with pytest.raises(a2a_outbound.EndpointValidationError) as exc:
        a2a_outbound.upsert_endpoint(
            "partner", URL, clear_credential=True, credential_kind="payment_token"
        )
    assert "not both" in str(exc.value)
    # The write was refused, not half-applied.
    assert a2a_outbound.resolve_endpoint("bot", "partner").credential == PAYMENT_TOKEN


def test_an_unknown_kind_is_refused_without_echoing_the_credential(oss_store):
    with pytest.raises(a2a_outbound.EndpointValidationError) as exc:
        a2a_outbound.upsert_endpoint(
            "partner", URL, PAYMENT_TOKEN, credential_kind="bearer-ish"
        )
    message = str(exc.value)
    assert "api_key" in message and "payment_token" in message
    assert PAYMENT_TOKEN not in message
    assert "bearer-ish" not in message, "the refusal echoed operator input"


def test_relabelling_back_to_api_key_leaves_a_pre_3185_shaped_record(oss_store):
    """`api_key` is the ABSENCE of the key — one spelling of the default, not two
    that future readers have to keep in agreement."""
    a2a_outbound.upsert_endpoint("partner", URL, SINGLE_USE_TOKEN,
                                 credential_kind="payment_token")
    a2a_outbound.upsert_endpoint("partner", URL, credential_kind="api_key")

    stored = a2a_outbound._load_endpoint_records()[0]
    assert set(stored) == {"id", "name", "url", "credential"}
    assert a2a_outbound.list_oss_endpoints()[0]["credential_kind"] == "api_key"


def test_a_single_use_token_is_flagged_rather_than_refused(oss_store):
    """T4. An x402 v3 token authorises ONE settlement, so the second call with it
    stored is refused by the remote. Flagged, not refused: a provider that issues
    only single-use tokens must stay usable."""
    record = a2a_outbound.upsert_endpoint("partner", URL, SINGLE_USE_TOKEN)
    assert record["credential_kind"] == "payment_token"
    assert record["credential_single_use"] is True
    assert a2a_outbound.list_oss_endpoints()[0]["credential_single_use"] is True


def test_a_reusable_payment_token_is_not_flagged_single_use(oss_store):
    record = a2a_outbound.upsert_endpoint("partner", URL, PAYMENT_TOKEN)
    assert record["credential_kind"] == "payment_token"
    assert "credential_single_use" not in record


def test_replacing_a_single_use_token_with_a_reusable_one_clears_the_flag(oss_store):
    """The flag describes the stored value, so it cannot outlive it — a stale
    `credential_single_use` would tell an operator to re-paste a token that is
    perfectly good."""
    a2a_outbound.upsert_endpoint("partner", URL, SINGLE_USE_TOKEN)
    record = a2a_outbound.upsert_endpoint("partner", URL, PAYMENT_TOKEN)
    assert "credential_single_use" not in record
    assert "credential_single_use" not in a2a_outbound._load_endpoint_records()[0]


def test_an_api_key_is_never_flagged_single_use_even_if_it_decodes(oss_store):
    """An explicit `api_key` label means "do not treat this as payment", and the
    single-use warning is a statement about a payment token."""
    record = a2a_outbound.upsert_endpoint(
        "partner", URL, SINGLE_USE_TOKEN, credential_kind="api_key"
    )
    assert "credential_single_use" not in record


def test_a_slot_with_no_credential_reports_no_kind_at_all(oss_store):
    a2a_outbound.upsert_endpoint("bare", URL)
    public = a2a_outbound.list_oss_endpoints()[0]
    assert public["has_credentials"] is False
    assert "credential_kind" not in public


def test_the_public_record_still_never_carries_the_value(oss_store):
    record = a2a_outbound.upsert_endpoint("partner", URL, SINGLE_USE_TOKEN)
    blob = json.dumps([record] + a2a_outbound.list_oss_endpoints())
    assert SINGLE_USE_TOKEN not in blob
    assert "credential" not in record


@pytest.mark.parametrize("junk", [
    None, 123, "", "   ", "not-base64-at-all", "e30=",            # {} — no x402Version
    base64.b64encode(json.dumps({"x402Version": "2", "payload": {}}).encode()).decode(),
    base64.b64encode(json.dumps({"x402Version": True, "payload": {}}).encode()).decode(),
    base64.b64encode(json.dumps({"x402Version": 2}).encode()).decode(),
    base64.b64encode(json.dumps([1, 2, 3]).encode()).decode(),
])
def test_inference_falls_back_to_api_key_on_anything_that_is_not_a_payload(junk):
    """The fail-SAFE direction: an unrecognised value is an API key. Guessing
    `payment_token` would announce an ordinary secret in-band as a payment."""
    assert a2a_outbound.infer_credential_kind(junk) == "api_key"
    assert a2a_outbound.credential_is_single_use(junk) is False


def test_the_store_and_the_client_share_one_token_codec():
    """One predicate, two callers (Invariant #1's "no second copy of a policy").

    A bare source-text pin would pass against two divergent copies; this asserts
    the shared function is the one BOTH reach, by feeding a token only the shared
    shape check accepts.
    """
    assert a2a_protocol.decode_payment_token(PAYMENT_TOKEN) is not None
    assert a2a_client._decode_payment_token(PAYMENT_TOKEN) == \
        a2a_protocol.decode_payment_token(PAYMENT_TOKEN)
    assert a2a_outbound._decode_token(PAYMENT_TOKEN) == \
        a2a_protocol.decode_payment_token(PAYMENT_TOKEN)


# =========================================================================== #
# 2. The request model
# =========================================================================== #

def test_the_model_accepts_the_two_kinds_and_refuses_a_third():
    from pydantic import ValidationError

    for kind in ("api_key", "payment_token"):
        body = models.A2AOutboundEndpointUpsert(
            name="partner", url=URL, credentials=PAYMENT_TOKEN, credential_kind=kind
        )
        assert body.credential_kind == kind

    with pytest.raises(ValidationError):
        models.A2AOutboundEndpointUpsert(
            name="partner", url=URL, credential_kind="bearer-ish"
        )


def test_the_model_defaults_the_kind_to_none_not_to_api_key():
    """`None` means "infer", which is a different instruction from "this is an
    API key" — collapsing them would make T6 unreachable over HTTP."""
    body = models.A2AOutboundEndpointUpsert(name="partner", url=URL)
    assert body.credential_kind is None


def test_the_model_refuses_a_kind_together_with_clear_credentials():
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc:
        models.A2AOutboundEndpointUpsert(
            name="partner", url=URL, clear_credentials=True,
            credential_kind="payment_token",
        )
    assert "not both" in str(exc.value)


def test_the_422_for_a_refused_kind_does_not_echo_the_credential():
    """The ent#109 pairing again, for the new field: a model-level refusal gets
    its `input` stripped by `validation_error_without_input`, so the guard closes
    the leak rather than relocating it from a 500 into a 422."""
    import asyncio

    from fastapi.exceptions import RequestValidationError
    from pydantic import ValidationError

    from error_handlers import validation_error_without_input

    try:
        models.A2AOutboundEndpointUpsert(
            name="partner", url=URL, credentials=PAYMENT_TOKEN,
            clear_credentials=True, credential_kind="payment_token",
        )
        raise AssertionError("the model accepted kind + clear_credentials")
    except ValidationError as exc:
        response = asyncio.run(
            validation_error_without_input(None, RequestValidationError(exc.errors()))
        )

    body = json.loads(bytes(response.body).decode())
    assert response.status_code == 422
    assert PAYMENT_TOKEN not in json.dumps(body)
    assert "not both" in json.dumps(body), (
        "the 422 must carry the model's own reason — a generic refusal reads "
        "the same as the pre-#3185 `extra=forbid` rejection"
    )


# =========================================================================== #
# 3. The settings route + the audit row
# =========================================================================== #

def test_the_put_reports_and_audits_the_kind_it_wrote(app_client, oss_store):
    r = app_client.http.put("/api/settings/a2a-endpoints", json={
        "name": "partner", "url": URL,
        "credentials": PAYMENT_TOKEN, "credential_kind": "payment_token",
    })
    assert r.status_code == 200, r.text
    assert r.json()["endpoint"]["credential_kind"] == "payment_token"

    details = app_client.audit.entries[-1]["details"]
    assert details["credential_kind"] == "payment_token"
    # The label is audited; the value is not — in any field of the row.
    assert PAYMENT_TOKEN not in json.dumps(details)


def test_the_put_reports_an_inferred_kind_so_the_operator_need_not_know_the_field(
        app_client, oss_store):
    r = app_client.http.put("/api/settings/a2a-endpoints", json={
        "name": "partner", "url": URL, "credentials": PAYMENT_TOKEN,
    })
    assert r.status_code == 200, r.text
    assert r.json()["endpoint"]["credential_kind"] == "payment_token"
    assert app_client.audit.entries[-1]["details"]["credential_kind"] == "payment_token"


def test_the_put_warns_once_about_a_single_use_token(app_client, oss_store):
    """Honest status in the same response as the write: without it, the second
    call reads as a mystery refusal on an endpoint that just worked."""
    r = app_client.http.put("/api/settings/a2a-endpoints", json={
        "name": "partner", "url": URL, "credentials": SINGLE_USE_TOKEN,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["endpoint"]["credential_single_use"] is True
    assert "single settlement" in body["hint"]

    # A reusable token gets no warning — a hint on every write is noise, and
    # noise is what makes the real one unreadable.
    r2 = app_client.http.put("/api/settings/a2a-endpoints", json={
        "name": "other", "url": URL, "credentials": PAYMENT_TOKEN,
    })
    assert "hint" not in r2.json()


def test_the_put_refuses_an_unknown_kind_without_echoing_the_credential(
        app_client, oss_store):
    r = app_client.http.put("/api/settings/a2a-endpoints", json={
        "name": "partner", "url": URL,
        "credentials": PAYMENT_TOKEN, "credential_kind": "bearer-ish",
    })
    assert r.status_code == 422, r.text
    assert PAYMENT_TOKEN not in r.text
    # Refused as an unknown KIND, not as an unknown FIELD: before the field
    # existed `extra="forbid"` answered 422 too, so a bare status assertion
    # passes identically against a build that never learned the parameter.
    assert "credential_kind" in r.text and "extra" not in r.text.lower()
    assert a2a_outbound.list_oss_endpoints() == [], "a refused write still stored"


def test_the_put_refuses_a_kind_with_clear_credentials(app_client, oss_store):
    a2a_outbound.upsert_endpoint("partner", URL, PAYMENT_TOKEN)
    r = app_client.http.put("/api/settings/a2a-endpoints", json={
        "name": "partner", "url": URL,
        "clear_credentials": True, "credential_kind": "payment_token",
    })
    assert r.status_code == 422, r.text
    # The named reason, not merely a 422 — `extra="forbid"` answered 422 for this
    # body before the field existed, so only the message distinguishes the two.
    assert "not both" in r.text
    assert a2a_outbound.resolve_endpoint("bot", "partner").credential == PAYMENT_TOKEN


def test_the_get_shows_the_kind_for_every_registered_endpoint(app_client, oss_store):
    a2a_outbound.upsert_endpoint("paid", URL, PAYMENT_TOKEN)
    a2a_outbound.upsert_endpoint("plain", URL, API_KEY)
    a2a_outbound.upsert_endpoint("bare", URL)

    rows = {row["name"]: row for row in
            app_client.http.get("/api/settings/a2a-endpoints").json()["endpoints"]}
    assert rows["paid"]["credential_kind"] == "payment_token"
    assert rows["plain"]["credential_kind"] == "api_key"
    assert "credential_kind" not in rows["bare"]
    assert PAYMENT_TOKEN not in json.dumps(rows)
