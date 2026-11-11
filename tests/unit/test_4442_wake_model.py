"""Platform-started turns run on the agent's own model (#4442).

Wakes (caller wake, ask answer / ending), events and unpinned reminders,
schedules and loops name no model, so `execute_task` resolves one through
`agent_model_service.resolve_default_model`, which reads
`agent_ownership.default_model`. Only a private module wrote that column, so on
every other agent those turns ran on the platform default while chat ran on the
agent's model. The fix fills the column the resolver reads:

* Settings → Model (`PUT /api/agents/{name}/model`) stores what the agent accepted;
* creation seeds it from the template's declared model (`runtime.model`, or a
  top-level `model:`), on the `local:` and the `github:` path;
* a boot pass fills it, where still NULL, from the container's env or the
  agent's own template.yaml;
* push `/chat` resolves the same model in the backend and records it.

The doubles keep two stores apart: the agent's (container env / template /
in-memory) model and the column are separate variables, never derived from
each other.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import sys
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit

DEFAULT = "claude-sonnet-4-6"
OWN = "claude-opus-5-5"
PINNED = "claude-opus-5"
SECRET = "sk-test-4442-do-not-log"


# --------------------------------------------------------------------------- #
# A real (tmp) database: the column the resolver reads
# --------------------------------------------------------------------------- #
@pytest.fixture()
def ops(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-4442.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))
    from db.engine import get_engine
    from db.tables import metadata, agent_ownership
    metadata.create_all(get_engine(), tables=[agent_ownership])
    from sqlalchemy import insert
    with get_engine().begin() as conn:
        for name in ("blank", "env-only", "template-only", "preset", "codex", "nothing", "stopped"):
            conn.execute(insert(agent_ownership).values(agent_name=name, owner_id=1))
    from db.agents import AgentOperations
    return AgentOperations.__new__(AgentOperations)


def _resolved(ops, name):
    from services.agent_model_service import resolve_default_model
    return resolve_default_model(
        name, runtime="claude-code", db_ops=ops,
        settings=SimpleNamespace(get_platform_default_model=lambda: DEFAULT)).model


# --------------------------------------------------------------------------- #
# What a template declares
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("data,expected", [
    ({"runtime": {"type": "claude-code", "model": OWN}}, OWN),
    ({"model": PINNED}, PINNED),                                   # top-level `model:`
    ({"runtime": {"type": "claude-code"}, "model": PINNED}, PINNED),
    ({"runtime": "claude-code", "model": f"  {PINNED} "}, PINNED),
    ({"runtime": {"model": OWN}, "model": PINNED}, OWN),           # runtime.model first
    ({"name": "x"}, None),
    ({"model": ""}, None),
    ({"model": {"id": OWN}}, None),
    (None, None),
    ("model: x", None),
])
def test_declared_model(data, expected):
    from services.agent_model_seed import declared_model
    assert declared_model(data) == expected


# --------------------------------------------------------------------------- #
# The create seed
# --------------------------------------------------------------------------- #
def _register(ops, *, runtime_model=None, runtime="claude-code", declared=None, template=None):
    from services.agent_service import crud
    config = SimpleNamespace(
        name="blank", runtime_model=runtime_model, runtime=runtime, ephemeral=None,
        display_label=None)
    user = SimpleNamespace(agent_name=None, username="owner", id=1)
    fake_db = MagicMock()
    fake_db.set_default_model_if.side_effect = ops.set_default_model_if
    fake_db.grant_default_permissions.return_value = 0
    with patch.object(crud, "db", fake_db), \
            patch.object(crud, "get_agent_default_require_email", lambda: True):
        crud._register_agent(config, user, template or {}, None, None, declared_model=declared)
    return fake_db


def test_an_agent_created_from_a_template_that_declares_model_gets_it(ops):
    """The P4 case: a template declaring `model: claude-opus-5` (no
    `runtime.model`, so the container env stays empty) seeds the column, and a
    wake then resolves to it instead of the platform default."""
    from services.agent_model_seed import declared_model
    assert _resolved(ops, "blank") == DEFAULT
    _register(ops, declared=declared_model({"name": "scout", "model": PINNED}))
    assert ops.get_default_model_raw("blank") == PINNED
    assert _resolved(ops, "blank") == PINNED


def test_the_request_runtime_model_seeds_ahead_of_the_declared_one(ops):
    _register(ops, runtime_model=OWN, declared=PINNED)
    assert ops.get_default_model_raw("blank") == OWN


def test_the_seed_never_overwrites_another_writers_value(ops):
    ops.set_default_model("blank", "claude-fable-5-1")
    _register(ops, declared=PINNED)
    assert ops.get_default_model_raw("blank") == "claude-fable-5-1"


@pytest.mark.parametrize("kw", [
    {"declared": "claude-opus-9"},                       # not in the catalog
    {"declared": "--dangerously-skip-permissions"},
    {"declared": None},
    {"declared": "gpt-5.1-codex", "runtime": "codex"},   # a non-Claude agent
    {"declared": PINNED, "runtime": "codex"},
])
def test_no_seed_for_an_unrunnable_blank_or_non_claude_model(ops, kw):
    _register(ops, **kw)
    assert ops.get_default_model_raw("blank") is None


def test_both_template_branches_feed_the_seed():
    """Wiring pin on top of the executed tests above: the `local:` and
    `github:` resolvers each set `declared_model`, and registration passes it."""
    src = (_BACKEND / "services" / "agent_service" / "crud.py").read_text()
    tree = ast.parse(src)
    fed = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
           and any(isinstance(t, ast.Attribute) and t.attr == "declared_model" for t in n.targets)]
    args = sorted(ast.unparse(n.value.args[0]) for n in fed)
    assert args == ["source_metadata", "tr.template_data"]
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "_register_agent"]
    assert calls and all(any(k.arg == "declared_model" and ast.unparse(k.value) == "tr.declared_model"
                             for k in c.keywords) for c in calls)


def test_the_local_resolver_reads_a_top_level_model(tmp_path, monkeypatch):
    """Executes the `local:` branch's parse on a real template.yaml."""
    from services.agent_service import crud
    from services.agent_model_seed import declared_model
    tdir = tmp_path / "scout"
    tdir.mkdir()
    (tdir / "template.yaml").write_text("name: scout\nmodel: claude-opus-5\n")
    monkeypatch.setattr(crud, "_resolve_local_template_dir", lambda raw: tdir)
    config = crud.AgentConfig(name="m1", template="local:scout")
    data, _ = crud._resolve_local_template(config)
    assert declared_model(data) == PINNED
    assert not config.runtime_model     # why the container env stays empty


# --------------------------------------------------------------------------- #
# The boot fill
# --------------------------------------------------------------------------- #
AGENTS = [
    ("env-only", "claude-code", "running"),
    ("template-only", "claude-code", "running"),
    ("preset", "claude-code", "running"),
    ("codex", "codex", "running"),
    ("nothing", "claude-code", "running"),
    ("stopped", "claude-code", "exited"),
]
ENV = {"env-only": OWN, "stopped": None}
TEMPLATES = {"template-only": {"name": "scout", "model": PINNED}, "stopped": {"model": OWN}}


def _fill(ops, *, env=None, templates=None, agents=None, live=None):
    from services.agent_model_seed import fill_blank_default_models
    env = ENV if env is None else env
    templates = TEMPLATES if templates is None else templates
    live = {} if live is None else live
    reads = []

    async def read_live(name):
        reads.append(("live", name))
        value = live.get(name)
        if isinstance(value, Exception):
            raise value
        return value

    def read_env(name):
        reads.append(("env", name))
        value = env.get(name)
        if isinstance(value, Exception):
            raise value
        return value

    async def read_template(name):
        reads.append(("template", name))
        return templates.get(name)

    counts = asyncio.run(fill_blank_default_models(
        db_ops=ops, list_agents=lambda: agents or AGENTS,
        read_env_model=read_env, read_template=read_template, read_live_model=read_live))
    return counts, reads


def test_boot_fill_takes_the_running_agents_live_model_over_its_env(ops):
    """A Settings pick made before #4442 lived only in the agent's memory; the
    env still holds the template's model. The live value wins."""
    counts, reads = _fill(ops, live={"env-only": "claude-fable-5-1", "template-only": OWN})
    assert ops.get_default_model_raw("env-only") == "claude-fable-5-1"
    assert ops.get_default_model_raw("template-only") == OWN
    assert ("env", "env-only") not in reads and ("template", "template-only") not in reads
    assert ("live", "stopped") not in reads   # a stopped agent is not asked
    assert counts["filled"] == 2


def test_boot_fill_falls_back_to_env_then_template_when_the_agent_does_not_answer(ops):
    """`_read_live_model` answers None for an unreachable agent; the env, then
    the template, still fill the column."""
    counts, reads = _fill(ops, live={"env-only": None, "template-only": None})
    assert ops.get_default_model_raw("env-only") == OWN
    assert ops.get_default_model_raw("template-only") == PINNED
    assert reads.index(("live", "env-only")) < reads.index(("env", "env-only"))
    assert ("env", "template-only") in reads and ("template", "template-only") in reads
    assert counts["filled"] == 2


def test_boot_fill_keeps_the_allowlist_and_fill_blank_guards_on_the_live_model(ops):
    ops.set_default_model("preset", "claude-fable-5-1")
    counts, reads = _fill(ops, live={"env-only": "claude-opus-9; rm -rf /", "preset": OWN})
    assert ops.get_default_model_raw("env-only") is None and counts["refused"] == 1
    assert ops.get_default_model_raw("preset") == "claude-fable-5-1"
    assert ("live", "preset") not in reads


def test_read_live_model_asks_the_agents_own_model_route(monkeypatch):
    from services import agent_model_seed
    import services.agent_client as agent_client
    asked = []

    class Client:
        def __init__(self, answer):
            self.answer = answer

        async def get(self, path, timeout=None):
            asked.append(path)
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

    for answer, expected in [
        (httpx.Response(200, json={"model": f" {OWN} "}), OWN),
        (httpx.Response(200, json={"model": None}), None),
        (httpx.Response(503, json={"model": OWN}), None),
        (httpx.ConnectError("refused"), None),
    ]:
        monkeypatch.setattr(agent_client, "get_agent_client", lambda n, a=answer: Client(a))
        assert asyncio.run(agent_model_seed._read_live_model("agent-a")) == expected
    assert set(asked) == {"/api/model"}


def test_boot_fill_writes_env_then_template_models_into_blank_columns(ops):
    ops.set_default_model("preset", "claude-fable-5-1")
    counts, reads = _fill(ops)
    assert ops.get_default_model_raw("env-only") == OWN
    assert ops.get_default_model_raw("template-only") == PINNED   # the P4 case
    assert ops.get_default_model_raw("preset") == "claude-fable-5-1"
    assert ops.get_default_model_raw("codex") is None
    assert ops.get_default_model_raw("nothing") is None
    assert ops.get_default_model_raw("stopped") is None   # no live template read
    assert ("template", "stopped") not in reads
    assert not [r for r in reads if r[1] in ("preset", "codex")]
    assert counts["filled"] == 2 and counts["nothing_declared"] == 2
    assert _resolved(ops, "template-only") == PINNED


def test_boot_fill_is_one_write_across_racing_workers(ops):
    first, _ = _fill(ops)
    second, reads = _fill(ops)
    assert first["filled"] == 2 and second["filled"] == 0
    assert ("env", "env-only") not in reads


def test_two_concurrent_seeds_make_exactly_one_write(ops):
    from services.agent_model_seed import seed_default_model
    assert seed_default_model("blank", OWN, db_ops=ops) is True
    assert seed_default_model("blank", PINNED, db_ops=ops) is False
    assert ops.get_default_model_raw("blank") == OWN


def test_boot_fill_skips_an_unreadable_or_unrunnable_agent(ops):
    counts, _ = _fill(ops, env={"env-only": RuntimeError("docker gone")},
                      templates={"template-only": {"model": "claude-opus-9"}})
    assert ops.get_default_model_raw("env-only") is None
    assert ops.get_default_model_raw("template-only") is None
    assert counts["failed"] == 1 and counts["refused"] == 1


def test_read_runtime_model_returns_one_key_and_never_logs_the_env(caplog):
    from services import docker_service
    env = [f"ANTHROPIC_API_KEY={SECRET}", f"AGENT_RUNTIME_MODEL= {OWN} ", "TEMPLATE_NAME=x"]
    container = SimpleNamespace(attrs={"Config": {"Env": env}})
    with caplog.at_level(logging.DEBUG), \
            patch.object(docker_service, "get_agent_container", lambda n: container):
        assert docker_service.read_runtime_model("a") == OWN

    class Exploding:
        @property
        def attrs(self):
            raise RuntimeError(f"inspect failed: ANTHROPIC_API_KEY={SECRET}")

    with caplog.at_level(logging.DEBUG), \
            patch.object(docker_service, "get_agent_container", lambda n: Exploding()):
        assert docker_service.read_runtime_model("a") is None
    with patch.object(docker_service, "get_agent_container",
                      lambda n: SimpleNamespace(attrs={"Config": {"Env": ["AGENT_RUNTIME_MODEL="]}})):
        assert docker_service.read_runtime_model("a") is None
    assert SECRET not in caplog.text
    assert all(not r.exc_info for r in caplog.records)


def test_boot_fill_is_scheduled_from_the_real_boot_phase():
    """`main._schedule_staggered_services` calls the scheduler (AST over the
    composition root: importing main costs Docker side effects)."""
    tree = ast.parse((_BACKEND / "main.py").read_text())
    phase = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                 and n.name == "_schedule_staggered_services")
    calls = [ast.unparse(n.func) for n in ast.walk(phase) if isinstance(n, ast.Call)]
    assert "_agent_model_seed_boot.schedule_boot_fill" in calls


def test_schedule_boot_fill_runs_the_fill(monkeypatch):
    from services import agent_model_seed
    ran = []

    async def fake_fill():
        ran.append(True)

    monkeypatch.setattr(agent_model_seed, "_BOOT_DELAY_S", 0)
    monkeypatch.setattr(agent_model_seed, "fill_blank_default_models", fake_fill)

    async def main():
        agent_model_seed.schedule_boot_fill()
        await agent_model_seed._boot_task

    asyncio.run(main())
    assert ran == [True]
    agent_model_seed.schedule_boot_fill()  # no running loop: skipped, never raises


# --------------------------------------------------------------------------- #
# Settings → Model persists (PUT /api/agents/{name}/model)
# --------------------------------------------------------------------------- #
def _agent(handler):
    @asynccontextmanager
    async def client(name, *a, **k):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            yield c
    return client


def _put(handler, *, model=OWN, runtime="claude-code", saved=True, db_exc=None):
    import routers.chat as chat
    from models import ModelChangeRequest

    container = SimpleNamespace(status="running", labels={"trinity.agent-runtime": runtime})
    user = SimpleNamespace(id=1, username="owner", role="user")
    fake_db = MagicMock()
    fake_db.set_default_model.side_effect = db_exc or (lambda n, m: saved)
    audit = MagicMock(log=AsyncMock())
    with patch.object(chat, "agent_httpx_client", _agent(handler)), \
            patch.object(chat, "get_agent_container", lambda name: container), \
            patch.object(chat, "db", fake_db), \
            patch.object(chat, "platform_audit_service", audit):
        try:
            result = asyncio.run(chat.set_agent_model(
                ModelChangeRequest(model=model), name="agent-a", current_user=user))
        except HTTPException as e:
            result = e
    return result, fake_db, audit


def _ok(request):
    return httpx.Response(200, json={"model": "accepted"})


def test_settings_model_is_stored_and_a_wake_then_runs_on_it(ops):
    """Settings then wake: the value the agent accepted reaches the column the
    wake's resolver reads."""
    import routers.chat as chat
    from models import ModelChangeRequest
    ops.set_default_model("blank", None)
    container = SimpleNamespace(status="running", labels={})
    with patch.object(chat, "agent_httpx_client", _agent(_ok)), \
            patch.object(chat, "get_agent_container", lambda name: container), \
            patch.object(chat, "db", ops), \
            patch.object(chat, "platform_audit_service", MagicMock(log=AsyncMock())):
        asyncio.run(chat.set_agent_model(ModelChangeRequest(model=OWN), name="blank",
                                         current_user=SimpleNamespace(id=1, username="o")))
    assert _resolved(ops, "blank") == OWN


def test_put_stores_the_accepted_model_and_audits_it():
    result, fake_db, audit = _put(_ok)
    assert result == {"model": "accepted"}
    fake_db.set_default_model.assert_called_once_with("agent-a", OWN)
    kw = audit.log.call_args.kwargs
    assert kw["event_action"] == "update_agent_model" and kw["details"] == {"default_model": OWN}


def test_put_stores_nothing_when_the_agent_refuses():
    result, fake_db, audit = _put(lambda r: httpx.Response(400, json={"detail": "Invalid model"}),
                                  model="invalid-model-xyz")
    assert isinstance(result, HTTPException) and result.status_code == 400
    fake_db.set_default_model.assert_not_called()
    audit.log.assert_not_called()


@pytest.mark.parametrize("kw", [{"saved": False}, {"db_exc": RuntimeError("db locked")}])
def test_put_names_a_failed_save(kw):
    result, _, audit = _put(_ok, **kw)
    assert isinstance(result, HTTPException) and result.status_code == 500
    assert result.detail["error"] == "model_not_saved"
    audit.log.assert_not_called()


def _stateful_agent(model):
    """An agent that holds its model in memory, as the agent server does."""
    state = {"model": model, "puts": []}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"model": state["model"]})
        import json
        state["model"] = json.loads(request.content)["model"]
        state["puts"].append(state["model"])
        return httpx.Response(200, json={"model": state["model"]})
    return state, handler


def test_a_failed_save_puts_the_agents_previous_model_back(ops):
    """The agent accepts the new model, the store fails: the agent is put back
    on its previous model and the stored value is unchanged, so nothing is
    left half-set and the message names the model it keeps running."""
    import routers.chat as chat
    from models import ModelChangeRequest
    ops.set_default_model("preset", PINNED)
    state, handler = _stateful_agent(PINNED)

    def failing_store(name, model):
        raise RuntimeError("db locked")

    container = SimpleNamespace(status="running", labels={})
    with patch.object(chat, "agent_httpx_client", _agent(handler)), \
            patch.object(chat, "get_agent_container", lambda name: container), \
            patch.object(chat, "db", SimpleNamespace(set_default_model=failing_store)), \
            patch.object(chat, "platform_audit_service", MagicMock(log=AsyncMock())):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(chat.set_agent_model(ModelChangeRequest(model=OWN), name="preset",
                                             current_user=SimpleNamespace(id=1, username="o")))
    assert exc.value.status_code == 500 and exc.value.detail["error"] == "model_not_saved"
    assert exc.value.detail["restored"] is True
    assert state["puts"] == [OWN, PINNED] and state["model"] == PINNED
    assert ops.get_default_model_raw("preset") == PINNED
    assert f"keeps running {PINNED}" in exc.value.detail["message"]
    assert f"now runs {OWN}" not in exc.value.detail["message"]


def test_a_failed_save_that_cannot_be_put_back_says_so():
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"model": PINNED})
        if len(calls) > 2:  # the put-back
            return httpx.Response(500, json={"detail": "boom"})
        return httpx.Response(200, json={"model": OWN})

    result, _, _ = _put(handler, saved=False)
    assert isinstance(result, HTTPException) and result.detail["restored"] is False
    assert "could not be put back" in result.detail["message"]
    assert calls == ["GET", "PUT", "PUT"]


def test_put_on_a_non_claude_agent_stores_nothing():
    result, fake_db, _ = _put(_ok, model="gpt-5.1-codex", runtime="codex")
    assert result == {"model": "accepted"}
    fake_db.set_default_model.assert_not_called()


# --------------------------------------------------------------------------- #
# Push /chat resolves and records the model
# --------------------------------------------------------------------------- #
def _payload(request_model, stored, *, runtime="claude-code"):
    from services import chat_execution_service as ces
    from models import ChatMessageRequest

    fake_db = MagicMock()
    fake_db.get_default_model_raw.return_value = stored
    user = SimpleNamespace(email="o@example.com", username="o", mcp_key_name=None)
    import services.agent_model_service as ams
    import services.docker_service as ds
    with patch.object(ces, "db", fake_db), \
            patch.object(ds, "get_agent_runtime", lambda n: runtime), \
            patch("database.db", fake_db), \
            patch.object(ams, "_runtime_of", lambda n: runtime), \
            patch("services.settings_service.get_platform_default_model", lambda: DEFAULT), \
            patch("services.task_execution_service.auto_memory_off_for", lambda *a: False):
        payload = ces.build_chat_payload(
            name="agent-a", request=ChatMessageRequest(message="hi", model=request_model),
            triggered_by="mcp", current_user=user, x_source_agent=None,
            task_execution_id="exec-4442")
    return payload, fake_db


def test_push_chat_naming_no_model_runs_and_records_the_agents_own_model():
    payload, fake_db = _payload(None, OWN)
    assert payload["model"] == OWN
    fake_db.stamp_model_used_if_null.assert_called_once_with("exec-4442", OWN)


def test_push_chat_naming_a_model_keeps_it():
    payload, fake_db = _payload("haiku", OWN)
    assert payload["model"] == "haiku"
    fake_db.stamp_model_used_if_null.assert_called_once_with("exec-4442", "haiku")


def test_push_chat_leaves_a_stored_model_outside_the_catalog_to_the_agent():
    # Settings accepts any claude-* id the agent takes; the catalog is narrower.
    # Sending the platform default here would move chat off the agent's pick.
    payload, fake_db = _payload(None, "claude-sonnet-4-6[1m]")
    assert "model" not in payload
    fake_db.stamp_model_used_if_null.assert_not_called()


def test_push_chat_with_no_stored_model_leaves_the_model_to_the_agent():
    payload, fake_db = _payload(None, None)
    assert "model" not in payload
    fake_db.stamp_model_used_if_null.assert_not_called()


def test_push_chat_on_a_non_claude_agent_names_no_model():
    payload, fake_db = _payload(None, OWN, runtime="codex")
    assert "model" not in payload
    fake_db.stamp_model_used_if_null.assert_not_called()


# --------------------------------------------------------------------------- #
# execute_task, per platform-started kind: the column decides an unnamed
# model, an explicit one wins and is never restamped
# --------------------------------------------------------------------------- #
@contextmanager
def _wire(stored):
    from services.task_execution_service import TaskExecutionService

    seams = TaskExecutionService.execute_task.__globals__
    mock_db = MagicMock()
    mock_db.get_max_parallel_tasks.return_value = 3
    mock_db.get_execution_timeout.return_value = 900
    mock_db.get_execution.return_value = MagicMock(id="exec-4442", status="running")
    mock_db.update_execution_status.return_value = True
    mock_db.get_default_model_raw.return_value = stored
    mock_db.create_task_execution.return_value = MagicMock(id="exec-new")
    capacity = MagicMock()
    capacity.acquire = AsyncMock(return_value=MagicMock(state="admitted"))
    capacity.release = AsyncMock()
    circuit = MagicMock()
    circuit.allow_request.return_value = True
    payloads: list[dict] = []

    async def _post(agent_name, endpoint, payload, **kwargs):
        payloads.append(dict(payload))
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"response": "ok", "metadata": {}}
        resp.text = "ok"
        return resp

    import config
    with (
        patch.object(config, "DISPATCH_ASYNC", False),
        patch.dict(seams, {
            "db": mock_db,
            "settings_service": SimpleNamespace(get_platform_default_model=lambda: DEFAULT),
            "_resolve_agent_runtime": lambda name: "claude-code",
            "get_capacity_manager": lambda: capacity,
            "activity_service": MagicMock(track_activity=AsyncMock(return_value="a"),
                                          complete_activity=AsyncMock()),
            "CircuitState": lambda *a, **k: circuit,
            "agent_post_with_retry": _post,
            "dispatch_breaker_active": lambda *a, **k: False,
            "_record_dispatch_terminal": AsyncMock(),
            "platform_audit_service": MagicMock(log=AsyncMock()),
        }),
    ):
        yield SimpleNamespace(db=mock_db, payloads=payloads)


KINDS = ["caller_wake", "operator_response", "operator_ending", "reminder", "schedule",
         "event", "loop", "a2a", "fan_out", "room", "retry"]


@pytest.mark.parametrize("kind", KINDS)
def test_each_platform_started_kind_runs_on_the_agents_own_model(kind):
    from services.task_execution_service import TaskExecutionService
    with _wire(OWN) as w:
        asyncio.run(TaskExecutionService().execute_task(
            agent_name="agent-4442", message="wake", triggered_by=kind))
    assert w.db.create_task_execution.call_args.kwargs["fields"].model_used == OWN
    assert w.payloads and w.payloads[0].get("model") == OWN


@pytest.mark.parametrize("kind", ["reminder", "schedule", "loop"])
def test_a_pinned_model_wins_and_is_never_restamped(kind):
    from services.task_execution_service import TaskExecutionService
    with _wire(OWN) as w:
        asyncio.run(TaskExecutionService().execute_task(
            agent_name="agent-4442", message="wake", triggered_by=kind,
            execution_id="exec-4442", model=PINNED))
    w.db.stamp_model_used_if_null.assert_not_called()
    assert w.payloads[0].get("model") == PINNED


# --------------------------------------------------------------------------- #
# Bypass guard: the wake producers never name a model of their own
# --------------------------------------------------------------------------- #
PRODUCERS = [
    "services/caller_wake_service.py",
    "services/operator_resume_service.py",
    "services/event_dispatch_service.py",
]


@pytest.mark.parametrize("rel", PRODUCERS)
def test_wake_producers_pass_no_model(rel):
    """They must reach `execute_task` with no model, so the resolver decides.
    Catches `model=` keywords, a `"model"` dict key and a `**{...}` carrying it."""
    tree = ast.parse((_BACKEND / rel).read_text())
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "model":
            hits.append(node.lineno)
        if isinstance(node, ast.Dict):
            hits += [k.lineno for k in node.keys
                     if isinstance(k, ast.Constant) and k.value == "model"]
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
                and node.slice.value == "model" and isinstance(node.ctx, ast.Store):
            hits.append(node.lineno)
    assert not hits, f"{rel} names a model at lines {hits}"
