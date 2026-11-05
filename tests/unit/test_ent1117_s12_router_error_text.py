"""#4284 — a 5xx error body must not carry the caught exception's text.

These routers raised `HTTPException(5xx, detail=f"...: {str(e)}")`, so the
caller received whatever the exception carried: a socket path, an internal
host, an SQL fragment, or — on the git routes — a GitHub PAT echoed by an
httpx header-validation error or embedded in a remote URL. CodeQL's
`py/stack-trace-exposure` does not flag the `HTTPException(detail=...)` form,
so none of these sites appeared on the Security tab.

The #1917 pattern applies: the full exception goes to the backend log
(`exc_info`), and the response keeps its status, its message prefix, and the
exception CLASS NAME. Each test drives the real handler with a planted sentinel
in the exception message and asserts three things: the sentinel is absent from
the response, the class name is present (a response that dropped the error
entirely would pass a sentinel-only check while making the failure
undiagnosable), and the sentinel IS in the log (the detail moved, it was not
lost). On the git routes the log must additionally be scrubbed of the PAT and
of any URL userinfo, because moving the text from the body into the
Vector-captured log must not move the credential with it.

`routers/skills.py` has no 5xx site that renders an exception (its
`str(e)` details are deliberate 4xx domain messages), so it has no test here.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException

# A string no platform-authored message would ever contain.
SENTINEL = "sekrit-internal-host-10.0.0.7:5432"


class PlantedError(RuntimeError):
    """A distinctive class name, so the class-name assertion can't pass by luck."""


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _admin():
    caller = MagicMock()
    caller.agent_name = None
    caller.connector_agent = None
    caller.mcp_scope = None
    caller.role = "admin"
    caller.username = "admin"
    caller.email = "admin@example.com"
    return caller


def _request():
    req = MagicMock()
    req.client.host = "127.0.0.1"
    req.scope = {"path": "/test"}
    req.state.request_id = None
    return req


def _live(name: str):
    """The module a handler's function-level `from X import y` will read.

    That import resolves through `sys.modules`, whereas `import a.b as m` binds
    the package ATTRIBUTE; once another test has re-imported a module the two
    can differ, and a patch on the attribute copy silently misses.
    """
    return importlib.import_module(name)


def _raise_http(coro) -> HTTPException:
    with pytest.raises(HTTPException) as info:
        _run(coro)
    return info.value


def _log_text(caplog) -> str:
    """Every captured record as the handler would render it, traceback included."""
    fmt = logging.Formatter("%(message)s")
    return "\n".join(fmt.format(r) for r in caplog.records)


def _assert_scrubbed_5xx(exc: HTTPException, status: int, prefix: str, cls: str):
    blob = json.dumps(exc.detail, default=str)
    assert exc.status_code == status
    assert SENTINEL not in blob, f"exception text reached the response: {blob}"
    assert cls in blob, f"class name missing from the response: {blob}"
    assert prefix in blob, f"message prefix changed: {blob}"


def _assert_logged(caplog):
    assert SENTINEL in _log_text(
        caplog
    ), "the exception detail was lost, not moved to the log"


# ---------------------------------------------------------------------------
# routers/systems.py
# ---------------------------------------------------------------------------


def test_systems_get_manifest_500(monkeypatch, caplog):
    import routers.systems as mod

    def boom(_):
        raise PlantedError(SENTINEL)

    monkeypatch.setattr(mod, "read_bundled_manifest", boom)
    caplog.set_level(logging.DEBUG)
    exc = _raise_http(mod.get_manifest("some-id", current_user=_admin()))
    _assert_scrubbed_5xx(exc, 500, "Failed to read bundled manifest", "PlantedError")
    _assert_logged(caplog)


# ---------------------------------------------------------------------------
# routers/settings/generic.py
# ---------------------------------------------------------------------------


def test_settings_generic_get_all_500(monkeypatch, caplog):
    import routers.settings.generic as mod

    monkeypatch.setattr(mod, "assert_admin", lambda user, **kw: None)
    fake_db = MagicMock()
    fake_db.get_all_settings.side_effect = PlantedError(SENTINEL)
    monkeypatch.setattr(mod, "db", fake_db)
    caplog.set_level(logging.DEBUG)
    exc = _raise_http(mod.get_all_settings(_request(), current_user=_admin()))
    _assert_scrubbed_5xx(exc, 500, "Failed to get settings", "PlantedError")
    _assert_logged(caplog)


# ---------------------------------------------------------------------------
# routers/settings/ops.py
# ---------------------------------------------------------------------------


def test_settings_ops_get_500(monkeypatch, caplog):
    import routers.settings.ops as mod

    monkeypatch.setattr(mod, "assert_admin", lambda user, **kw: None)
    fake_db = MagicMock()
    fake_db.get_settings_dict.side_effect = PlantedError(SENTINEL)
    monkeypatch.setattr(mod, "db", fake_db)
    caplog.set_level(logging.DEBUG)
    exc = _raise_http(mod.get_ops_settings(_request(), current_user=_admin()))
    _assert_scrubbed_5xx(exc, 500, "Failed to get ops settings", "PlantedError")
    _assert_logged(caplog)


# ---------------------------------------------------------------------------
# routers/mcp_keys.py
# ---------------------------------------------------------------------------


def test_mcp_keys_list_500(monkeypatch, caplog):
    import routers.mcp_keys as mod

    fake_db = MagicMock()
    fake_db.list_all_mcp_api_keys.side_effect = PlantedError(SENTINEL)
    monkeypatch.setattr(mod, "db", fake_db)
    caplog.set_level(logging.DEBUG)
    exc = _raise_http(mod.list_mcp_api_keys_endpoint(_request(), current_user=_admin()))
    _assert_scrubbed_5xx(exc, 500, "Failed to list MCP API keys", "PlantedError")
    _assert_logged(caplog)


# ---------------------------------------------------------------------------
# routers/git.py — the exception text can carry a PAT; the LOG must be scrubbed
# ---------------------------------------------------------------------------

# One planted value per redaction pass, so each pass is proven on its own:
# - PAT: the token the handler HOLDS. Deliberately not GitHub-shaped, so only
#   the held-value pass can catch it (the shape pass would mask a broken one).
#   It appears bare and in its `x-access-token:` b64 form.
# - STALE_URL_TOKEN: a different token baked into a remote URL's userinfo,
#   which only the URL pass catches.
# - SHAPED_TOKEN: an unrelated GitHub-shaped token, which only the shape pass
#   catches.
PAT = "planted-held-pat-0123456789"
STALE_URL_TOKEN = "planted-stale-url-token-9876"
SHAPED_TOKEN = "ghp_" + "S" * 36
PAT_B64 = base64.b64encode(f"x-access-token:{PAT}".encode()).decode()


def _pat_bearing_error() -> PlantedError:
    return PlantedError(
        # Bare, with no `key=` or `Bearer` lead-in: the shape pass's key=value
        # and Bearer rules would otherwise mask a broken held-value pass.
        f"{SENTINEL} pushing with {PAT} as {PAT_B64} beside {SHAPED_TOKEN} "
        f"while fetching "
        f"https://x-access-token:{STALE_URL_TOKEN}@github.com/acme/repo.git"
    )


def _assert_no_credential(text: str, where: str):
    for value, label in (
        (PAT, "held PAT"),
        (PAT_B64, "held PAT (b64 form)"),
        (STALE_URL_TOKEN, "URL userinfo token"),
        (SHAPED_TOKEN, "GitHub-shaped token"),
    ):
        assert value not in text, f"{label} reached the {where}: {text}"


def test_git_validate_pat_500_scrubs_log(monkeypatch, caplog):
    import routers.git as mod
    gh_mod = _live("services.github_service")

    class FakeGitHub:
        def __init__(self, pat):
            self.pat = pat

        async def validate_token(self):
            raise _pat_bearing_error()

    monkeypatch.setattr(gh_mod, "GitHubService", FakeGitHub)
    body = MagicMock()
    body.pat = PAT
    caplog.set_level(logging.DEBUG)
    exc = _raise_http(mod.set_agent_github_pat("agent-a", body, _request()))
    _assert_scrubbed_5xx(exc, 500, "Failed to validate PAT", "PlantedError")
    _assert_no_credential(json.dumps(exc.detail, default=str), "response")
    log = _log_text(caplog)
    _assert_logged(caplog)
    _assert_no_credential(log, "log")


def test_git_initialize_500_scrubs_log(monkeypatch, caplog):
    import routers.git as mod
    docker_mod = _live("services.docker_service")
    gh_mod = _live("services.github_service")

    container = MagicMock()
    container.status = "running"
    monkeypatch.setattr(docker_mod, "get_agent_container", lambda name: container)
    monkeypatch.setattr(mod.git_service, "get_agent_git_config", lambda name: None)
    monkeypatch.setattr(mod, "get_github_pat_for_agent", lambda name: PAT)

    class FakeGitHub:
        def __init__(self, pat):
            self.pat = pat

        async def check_repo_exists(self, owner, name):
            raise _pat_bearing_error()

    monkeypatch.setattr(gh_mod, "GitHubService", FakeGitHub)
    body = MagicMock()
    body.repo_owner = "acme"
    body.repo_name = "repo"
    body.create_repo = False
    caplog.set_level(logging.DEBUG)
    exc = _raise_http(
        mod.initialize_github_sync("agent-a", body, _request(), current_user=_admin())
    )
    _assert_scrubbed_5xx(exc, 500, "Failed to initialize GitHub sync", "PlantedError")
    _assert_no_credential(json.dumps(exc.detail, default=str), "response")
    _assert_logged(caplog)
    _assert_no_credential(_log_text(caplog), "log")


# ---------------------------------------------------------------------------
# routers/credentials.py
# ---------------------------------------------------------------------------


def _running(monkeypatch, mod):
    monkeypatch.setattr(mod, "get_agent_container", lambda name: MagicMock())
    status = MagicMock()
    status.status = "running"
    monkeypatch.setattr(mod, "get_agent_status_from_container", lambda c: status)


def test_credentials_export_500(monkeypatch, caplog):
    import routers.credentials as mod
    enc_mod = _live("services.credential_encryption")

    _running(monkeypatch, mod)
    monkeypatch.setattr(mod, "reject_agent_principal", lambda user: None)
    svc = MagicMock()
    svc.export_to_agent = AsyncMock(side_effect=PlantedError(SENTINEL))
    monkeypatch.setattr(enc_mod, "get_credential_encryption_service", lambda: svc)
    caplog.set_level(logging.DEBUG)
    exc = _raise_http(
        mod.export_credentials(_request(), agent_name="agent-a", current_user=_admin())
    )
    _assert_scrubbed_5xx(exc, 500, "Export failed", "PlantedError")
    _assert_logged(caplog)


def test_credentials_status_503(monkeypatch, caplog):
    import routers.credentials as mod

    _running(monkeypatch, mod)

    class FailingClient:
        async def get(self, *a, **kw):
            raise httpx.ConnectError(SENTINEL)

    @asynccontextmanager
    async def fake_client(agent_name, **kw):
        yield FailingClient()

    monkeypatch.setattr(mod, "agent_httpx_client", fake_client)
    caplog.set_level(logging.DEBUG)
    exc = _raise_http(
        mod.get_agent_credentials_status(agent_name="agent-a", current_user=_admin())
    )
    _assert_scrubbed_5xx(exc, 503, "Failed to connect to agent", "ConnectError")
    _assert_logged(caplog)


# ---------------------------------------------------------------------------
# routers/logs.py
# ---------------------------------------------------------------------------


def test_logs_stats_500(monkeypatch, caplog):
    import routers.logs as mod

    svc = MagicMock()
    svc.get_log_stats.side_effect = PlantedError(SENTINEL)
    monkeypatch.setattr(mod, "log_archive_service", svc)
    caplog.set_level(logging.DEBUG)
    exc = _raise_http(mod.get_log_stats(current_user=_admin()))
    _assert_scrubbed_5xx(exc, 500, "Failed to get log stats", "PlantedError")
    _assert_logged(caplog)


# ---------------------------------------------------------------------------
# routers/docs.py
# ---------------------------------------------------------------------------


def test_docs_index_500(monkeypatch, tmp_path, caplog):
    import routers.docs as mod

    (tmp_path / "index.json").write_text("{}")
    monkeypatch.setattr(mod, "get_docs_dir", lambda: tmp_path)
    fake_json = MagicMock()
    fake_json.load.side_effect = PlantedError(SENTINEL)
    monkeypatch.setattr(mod, "json", fake_json)
    caplog.set_level(logging.DEBUG)
    exc = _raise_http(mod.get_docs_index(current_user=_admin()))
    _assert_scrubbed_5xx(exc, 500, "Failed to read index", "PlantedError")
    _assert_logged(caplog)


# ---------------------------------------------------------------------------
# routers/whatsapp.py and routers/voip.py — the Twilio credential check
# ---------------------------------------------------------------------------


def _failing_async_client(monkeypatch, mod):
    class FailingClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **kw):
            raise httpx.ConnectError(SENTINEL)

    monkeypatch.setattr(mod.httpx, "AsyncClient", FailingClient)


@pytest.mark.parametrize("module_name", ["routers.whatsapp", "routers.voip"])
def test_twilio_unreachable_502(module_name, monkeypatch, caplog):
    mod = importlib.import_module(module_name)
    _failing_async_client(monkeypatch, mod)
    caplog.set_level(logging.DEBUG)
    exc = _raise_http(mod._validate_twilio_credentials("AC123", "auth-token-value"))
    _assert_scrubbed_5xx(exc, 502, "Could not reach Twilio API", "ConnectError")
    _assert_logged(caplog)
    assert "auth-token-value" not in _log_text(caplog)


# ---------------------------------------------------------------------------
# routers/telemetry.py
# ---------------------------------------------------------------------------


def test_telemetry_host_500(monkeypatch, caplog):
    import routers.telemetry as mod

    fake_psutil = MagicMock()
    fake_psutil.cpu_percent.side_effect = PlantedError(SENTINEL)
    monkeypatch.setattr(mod, "psutil", fake_psutil)
    caplog.set_level(logging.DEBUG)
    exc = _raise_http(mod.get_host_stats(current_user=_admin()))
    _assert_scrubbed_5xx(exc, 500, "Error getting host stats", "PlantedError")
    _assert_logged(caplog)
