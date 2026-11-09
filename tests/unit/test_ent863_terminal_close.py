"""abilityai/trinity-enterprise#863 — terminals follow the account.

What is pinned here, for both terminal doors (the agent terminal and the
admin-only system-agent terminal):

  * an open terminal registers with the worker's live-connection registry and
    closes when the account's access changes: the browser gets an error frame
    and ``4003``, the handler returns normally through its own cleanup (its
    task is never cancelled, so the server logs no error per close), the
    container socket is closed, and the one-terminal-per-person slot is freed;
  * the close cancels the handler's two forwarders, so it ends a session blocked
    in the browser read and in the container read alike;
  * input that arrives while the terminal is being closed is not forwarded;
  * a change that lands while the shell is starting does not interrupt the
    start: the door checks once the start returns, closes the socket it got and
    never reports the session as started;
  * a terminal closed for an access change also ends its shell session in the
    container, and only then; the end-of-shell exec is told the session's own
    marker, the one the shell was started with;
  * the snapshot a terminal is compared with holds the role and email the door
    authorized it on;
  * the end-of-shell script ends the marked processes and their session and
    nothing else (executed where ``/proc`` exists, i.e. on Linux CI).
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
while str(_BACKEND) in sys.path:
    sys.path.remove(str(_BACKEND))
sys.path.insert(0, str(_BACKEND))
_TESTS = str(Path(__file__).resolve().parents[1])
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)

from db_harness import db_backend, seed_user  # noqa: E402,F401

import routers.system_agent as system_agent  # noqa: E402
import services.agent_service.terminal as terminal  # noqa: E402

pytestmark = pytest.mark.unit

ALICE, ADMIN = "tc-alice", "tc-admin"
ALICE_EMAIL, ADMIN_EMAIL = "tc-alice@example.test", "tc-admin@example.test"
AGENT = "tc-atlas"


def _run_sql(sql: str, **binds) -> None:
    from sqlalchemy import text
    from db.engine import get_engine

    with get_engine().begin() as conn:
        conn.execute(text(sql), binds)


@pytest.fixture
def people(db_backend):  # noqa: F811 — the db_harness fixture, by name
    seed_user(1, ALICE, role="creator")
    seed_user(2, ADMIN, role="admin")
    _run_sql("UPDATE users SET email = :e, password_hash = 'hash-a1' WHERE id = 1", e=ALICE_EMAIL)
    _run_sql("UPDATE users SET email = :e, password_hash = 'hash-x1' WHERE id = 2", e=ADMIN_EMAIL)


def _terminal_globals():
    """The namespace the terminal code really runs in (a re-imported module
    elsewhere in the session cannot leave a patch unreached)."""
    return terminal.TerminalAccess.__init__.__globals__


@pytest.fixture
def lc(monkeypatch):
    """The live-connection module the terminals use, with a fresh registry."""
    module = _terminal_globals()["live_connections"]
    reg = module.LiveConnectionRegistry()
    monkeypatch.setattr(module, "registry", reg)
    return module


class _Docker:
    """The Docker surface a terminal touches: a socketpair stands in for the
    exec's attach socket, and the end-of-shell exec is recorded."""

    def __init__(self, on_start=None, end_result=None):
        self.door_end, self.peer = socket.socketpair()
        self.peer.settimeout(2.0)
        self.created = []
        self.ended = []
        self.on_start = on_start
        self.end_result = end_result or {"exit_code": 0, "output": "ended", "timed_out": False}

    def container(self, *_a, **_k):
        return SimpleNamespace(id="cid-1", name=f"agent-{AGENT}", status="running")

    async def reload(self, _container):
        return None

    async def exec_create(self, container_id, cmd, **kwargs):
        self.created.append({"container_id": container_id, "cmd": cmd, **kwargs})
        return {"Id": "exec-1"}

    async def exec_start(self, exec_id, socket=False, tty=True):
        if self.on_start is not None:
            await self.on_start()
        return SimpleNamespace(_sock=self.door_end)

    async def end_shell(self, container_name, command, timeout=60, *, environment=None, user="developer"):
        self.ended.append({"container": container_name, "command": command, "user": user})
        return self.end_result

    def marker(self):
        """The session marker the shell was started with."""
        env = self.created[0]["environment"]
        return env[terminal.TERMINAL_SESSION_ENV]

    def peer_closed(self) -> bool:
        try:
            return self.peer.recv(1) == b""
        except OSError:
            return True


class _Browser:
    """A browser websocket: the auth message, then input only when pushed."""

    def __init__(self, token="tok"):
        self._auth = json.dumps({"type": "auth", "token": token})
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.frames = []
        self.closed = None
        self.on_error_frame = None

    async def accept(self):
        return None

    async def receive_text(self):
        return self._auth

    async def receive(self):
        return await self.inbox.get()

    async def send_text(self, text):
        frame = json.loads(text)
        self.frames.append(frame)
        if frame.get("type") == "error" and self.on_error_frame is not None:
            await self.on_error_frame()

    async def send_bytes(self, data):
        return None

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)

    def started(self) -> bool:
        return any(f.get("type") == "auth_success" for f in self.frames)


def _wire_agent_door(monkeypatch, docker, role="creator"):
    g = terminal.TerminalSessionManager.handle_terminal_session.__globals__
    monkeypatch.setattr(g["db"], "can_user_share_agent", lambda *_a, **_k: True)
    monkeypatch.setitem(g, "get_agent_container", docker.container)
    monkeypatch.setitem(g, "container_reload", docker.reload)
    monkeypatch.setitem(g, "api_exec_create", docker.exec_create)
    monkeypatch.setitem(g, "api_exec_start", docker.exec_start)
    monkeypatch.setitem(_terminal_globals(), "execute_command_in_container", docker.end_shell)
    return lambda _t: {"sub": ALICE, "email": ALICE_EMAIL, "role": role}


def _wire_system_door(monkeypatch, docker, role="admin"):
    g = system_agent.system_agent_terminal.__globals__
    monkeypatch.setitem(g, "decode_token", lambda _t: {"sub": ADMIN, "email": ADMIN_EMAIL, "role": role})
    monkeypatch.setitem(g, "get_agent_container", docker.container)
    monkeypatch.setitem(g, "container_reload", docker.reload)
    monkeypatch.setitem(g, "api_exec_create", docker.exec_create)
    monkeypatch.setitem(g, "api_exec_start", docker.exec_start)
    sessions = {}
    monkeypatch.setitem(g, "_active_terminal_sessions", sessions)
    monkeypatch.setitem(_terminal_globals(), "execute_command_in_container", docker.end_shell)
    return sessions


async def _until(predicate, what, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


async def _settle(reg) -> None:
    for _ in range(50):
        if not reg._inflight:
            return
        await asyncio.gather(*list(reg._inflight))
    raise AssertionError("re-checks never settled")


def _start_agent_terminal(browser, decode, manager=None):
    manager = manager or terminal.TerminalSessionManager()
    task = asyncio.ensure_future(manager.handle_terminal_session(browser, AGENT, "bash", decode))
    return manager, task


async def _finish(task, timeout=1.0):
    """The handler returns on its own, within ``timeout``, without raising and
    without being cancelled."""
    await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    assert not task.cancelled()
    assert task.exception() is None


# --------------------------------------------------------------------------- #
# The agent terminal
# --------------------------------------------------------------------------- #

def test_an_agent_terminal_closes_when_the_account_changes(people, lc, monkeypatch, caplog):
    """T3.8 / T5.2: a role change closes an open terminal that is blocked in
    the browser read and in the container read."""
    docker = _Docker()
    decode = _wire_agent_door(monkeypatch, docker)
    caplog.set_level(logging.DEBUG)

    async def scenario():
        browser = _Browser()
        manager, task = _start_agent_terminal(browser, decode)
        await _until(browser.started, "the session to start")
        assert lc.registry.count() == 1
        assert manager._active_sessions

        _run_sql("UPDATE users SET role = 'operator' WHERE username = :u", u=ALICE)
        lc.notify_account_changed(ALICE)
        await _finish(task)

        errors = [f for f in browser.frames if f.get("type") == "error"]
        assert errors and errors[-1]["close"] is True
        assert "access changed" in errors[-1]["message"].lower()
        assert browser.closed == (4003, "Access changed")
        assert docker.peer_closed()
        assert manager._active_sessions == {}
        assert lc.registry.count() == 0
        return docker

    asyncio.run(scenario())
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_a_terminal_closed_for_an_access_change_ends_its_shell(people, lc, monkeypatch):
    """D11: the end-of-shell exec runs once, as the terminal's own user, and is
    given the marker the shell was started with."""
    docker = _Docker()
    decode = _wire_agent_door(monkeypatch, docker)

    async def scenario():
        browser = _Browser()
        _, task = _start_agent_terminal(browser, decode)
        await _until(browser.started, "the session to start")
        _run_sql("UPDATE users SET role = 'operator' WHERE username = :u", u=ALICE)
        lc.notify_account_changed(ALICE)
        await _finish(task)

    asyncio.run(scenario())
    marker = docker.marker()
    uuid.UUID(hex=marker)  # a fresh random id per session
    assert len(docker.ended) == 1
    call = docker.ended[0]
    assert call["container"] == f"agent-{AGENT}"
    assert call["user"] == "developer"
    assert call["command"][:2] == ["/bin/sh", "-c"]
    assert call["command"][-1] == marker


def test_an_unchanged_account_keeps_its_terminal_and_its_shell(people, lc, monkeypatch):
    """The control: a re-check that finds the account as it was closes nothing,
    and an ordinary close by the browser does not end the shell."""
    docker = _Docker()
    decode = _wire_agent_door(monkeypatch, docker)

    async def scenario():
        browser = _Browser()
        manager, task = _start_agent_terminal(browser, decode)
        await _until(browser.started, "the session to start")
        lc.notify_account_changed(ALICE)
        await _settle(lc.registry)
        await lc.registry.revalidate()
        await asyncio.sleep(0.05)
        assert not task.done()
        assert browser.closed is None
        await browser.inbox.put({"type": "websocket.disconnect"})
        docker.peer.close()  # the shell's output side ends too
        await _finish(task)
        assert manager._active_sessions == {}
        assert lc.registry.count() == 0

    asyncio.run(scenario())
    assert docker.ended == []


def test_a_deactivated_account_loses_its_terminal_on_the_next_recheck(people, lc, monkeypatch):
    """A change no writer reported (another worker, a direct edit) is caught by
    the periodic re-check; deactivation reads as access ended."""
    docker = _Docker()
    decode = _wire_agent_door(monkeypatch, docker)

    async def scenario():
        browser = _Browser()
        _, task = _start_agent_terminal(browser, decode)
        await _until(browser.started, "the session to start")
        _run_sql("UPDATE users SET suspended_at = '2026-11-08T00:00:00Z' WHERE username = :u", u=ALICE)
        await lc.registry.revalidate()
        await _finish(task)
        assert browser.closed == (4003, "Access ended")
        assert "ended" in [f for f in browser.frames if f.get("type") == "error"][-1]["message"].lower()

    asyncio.run(scenario())
    assert len(docker.ended) == 1


def test_input_that_arrives_while_the_terminal_closes_is_not_forwarded(people, lc, monkeypatch):
    docker = _Docker()
    decode = _wire_agent_door(monkeypatch, docker)

    async def scenario():
        browser = _Browser()

        async def type_while_closing():
            await browser.inbox.put({"type": "websocket.receive", "text": "late-keystrokes"})
            for _ in range(20):
                await asyncio.sleep(0)

        browser.on_error_frame = type_while_closing
        _, task = _start_agent_terminal(browser, decode)
        await _until(browser.started, "the session to start")
        _run_sql("UPDATE users SET role = 'operator' WHERE username = :u", u=ALICE)
        lc.notify_account_changed(ALICE)
        await _finish(task)

    asyncio.run(scenario())
    assert docker.peer.recv(64) == b""  # closed, and nothing was written first


def test_a_change_while_the_shell_starts_refuses_the_session(people, lc, monkeypatch):
    """T5.1: the start is not interrupted (it runs in a thread that a cancel
    would not stop); the door checks once it returns, closes the socket it got,
    never reports the session as started, and ends the shell."""
    holder = {}

    async def change_during_start():
        _run_sql("UPDATE users SET role = 'operator' WHERE username = :u", u=ALICE)
        holder["lc"].notify_account_changed(ALICE)
        await _settle(holder["lc"].registry)

    docker = _Docker(on_start=change_during_start)
    holder["lc"] = lc
    decode = _wire_agent_door(monkeypatch, docker)

    async def scenario():
        browser = _Browser()
        manager, task = _start_agent_terminal(browser, decode)
        await _finish(task)
        assert not browser.started()
        assert browser.closed == (4003, "Access changed")
        assert docker.peer_closed()
        assert manager._active_sessions == {}
        assert lc.registry.count() == 0

    asyncio.run(scenario())
    assert len(docker.ended) == 1


def test_the_snapshot_holds_what_the_door_authorized(people, lc, monkeypatch):
    """The door authorizes on the role its token read returned; if the row says
    otherwise by the time the snapshot is read, the connection must not be
    recorded as if it had been opened at the row's role."""
    docker = _Docker()
    decode = _wire_agent_door(monkeypatch, docker, role="admin")  # the row says creator

    async def scenario():
        browser = _Browser()
        _, task = _start_agent_terminal(browser, decode)
        await _until(browser.started, "the session to start")
        await lc.registry.revalidate()
        await _finish(task)
        assert browser.closed == (4003, "Access changed")

    asyncio.run(scenario())


def test_a_change_after_the_start_but_before_forwarding_still_ends_the_session(people, lc, monkeypatch):
    """A close that lands while the door reports the start (after go_live,
    before the forwarders run) finds no forwarder to cancel: the door must
    then not start them, and the session ends as any access close does."""
    docker = _Docker()
    decode = _wire_agent_door(monkeypatch, docker)

    class _ClosedWhileStarting(_Browser):
        async def send_text(self, text):
            await super().send_text(text)
            if json.loads(text).get("type") == "auth_success":
                _run_sql("UPDATE users SET role = 'operator' WHERE username = :u", u=ALICE)
                lc.notify_account_changed(ALICE)
                await _settle(lc.registry)

    async def scenario():
        browser = _ClosedWhileStarting()
        manager, task = _start_agent_terminal(browser, decode)
        await _finish(task)
        assert browser.closed == (4003, "Access changed")
        assert docker.peer_closed()
        assert manager._active_sessions == {}
        assert lc.registry.count() == 0

    asyncio.run(scenario())
    assert len(docker.ended) == 1


def test_a_failed_end_of_shell_is_logged_and_the_session_still_ends(people, lc, monkeypatch, caplog):
    docker = _Docker(end_result={"exit_code": 1, "output": "still running: 42", "timed_out": False})
    decode = _wire_agent_door(monkeypatch, docker)
    caplog.set_level(logging.WARNING)

    async def scenario():
        browser = _Browser()
        manager, task = _start_agent_terminal(browser, decode)
        await _until(browser.started, "the session to start")
        _run_sql("UPDATE users SET role = 'operator' WHERE username = :u", u=ALICE)
        lc.notify_account_changed(ALICE)
        await _finish(task)
        assert manager._active_sessions == {}
        assert lc.registry.count() == 0

    asyncio.run(scenario())
    assert any("still running: 42" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


# --------------------------------------------------------------------------- #
# The system-agent terminal
# --------------------------------------------------------------------------- #

def test_the_system_agent_terminal_closes_when_the_account_changes(people, lc, monkeypatch, caplog):
    """T3.8b: the same close for the admin-only door."""
    docker = _Docker()
    sessions = _wire_system_door(monkeypatch, docker)
    caplog.set_level(logging.DEBUG)

    async def scenario():
        browser = _Browser()
        task = asyncio.ensure_future(system_agent.system_agent_terminal(browser, mode="bash"))
        await _until(browser.started, "the session to start")
        assert lc.registry.count() == 1
        assert ADMIN_EMAIL in sessions

        _run_sql("UPDATE users SET role = 'creator' WHERE username = :u", u=ADMIN)
        lc.notify_account_changed(ADMIN)
        await _finish(task)

        assert browser.closed == (4003, "Access changed")
        assert docker.peer_closed()
        assert ADMIN_EMAIL not in sessions
        assert lc.registry.count() == 0

    asyncio.run(scenario())
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert len(docker.ended) == 1
    assert docker.ended[0]["command"][-1] == docker.marker()


def test_an_account_with_no_email_keeps_its_terminal_across_a_recheck(people, lc, monkeypatch):
    """The token read returns ``email: None`` for an account with no email
    (as the built-in admin is provisioned). Its snapshot must equal the
    re-check's view of the same unchanged row, or the terminal would close at
    every re-check."""
    docker = _Docker()
    _wire_system_door(monkeypatch, docker)
    _run_sql("UPDATE users SET email = NULL WHERE username = :u", u=ADMIN)
    monkeypatch.setitem(system_agent.system_agent_terminal.__globals__, "decode_token",
                        lambda _t: {"sub": ADMIN, "email": None, "role": "admin"})

    async def scenario():
        browser = _Browser()
        task = asyncio.ensure_future(system_agent.system_agent_terminal(browser, mode="bash"))
        await _until(browser.started, "the session to start")
        await lc.registry.revalidate()
        lc.notify_account_changed(ADMIN)
        await _settle(lc.registry)
        await asyncio.sleep(0.05)
        assert not task.done()
        assert browser.closed is None
        await browser.inbox.put({"type": "websocket.disconnect"})
        docker.peer.close()
        await _finish(task)

    asyncio.run(scenario())
    assert docker.ended == []


def test_a_change_while_the_system_agent_shell_starts_refuses_the_session(people, lc, monkeypatch):
    holder = {}

    async def change_during_start():
        _run_sql("UPDATE users SET suspended_at = '2026-11-08T00:00:00Z' WHERE username = :u", u=ADMIN)
        holder["lc"].notify_account_changed(ADMIN)
        await _settle(holder["lc"].registry)

    docker = _Docker(on_start=change_during_start)
    holder["lc"] = lc
    sessions = _wire_system_door(monkeypatch, docker)

    async def scenario():
        browser = _Browser()
        task = asyncio.ensure_future(system_agent.system_agent_terminal(browser, mode="bash"))
        await _finish(task)
        assert not browser.started()
        assert browser.closed == (4003, "Access ended")
        assert docker.peer_closed()
        assert ADMIN_EMAIL not in sessions
        assert lc.registry.count() == 0

    asyncio.run(scenario())
    assert len(docker.ended) == 1


# --------------------------------------------------------------------------- #
# Shape: both doors register in the step that authorizes them
# --------------------------------------------------------------------------- #

def _function(path: Path, name: str) -> ast.AST:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {path}")


def _calls(fn: ast.AST, name: str):
    found = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            ident = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
            if ident == name:
                found.append(node.lineno)
    return sorted(found)


def _refusal_blocks(fn: ast.AST):
    """Line spans of `if` bodies that end in `return` (a door's refusals)."""
    spans = []
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and node.body and isinstance(node.body[-1], ast.Return):
            spans.append((node.body[0].lineno, node.body[-1].lineno))
    return spans


@pytest.mark.parametrize("path,handler,decoder", [
    (_BACKEND / "services" / "agent_service" / "terminal.py", "handle_terminal_session", "decode_token_fn"),
    (_BACKEND / "routers" / "system_agent.py", "system_agent_terminal", "decode_token"),
])
def test_each_terminal_door_registers_where_it_authorizes_and_goes_live_after_the_start(path, handler, decoder):
    fn = _function(path, handler)
    [read] = _calls(fn, decoder)
    [admit] = _calls(fn, "TerminalAccess")
    [start] = _calls(fn, "api_exec_start")
    [go_live] = _calls(fn, "go_live")
    [success] = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Constant) and n.value == "auth_success"]
    [forward] = _calls(fn, "forward")
    assert read < admit < start < go_live < success < forward
    refusals = _refusal_blocks(fn)
    between = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Await) and read < n.lineno < admit]
    stray = [ln for ln in between if not any(lo <= ln <= hi for lo, hi in refusals)]
    assert stray == [], f"an await between the account read and the registration: lines {stray}"


# --------------------------------------------------------------------------- #
# The end-of-shell script, executed (needs /proc: Linux)
# --------------------------------------------------------------------------- #

_HAS_PROC = Path("/proc/self/environ").exists() and Path("/proc/self/stat").exists()


def _alive(proc: subprocess.Popen) -> bool:
    return proc.poll() is None


def _stat_fields(pid: int):
    """``/proc/<pid>/stat`` after the command name: [state, ppid, pgrp, session, ...]."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return raw.rsplit(")", 1)[1].split()


def _gone(pid: int) -> bool:
    fields = _stat_fields(pid)
    return fields is None or fields[0] == "Z"


def _session_members(sid: int):
    members = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            fields = _stat_fields(int(entry.name))
            if fields is not None and fields[0] != "Z" and int(fields[3]) == sid:
                members.append(int(entry.name))
    return members


@pytest.mark.skipif(not _HAS_PROC, reason="the script reads /proc (Linux only)")
def test_the_end_of_shell_script_ends_the_marked_session_and_nothing_else():
    marker, other = uuid.uuid4().hex, uuid.uuid4().hex
    base = {k: v for k, v in os.environ.items() if k != terminal.TERMINAL_SESSION_ENV}

    def spawn(env, *, session):
        return subprocess.Popen(["sleep", "120"], env=env, start_new_session=session)

    # A marked session leader (the shell), and in its session a child whose
    # environment no longer carries the marker and one that also ignores HUP
    # (so it outlives the leader and is found only by the leader's session);
    # a marked process in a session of its own; a process of another terminal
    # session; an unmarked bystander.
    unmark = "env -u " + terminal.TERMINAL_SESSION_ENV
    leader = subprocess.Popen(
        ["sh", "-c", unmark + " sleep 120 & " + unmark + " nohup sleep 120 >/dev/null 2>&1 & exec sleep 120"],
        env={**base, terminal.TERMINAL_SESSION_ENV: marker}, start_new_session=True,
    )
    detached = spawn({**base, terminal.TERMINAL_SESSION_ENV: marker}, session=True)
    neighbour = spawn({**base, terminal.TERMINAL_SESSION_ENV: other}, session=True)
    bystander = spawn(base, session=False)
    time.sleep(0.3)
    child_pids = [p for p in _session_members(leader.pid) if p != leader.pid]
    assert len(child_pids) == 2, "the leader's two unmarked children are in its session"
    try:
        out = subprocess.run(
            ["/bin/sh", "-c", terminal._END_SHELL_SCRIPT, "sh", marker],
            env=base, capture_output=True, text=True, timeout=20,
        )
        assert out.returncode == 0, out.stdout + out.stderr
        time.sleep(0.2)
        leader.wait(timeout=5)
        detached.wait(timeout=5)
        assert all(_gone(pid) for pid in child_pids)
        assert _alive(neighbour)
        assert _alive(bystander)
    finally:
        for proc in (leader, detached, neighbour, bystander):
            if _alive(proc):
                proc.send_signal(signal.SIGKILL)
                proc.wait(timeout=5)
        for pid in child_pids:
            if not _gone(pid):
                os.kill(pid, signal.SIGKILL)
