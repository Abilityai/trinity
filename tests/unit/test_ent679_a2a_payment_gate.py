"""ent#679 checkpoint B — the x402 payment gate on the A2A inbound door.

What this file proves, driven at the layer each thing lives in:

* `dependencies.get_user_or_anonymous`: None on 401 ONLY; a 403 is re-raised.
* `a2a_payment_gate.is_priced`: exposure ∧ enabled config ∧ key, each absence.
* `a2a_payment_gate.extract_token`: metadata-first, header fallback, precedence
  when both are present, and every malformed-payload shape falling through.
* `a2a_payment_gate.payment_caller_allowed`: fail-CLOSED, `x402:{payer}` identity.
* The router's anonymous branch over a real `TestClient`: today's 401 bytes for
  a non-priced agent, 402 parity with the paid door, 403 on a rejected token,
  limiter ordering ahead of any DB read, the settled/unsettled/failed Tasks and
  their x402 metadata, replay, and payer-bound `tasks/get` / `tasks/cancel`
  including payer A polling payer B's task.
* The principal path is untouched (the hard line): a Trinity key still runs the
  turn for free, with no facilitator call and no payment log row.
"""
from __future__ import annotations

import base64
import json
import sys
import types
from pathlib import Path

import pytest
from fastapi import HTTPException, status

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import dependencies as deps  # noqa: E402
import routers.a2a as a2a  # noqa: E402
from services import a2a_gate, a2a_payment_gate, a2a_protocol, paid_turn_service  # noqa: E402

pytestmark = pytest.mark.unit

AGENT = "bot"
PAYER = "0xPayerAAA"
OTHER_PAYER = "0xPayerBBB"


# --------------------------------------------------------------------------- #
# dependencies.get_user_or_anonymous
# --------------------------------------------------------------------------- #
class TestUserOrAnonymous:
    """401 degrades, 403 does not. The difference is the whole point."""

    @pytest.mark.asyncio
    async def test_no_token_is_anonymous(self):
        assert await deps.get_user_or_anonymous(object(), token="") is None

    @pytest.mark.asyncio
    async def test_401_degrades_to_anonymous(self, monkeypatch):
        async def _raise(request, token):
            raise HTTPException(status_code=401, detail="Not authenticated")
        monkeypatch.setattr(deps, "get_current_user", _raise)
        assert await deps.get_user_or_anonymous(object(), token="bad") is None

    @pytest.mark.asyncio
    async def test_403_is_reraised_not_degraded(self, monkeypatch):
        """A fenced connector/ephemeral key must NOT become a payer.

        If this collapsed to None, every containment fence inside
        `get_current_user` would turn into a downgrade onto the payment path —
        a refused credential buying the access it was refused.
        """
        async def _raise(request, token):
            raise HTTPException(status_code=403, detail="Connector scope")
        monkeypatch.setattr(deps, "get_current_user", _raise)
        with pytest.raises(HTTPException) as exc:
            await deps.get_user_or_anonymous(object(), token="fenced")
        assert exc.value.status_code == status.HTTP_403_FORBIDDEN

    @pytest.mark.asyncio
    async def test_a_valid_key_resolves_to_its_user(self, monkeypatch):
        sentinel = types.SimpleNamespace(username="alice")

        async def _ok(request, token):
            return sentinel
        monkeypatch.setattr(deps, "get_current_user", _ok)
        assert await deps.get_user_or_anonymous(object(), token="good") is sentinel


# --------------------------------------------------------------------------- #
# is_priced
# --------------------------------------------------------------------------- #
def _config(enabled=True, credits=2):
    return types.SimpleNamespace(
        agent_name=AGENT, enabled=enabled, credits_per_request=credits,
        nvm_environment="sandbox", nvm_plan_id="plan-1", nvm_agent_id="agent-1",
    )


def _fake_db(*, exposed=True, enabled=True, key="sandbox:jwt", bindings=None):
    cfg = _config(enabled=enabled)
    return types.SimpleNamespace(
        get_a2a_exposed=lambda name: exposed and name == AGENT,
        get_nevermined_config_with_key=lambda name: (
            {"config": cfg, "nvm_api_key": key} if name == AGENT else None
        ),
        nevermined_payer_owns_execution=lambda a, e, p: (a, e, p) in (bindings or set()),
    )


class TestIsPriced:
    def test_exposed_and_enabled_is_priced(self):
        priced = a2a_payment_gate.is_priced(AGENT, db=_fake_db())
        assert priced is not None and priced.nvm_api_key == "sandbox:jwt"

    def test_not_exposed_is_not_priced(self):
        assert a2a_payment_gate.is_priced(AGENT, db=_fake_db(exposed=False)) is None

    def test_config_disabled_is_not_priced(self):
        assert a2a_payment_gate.is_priced(AGENT, db=_fake_db(enabled=False)) is None

    def test_missing_key_is_not_priced(self):
        assert a2a_payment_gate.is_priced(AGENT, db=_fake_db(key="")) is None

    def test_unknown_agent_is_not_priced(self):
        assert a2a_payment_gate.is_priced("ghost", db=_fake_db()) is None

    def test_sdk_absence_is_not_fused_into_is_priced(self):
        """"Takes payment" and "can process one now" are different facts.

        Fusing them would answer 401 — "authenticate" — to a caller holding a
        valid token for an agent whose card advertises a price, when no
        credential it could obtain would work. The router answers 501 for that,
        which it can only do if this function does not swallow the case.
        """
        import inspect
        src = inspect.getsource(a2a_payment_gate.is_priced)
        assert "NEVERMINED_AVAILABLE" not in src
        assert "sdk_available" not in inspect.signature(
            a2a_payment_gate.is_priced).parameters


# --------------------------------------------------------------------------- #
# extract_token
# --------------------------------------------------------------------------- #
def _payload(nonce="n1"):
    return {"x402Version": 1, "payload": {"signature": "0xsig", "nonce": nonce}}


def _encoded(payload):
    from payments_py.x402.token import encode_access_token
    return encode_access_token(payload)


def _message(*, payload=None, text="hi"):
    msg = {"parts": [{"kind": "text", "text": text}], "messageId": "m1"}
    if payload is not None:
        msg["metadata"] = {a2a_protocol.X402_PAYLOAD_KEY: payload}
    return msg


class TestExtractToken:
    def test_metadata_payload_is_re_encoded(self):
        token = a2a_payment_gate.extract_token(_message(payload=_payload()), {})
        assert token == _encoded(_payload())

    def test_header_is_the_fallback(self):
        token = a2a_payment_gate.extract_token(
            _message(), {a2a_protocol.X402_PAYMENT_SIGNATURE_HEADER: "hdr-token"})
        assert token == "hdr-token"

    def test_metadata_wins_over_header(self):
        """The SDK's own precedence (`inband_token or header_token`).

        Header-first would let a stale header silently decide what a client
        migrating onto the in-band rail pays with.
        """
        token = a2a_payment_gate.extract_token(
            _message(payload=_payload()),
            {a2a_protocol.X402_PAYMENT_SIGNATURE_HEADER: "hdr-token"})
        assert token == _encoded(_payload())

    @pytest.mark.parametrize("payload", [
        "not-a-dict",
        {"payload": {}},                       # no x402Version
        {"x402Version": "1", "payload": {}},   # version not an int
        {"x402Version": True, "payload": {}},  # bool is not an int here
        {"x402Version": 1},                    # no payload key
    ])
    def test_malformed_payload_falls_through_to_the_header(self, payload):
        token = a2a_payment_gate.extract_token(
            _message(payload=payload),
            {a2a_protocol.X402_PAYMENT_SIGNATURE_HEADER: "hdr-token"})
        assert token == "hdr-token"

    def test_neither_rail_is_no_token(self):
        assert a2a_payment_gate.extract_token(_message(), {}) is None

    def test_unencodable_payload_never_raises(self, monkeypatch):
        """A payload the SDK cannot encode is "no in-band payment", not a 500."""
        monkeypatch.setattr(a2a_payment_gate, "_encode_payload", lambda p: None)
        assert a2a_payment_gate.extract_token(_message(payload=_payload()), {}) is None

    def test_a_hostile_header_mapping_is_no_header(self):
        class _Boom:
            def get(self, _k):
                raise RuntimeError("nope")
        assert a2a_payment_gate.extract_token(_message(), _Boom()) is None


# --------------------------------------------------------------------------- #
# T7 — the allow-list seam, fail-CLOSED on the payment path
# --------------------------------------------------------------------------- #
class TestPaymentCallerAllowed:
    def teardown_method(self):
        a2a_gate.clear_provider()

    def test_oss_no_provider_allows(self):
        a2a_gate.clear_provider()
        assert a2a_payment_gate.payment_caller_allowed(AGENT, PAYER) is True

    def test_identity_is_the_prefixed_wallet(self):
        seen = []

        class _P:
            def is_inbound_allowed(self, agent, identity):
                seen.append((agent, identity))
                return True
        a2a_gate.register_provider(_P())
        assert a2a_payment_gate.payment_caller_allowed(AGENT, PAYER) is True
        assert seen == [(AGENT, f"x402:{PAYER}")]

    def test_a_listed_provider_can_refuse(self):
        class _P:
            def is_inbound_allowed(self, agent, identity):
                return False
        a2a_gate.register_provider(_P())
        assert a2a_payment_gate.payment_caller_allowed(AGENT, PAYER) is False

    def test_provider_error_fails_closed(self):
        """The OPPOSITE bias to `a2a_gate.check_inbound_allowed`, on purpose.

        There the caller is already authenticated as owner/shared, so the
        allow-list is an extra layer over a decision already made and an error
        must not block them. Here the payment IS the authorization, so an error
        must refuse rather than admit an unlisted wallet.
        """
        class _P:
            def is_inbound_allowed(self, agent, identity):
                raise RuntimeError("policy store down")
        a2a_gate.register_provider(_P())
        assert a2a_payment_gate.payment_caller_allowed(AGENT, PAYER) is False

    def test_no_payer_fails_closed_when_a_provider_exists(self):
        class _P:
            def is_inbound_allowed(self, agent, identity):
                return True
        a2a_gate.register_provider(_P())
        assert a2a_payment_gate.payment_caller_allowed(AGENT, None) is False


# --------------------------------------------------------------------------- #
# Router harness — the anonymous branch end to end
# --------------------------------------------------------------------------- #
class _Verify:
    def __init__(self, success=True, payer=PAYER, error=None, retryable=False):
        self.success, self.payer, self.error = success, payer, error
        self.agent_request_id = "req-1"
        self.retryable = retryable


class _Settle:
    def __init__(self, success=True, error=None):
        self.success, self.error = success, error
        self.tx_hash = "0xtx" if success else None
        self.remaining_balance = "41" if success else None
        self.credits_redeemed = "2" if success else None


class _Dec:
    def __init__(self, key=None, scope=None, replay=False, in_flight=False, snapshot=None):
        self.key, self.scope = key, scope
        self.replay, self.in_flight, self.snapshot = replay, in_flight, snapshot
        self.enabled = key is not None


class _Idem:
    """Enough of `idempotency_service` to exercise replay, keyed as the real one."""

    def __init__(self):
        self.store = {}
        self.calls = []

    @staticmethod
    def derive_payment_key(token, body):
        import hashlib
        if not token:
            return None
        return hashlib.sha256(token.encode() + b"\x00" + (body or b"")).hexdigest()

    def begin(self, scope, key):
        self.calls.append(("begin", scope, key))
        if not key:
            return _Dec()
        rec = self.store.get((scope, key))
        if rec is not None:
            return _Dec(key=key, scope=scope, replay=True, snapshot=rec)
        return _Dec(key=key, scope=scope)

    def attach_execution(self, decision, execution_id):
        pass

    def complete(self, decision, execution_id, snapshot):
        if decision.key and not decision.replay:
            self.store[(decision.scope, decision.key)] = snapshot

    def upgrade_snapshot(self, scope, key, snapshot):
        if key:
            self.store[(scope, key)] = snapshot

    def fail(self, decision):
        self.calls.append(("fail", decision.scope, decision.key))


@pytest.fixture()
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    state = {
        "exposed": {AGENT},
        "priced": {AGENT},
        "executions": {},
        "payment_log": [],
        "bindings": set(),          # (agent, execution_id, payer)
        "verify": _Verify(),
        "settle": _Settle(),
        "exec_status": "success",
        "exec_response": "the answer",
        "db_reads": [],
        "built_402": {"scheme": "exact", "x402Version": 1},
        "endpoints": [],
    }

    def _cancel_queued(eid, reason=None):
        row = state["executions"].get(eid)
        if not row or row.get("status") != "queued":
            return False
        row["status"] = "cancelled"
        return True

    def _get_cfg_with_key(name):
        state["db_reads"].append(("config", name))
        if name not in state["priced"]:
            return None
        return {"config": _config(), "nvm_api_key": "sandbox:jwt"}

    def _exposed(name):
        state["db_reads"].append(("exposed", name))
        return name in state["exposed"]

    fake_db = types.SimpleNamespace(
        get_a2a_exposed=_exposed,
        can_user_access_agent=lambda user, name: name in state["exposed"],
        get_execution=lambda eid: state["executions"].get(eid),
        cancel_queued_execution=_cancel_queued,
        get_nevermined_config_with_key=_get_cfg_with_key,
        get_public_channel_model=lambda name: "claude-opus-5",
        nevermined_payer_owns_execution=lambda a, e, p: (a, e, p) in state["bindings"],
        log_nevermined_payment=lambda **kw: state["payment_log"].append(kw),
    )
    monkeypatch.setattr(a2a, "db", fake_db)

    monkeypatch.setattr(
        a2a, "get_agent_container",
        lambda name: types.SimpleNamespace(status="running", labels={})
        if name in state["exposed"] else None)

    async def _tmpl(name, container):
        return {"display_name": name, "capabilities": ["chat"]}
    monkeypatch.setattr(a2a, "_fetch_template_data", _tmpl)

    class _Result:
        def __init__(self):
            self.execution_id = "exec-1"
            self.status = state["exec_status"]
            self.response = state["exec_response"]
            self.error = "boom" if state["exec_status"] == "failed" else None

    async def _adapter(**kwargs):
        state["last_dispatch"] = kwargs
        if state.get("exec_raises"):
            raise RuntimeError("agent unreachable")
        return _Result()
    monkeypatch.setattr(a2a, "dispatch_and_await_terminal", _adapter)

    async def _terminate(agent, eid):
        state["terminated"] = (agent, eid)
        return True
    monkeypatch.setattr(a2a, "terminate_execution_on_agent", _terminate)

    class _Audit:
        async def log(self, **kwargs):
            state.setdefault("audit", []).append(kwargs)
    monkeypatch.setattr(a2a, "platform_audit_service", _Audit())

    idem = _Idem()
    monkeypatch.setattr(a2a, "idempotency_service", idem)

    class _PaymentService:
        def build_402_response(self, config, base_url="", endpoint=None):
            state["endpoints"].append(("build", endpoint))
            if state.get("build_402_raises"):
                raise RuntimeError("bad plan config")
            return state["built_402"]

        async def verify_payment(self, **kw):
            state["endpoints"].append(("verify", kw.get("endpoint")))
            state.setdefault("verifies", []).append(kw["access_token"])
            return state["verify"]

        async def settle_payment_once(self, **kw):
            state["endpoints"].append(("settle", kw.get("endpoint")))
            state.setdefault("settles", []).append(kw.get("execution_id"))
            return state["settle"]

    monkeypatch.setattr(a2a, "get_nevermined_payment_service", lambda: _PaymentService())
    monkeypatch.setattr(a2a, "NEVERMINED_AVAILABLE", True)
    monkeypatch.setattr(a2a, "build_public_channel_caller_prompt", lambda name: "PUBLIC")
    monkeypatch.setattr(a2a.rate_limiter, "enforce",
                        lambda *a_, **k_: state.setdefault("limits", []).append(a_[0]))
    a2a_gate.clear_provider()

    app = FastAPI()
    app.include_router(a2a.a2a_server_router)
    # Anonymous by default — this file's subject is the payment branch.
    app.dependency_overrides[deps.get_user_or_anonymous] = lambda: None

    return types.SimpleNamespace(
        http=TestClient(app), state=state, idem=idem, app=app, deps=deps)


def _send(client, *, payload=None, header=None, text="hi", method="message/send",
          agent=AGENT, rpc_id=1):
    body = {"jsonrpc": "2.0", "id": rpc_id, "method": method,
            "params": {"message": _message(payload=payload, text=text)}}
    headers = {a2a_protocol.X402_PAYMENT_SIGNATURE_HEADER: header} if header else {}
    return client.http.post(f"/a2a/{agent}", json=body, headers=headers)


def _task_of(response):
    return response.json()["result"]


def _payment_meta(task):
    return task["status"]["message"]["metadata"]


# --------------------------------------------------------------------------- #
# The refusals
# --------------------------------------------------------------------------- #
class TestAnonymousRefusals:
    def test_non_priced_agent_gets_todays_401_bytes(self, client):
        client.state["priced"].clear()
        r = _send(client, header="tok")
        assert r.status_code == 401
        assert r.json() == {"detail": "Not authenticated"}
        assert r.headers["WWW-Authenticate"] == "Bearer"

    def test_non_exposed_agent_gets_todays_401_bytes(self, client):
        client.state["exposed"].clear()
        r = _send(client, header="tok")
        assert r.status_code == 401
        assert r.json() == {"detail": "Not authenticated"}

    def test_unknown_agent_gets_todays_401_bytes(self, client):
        """Uniform with a non-priced agent — no enumeration oracle."""
        r = _send(client, header="tok", agent="ghost")
        assert r.status_code == 401
        assert r.json() == {"detail": "Not authenticated"}

    def test_priced_agent_without_the_sdk_is_501_not_401(self, client, monkeypatch):
        monkeypatch.setattr(a2a, "NEVERMINED_AVAILABLE", False)
        r = _send(client, header="tok")
        assert r.status_code == 501
        assert r.json() == {
            "detail": "Nevermined payment integration is not available"}

    def test_the_limiter_runs_before_any_db_read(self, client, monkeypatch):
        """Ordering IS the mitigation: a flood must not reach the facilitator.

        Driven by making the limiter raise and asserting the DB was never
        touched — a limiter placed after `is_priced` would already have paid for
        two reads per hit.
        """
        def _raise(key, *a_, **k_):
            raise HTTPException(status_code=429, detail="slow down")
        monkeypatch.setattr(a2a.rate_limiter, "enforce", _raise)
        client.state["db_reads"].clear()
        r = _send(client, header="tok")
        assert r.status_code == 429
        assert client.state["db_reads"] == []

    def test_both_an_ip_and_an_agent_bucket_are_enforced(self, client):
        _send(client, header="tok")
        keys = client.state["limits"]
        assert any(k.startswith("a2a_pay_ip:") for k in keys)
        assert f"a2a_pay_agent:{AGENT}" in keys

    def test_missing_token_is_402_with_the_paid_doors_bytes(self, client):
        r = _send(client)
        assert r.status_code == 402
        assert r.json() == {
            "detail": "Payment required",
            "payment_required": client.state["built_402"],
            "credits_per_request": 2,
        }
        decoded = json.loads(base64.b64decode(
            r.headers[a2a_protocol.X402_PAYMENT_REQUIRED_HEADER]))
        assert decoded == client.state["built_402"]

    def test_the_402_resource_url_is_the_a2a_door(self, client):
        """#679 E2: an x402 v3 token signs `resourceUrl`, compared origin+path.

        A 402 quoting the paid door would have the caller mint a token that
        cannot authorize `/a2a/{name}` — and a non-Trinity client follows
        `resource.url` verbatim.
        """
        _send(client)
        built = [e for e in client.state["endpoints"] if e[0] == "build"]
        assert built and built[0][1].endswith(f"/a2a/{AGENT}")

    def test_a_broken_plan_config_is_a_500_not_an_unusable_402(self, client):
        client.state["build_402_raises"] = True
        r = _send(client)
        assert r.status_code == 500
        assert r.json() == {"detail": "Failed to build payment requirements"}

    def test_a_rejected_token_is_403_with_a_reject_row(self, client):
        client.state["verify"] = _Verify(success=False, error="insufficient balance")
        r = _send(client, header="bad-token")
        assert r.status_code == 403
        assert r.json()["detail"] == "Payment verification failed"
        assert r.json()["error"] == "insufficient balance"
        actions = [row["action"] for row in client.state["payment_log"]]
        assert actions == ["reject"]

    def test_a_retryable_verify_is_a_retryable_error_not_a_403(self, client):
        """I1: a facilitator timeout is OUR outage, not the payer's problem.

        `retryable` is set on a timeout, an SDK error and a saturated
        concurrency gate (E7) — cases where the facilitator never DECIDED. A 403
        is read by #3209's client as `payment_rejected`, i.e. stop retrying and
        go buy another token, which is the wrong instruction and costs the payer
        money for our unavailability. So: a JSON-RPC error carrying
        `data.retryable`, and never a -32001 (that is A2A TaskNotFound).
        """
        client.state["verify"] = _Verify(
            success=False, error="facilitator timeout", retryable=True)
        r = _send(client, header="tok")

        assert r.status_code == 200          # the error rides in the envelope
        err = r.json()["error"]
        assert err["code"] == a2a_protocol.RPC_INTERNAL_ERROR
        assert err["code"] != a2a_protocol.A2A_TASK_NOT_FOUND
        assert err["data"] == {"code": "verify_unavailable", "retryable": True}

    def test_a_retryable_verify_is_logged_as_an_attempt_not_a_rejection(self, client):
        """A verify that never decided must not read as "this wallet was refused"."""
        client.state["verify"] = _Verify(
            success=False, error="facilitator timeout", retryable=True)
        _send(client, header="tok")

        rows = client.state["payment_log"]
        assert [row["action"] for row in rows] == ["verify"]
        assert rows[0]["success"] is False
        assert rows[0]["error"] == "facilitator timeout"

    def test_a_rejected_token_still_carries_the_discriminator(self, client):
        """A real rejection keeps its 403 bytes (T3) — the paid door's shape."""
        client.state["verify"] = _Verify(success=False, error="expired")
        r = _send(client, header="tok")
        assert r.status_code == 403
        assert r.json() == {"detail": "Payment verification failed", "error": "expired"}

    def test_verify_runs_before_the_dedup_gate(self, client):
        """A rejected token must not consume an idempotency key."""
        client.state["verify"] = _Verify(success=False, error="nope")
        _send(client, header="bad-token")
        assert [c for c in client.idem.calls if c[0] == "begin"] == []

    def test_the_body_cap_precedes_token_extraction(self, client, monkeypatch):
        monkeypatch.setattr(a2a, "_MAX_RPC_BODY_BYTES", 10)
        r = _send(client, header="tok", text="x" * 200)
        assert r.status_code == 200
        assert r.json()["error"]["message"] == "Request body too large"
        assert "verifies" not in client.state

    @pytest.mark.parametrize("raw,msg", [
        ("not json", "Parse error: body is not valid JSON"),
        ('{"jsonrpc":"1.0","method":"message/send"}', "Invalid JSON-RPC 2.0 request"),
    ])
    def test_envelope_refusals_match_the_principal_path(self, client, raw, msg):
        r = client.http.post(f"/a2a/{AGENT}", content=raw,
                             headers={"content-type": "application/json"})
        assert r.status_code == 200
        assert r.json()["error"]["message"] == msg

    def test_message_without_text_is_invalid_params(self, client):
        body = {"jsonrpc": "2.0", "id": 1, "method": "message/send",
                "params": {"message": {"parts": []}}}
        r = client.http.post(f"/a2a/{AGENT}", json=body)
        assert r.json()["error"]["message"] == "message has no text parts"

    def test_an_unknown_method_is_method_not_found(self, client):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tasks/frobnicate", "params": {}}
        r = client.http.post(f"/a2a/{AGENT}", json=body)
        assert r.json()["error"]["code"] == a2a_protocol.RPC_METHOD_NOT_FOUND


# --------------------------------------------------------------------------- #
# T7 on the wire
# --------------------------------------------------------------------------- #
class TestAllowlistOnThePaymentPath:
    def teardown_method(self):
        a2a_gate.clear_provider()

    def test_an_unlisted_payer_is_403_with_a_reject_row_and_no_execution(self, client):
        class _P:
            def is_inbound_allowed(self, agent, identity):
                return False
        a2a_gate.register_provider(_P())
        r = _send(client, header="tok")
        assert r.status_code == 403
        assert "allow-list" in r.json()["detail"]
        assert "last_dispatch" not in client.state
        assert [row["action"] for row in client.state["payment_log"]] == [
            "verify", "reject"]

    def test_the_allowlist_is_consulted_after_verify(self, client):
        """The wallet only exists once the facilitator has answered."""
        seen = []

        class _P:
            def is_inbound_allowed(self, agent, identity):
                seen.append(identity)
                return True
        a2a_gate.register_provider(_P())
        _send(client, header="tok")
        assert seen == [f"x402:{PAYER}"]


# --------------------------------------------------------------------------- #
# The happy paths and the honest-failure paths
# --------------------------------------------------------------------------- #
class TestPaidSend:
    def test_a_settled_turn_is_a_completed_task_with_a_receipt(self, client):
        r = _send(client, payload=_payload())
        task = _task_of(r)
        assert task["status"]["state"] == "completed"
        assert task["artifacts"][0]["parts"][0]["text"] == "the answer"
        meta = _payment_meta(task)
        assert meta[a2a_protocol.X402_STATUS_KEY] == a2a_protocol.X402_STATUS_COMPLETED
        receipt = meta[a2a_protocol.X402_RECEIPTS_KEY][0]
        assert receipt == {
            "payer": PAYER, "transaction": "0xtx",
            "creditsRedeemed": 2, "remainingBalance": "41",
        }
        assert [row["action"] for row in client.state["payment_log"]] == [
            "verify", "verify", "settle"]

    def test_the_turn_runs_with_no_trinity_principal_and_public_channel_settings(
            self, client):
        _send(client, payload=_payload())
        kw = client.state["last_dispatch"]
        assert kw["triggered_by"] == "a2a"
        assert kw["source_user_id"] is None
        assert kw["source_user_email"] is None
        assert kw["source_mcp_key_id"] is None
        assert kw["system_prompt"] == "PUBLIC"
        assert kw["model"] == "claude-opus-5"

    def test_the_in_band_token_never_reaches_the_agent(self, client):
        """The hard line: the credential goes to the facilitator, not the prompt."""
        token = _encoded(_payload())
        _send(client, payload=_payload())
        kw = client.state["last_dispatch"]
        assert token not in json.dumps(kw)
        assert client.state["verifies"] == [token]

    def test_the_dedup_scope_is_namespaced_by_payer(self, client):
        _send(client, payload=_payload())
        scopes = [c[1] for c in client.idem.calls if c[0] == "begin"]
        assert scopes == [f"a2a:{AGENT}:pay:{PAYER}"]

    def test_two_payers_do_not_share_a_replay_namespace(self, client):
        _send(client, payload=_payload())
        client.state["verify"] = _Verify(payer=OTHER_PAYER)
        _send(client, payload=_payload())
        # Same (token, text) → same key, but the scopes differ, so the second
        # payer executes its own turn instead of replaying the first's answer.
        assert client.state["settles"] == ["exec-1", "exec-1"]
        scopes = {c[1] for c in client.idem.calls if c[0] == "begin"}
        assert scopes == {f"a2a:{AGENT}:pay:{PAYER}",
                          f"a2a:{AGENT}:pay:{OTHER_PAYER}"}

    def test_a_retry_of_the_same_token_and_text_replays_without_re_executing(
            self, client):
        first = _task_of(_send(client, payload=_payload()))
        client.state.pop("last_dispatch")
        r = _send(client, payload=_payload())
        assert r.headers["X-Idempotent-Replay"] == "true"
        assert _task_of(r)["status"]["state"] == "completed"
        assert _payment_meta(_task_of(r))[a2a_protocol.X402_STATUS_KEY] == \
            a2a_protocol.X402_STATUS_COMPLETED
        # Neither the LLM nor a second settle ran.
        assert "last_dispatch" not in client.state
        assert client.state["settles"] == ["exec-1"]
        assert _task_of(r)["artifacts"][0]["parts"][0]["text"] == \
            first["artifacts"][0]["parts"][0]["text"]

    def test_a_messageid_change_does_not_fork_a_retry(self, client):
        """#3209's client mints a fresh uuid4 messageId per call.

        Keying the dedup on messageId would make every retry after its 30 s RPC
        timeout a fresh execution AND a fresh settle — the payer pays twice for
        one answer.
        """
        _send(client, payload=_payload())
        client.state.pop("last_dispatch")
        body = {"jsonrpc": "2.0", "id": 9, "method": "message/send",
                "params": {"message": {
                    "parts": [{"kind": "text", "text": "hi"}],
                    "messageId": "a-totally-different-id",
                    "metadata": {a2a_protocol.X402_PAYLOAD_KEY: _payload()},
                }}}
        r = client.http.post(f"/a2a/{AGENT}", json=body)
        assert r.headers.get("X-Idempotent-Replay") == "true"
        assert "last_dispatch" not in client.state

    def test_a_delivered_but_unsettled_turn_keeps_its_artifact(self, client):
        """#1018 deliver-then-reconcile, carried onto this wire.

        `payment-verified` (not `payment-failed`) because #3209's client parses
        a task normally on anything it does not recognise as a refusal — so the
        artifact the payer paid for survives instead of being discarded.
        """
        client.state["settle"] = _Settle(success=False, error="facilitator 503")
        task = _task_of(_send(client, payload=_payload()))
        assert task["status"]["state"] == "completed"
        assert task["artifacts"][0]["parts"][0]["text"] == "the answer"
        meta = _payment_meta(task)
        assert meta[a2a_protocol.X402_STATUS_KEY] == a2a_protocol.X402_STATUS_VERIFIED
        assert meta[a2a_protocol.X402_ERROR_KEY] == {
            "code": "settle_retry_needed", "reason": "facilitator 503"}
        assert a2a_protocol.X402_RECEIPTS_KEY not in meta
        assert "settle_failed" in [row["action"] for row in client.state["payment_log"]]

    def test_a_concurrent_settle_is_named_as_in_progress(self, client):
        client.state["settle"] = _Settle(
            success=False, error="settlement already in progress")
        meta = _payment_meta(_task_of(_send(client, payload=_payload())))
        assert meta[a2a_protocol.X402_ERROR_KEY]["code"] == "settle_in_progress"
        # The concurrently-running settle logs its own outcome, once.
        assert "settle_failed" not in [
            row["action"] for row in client.state["payment_log"]]

    def test_a_failed_turn_is_not_charged_and_carries_no_artifact(self, client):
        client.state["exec_status"] = "failed"
        task = _task_of(_send(client, payload=_payload()))
        assert task["status"]["state"] == "failed"
        assert "artifacts" not in task
        meta = _payment_meta(task)
        assert meta[a2a_protocol.X402_STATUS_KEY] == a2a_protocol.X402_STATUS_VERIFIED
        assert meta[a2a_protocol.X402_ERROR_KEY]["code"] == "execution_failed"
        assert "settles" not in client.state
        assert "settle" not in [row["action"] for row in client.state["payment_log"]]

    def test_a_cancelled_turn_keeps_its_text_and_is_not_charged(self, client):
        client.state["exec_status"] = "cancelled"
        task = _task_of(_send(client, payload=_payload()))
        assert task["status"]["state"] == "canceled"
        assert task["artifacts"][0]["parts"][0]["text"] == "the answer"
        assert _payment_meta(task)[a2a_protocol.X402_ERROR_KEY]["code"] == \
            "execution_cancelled"
        assert "settles" not in client.state

    def test_a_raised_execution_is_a_json_rpc_error_and_no_charge(self, client):
        client.state["exec_raises"] = True
        r = _send(client, payload=_payload())
        assert r.json()["error"]["message"] == "Task execution failed"
        assert "settles" not in client.state
        assert [c for c in client.idem.calls if c[0] == "fail"]

    def test_the_audit_row_has_the_payer_and_no_actor_user(self, client):
        _send(client, payload=_payload())
        row = client.state["audit"][-1]
        assert row["actor_user"] is None
        assert row["details"]["payer"] == PAYER
        assert row["details"]["settled"] is True

    def test_verify_and_settle_are_scoped_to_the_a2a_door(self, client):
        _send(client, payload=_payload())
        for kind, endpoint in client.state["endpoints"]:
            assert endpoint.endswith(f"/a2a/{AGENT}"), kind


class TestPaidStream:
    def _events(self, response):
        return [json.loads(line[len("data: "):])
                for line in response.text.splitlines() if line.startswith("data: ")]

    def test_stream_emits_working_then_the_paid_task(self, client):
        r = _send(client, payload=_payload(), method="message/stream")
        assert r.headers["content-type"].startswith("text/event-stream")
        events = self._events(r)
        assert events[0]["result"]["status"]["state"] == "working"
        final = events[-1]["result"]
        assert final["final"] is True
        assert final["status"]["state"] == "completed"
        assert final["status"]["message"]["metadata"][
            a2a_protocol.X402_STATUS_KEY] == a2a_protocol.X402_STATUS_COMPLETED

    def test_a_missing_token_on_stream_is_still_an_http_402(self, client):
        """A 402 is an HTTP status and cannot be expressed mid-stream, so it
        must be answered before the event stream opens."""
        r = _send(client, method="message/stream")
        assert r.status_code == 402
        assert not r.headers["content-type"].startswith("text/event-stream")

    def test_a_rejected_token_on_stream_is_an_sse_error_not_a_task(self, client):
        client.state["verify"] = _Verify(success=False, error="nope")
        r = _send(client, header="bad", method="message/stream")
        events = self._events(r)
        assert "error" in events[-1]
        assert events[-1]["error"]["message"] == "Payment verification failed"

    def test_the_stream_never_answers_task_not_found_for_a_payment_refusal(self, client):
        """I3: -32001 is A2A TaskNotFound and this is not a task condition.

        A streaming client is told its task does not exist when the truth is
        "buy a token" — an answer it cannot act on, and one the `message/send`
        path never gave. The stream now renders the SAME classification `send`
        does, with `data.code` as the discriminator.
        """
        client.state["verify"] = _Verify(success=False, error="nope")
        err = self._events(_send(client, header="bad", method="message/stream"))[-1]["error"]

        assert err["code"] != a2a_protocol.A2A_TASK_NOT_FOUND
        assert err["code"] == a2a_protocol.RPC_INTERNAL_ERROR
        assert err["data"] == {"code": "payment_rejected", "retryable": False}

    def test_a_retryable_verify_on_stream_keeps_its_retryable_flag(self, client):
        client.state["verify"] = _Verify(
            success=False, error="facilitator busy", retryable=True)
        err = self._events(_send(client, header="tok", method="message/stream"))[-1]["error"]

        assert err["code"] != a2a_protocol.A2A_TASK_NOT_FOUND
        assert err["data"] == {"code": "verify_unavailable", "retryable": True}

    def test_an_unlisted_payer_on_stream_is_named_not_allowed(self, client):
        """The allow-list 403 cannot be an HTTP status mid-stream (T7 + I3).

        It used to flatten into the same `-32603 Task execution failed` an
        in-flight duplicate got, so a client could not tell "you are not
        allowed here" (never retry) from "your own duplicate is still running"
        (retry shortly).
        """
        class _P:
            def is_inbound_allowed(self, agent, identity):
                return False
        a2a_gate.register_provider(_P())
        try:
            err = self._events(
                _send(client, header="tok", method="message/stream"))[-1]["error"]
        finally:
            a2a_gate.clear_provider()

        assert err["code"] != a2a_protocol.A2A_TASK_NOT_FOUND
        assert err["data"] == {"code": "not_allowed", "retryable": False}
        assert "allow-list" in err["message"]


# --------------------------------------------------------------------------- #
# T5 — payer-bound tasks/get and tasks/cancel
# --------------------------------------------------------------------------- #
def _rpc(client, method, exec_id, *, payload=None, header=None, agent=AGENT):
    params = {"id": exec_id}
    if payload is not None:
        params["message"] = _message(payload=payload)
    body = {"jsonrpc": "2.0", "id": 7, "method": method, "params": params}
    headers = {a2a_protocol.X402_PAYMENT_SIGNATURE_HEADER: header} if header else {}
    return client.http.post(f"/a2a/{agent}", json=body, headers=headers)


class TestPayerBoundTaskRpc:
    def test_a_bound_payer_can_read_its_own_task(self, client):
        client.state["executions"]["exec-1"] = {
            "agent_name": AGENT, "status": "success", "response": "the answer"}
        client.state["bindings"].add((AGENT, "exec-1", PAYER))
        task = _task_of(_rpc(client, "tasks/get", "exec-1", header="tok"))
        assert task["status"]["state"] == "completed"
        assert task["artifacts"][0]["parts"][0]["text"] == "the answer"

    def test_payer_a_polling_payer_bs_task_gets_task_not_found(self, client):
        """T5's named case. Byte-identical to an unknown id — no oracle.

        The row EXISTS and belongs to this agent; only the wallet differs, so
        any answer other than the not-found one would confirm its existence to
        a stranger.
        """
        client.state["executions"]["exec-1"] = {
            "agent_name": AGENT, "status": "success", "response": "B's secret answer"}
        client.state["bindings"].add((AGENT, "exec-1", OTHER_PAYER))
        r = _rpc(client, "tasks/get", "exec-1", header="tok")   # verify → PAYER
        assert r.json()["error"] == {
            "code": a2a_protocol.A2A_TASK_NOT_FOUND, "message": "Task not found"}
        assert "B's secret answer" not in r.text

    def test_an_unknown_task_id_gets_the_identical_answer(self, client):
        r = _rpc(client, "tasks/get", "no-such-exec", header="tok")
        assert r.json()["error"] == {
            "code": a2a_protocol.A2A_TASK_NOT_FOUND, "message": "Task not found"}

    def test_no_token_gets_the_identical_answer(self, client):
        client.state["executions"]["exec-1"] = {"agent_name": AGENT, "status": "success"}
        client.state["bindings"].add((AGENT, "exec-1", PAYER))
        r = _rpc(client, "tasks/get", "exec-1")
        assert r.json()["error"]["code"] == a2a_protocol.A2A_TASK_NOT_FOUND

    def test_a_token_that_fails_verify_gets_the_identical_answer(self, client):
        client.state["executions"]["exec-1"] = {"agent_name": AGENT, "status": "success"}
        client.state["bindings"].add((AGENT, "exec-1", PAYER))
        client.state["verify"] = _Verify(success=False, error="expired")
        r = _rpc(client, "tasks/get", "exec-1", header="tok")
        assert r.json()["error"]["code"] == a2a_protocol.A2A_TASK_NOT_FOUND

    def test_a_verify_that_raises_is_not_an_entitlement(self, client, monkeypatch):
        client.state["executions"]["exec-1"] = {"agent_name": AGENT, "status": "success"}
        client.state["bindings"].add((AGENT, "exec-1", PAYER))

        class _Boom:
            def build_402_response(self, *a_, **k_):
                return {}

            async def verify_payment(self, **kw):
                raise RuntimeError("facilitator exploded")
        monkeypatch.setattr(a2a, "get_nevermined_payment_service", lambda: _Boom())
        r = _rpc(client, "tasks/get", "exec-1", header="tok")
        assert r.json()["error"]["code"] == a2a_protocol.A2A_TASK_NOT_FOUND

    def test_the_token_may_arrive_in_band_on_a_poll_too(self, client):
        client.state["executions"]["exec-1"] = {
            "agent_name": AGENT, "status": "running"}
        client.state["bindings"].add((AGENT, "exec-1", PAYER))
        task = _task_of(_rpc(client, "tasks/get", "exec-1", payload=_payload()))
        assert task["status"]["state"] == "working"

    def test_a_bound_payer_can_cancel_a_running_task(self, client):
        client.state["executions"]["exec-1"] = {"agent_name": AGENT, "status": "running"}
        client.state["bindings"].add((AGENT, "exec-1", PAYER))
        task = _task_of(_rpc(client, "tasks/cancel", "exec-1", header="tok"))
        assert task["status"]["state"] == "canceled"
        assert client.state["terminated"] == (AGENT, "exec-1")

    def test_cancelling_a_terminal_task_says_so(self, client):
        client.state["executions"]["exec-1"] = {"agent_name": AGENT, "status": "success"}
        client.state["bindings"].add((AGENT, "exec-1", PAYER))
        r = _rpc(client, "tasks/cancel", "exec-1", header="tok")
        assert r.json()["error"]["code"] == a2a_protocol.A2A_TASK_NOT_CANCELABLE

    def test_an_unbound_payer_cannot_cancel(self, client):
        client.state["executions"]["exec-1"] = {"agent_name": AGENT, "status": "running"}
        client.state["bindings"].add((AGENT, "exec-1", OTHER_PAYER))
        r = _rpc(client, "tasks/cancel", "exec-1", header="tok")
        assert r.json()["error"]["code"] == a2a_protocol.A2A_TASK_NOT_FOUND
        assert "terminated" not in client.state

    def test_resubscribe_is_still_unsupported(self, client):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tasks/resubscribe", "params": {}}
        r = client.http.post(f"/a2a/{AGENT}", json=body)
        assert r.json()["error"]["code"] == a2a_protocol.A2A_UNSUPPORTED


# --------------------------------------------------------------------------- #
# The hard line: the authenticated path is untouched
# --------------------------------------------------------------------------- #
class TestPrincipalPathUnaffected:
    """AC4: internal fleet callers and subscription tenants pay nothing and
    notice nothing. A payment gate that quietly started charging the fleet
    would be the expensive failure, so it is asserted rather than assumed."""

    @pytest.fixture()
    def principal(self, client):
        user = types.SimpleNamespace(id=1, username="alice", email="alice@example.com",
                                     role="user", agent_name=None, mcp_key_id="k1")
        client.app.dependency_overrides[deps.get_user_or_anonymous] = lambda: user
        return client

    def test_a_trinity_key_runs_the_turn_for_free(self, principal):
        task = _task_of(_send(principal, payload=_payload()))
        assert task["status"]["state"] == "completed"
        assert task["artifacts"][0]["parts"][0]["text"] == "the answer"
        # No facilitator call, no payment row, no payment metadata.
        assert "verifies" not in principal.state
        assert principal.state["payment_log"] == []
        assert "message" not in task["status"]

    def test_a_trinity_key_is_not_rate_limited_by_the_paying_buckets(self, principal):
        principal.state.setdefault("limits", []).clear()
        _send(principal, payload=_payload())
        assert not [k for k in principal.state["limits"] if k.startswith("a2a_pay_")]

    def test_the_principal_keeps_its_own_attribution(self, principal):
        _send(principal, payload=_payload())
        kw = principal.state["last_dispatch"]
        assert kw["source_user_id"] == 1
        assert kw["source_user_email"] == "alice@example.com"
        assert kw["source_mcp_key_id"] == "k1"
        # And NOT the public-channel caller prompt the payment path applies.
        assert "system_prompt" not in kw

    def test_a_principal_on_a_priced_agent_still_pays_nothing(self, principal):
        assert AGENT in principal.state["priced"]
        _task_of(_send(principal, payload=_payload()))
        assert principal.state["payment_log"] == []

    def test_a_principal_on_a_non_exposed_agent_still_gets_a_uniform_404(
            self, principal):
        principal.state["exposed"].clear()
        r = _send(principal, payload=_payload())
        assert r.status_code == 404
        assert r.json() == {"detail": "Not found"}


# --------------------------------------------------------------------------- #
# Outcome → Task mapping, at the service layer
# --------------------------------------------------------------------------- #
class TestTaskFromPaidPayload:
    def test_refusal_kinds_are_not_tasks(self):
        for kind in (paid_turn_service.VERIFY_FAILED, paid_turn_service.IN_FLIGHT,
                     paid_turn_service.ABORTED, paid_turn_service.EXECUTION_ERROR):
            outcome = paid_turn_service.PaidTurnOutcome(kind=kind, payload={})
            assert a2a_payment_gate.task_from_paid_payload(
                outcome, task_builder=a2a._task_object) is None

    def test_every_non_refusal_outcome_kind_renders(self):
        """A new outcome kind must be a visible decision, not a silent None."""
        rendered = set(a2a_payment_gate._OUTCOME_RENDER)
        refusals = {paid_turn_service.VERIFY_FAILED, paid_turn_service.IN_FLIGHT,
                    paid_turn_service.ABORTED, paid_turn_service.EXECUTION_ERROR}
        all_kinds = {
            v for k, v in vars(paid_turn_service).items()
            if k.isupper() and isinstance(v, str) and not k.startswith("_")
        }
        assert all_kinds - refusals == rendered
