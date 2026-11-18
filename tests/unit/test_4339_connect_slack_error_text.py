"""`connect_slack` answers an unexpected failure with its class name only.

abilityai/trinity#4339, the last #4284 site. When the workspace is already
connected, `POST /api/agents/{name}/public-links/{link_id}/slack/connect`
creates the channel and binds it inside one `try`. Its broad arm used to
answer `HTTPException(500, detail=str(e))`: the bind is a SQLAlchemy upsert,
so the caller got the SQL statement and its bound parameters. It now answers
a fixed sentence plus the class name, and logs the traceback with the
workspace bot token it holds masked.

Each test drives the REAL handler. Patches go through the route's own
`__globals__`, so they reach whichever module object the route runs in.

- The 500 detail carries neither the planted sentinel nor the token, and
  names the class.
- The log record carries the sentinel and the traceback, and no 8+ character
  piece of the token.
- The held-value mask does the work: a well-formed `xoxb-` token is masked by
  the shape pass anyway, so one test holds a token split by a line break,
  whose tail the shape pass leaves visible (pinned below).
- The 400 answers in the same function and the `except HTTPException: raise`
  passthrough are unchanged.

This file imports nothing the previous code lacks, so on that code it fails on
its assertions, not on an import.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional, Tuple

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi import HTTPException  # noqa: E402

import routers.slack as slack_router  # noqa: E402

pytestmark = pytest.mark.unit

# A string no platform-authored message would ever contain.
SENTINEL = "sekrit-4339-internal-db-host-10.0.0.7:5432"
# Synthetic tokens. TOKEN is well-formed, so the shape pass masks it too.
TOKEN = "xoxb-fake-4339-test-token-not-a-real-one"
# A token pasted with a line break: the shape pass stops at the break and
# leaves TAIL visible, so only the held-value mask keeps TAIL out of the log.
TAIL = "T" * 24 + "tokntailZ"
SPLIT = "xoxb-" + "B" * 24 + "\n" + TAIL

LOG_PREFIX = "Failed to bind agent to Slack channel"
CLASS_ONLY = "Failed to connect Slack (RuntimeError — details in backend logs)"


def _pieces(held: str) -> list[str]:
    """The held value plus every run of 8+ characters that no escaping
    rewrites: none of them may appear in the log."""
    return [held, *re.findall(r"[A-Za-z0-9_.~-]{8,}", held)]


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _created(*_a: Any, **_k: Any) -> Tuple[bool, Optional[str], Optional[str]]:
    return True, "C1", None


def _raiser(exc: BaseException) -> Callable[..., None]:
    def bind(**_k: Any) -> None:
        raise exc

    return bind


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    workspace_token: Optional[str],
    bind: Callable[..., None] = lambda **_k: None,
    create_channel: Callable[..., Any] = _created,
    signing_secret: str = "",
    get_oauth_url: Optional[Callable[[str], str]] = None,
) -> logging.Logger:
    """Patch the route's collaborators; return the logger it writes to."""
    g: dict[str, Any] = slack_router.connect_slack.__globals__
    workspaces = (
        []
        if workspace_token is None
        else [{"team_id": "T1", "team_name": "W", "bot_token": workspace_token}]
    )
    fake_db = SimpleNamespace(
        get_public_link=lambda link_id: {"agent_name": "a1"},
        get_slack_connection_by_link=lambda link_id: None,
        get_all_slack_workspaces=lambda: workspaces,
        get_slack_agents_for_workspace=lambda team_id: [],
        bind_slack_channel_to_agent=bind,
        create_slack_connection=lambda **_k: None,
    )

    def _no_oauth_url(state: str) -> str:
        raise AssertionError("the OAuth branch was not expected")

    fake_service = SimpleNamespace(
        create_channel=create_channel,
        encode_oauth_state=lambda *a: "state",
        get_oauth_url=get_oauth_url or _no_oauth_url,
    )
    monkeypatch.setitem(g, "db", fake_db)
    monkeypatch.setitem(g, "slack_service", fake_service)
    monkeypatch.setitem(g, "assert_agent_owner", lambda *a, **k: None)
    monkeypatch.setitem(g, "get_slack_signing_secret", lambda: signing_secret)
    logger: logging.Logger = g["logger"]
    return logger


def _connect() -> Any:
    user: Any = SimpleNamespace(id=7, username="owner")  # stands in for models.User
    return _run(slack_router.connect_slack(name="a1", link_id="L1", current_user=user))


def _connect_error() -> HTTPException:
    with pytest.raises(HTTPException) as ei:
        _connect()
    return ei.value


def _bind_records(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.ERROR and r.getMessage().startswith(LOG_PREFIX)
    ]


# ---------------------------------------------------------------------------
# the 500 and its log record
# ---------------------------------------------------------------------------


def test_bind_failure_answers_the_class_name_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        workspace_token=TOKEN,
        bind=_raiser(RuntimeError(f"{SENTINEL} bot_token={TOKEN}")),
    )

    err = _connect_error()

    assert err.status_code == 500
    detail = str(err.detail)
    assert SENTINEL not in detail, detail
    assert TOKEN not in detail, detail
    assert detail == CLASS_ONLY


def test_bind_failure_logs_the_masked_traceback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    logger = _install(
        monkeypatch,
        workspace_token=TOKEN,
        bind=_raiser(RuntimeError(f"{SENTINEL} bot_token={TOKEN}")),
    )
    caplog.set_level(logging.ERROR, logger=logger.name)

    _connect_error()

    records = _bind_records(caplog)
    assert len(records) == 1, records
    [record] = records
    assert SENTINEL in record, "the detail must reach the backend log"
    assert "Traceback (most recent call last)" in record, record
    leaked = [p for p in _pieces(TOKEN) if p in record]
    assert not leaked, f"the bot token reached the log: {leaked}"
    assert "***" in record


def test_the_split_token_tail_is_not_recognised_by_the_shape_pass() -> None:
    """Precondition for the next test: without the held-value mask, the
    shape pass alone would leave the tail in the log in both renderings."""
    from utils.credential_sanitizer import sanitize_text

    assert TAIL in sanitize_text(f"x {SPLIT} y")
    assert TAIL in sanitize_text(f"x {SPLIT!r} y")


@pytest.mark.parametrize("rendering", ["raw", "params_repr"])
def test_bind_failure_masks_the_held_token_by_identity(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, rendering: str
) -> None:
    # A SQLAlchemy error renders its bound parameters as a `%r` dict.
    shown = SPLIT if rendering == "raw" else repr({"bot_token": SPLIT})
    logger = _install(
        monkeypatch,
        workspace_token=SPLIT,
        bind=_raiser(RuntimeError(f"{SENTINEL} [parameters: {shown}]")),
    )
    caplog.set_level(logging.ERROR, logger=logger.name)

    err = _connect_error()

    # The log first: this is what only the held-value mask can make pass.
    [record] = _bind_records(caplog)
    assert SENTINEL in record
    assert TAIL not in record, "only the held-value mask keeps the tail out"
    leaked = [p for p in _pieces(SPLIT) if p in record]
    assert not leaked, f"the bot token reached the log: {leaked}"
    assert err.status_code == 500 and TAIL not in str(err.detail)


# ---------------------------------------------------------------------------
# the 400 answers and the passthrough are unchanged
# ---------------------------------------------------------------------------


def test_a_refused_channel_still_answers_its_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refused(*_a: Any, **_k: Any) -> Tuple[bool, Optional[str], Optional[str]]:
        return False, None, "name_reserved_x"

    _install(monkeypatch, workspace_token=TOKEN, create_channel=refused)

    err = _connect_error()

    assert err.status_code == 400
    assert err.detail == "Failed to create Slack channel: name_reserved_x"


def test_no_workspace_and_no_signing_secret_still_answers_its_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, workspace_token=None, signing_secret="")

    err = _connect_error()

    assert err.status_code == 400
    assert err.detail == "Slack integration not configured"


def test_an_unconfigured_public_url_still_answers_its_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_public_url(state: str) -> str:
        raise ValueError("PUBLIC_CHAT_URL not configured")

    _install(
        monkeypatch,
        workspace_token=None,
        signing_secret="s3cret-4339",
        get_oauth_url=no_public_url,
    )

    err = _connect_error()

    assert err.status_code == 400
    assert err.detail == "PUBLIC_CHAT_URL not configured"


def test_a_successful_bind_still_answers_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, workspace_token=TOKEN)

    out = _connect()

    assert out == {
        "status": "connected",
        "channel_id": "C1",
        "channel_name": "a1",
        "workspace": "W",
    }
