"""trinity-enterprise#595 — the REAL `/ws` handler refuses a ticket minted for
another socket.

`consume_ticket` takes the exact scope its consumer accepts. The service matrix
(`test_ws_ticket_service.py`) proves the comparison and the AST pin
(`test_837_ws_workspace_event_kinds.py`) proves `/ws` passes `scope="user"`;
this file proves the two together on `main.websocket_endpoint` itself: a voice
or VoIP ticket whose subject IS a resolvable username is consumed and closed
4001 before identity resolution and before the socket is registered. A
`user`-scoped ticket for the same subject is the control that shows the harness
reaches resolution at all.

`main` is imported in a SUBPROCESS (the `test_2996_route_census_runtime.py`
pattern): importing it in the unit island is order-dependent (#1483), and this
must neither skip nor be polluted. It fails, never skips, when the subprocess
cannot import `main` or prints nothing parseable.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
MARK = "__WS595__"

DRIVER = r'''
import asyncio, json, os, sys, tempfile
from pathlib import Path
backend = sys.argv[1]
td = os.path.realpath(tempfile.mkdtemp(prefix="trinity-ws595-"))
# Every store this process could touch is pinned to a throwaway BEFORE `import
# main` (which initialises and migrates the database): the parent's
# DATABASE_URL / REDIS_URL are dropped by the caller and overwritten here, so a
# developer's or CI's real store is never reached. Redis is never contacted —
# the ticket service is given fakeredis below; the placeholder is unroutable.
db_file = str(Path(td) / "ws595.db")
for k, v in {
    "DATABASE_URL": "sqlite:///" + db_file, "TRINITY_DB_PATH": db_file,
    "REDIS_URL": "redis://test:test@127.0.0.1:1/0", "REDIS_PASSWORD": "test",
    "REDIS_BACKEND_PASSWORD": "test", "AGENT_AUTH_SECRET": "0" * 64,
    "SECRET_KEY": "x" * 32, "INTERNAL_API_SECRET": "y" * 32,
    "LOG_ARCHIVE_PATH": str(Path(td) / "logs"),
}.items():
    os.environ[k] = v
sys.path.insert(0, backend)
from db.engine import resolve_database_url
from db.connection import _resolve_sqlite_path
store_check = {"tmp": td, "url": resolve_database_url(), "sqlite_path": _resolve_sqlite_path()}
assert store_check["url"] == "sqlite:///" + db_file, store_check
assert os.path.realpath(store_check["sqlite_path"]).startswith(td + os.sep), store_check
import fakeredis
import main
import services.ws_ticket_service as tickets
from fastapi import WebSocketDisconnect

store = fakeredis.FakeRedis(decode_responses=True)
tickets.get_redis_client = lambda: store
calls = {"resolve": [], "connect": 0}

def resolve(sub):
    calls["resolve"].append(sub)
    return {"email": "alice@example.com", "is_admin": False, "accessible_agents": [],
            "allowed_types": None, "allowed_fields": None}

async def connect(ws, **kw):
    calls["connect"] += 1

main.resolve_ws_identity = resolve
main.manager.connect = connect
main.manager.disconnect = lambda ws: None

class Socket:
    def __init__(self):
        self.accepted, self.close_code = False, None
    async def accept(self):
        self.accepted = True
    async def close(self, code=1000, reason=""):
        self.close_code = code
    async def receive_text(self):
        raise WebSocketDisconnect()
    async def send_text(self, data):
        pass

out = {}
for name, scope, claims in [
    ("voice", "voice:vs_595", {"user_id": 1, "origin": "workspace"}),
    ("voip", "voip:call_595", None),
    ("user", "user", None),
]:
    calls["resolve"], calls["connect"] = [], 0
    ticket = tickets.mint_ticket("alice", scope=scope, claims=claims) if claims else \
        tickets.mint_ticket("alice", scope=scope)
    ws = Socket()
    asyncio.run(main.websocket_endpoint(ws, ticket=ticket, last_event_id=None))
    out[name] = {"close": ws.close_code, "resolved": list(calls["resolve"]),
                 "registered": calls["connect"], "still_stored": bool(store.exists("ws_ticket:" + ticket))}
from db.engine import resolve_database_url as _url_after
out["_store"] = dict(store_check, url_after_import=_url_after())
print("''' + MARK + r'''" + json.dumps(out))
'''


# Inherited variables that could point the subprocess at a real store or tree.
_DROP = ("PYTHONPATH", "DATABASE_URL", "TEST_POSTGRES_URL", "TRINITY_DB_PATH", "REDIS_URL",
         "REDIS_PASSWORD", "REDIS_BACKEND_PASSWORD")


def _drive(backend: Path = BACKEND) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in _DROP and not k.startswith(("POSTGRES", "PG"))}
    proc = subprocess.run([sys.executable, "-c", DRIVER, str(backend)], capture_output=True,
                          text=True, timeout=300, cwd=str(backend.parents[1]), env=env)
    assert proc.returncode == 0, f"the /ws driver failed (exit {proc.returncode}):\n{proc.stderr[-4000:]}"
    at = proc.stdout.rfind(MARK)
    assert at >= 0, f"the /ws driver printed no result:\n{proc.stdout[-2000:]}"
    return json.loads(proc.stdout[at + len(MARK):])


@pytest.fixture(scope="module")
def outcomes():
    return _drive()


@pytest.mark.parametrize("surface", ["voice", "voip"])
def test_ws_refuses_a_ticket_minted_for_another_socket_before_resolving_anyone(outcomes, surface):
    got = outcomes[surface]
    assert got["close"] == 4001, got
    assert got["resolved"] == [], f"/ws resolved the subject of a {surface} ticket: {got}"
    assert got["registered"] == 0, got
    assert got["still_stored"] is False, "the refused ticket must be spent, not left for its own socket"


def test_the_driver_only_ever_used_a_throwaway_database(outcomes):
    """Guard: whatever DATABASE_URL the parent shell carries, `import main` in
    the driver initialised a SQLite file under its own temp dir."""
    store = outcomes["_store"]
    tmp = store["tmp"]
    assert store["url"] == store["url_after_import"]
    assert store["url"].startswith("sqlite:///" + tmp + os.sep), store
    assert os.path.realpath(store["sqlite_path"]).startswith(tmp + os.sep), store


def test_the_control_a_user_ticket_reaches_resolution_and_registration(outcomes):
    got = outcomes["user"]
    assert got["close"] is None, got
    assert got["resolved"] == ["alice"]
    assert got["registered"] == 1
    assert got["still_stored"] is False
