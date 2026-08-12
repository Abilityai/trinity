"""#736 — the outbound A2A route, its gates, and the seam behind it.

Split from `test_736_a2a_outbound_transport.py` on purpose: that file drives a
real `httpx` client over `MockTransport` because its properties are transport
properties; this one drives the FastAPI route and the orchestration service,
where the properties are authorization, dedup identity, refusal mapping and the
fail-closed seam.

The single most valuable test here is `test_loopback_round_trip_against_trinitys_own_inbound_server`.
Everything else proves a refusal, and a suite of refusals with no successful
round trip proves a feature that is switched off.
"""
import asyncio
import json
import os
import sys
import types

import httpx
import pytest

_BACKEND = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend")
)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import dependencies as deps  # noqa: E402
import routers.a2a as a2a  # noqa: E402
from services import a2a_client, a2a_outbound, a2a_outbound_service  # noqa: E402
from utils.url_validation import ValidatedPublicUrl  # noqa: E402

pytestmark = pytest.mark.unit

PEER = ValidatedPublicUrl(
    url="https://peer.example.com/a2a/bot",
    hostname="peer.example.com",
    port=443,
    addresses=("93.184.216.34",),
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _StubProvider:
    """An endpoint provider standing in for the OSS `system_settings` one."""

    def __init__(self, endpoints):
        self.endpoints = endpoints

    def resolve_endpoint(self, agent_name, ref):
        return self.endpoints.get(ref)

    def list_endpoints(self, agent_name):
        return [
            {"id": e.id, "name": e.name, "url": e.url, "has_credentials": bool(e.credential)}
            for e in self.endpoints.values()
        ]


#: The real kill-switch resolver, captured before the autouse fixture below
#: replaces it. The fixture forces the feature ON for every other test, so a
#: kill-switch test calling the module attribute would be asserting against the
#: fixture's lambda rather than the resolution ladder.
_REAL_IS_ENABLED = a2a_outbound_service.is_outbound_enabled


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """Enabled feature, no rate limiting, no dedup, no real DNS, clean seam."""
    a2a_outbound.clear_provider()
    a2a_client.clear_dialect_cache()
    monkeypatch.setattr(a2a_outbound_service, "is_outbound_enabled", lambda: True)
    monkeypatch.setattr(a2a_outbound_service, "_enforce_bounds", lambda agent_name: None)

    async def _no_activity(*a, **k):
        return None

    monkeypatch.setattr(a2a_outbound_service, "_record_activity", _no_activity)

    async def _validate(url):
        return PEER

    monkeypatch.setattr(a2a_client, "validate_endpoint", _validate)
    yield
    a2a_outbound.clear_provider()


@pytest.fixture()
def endpoint():
    ep = a2a_outbound.ResolvedEndpoint(
        id="a2aep_1", name="partner", url=PEER.url, credential="s3cret-token"
    )
    a2a_outbound.register_provider(_StubProvider({"partner": ep, "a2aep_1": ep}))
    return ep


CARD = {"protocolVersion": "0.3.0", "name": "Peer", "url": PEER.url}


def _factory(handler, record=None):
    def _make(timeout):
        def _handle(request: httpx.Request) -> httpx.Response:
            if record is not None:
                record.append(request)
            resp = handler(request)
            if resp.is_stream_consumed and "content-encoding" not in resp.headers:
                resp = httpx.Response(
                    resp.status_code, headers=resp.headers,
                    stream=httpx.ByteStream(resp.content),
                )
            return resp

        return httpx.AsyncClient(transport=httpx.MockTransport(_handle), timeout=timeout,
                                 follow_redirects=False, trust_env=False)

    return _make


def _peer(reply_text="ok", record=None):
    """A cooperative peer: serves a card, answers message/send AND tasks/get."""

    def _handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=CARD)
        body = json.loads(request.content)
        params = body.get("params") or {}
        if body.get("method") == "tasks/get":
            text = f"{reply_text}:polled"
        else:
            text = f"{reply_text}:{params['message']['parts'][0]['text']}"
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": body.get("id"),
            "result": {
                "id": params.get("id", "t-1"), "contextId": "c-1", "kind": "task",
                "status": {"state": "completed"},
                "artifacts": [{"artifactId": "a", "parts": [{"kind": "text", "text": text}]}],
            },
        })

    return _factory(_handler, record)


@pytest.fixture()
def client(monkeypatch, endpoint):
    """The route under a TestClient, with a patched client factory."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    calls = []
    factory_holder = {"factory": _peer(record=calls)}

    real_call = a2a_client.call_endpoint
    real_get = a2a_client.get_task

    async def _call_endpoint(**kwargs):
        kwargs.setdefault("client_factory", factory_holder["factory"])
        return await real_call(**kwargs)

    async def _get_task(**kwargs):
        kwargs.setdefault("client_factory", factory_holder["factory"])
        return await real_get(**kwargs)

    monkeypatch.setattr(a2a_client, "call_endpoint", _call_endpoint)
    monkeypatch.setattr(a2a_client, "get_task", _get_task)

    class _Audit:
        entries = []

        async def log(self, **kwargs):
            _Audit.entries.append(kwargs)
            return None

    _Audit.entries = []
    monkeypatch.setattr(a2a, "platform_audit_service", _Audit())

    app = FastAPI()
    app.include_router(a2a.router)
    user = types.SimpleNamespace(id=1, username="alice", email="alice@example.com",
                                 role="user", agent_name=None, mcp_key_id="k1")
    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[deps.get_authorized_agent_by_name] = lambda: "bot"

    return types.SimpleNamespace(
        http=TestClient(app), user=user, audit=_Audit, calls=calls,
        set_factory=lambda f: factory_holder.__setitem__("factory", f),
        app=app,
    )


def _body(**over):
    body = {"endpoint": "partner", "message": "hello", "dedup_label": "step-1"}
    body.update(over)
    return body


# =========================================================================== #
# 1. THE FEATURE WORKS — listed first (F7)
# =========================================================================== #
def test_loopback_round_trip_against_trinitys_own_inbound_server(monkeypatch):
    """Outbound client → Trinity's OWN A2A inbound server, in process.

    This is the highest-value test available: the peer is `a2a_server_router`
    from this very repo, so one assertion covers dialect selection (the inbound
    server dispatches SLASH names), the same-origin pin against a real Trinity
    card, credential attachment, the response allowlist, and #738 federation's
    entire premise — a Trinity calling a Trinity.

    It is also the direct refutation of the issue's "target v1.0 only" note: a
    v1.0-only client sends `SendMessage` and Trinity answers
    `-32601 Method not found`, so federation would be dead on arrival.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    # --- a real Trinity inbound server, in-process -------------------------
    state = {"executions": {}}
    fake_db = types.SimpleNamespace(
        get_a2a_exposed=lambda name: name == "remotebot",
        can_user_access_agent=lambda user, name: True,
        get_execution=lambda eid: state["executions"].get(eid),
        cancel_queued_execution=lambda eid, reason=None: False,
    )
    monkeypatch.setattr(a2a, "db", fake_db)
    monkeypatch.setattr(
        a2a, "get_agent_container",
        lambda name: types.SimpleNamespace(status="running", labels={}),
    )

    async def _tmpl(name, container):
        return {"display_name": name, "capabilities": ["chat"]}

    monkeypatch.setattr(a2a, "_fetch_template_data", _tmpl)

    class _Result:
        execution_id = "exec-1"
        status = "success"
        response = "the remote agent answered"
        error = None

    class _Svc:
        async def execute_task(self, **kwargs):
            state["last"] = kwargs
            return _Result()

    monkeypatch.setattr(a2a, "get_task_execution_service", lambda: _Svc())

    class _Idem:
        def begin(self, scope, key):
            return types.SimpleNamespace(enabled=False, replay=False, in_flight=False,
                                         snapshot=None, key=None)

        def complete(self, *a, **k):
            return None

        def fail(self, *a, **k):
            return None

    monkeypatch.setattr(a2a, "idempotency_service", _Idem())

    class _Audit:
        async def log(self, **kwargs):
            return None

    monkeypatch.setattr(a2a, "platform_audit_service", _Audit())
    monkeypatch.setattr(a2a.rate_limiter, "enforce", lambda *a, **k: None)

    remote = FastAPI()
    remote.include_router(a2a.a2a_server_router)
    remote.dependency_overrides[deps.get_current_user] = lambda: types.SimpleNamespace(
        id=2, username="peer", email="peer@example.com", role="user",
        agent_name=None, mcp_key_id="k2",
    )
    remote_client = TestClient(remote, base_url="https://peer.example.com")

    # --- drive the outbound client at it -----------------------------------
    seen = []

    def _bridge(timeout):
        def _handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            # The pin rewrote the host to the validated IP; route by PATH into
            # the in-process Trinity, exactly as a socket to that IP would.
            path = request.url.path
            if request.method == "GET":
                # The outbound client fetches /.well-known/agent-card.json at
                # the ORIGIN; Trinity serves it under /a2a/{name}/...
                resp = remote_client.get(f"/a2a/remotebot/.well-known/agent-card.json")
            else:
                resp = remote_client.post(
                    "/a2a/remotebot", content=request.content,
                    headers={"content-type": "application/json"},
                )
            return httpx.Response(
                resp.status_code,
                headers={"content-type": resp.headers.get("content-type", "application/json")},
                stream=httpx.ByteStream(resp.content),
            )

        return httpx.AsyncClient(transport=httpx.MockTransport(_handle), timeout=timeout,
                                 follow_redirects=False, trust_env=False)

    # Trinity's card declares `{base}/a2a/remotebot`; the registered endpoint
    # must be same-origin with it, which it is.
    monkeypatch.setenv("PUBLIC_CHAT_URL", "https://peer.example.com")
    a2a_client.clear_dialect_cache()
    result = asyncio.run(a2a_client.call_endpoint(
        endpoint_url="https://peer.example.com/a2a/remotebot",
        credential="remote-mcp-key",
        message="delegate this",
        validated=ValidatedPublicUrl(
            url="https://peer.example.com/a2a/remotebot",
            hostname="peer.example.com", port=443, addresses=("93.184.216.34",),
        ),
        client_factory=_bridge,
    ))

    assert result.state == "completed"
    assert result.text == "the remote agent answered"
    assert result.protocol_version == "0.3"
    # The remote really executed, as an A2A-triggered task.
    assert state["last"]["triggered_by"] == "a2a"
    assert state["last"]["message"] == "delegate this"
    # And the wire really carried v0.3 slash names.
    assert json.loads(seen[1].content)["method"] == "message/send"


def test_route_returns_the_peers_answer(client):
    r = client.http.post("/api/agents/bot/a2a/call", json=_body())
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["success"] is True
    assert payload["state"] == "completed"
    assert payload["text"] == "ok:hello"
    assert payload["endpoint"] == "partner"


def test_the_response_is_an_allowlist_and_never_leaks_the_credential(client):
    r = client.http.post("/api/agents/bot/a2a/call", json=_body())
    assert "s3cret-token" not in r.text
    assert set(r.json()) == {
        "success", "state", "text", "task_id", "context_id",
        "truncated", "protocol_version", "endpoint", "replayed",
    }


# =========================================================================== #
# 2. effect_guard identity — the C2 bug
# =========================================================================== #
def test_two_different_messages_in_one_execution_produce_two_outbound_calls(monkeypatch, endpoint):
    """THE regression test for the most dangerous defect found in review.

    Inheriting `send_message`'s keying — `{recipient}` alone — is correct for a
    notification sink and WRONG for a request/response conversation: a second
    question to the same endpoint inside one execution would be a completed
    replay, and the agent would receive the answer to its FIRST question with
    no error, nothing logged, and no way to notice. It would then reason
    confidently on stale data.

    So: same execution, same endpoint, DIFFERENT messages and different labels
    ⇒ two real calls and two distinct answers.
    """
    calls = []
    factory = _peer(record=calls)

    real_call = a2a_client.call_endpoint

    async def _call(**kwargs):
        kwargs.setdefault("client_factory", factory)
        return await real_call(**kwargs)

    monkeypatch.setattr(a2a_client, "call_endpoint", _call)

    # A REAL effect_guard over an in-memory idempotency store, so the identity
    # is exercised rather than stubbed away.
    store = {}

    def _claim(scope, key):
        if key in store:
            return {"state": "completed", "snapshot": store[key], "execution_id": "exec-1"}
        store[key] = None
        return {"state": "new"}

    import services.idempotency_service as idem

    monkeypatch.setattr(idem.db, "idempotency_claim", _claim, raising=False)
    monkeypatch.setattr(
        idem.db, "idempotency_complete",
        lambda scope, key, execution_id, snapshot: store.__setitem__(key, snapshot),
        raising=False,
    )
    monkeypatch.setattr(idem, "resolve_and_validate_execution",
                        lambda eid, agent: {"id": eid} if eid else None)

    first = asyncio.run(a2a_outbound_service.call_agent(
        agent_name="bot", endpoint_ref="partner", message="question one",
        dedup_label="step-1", execution_id="exec-1",
    ))
    second = asyncio.run(a2a_outbound_service.call_agent(
        agent_name="bot", endpoint_ref="partner", message="question two",
        dedup_label="step-2", execution_id="exec-1",
    ))

    posts = [json.loads(c.content)["params"]["message"]["parts"][0]["text"]
             for c in calls if c.method == "POST"]
    assert posts == ["question one", "question two"], "the second question was swallowed as a replay"
    assert first.result.text == "ok:question one"
    assert second.result.text == "ok:question two"
    assert second.replayed is False


def test_the_same_label_within_one_execution_replays_without_a_second_call(monkeypatch, endpoint):
    """The other half: dedup still WORKS. A genuine re-delivery of the same
    labelled call must not re-send."""
    calls = []
    factory = _peer(record=calls)
    real_call = a2a_client.call_endpoint

    async def _call(**kwargs):
        kwargs.setdefault("client_factory", factory)
        return await real_call(**kwargs)

    monkeypatch.setattr(a2a_client, "call_endpoint", _call)

    store = {}
    import services.idempotency_service as idem

    def _claim(scope, key):
        if key in store:
            return {"state": "completed", "snapshot": store[key], "execution_id": "exec-1"}
        store[key] = None
        return {"state": "new"}

    monkeypatch.setattr(idem.db, "idempotency_claim", _claim, raising=False)
    monkeypatch.setattr(
        idem.db, "idempotency_complete",
        lambda scope, key, execution_id, snapshot: store.__setitem__(key, snapshot),
        raising=False,
    )
    monkeypatch.setattr(idem, "resolve_and_validate_execution",
                        lambda eid, agent: {"id": eid} if eid else None)

    asyncio.run(a2a_outbound_service.call_agent(
        agent_name="bot", endpoint_ref="partner", message="only once",
        dedup_label="same", execution_id="exec-1",
    ))
    again = asyncio.run(a2a_outbound_service.call_agent(
        agent_name="bot", endpoint_ref="partner", message="only once",
        dedup_label="same", execution_id="exec-1",
    ))

    assert len([c for c in calls if c.method == "POST"]) == 1
    assert again.replayed is True
    assert again.result.text == "ok:only once"


def test_dedup_label_is_required_by_the_model():
    """A required parameter keeps the agent's intent legible. Making it optional
    would reintroduce the C2 bug for every caller that omits it."""
    from models import A2ACallRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        A2ACallRequest(endpoint="partner", message="hi")


def test_the_model_rejects_a_url_parameter_outright():
    """The signature change IS the security story. `extra="forbid"` means a
    caller that tries to pass `agent_card_url` gets a 422, not a silently
    ignored field."""
    from models import A2ACallRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        A2ACallRequest(endpoint="p", message="hi", dedup_label="x",
                       agent_card_url="https://attacker.example/")


def test_no_stream_parameter_is_accepted():
    """A parameter that is accepted and silently does not stream is a lie in a
    schema agents read."""
    from models import A2ACallRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        A2ACallRequest(endpoint="p", message="hi", dedup_label="x", stream=True)


def test_an_in_flight_duplicate_is_a_409(client, monkeypatch):
    from services.idempotency_service import EffectInProgressError

    async def _boom(**kwargs):
        raise EffectInProgressError("already in progress")

    monkeypatch.setattr(a2a_outbound_service, "call_agent", _boom)
    r = client.http.post("/api/agents/bot/a2a/call", json=_body())
    assert r.status_code == 409


def test_a_failed_call_releases_the_claim_so_a_retry_is_possible(monkeypatch, endpoint):
    """`effect_guard` releases on exception. Without it a transient peer failure
    would wedge that (endpoint, label) for the whole 24h TTL."""
    released = []
    import services.idempotency_service as idem

    monkeypatch.setattr(idem.db, "idempotency_claim",
                        lambda scope, key: {"state": "new"}, raising=False)
    monkeypatch.setattr(idem.db, "idempotency_release",
                        lambda scope, key: released.append(key), raising=False)
    monkeypatch.setattr(idem, "resolve_and_validate_execution",
                        lambda eid, agent: {"id": eid} if eid else None)

    async def _explode(**kwargs):
        raise a2a_client.A2ACallError("rpc_unreachable", "peer down")

    monkeypatch.setattr(a2a_client, "call_endpoint", _explode)

    with pytest.raises(a2a_client.A2ACallError):
        asyncio.run(a2a_outbound_service.call_agent(
            agent_name="bot", endpoint_ref="partner", message="m",
            dedup_label="l", execution_id="exec-9",
        ))
    assert released, "the claim was not released; a retry would be blocked for the TTL"


# =========================================================================== #
# 3. The fail-closed seam
# =========================================================================== #
def test_no_provider_and_no_stored_endpoints_resolves_nothing(monkeypatch):
    a2a_outbound.clear_provider()
    monkeypatch.setattr(a2a_outbound, "_load_endpoint_records", lambda: [])
    assert a2a_outbound.resolve_endpoint("bot", "anything") is None


def test_a_raising_provider_refuses_rather_than_opening_the_gate():
    """`a2a_gate` fails OPEN and says why that is acceptable — it is not a
    security boundary. This one is, so it inverts that."""
    class _Boom:
        def resolve_endpoint(self, agent_name, ref):
            raise RuntimeError("policy store down")

        def list_endpoints(self, agent_name):
            raise RuntimeError("policy store down")

    a2a_outbound.register_provider(_Boom())
    assert a2a_outbound.resolve_endpoint("bot", "partner") is None
    assert a2a_outbound.list_endpoints("bot") == []


def test_a_provider_returning_a_mock_shaped_object_is_refused():
    """The `isinstance` check is not padding. Under a stubbed `sys.modules` a
    MagicMock module's `resolve_endpoint()` returns a truthy mock with a mock
    `.url` — silently turning fail-closed into fail-open INSIDE the suite that
    is meant to prove it closed."""
    from unittest.mock import MagicMock

    class _Mocky:
        def resolve_endpoint(self, agent_name, ref):
            return MagicMock()

        def list_endpoints(self, agent_name):
            return []

    a2a_outbound.register_provider(_Mocky())
    assert a2a_outbound.resolve_endpoint("bot", "partner") is None


def test_a_provider_returning_an_endpoint_with_no_url_is_refused():
    class _Empty:
        def resolve_endpoint(self, agent_name, ref):
            return a2a_outbound.ResolvedEndpoint(id="x", name="x", url="  ", credential=None)

        def list_endpoints(self, agent_name):
            return []

    a2a_outbound.register_provider(_Empty())
    assert a2a_outbound.resolve_endpoint("bot", "partner") is None


def test_resolved_endpoint_never_reprs_its_credential():
    """`ResolvedEndpoint` crosses a module boundary carrying plaintext. The
    default dataclass repr prints every field, and this codebase already has
    `validation_error_without_input` because rejecting a bad secret at the
    Pydantic boundary was found to ECHO it."""
    ep = a2a_outbound.ResolvedEndpoint(id="i", name="n", url="https://x/",
                                       credential="hunter2")
    assert "hunter2" not in repr(ep)
    assert "hunter2" not in str(ep)
    assert "hunter2" not in f"{ep}"


def test_an_unknown_endpoint_reference_is_a_404(client):
    r = client.http.post("/api/agents/bot/a2a/call", json=_body(endpoint="nope"))
    assert r.status_code == 404
    assert r.json()["detail"]["reason"] == "endpoint_not_found"


# =========================================================================== #
# 4. Kill switch
# =========================================================================== #
def test_both_routes_404_when_the_kill_switch_is_off(client, monkeypatch):
    """404 rather than 403: "this capability is not present" is Trinity's answer
    everywhere else, and it keeps a disabled deployment from advertising a
    surface it will not serve."""
    monkeypatch.setattr(a2a_outbound_service, "is_outbound_enabled", lambda: False)
    assert client.http.post("/api/agents/bot/a2a/call", json=_body()).status_code == 404
    assert client.http.post(
        "/api/agents/bot/a2a/task", json={"endpoint": "partner", "task_id": "t"}
    ).status_code == 404


def test_the_kill_switch_defaults_off(monkeypatch):
    """Default OFF is the whole point: this is the platform's first
    backend-executed, credentialed, agent-triggerable outbound fetcher."""
    import services.settings_service as ss

    monkeypatch.setattr(ss.settings_service, "get_setting", lambda key: None)
    monkeypatch.delenv("A2A_OUTBOUND_ENABLED", raising=False)
    assert _REAL_IS_ENABLED() is False


def test_the_env_leg_opts_in(monkeypatch):
    import services.settings_service as ss

    monkeypatch.setattr(ss.settings_service, "get_setting", lambda key: None)
    monkeypatch.setenv("A2A_OUTBOUND_ENABLED", "true")
    assert _REAL_IS_ENABLED() is True


def test_a_stored_row_overrides_the_env_in_both_directions(monkeypatch):
    import services.settings_service as ss

    monkeypatch.setenv("A2A_OUTBOUND_ENABLED", "true")
    monkeypatch.setattr(ss.settings_service, "get_setting", lambda key: "false")
    assert _REAL_IS_ENABLED() is False


# =========================================================================== #
# 5. Authorization (FR-10)
# =========================================================================== #
def test_an_agent_key_may_only_call_as_itself(client):
    """`AuthorizedAgentByName` resolves an agent key to its OWNER, so without
    this a PERMITTED sibling could burn a neighbour's registered credential.
    Which agents you may reach is a sharing question; whose credential you may
    spend is not."""
    client.user.agent_name = "other-bot"
    r = client.http.post("/api/agents/bot/a2a/call", json=_body())
    assert r.status_code == 403
    client.user.agent_name = None


def test_an_agent_key_calling_as_itself_is_allowed(client):
    client.user.agent_name = "bot"
    r = client.http.post("/api/agents/bot/a2a/call", json=_body())
    assert r.status_code == 200
    client.user.agent_name = None


def test_the_route_is_outside_the_ephemeral_ghost_fence():
    """A ghost's own key is refused by construction, not by a check in the
    route: the fence is an ALLOWLIST at the auth entry point, and these paths
    are not on it. Pinned so nobody "adds it for convenience" — a disposable
    agent running untrusted work is the last principal that should spend a
    stored credential."""
    for method, pattern in deps._EPHEMERAL_ALLOWED_ROUTES:
        assert not pattern.fullmatch("/api/agents/ghost-abc/a2a/call")
        assert not pattern.fullmatch("/api/agents/ghost-abc/a2a/task")


def test_agent_principals_are_NOT_rejected_outright(client):
    """`reject_agent_principal` is deliberately absent: this is a USE of a
    capability an admin granted by registering the endpoint, not a GRANT of one
    (the Invariant #8 line). Rejecting agent principals would make the feature
    unreachable by the only callers it exists for."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(a2a.call_a2a_agent).lstrip())
    fn = tree.body[0]
    body = fn.body[1:] if (fn.body and isinstance(fn.body[0], ast.Expr)
                           and isinstance(fn.body[0].value, ast.Constant)) else fn.body
    code = "\n".join(ast.dump(node) for node in body)
    assert "reject_agent_principal" not in code


# =========================================================================== #
# 6. Error mapping
# =========================================================================== #
@pytest.mark.parametrize(
    "reason,expected",
    [
        ("endpoint_not_https", 400),
        ("endpoint_private_address", 400),
        ("endpoint_dns_failure", 400),
        ("message_too_long", 422),
        ("timeout", 504),
        ("card_origin_mismatch", 502),
        ("card_too_large", 502),
        ("card_encoding", 502),
        ("unsupported_protocol_version", 502),
        ("remote_error", 502),
        ("rpc_unreachable", 502),
    ],
)
def test_refusal_reasons_map_to_stable_status_codes(client, monkeypatch, reason, expected):
    async def _fail(**kwargs):
        raise a2a_client.A2ACallError(reason, "nope")

    monkeypatch.setattr(a2a_outbound_service, "call_agent", _fail)
    r = client.http.post("/api/agents/bot/a2a/call", json=_body())
    assert r.status_code == expected
    assert r.json()["detail"]["reason"] == reason


def test_a_remote_jsonrpc_error_becomes_a_502_and_never_a_success(client):
    """The silent-success path, at the route level: HTTP 200 carrying a
    JSON-RPC error must not reach the agent as an answer."""
    def _handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=CARD)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "x", "error": {
            "code": -32601, "message": "Method not found"}})

    client.set_factory(_factory(_handler))
    r = client.http.post("/api/agents/bot/a2a/call", json=_body())
    assert r.status_code == 502
    assert r.json()["detail"]["reason"] == "remote_error"
    assert r.json()["detail"]["remote_code"] == -32601


# =========================================================================== #
# 7. No Trinity execution row (FR-7)
# =========================================================================== #
def test_no_schedule_executions_row_is_created(client, monkeypatch):
    """An outbound call is an execution of a REMOTE agent. Minting a row would
    pollute fleet cost/analytics, capacity accounting and the canary invariants
    that reason about `running` rows."""
    created = []
    import database

    monkeypatch.setattr(database.db, "create_execution",
                        lambda *a, **k: created.append(a), raising=False)
    client.http.post("/api/agents/bot/a2a/call", json=_body())
    assert created == []


def test_no_new_trigger_constant_was_introduced():
    """A new `triggered_by` value must be added to all three constants
    (`_VALID_TRIGGERS`, `_TRIGGER_BUCKETS`, `_AUTONOMOUS_TRIGGERS`) or it
    silently vanishes from analytics. This feature creates none — pinned so a
    later revision that mints a row is forced to notice."""
    from db.schedules import stats as schedule_stats

    assert "a2a_outbound" not in getattr(schedule_stats, "_TRIGGER_BUCKETS", {})


# =========================================================================== #
# 8. Audit shape
# =========================================================================== #
def test_audit_carries_the_host_and_never_the_url_message_or_credential(client):
    client.http.post("/api/agents/bot/a2a/call", json=_body(message="secret business"))
    entry = client.audit.entries[-1]
    assert entry["event_action"] == "a2a_outbound_call"
    details = entry["details"]
    assert details["host"] == "peer.example.com"
    assert details["endpoint"] == "partner"
    blob = json.dumps(entry, default=str)
    assert "s3cret-token" not in blob
    assert "secret business" not in blob
    assert "/a2a/bot" not in blob      # the full URL, incl. its path


# =========================================================================== #
# 9. Route resolution (Invariant #4)
# =========================================================================== #
def test_the_new_routes_resolve_and_do_not_collide_with_the_agent_catch_all(client):
    """Cheap, and exactly the kind of thing a later refactor breaks silently."""
    paths = {r.path for r in client.app.routes}
    assert "/api/agents/{agent_name}/a2a/call" in paths
    assert "/api/agents/{agent_name}/a2a/task" in paths


# =========================================================================== #
# 10. Poll route
# =========================================================================== #
def test_the_poll_route_returns_the_remote_state(client):
    r = client.http.post("/api/agents/bot/a2a/task",
                         json={"endpoint": "partner", "task_id": "t-1"})
    assert r.status_code == 200
    assert r.json()["state"] == "completed"


def test_the_poll_route_is_not_effect_guarded(client):
    """A poll is a READ. Deduping it would answer "has it finished yet?" from a
    cached snapshot of the last time it had not — the C2 bug in miniature."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(a2a_outbound_service.poll_task).lstrip())
    fn = tree.body[0]
    # Drop the docstring — it EXPLAINS why there is no guard, so a plain
    # substring search over the source finds the explanation and fails.
    body = fn.body[1:] if (fn.body and isinstance(fn.body[0], ast.Expr)
                           and isinstance(fn.body[0].value, ast.Constant)) else fn.body
    code = "\n".join(ast.dump(node) for node in body)
    assert "effect_guard" not in code
