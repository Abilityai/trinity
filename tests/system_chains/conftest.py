"""System chain tests (trinity-enterprise#794) — fixtures and the report plugin.

A *chain* checks that a change in one part of a rolled-out system shows up in
another part, on records the SYSTEM wrote (platform rows, files the platform
wrote into a container, the execution record) — never on an agent's own word
that it is done.

Three rules:

**Off unless asked for.** Collected only when ``TRINITY_CHAIN_TESTS=1``
(``scripts/system/run_chains.sh`` sets it). A chain creates agents, registers
skill sources and waits minutes for staleness; it must never ride along with
``run-full.sh`` or the per-PR journey lane by accident.

**Not run is a verdict, never a pass.** A chain whose precondition is missing
(no model key, no test skills repo, a blocking issue) calls :func:`not_run`;
the report says so with the reason. See ``chain_report.verdict``.

**Every run writes a report.** ``chain-report.json`` and ``chain-report.md`` in
``CHAIN_REPORT_DIR`` (default ``./chain-report``): per chain, passed / failed at
a named step / partial / not run — the evidence attached to #783.
"""
from __future__ import annotations

import os
import platform
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

from .chain_report import NOT_RUN_PREFIX, ChainRun, to_json, to_markdown, verdict

ENABLED = os.getenv("TRINITY_CHAIN_TESTS", "").strip() == "1"
if not ENABLED:
    collect_ignore_glob = ["test_*.py"]

_RESULTS: list = []
#: Read once through the authenticated client (``/api/version`` needs a token).
_TARGET = {"version": "unknown"}
#: ``CHAIN_KEEP_AGENTS=1`` leaves the agents a chain created in place, so a person
#: can open them in the UI afterwards; the report lists them. Off by default —
#: the tier's rule is that it leaves nothing behind.
KEEP_AGENTS = os.getenv("CHAIN_KEEP_AGENTS", "").strip() == "1"
_KEPT: list = []


def not_run(reason: str) -> None:
    """Stop this chain as NOT RUN, with the reason the report shows."""
    pytest.skip(NOT_RUN_PREFIX + reason)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "chain(chain_id, title): a system chain test (trinity-enterprise#794)")


@pytest.fixture
def chain(request) -> ChainRun:
    marker = request.node.get_closest_marker("chain")
    if marker is None or len(marker.args) < 2:
        raise AssertionError("a chain test needs @pytest.mark.chain('Jnn', 'title')")
    run = ChainRun(chain_id=marker.args[0], title=marker.args[1])
    request.node._chain_run = run
    return run


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if getattr(item, "_chain_reported", False):
        return
    run = getattr(item, "_chain_run", None)
    if run is None:
        # Setup broke before the `chain` fixture ran (e.g. the suite-wide
        # autouse login failed): the chain is still reported — from its marker —
        # never silently missing from the report.
        marker = item.get_closest_marker("chain")
        if marker is None or len(marker.args) < 2:
            return
        run = ChainRun(chain_id=marker.args[0], title=marker.args[1])
    # The verdict comes from the first phase that did not pass, else from call.
    if rep.when == "setup" and rep.passed:
        return
    if rep.when == "teardown" and rep.passed:
        return
    skip_reason = None
    if rep.skipped and isinstance(rep.longrepr, tuple):
        skip_reason = rep.longrepr[2]
    error = None
    if rep.failed:
        # The crash line ("httpx.HTTPStatusError: 401 Unauthorized …"), not the
        # last line of the traceback, which is often a footer or a doc link.
        crash = getattr(rep.longrepr, "reprcrash", None)
        error = (getattr(crash, "message", None)
                 or (str(rep.longrepr).strip().splitlines() or [""])[-1])[:300]
        if rep.when == "setup":
            error = f"setup failed before the chain started: {error}"
    _RESULTS.append(verdict(run, outcome=rep.outcome, skip_reason=skip_reason, error=error))
    item._chain_reported = True


def pytest_sessionfinish(session, exitstatus):
    if not ENABLED:
        return
    out = Path(os.getenv("CHAIN_REPORT_DIR", "chain-report"))
    out.mkdir(parents=True, exist_ok=True)
    meta = {
        "target": os.getenv("TRINITY_API_URL", "http://localhost:8000"),
        "version": _TARGET["version"],
        "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "runner": platform.node() or "unknown",
    }
    if _KEPT:
        meta["kept agents"] = ", ".join(_KEPT)
    (out / "chain-report.json").write_text(to_json(_RESULTS, meta))
    (out / "chain-report.md").write_text(to_markdown(_RESULTS, meta))


# ---------------------------------------------------------------------------
# The live stack — the journey tier's own fixtures and primitives, reused
# (one create path, one poll primitive), not re-implemented.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def chain_client(api_client):
    url = os.getenv("TRINITY_API_URL", "http://localhost:8000")
    try:
        health = requests.get(f"{url}/health", timeout=10)
    except requests.RequestException as e:
        pytest.fail(f"chain tests need a live stack at {url} and could not reach it "
                    f"({type(e).__name__}). A run that cannot reach its target is a failure.")
    if health.status_code != 200:
        pytest.fail(f"chain tests need a healthy stack at {url}; /health answered "
                    f"{health.status_code}.")
    try:
        body = api_client.get("/api/version").json()
        _TARGET["version"] = str(body.get("version") or body)[:60]
    except Exception:  # noqa: BLE001 — the report still names the target URL
        pass
    return api_client


def release_agent(client, name: str) -> None:
    """Teardown for a chain's agent: deleted, unless ``CHAIN_KEEP_AGENTS=1``."""
    if KEEP_AGENTS:
        _KEPT.append(name)
        return
    from journeys.conftest import delete_agent_idempotent

    delete_agent_idempotent(client, name)


def wait_agent_server(client, agent: str) -> None:
    """`running` is the container; the agent's own server answers a moment later
    (503 "may still be starting up"). Every client waits for it — so does a chain."""
    from journeys.conftest import poll_until

    poll_until(lambda: client.get(f"/api/agents/{agent}/files/download",
                                  params={"path": "template.yaml"}).status_code != 503,
               deadline_s=90, describe=f"{agent}'s agent server never answered after it was running")


def model_key_available(client) -> bool:
    """Whether the target can run a real model turn. Read from the platform's own
    feature flags, never guessed from the runner's environment."""
    try:
        flags = client.get("/api/settings/feature-flags").json()
    except Exception:  # noqa: BLE001
        return False
    return bool(flags.get("claude_auth_configured"))
