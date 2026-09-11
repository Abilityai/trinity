"""ent#532 — the rail's Canvas/Files dot lights on an event, not the next refetch.

Two `/ws` triggers are emitted beside the write that causes them:
`canvas_updated` from `canvas_service.write_canvas` (the one path that writes
`agent_canvases`) and `file_shared` from
`agent_shared_files_service._persist_and_register` (the shared tail of both
share entry points). What is pinned here:

  * the wire carries **ids only** — the #918 rule. A canvas block, a title, a
    filename, and above all the share `url` (which embeds a `?sig=` bearer
    token) must never appear in the serialized bytes, because `/ws` is
    `SCOPE_ALL`;
  * the payload is **agent-keyed**, so ent#467's payload-derived scope filters
    it without an allowlist entry — and the call site is a dict **literal**,
    which is what that guard's AST discovery can actually read;
  * one emit per write: `patch_canvas` funnels through `write_canvas` and emits
    once, a failed write emits nothing, and `create_share`'s idempotent replay
    emits nothing because nothing new was shared;
  * the emit can never fail or delay a write — no manager, no running loop and
    a raising manager are all silent, and the raising case leaves no
    unretrieved-task exception behind;
  * `main.py` imports and CALLS both setters, under alias names that
    `test_1483_ws_setters_wired.py`'s generic discovery regex actually covers.
"""
from __future__ import annotations

import ast
import asyncio
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from services import agent_shared_files_service as files_service
from services import canvas_service
from services.canvas_blocks import CanvasError
from services.event_bus import agent_names_in_payload

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_SETTER_RE = re.compile(r"^set_.*(?:ws|websocket)_manager$")

pytestmark = pytest.mark.unit


class _FakeManager:
    """Records what reached `/ws`. `raises` proves a trigger cannot fail a write."""

    def __init__(self, raises: bool = False):
        self.messages: list = []
        self._raises = raises

    async def broadcast(self, message):
        self.messages.append(message)
        if self._raises:
            raise RuntimeError("ws is down")


async def _drain():
    """Let the fire-and-forget task run. It has no real await, so one turn of
    the loop is enough; three is cheap insurance against a future await."""
    for _ in range(3):
        await asyncio.sleep(0)


@pytest.fixture
def canvas_mgr():
    mgr = _FakeManager()
    canvas_service.set_websocket_manager(mgr)
    try:
        yield mgr
    finally:
        canvas_service.set_websocket_manager(None)


@pytest.fixture
def files_mgr():
    mgr = _FakeManager()
    files_service.set_websocket_manager(mgr)
    try:
        yield mgr
    finally:
        files_service.set_websocket_manager(None)


def _wire_canvas(monkeypatch, *, upsert=None):
    monkeypatch.setattr(
        canvas_service.db, "upsert_agent_canvas",
        upsert or (lambda agent, canvas_id, **kw: {"agent_name": agent, "canvas_id": canvas_id, **kw}),
    )
    monkeypatch.setattr(canvas_service, "resolve_and_validate_execution", lambda eid, agent: None)


def _wire_files(monkeypatch, tmp_path):
    monkeypatch.setattr(files_service, "STORAGE_ROOT", str(tmp_path))
    monkeypatch.setattr(files_service, "detect_mime", lambda data: "text/csv")
    monkeypatch.setattr(files_service, "check_mime_blocklist", lambda data, mime: None)
    monkeypatch.setattr(files_service, "enforce_quota", lambda agent, size: None)
    monkeypatch.setattr(files_service, "check_disk_space", lambda size: None)
    monkeypatch.setattr(
        files_service, "build_download_url",
        lambda file_id, token: f"https://example.test/api/files/{file_id}?sig={token}",
    )
    monkeypatch.setattr(files_service.db, "create_agent_shared_file", lambda **kw: None)


# ---------------------------------------------------------------------------
# canvas_updated
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_canvas_emits_one_thin_canvas_updated(monkeypatch, canvas_mgr):
    _wire_canvas(monkeypatch)

    canvas_service.write_canvas(
        "a1", "q4-board",
        [{"kind": "markdown", "payload": {"markdown": "TOP SECRET acme numbers"}}],
        title="Quarterly close", audience="roster", execution_id=None,
    )
    await _drain()

    assert len(canvas_mgr.messages) == 1
    wire = canvas_mgr.messages[0]
    event = json.loads(wire)
    # The WHOLE payload — an equality assert, so a field added later fails here
    # rather than shipping silently onto an unfiltered channel.
    assert event == {"type": "canvas_updated", "agent_name": "a1", "canvas_id": "q4-board"}
    for secret in ("TOP SECRET", "acme", "Quarterly", "markdown", "roster"):
        assert secret not in wire, f"{secret!r} reached the /ws wire"
    # ent#467: the payload is self-scoping — the dispatcher reads the agent out
    # of it, so no event_bus change and no FLEET_LEVEL_ALLOWLIST entry.
    assert agent_names_in_payload(event) == {"a1"}


@pytest.mark.asyncio
async def test_patch_canvas_emits_exactly_once(monkeypatch, canvas_mgr):
    _wire_canvas(monkeypatch)
    stored = {
        "agent_name": "a1", "canvas_id": "main", "title": "Board", "audience": "roster",
        "template": None,
        "blocks": [{"id": "b1", "kind": "markdown", "payload": {"markdown": "before"}}],
    }
    monkeypatch.setattr(canvas_service.db, "get_agent_canvas", lambda a, c, audience=None: stored)

    canvas_service.patch_canvas(
        "a1", "main",
        [{"id": "b1", "kind": "markdown", "payload": {"markdown": "after"}}],
        execution_id=None,
    )
    await _drain()

    # patch_canvas funnels through write_canvas: one write, one trigger.
    assert len(canvas_mgr.messages) == 1
    assert json.loads(canvas_mgr.messages[0])["canvas_id"] == "main"


@pytest.mark.asyncio
async def test_a_write_that_did_not_happen_emits_nothing(monkeypatch, canvas_mgr):
    """The emit sits AFTER the store, on the success path only — so a cap
    rejection (the ent#553 409) and a validation refusal both stay silent."""
    def _boom(*a, **kw):
        raise CanvasError(409, "canvas limit exceeded")

    _wire_canvas(monkeypatch, upsert=_boom)
    with pytest.raises(CanvasError):
        canvas_service.write_canvas("a1", "main", [], title=None, audience="roster", execution_id=None)
    await _drain()
    assert canvas_mgr.messages == []

    _wire_canvas(monkeypatch)
    with pytest.raises(CanvasError):
        canvas_service.write_canvas("a1", "bad id!", [], title=None, audience="roster", execution_id=None)
    await _drain()
    assert canvas_mgr.messages == []


@pytest.mark.asyncio
async def test_no_manager_is_silent(monkeypatch):
    _wire_canvas(monkeypatch)
    canvas_service.set_websocket_manager(None)
    out = canvas_service.write_canvas("a1", "main", [], title=None, audience="roster", execution_id=None)
    await _drain()
    assert out["canvas_id"] == "main"


def test_no_running_loop_is_silent(monkeypatch, canvas_mgr):
    """A future caller from an executor thread must degrade to 'no trigger'
    (the pre-ent#532 refetch triggers), never raise inside a write. This test is
    deliberately SYNC: there is no running loop in it."""
    _wire_canvas(monkeypatch)
    out = canvas_service.write_canvas("a1", "main", [], title=None, audience="roster", execution_id=None)
    assert out["canvas_id"] == "main"
    assert canvas_mgr.messages == []


@pytest.mark.asyncio
async def test_a_raising_manager_never_fails_the_write(monkeypatch):
    _wire_canvas(monkeypatch)
    mgr = _FakeManager(raises=True)
    canvas_service.set_websocket_manager(mgr)
    try:
        out = canvas_service.write_canvas("a1", "main", [], title=None, audience="roster", execution_id=None)
        assert out["canvas_id"] == "main", "a broadcast failure must not fail the write"
        await _drain()
        # The await is wrapped inside the task, so gathering it is clean. Drop
        # that wrapper and this raises — which is also the "Task exception was
        # never retrieved" noise the wrapper exists to prevent.
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        await asyncio.gather(*pending)
    finally:
        canvas_service.set_websocket_manager(None)


# ---------------------------------------------------------------------------
# file_shared
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_persist_and_register_emits_one_thin_file_shared(monkeypatch, tmp_path, files_mgr):
    _wire_files(monkeypatch, tmp_path)

    result = files_service._persist_and_register(
        "a1", b"employee,salary\n", basename="payroll-Q3.xlsx",
        display_name="payroll-Q3.xlsx", expires_in=3600, created_by="a1",
    )
    await _drain()

    assert len(files_mgr.messages) == 1
    wire = files_mgr.messages[0]
    event = json.loads(wire)
    assert event == {"type": "file_shared", "agent_name": "a1", "file_id": result["file_id"]}
    # The url is a bearer credential (`?sig=<download_token>`), and the display
    # name is the agent's own text. Neither belongs on an unfiltered channel.
    assert result["url"] not in wire
    assert "sig=" not in wire and "payroll" not in wire and "text/csv" not in wire
    assert agent_names_in_payload(event) == {"a1"}


@pytest.mark.asyncio
async def test_create_share_emits_once_and_a_replay_emits_nothing(monkeypatch, tmp_path, files_mgr):
    _wire_files(monkeypatch, tmp_path)
    monkeypatch.setattr(files_service.db, "get_file_sharing_enabled", lambda a: True)

    async def _extract(agent, container_path):
        return (b"bytes", "report.csv")

    monkeypatch.setattr(files_service, "extract_from_agent", _extract)

    class _Guard:
        def __init__(self, replay, snapshot=None):
            self.replay = replay
            self.snapshot = snapshot

    def _guard_factory(guard):
        @asynccontextmanager
        async def _guard(*a, **kw):
            yield guard
        return _guard

    # A real share runs the tail and emits.
    monkeypatch.setattr(files_service.idempotency_service, "effect_guard",
                        _guard_factory(_Guard(replay=False)))
    await files_service.create_share("a1", "report.csv", execution_id="e1")
    await _drain()
    assert len(files_mgr.messages) == 1

    # A replay returns the stored snapshot WITHOUT reaching the tail — nothing
    # new was shared, so the dot must not light a second time.
    snapshot = {"file_id": "old-1", "url": "https://example.test/api/files/old-1?sig=tok"}
    monkeypatch.setattr(files_service.idempotency_service, "effect_guard",
                        _guard_factory(_Guard(replay=True, snapshot=snapshot)))
    out = await files_service.create_share("a1", "report.csv", execution_id="e1")
    await _drain()
    assert out == snapshot
    assert len(files_mgr.messages) == 1, "the idempotent replay emitted a second trigger"


# ---------------------------------------------------------------------------
# Shape guards — what the ent#467 and #1483 guards need from us
# ---------------------------------------------------------------------------

def _broadcast_literals(rel: str):
    """Every `_broadcast({...})` call site in one service, as key→value maps."""
    tree = ast.parse((_BACKEND / rel).read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if "broadcast" not in name or not isinstance(node.args[0], ast.Dict):
            continue
        out.append({
            k.value: getattr(v, "value", "<expr>")
            for k, v in zip(node.args[0].keys, node.args[0].values)
            if isinstance(k, ast.Constant)
        })
    return out


@pytest.mark.parametrize("rel,event,id_key", [
    ("services/canvas_service.py", "canvas_updated", "canvas_id"),
    ("services/agent_shared_files_service.py", "file_shared", "file_id"),
])
def test_the_call_site_is_a_dict_literal_the_scope_guard_can_read(rel, event, id_key):
    """ent#467's discovery resolves the payload by AST. A helper that splices
    `{"type": event, **payload}` resolves to `<dynamic>` with no agent key and
    is classified as fleet-level — i.e. it fails the guard. The literal at the
    call site is the mechanism, not a style preference."""
    literals = [d for d in _broadcast_literals(rel) if d.get("type") == event]
    assert len(literals) == 1, f"expected exactly one {event} emit in {rel}"
    assert set(literals[0]) == {"type", "agent_name", id_key}
    assert agent_names_in_payload({k: "x" for k in literals[0]}), (
        "the payload must carry a key ent#467's agent_names_in_payload reads"
    )


def test_main_imports_and_calls_both_setters():
    """Belt-and-braces beside `test_1483_ws_setters_wired.py` — and an
    assertion that our alias names actually match ITS discovery regex, since an
    unmatched alias is silently uncovered rather than loudly broken."""
    tree = ast.parse((_BACKEND / "main.py").read_text(encoding="utf-8"))
    aliases = {
        alias.asname
        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        for alias in node.names if alias.asname and alias.name == "set_websocket_manager"
    }
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    for expected in ("set_canvas_ws_manager", "set_shared_files_ws_manager"):
        assert expected in aliases, f"main.py does not import {expected}"
        assert expected in called, f"main.py imports {expected} but never calls it"
        assert _SETTER_RE.match(expected), "the alias is outside #1483's discovery regex"
