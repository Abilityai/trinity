"""#2582 — reading back and deleting a Workspace client's own uploads.

A client upload has **no DB row**. It is a file in a container directory with no
id, no URL and no stored MIME, which is why listing one needs a docker exec,
reading one back needs `extract_from_agent`, deleting one needs `rm`, and the
type has to be guessed. Everything pinned here follows from that fact.

The two tests that matter most are not the happy paths:

  * **the traversal answer is a UNIFORM 404**, asserted at the handler AND
    through the mounted route. A handler-only assertion cannot tell "gated" from
    "unroutable" — uvicorn and httpx both apply RFC 3986 dot-segment removal
    before Starlette matches, so a literal `../` test can pass while exercising
    nothing;
  * **the gate order** — off-roster 404 happens before any docker work, so a
    caller cannot mint unbounded limiter keys or make the platform do work for
    an agent they cannot reach (the ent#287 rule, extended to two more routes).
"""
from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.unit


class _Res:
    def __init__(self, exit_code=0, output=b""):
        self.exit_code = exit_code
        self.output = output


class _Container:
    def __init__(self, status="running"):
        self.status = status


@pytest.fixture()
def portal(tmp_path, monkeypatch):
    """Router + service with Redis off, a stubbed roster, and stubbed docker.

    Patching rule (see `test_ent79_portal_exposure.py`'s module docstring): the
    service reaches docker through function-local `from services.x import y`,
    which resolves `sys.modules["services.x"]` — so the patch target is the
    module object, obtained here the same way that file obtains it.
    """
    monkeypatch.setenv("TRINITY_DB_PATH", str(tmp_path / "t.db"))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(tmp_path / "t.db"))

    import importlib
    import sys

    from services import rate_limiter
    from client_portal import router as portal_router
    from client_portal import service

    monkeypatch.setattr(rate_limiter, "_get_redis", lambda: None)
    rate_limiter.clear_inprocess()

    monkeypatch.setattr(
        service, "agent_on_roster",
        lambda agent_name, email, include_owned=False: not agent_name.startswith("off-"),
    )

    calls: dict = {"exec": [], "extract": [], "audit": []}
    state = {"container": _Container(), "exec_exit": 0, "extract": (b"hello", "report.pdf")}

    importlib.import_module("services.docker_service")
    docker_service = sys.modules["services.docker_service"]
    importlib.import_module("services.docker_utils")
    docker_utils = sys.modules["services.docker_utils"]
    importlib.import_module("services.agent_shared_files_service")
    shared_files = sys.modules["services.agent_shared_files_service"]

    monkeypatch.setattr(docker_service, "get_agent_container", lambda name: state["container"])

    async def _exec(container, cmd, user=None, **kw):
        calls["exec"].append((cmd, user))
        return _Res(exit_code=state["exec_exit"])

    async def _extract(agent_name, path):
        calls["extract"].append((agent_name, path))
        return state["extract"]

    monkeypatch.setattr(docker_utils, "container_exec_run", _exec)
    monkeypatch.setattr(shared_files, "extract_from_agent", _extract)

    async def _audit(**kw):
        calls["audit"].append(kw)
        return "evt"

    monkeypatch.setattr(portal_router.platform_audit_service, "log", _audit)

    try:
        yield portal_router, service, calls, state
    finally:
        rate_limiter.clear_inprocess()


def _principal(email="bob@example.com", is_platform=False):
    from client_portal.portal_auth import PortalPrincipal
    return PortalPrincipal(email=email, is_platform=is_platform)


class _Request:
    """The two attributes the audit helper reads off a request."""

    def __init__(self, path="/api/enterprise/client-portal/x"):
        self.url = type("U", (), {"path": path})()
        self.client = type("C", (), {"host": "10.0.0.1"})()
        self.state = type("S", (), {})()


def _download(router, agent="atlas", filename="report.pdf", email="bob@example.com"):
    return asyncio.run(router.portal_download_upload(
        agent, filename, _Request(), principal=_principal(email)))


def _delete(router, agent="atlas", filename="report.pdf", email="bob@example.com"):
    return asyncio.run(router.portal_delete_upload(
        agent, filename, _Request(), principal=_principal(email)))


def _status_of(fn, *a, **kw):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        fn(*a, **kw)
    return exc.value


# --------------------------------------------------------------------------- #
# mime_type reaches the client
# --------------------------------------------------------------------------- #

def test_read_inbox_guesses_a_mime_type_and_the_model_declares_it():
    """A live bug, not only a new field. `PortalRailFiles.vue` has always
    rendered `<FileIcon :mime="u.mime_type">`, but `PortalUploadItem` did not
    declare the field — and the route's `response_model` strips undeclared keys
    silently, so the icon has been unconditionally generic since it shipped."""
    from client_portal.models import PortalUploadItem

    assert "mime_type" in PortalUploadItem.model_fields
    item = PortalUploadItem(filename="a.png", size_bytes=1, mime_type="image/png")
    assert item.model_dump()["mime_type"] == "image/png"


def test_read_inbox_populates_mime_from_the_extension(portal, monkeypatch):
    import json
    _router, service, _calls, _state = portal

    import importlib
    import sys
    importlib.import_module("services.docker_utils")
    docker_utils = sys.modules["services.docker_utils"]

    listing = [
        {"filename": "chart.png", "size_bytes": 10, "mtime": 1_700_000_000},
        {"filename": "notes.md", "size_bytes": 20, "mtime": 1_700_000_000},
        {"filename": "blob", "size_bytes": 30, "mtime": 1_700_000_000},
    ]

    async def _exec(container, cmd, user=None, **kw):
        return _Res(output=json.dumps(listing).encode())

    monkeypatch.setattr(docker_utils, "container_exec_run", _exec)
    rows = asyncio.run(service._read_inbox("atlas", "bob@example.com"))
    by_name = {r["filename"]: r for r in rows}
    assert by_name["chart.png"]["mime_type"] == "image/png"
    assert by_name["notes.md"]["mime_type"] in ("text/markdown", "text/x-markdown")
    assert by_name["blob"]["mime_type"] is None, "an unguessable name is None, never a lie"


# --------------------------------------------------------------------------- #
# Gate order and the uniform 404
# --------------------------------------------------------------------------- #

def test_an_off_roster_agent_404s_before_any_docker_work(portal):
    router, _service, calls, _state = portal
    exc = _status_of(_download, router, agent="off-limits")
    assert exc.status_code == 404
    assert exc.detail == "Agent not found"
    assert calls["extract"] == [] and calls["exec"] == []


@pytest.mark.parametrize("bad", ["../x", "..", "a/b", "..\\x", "  ", "", "." * 3])
def test_an_unaddressable_filename_is_the_same_uniform_404(portal, bad):
    """404, never 400. A distinguishable refusal for a traversal attempt tells
    the caller their guess was structurally interesting, which is the
    differential OSS invariant #8 exists to close."""
    router, _service, calls, _state = portal
    exc = _status_of(_download, router, filename=bad)
    assert exc.status_code == 404
    assert exc.detail == "File not found"
    assert calls["extract"] == []


def test_the_traversal_answer_holds_through_the_mounted_route(portal):
    """The half a handler-only test cannot prove — and it proves TWO things,
    because on this route the wire does part of the work.

    httpx and uvicorn both apply RFC 3986 dot-segment removal, and Starlette
    matches `{filename}` against the RAW segment — so `%2F` shapes never match
    the route at all and answer Starlette's own 404, while `%2E%2E` DOES reach
    the handler and answers the service's uniform 404. Both are 404 and neither
    leaks a path, which is the security property; but a test that asserted only
    the status could not tell "gated" from "unroutable", so the two are
    separated here, and a positive control proves the route is mounted at all.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    router, _service, _calls, _state = portal
    from client_portal.portal_auth import get_portal_principal

    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[get_portal_principal] = lambda: _principal()
    client = TestClient(app)

    base = "/api/enterprise/client-portal/agents/atlas/uploads"

    # Positive control FIRST: without it, every assertion below would also pass
    # against a route that was never mounted.
    ok = client.get(f"{base}/report.pdf")
    assert ok.status_code == 200, "the route must be reachable for the 404s to mean anything"

    # Reaches the handler; the service's guard is what refuses it.
    gated = client.get(f"{base}/%2E%2E")
    assert gated.status_code == 404
    assert gated.json()["detail"] == "File not found"

    # Never matches the route; refused one layer earlier. Still 404, still no path.
    for suffix in ["%2E%2E%2Fescape", "..%2Fescape", "%2Fetc%2Fpasswd", "%2e%2e%2fx"]:
        res = client.get(f"{base}/{suffix}")
        assert res.status_code == 404, f"{suffix} -> {res.status_code}"
        assert "/home/developer" not in res.text, suffix


def test_a_legitimate_filename_reaches_the_expected_container_path(portal):
    """The positive control for the guard above: without it, a passing traversal
    test would be indistinguishable from a route that refuses everything."""
    router, _service, calls, _state = portal
    _download(router, filename="report (1).pdf")
    agent, path = calls["extract"][0]
    assert agent == "atlas"
    assert path.startswith("/home/developer/inbox/")
    assert path.endswith("/report (1).pdf")


def test_two_emails_that_slug_alike_read_different_inboxes(portal):
    """ent#308's collision, restated on the read side. The inbox directory is
    the ONLY thing separating one client's files from another's, so a route that
    reads files back must inherit the injective mapping, not re-derive one."""
    router, _service, calls, _state = portal
    _download(router, email="victim+x@example.com")
    _download(router, email="victim_x@example.com")
    assert calls["extract"][0][1] != calls["extract"][1][1]


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #

def test_download_serves_attachment_and_never_caches(portal):
    router, _service, _calls, _state = portal
    res = _download(router)
    assert res.headers["Content-Disposition"].startswith("attachment;")
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["Cache-Control"] == "private, no-store"
    assert res.body == b"hello"


def test_download_refuses_a_stopped_agent_with_409_and_a_missing_one_with_502(portal):
    """Deliberate, not incidental: `get_archive` could read a stopped
    container's filesystem, but `_read_inbox` returns [] for one — so the list
    the client is looking at is empty anyway, and a download that worked from a
    surface showing nothing would be the odder behaviour."""
    router, _service, calls, state = portal

    state["container"] = _Container(status="exited")
    assert _status_of(_download, router).status_code == 409

    state["container"] = None
    assert _status_of(_download, router).status_code == 502
    assert calls["extract"] == []


@pytest.mark.parametrize("raised,expected", [(404, 404), (400, 404), (413, 413), (500, 502)])
def test_extract_exceptions_are_translated_not_forwarded(portal, monkeypatch, raised, expected):
    """`extract_from_agent`'s own 404 detail echoes the CONTAINER PATH, which
    this external surface must not disclose."""
    import importlib
    import sys
    from fastapi import HTTPException

    router, _service, _calls, _state = portal
    importlib.import_module("services.agent_shared_files_service")
    shared_files = sys.modules["services.agent_shared_files_service"]

    async def _boom(agent_name, path):
        raise HTTPException(status_code=raised, detail=f"FILE_NOT_FOUND: {path}")

    monkeypatch.setattr(shared_files, "extract_from_agent", _boom)
    exc = _status_of(_download, router)
    assert exc.status_code == expected
    assert "/home/developer" not in str(exc.detail)


def test_download_is_audited_with_the_principal_as_actor(portal):
    """`actor_email`, not `actor_user`: a PortalPrincipal is not a User and has
    no row to resolve — this is the field built for exactly that case (#848)."""
    router, _service, calls, _state = portal
    _download(router)
    entry = calls["audit"][-1]
    assert entry["event_action"] == "portal_upload_download"
    assert entry["actor_email"] == "bob@example.com"
    assert entry["target_id"] == "atlas"


# --------------------------------------------------------------------------- #
# Delete
# --------------------------------------------------------------------------- #

def test_delete_uses_rm_f_with_a_terminator_and_the_developer_user(portal):
    """Two defences, and both are needed for a reason the other does not cover.

    `--` because `_safe_filename` admits a LEADING HYPHEN (`.strip(". ")` does
    not strip one), so a name like `-rf.txt` is a plausible upload. Today the
    argument is an absolute path and therefore never looks like a flag — which
    is exactly why the terminator has to be pinned rather than left to that
    accident: a later change that passes a bare name would silently hand `rm` a
    flag.

    `shlex.quote` because a filename may contain spaces, parens and quotes,
    which `_safe_filename` also admits. Asserted with a name that actually needs
    quoting, so the check cannot pass vacuously.
    """
    import shlex

    router, _service, calls, _state = portal

    _delete(router, filename="-rf.txt")
    cmd, user = calls["exec"][-1]
    assert cmd.startswith("rm -f -- "), cmd
    assert user == "developer"
    # One shell word after the terminator — the path, and nothing else.
    assert len(shlex.split(cmd)) == 4, cmd

    _delete(router, filename="my report (1).txt")
    cmd, _user = calls["exec"][-1]
    assert len(shlex.split(cmd)) == 4, "a name with spaces must still be ONE argument"
    assert shlex.split(cmd)[-1].endswith("/my report (1).txt")


def test_delete_is_idempotent(portal):
    """`rm -f` on a missing file exits 0, and a client who clicks Delete twice
    has got what they asked for both times."""
    router, _service, _calls, _state = portal
    _delete(router)
    _delete(router)


def test_a_failing_rm_is_a_named_502_not_a_silent_success(portal):
    router, _service, _calls, state = portal
    state["exec_exit"] = 1
    exc = _status_of(_delete, router)
    assert exc.status_code == 502
    assert "delete" in str(exc.detail).lower()


def test_delete_is_audited(portal):
    router, _service, calls, _state = portal
    _delete(router)
    assert calls["audit"][-1]["event_action"] == "portal_upload_delete"


# --------------------------------------------------------------------------- #
# The two limiter tiers
# --------------------------------------------------------------------------- #

def test_the_burst_tier_rejects_with_a_retryable_429(portal):
    router, _service, calls, _state = portal
    limit = router.PORTAL_FILE_BURST_LIMIT
    for _ in range(limit):
        _download(router)
    assert len(calls["extract"]) == limit

    exc = _status_of(_download, router)
    assert exc.status_code == 429
    assert int(exc.headers["Retry-After"]) > 0
    assert len(calls["extract"]) == limit, "a rejected read must never reach docker"


def test_the_hourly_tier_is_the_one_that_actually_bounds_it(portal, monkeypatch):
    """A burst tier alone permits 20/min = 1200/hour, which is not a budget bound
    at all — `router.py`'s own comment says so, and it is why this route ships
    two tiers rather than the single one three reviewers flagged."""
    router, _service, _calls, _state = portal
    from services import rate_limiter

    assert router.PORTAL_FILE_HOURLY_LIMIT < router.PORTAL_FILE_BURST_LIMIT * 60

    # Spend the hourly allowance without ever tripping the burst window.
    for _ in range(router.PORTAL_FILE_HOURLY_LIMIT):
        rate_limiter.check("portal_file_hourly:bob@example.com",
                           router.PORTAL_FILE_HOURLY_LIMIT, 3600)
    assert _status_of(_download, router).status_code == 429


def test_the_download_limiter_is_tighter_than_upload_and_delete_is_looser(portal):
    """The costs are not equal and neither are the tiers: a `get_archive` is a
    tar of up to 25 MiB through the global 4-worker pool, an `rm` is one cheap
    exec."""
    router, _service, _calls, _state = portal
    assert router.PORTAL_FILE_BURST_LIMIT <= router.PORTAL_UPLOAD_BURST_LIMIT
    assert router.PORTAL_FILE_DELETE_BURST_LIMIT > router.PORTAL_FILE_BURST_LIMIT


def test_delete_and_download_do_not_share_a_counter(portal):
    """Metering them together would price the cheap verb at the expensive one's
    rate — and, worse, let a burst of deletes lock the user out of reading their
    own files back."""
    router, _service, _calls, _state = portal
    for _ in range(router.PORTAL_FILE_BURST_LIMIT):
        _download(router)
    _status_of(_download, router)
    _delete(router)     # must still work
