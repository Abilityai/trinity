"""The `ops_write` never-grantable sweep over the LIVE app (#3180).

Imports the real `main.app` in a SUBPROCESS (the `_route_census.runtime_routes`
pattern: no module state leaks into the unit island), mints REAL keys in a
throwaway SQLite, and drives every registered OSS route through the real
credential resolver with an `ops_write` key. The parent test
(`test_3180_ops_write_route_census.py`) asserts on what comes back.

Each route is classified by what its credential dependency DOES, never by which
census table lists it (A2A is `OWN_AUTH` in the census and still resolves keys):

* ``resolver``      - its dependency tree reaches `get_current_user`, or a door
                      that calls `resolve_platform_user_unfloored` and lets a
                      refusal through: `get_portal_principal`, and
                      `get_room_principal`, which catches the first refusal and
                      then resolves again through `get_portal_principal`;
* ``or_anonymous``  - `get_user_or_anonymous` (A2A): a fence 403 is re-raised;
* ``swallowing``    - `get_optional_user`, which turns any refusal into "no
                      credential": the key must buy exactly what an
                      unrecognised key buys;
* ``none``          - no Trinity-credential dependency at all: listed by name in
                      the parent test with the credential it uses instead.

Modes, each run in this one process so `import main` is paid once:
``normal``, ``hole_path`` (the path list admits everything), ``stray_mark``
(every handler marked), ``demoted_creator`` / ``demoted_operator`` (owner role),
``suspended``, the named ``families`` with and without the fence call, the
raw-validator doors, ``/ws/events``, and the read-only `ops` key over every
write. OSS routes only: the enterprise submodule is not part of this app.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
REPO = BACKEND.parents[1]

SCRIPT = r'''
import json, os, re, secrets, sys, tempfile, uuid
from pathlib import Path

backend = sys.argv[1]
td = tempfile.mkdtemp(prefix="trinity-ops-write-sweep-")
for k, v in {
    "REDIS_URL": "redis://test:test@redis:6379", "REDIS_PASSWORD": "test",
    "REDIS_BACKEND_PASSWORD": "test", "AGENT_AUTH_SECRET": "0" * 64,
    "SECRET_KEY": "x" * 32, "INTERNAL_API_SECRET": "y" * 32,
}.items():
    os.environ.setdefault(k, v)
os.environ["TRINITY_DB_PATH"] = str(Path(td) / "sweep.db")
os.environ["LOG_ARCHIVE_PATH"] = str(Path(td) / "logs")
os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, backend)

import main
import dependencies as D
from database import db
from db_models import McpApiKeyCreate, UserCreate
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.convertors import FloatConvertor, IntegerConvertor, UUIDConvertor
from starlette.websockets import WebSocketDisconnect

OWNER = "sweep3180-owner"
AGENT = "sweep3180-agent"
db.create_user(UserCreate(username=OWNER, role="admin", email="sweep3180-owner@example.com"))
db.register_agent_owner(AGENT, OWNER)


def mint(scope, name):
    k = db.create_mcp_api_key(OWNER, McpApiKeyCreate(name=name, description=None, scope=scope))
    assert k is not None and k.scope == scope, (scope, k)
    return k


OW = mint("ops_write", "sweep ops_write")
OPS = mint("ops", "sweep ops")
USER = mint("user", "sweep admin user key")  # the unbounded key `ops_write` exists to replace
BOGUS = "trinity_mcp_" + secrets.token_hex(20)
client = TestClient(main.app, raise_server_exceptions=False)

# A live `authorized` canvas share on the owner's agent: the one
# `get_optional_user` door, probed with a token that MATCHES a row, so an
# authenticated viewer and an anonymous one get different answers.
CANVAS_ID = "cv-sweep3180"
db.upsert_agent_canvas(AGENT, CANVAS_ID, blocks=[{"type": "markdown", "markdown": "ops_write sweep"}],
                       title="sweep3180")
SHARE = db.create_canvas_share(AGENT, CANVAS_ID, scope="authorized", created_by=OWNER)


def canvas_view(token):
    got = call("GET", f"/api/public/canvas/{SHARE['token']}", token)
    detail = got.get("detail")
    got["share_status"] = detail.get("status") if isinstance(detail, dict) else None
    if got["status"] == 200:
        got["share_status"] = json.loads(got["body"]).get("status")
    return got


def flat_routes():
    out = []
    for entry in main.app.routes:
        if type(entry).__name__ == "_IncludedRouter":
            out.extend(r for r in entry.original_router.routes if isinstance(r, APIRoute))
        elif isinstance(entry, APIRoute):
            out.append(entry)
    return out


def calls(dependant):
    out = []
    for d in dependant.dependencies:
        c = d.call
        out.append((getattr(c, "__module__", "?"), getattr(c, "__name__", "?")))
        out += calls(d)
    return out


RESOLVER = {
    ("dependencies", "get_current_user"),
    ("client_portal.portal_auth", "get_portal_principal"),
    ("shared_sessions.router", "get_room_principal"),
}
OR_ANON = {("dependencies", "get_user_or_anonymous")}
SWALLOW = {("dependencies", "get_optional_user")}


def classify(route):
    cs = set(calls(route.dependant))
    if cs & RESOLVER:
        return "resolver"
    if cs & OR_ANON:
        return "or_anonymous"
    if cs & SWALLOW:
        return "swallowing"
    return "none"


def concrete(route, agent):
    path = route.path_format
    for name, conv in route.param_convertors.items():
        if isinstance(conv, IntegerConvertor):
            val = "1"
        elif isinstance(conv, FloatConvertor):
            val = "1.0"
        elif isinstance(conv, UUIDConvertor):
            val = str(uuid.UUID(int=1))
        elif name in ("agent_name", "name"):
            val = agent
        else:
            val = "x"
        path = path.replace("{" + name + "}", val)
    return path


def key_of(route):
    mod = route.endpoint.__module__
    rel = mod.replace(".", "/") + ".py"
    if not (Path(backend) / rel).exists():
        rel = mod.replace(".", "/") + "/__init__.py"
    return f"{rel}::{route.endpoint.__qualname__}"


def call(method, path, token, **kw):
    headers = dict(kw.pop("headers", {}) or {})
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    r = client.request(method, path, headers=headers, **kw)
    try:
        body = r.json()
    except Exception:
        body = None
    detail = body.get("detail") if isinstance(body, dict) else None
    code = detail.get("code") if isinstance(detail, dict) else None
    return {"status": r.status_code, "code": code,
            "detail": detail if isinstance(detail, (str, dict)) else None,
            "body": r.text[:2000]}


ROUTES = [r for r in flat_routes() if not r.endpoint.__module__.startswith("enterprise")]
TABLE = []
for r in ROUTES:
    for m in sorted(r.methods or []):
        TABLE.append({"key": key_of(r), "method": m, "template": r.path, "cls": classify(r),
                      "path": concrete(r, "x"), "route": r})

out = {"routes": [{k: v for k, v in row.items() if k != "route"} for row in TABLE],
       "owner": OWNER, "agent": AGENT, "ops_write_key_id": OW.id}


def sweep(token, rows, compare_bogus=False):
    res = {}
    for row in rows:
        k = f"{row['method']} {row['template']}"
        got = call(row["method"], row["path"], token)
        if compare_bogus:
            got["bogus"] = call(row["method"], row["path"], BOGUS)
        res[k] = got
    return res


fenced = [row for row in TABLE if row["cls"] in ("resolver", "or_anonymous")]
swallowing = [row for row in TABLE if row["cls"] == "swallowing"]

out["normal"] = sweep(OW.api_key, fenced)
out["swallowing"] = sweep(OW.api_key, swallowing, compare_bogus=True)
out["canvas"] = {
    "ops_write": canvas_view(OW.api_key),
    "anonymous": canvas_view(None),
    "bogus_key": canvas_view(BOGUS),
    "owner_user_key": canvas_view(USER.api_key),
    "owner_session": canvas_view(D.create_access_token({"sub": OWNER})),
}

# Layer masking, direction 1: the path list holed (admits every method and path).
real_list = D._OPS_WRITE_ALLOWED_ROUTES
D._OPS_WRITE_ALLOWED_ROUTES = tuple(
    (m, re.compile(r".*")) for m in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"))
out["hole_path"] = sweep(OW.api_key, fenced)
D._OPS_WRITE_ALLOWED_ROUTES = real_list

# Direction 2: a stray mark on EVERY handler; the path list must still refuse.
originals = {}
for row in TABLE:
    ep = row["route"].endpoint
    if id(ep) not in originals:
        originals[id(ep)] = (ep, getattr(ep, D.OPS_WRITE_ROUTE_ATTR, None))
        setattr(ep, D.OPS_WRITE_ROUTE_ATTR, "stray mark (#3180 sweep)")
out["stray_mark"] = sweep(OW.api_key, fenced)
for ep, prior in originals.values():
    if prior is None:
        delattr(ep, D.OPS_WRITE_ROUTE_ATTR)
    else:
        setattr(ep, D.OPS_WRITE_ROUTE_ATTR, prior)
out["marked_after_restore"] = sorted(
    {row["key"] for row in TABLE if getattr(row["route"].endpoint, D.OPS_WRITE_ROUTE_ATTR, None)})

# Dies with its owner, on EVERY route.
for role in ("creator", "operator"):
    db.update_user_role(OWNER, role)
    out[f"demoted_{role}"] = sweep(OW.api_key, fenced)
db.update_user_role(OWNER, "admin")
from db.engine import get_engine
from sqlalchemy import text
with get_engine().begin() as conn:
    conn.execute(text("UPDATE users SET suspended_at = '2026-10-01T00:00:00Z' WHERE username = :u"), {"u": OWNER})
out["suspended"] = sweep(OW.api_key, fenced)
with get_engine().begin() as conn:
    conn.execute(text("UPDATE users SET suspended_at = NULL WHERE username = :u"), {"u": OWNER})

# The read-only `ops` key over every write the fence would otherwise see.
out["ops_key_writes"] = sweep(OPS.api_key, [row for row in fenced if row["method"] not in ("GET", "HEAD")])

# Named families, with a REAL agent and real key ids, through their routes.
FAMILIES = json.loads(sys.argv[2])
by_key = {}
for row in TABLE:
    by_key.setdefault(row["key"], []).append(row)


def fill(template):
    return (template.replace("{agent_name}", AGENT).replace("{name}", AGENT)
            .replace("{username}", OWNER).replace("{capability}", "schedules.manage")
            .replace("{email:path}", "someone@example.com").replace("{subscription_id}", "sub-1")
            .replace("{key_id}", "@KEY@"))


def run_families(token, tag, only=None):
    sacrifice = {"revoke": mint("user", f"sweep sacrificial revoke {tag}"),
                 "delete": mint("user", f"sweep sacrificial delete {tag}")}
    res = {"_sacrifice_active": {}}
    for fam in FAMILIES:
        if only is not None and not only(fam):
            continue
        rows = by_key.get(fam["key"])
        if not rows:
            res[fam["key"]] = {"missing": True}
            continue
        row = next(r for r in rows if r["method"] == fam["method"])
        path = fill(row["template"])
        if "@KEY@" in path:
            path = path.replace("@KEY@", sacrifice[fam.get("sacrifice", "revoke")].id)
        if fam.get("query"):
            path += "?" + fam["query"]
        got = call(fam["method"], path, token, json=fam.get("json"))
        for k in sacrifice.values():  # the only per-run difference between two runs
            got["body"] = got["body"].replace(k.id, "@KEY@")
        res[fam["key"] + " " + fam["method"]] = got
    res["_sacrifice_active"] = {
        name: bool(db.validate_mcp_api_key(k.api_key, track_usage=False)) for name, k in sacrifice.items()}
    return res


out["families"] = run_families(OW.api_key, "fenced")
real_fence = D._enforce_ops_write_key_fence
D._enforce_ops_write_key_fence = lambda request, owner: None
out["families_no_fence"] = run_families(OW.api_key, "no-fence")
out["canvas_no_fence"] = canvas_view(OW.api_key)
D._enforce_ops_write_key_fence = real_fence
# The unbounded key `ops_write` replaces, over the fence-only families ONLY: it
# passes the admin gates, so letting it run a gated family first (registering a
# subscription, say) would change what a later family finds.
out["families_user_key"] = run_families(USER.api_key, "user-key", only=lambda fam: fam.get("own_gate") is None)

# Doors that validate a Trinity key themselves and never run the resolver.
# The pull seams refuse a non-pilot agent before they read the key at all, so
# the seeded agent is made a pilot first; each door is then also driven with
# the agent's OWN key, which must get past the key check, so a 403 for
# `ops_write` is the key check refusing and not something in front of it.
os.environ["PULL_MODE_PILOT_AGENTS"] = AGENT
AGENT_KEY = db.create_agent_mcp_api_key(AGENT, OWNER)
eid = "sweep3180-exec"


def door_calls(token):
    return {
        "heartbeat": call("POST", f"/api/agents/{AGENT}/heartbeat", token, json={}),
        "result_callback": call("POST", f"/api/agents/{AGENT}/executions/{eid}/result", token,
                                json={"status": "success", "response": "x"}),
        "pull_next_task": call("GET", f"/api/internal/next-task?agent_name={AGENT}&worker_id=w-3180", token),
        "pull_task_result": call("POST", f"/api/internal/tasks/{eid}/result", token,
                                 json={"claim_token": "c-3180", "status": "success", "content": "x"}),
    }


out["doors"] = {"ops_write": door_calls(OW.api_key), "agent_key": door_calls(AGENT_KEY.api_key),
                "mcp_validate": call("POST", "/api/mcp/validate", OW.api_key)}


def ws_close(path):
    try:
        with client.websocket_connect(path) as ws:
            ws.receive_text()
        return {"closed": None}
    except WebSocketDisconnect as exc:
        return {"closed": exc.code}
    except Exception as exc:
        return {"error": type(exc).__name__ + ": " + str(exc)[:200]}


out["ws_events"] = ws_close(f"/ws/events?token={OW.api_key}")
sys.stdout.write("\n__OPS_WRITE_SWEEP__" + json.dumps(out))
'''


def run(families, python: str = sys.executable, timeout: int = 600) -> dict:
    """Run the sweep; raise (never skip) on a failed import or an empty result.

    Hermetic environment: a sibling test that sets a variable in `os.environ`
    (a credential key, a feature flag) must not change what the app does here."""
    env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG") if k in os.environ}
    proc = subprocess.run(
        [python, "-c", SCRIPT, str(BACKEND), json.dumps(families)],
        capture_output=True, text=True, timeout=timeout, cwd=str(REPO), env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ops_write sweep subprocess failed (exit {proc.returncode}):\n{proc.stderr[-6000:]}")
    marker = proc.stdout.rfind("__OPS_WRITE_SWEEP__")
    if marker < 0:
        raise RuntimeError(f"ops_write sweep printed no result:\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}")
    out = json.loads(proc.stdout[marker + len("__OPS_WRITE_SWEEP__"):])
    if not out.get("routes"):
        raise RuntimeError("ops_write sweep found zero routes")
    return out
