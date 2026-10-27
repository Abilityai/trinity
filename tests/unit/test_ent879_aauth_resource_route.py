"""ent#879 — the resource-call route + its service seam (checkpoint C).

`POST /api/agents/{name}/aauth/resource-call` through FastAPI's TestClient over
the real ladder (`resource_client`), the real `signer` → `egress` path and the
scripted Senzing-shaped resource + Hellō-shaped person server of
`test_ent879_aauth_resource_client.py` (its fixtures are imported). What is
proven here:

  * the gate ORDER: uniform 404 while inert (even for a malformed body, and
    for both request shapes), then the agent's OWN key (a human, a sibling's
    key, a `system` key → 403 `aauth_agent_only`) BEFORE the allow-list is
    consulted — a refused principal learns nothing about what is listed;
  * the request model is discriminated (call XOR resume) and no request or
    response field can carry a token;
  * every outcome's HTTP shape: `result` / `interaction_required` / `pending` /
    `denied` / `expired` are 200 with `outcome`; refusals are mapped errors
    (403 / 402 / 422 / 429 / 503 / 504 / 502) with `possibly_executed` on a
    POST timeout;
  * the effect guard: a terminal `result` replays from the snapshot (no second
    send, `replayed: true`); a pending / refusal RELEASES the claim so the
    post-consent retry runs for real; a resume never enters the guard; an
    in-flight duplicate is 409;
  * the audit row: `EXECUTION aauth_resource_call` after egress,
    `AUTHORIZATION aauth_resource_refused` before it, details allow-listed —
    never a token, the body, the justification, a handle, a url or a code;
  * the activity row shape (direction `aauth_resource`, host only);
  * composition: registered in `main.py` after the person-token router, the
    `# mcp:` header, the path resolves beside the agent catch-all.

Sync tests with TestClient (tests/unit/pytest.ini has no asyncio_mode).
"""
from __future__ import annotations

import ast
import json
import os
import sys
import time
import types

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import dependencies as deps  # noqa: E402
import routers.aauth_resource as ra  # noqa: E402
import services.idempotency_service as idem  # noqa: E402
from services.aauth import ps_client, resource_call, resource_client as rc, signer  # noqa: E402
from services.aauth import person_token as pt  # noqa: E402

from unit.test_ent878_aauth_person_token import FakePS, agent_token_of  # noqa: E402
from unit.test_ent879_aauth_resource_client import (  # noqa: E402,F401 — fixtures are discovered by name
    AGENT, BODY, PATH, PS, RES, FakePS2, FakeResource, harness, live, wired,
)

pytestmark = pytest.mark.unit

URL = f"/api/agents/{AGENT}/aauth/resource-call"


def _user(**over):
    base = dict(id=1, username="admin", email="admin@example.com", role="admin",
                agent_name=None, mcp_key_id=None, mcp_scope=None)
    base.update(over)
    return types.SimpleNamespace(**base)


AGENT_KEY = _user(agent_name=AGENT, mcp_key_id="k-echo", mcp_scope="agent")
HUMAN = _user()
SIBLING_KEY = _user(agent_name="sibling", mcp_key_id="k-sib", mcp_scope="agent")
SYSTEM_KEY = _user(agent_name=None, mcp_scope="system")

CALL = {"resource": RES, "method": "POST", "path": PATH, "body": {"query": "ACME"}, "dedup_label": "step-1",
        "justification": "sanctions check for the demo", "execution_id": "exec-1", "trigger_source": "mcp"}


@pytest.fixture()
def route(live, monkeypatch):
    """The route over the wired harness, with recorders on every platform seam."""
    from fastapi import FastAPI, Path
    from fastapi.testclient import TestClient

    now = int(time.time())
    state = {"audit": [], "rate": [], "activity": [], "user": AGENT_KEY, "guard": {}}
    monkeypatch.setattr(ra.rate_limiter, "enforce",
                        lambda key, limit, window, detail="": state["rate"].append((key, limit, window)))

    class _Audit:
        async def log(self, **kw):
            state["audit"].append(kw)
    monkeypatch.setattr(ra, "platform_audit_service", _Audit())

    class _Activity:
        async def track_activity(self, **kw):
            state["activity"].append(kw)
            return "act-1"

        async def complete_activity(self, activity_id, status=None, error=None):
            state["activity"].append({"complete": activity_id, "status": status, "error": error})
    import services.activity_service as activity_mod
    monkeypatch.setattr(activity_mod, "activity_service", _Activity())

    # The effect guard over an in-memory store: fresh / in_flight / completed.
    store = state["guard"]

    def _claim(scope, key):
        if key in store:
            snap = store[key]
            return ({"state": "in_flight"} if snap is None
                    else {"state": "completed", "snapshot": snap, "execution_id": "exec-1"})
        store[key] = None
        return {"state": "new"}
    monkeypatch.setattr(idem.db, "idempotency_claim", _claim, raising=False)
    monkeypatch.setattr(idem.db, "idempotency_complete",
                        lambda scope, key, execution_id, snapshot: store.__setitem__(key, snapshot), raising=False)
    monkeypatch.setattr(idem.db, "idempotency_release", lambda scope, key: store.pop(key, None), raising=False)
    monkeypatch.setattr(idem, "resolve_and_validate_execution", lambda eid, agent: {"id": eid} if eid else None)

    app = FastAPI()
    app.include_router(ra.router)

    async def _agent(agent_name: str = Path(...)):
        return agent_name
    app.dependency_overrides[deps.get_authorized_agent_by_name] = _agent
    app.dependency_overrides[deps.get_current_user] = lambda: state["user"]
    http = TestClient(app)

    res, ps = live["res"], live["ps2"]

    def post(body, agent=AGENT):
        return http.post(f"/api/agents/{agent}/aauth/resource-call", json=body)

    # Live-time scripts (the fakes default to the far-future NOW of the service tests).
    def require_auth(req):
        return res.require_auth(req, iat=now, exp=now + 300)

    def auth_ok(req):
        return FakePS2.auth_ok(ps.auth_token_for(req, iat=now, exp=now + 3600))

    def auth_ok_on_poll(req):
        """A poll is a GET: mint against the person token the platform holds (the service-test shape)."""
        import httpx
        held = pt.held_token(AGENT, RES, now=int(time.time())).token
        pseudo = httpx.Request("POST", PS, content=json.dumps({"presented_token": held}).encode())
        return FakePS2.auth_ok(ps.auth_token_for(pseudo, iat=now, exp=now + 3600))

    def bind():
        ps.post_script.append(lambda req: FakePS.ok(ps.person_token_for(agent_token_of(req), iat=now, exp=now + 3600)))

    return types.SimpleNamespace(post=post, state=state, res=res, ps=ps, live=live, now=now,
                                 require_auth=require_auth, auth_ok=auth_ok, auth_ok_on_poll=auth_ok_on_poll, bind=bind)


def _pollable(route):
    """Make every pending record pollable NOW (`Retry-After` is clamped to ≥ 1 s; the route has no fake clock)."""
    redis = route.live["redis"]
    for key in redis.handles():
        rec = json.loads(redis.kv[key])
        rec["next_poll_at"] = 0
        redis.kv[key] = json.dumps(rec)


def _full_chain(route):
    """Script the whole ladder: agent token → person-token → auth-token → 200 with a budget.

    Senzing's metadata says `access_mode: auth-token`, so the client would START
    at the person token (decision #24) and the resource would never challenge
    `person-token`; `agent-token` mode makes every rung visible in `challenges`.
    """
    route.res.metadata["access_mode"] = "agent-token"
    route.bind()
    route.ps.auth_script = [route.auth_ok]
    route.res.script = [FakeResource.require("person-token"), route.require_auth, FakeResource.ok()]


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #
def test_route_is_a_uniform_404_when_aauth_is_inert_for_every_body(route):
    route.live["issuer"] = None
    for body in (CALL, {"pending_handle": "h" * 32}, {}, {"resource": RES, "unknown": 1},
                 {"pending_handle": "short"}, {"resource": RES, "path": "/v1/%41", "dedup_label": "x"}):
        assert route.post(body).status_code == 404, body
    assert route.res.calls == [] and route.state["audit"] == [] and route.state["rate"] == []
    route.live["issuer"] = "https://a.example"
    assert route.post({}).status_code == 422                            # live again: validation speaks


def test_route_admits_only_the_agents_own_key_before_the_allow_list(route):
    route.live["allowed"] = ()                                          # nothing listed: no oracle either way
    for principal in (HUMAN, SIBLING_KEY, SYSTEM_KEY):
        route.state["user"] = principal
        for body in (CALL, {"pending_handle": "h" * 32}):
            r = route.post(body)
            assert r.status_code == 403 and r.json()["detail"]["reason"] == "aauth_agent_only", principal
            assert "resource" in r.json()["detail"]["message"].lower()
    assert route.res.calls == [] and route.state["rate"] == [] and route.state["audit"] == []
    # The sibling's key may not call AS echo even through echo's path (uniform with the human).
    route.state["user"] = SIBLING_KEY
    assert route.post(CALL, agent=AGENT).status_code == 403


def test_request_is_discriminated_call_xor_resume_and_fields_are_bounded(route):
    for bad in ({**CALL, "pending_handle": "h" * 32},                   # both shapes
                {"pending_handle": "h" * 32, "resource": RES},           # resume + a call field
                {"resource": RES, "path": PATH},                         # no dedup_label
                {**CALL, "method": "PUT"}, {**CALL, "path": "v1/x"}, {**CALL, "path": "/v1/a?b=1"},
                {**CALL, "path": "/v1/%41"}, {**CALL, "body": ["not", "an", "object"]},
                {**CALL, "justification": "j" * 501}, {**CALL, "body": {"k": "v" * (64 * 1024)}},
                {"pending_handle": "h" * 32, "wait": 16}, {"pending_handle": "h" * 31}):
        assert route.post(bad).status_code == 422, bad
    assert route.res.calls == [] and route.state["rate"] == []


def test_no_request_or_response_field_can_carry_a_token():
    from models import (AAuthBudget, AAuthResourceCallNew, AAuthResourceCallResponse, AAuthResourceCallResume,
                        AAuthResourceInteraction)
    from typing import Literal, get_args, get_origin

    for model in (AAuthResourceCallNew, AAuthResourceCallResume, AAuthBudget, AAuthResourceInteraction):
        assert not [f for f in model.model_fields if "token" in f or "secret" in f or "key" in f], model
    fields = set(AAuthResourceCallResponse.model_fields)
    assert fields == {"success", "outcome", "resource", "method", "path", "status", "body", "content_type",
                      "truncated", "budget", "token_kind", "challenges", "replayed", "interaction", "reason",
                      "detail", "do_not_retry", "next_step"}
    # The one "token" field is a closed three-word vocabulary, not a string.
    kind = AAuthResourceCallResponse.model_fields["token_kind"].annotation
    inner = next(a for a in get_args(kind) if a is not type(None))
    assert get_origin(inner) is Literal and set(get_args(inner)) == {"agent", "person", "auth"}
    assert set(AAuthResourceCallNew.model_fields) == {"resource", "method", "path", "body", "justification",
                                                      "dedup_label", "execution_id", "trigger_source"}
    assert set(AAuthResourceCallResume.model_fields) == {"pending_handle", "wait", "execution_id", "trigger_source"}


# --------------------------------------------------------------------------- #
# Outcomes
# --------------------------------------------------------------------------- #
def test_result_through_the_full_chain_audits_and_never_echoes_a_token(route):
    _full_chain(route)
    r = route.post(CALL)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] and body["outcome"] == "result" and body["status"] == 200
    assert json.loads(body["body"]) == {"entities": [{"id": 1}]} and body["truncated"] is False
    assert body["token_kind"] == "auth" and body["challenges"] == ["person-token", "auth-token"]
    assert body["budget"] == {"cost": 2, "balance": 38, "unit": "credits"}
    assert body["resource"] == RES and body["method"] == "POST" and body["path"] == PATH and body["replayed"] is False
    assert "no new consent" in body["next_step"]
    assert "eyJ" not in r.text
    # the body the resource saw is the serialised JSON object, signed as sent
    assert json.loads(route.res.calls[-1].content) == {"query": "ACME"}
    assert route.state["rate"] == [("aauth_rc_call:echo", 20, 60), ("aauth_rc:fleet", 120, 60)]

    (row,) = route.state["audit"]
    assert row["event_type"].value == "execution" and row["event_action"] == "aauth_resource_call"
    assert row["target_id"] == AGENT and row["mcp_key_id"] == "k-echo" and row["endpoint"] == URL
    d = row["details"]
    assert d["outcome"] == "result" and d["token_kind"] == "auth" and d["status"] == 200
    assert d["resource"] == RES and d["method"] == "POST" and d["path"] == PATH
    assert d["challenges"] == ["person-token", "auth-token"] and d["budget_cost"] == 2 and d["budget_balance"] == 38
    assert d["execution_id"] == "exec-1" and d["trigger_source"] == "mcp" and d["replayed"] is False
    blob = json.dumps(d)
    assert "eyJ" not in blob and "ACME" not in blob and "sanctions" not in blob and '"id": 1' not in blob

    # the activity row: direction + host + state + token kind, written closed
    act = route.state["activity"]
    assert act[0]["details"] == {"direction": "aauth_resource", "auth": "aauth", "host": "res.example",
                                 "state": "result", "token_kind": "auth"}
    assert act[0]["triggered_by"] == "agent" and act[1]["error"] is None


def test_second_call_reuses_the_auth_token_with_no_person_server_trip(route):
    _full_chain(route)
    assert route.post(CALL).json()["token_kind"] == "auth"
    route.res.script = [FakeResource.ok({"second": True})]
    body = route.post({**CALL, "dedup_label": "step-2"}).json()
    assert body["outcome"] == "result" and body["token_kind"] == "auth" and body["challenges"] == []
    assert body["replayed"] is False and json.loads(body["body"]) == {"second": True}
    assert len(route.ps.auth_posts) == 1 and len(route.ps.posts) == 1


def test_the_same_label_replays_the_snapshot_without_a_second_send(route):
    _full_chain(route)
    first = route.post(CALL).json()
    sends = len(route.res.calls)
    again = route.post({**CALL, "body": {"query": "something else"}})   # the body is not identity (#1084)
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert {k: v for k, v in again.json().items() if k != "replayed"} == {k: v for k, v in first.items() if k != "replayed"}
    assert len(route.res.calls) == sends
    assert route.state["audit"][-1]["details"]["replayed"] is True
    # the snapshot holds the sanitised answer, never a token
    (snap,) = [v for v in route.state["guard"].values() if v]
    assert "eyJ" not in json.dumps(snap) and "handle" not in snap


def test_a_pending_outcome_releases_the_claim_so_the_post_consent_retry_runs(route):
    route.bind()
    route.ps.auth_script = [FakePS2.auth_pending(retry_after="0")]
    route.res.script = [route.require_auth]
    r = route.post(CALL)
    body = r.json()
    assert r.status_code == 200 and body["success"] and body["outcome"] == "interaction_required"
    assert body["challenges"] == ["auth-token"]
    inter = body["interaction"]
    assert inter["link"] == PS + "/auth?code=WXYZ-5678" and inter["code"] == "WXYZ-5678" and inter["url"] == PS + "/auth"
    assert inter["requirement"] == "interaction" and inter["at"] == "person_server" and inter["retry_after"] == 1
    assert "owner" in body["next_step"].lower() and "privately" in body["next_step"].lower()
    assert route.state["guard"] == {}                                   # released, not completed
    row = route.state["audit"][-1]
    assert row["event_action"] == "aauth_resource_call" and row["details"]["pending"] is True
    blob = json.dumps(row["details"])
    assert inter["pending_handle"] not in blob and "WXYZ" not in blob and "/auth" not in blob
    assert row["details"]["at"] == "person_server" and row["details"]["requirement"] == "interaction"

    # The person approves; the SAME label is not a replay — it runs for real.
    route.ps.auth_script = [route.auth_ok]
    route.res.script = [route.require_auth, FakeResource.ok({"after": "consent"})]
    body = route.post(CALL).json()
    assert body["outcome"] == "result" and body["replayed"] is False and json.loads(body["body"]) == {"after": "consent"}


def test_resume_by_handle_polls_the_person_server_then_resends_the_stored_request(route):
    route.bind()
    route.ps.auth_script = [FakePS2.auth_pending(retry_after="0")]
    route.res.script = [route.require_auth]
    handle = route.post(CALL).json()["interaction"]["pending_handle"]
    route.state["audit"].clear()

    # inside the interval: the stored shape again, no poll, no network
    early = route.post({"pending_handle": handle}).json()
    assert early["outcome"] == "interaction_required" and early["interaction"]["pending_handle"] == handle
    assert route.ps.auth_polls == []
    _pollable(route)
    route.ps.auth_poll_script = [route.auth_ok_on_poll]
    route.res.script = [FakeResource.ok({"resumed": True})]
    r = route.post({"pending_handle": handle, "wait": 2, "execution_id": "exec-2"})
    body = r.json()
    assert r.status_code == 200 and body["outcome"] == "result" and json.loads(body["body"]) == {"resumed": True}
    assert body["token_kind"] == "auth" and route.ps.auth_polls[0].headers["prefer"] == "wait=2"
    assert json.loads(route.res.calls[-1].content) == {"query": "ACME"}      # the STORED body, not re-supplied
    assert route.state["rate"][-2][0] == "aauth_rc_resume:echo"
    assert route.state["guard"] == {}                                   # a resume never enters the guard
    row = route.state["audit"][-1]
    assert row["details"]["resumed"] is True and row["details"]["execution_id"] == "exec-2"
    assert ps_client.load_pending(AGENT, handle) is None
    # the handle is gone: a later resume is `expired`, 200, do_not_retry
    body = route.post({"pending_handle": handle}).json()
    assert body["outcome"] == "expired" and body["reason"] == "handle_unknown" and body["do_not_retry"] is True
    assert "WITHOUT a handle" in body["next_step"]


def test_denied_and_approval_pending_are_outcomes_not_errors(route):
    route.bind()
    route.ps.auth_script = [FakePS2.auth_pending(requirement_header="requirement=approval", retry_after="0")]
    route.res.script = [route.require_auth]
    body = route.post(CALL).json()
    assert body["outcome"] == "pending" and body["interaction"]["requirement"] == "approval"
    assert body["interaction"]["url"] is None and "nothing to relay" in body["next_step"]
    handle = body["interaction"]["pending_handle"]

    _pollable(route)
    route.ps.auth_poll_script = [FakePS.problem(403, "denied")]
    r = route.post({"pending_handle": handle})
    body = r.json()
    assert r.status_code == 200 and body["success"] is False and body["outcome"] == "denied"
    assert body["reason"] == "denied" and body["do_not_retry"] is True and "do not retry" in body["next_step"].lower()
    assert route.state["audit"][-1]["details"]["reason"] == "denied"
    assert route.state["activity"][-1]["error"] == "denied"


def test_refusals_are_mapped_http_errors(route):
    # local (pre-egress): AUTHORIZATION rows
    route.live["allowed"] = ()
    r = route.post(CALL)
    assert r.status_code == 403 and r.json()["detail"]["reason"] == "resource_not_allowed"
    route.live["allowed"] = (RES,)
    route.live["ps"] = None
    r = route.post(CALL)
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "person_server_unconfigured"
    route.live["ps"] = PS
    route.live["no_redis"] = True                                       # the person-token leg refuses before any POST
    r = route.post(CALL)
    assert r.status_code == 503 and r.json()["detail"]["reason"] == "state_unavailable"
    route.live["no_redis"] = False
    assert [a["event_action"] for a in route.state["audit"]] == ["aauth_resource_refused"] * 3
    assert {a["event_type"].value for a in route.state["audit"]} == {"authorization"}
    assert route.res.calls == [] and route.ps.posts == []
    assert route.state["guard"] == {}                                   # every refusal released its claim

    # after egress: EXECUTION rows
    route.bind()
    route.res.script = [(402, {"content-type": "application/problem+json"}, json.dumps({"error": "payment_required"}))]
    r = route.post(CALL)
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "payment_required"
    route.res.script = [(429, {"retry-after": "7", "content-type": "application/problem+json"},
                         json.dumps({"error": "slow_down"}))]
    r = route.post(CALL)
    assert r.status_code == 429 and r.json()["detail"]["retry_after"] == 7
    route.res.script = [FakeResource.require("bogus-requirement")]
    r = route.post(CALL)
    assert r.status_code == 502 and r.json()["detail"]["reason"] == "unsupported_requirement:bogus-requirement"
    assert r.json()["detail"]["challenges"] == ["bogus-requirement"]
    assert route.state["audit"][-1]["event_action"] == "aauth_resource_call"
    assert route.state["audit"][-1]["details"]["reason"].startswith("unsupported_requirement")


def test_a_post_timeout_is_504_with_possibly_executed(route, monkeypatch):
    import httpx

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)
    route.bind()
    route.res.script = [slow]
    r = route.post(CALL)
    assert r.status_code == 504 and r.json()["detail"]["reason"] == "timeout"
    assert r.json()["detail"]["possibly_executed"] is True
    assert route.state["audit"][-1]["details"]["possibly_executed"] is True
    assert route.state["guard"] == {}                                   # released: the agent decides, with the warning


def test_an_in_flight_duplicate_is_409(route):
    key = idem.derive_effect_key("exec-1", resource_call.EFFECT_TYPE,
                                 {"resource": RES, "method": "POST", "path": PATH}, "step-1")
    route.state["guard"][key] = None                                    # someone holds the claim
    r = route.post(CALL)
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "effect_in_progress"
    assert route.res.calls == [] and route.state["audit"] == []


def test_without_an_execution_id_the_call_runs_unguarded_or_is_refused_on_a_pull_agent(route, monkeypatch):
    _full_chain(route)
    monkeypatch.setattr(idem, "effect_dedup_required", lambda agent: False)
    body = route.post({k: v for k, v in CALL.items() if k != "execution_id"}).json()
    assert body["outcome"] == "result" and route.state["guard"] == {}
    assert "execution_id" not in route.state["audit"][-1]["details"]
    # #2392: a pull-mode agent is refused, not sent unguarded — 422, nothing dialled.
    monkeypatch.setattr(idem, "effect_dedup_required", lambda agent: True)

    # #3254 re-pin: the refusal's alarm reports through the platform-alert
    # seam, so that is where it is stubbed (the old helper patch was dead).
    from services import platform_alerts
    monkeypatch.setattr(platform_alerts, "observe", lambda *a, **k: platform_alerts.OBSERVED_CREATED)
    sends = len(route.res.calls)
    r = route.post({k: v for k, v in CALL.items() if k != "execution_id"})
    assert r.status_code == 422 and r.json()["detail"]["reason"] == "effect_unguarded"
    assert len(route.res.calls) == sends


def test_get_sends_no_body_and_the_justification_is_logged_not_audited(route, caplog):
    import logging

    route.bind()                                                        # auth-token mode starts at the person token
    route.res.script = [FakeResource.ok({"me": "agent"})]
    with caplog.at_level(logging.INFO, logger="services.aauth.resource_client"):
        body = route.post({**CALL, "method": "GET", "path": "/v1/whoami", "body": None}).json()
    assert body["outcome"] == "result" and body["token_kind"] == "person" and body["challenges"] == []
    req = route.res.calls[-1]
    assert req.method == "GET" and req.content == b"" and "content-type" not in req.headers
    assert any("sanctions check" in rec.getMessage() for rec in caplog.records)
    assert "sanctions" not in json.dumps(route.state["audit"][-1]["details"])


# --------------------------------------------------------------------------- #
# Service seam: snapshots
# --------------------------------------------------------------------------- #
def test_snapshot_round_trip_is_lossless_and_tolerant():
    from services.aauth import resource_meta

    res = rc.ResourceResult(status=200, body="{}", truncated=False, token_kind="auth", challenges=["auth-token"],
                            resource=RES, method="POST", path=PATH,
                            budget=resource_meta.Budget(cost=2, balance=38, unit="credits"), content_type="application/json")
    back = resource_call.from_snapshot(resource_call.snapshot_of(res), resource="", method="", path="")
    assert back == res
    den = rc.Denied("abandoned", "gone away", ["person-token"], RES)
    assert resource_call.from_snapshot(resource_call.snapshot_of(den), resource="", method="", path="") == den
    # an older / empty snapshot never raises
    out = resource_call.from_snapshot(None, resource=RES, method="GET", path="/v1/x")
    assert out.outcome == "result" and out.resource == RES and out.status == 0


# --------------------------------------------------------------------------- #
# Composition
# --------------------------------------------------------------------------- #
def test_router_is_registered_in_main_after_the_person_token_router():
    tree = ast.parse(open(os.path.join(_BACKEND, "main.py"), encoding="utf-8").read())
    imported = any(isinstance(n, ast.ImportFrom) and n.module == "routers.aauth_resource"
                   and any(a.name == "router" and a.asname == "aauth_resource_router" for a in n.names)
                   for n in ast.walk(tree))
    included = [n.args[0].id for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "include_router" and n.args and isinstance(n.args[0], ast.Name)
                and n.args[0].id in ("aauth_router", "aauth_person_router", "aauth_resource_router")]
    assert imported and included == ["aauth_router", "aauth_person_router", "aauth_resource_router"]
    assert open(ra.__file__, encoding="utf-8").readline().strip() == "# mcp: tools/aauth.ts"


def test_the_route_resolves_beside_the_agent_catch_all():
    """Cheap, and exactly the kind of thing a later refactor breaks silently (#736 shape)."""
    from fastapi import FastAPI
    from fastapi.routing import APIRoute

    app = FastAPI()
    app.include_router(ra.router)

    @app.get("/api/agents/{name}")
    async def catch_all(name: str):
        return {"name": name}

    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/api/agents/{agent_name}/aauth/resource-call" in paths


def test_the_gate_helpers_are_the_person_token_routers():
    """T7: one own-key gate for both AAuth routes — the resource route carries no copy."""
    import inspect

    src = inspect.getsource(ra)
    assert "aauth_person.require_live" in src and "aauth_person.assert_own_key" in src
    assert "def require_live" not in src and "def assert_own_key" not in src
    assert "aauth_agent_only" not in src.split('"""', 2)[2]             # the reason string lives in one place
