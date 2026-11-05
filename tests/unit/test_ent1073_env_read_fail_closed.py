"""trinity-enterprise#1073 — a whole-file `.env` writer never treats a file it
could not read as empty.

The agent's ``GET /api/credentials/read`` reports ``.env`` as text, as
non-UTF-8 bytes (``files_b64``), as proven absent (``missing``, ent#1066), or
not at all (a read error, or an image too old to say "absent"). Three backend
paths read the file and write the WHOLE file back. Two of them collapsed every
non-text answer to "" and so replaced the file with only their own keys,
reporting success:

- the Nevermined bind and revoke (pinned through the routes in
  ``test_ent966_delegation_routes.py::TestEnvReadFailsClosed``);
- the GitHub PAT write, whenever it may add a missing line (the per-agent path,
  and the global rotation for an agent with a repo).

The third, the Workspace credential sink (ent#1066), already refused, and now
shares the one rule (``services/agent_env_read.py``) with the other two. This
file pins that rule, the GitHub PAT path against a real httpx transport, and a
guard that no backend module reads ``.env`` from the agent without it.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import httpx
import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services import agent_env_read  # noqa: E402

pytestmark = pytest.mark.unit


# ------------------------------------------------------------------ the rule

@pytest.mark.parametrize("body, expected", [
    ({"files": {".env": "A=1\n"}}, (agent_env_read.TEXT, "A=1\n")),
    ({"files": {".env": ""}}, (agent_env_read.TEXT, "")),               # an EMPTY file is text
    ({"files": {}, "missing": [".env"]}, (agent_env_read.ABSENT, "")),
    ({"files": {}, "files_b64": {".env": "//79"}}, (agent_env_read.NOT_UTF8, None)),
    ({"files": {}}, (agent_env_read.UNCONFIRMED, None)),                 # read error / old image
    ({}, (agent_env_read.UNCONFIRMED, None)),
    ({"files": {".env": None}, "missing": []}, (agent_env_read.UNCONFIRMED, None)),
])
def test_only_text_and_proven_absence_are_writable(body, expected):
    assert agent_env_read.classify_env_read(body) == expected


@pytest.mark.parametrize("body", [None, [], "x", {"files": "x"}, {"files_b64": "x"}, {"missing": "x"}])
def test_a_body_that_is_not_the_read_shape_is_an_error(body):
    with pytest.raises(ValueError):
        agent_env_read.classify_env_read(body)


# ------------------------------------------------------------- GitHub PAT path

AGENT = "pat-agent"
ENV = "OTHER_KEY=keep-me\nSOME_SECRET=s3cr3t\n"


def _apply(monkeypatch, read_body, *, add_if_missing):
    from services import github_pat_propagation_service as svc
    monkeypatch.setattr(svc, "build_agent_auth_headers", lambda name: {})
    injects = []

    def handler(request):
        if request.url.path == "/api/credentials/read":
            return httpx.Response(200, json=read_body)
        if request.url.path == "/api/credentials/inject":
            injects.append(json.loads(request.content)["files"][".env"])
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)  # pragma: no cover

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await svc._apply_pat_to_env(client, AGENT, f"http://agent-{AGENT}:8000", "ghp_new",
                                               add_if_missing=add_if_missing)
    return asyncio.run(run()), injects


@pytest.mark.parametrize("read_body", [
    {"files": {}, "files_b64": {".env": "//79"}, "missing": []},
    {"files": {}},
])
@pytest.mark.parametrize("add_if_missing", [True, False])
def test_an_unreadable_env_is_never_rewritten(monkeypatch, read_body, add_if_missing):
    status, injects = _apply(monkeypatch, read_body, add_if_missing=add_if_missing)
    assert status == "skipped_env_unreadable"
    assert injects == []


def test_a_text_env_is_patched_and_keeps_every_other_line(monkeypatch):
    status, (written,) = _apply(monkeypatch, {"files": {".env": ENV}}, add_if_missing=True)
    assert status == "updated"
    assert "OTHER_KEY=keep-me" in written and "SOME_SECRET=s3cr3t" in written
    assert re.search(r"^GITHUB_PAT=.*ghp_new", written, re.M)


def test_a_proven_absent_env_gets_the_line_only_when_adding_is_allowed(monkeypatch):
    absent = {"files": {}, "files_b64": {}, "missing": [".env"]}
    status, (written,) = _apply(monkeypatch, absent, add_if_missing=True)
    assert status == "updated" and re.search(r"^GITHUB_PAT=.*ghp_new", written, re.M)
    status, injects = _apply(monkeypatch, absent, add_if_missing=False)
    assert status == "skipped_no_pat" and injects == []


def _rotate(monkeypatch, *, remote_updated):
    """`propagate_github_pat` over one running agent whose `.env` the HTTP write
    skipped as unreadable; `remote_updated` is what `update_remote_pat` (the
    docker-exec path) reported."""
    from types import SimpleNamespace
    from models import AgentPropagationStatus
    from services import github_pat_propagation_service as svc
    monkeypatch.setattr(svc, "list_all_agents_fast",
                        lambda: [SimpleNamespace(name=AGENT, status="running")])
    monkeypatch.setattr(svc.db, "has_agent_github_pat", lambda name: False)

    async def propagate(name, pat, client):
        return AgentPropagationStatus(agent_name=name, status="skipped_env_unreadable",
                                      remote_updated=remote_updated)
    monkeypatch.setattr(svc, "_propagate_to_agent", propagate)
    return asyncio.run(svc.propagate_github_pat("ghp_new"))


def test_a_rotation_whose_token_landed_over_exec_counts_the_agent_as_reached(monkeypatch, caplog):
    """The `.env` HTTP write was skipped, but `update_remote_pat` placed the
    token and re-templated the remote: the agent is reached. Reporting it as
    skipped said "0 of N — keep the previous token until restarted" for a
    rotation that worked."""
    import logging
    with caplog.at_level(logging.WARNING):
        result = _rotate(monkeypatch, remote_updated=True)
    assert result.updated == [AGENT]
    assert result.remotes_updated == 1
    assert result.skipped == []
    assert not any("reached 0 of" in r.getMessage() for r in caplog.records)


def test_a_rotation_that_reached_nothing_reports_it_as_skipped(monkeypatch):
    result = _rotate(monkeypatch, remote_updated=False)
    assert result.updated == [] and result.remotes_updated == 0
    assert [a.status for a in result.skipped] == ["skipped_env_unreadable"]


def test_the_settings_page_names_the_skipped_state():
    """Settings names the state rather than reading it as "no GITHUB_PAT"."""
    vue = (_BACKEND.parent / "frontend" / "src" / "views" / "Settings.vue").read_text()
    assert "skipped_env_unreadable" in vue


# ----------------------------------------------- the docker-exec `.env` write

def _upsert(home: Path, *, mode=None):
    """Run the real `_ENV_PAT_UPSERT_SCRIPT` under bash against `home/.env`."""
    import os
    import subprocess
    from services.git_service import token_scrub
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "TRINITY_AGENT_HOME": str(home), "TRINITY_NEW_GITHUB_PAT": "ghp_new"}
    if mode is not None:
        (home / ".env").chmod(mode)
    try:
        return subprocess.run(["bash", "-c", token_scrub._ENV_PAT_UPSERT_SCRIPT], env=env,
                              capture_output=True, timeout=30).returncode
    finally:
        if mode is not None:
            (home / ".env").chmod(0o600)


@pytest.mark.parametrize("raw", [
    ENV.encode("utf-16"),                     # NUL bytes: grep used to print nothing
    b"OTHER_KEY=caf\xe9\nSOME_SECRET=s3cr3t\n",  # Latin-1, not UTF-8
    b"OTHER_KEY=a\x00b\nSOME_SECRET=s3cr3t\n",  # a stray NUL in otherwise ASCII text
])
def test_the_exec_write_refuses_a_file_that_is_not_text(tmp_path, raw):
    (tmp_path / ".env").write_bytes(raw)
    assert _upsert(tmp_path) == 6
    assert (tmp_path / ".env").read_bytes() == raw
    assert not list(tmp_path.glob(".env.trinity-ent615.*"))


def test_the_exec_write_never_turns_a_read_error_into_an_empty_file(tmp_path):
    import os
    if os.geteuid() == 0:
        pytest.skip("root reads a 0000 file; the error path needs an unprivileged user")
    (tmp_path / ".env").write_text(ENV)
    assert _upsert(tmp_path, mode=0o000) != 0
    assert (tmp_path / ".env").read_text() == ENV
    assert not list(tmp_path.glob(".env.trinity-ent615.*"))


def test_the_exec_write_keeps_every_other_line(tmp_path):
    (tmp_path / ".env").write_text("GITHUB_PAT=ghp_old\n" + ENV)
    assert _upsert(tmp_path) == 0
    lines = (tmp_path / ".env").read_text().splitlines()
    assert "OTHER_KEY=keep-me" in lines and "SOME_SECRET=s3cr3t" in lines
    assert [l for l in lines if l.startswith("GITHUB_PAT=")] == ["GITHUB_PAT=ghp_new"]


def test_the_exec_write_replaces_a_file_holding_only_the_token(tmp_path):
    """grep's exit 1 (no line kept) is not an error."""
    (tmp_path / ".env").write_text("GITHUB_PAT=ghp_old\n")
    assert _upsert(tmp_path) == 0
    assert (tmp_path / ".env").read_text() == "GITHUB_PAT=ghp_new\n"


# ------------------------------------------------------------------- the guard

def test_every_backend_env_reader_goes_through_the_rule():
    """A module that GETs `.env` from the agent's credentials-read route and
    writes it back must classify the answer with `agent_env_read`. The
    collapse this issue fixes was copied once already."""
    offenders = []
    for path in (_BACKEND / "services").rglob("*.py"):
        text = path.read_text()
        reads_env = "/api/credentials/read" in text and re.search(r"""paths["']?\s*[:=]\s*["']\.env["']""", text)
        writes = "/api/credentials/inject" in text
        if reads_env and writes and "agent_env_read" not in text:
            offenders.append(str(path.relative_to(_BACKEND)))
    assert offenders == [], offenders
