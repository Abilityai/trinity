"""J18 — a change to a library skill reaches every agent that holds it (trinity-enterprise#794).

A skill changes in the library; every agent assigned it carries the new version
on its next call; an agent without the permission is refused when it tries to
change its own or another agent's skills.

Records the system wrote, and nothing else:

* the library's own per-skill `version` (the git tree SHA, `GET /api/skills/library`);
* the package the platform writes into each holder at
  `~/.claude/skills/<name>/.trinity-skill.json` (`version`, `commit`) and the
  `SKILL.md` it shipped — read back through the agent's files door;
* the platform's 403 to the agent's OWN key.

**Needs a skills repo the run controls.** A skill source must be a github.com
repo (`utils/url_validation.ALLOWED_SKILLS_LIBRARY_HOSTS`) and no API edits a
skill's content, so the chain commits to a repo it is given:

* ``CHAIN_SKILLS_REPO``  — ``owner/name`` on github.com, readable by the
  platform (public, or reachable with the platform's GitHub PAT);
* ``CHAIN_SKILLS_GITHUB_TOKEN`` — a token that can push to it;
* ``CHAIN_SKILLS_REF`` — the branch (default ``main``).

Without them the chain reports NOT RUN, never passed.

**Fleet re-inject is opt-in** (`skills_library_auto_reinject_enabled`, default
OFF, ent#236): with it off, a library change reaches a holder only when that
agent restarts. The chain reads the setting, turns it on for the run when it is
off — saying so on the step, so the report never hides that the shipped default
does not propagate — and restores it afterwards. Each run uses its own
skill name (``chain-probe-<id>``), so runs never see each other's skill and
nothing shadows the bundled library; the source, the skill and the agents are
removed afterwards.
"""
from __future__ import annotations

import base64
import json
import os
import uuid

import pytest
import requests

from journeys.conftest import (
    agent_mcp_key,
    create_agent_and_wait,
    delete_agent_idempotent,
    poll_until,
)

from .conftest import not_run, wait_agent_server

REPO = os.getenv("CHAIN_SKILLS_REPO", "").strip()
TOKEN = os.getenv("CHAIN_SKILLS_GITHUB_TOKEN", "").strip()
REF = os.getenv("CHAIN_SKILLS_REF", "main").strip() or "main"
API = os.getenv("TRINITY_API_URL", "http://localhost:8000")
REINJECT_DEADLINE_S = float(os.getenv("CHAIN_REINJECT_DEADLINE_S", "240"))
GH = "https://api.github.com"


def _skill_md(name: str, marker: str) -> str:
    return (f"---\nname: {name}\ndescription: Probe skill for the system chain test "
            f"(trinity-enterprise#794). Safe to delete.\n---\n\n# {name}\n\nMarker: {marker}\n")


class _Repo:
    """The test skills repo, through the GitHub contents API."""

    def __init__(self, repo: str, token: str, ref: str):
        self.repo, self.ref = repo, ref
        self.h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

    def _url(self, path: str) -> str:
        return f"{GH}/repos/{self.repo}/contents/{path}"

    def sha(self, path: str):
        r = requests.get(self._url(path), headers=self.h, params={"ref": self.ref}, timeout=30)
        return r.json().get("sha") if r.status_code == 200 else None

    def put(self, path: str, content: str, message: str) -> None:
        body = {"message": message, "branch": self.ref,
                "content": base64.b64encode(content.encode()).decode()}
        sha = self.sha(path)
        if sha:
            body["sha"] = sha
        r = requests.put(self._url(path), headers=self.h, json=body, timeout=30)
        assert r.status_code in (200, 201), f"committing {path} to {self.repo} answered {r.status_code}: {r.text[:300]}"

    def delete(self, path: str) -> None:
        sha = self.sha(path)
        if sha:
            requests.delete(self._url(path), headers=self.h, timeout=30,
                            json={"message": "chain test cleanup", "branch": self.ref, "sha": sha})


def _library_version(client, name: str):
    r = client.get("/api/skills/library")
    if r.status_code != 200:
        return None
    return next((s.get("version") for s in r.json() if s.get("name") == name), None)


def _sync_library(client) -> None:
    # The full sweep, not the per-source sync: it is the path that re-injects the
    # fleet afterwards (routers/skills.py — the background fleet re-inject).
    r = client.post("/api/skills/library/sync")
    assert r.status_code in (200, 202, 409), f"library sync answered {r.status_code}: {r.text[:300]}"


def _installed(client, agent: str, name: str):
    """The package record the platform wrote into the agent, or None."""
    r = client.get(f"/api/agents/{agent}/files/download",
                   params={"path": f".claude/skills/{name}/.trinity-skill.json"})
    if r.status_code != 200:
        return None
    try:
        body = r.json()
        if isinstance(body, dict) and "content" in body:
            body = json.loads(body["content"])
        return body if isinstance(body, dict) else None
    except ValueError:
        return None


@pytest.fixture
def skills_repo():
    if not REPO or not TOKEN:
        not_run("no test skills repo — set CHAIN_SKILLS_REPO (owner/name on github.com) and "
                "CHAIN_SKILLS_GITHUB_TOKEN (push access)")
    repo = _Repo(REPO, TOKEN, REF)
    r = requests.get(f"{GH}/repos/{REPO}", headers=repo.h, timeout=30)
    if r.status_code != 200:
        not_run(f"the test skills repo {REPO} is not reachable with the given token ({r.status_code})")
    return repo


@pytest.fixture
def holders(chain_client):
    names = [f"pytest-ephemeral-chain-{uuid.uuid4().hex[:8]}" for _ in range(2)]
    try:
        for n in names:
            create_agent_and_wait(chain_client, n)
            wait_agent_server(chain_client, n)
        yield names
    finally:
        for n in names:
            delete_agent_idempotent(chain_client, n)


@pytest.mark.chain("J18", "A library change reaches every holder")
def test_a_library_change_reaches_every_holder(chain, chain_client, skills_repo, holders):
    client = chain_client
    name = f"chain-probe-{uuid.uuid4().hex[:6]}"
    path = f"skills/{name}/SKILL.md"
    source_id = None
    reinject_was = None
    try:
        with chain.step("the instance re-injects holders after a library change") as rec:
            r = client.get("/api/settings/skills-library")
            assert r.status_code == 200, f"reading the skills-library settings answered {r.status_code}"
            reinject_was = bool(r.json().get("auto_reinject_enabled"))
            if not reinject_was:
                r = client.put("/api/settings/skills-library", json={"auto_reinject_enabled": True})
                assert r.status_code == 200, f"turning fleet re-inject on answered {r.status_code}: {r.text[:200]}"
                rec.detail = ("fleet re-inject was OFF on this instance (the shipped default) and was "
                              "turned on for this run; with it off a change reaches a holder only on restart")

        with chain.step("the library carries the skill"):
            skills_repo.put(path, _skill_md(name, "v1"), f"chain test: add {name}")
            r = client.post("/api/skills/sources", json={
                "name": f"chain-test-{name}", "url": f"https://github.com/{REPO}",
                "ref": REF, "ref_type": "branch"})
            assert r.status_code == 201, f"registering the source answered {r.status_code}: {r.text[:300]}"
            source_id = r.json().get("id") or r.json().get("source_id")
            _sync_library(client)
            v1 = poll_until(lambda: _library_version(client, name), deadline_s=120,
                            describe=f"the library never listed {name} after a sync")

        with chain.step("every holder is assigned it and carries that version"):
            for agent in holders:
                r = client.put(f"/api/agents/{agent}/skills", json={"skills": [name]})
                assert r.status_code == 200, f"assigning to {agent} answered {r.status_code}: {r.text[:300]}"
            for agent in holders:
                rec = poll_until(lambda a=agent: (_installed(client, a, name) or {}).get("version") == v1
                                 and _installed(client, a, name),
                                 deadline_s=REINJECT_DEADLINE_S,
                                 describe=f"{agent} never carried {name} at the library's version")
                assert rec["version"] == v1

        with chain.step("the skill changes in the library"):
            skills_repo.put(path, _skill_md(name, "v2"), f"chain test: change {name}")
            _sync_library(client)
            v2 = poll_until(lambda: (lambda v: v if v and v != v1 else None)(_library_version(client, name)),
                            deadline_s=120, describe=f"the library never moved {name} past {v1}")

        with chain.step("every holder carries the new version, with no per-agent action"):
            for agent in holders:
                poll_until(lambda a=agent: (_installed(client, a, name) or {}).get("version") == v2,
                           deadline_s=REINJECT_DEADLINE_S,
                           describe=f"{agent} still carries the old version of {name}")
                body = client.get(f"/api/agents/{agent}/files/download",
                                  params={"path": f".claude/skills/{name}/SKILL.md"}).text
                assert "Marker: v2" in body, f"{agent}'s SKILL.md is not the changed one"

        with chain.step("an agent without the permission is refused, on itself and on another"):
            try:
                key = agent_mcp_key(holders[1])
            except Exception as e:  # noqa: BLE001
                not_run(f"the agent's own key could not be read ({type(e).__name__}); the run "
                        f"needs the Docker socket of the host running the stack")
            for target in (holders[1], holders[0]):
                r = requests.put(f"{API}/api/agents/{target}/skills",
                                 headers={"Authorization": f"Bearer {key}"},
                                 json={"skills": []}, timeout=30)
                detail = r.json().get("detail") if r.headers.get("content-type", "").startswith(
                    "application/json") else None
                code = detail.get("code") if isinstance(detail, dict) else None
                assert r.status_code == 403 and code == "skill_management_not_permitted", (
                    f"an agent key without skills.manage changing {target}'s skills answered "
                    f"{r.status_code} {code!r}")
            for agent in holders:
                assert (_installed(client, agent, name) or {}).get("version") == v2, \
                    f"the refused write still changed {agent}'s skills"
    finally:
        if reinject_was is False:
            client.put("/api/settings/skills-library", json={"auto_reinject_enabled": False})
        if source_id:
            client.delete(f"/api/skills/sources/{source_id}")
        skills_repo.delete(path)
