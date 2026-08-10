#!/usr/bin/env python3
"""Ensure a live agent exists for the agent-dependent tiers (#2080).

`TEST_AGENT_NAME` gates a whole class of tests — agent-server direct calls,
agent auth isolation, terminal readiness. Unset, they `pytest.skip(...)`, and a
skip is indistinguishable from a pass in the summary. So the suite has been
reporting green on tiers that never executed.

This creates (or reuses) one long-lived agent and prints its name on stdout for
the runner to export. Everything else goes to stderr so the caller can capture
the name with a plain `$(...)`.

Deliberately NOT a pytest fixture: the point is that the runner knows before
the first tier starts whether agent-dependent tests can run, so their absence
is reported by the skip audit as a decision rather than discovered per-test.

Exit codes:
  0  a usable agent name was printed
  1  no live backend / could not provision — the caller reports it and the
     skip audit then flags every agent-gated skip
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# A stable, obviously-machine-owned name. Reused across runs: creating an agent
# per run leaves a trail of containers when a run is interrupted, and the
# reuse path is also what keeps the tier fast.
AGENT_NAME = os.environ.get("TRINITY_TEST_AGENT_NAME", "test-harness-agent")
READY_TIMEOUT_S = int(os.environ.get("TRINITY_TEST_AGENT_TIMEOUT", "180"))


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def main() -> int:
    try:
        from testkit.api_client import ApiConfig, TrinityApiClient
    except Exception as exc:  # pragma: no cover - venv problem, reported by caller
        _log(f"cannot import the test client: {exc}")
        return 1

    config = ApiConfig.from_env()
    client = TrinityApiClient(config)
    try:
        client.authenticate()
    except Exception as exc:
        _log(f"no authenticated backend at {config.base_url}: {exc}")
        return 1

    resp = client.get(f"/api/agents/{AGENT_NAME}")
    if resp.status_code == 200:
        state = (resp.json() or {}).get("status")
        if state != "running":
            _log(f"{AGENT_NAME} exists but is {state!r} — starting it")
            client.post(f"/api/agents/{AGENT_NAME}/start")
    elif resp.status_code == 404:
        _log(f"creating {AGENT_NAME}")
        created = client.post(
            "/api/agents",
            json={"name": AGENT_NAME, "template": "local:default"},
        )
        if created.status_code not in (200, 201):
            _log(f"create failed ({created.status_code}): {created.text[:200]}")
            return 1
    else:
        _log(f"unexpected status looking up {AGENT_NAME}: {resp.status_code}")
        return 1

    deadline = time.time() + READY_TIMEOUT_S
    while time.time() < deadline:
        check = client.get(f"/api/agents/{AGENT_NAME}")
        if check.status_code == 200 and (check.json() or {}).get("status") == "running":
            print(AGENT_NAME)          # stdout: the ONE thing the runner reads
            return 0
        time.sleep(3)

    _log(f"{AGENT_NAME} did not reach 'running' within {READY_TIMEOUT_S}s")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
