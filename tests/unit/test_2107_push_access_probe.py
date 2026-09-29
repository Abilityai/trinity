"""
#2107 — git-sync write access is validated by asking the side that enforces it.

`git ls-remote` (upload-pack) and the REST `permissions.push` field both said
"fine" for a Contents: read-only fine-grained PAT, and the agent then failed
64 auto-syncs in a row. `probe_push_access` runs a real `git push --dry-run`
of a throwaway ref, so these tests drive REAL git against a local fake
smart-HTTP server that answers the receive-pack advertisement the way GitHub
does:
  - 403 + `Write access to repository not granted.` → "denied", GitHub's line
    surfaced verbatim
  - a valid receive-pack advertisement → "ok", and nothing is POSTed (dry run)
  - 5xx / unreachable / unrecognised → "transient"
  - the token reaches the server as an Authorization header and is never on
    the git argv
And `is_push_denied` over the error text the agent actually records.
"""
from __future__ import annotations

import asyncio
import base64
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)

_BACKEND = str(Path(__file__).resolve().parents[2] / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

PAT = "github_pat_example_0000000000"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture()
def gs(monkeypatch):
    """The real `services.git_service` package over mocked I/O neighbours."""
    mocks = {
        "redis": MagicMock(),
        "redis.asyncio": MagicMock(),
        "database": MagicMock(),
        "services.agent_auth": MagicMock(),
        "services.docker_service": MagicMock(),
    }
    patcher = patch.dict("sys.modules", mocks)
    patcher.start()
    for key in list(sys.modules):
        if (key == "services" or key.startswith("services.")) and key not in mocks:
            monkeypatch.delitem(sys.modules, key, raising=False)
    import services.git_service as git_service
    try:
        yield git_service
    finally:
        patcher.stop()


def _pkt(line: str) -> bytes:
    data = line.encode()
    return f"{len(data) + 4:04x}".encode() + data


# An empty repository's receive-pack advertisement (protocol v0).
_ADVERTISEMENT = (
    _pkt("# service=git-receive-pack\n") + b"0000"
    + _pkt("0" * 40 + " capabilities^{}\0report-status delete-refs ofs-delta\n")
    + b"0000"
)


class _FakeGitHub:
    """Answers `GET /<repo>.git/info/refs?service=git-receive-pack` with a
    scripted (status, content-type, body) and records what it was sent."""

    def __init__(self, status, body, content_type="text/plain"):
        self.status, self.body, self.content_type = status, body, content_type
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self):
                outer.requests.append((self.command, self.path, dict(self.headers)))
                if self.command == "POST":
                    self.send_response(500)
                    self.end_headers()
                    return
                self.send_response(outer.status)
                self.send_header("Content-Type", outer.content_type)
                self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
                self.wfile.write(outer.body)

            do_GET = _answer
            do_POST = _answer

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"


def _probe(gs, monkeypatch, server_or_base):
    base = server_or_base if isinstance(server_or_base, str) else server_or_base.base
    monkeypatch.setenv("TRINITY_GIT_BASE_URL", base)
    return _run(gs.probe_push_access("o/r", PAT))


class TestRealTransport:
    def test_a_read_only_token_is_denied_with_githubs_own_reason(self, gs, monkeypatch):
        with _FakeGitHub(403, b"Write access to repository not granted.\n") as fake:
            outcome, detail = _probe(gs, monkeypatch, fake)
        assert outcome == "denied"
        assert detail == "remote: Write access to repository not granted."
        method, path, _ = fake.requests[0]
        assert (method, path) == ("GET", "/o/r.git/info/refs?service=git-receive-pack")

    def test_a_writable_token_is_ok_and_nothing_is_pushed(self, gs, monkeypatch):
        with _FakeGitHub(200, _ADVERTISEMENT,
                         "application/x-git-receive-pack-advertisement") as fake:
            outcome, detail = _probe(gs, monkeypatch, fake)
        assert (outcome, detail) == ("ok", "")
        assert [m for m, _, _ in fake.requests] == ["GET"], "a dry run must not POST a pack"

    def test_the_token_is_a_header_never_an_argument(self, gs, monkeypatch):
        seen_argv = []
        real_exec = asyncio.create_subprocess_exec

        async def spy(*args, **kwargs):
            seen_argv.append(args)
            return await real_exec(*args, **kwargs)

        monkeypatch.setattr(gs.provisioning.asyncio, "create_subprocess_exec", spy)
        with _FakeGitHub(403, b"Write access to repository not granted.\n") as fake:
            _probe(gs, monkeypatch, fake)
        expected = "basic " + base64.b64encode(f"x-access-token:{PAT}".encode()).decode()
        headers = {k.lower(): v for k, v in fake.requests[0][2].items()}
        assert headers["authorization"].lower() == expected.lower()
        assert seen_argv and all(PAT not in " ".join(a) for a in seen_argv)
        push = [a for a in seen_argv if "push" in a][0]
        assert "--dry-run" in push and "HEAD:refs/heads/__trinity_write_probe" in push

    def test_a_server_error_is_transient(self, gs, monkeypatch):
        with _FakeGitHub(502, b"bad gateway\n") as fake:
            outcome, _ = _probe(gs, monkeypatch, fake)
        assert outcome == "transient"

    def test_unreachable_is_transient(self, gs, monkeypatch):
        with _FakeGitHub(200, b"") as fake:
            base = fake.base  # a port that is about to be closed
        outcome, _ = _probe(gs, monkeypatch, base)
        assert outcome == "transient"


class TestClassifier:
    @pytest.mark.parametrize("text", [
        "remote: Write access to repository not granted.",
        "remote: Permission to o/r.git denied to someone.",
        "fatal: unable to access 'https://github.com/o/r.git/': The requested URL returned error: 403",
        "fatal: Authentication failed for 'https://github.com/o/r.git/'",
        "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
        "remote: Repository not found.",
    ])
    def test_refusals(self, gs, text):
        assert gs.is_push_denied(text) is True

    @pytest.mark.parametrize("text", [
        "! [rejected] main -> main (non-fast-forward)",
        "fatal: unable to access '...': Could not resolve host: github.com",
        "diverged: rebase conflict on main",
        "error: permission to write the index file",  # local fs, not GitHub
        "",
        None,
    ])
    def test_not_refusals(self, gs, text):
        assert gs.is_push_denied(text) is False
