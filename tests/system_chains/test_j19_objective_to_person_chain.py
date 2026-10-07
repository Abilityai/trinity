"""J19 — an objective reaches the person (trinity-enterprise#794).

An objective in the canon names a number; the agent records it; the role card
shows it against the target; when recording stops, the number reads stale.

Every check reads a record the system wrote:

* the point is recorded by the agent's OWN key (`record_metrics`' backend route,
  which refuses a key recording as anyone else) — the platform's point store;
* the role card (`GET /api/enterprise/client-portal/agents/{n}/role`) and the
  operator's objectives read (`GET /api/agents/{n}/objectives`) are both the one
  objective ↔ metric join (ent#666) over that store — `actual`, `target` and the
  2× cadence stale rule are decided by the platform, not by the agent.

**The brief half is NOT RUN, on purpose.** The scheduled brief (ent#637) gets no
objective number from the platform — any number in a brief is written by the
model, and this chain does not pass on what an agent says. The step is recorded
as not run, so the chain reports ``partial`` until the platform puts the number
in the brief.

Robust to trinity#3271: where the assignments module is present, the seat is
also recorded on the platform (the agent holds it, ent#811), the way an admin
would, so the join finds the objective whether it reads `x-role`
(dev) or the seat on record (#3271).
"""
from __future__ import annotations

import os
import uuid

import pytest
import requests
import yaml

from journeys.conftest import (
    agent_mcp_key,
    create_agent_and_wait,
    poll_until,
)

from .conftest import not_run, release_agent, wait_agent_server

#: Per run: a seat id is unique on the instance, and a chain must not depend on
#: an earlier run's cleanup (a soft-deleted agent kept its held seat — found by
#: this chain, reported on trinity-enterprise#818).
RUN = uuid.uuid4().hex[:6]
ROLE = f"chain-pipeline-owner-{RUN}"
OBJECTIVE = f"chain-q4-pipeline-{RUN}"
METRIC = "chain_pipeline"
TARGET = 100
VALUE = 42
#: The platform's minimum cadence (template_metrics.CADENCE_MIN_SECONDS). Stale
#: is "no point within 2× cadence", so the stale step waits past 120 s.
CADENCE = "1m"
STALE_DEADLINE_S = float(os.getenv("CHAIN_STALE_DEADLINE_S", "240"))
API = os.getenv("TRINITY_API_URL", "http://localhost:8000")

ROLE_FILE = f"""schema_version: 1
id: {ROLE}
title: Chain Pipeline Owner
mission: Keep the quarter's pipeline at target.
status: active
review_by: 2099-12-01
"""
OBJECTIVE_FILE = f"""schema_version: 1
id: {OBJECTIVE}
statement: Reach {TARGET} qualified pipeline this quarter.
horizon: quarter
owner: role:{ROLE}
metrics:
  - name: {METRIC}
    direction: up
    target: {TARGET}
status: active
"""


def _put_file(client, agent: str, path: str, content: str) -> None:
    r = client.put(f"/api/agents/{agent}/files?path={requests.utils.quote(path)}",
                   json={"content": content})
    assert r.status_code in (200, 201), f"writing {path} answered {r.status_code}: {r.text[:300]}"


def _read_file(client, agent: str, path: str) -> str:
    r = client.get(f"/api/agents/{agent}/files/download", params={"path": path})
    if r.status_code != 200:
        return ""
    try:
        body = r.json()
        return body.get("content", "") if isinstance(body, dict) else str(body)
    except ValueError:
        return r.text


def _metric_on_card(card: dict):
    for o in card.get("objectives") or []:
        if o.get("id") != OBJECTIVE:
            continue
        for m in o.get("metrics") or []:
            if m.get("name") == METRIC:
                return o, m
    return None, None


@pytest.fixture
def agent(chain_client):
    name = f"pytest-ephemeral-chain-{uuid.uuid4().hex[:8]}"
    try:
        create_agent_and_wait(chain_client, name)
        wait_agent_server(chain_client, name)
        yield name
    finally:
        release_agent(chain_client, name)


@pytest.mark.chain("J19", "An objective reaches the person")
def test_an_objective_reaches_the_person(chain, chain_client, agent):
    client = chain_client

    with chain.step("the canon names the objective and the agent declares the metric"):
        template = yaml.safe_load(_read_file(client, agent, "template.yaml") or "") or {}
        if not isinstance(template, dict):
            template = {}
        template.setdefault("name", agent)
        template["x-role"] = {"role": ROLE, "status": "calibrating"}
        template["x-canon"] = {"clone_path": "canon"}
        template["metrics"] = [{"name": METRIC, "type": "gauge", "label": "Chain pipeline",
                                "cadence": CADENCE, "direction": "up_good"}]
        _put_file(client, agent, "template.yaml", yaml.safe_dump(template, sort_keys=False))
        _put_file(client, agent, f"canon/roles/{ROLE}.yaml", ROLE_FILE)
        _put_file(client, agent, f"canon/objectives/{OBJECTIVE}.yaml", OBJECTIVE_FILE)
        r = client.post(f"/api/agents/{agent}/metrics/definitions/refresh")
        assert r.status_code == 200, f"refreshing the metric registry answered {r.status_code}: {r.text[:300]}"
        # A rejected `metrics:` block still answers 200 (the refresh reports its
        # errors) — so the step checks the registry now holds the metric.
        assert METRIC in r.text, f"the registry did not take the declared metric: {r.text[:400]}"

    with chain.step("the seat is on record (where the assignments module is present)"):
        # The agent HOLDS the seat itself (ent#811) — no person needed, and the
        # join owns the seat's objectives exactly as for a companion's primary.
        r = client.put(f"/api/enterprise/assignments/agents/{agent}/seat", json={"role_id": ROLE})
        # 404: the module is not installed — `x-role` alone carries the seat on dev.
        assert r.status_code in (200, 201, 204, 404), \
            f"recording the seat answered {r.status_code}: {r.text[:300]}"

    with chain.step("the agent records the number with its own key"):
        try:
            key = agent_mcp_key(agent)
        except Exception as e:  # noqa: BLE001 — a missing Docker socket is a precondition
            not_run(f"the agent's own key could not be read ({type(e).__name__}); the run "
                    f"needs the Docker socket of the host running the stack")
        r = requests.post(f"{API}/api/agents/{agent}/metrics/points",
                          headers={"Authorization": f"Bearer {key}"},
                          json={"points": [{"metric": METRIC, "value": VALUE}]}, timeout=30)
        assert r.status_code in (200, 201), f"recording a point answered {r.status_code}: {r.text[:300]}"

    with chain.step("the role card shows the number against the target"):
        def card_shows():
            card = client.get(f"/api/enterprise/client-portal/agents/{agent}/role").json()
            obj, m = _metric_on_card(card)
            return (card, obj, m) if m and m.get("actual") == VALUE else None
        card, obj, m = poll_until(card_shows, deadline_s=60,
                                  describe="the role card never showed the recorded number")
        assert obj["owned"] is True, f"the objective is not owned on the card: {obj}"
        assert m["target"] == TARGET, f"target on the card is {m['target']!r}, not {TARGET}"
        assert m["stale"] is False, f"a point recorded just now reads stale: {m}"

    with chain.step("the operator's objectives read agrees"):
        r = client.get(f"/api/agents/{agent}/objectives")
        assert r.status_code == 200, f"objectives read answered {r.status_code}: {r.text[:300]}"
        ops = {o.get("id"): o for o in r.json().get("objectives") or []}
        assert OBJECTIVE in ops, f"the operator read does not list {OBJECTIVE}: {sorted(ops)}"

    with chain.step("when recording stops, the number reads stale on the card"):
        def card_stale():
            card = client.get(f"/api/enterprise/client-portal/agents/{agent}/role").json()
            _, m = _metric_on_card(card)
            return m if m and m.get("stale") is True else None
        m = poll_until(card_stale, deadline_s=STALE_DEADLINE_S, interval_s=10,
                       describe="the number never read stale after recording stopped (2× cadence)")
        assert m["actual"] == VALUE, "stale must keep the last value beside the mark"

    chain.not_run_step(
        "the brief shows the number against the target, and stale when it is",
        "the platform puts no objective number in a brief — any number there is model-written, "
        "which this chain does not accept (follow-up to trinity-enterprise#794)")
