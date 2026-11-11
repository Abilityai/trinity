"""Drive human-only refusals through the REAL app and report the answers and
the audit rows (trinity-enterprise#719).

Run ONLY as a subprocess, by `test_ent719_human_only_refusal_audit.py`:
`import main` stands the whole platform up (every router, the app's exception
handler table, OpenTelemetry), and none of that may leak into the unit island,
which is why `test_2996_route_census_runtime.py` imports the app the same way.

The parent sets `TRINITY_DB_PATH` to a new file, so this starts on a fresh
install: no audit rows. `get_current_user` is overridden with stated `User`
principals (every scope field set, never a `MagicMock`); everything else is the
shipped app. Prints one JSON document after the `__AUDIT__` marker.
"""

import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

SENTINEL = "hoaudit-body-sentinel-7f3a"
OWNER = "hoaudit-owner"
OPERATOR = "hoaudit-operator"
AGENT_A, AGENT_B = "hoaudit-agent-a", "hoaudit-agent-b"

# One route per raise site of the human-only primitives, plus the routes the
# negative cases need. (method, url, json body or None)
DEPENDS = ("GET", "/api/mcp/keys", None)  # Depends(require_interactive)
IMPERATIVE = (
    "GET",
    "/api/users/me/preferences",
    None,
)  # reject_non_interactive_principal
PERSON = (
    "PUT",
    f"/api/agents/{AGENT_A}/autonomy",
    {"enabled": True},
)  # Depends(require_person)
ASK = (
    "POST",
    "/api/operator-queue/hoaudit-item/respond",
    {"response": "approve"},
)  # reject_non_person_principal
SELF_GRANT = (
    "PUT",
    f"/api/agents/{AGENT_A}/read-only",
    {"enabled": False},
)  # require_person_or_capability, self
FILES = (
    "GET",
    f"/api/agents/{AGENT_A}/files/download?path=.env",
    None,
)  # audits its own refusal
ADMIN_READ = ("GET", "/api/settings", None)  # require_admin: a plain 403
SIBLING_GRANT = (
    "PUT",
    f"/api/agents/{AGENT_B}/read-only",
    {"enabled": False},
)  # capability refusal
BODY = (
    "PUT",
    f"/api/users/me/preferences/grid?probe={SENTINEL}",
    {"value": {"note": SENTINEL}, "base_updated_at": None},
)
# One route template, two concrete paths and two methods (the window's key).
PREF_BODY = {"value": {"n": 1}, "base_updated_at": None}
PREF_PUT_A = ("PUT", "/api/users/me/preferences/a", PREF_BODY)
PREF_PUT_B = ("PUT", "/api/users/me/preferences/b", PREF_BODY)
PREF_DELETE_A = ("DELETE", "/api/users/me/preferences/a", None)
# Values longer than the row keeps (`error_handlers._CALLER_SIZED_MAX`).
LONG_AGENT = "a" * 4000
LONG_REQUEST_ID = "r" * 4000
CALLER_SIZED = ("PUT", f"/api/agents/{LONG_AGENT}/autonomy", {"enabled": True})


def _bootstrap(backend: str) -> str:
    td = tempfile.mkdtemp(prefix="trinity-refusal-audit-")
    for k, v in {
        "REDIS_URL": "redis://test:test@redis:6379",
        "REDIS_PASSWORD": "test",
        "REDIS_BACKEND_PASSWORD": "test",
        "AGENT_AUTH_SECRET": "0" * 64,
        "SECRET_KEY": "x" * 32,
        "INTERNAL_API_SECRET": "y" * 32,
    }.items():
        os.environ.setdefault(k, v)
    if not os.environ.get("TRINITY_DB_PATH"):
        raise SystemExit("TRINITY_DB_PATH must name a fresh database file")
    os.environ["LOG_ARCHIVE_PATH"] = str(Path(td) / "logs")
    sys.path.insert(0, backend)
    return td


def run(backend: str, td: str) -> dict:
    import main  # noqa: F401 — the real app, its handler table included
    import dependencies
    import error_handlers
    from database import db
    from db.engine import get_engine
    from db_models import UserCreate
    from fastapi.testclient import TestClient
    from models import User
    from sqlalchemy import text
    from services.platform_audit_service import platform_audit_service

    app = main.app
    refused_cls = dependencies.HumanOnlyRefused
    registered = app.exception_handlers.get(refused_cls)
    out: dict = {
        "handler_registered": registered is error_handlers.human_only_refused,
        # Starlette consults a status-code handler before any class handler, so
        # one for 403 would answer every refusal before the audit handler ran.
        "status_403_handler": 403 in app.exception_handlers,
        "temp_dir": td,
    }

    def rows_after(last_id: int) -> list:
        with get_engine().connect() as conn:
            return [
                dict(r._mapping)
                for r in conn.execute(
                    text("SELECT * FROM audit_log WHERE id > :i ORDER BY id"),
                    {"i": last_id},
                )
            ]

    def last_id() -> int:
        with get_engine().connect() as conn:
            return conn.execute(
                text("SELECT COALESCE(MAX(id), 0) FROM audit_log")
            ).scalar()

    out["rows_at_start"] = len(rows_after(0))

    db.create_user(UserCreate(username=OWNER, role="admin", email="owner@example.com"))
    db.create_user(
        UserCreate(username=OPERATOR, role="operator", email="operator@example.com")
    )
    owner_id = db.get_user_by_username(OWNER)["id"]
    operator_id = db.get_user_by_username(OPERATOR)["id"]
    out["owner_id"] = owner_id
    db.register_agent_owner(AGENT_A, OWNER)
    db.register_agent_owner(AGENT_B, OWNER)

    def principal(**kw) -> User:
        base = dict(
            id=owner_id,
            username=OWNER,
            email="owner@example.com",
            role="admin",
            agent_name=None,
            connector_agent=None,
            portal_delegate=False,
            mcp_scope=None,
            mcp_key_id=None,
            mcp_key_name=None,
            vouched_source_agent=None,
            loopback_chain_depth=None,
            is_event_loopback=False,
        )
        base.update(kw)
        return User(**base)

    kinds = {
        "agent": principal(
            mcp_scope="agent",
            agent_name=AGENT_A,
            mcp_key_id="key-agent-a",
            mcp_key_name="agent-a-key",
        ),
        "sibling": principal(
            mcp_scope="agent",
            agent_name=AGENT_B,
            mcp_key_id="key-agent-b",
            mcp_key_name="agent-b-key",
        ),
        "system": principal(
            mcp_scope="system",
            mcp_key_id="key-system",
            mcp_key_name="trinity-system-key",
        ),
        "user": principal(
            mcp_scope="user", mcp_key_id="key-user", mcp_key_name="owner-user-key"
        ),
        "connector": principal(
            mcp_scope="connector",
            connector_agent=AGENT_A,
            mcp_key_id="key-connector",
            mcp_key_name="connector-key",
        ),
        "ops": principal(mcp_scope="ops", mcp_key_id="key-ops", mcp_key_name="ops-key"),
        "portal_delegate": principal(
            mcp_scope="portal_delegate",
            portal_delegate=True,
            mcp_key_id="key-delegate",
            mcp_key_name="delegate-key",
        ),
        "unknown": principal(
            mcp_scope="a-scope-no-one-has-shipped",
            mcp_key_id="key-unknown",
            mcp_key_name="unknown-key",
        ),
        # A vouched EVT-001 loopback JWT: no scope, the backend's source agent.
        "loopback": principal(vouched_source_agent=AGENT_A, is_event_loopback=True),
        # An unvouched loopback JWT (no source agent), and a stand-in principal
        # with no `mcp_scope` at all: neither names an agent or a key.
        "unvouched_loopback": principal(is_event_loopback=True),
        "no_scope": SimpleNamespace(
            **{k: v for k, v in principal().model_dump().items() if k != "mcp_scope"}
        ),
        "session": principal(),
        "operator_session": principal(
            id=operator_id,
            username=OPERATOR,
            email="operator@example.com",
            role="operator",
        ),
    }

    current = {"user": None}
    app.dependency_overrides[dependencies.get_current_user] = lambda: current["user"]
    client = TestClient(app, raise_server_exceptions=True)
    # The audit-failure cases answer through this one: were the failure to
    # escape the handler, the case would read 500 instead of ending the run.
    lenient = TestClient(app, raise_server_exceptions=False)
    seq = {"n": 0}

    def call(
        kind: str, route: tuple, headers: dict | None = None, via=None
    ) -> dict:
        method, url, body = route
        current["user"] = kinds[kind]
        seq["n"] += 1
        rid = f"hoaudit-req-{seq['n']}"
        before = last_id()
        hdrs = {"X-Request-ID": rid}
        hdrs.update(headers or {})
        r = (via or client).request(method, url, json=body, headers=hdrs)
        return {
            "kind": kind,
            "sent": {"method": method, "url": url, "json": body, "headers": hdrs},
            "request_id": rid,
            "status": r.status_code,
            "body": r.content.decode("latin-1"),
            "headers": sorted([k.lower(), v] for k, v in r.headers.items()),
            "rows": rows_after(before),
        }

    def reset_window() -> None:
        error_handlers._recent_refusals.clear()

    clock = {"t": 1000.0}
    error_handlers._clock = lambda: clock["t"]

    def set_handler(present: bool) -> None:
        if present:
            if registered is not None:
                app.exception_handlers[refused_cls] = registered
        else:
            app.exception_handlers.pop(refused_cls, None)
        app.middleware_stack = None  # rebuilt from the handler table on the next call

    # --- fresh install: the first refusal writes the first row -----------------
    out["first_refusal"] = call("agent", DEPENDS)
    reset_window()

    # --- one Depends route and one imperative route, three principals each -----
    out["matrix"] = {}
    for name, route in (("depends", DEPENDS), ("imperative", IMPERATIVE)):
        out["matrix"][name] = {}
        for kind in ("agent", "system", "user"):
            out["matrix"][name][kind] = call(kind, route)
            reset_window()

    # --- every other non-person principal, on the Depends route ---------------
    out["other_kinds"] = {}
    for kind in (
        "sibling",
        "connector",
        "ops",
        "portal_delegate",
        "unknown",
        "loopback",
    ):
        out["other_kinds"][kind] = call(kind, DEPENDS)
        reset_window()

    # --- principals that name no agent and no key (PERSON refuses both) -------
    out["unnamed_kinds"] = {}
    for kind in ("unvouched_loopback", "no_scope"):
        out["unnamed_kinds"][kind] = call(kind, PERSON)
        reset_window()

    # --- a person is not refused and writes nothing ---------------------------
    out["person_passes"] = {
        "depends": call("session", DEPENDS),
        "imperative": call("session", IMPERATIVE),
    }

    # --- the other raise sites of the family ----------------------------------
    out["sites"] = {}
    for name, route in (("person", PERSON), ("ask", ASK), ("self_grant", SELF_GRANT)):
        out["sites"][name] = {}
        for kind in ("agent", "system"):
            if name == "self_grant" and kind == "system":
                continue  # the self arm exists only for an agent key
            out["sites"][name][kind] = call(kind, route)
            reset_window()

    # --- a site that already audits its own refusal ---------------------------
    out["already_audits"] = {k: call(k, FILES) for k in ("agent", "system")}
    reset_window()

    # --- unrelated 403s -------------------------------------------------------
    out["unrelated"] = {
        "admin_gate_operator": call("operator_session", ADMIN_READ),
        "admin_gate_agent": call("agent", ADMIN_READ),
        "capability_refusal": call("agent", SIBLING_GRANT),
    }
    reset_window()

    # --- what a row carries of the request ------------------------------------
    # The sentinel goes in the body, the query and five headers, three of them
    # the forwarding headers a proxy sets; the handler copies only the request
    # id, and actor_ip is the address the app resolved.
    out["body"] = call(
        "agent",
        BODY,
        headers={
            "X-Probe": SENTINEL,
            "User-Agent": SENTINEL,
            "X-Forwarded-For": f"{SENTINEL}, 10.0.0.1",
            "X-Real-IP": SENTINEL,
            "CF-Connecting-IP": SENTINEL,
        },
    )
    reset_window()

    # the caller-sized values are cut to the length the row keeps
    out["caller_sized"] = call(
        "agent", CALLER_SIZED, headers={"X-Request-ID": LONG_REQUEST_ID}
    )
    reset_window()

    # --- the bound: one row per principal, method and route per window --------
    window = []
    for t in (1000.0, 1030.0, 1059.9):
        clock["t"] = t
        window.append(call("agent", DEPENDS))
    clock["t"] = 1060.0
    window.append(call("agent", DEPENDS))  # the window has passed
    clock["t"] = 1061.0
    window.append(call("agent", IMPERATIVE))  # same principal, another route
    window.append(call("sibling", DEPENDS))  # another principal, same route
    out["window"] = window
    reset_window()

    # the key is the route template and the method, not the concrete path
    clock["t"] = 3000.0
    out["template"] = {
        "put_a": call("agent", PREF_PUT_A),
        "put_b": call("agent", PREF_PUT_B),
        "delete_a": call("agent", PREF_DELETE_A),
    }
    reset_window()

    clock["t"] = 5000.0
    loop = [call("agent", DEPENDS) for _ in range(50)]
    out["loop"] = {
        "statuses": sorted({c["status"] for c in loop}),
        "rows": sum(len(c["rows"]) for c in loop),
    }
    reset_window()

    # memory bound: with room for two keys, a full window records nothing new
    # until entries expire, and never forgets an unexpired key
    warnings: list = []

    class _Capture(logging.Handler):
        def emit(self, record):
            warnings.append(record.getMessage())

    capture = _Capture(level=logging.WARNING)
    error_handlers.logger.addHandler(capture)
    saved_max = error_handlers._REFUSAL_WINDOW_MAX
    error_handlers._REFUSAL_WINDOW_MAX = 2
    error_handlers._window_full_logged_at = None
    seq_rows = []
    sizes = []
    for t, kind in (
        (9000.0, "agent"),
        (9001.0, "system"),
        (9002.0, "user"),  # the window is full: not recorded
        (9003.0, "agent"),  # still inside its window: not recorded
        (9004.0, "sibling"),  # still full: not recorded, logged once only
        (9061.0, "user"),  # both entries expired: recorded
        (9062.0, "agent"),  # its window ended too: recorded
    ):
        clock["t"] = t
        seq_rows.append(len(call(kind, DEPENDS)["rows"]))
        sizes.append(len(error_handlers._recent_refusals))
    out["full_window"] = {
        "rows": seq_rows,
        "sizes": sizes,
        "full_warnings": sum("window is full" in w for w in warnings),
    }
    error_handlers._REFUSAL_WINDOW_MAX = saved_max
    error_handlers._window_full_logged_at = None
    error_handlers.logger.removeHandler(capture)
    reset_window()

    # --- an audit failure still answers the same 403 --------------------------
    # Each failure is followed, with the failure removed and the window NOT
    # reset, by the same refusal one second later: a write that failed must not
    # have used the window.
    failures = {}
    retries = {}

    async def log_raises(*a, **kw):
        raise RuntimeError("audit sink down")

    clock["t"] = 7000.0
    real_log = platform_audit_service.log
    platform_audit_service.log = log_raises
    try:
        failures["log_raises"] = call("agent", DEPENDS, via=lenient)
    finally:
        platform_audit_service.log = real_log
    clock["t"] = 7001.0
    retries["log_raises"] = call("agent", DEPENDS)
    reset_window()

    def insert_raises(entry):
        raise RuntimeError("disk full")

    clock["t"] = 7100.0
    db.create_audit_entry = insert_raises  # the service swallows this one itself
    try:
        failures["insert_raises"] = call("agent", DEPENDS, via=lenient)
    finally:
        del db.create_audit_entry
    clock["t"] = 7101.0
    retries["insert_raises"] = call("agent", DEPENDS)
    reset_window()

    real_actor = error_handlers._refusal_actor

    def actor_raises(user):
        raise RuntimeError("cannot read the principal")

    clock["t"] = 7200.0
    error_handlers._refusal_actor = actor_raises
    try:
        failures["handler_raises"] = call("agent", DEPENDS, via=lenient)
    finally:
        error_handlers._refusal_actor = real_actor
    clock["t"] = 7201.0
    retries["handler_raises"] = call("agent", DEPENDS)
    reset_window()
    out["failures"] = failures
    out["failure_retries"] = retries

    # --- byte identity: the same requests with the handler removed ------------
    refused = [
        ("agent", DEPENDS),
        ("system", DEPENDS),
        ("user", DEPENDS),
        ("agent", IMPERATIVE),
        ("system", IMPERATIVE),
        ("user", IMPERATIVE),
        ("loopback", DEPENDS),
        ("agent", PERSON),
        ("system", PERSON),
        ("agent", ASK),
        ("agent", SELF_GRANT),
        ("agent", FILES),
        ("agent", BODY),
    ]
    identity = {"with": [], "without": []}
    for phase, present in (("with", True), ("without", False)):
        set_handler(present)
        for kind, route in refused:
            reset_window()
            r = call(kind, route)
            identity[phase].append(
                {
                    "case": f"{kind} {route[0]} {route[1]}",
                    "status": r["status"],
                    "body": r["body"],
                    # X-Request-ID is per call; every other header must match
                    "headers": [h for h in r["headers"] if h[0] != "x-request-id"],
                    "human_only_rows": sum(
                        1
                        for row in r["rows"]
                        if row["event_action"] == "human_only_refused"
                    ),
                }
            )
    set_handler(True)
    out["identity"] = identity

    # --- the answer is the app's own HTTPException handler, whatever it is -----
    from starlette.exceptions import HTTPException as StarletteHTTPException

    base = app.exception_handlers[StarletteHTTPException]

    async def marked(request, exc):
        response = await base(request, exc)
        response.headers["x-test-http-handler"] = "replaced"
        return response

    app.exception_handlers[StarletteHTTPException] = marked
    app.middleware_stack = None
    try:
        reset_window()
        out["replaced_http_handler"] = {
            "refused": call("agent", DEPENDS),
            "unrelated": call("operator_session", ADMIN_READ),
        }
    finally:
        app.exception_handlers[StarletteHTTPException] = base
        app.middleware_stack = None
    reset_window()
    out["details"] = {
        "HUMAN_ONLY_DETAIL": dependencies.HUMAN_ONLY_DETAIL,
        "PERSON_REQUIRED_DETAIL": dependencies.PERSON_REQUIRED_DETAIL,
    }
    out["all_rows"] = rows_after(0)
    return out


if __name__ == "__main__":
    temp_dir = _bootstrap(sys.argv[1])
    try:
        result = run(sys.argv[1], temp_dir)
    finally:
        shutil.rmtree(temp_dir)
    sys.stdout.write("\n__AUDIT__" + json.dumps(result, default=str))
