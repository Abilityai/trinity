"""trinity-enterprise#719 — the inline second layer is the route's own rule.

Seven handlers here carry a human-only rule in their route decorator AND an
inline check as their first statement, kept "for direct callers": sharing and
unsharing an agent, the Telegram progress-indicator toggle, the Slack
proactive-consent toggle, and the Workspace client log out, block and unblock.

The inline check used to be `reject_agent_principal`, a denylist that refuses
only an agent key: a direct caller holding the `system`, `ops`,
`portal_delegate` or an unknown-scope key, or a principal with no `mcp_scope`
at all, passed it, though the route's rule refuses every one of them. Each
handler's first statement is now the allowlist that matches its decorator:
`reject_non_interactive_principal` on the INTERACTIVE share route,
`assert_person` on the six PERSON routes.

Driven by calling each handler directly, as a direct caller would, with the
handler's first side effect after the check replaced by a sentinel that raises
`_Reached`. A refused principal gets the rule's own 403 and never reaches the
sentinel; a person passes the check and does reach it, which proves the
sentinel sits where the handler goes next.

Sync throughout (`tests/unit/pytest.ini` runs pytest-asyncio in strict mode).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable

import pytest
import dependencies
import routers.sharing as sharing_router
import routers.slack as slack_router
import routers.telegram as telegram_router
from client_portal import router as portal_router
from fastapi import HTTPException
from models import User

pytestmark = pytest.mark.unit

AGENT = "inline-layer-agent"
EMAIL = "client@example.com"
INTERACTIVE_DETAIL = (
    "This operation requires an interactive session; MCP API keys cannot perform it"
)


class _Reached(Exception):
    """The handler got past its inline check to its first side effect."""


def _reached(*_a: Any, **_kw: Any) -> None:
    raise _Reached()


class _Unreachable:
    """A module attribute (`db`, `service`) every use of which is a `_Reached`."""

    def __getattr__(self, _name: str) -> Callable[..., None]:
        return _reached


@dataclass(frozen=True)
class Handler:
    id: str
    rule: str  # "person" | "interactive"
    call: Callable[[Any], Any]  # principal -> coroutine
    arm: Callable[[pytest.MonkeyPatch], None]  # puts the sentinel in place


def _patch(module: Any, attr: str, value: Any) -> Callable[[pytest.MonkeyPatch], None]:
    return lambda mp: mp.setattr(module, attr, value)


def _patch_all(
    *arms: Callable[[pytest.MonkeyPatch], None]
) -> Callable[[pytest.MonkeyPatch], None]:
    def arm(mp: pytest.MonkeyPatch) -> None:
        for a in arms:
            a(mp)

    return arm


HANDLERS = [
    Handler(
        "share_agent",
        "interactive",
        lambda u: sharing_router.share_agent_endpoint(
            agent_name=AGENT,
            share_request=SimpleNamespace(email=EMAIL),
            request=None,
            current_user=u,
        ),
        _patch(sharing_router, "get_agent_container", _reached),
    ),
    Handler(
        "unshare_agent",
        "person",
        lambda u: sharing_router.unshare_agent_endpoint(
            agent_name=AGENT, email=EMAIL, request=None, current_user=u
        ),
        _patch(sharing_router, "get_agent_container", _reached),
    ),
    Handler(
        "telegram_progress_indicator",
        "person",
        lambda u: telegram_router.set_telegram_progress_indicator(
            agent_name=AGENT, request=SimpleNamespace(enabled=True), current_user=u
        ),
        _patch(telegram_router, "db", _Unreachable()),
    ),
    Handler(
        "slack_channel_proactive",
        "person",
        lambda u: slack_router.set_slack_channel_proactive(
            name=AGENT,
            channel_id="C0000000",
            request=SimpleNamespace(enabled=True),
            current_user=u,
        ),
        _patch(slack_router, "assert_agent_owner", _reached),
    ),
    Handler(
        "portal_client_logout",
        "person",
        lambda u: portal_router.logout_agent_client(
            agent_name=AGENT, email=EMAIL, request=None, current_user=u
        ),
        _patch(portal_router, "service", _Unreachable()),
    ),
    Handler(
        "portal_client_block",
        "person",
        lambda u: portal_router.block_agent_client(
            agent_name=AGENT,
            email=EMAIL,
            body=SimpleNamespace(reason=None),
            request=None,
            current_user=u,
        ),
        # `assert_admin` admits the system key by #2323 design, so it is the
        # first thing past the inline check for an admin-owned key.
        _patch_all(
            _patch(portal_router, "assert_admin", _reached),
            _patch(portal_router, "service", _Unreachable()),
        ),
    ),
    Handler(
        "portal_client_unblock",
        "person",
        lambda u: portal_router.unblock_agent_client(
            agent_name=AGENT, email=EMAIL, request=None, current_user=u
        ),
        _patch_all(
            _patch(portal_router, "assert_admin", _reached),
            _patch(portal_router, "service", _Unreachable()),
        ),
    ),
]
_IDS = [h.id for h in HANDLERS]


def _key(scope: str) -> User:
    # An admin-owned key, as on a default install: it carries the owner's role.
    return User(id=1, username="admin", role="admin", agent_name=None, mcp_scope=scope)


# Every principal a denylist on `agent_name` lets through, and the route's rule
# does not. None of them carries an agent name.
_MACHINE = {
    "system-key": lambda: _key("system"),
    "ops-key": lambda: _key("ops"),
    "portal-delegate-key": lambda: _key("portal_delegate"),
    "unknown-scope-key": lambda: _key("a-scope-nobody-has-shipped"),
    "no-mcp-scope": lambda: SimpleNamespace(
        id=1, username="admin", role="admin", agent_name=None, connector_agent=None
    ),
}


def _refusal(rule: str) -> Any:
    return (
        INTERACTIVE_DETAIL
        if rule == "interactive"
        else dict(dependencies.HUMAN_ONLY_DETAIL)
    )


@pytest.mark.parametrize("principal", list(_MACHINE), ids=list(_MACHINE))
@pytest.mark.parametrize("handler", HANDLERS, ids=_IDS)
def test_a_direct_caller_holding_a_machine_key_gets_the_routes_own_refusal(
    monkeypatch, handler, principal
):
    handler.arm(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(handler.call(_MACHINE[principal]()))
    assert exc.value.status_code == 403
    assert exc.value.detail == _refusal(handler.rule)


@pytest.mark.parametrize(
    "handler",
    [h for h in HANDLERS if h.rule == "interactive"],
    ids=[h.id for h in HANDLERS if h.rule == "interactive"],
)
def test_the_persons_user_key_is_refused_inline_where_the_rule_is_interactive(
    monkeypatch, handler
):
    handler.arm(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(handler.call(_key("user")))
    assert exc.value.status_code == 403
    assert exc.value.detail == INTERACTIVE_DETAIL


@pytest.mark.parametrize("handler", HANDLERS, ids=_IDS)
def test_a_signed_in_person_passes_the_inline_check(monkeypatch, handler):
    """The control: a session passes, so the sentinel is what comes next."""
    handler.arm(monkeypatch)
    person = User(id=1, username="admin", role="admin", agent_name=None, mcp_scope=None)
    with pytest.raises(_Reached):
        asyncio.run(handler.call(person))


@pytest.mark.parametrize(
    "handler",
    [h for h in HANDLERS if h.rule == "person"],
    ids=[h.id for h in HANDLERS if h.rule == "person"],
)
def test_the_persons_user_key_passes_the_inline_check_where_the_rule_is_person(
    monkeypatch, handler
):
    handler.arm(monkeypatch)
    with pytest.raises(_Reached):
        asyncio.run(handler.call(_key("user")))
