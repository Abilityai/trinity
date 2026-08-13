"""`recreate_container_with_updated_config` no longer starts an agent silently (#2092).

The function always ended in `containers_run(...)`, so it created AND started the
replacement. Every in-tree caller pre-checked `container.status == "running"` and
refused otherwise — a real and correct precondition, enforced only by convention,
repeated per call site, and absent from a docstring that documented
`env_overrides` at length.

So recreating a deliberately-stopped agent started it, with no error and no log
line saying the run state had changed. Out-of-tree ops tooling doing a
base-image adoption wave hit it: two agents stopped eight days earlier came back
up. `autonomy_enabled = 0` did not contain that — it gates cron and reminders
(#1806), not human-initiated inbound chat — so a channel binding and a public
link became reachable again.

What is pinned here is the CONTRACT, not the plumbing: the default refuses,
opting out is explicit, and opting out with `preserve_run_state` puts the state
back. The container-creation body is not exercised — it needs Docker — so these
drive the guard and the state restore around it.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)
os.environ.setdefault("TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-2092.db"))
os.environ.setdefault("LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-2092-logs"))

import inspect

import pytest

pytestmark = pytest.mark.unit


class _Container:
    def __init__(self, status="running"):
        self.status = status
        self.attrs = {"Config": {}, "HostConfig": {}}


def _fn():
    from services.agent_service.lifecycle import recreate_container_with_updated_config
    return recreate_container_with_updated_config


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["exited", "created", "paused", None])
async def test_it_refuses_to_start_an_agent_that_was_not_running(status):
    """The whole point: a caller that has not thought about run state gets an
    error, not a running agent."""
    with pytest.raises(ValueError) as e:
        await _fn()("scribe", _Container(status), "alice")

    assert "require_running=False" in str(e.value)
    assert "scribe" in str(e.value)


@pytest.mark.asyncio
async def test_the_refusal_names_the_way_out():
    """An error that does not say what to do instead just gets worked around —
    and the workaround here is the bug (`require_running=False` without
    `preserve_run_state` silently starts the agent again)."""
    with pytest.raises(ValueError) as e:
        await _fn()("scribe", _Container("exited"), "alice")

    assert "preserve_run_state=True" in str(e.value)


@pytest.mark.asyncio
async def test_it_refuses_BEFORE_doing_any_work(monkeypatch):
    """Read the state first, so a refusal cannot half-recreate. If the guard sat
    after the config extraction, a caller could be left with volumes moved and
    no container."""
    from services.agent_service import lifecycle

    called = []
    monkeypatch.setattr(lifecycle, "validate_base_image", lambda *a, **k: called.append("validate"))

    with pytest.raises(ValueError):
        await _fn()("scribe", _Container("exited"), "alice")

    assert called == []


# ---------------------------------------------------------------------------
# Contract shape — what existing callers rely on
# ---------------------------------------------------------------------------

def test_the_default_is_the_behaviour_every_current_caller_already_enforces():
    """`require_running` defaults True, so this is not a behaviour change for
    `repo_binding.py` or `agent_mcp_key_service.py` — both pre-check the same
    condition and would never reach the raise."""
    params = inspect.signature(_fn()).parameters

    assert params["require_running"].default is True
    assert params["preserve_run_state"].default is False


def test_both_new_options_are_keyword_only():
    """Positional would let a caller pass one by accident — the opposite of
    making the choice explicit."""
    params = inspect.signature(_fn()).parameters

    for name in ("require_running", "preserve_run_state"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY


def test_the_docstring_states_that_it_starts_the_container():
    """Request 1 of the issue. The precondition existed only at the call sites;
    the next caller reads this docstring, not them."""
    doc = inspect.getdoc(_fn()) or ""

    assert "STARTS the replacement container" in doc
    assert "require_running" in doc and "preserve_run_state" in doc


def test_the_stop_is_conditional_on_the_ORIGINAL_state():
    """`preserve_run_state` must restore what was there — not stop
    unconditionally, which would break every normal recreate of a running agent.
    """
    from services.agent_service.lifecycle import _restore_stopped_state

    src = inspect.getsource(_restore_stopped_state)

    assert "if not (preserve_run_state and not was_running):" in src


def test_a_failed_stop_does_not_fail_the_recreate_but_is_logged_loudly():
    """The replacement exists and is healthy; failing the whole call over the
    stop leaves the caller worse off than the outcome they asked to avoid. But
    the agent IS running against their wishes, so it cannot be a warning."""
    from services.agent_service.lifecycle import _restore_stopped_state

    src = inspect.getsource(_restore_stopped_state)

    assert "logger.error(" in src
    assert "The agent is running" in src


def test_the_stop_is_NOT_in_the_shared_provisioning_helper():
    """I put it there first, and the tests above caught it.

    `_provision_folders_and_run_agent_container` is the shared tail of BOTH the
    recreate path and agent creation. "The original was stopped" is meaningless
    for a brand-new agent, so a `preserve_run_state` branch living there would
    fire for callers that never asked — the same class of surprise #2092 is
    about, moved one function down.
    """
    from services.agent_service.lifecycle import _provision_folders_and_run_agent_container

    src = inspect.getsource(_provision_folders_and_run_agent_container)

    assert "preserve_run_state" not in src
    assert "was_running" not in src


def test_the_restore_runs_on_the_container_the_recreate_produced():
    """Wired into the recreate function itself, not left as an unused helper."""
    src = inspect.getsource(_fn())

    assert "_restore_stopped_state(" in src
