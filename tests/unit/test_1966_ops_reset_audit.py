"""#1966 — `POST /api/settings/ops/reset` must write an audit entry.

ent#297 / PR #1893 added validation **and** an audit entry to
`PUT /api/settings/ops/config`, and its prose claimed the audit half covered
both routes:

    "Neither this endpoint nor `/ops/reset` logged anything, while the generic
     PUT /{key} directly above them does…"

`/ops/reset` never got one. So the exact asymmetry ent#297 objected to survived
one route over: the generic `PUT /{key}` audits, `/ops/config` audits,
`/ops/reset` — admin-only, deletes a row per key in `OPS_SETTINGS_DEFAULTS` —
left no trace at all.

The retention half of that prose *does* hold: reset `continue`s over
`RETENTION_OPS_KEYS` (#1638), so it cannot shrink a retention window. What it
could silently revert is everything else, including **`ssh_access_enabled`** —
the setting that decides whether ephemeral SSH credentials can be minted at all.

Mirrors the harness of `test_297_ops_settings_validation.py` deliberately: same
`_Admin` dataclass (a bare MagicMock has a truthy `.agent_name`, so it reads as
an agent key and exercises the wrong branch — the #1816 trap), same monkeypatched
audit sink, same structural-then-behavioural split.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


def _settings_source() -> str:
    return (_BACKEND / "routers" / "settings.py").read_text(encoding="utf-8")


def _reset_handler_source() -> str:
    """Just the reset handler, so an assertion cannot be satisfied by the
    sibling `/ops/config` audit call ~30 lines above it.

    It is currently the LAST function in the file, so slice to EOF when nothing
    follows — searching unconditionally for a trailing `def ` raises, which is
    a test-harness failure dressed up as a code failure.
    """
    src = _settings_source()
    start = src.index("async def reset_ops_settings(")
    nxt = src.find("\ndef ", start)
    nxt_async = src.find("\nasync def ", start)
    candidates = [i for i in (nxt, nxt_async) if i != -1]
    return src[start:min(candidates)] if candidates else src[start:]


# ---------------------------------------------------------------------------
# Structural — cheap intent pins.
# ---------------------------------------------------------------------------


def test_reset_handler_logs_its_own_audit_event():
    """AC #1. Scoped to the handler body: `ops_settings_change` from
    `/ops/config` sits ~30 lines up, and a whole-file grep would pass on it."""
    handler = _reset_handler_source()
    assert 'event_action="ops_settings_reset"' in handler, (
        "POST /api/settings/ops/reset still writes no audit entry (#1966)"
    )
    assert "AuditEventType.CONFIGURATION" in handler


def test_reset_audit_action_is_distinct_from_the_config_one():
    """`ops_settings_change` and `ops_settings_reset` are different acts —
    one sets values, the other deletes rows. Sharing an action name would make
    them indistinguishable in exactly the log built to tell them apart."""
    handler = _reset_handler_source()
    assert 'event_action="ops_settings_change"' not in handler


def test_architecture_prose_is_now_true():
    """AC #3. The claim 'neither this route nor /ops/reset logged anything
    before' shipped as documentation of a fix that only covered one route."""
    arch = (
        Path(__file__).resolve().parents[2]
        / "docs" / "memory" / "architecture.md"
    )
    if not arch.exists():  # pragma: no cover
        pytest.skip("architecture.md not present")
    # Nothing to assert about the prose itself — the point is that the code it
    # describes now matches it, which the handler assertions above establish.
    assert 'event_action="ops_settings_reset"' in _reset_handler_source()


# ---------------------------------------------------------------------------
# Behavioural — through the real handler.
# ---------------------------------------------------------------------------

from dataclasses import dataclass          # noqa: E402
from typing import Optional                # noqa: E402


@dataclass
class _Admin:
    """Explicit human-admin principal. NOT a MagicMock: a bare MagicMock has a
    truthy `.agent_name`, so it reads as an agent key and would exercise the
    wrong branch (#1816, re-hit in ent#293)."""
    id: int = 1
    username: str = "admin"
    email: Optional[str] = "admin@example.com"
    role: str = "admin"
    agent_name: Optional[str] = None
    connector_agent: Optional[str] = None
    # #2323: the admin gate is now an ALLOWLIST over `mcp_scope`, and a
    # principal that does not carry the field at all fails CLOSED — deliberately,
    # because defaulting an absent authorization discriminator to `None` would
    # make it the privileged JWT value. `models.User` always declares it, so a
    # stand-in must too. `None` = an interactive human, which is what this is.
    mcp_scope: Optional[str] = None


class _Req:
    client = type("c", (), {"host": "127.0.0.1"})()
    url = type("u", (), {"path": "/api/settings/ops/reset"})()
    state = type("s", (), {"request_id": "r1"})()
    headers: dict = {}


@pytest.fixture
def reset_router(monkeypatch):
    """The handler with its two side-effects captured: which keys it deleted,
    and what it audited."""
    try:
        from routers import settings as mod
    except ImportError:  # pragma: no cover
        pytest.skip("backend venv required")

    deleted = []
    audited = []

    def _delete(key, *a, **kw):
        deleted.append(key)
        return True  # a row existed

    monkeypatch.setattr(mod.db, "delete_setting", _delete, raising=False)

    async def _log(**kwargs):
        audited.append(kwargs)

    monkeypatch.setattr(mod.platform_audit_service, "log", _log, raising=False)
    return mod, deleted, audited


def test_reset_writes_exactly_one_audit_entry(reset_router):
    """AC #1, driven for real rather than grepped."""
    import asyncio

    mod, _, audited = reset_router

    asyncio.run(mod.reset_ops_settings(request=_Req(), current_user=_Admin()))

    assert len(audited) == 1, "reset must write exactly one audit entry"
    entry = audited[0]
    assert entry["event_action"] == "ops_settings_reset"
    assert entry["source"] == "api"
    assert entry["endpoint"] == "/api/settings/ops/reset"
    assert entry["request_id"] == "r1"
    assert entry["actor_ip"] == "127.0.0.1"


def test_audit_names_the_keys_that_were_reset(reset_router):
    """"Which keys reverted" is the whole point — a bare "someone pressed
    reset" entry would not answer the question the log exists for."""
    import asyncio

    mod, deleted, audited = reset_router

    asyncio.run(mod.reset_ops_settings(request=_Req(), current_user=_Admin()))

    details = audited[0]["details"]
    assert details["reset"] == deleted
    assert details["reset_count"] == len(deleted)
    assert deleted, "the fixture should have exercised at least one delete"


def test_the_security_relevant_key_is_covered(reset_router):
    """`ssh_access_enabled` is the one the issue singles out: resetting it
    changes whether ephemeral SSH credentials can be minted at all. If it is
    reachable by reset it must be nameable in the log."""
    import asyncio

    mod, deleted, audited = reset_router

    asyncio.run(mod.reset_ops_settings(request=_Req(), current_user=_Admin()))

    assert "ssh_access_enabled" in deleted
    assert "ssh_access_enabled" in audited[0]["details"]["reset"]


def test_retention_keys_appear_under_skipped_never_reset(reset_router):
    """AC #2's second half. #1638 protects the windows; the audit entry must
    report that protection honestly rather than implying they were touched."""
    import asyncio
    from services.settings_service import RETENTION_OPS_KEYS

    mod, deleted, audited = reset_router

    asyncio.run(mod.reset_ops_settings(request=_Req(), current_user=_Admin()))

    details = audited[0]["details"]
    for key in RETENTION_OPS_KEYS:
        assert key not in details["reset"], (
            f"{key} is a retention window and must never be reported as reset"
        )
        assert key not in deleted, (
            f"{key} was actually DELETED — #1638's protection regressed"
        )
        assert key in details["skipped"]


def test_audit_carries_no_values_only_keys(reset_router):
    """Unlike /ops/config there is no value worth recording — every one of
    these rows is being deleted, so the durable fact is which keys reverted,
    not what they held on the way out."""
    import asyncio

    mod, _, audited = reset_router

    asyncio.run(mod.reset_ops_settings(request=_Req(), current_user=_Admin()))

    details = audited[0]["details"]
    assert set(details) == {"reset", "reset_count", "skipped"}
    for value in details.values():
        assert isinstance(value, (list, int))


def test_reset_is_audited_even_when_nothing_was_stored(reset_router, monkeypatch):
    """An admin pressing reset on already-default settings is still a real
    administrative act. Gating the log on `deleted` (the way /ops/config gates
    on `updated`) would make "reset, nothing to do" indistinguishable from
    "never attempted" — which is the reporting gap this issue is about, in
    miniature."""
    import asyncio

    mod, _, audited = reset_router
    monkeypatch.setattr(mod.db, "delete_setting", lambda *a, **kw: False,
                        raising=False)

    result = asyncio.run(
        mod.reset_ops_settings(request=_Req(), current_user=_Admin())
    )

    assert result["success"] is True
    assert result["reset"] == []
    assert len(audited) == 1
    assert audited[0]["details"]["reset_count"] == 0


def test_the_handler_itself_does_not_guard_the_audit_call(reset_router, monkeypatch):
    """Documents WHERE the safety lives, rather than implying it lives here.

    The `log()` call sits inside the try whose `except` maps to a 500, with no
    guard of its own — so a raising sink WOULD turn a completed reset into a
    500 the operator retries against already-deleted rows. That is safe only
    because `platform_audit_service.log` never raises, which the next test
    pins at its source. Stated explicitly so a future change to either side
    sees the coupling instead of rediscovering it.
    """
    import asyncio

    mod, deleted, _ = reset_router

    async def _boom(**kwargs):
        raise RuntimeError("audit sink down")

    monkeypatch.setattr(mod.platform_audit_service, "log", _boom, raising=False)

    with pytest.raises(Exception):
        asyncio.run(mod.reset_ops_settings(request=_Req(), current_user=_Admin()))

    # And the deletes had already happened — which is precisely why the
    # service's swallow-everything contract is load-bearing here.
    assert deleted


def test_real_audit_service_swallows_its_own_failures():
    """The guarantee the handler leans on, asserted at its source rather than
    taken on faith from a docstring."""
    import asyncio

    try:
        from services.platform_audit_service import platform_audit_service
    except ImportError:  # pragma: no cover
        pytest.skip("backend venv required")

    import services.platform_audit_service as svc

    original = getattr(svc.db, "create_audit_entry", None)
    if original is None:  # pragma: no cover
        pytest.skip("audit service has no db seam to break")

    def _boom(*a, **kw):
        raise RuntimeError("db down")

    svc.db.create_audit_entry = _boom
    try:
        result = asyncio.run(platform_audit_service.log(
            event_type=svc.AuditEventType.CONFIGURATION,
            event_action="ops_settings_reset",
            source="api",
        ))
    finally:
        svc.db.create_audit_entry = original

    assert result is None, (
        "platform_audit_service.log must return None on failure, never raise — "
        "the reset handler calls it inside the try that maps to a 500"
    )
