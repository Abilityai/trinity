"""`is_port_available` is a bind-only probe: it never listens, and it closes.

`services.docker_service.is_port_available(port)` opens a TCP socket, binds it
to `0.0.0.0:port`, and closes it when its `with` block exits. It never calls
`listen()` or `accept()`, so nothing can ever connect to that socket; the bind
only asks the kernel whether the port is taken. (It is a weak extra filter for
SSH-port allocation; `test_2215_port_allocation.py` covers the allocator and
always stubs this function out.)

The wildcard address is deliberate: a bind on `0.0.0.0` collides with an
existing bind on ANY local address, which a `127.0.0.1` probe would miss.

Pinned here, on the function's source:

* it constructs exactly one socket, `socket.socket(socket.AF_INET,
  socket.SOCK_STREAM)`, used as a context manager, and names no `listen`,
  `accept`, `create_server`, `create_connection` or low-level `_socket`;

and on the real function and real sockets:

* the exact socket calls are `settimeout`, `bind(("0.0.0.0", port))`, `close`,
  on one AF_INET / SOCK_STREAM socket, on both the free and the busy path;
* after it returns, the socket is closed and nothing accepts a connection;
* a bind held on 127.0.0.1, and one held on another local address, both make it
  report the port busy, while a 127.0.0.1 probe would miss the second.

abilityai/trinity-enterprise#1117
"""
from __future__ import annotations

import ast
import importlib
import socket
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit

_RealSocket = socket.socket


@pytest.fixture
def is_port_available():
    """The real function, imported at test time: other files in the suite stub
    `services.docker_service`, so a collection-time import could bind a stub."""
    module = importlib.import_module("services.docker_service")
    assert Path(getattr(module, "__file__", "")).resolve() == (
        _BACKEND / "services" / "docker_service.py"
    ).resolve(), f"services.docker_service is not the real module: {module!r}"
    return module.is_port_available
_TRACKED = {
    "accept",
    "bind",
    "close",
    "connect",
    "connect_ex",
    "detach",
    "listen",
    "recv",
    "send",
    "sendall",
    "setblocking",
    "setsockopt",
    "settimeout",
}


@pytest.fixture
def recorded(monkeypatch):
    """Replace `socket.socket` (the probe imports `socket` at call time) with a
    subclass that records each socket it creates and every tracked call."""
    sockets: list = []

    class Recorder(_RealSocket):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            object.__setattr__(self, "_calls", [])
            sockets.append(self)

        def __getattribute__(self, name):
            if name in _TRACKED:
                calls = object.__getattribute__(self, "_calls")
                method = super().__getattribute__(name)

                def traced(*args, **kwargs):
                    calls.append((name, args))
                    return method(*args, **kwargs)

                return traced
            return super().__getattribute__(name)

    monkeypatch.setattr(socket, "socket", Recorder)
    return sockets


_SOURCE = _BACKEND / "services" / "docker_service.py"
_FORBIDDEN_NAMES = {"listen", "accept", "create_server", "create_connection", "fromfd", "socketpair", "_socket"}


def _probe_source() -> ast.FunctionDef:
    tree = ast.parse(_SOURCE.read_text())
    found = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "is_port_available"]
    assert len(found) == 1, f"is_port_available not found exactly once in {_SOURCE}"
    return found[0]


def test_probe_source_builds_one_stream_socket_and_names_no_listener():
    fn = _probe_source()
    names = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(a.name for a in node.names)
            if isinstance(node, ast.ImportFrom):
                names.add(node.module or "")
    assert not names & _FORBIDDEN_NAMES, sorted(names & _FORBIDDEN_NAMES)
    assert "__import__" not in names and "importlib" not in names, sorted(names)
    constructors = [
        ast.unparse(n)
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and ast.unparse(n.func).split(".")[-1] == "socket"
    ]
    assert constructors == ["socket.socket(socket.AF_INET, socket.SOCK_STREAM)"], constructors
    withs = [n for n in ast.walk(fn) if isinstance(n, ast.With)]
    assert [ast.unparse(item.context_expr) for w in withs for item in w.items] == constructors


def _free_port() -> int:
    with _RealSocket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _probe_calls(sockets, port):
    assert len(sockets) == 1, f"expected one probe socket, got {len(sockets)}"
    (probe,) = sockets
    assert (probe.family, probe.type) == (socket.AF_INET, socket.SOCK_STREAM)
    assert probe.fileno() == -1, "the probe socket was left open"
    return [(name, args) for name, args in probe._calls]


def test_free_port_is_probed_by_bind_then_close_only(recorded, is_port_available):
    port = _free_port()
    assert is_port_available(port) is True
    assert _probe_calls(recorded, port) == [
        ("settimeout", (1,)),
        ("bind", (("0.0.0.0", port),)),
        ("close", ()),
    ]


def test_busy_port_is_probed_the_same_way_and_still_closed(recorded, is_port_available):
    with _RealSocket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        port = held.getsockname()[1]
        assert is_port_available(port) is False
    assert _probe_calls(recorded, port) == [
        ("settimeout", (1,)),
        ("bind", (("0.0.0.0", port),)),
        ("close", ()),
    ]


def test_nothing_accepts_connections_after_the_probe(is_port_available):
    port = _free_port()
    assert is_port_available(port) is True
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()


def _other_local_ipv4() -> str:
    """A local IPv4 address other than 127.0.0.1 that a socket can bind.

    Linux routes all of 127.0.0.0/8 to loopback, so 127.0.0.2 always works
    there; elsewhere use the address of the default route. No address is a
    failure, not a skip: the wildcard property must be shown, not assumed.
    """
    with _RealSocket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.2", 0))
            return "127.0.0.2"
        except OSError:
            pass
    with _RealSocket(socket.AF_INET, socket.SOCK_DGRAM) as u:
        try:
            u.connect(("192.0.2.1", 9))  # TEST-NET-1; a UDP connect sends nothing
            ip = u.getsockname()[0]
        except OSError as exc:
            pytest.fail(f"no second local IPv4 address to bind on this host: {exc}")
    if ip.startswith("127.") or ip == "0.0.0.0":
        pytest.fail(f"no second local IPv4 address to bind on this host (got {ip})")
    return ip


def test_wildcard_probe_sees_a_bind_on_another_local_address(is_port_available):
    other = _other_local_ipv4()
    with _RealSocket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind((other, 0))
        port = held.getsockname()[1]
        assert is_port_available(port) is False
        # The reason the probe binds the wildcard: a loopback-only probe would
        # have called this port free.
        with _RealSocket(socket.AF_INET, socket.SOCK_STREAM) as loopback:
            loopback.bind(("127.0.0.1", port))
