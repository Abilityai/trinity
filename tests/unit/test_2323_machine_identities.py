"""Machine identities for admin/ops APIs (#2323).

The issue as filed asked for a service credential that survives enforced 2FA,
on the premise that none existed. One did: a `user`-scoped MCP key owned by an
admin already reaches every admin gate, already bypasses the MFA gate (which
lives on the two login routes and never sees key validation), is already
revocable and already rotates by minting a second key.

What it is NOT is bounded, attributable, or expiring — so the only way to keep a
monitoring dashboard alive under enforced 2FA was to hand it a permanent,
invisible, unlimited admin credential. This suite pins the three halves of the
fix:

* the admin gate as an ALLOWLIST (see also test_293), so a scope nobody has
  invented yet cannot inherit an owner's admin role;
* audit attribution derived from the PRESENTED credential rather than a
  client-supplied header;
* the `ops` scope: read-only, route-fenced, authorized by what it is.

No MagicMock, deliberately — a bare mock auto-creates truthy attributes, so an
`ops` principal built from one would carry a truthy `agent_name` and pass for
entirely the wrong reason.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


def _deps():
    try:
        import dependencies
    except ImportError:  # pragma: no cover - backend venv required
        pytest.skip("backend venv required")
    return dependencies


def _request(method: str, path: str):
    """The two fields the fence reads. It touches no DB and no Redis — which is
    exactly why, unlike the ephemeral fence, it has nothing that can fail open."""
    return SimpleNamespace(method=method, url=SimpleNamespace(path=path))


# --------------------------------------------------------------------------- #
# The method belt
# --------------------------------------------------------------------------- #

def test_every_allowlisted_route_is_a_GET():
    """The belt that stops a future `POST /api/ops/<anything>` from inheriting
    read access. `/api/ops` is where `emergency-stop` and `fleet/stop` live, so
    a write slipping into this list is a privilege bug, not a convenience.

    Asserted by IMPORTING the constant, never by grepping the source: a grep
    guard passes on the prose explaining it and misses a second declaration
    syntax entirely."""
    d = _deps()
    assert d._OPS_ALLOWED_ROUTES, "fence must not be empty — an empty fence denies everything"
    for method, pattern in d._OPS_ALLOWED_ROUTES:
        assert method == "GET", f"non-GET route in the ops fence: {method} {pattern.pattern}"


def test_the_fence_is_anchored_not_a_prefix_match():
    """A prefix match over `/api/ops/` would admit every future route in that
    router. Each entry must be fully anchored so a new route is INACCESSIBLE
    until someone adds it deliberately — the failure direction we want."""
    d = _deps()
    for _, pattern in d._OPS_ALLOWED_ROUTES:
        assert pattern.pattern.startswith("^") and pattern.pattern.endswith("$")


# --------------------------------------------------------------------------- #
# Route parity — resolved against the live route table
# --------------------------------------------------------------------------- #

def _app_routes():
    """Resolve against the LIVE route objects, never AST or grep.

    Built from the router modules rather than `main.app`: importing the app
    needs the full runtime stack, which this unit environment does not have —
    and a guard that skips is a guard that does not exist. The router modules
    import cleanly and carry the same route objects.
    """
    out = []
    for module_name in ("routers.ops", "routers.monitoring", "routers.telemetry"):
        try:
            mod = __import__(module_name, fromlist=["router"])
        except ImportError:  # pragma: no cover - backend venv required
            pytest.skip("backend venv required")
        for r in mod.router.routes:
            # `route.path` ALREADY carries the router prefix — FastAPI prepends
            # it at add_api_route time. Concatenating `router.prefix` again
            # doubles it, which this guard caught on its own first run.
            path = getattr(r, "path", None)
            if not path:
                continue
            for m in (getattr(r, "methods", None) or set()):
                out.append((m, path))
    assert out, "no routes resolved — the guard would pass vacuously"
    return out


def _fence_allows(d, method, path):
    return any(m == method and p.fullmatch(path) for m, p in d._OPS_ALLOWED_ROUTES)


def test_every_allowlisted_ops_route_resolves_to_a_real_route():
    """A stale entry fails CLOSED (legit ops traffic 403s), which is the safe
    direction — but nobody would learn until an integration broke. No fence in
    this codebase had a route-existence guard before #2323.

    Scoped to the routers this guard can import; the app-level `/api/version`
    is covered by the declaration-family test above."""
    d = _deps()
    live = {re.sub(r"\{[^}]+\}", "x", p) for m, p in _app_routes() if m == "GET"}
    named = [pat.pattern for _, pat in d._OPS_ALLOWED_ROUTES
             if any(pat.pattern.startswith("^" + pfx)
                    for pfx in ("/api/ops/", "/api/monitoring/", "/api/telemetry/"))]
    assert named, "fence names no route from the importable routers"
    for pattern_src in named:
        concrete = pattern_src.strip("^$")
        assert concrete in live, f"ops fence names a route that does not exist: {concrete}"


def test_the_fence_spans_both_route_declaration_families():
    """Routes here are declared two ways — `@router.<verb>` under a prefix, and
    `@app.get` at app level (`/api/version`). A guard that sees only one family
    reports a clean pass over the half it cannot see, so assert both are
    represented rather than counting a total."""
    d = _deps()
    patterns = [p.pattern for _, p in d._OPS_ALLOWED_ROUTES]
    assert any("/api/version" in p for p in patterns), "app-level family missing"
    assert any("/api/ops/" in p for p in patterns), "router-prefixed family missing"


def test_every_ops_route_not_allowlisted_is_denied():
    """The mutation-proof half. A forward-only guard proves the listed routes
    are listed; only the inverse catches a future destructive route — which is
    the thing that actually matters."""
    d = _deps()
    from fastapi import HTTPException

    checked = 0
    for method, path in _app_routes():
        if not (path.startswith("/api/ops") or path.startswith("/api/monitoring")):
            continue
        concrete = re.sub(r"\{[^}]+\}", "x", path)
        if _fence_allows(d, method, concrete):
            continue  # deliberately allowlisted — covered by the admit test
        checked += 1
        with pytest.raises(HTTPException) as exc:
            d._enforce_ops_key_fence(_request(method, concrete))
        assert exc.value.status_code == 403, f"{method} {path} is reachable by an ops key"
    assert checked, "no non-allowlisted ops route was exercised — guard is vacuous"

    # Named explicitly as well, so the three routes that halt a fleet can never
    # drop out of the sweep silently (e.g. if a router were renamed).
    live = {p for _, p in _app_routes()}
    for write in ("/api/ops/fleet/restart", "/api/ops/fleet/stop", "/api/ops/emergency-stop"):
        assert write in live, f"{write} vanished from the route table — re-check this guard"
        with pytest.raises(HTTPException):
            d._enforce_ops_key_fence(_request("POST", write))


# --------------------------------------------------------------------------- #
# Adversarial denial roster
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "method, path",
    [
        ("POST", "/api/ops/emergency-stop"),        # halt the fleet
        ("POST", "/api/ops/fleet/stop"),
        ("POST", "/api/ops/fleet/restart"),
        ("POST", "/api/ops/schedules/pause"),
        ("GET", "/api/users"),                       # other humans
        ("POST", "/api/mcp/keys"),                   # mint more credentials
        ("DELETE", "/api/mcp/keys/abc"),             # a fleet-wide auth wipe
        ("GET", "/api/audit-log"),                   # the whole audit trail
        ("PUT", "/api/settings/max-parallel-tasks-ceiling"),
        ("POST", "/api/agents"),                     # create an agent
        ("POST", "/api/agents/atlas/chat"),          # spend money as the owner
        ("GET", "/api/agents/atlas/files/download"),  # read an agent's files
        ("GET", "/api/settings/retention"),
        ("POST", "/api/ops/fleet/status"),           # right path, wrong method
    ],
)
def test_ops_fence_denies(method, path):
    from fastapi import HTTPException

    d = _deps()
    with pytest.raises(HTTPException) as exc:
        d._enforce_ops_key_fence(_request(method, path))
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "/api/version",
        "/api/ops/fleet/status",
        "/api/ops/fleet/health",
        "/api/ops/costs",
        "/api/ops/auth-report",
        "/api/telemetry/host",
        "/api/telemetry/containers",
        "/api/agents",
        "/api/agents/execution-stats",
        "/api/agents/slots",
        "/api/agents/atlas/executions",
        "/api/agents/atlas/executions/exec-1/stream",
        "/api/executions",
        "/api/subscriptions",
        "/api/subscriptions/sub-1/usage",
    ],
)
def test_ops_fence_admits_the_measured_consumer_read_set(path):
    """Guard against a fence satisfied by denying everyone. This list is the
    Trinity Control dashboard's ACTUAL authenticated read set, taken from its
    source — not from the issue's wording, which named only `/api/ops/*` and
    would have shipped a credential that could not run the one consumer it
    exists for."""
    d = _deps()
    d._enforce_ops_key_fence(_request("GET", path))


# --------------------------------------------------------------------------- #
# Mint gate
# --------------------------------------------------------------------------- #

def test_ops_is_mintable_at_the_db_layer():
    try:
        from db.mcp_keys import McpKeyOperations
    except ImportError:  # pragma: no cover
        pytest.skip("backend venv required")
    assert "ops" in McpKeyOperations._USER_CREATABLE_SCOPES


# --------------------------------------------------------------------------- #
# PR-A — attribution comes from the credential, not from a header
# --------------------------------------------------------------------------- #

def test_user_model_carries_the_key_identity():
    """`routers/a2a.py` read `mcp_key_id` off this model in two places via
    `getattr`, and both silently did nothing because the field did not exist —
    one of them the A2A idempotency SCOPE, where falling back to `username`
    collapsed two agent-scoped keys of one owner into a shared namespace."""
    try:
        from models import User
    except ImportError:  # pragma: no cover
        pytest.skip("backend venv required")
    u = User(id=1, username="admin", role="admin")
    assert u.mcp_key_id is None and u.mcp_key_name is None
    keyed = User(id=1, username="admin", role="admin", mcp_key_id="k1", mcp_key_name="dash")
    assert keyed.mcp_key_id == "k1"


def test_audit_derives_the_credential_from_the_principal():
    try:
        from services.platform_audit_service import PlatformAuditService
    except ImportError:  # pragma: no cover
        pytest.skip("backend venv required")
    import inspect

    src = inspect.getsource(PlatformAuditService.log)
    assert 'getattr(actor_user, "mcp_key_id", None)' in src
    assert 'getattr(actor_user, "mcp_key_name", None)' in src


def test_derivation_tolerates_an_actor_without_the_fields():
    """Some callers pass a `SimpleNamespace` actor (`routers/voice.py`), and a
    plain attribute access would raise into `log()`'s bare `except`, which
    returns None — silently DROPPING the audit row. `None` is also the
    unprivileged default, which is the rule for a getattr across principal
    types."""
    actor = SimpleNamespace(id=1, email="a@b.c")
    assert getattr(actor, "mcp_key_id", None) is None


def test_dispatch_admission_no_longer_trusts_the_client_header():
    """`X-MCP-Key-Id` is `Header(None)` on six routes, validated nowhere, and is
    persisted into the backlog replay blob — so honouring it let any
    authenticated caller forge the credential named in the two highest-volume
    audit events, with the forgery surfacing minutes later on queue drain."""
    import inspect

    try:
        from services import dispatch_admission_service as das
    except ImportError:  # pragma: no cover
        pytest.skip("backend venv required")
    src = inspect.getsource(das)
    assert "mcp_key_id=x_mcp_key_id" not in src
    assert 'mcp_key_id=getattr(current_user, "mcp_key_id", None)' in src


def test_audit_log_can_be_queried_by_credential():
    """`actor_type` deliberately stays `"user"` — the owner is the accountable
    party and is the only branch yielding an email — so without these filters
    the credential dimension would be recorded but unqueryable, and 'what did
    that leaked key touch?' would still be unanswerable."""
    try:
        from db.audit import PlatformAuditOperations
    except ImportError:  # pragma: no cover
        pytest.skip("backend venv required")
    import inspect

    sig = inspect.signature(PlatformAuditOperations.get_audit_entries)
    assert "mcp_key_id" in sig.parameters and "mcp_scope" in sig.parameters


# --------------------------------------------------------------------------- #
# Review findings (#2389) — the fence and the gate must agree
# --------------------------------------------------------------------------- #
#
# The admit-set test above exercises `_enforce_ops_key_fence` and nothing else,
# so it passed while `GET /api/subscriptions/{id}/usage` was admitted by the
# fence and then refused by its own `assert_admin` — the subscription-pressure
# read the fence was measured for could not work. A test that proves one half of
# a two-gate path is a test that gives false assurance.
#
# This guards the CLASS: every allowlisted route whose handler calls
# `assert_admin` must opt `ops` in. Resolved from the live handler object, not
# grepped from a file, so it follows the code if a route moves.

def _handler_sources_for_allowlisted_routes():
    """(path, source) for each allowlisted route that resolves to a handler."""
    import inspect

    out = []
    for module_name in ("routers.ops", "routers.monitoring", "routers.telemetry",
                        "routers.subscriptions", "routers.agents", "routers.executions",
                        "routers.chat", "routers.schedules"):
        try:
            mod = __import__(module_name, fromlist=["router"])
        except ImportError:  # pragma: no cover - backend venv required
            pytest.skip("backend venv required")
        for r in getattr(mod, "router", None).routes if getattr(mod, "router", None) else []:
            path = getattr(r, "path", None)
            endpoint = getattr(r, "endpoint", None)
            if not path or endpoint is None:
                continue
            if "GET" not in (getattr(r, "methods", None) or set()):
                continue
            concrete = re.sub(r"\{[^}]+\}", "x", path)
            d = _deps()
            if not any(m == "GET" and pat.fullmatch(concrete)
                       for m, pat in d._OPS_ALLOWED_ROUTES):
                continue
            try:
                out.append((path, inspect.getsource(endpoint)))
            except (OSError, TypeError):  # pragma: no cover
                continue
    return out


def test_every_admin_gated_allowlisted_route_opts_ops_in():
    """A route the fence admits but the gate refuses is dead to an ops key.

    `ADMIN_GATE_SCOPES` deliberately excludes `ops`, so any allowlisted handler
    calling `assert_admin` MUST pass `allow_scopes={"ops"}` — that opt-in is the
    grant. Bare `assert_admin` on an allowlisted route is the #2389 finding."""
    pairs = _handler_sources_for_allowlisted_routes()
    assert pairs, "resolved no allowlisted handlers — guard would be vacuous"
    offenders = []
    for path, src in pairs:
        for m in re.finditer(r"assert_admin\(([^)]*)\)", src, re.S):
            if '"ops"' not in m.group(1) and "'ops'" not in m.group(1):
                offenders.append(path)
    assert not offenders, (
        "allowlisted by the ops fence but refused by their own admin gate: "
        f"{sorted(set(offenders))}"
    )


# --------------------------------------------------------------------------- #
# Review findings (#2389) — the event stream is not in the allowlist
# --------------------------------------------------------------------------- #

def test_bounded_scopes_cannot_open_the_fleet_event_stream():
    """`/ws/events` authenticates the key itself and never runs
    `get_current_user`, so NONE of that function's fences reach it. Without an
    explicit gate the ops fence's own docstring claim is false and a bounded
    credential reads fleet-wide activity through an unlisted surface."""
    d = _deps()
    for scope in ("ops", "connector", "portal_delegate", "scope_from_2027"):
        assert not d.scope_may_open_event_stream(scope), scope


@pytest.mark.parametrize("scope", [None, "", "user", "agent", "system"])
def test_the_event_stream_still_admits_the_scopes_that_drive_it(scope):
    """Guard against a gate satisfied by denying everyone. `None`/`""` coerce to
    `user`, matching `validate_mcp_api_key` and `get_current_user`."""
    d = _deps()
    assert d.scope_may_open_event_stream(scope)


# --------------------------------------------------------------------------- #
# Review findings (#2389) — assert the written row, not a constant
# --------------------------------------------------------------------------- #

def test_a_minted_ops_key_carries_no_agent_name(tmp_path, monkeypatch):
    """The earlier test asserted membership in `_AGENTLESS_SCOPES`, which is a
    tuple asserting itself — it documented a protection rather than proving one,
    and the guard it stood for read a field `McpApiKeyCreate` cannot carry (so it
    could never fire). This drives the real creator and reads the row back.

    Three sweeps filter on `scope IN ('agent','connector')`, so an ops key
    holding an agent name would be invisible to all of them and outlive its
    agent forever."""
    db_file = tmp_path / "trinity-ops-key.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    try:
        import db.connection as conn_mod
        monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))
        from db.engine import get_engine
        from db.tables import metadata as m, users, mcp_api_keys
        from sqlalchemy import insert, select
    except ImportError:  # pragma: no cover - backend venv required
        pytest.skip("backend venv required")

    m.create_all(get_engine(), tables=[users, mcp_api_keys])
    with get_engine().begin() as conn:
        conn.execute(insert(users).values(
            id=1, username="admin", role="admin", email="admin@example.com",
            created_at="t", updated_at="t"))

    from database import db as core_db, McpApiKeyCreate

    created = core_db.create_mcp_api_key(
        "admin", McpApiKeyCreate(name="obs", description=None, scope="ops")
    )
    assert created is not None, "ops must be mintable at the db layer"
    assert created.scope == "ops"

    with get_engine().connect() as conn:
        row = conn.execute(
            select(mcp_api_keys.c.scope, mcp_api_keys.c.agent_name)
            .where(mcp_api_keys.c.id == created.id)
        ).mappings().first()
    assert row["scope"] == "ops"
    assert row["agent_name"] is None, "an ops key must never bind an agent"


def test_durable_execution_provenance_is_not_taken_from_a_client_header():
    """#2389: the audit path stopped trusting `X-MCP-Key-Id`, but
    `schedule_executions.source_mcp_key_id` still recorded it — so one request
    produced two provenance records that disagree, and the forged one is the one
    that survives longest."""
    import inspect

    try:
        from services import chat_execution_service as ces
    except ImportError:  # pragma: no cover
        pytest.skip("backend venv required")
    src = inspect.getsource(ces)
    assert "source_mcp_key_id=x_mcp_key_id" not in src
    assert "source_mcp_key_name=x_mcp_key_name" not in src
    assert 'source_mcp_key_id=getattr(current_user, "mcp_key_id", None)' in src


def test_no_route_accepts_a_credential_name_as_a_request_header():
    """#2389 — the strongest form of the rule: a value nothing declares cannot
    be forged, threaded, or quietly picked up by a new consumer.

    Grepping for the *use* was not enough. `X-MCP-Key-Id`/`X-MCP-Key-Name` were
    removed from the audit path in #2323 while five routers still declared them
    as `Header(None)` and wrote them into durable provenance columns
    (`schedule_executions`, `agent_loops`, `agent_reminders`, the fan-out rows)
    and into the backlog replay blob. This asserts the declaration is gone from
    every router, so the class cannot come back one endpoint at a time."""
    routers_dir = Path(__file__).resolve().parents[2] / "src" / "backend" / "routers"
    if not routers_dir.is_dir():  # pragma: no cover
        pytest.skip("backend tree required")
    offenders = []
    for f in sorted(routers_dir.rglob("*.py")):
        text = f.read_text()
        for m in re.finditer(r"x_mcp_key_(?:id|name)\s*:\s*[^=\n]+=\s*Header\(", text):
            offenders.append(f"{f.name}:{text[:m.start()].count(chr(10)) + 1}")
    assert not offenders, (
        "routers still declaring the forgeable credential header: " + ", ".join(offenders)
    )


def test_the_backlog_replay_blob_carries_no_credential_identity():
    """#2389 — `backlog_metadata` is the longest-lived copy of a request and is
    the one surface canary G-04 scans and #1449 scrubs. It persisted
    `x_mcp_key_id`/`x_mcp_key_name` that `_spawn_drain` never read back: a
    forgeable value stored, never used, and surviving the request that carried
    it. Asserted against the metadata the REAL producer writes."""
    try:
        from services import backlog_service as bs
    except ImportError:  # pragma: no cover
        pytest.skip("backend venv required")

    import inspect
    src = inspect.getsource(bs.BacklogService.enqueue)
    assert '"x_mcp_key_id"' not in src and '"x_mcp_key_name"' not in src
    sig = inspect.signature(bs.BacklogService.enqueue)
    assert "x_mcp_key_id" not in sig.parameters
    assert "x_mcp_key_name" not in sig.parameters
