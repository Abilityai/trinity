"""Pull claim envelope ↔ push drain parity on `backlog_metadata` (#2317).

`backlog_service.enqueue` is the ONLY producer of a queued row's
`backlog_metadata`, and it writes every per-task setting as a **flat** key. The
push drain (`BacklogService._spawn_drain`) reads them flat and rebuilds the
request faithfully. The pull claim envelope
(`pull_coordination_service._build_claim_response`) used to read a **nested**
`task_overrides` object instead — a key no producer has ever written — so
`overrides` was always `{}` and every pulled turn silently ran with agent/global
defaults instead of the row's `model`, `allowed_tools`, `max_turns` and
`timeout_seconds`. Session identity had the same shape of bug one line earlier
(`meta["session_id"]`, versus the `resume_session_id` the producer writes).

The divergence was invisible because the two consumers were only ever tested
against **hand-written** metadata fixtures shaped like whatever the consumer
under test happened to read. So the guard here is structural, and every test in
this file runs off ONE shared fixture that the **real producer** built:

  * `test_producer_keys_are_exhaustively_classified` — every key `enqueue`
    writes is either runtime-facing (must ride the wire) or backend-only
    (deliberately does not). A new key in `enqueue` fails here until someone
    classifies it, which is the moment to ask whether the pull path needs it.
  * `test_pull_envelope_and_push_drain_apply_the_same_settings` — both consumers
    resolve the SAME producer keys to the SAME values.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any, Dict
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# sys.modules hygiene (Issue #762)
# ---------------------------------------------------------------------------
# This file drives the REAL producer + BOTH real consumers, which means it
# imports backend service modules and installs two throwaway stubs
# (`database`, `services.chat_execution_service`) while doing it. Backend
# modules bind `from database import db` AT IMPORT, so a service imported for
# the first time while the stub is installed keeps the stub's fake FOREVER —
# which is how an early version of this file reddened
# `test_1081_pull_endpoints.py` under some pytest-randomly seeds (its service
# tests got a `_FakeDb` with no `get_execution_timeout`). Two guards, both
# needed:
#   1. `_import_backend_modules()` imports every module this file touches
#      BEFORE any stub goes in, so nothing binds a fake.
#   2. this snapshot/restore pair rolls sys.modules back after every test, so
#      a module this file imported first is handed back to its owner
#      un-imported (the db-harness files re-import them against their engine).
# Precedent for the named pair: tests/unit/test_telegram_webhook_backfill.py,
# recognised by tests/lint_sys_modules.py.
_STUBBED_MODULE_NAMES = [
    "database",
    "models",
    "services.backlog_service",
    "services.chat_execution_service",
    "services.pull_coordination_service",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _import_backend_modules():
    """Import (and cache) every backend module this file drives, BEFORE any
    stub is installed — see the note above. Returns the two entry points."""
    from models import ParallelTaskRequest                       # noqa: F401
    from services import pull_coordination_service               # noqa: F401
    from services.backlog_service import BacklogService

    return ParallelTaskRequest, BacklogService


# ---------------------------------------------------------------------------
# The producer key classification (#2317)
# ---------------------------------------------------------------------------
# Keys that describe the TURN and must reach the runtime. Both paths carry them:
# push via the rebuilt ParallelTaskRequest, pull via the claim envelope.
_RUNTIME_KEYS = (
    "message",
    "model",
    "allowed_tools",
    "system_prompt",
    "timeout_seconds",
    "max_turns",
    "resume_session_id",
)

# Keys that are BACKEND-side concerns and reach no agent on either path:
# chat-session persistence + result injection (applied by
# `chat_execution_service.run_async_task` after the turn returns; the pull sink's
# equivalent is `pull_coordination_service.apply_task_result`) and the
# provenance/identity block. `chat_session_id` belongs here and NOT in
# `_RUNTIME_KEYS`: it is a Trinity `chat_sessions` row id, not the Claude Code
# session UUID the envelope's `session_id` means — handing it to the runtime
# would resume the wrong session.
_BACKEND_ONLY_KEYS = (
    "save_to_session",
    "user_message",
    "create_new_session",
    "chat_session_id",
    "inject_result",
    "user_id",
    "user_email",
    "subscription_id",
    "x_source_agent",
    "x_mcp_key_id",
    "x_mcp_key_name",
    "triggered_by",
    "collaboration_activity_id",
    "is_self_task",
    "self_task_activity_id",
)


# ---------------------------------------------------------------------------
# The shared fixture — built by the REAL producer, not by hand
# ---------------------------------------------------------------------------
class _FakeDb:
    """Minimal stand-in for the `database.db` singleton `enqueue` late-imports."""

    def __init__(self) -> None:
        self.queued: Dict[str, str] = {}

    def get_queued_count(self, agent_name):        # noqa: D102 - fake
        return 0

    def get_max_backlog_depth(self, agent_name):   # noqa: D102 - fake
        return 50

    def update_execution_to_queued(self, execution_id, metadata, queued_at):
        self.queued[execution_id] = metadata
        return True


@pytest.fixture
def enqueued_metadata(monkeypatch) -> Dict[str, Any]:
    """The `backlog_metadata` blob the real `BacklogService.enqueue` writes for a
    request with EVERY per-task setting populated, parsed back to a dict.

    This is the single source the two consumers are compared against — a
    hand-written fixture is exactly what let #2317 hide.
    """
    ParallelTaskRequest, BacklogService = _import_backend_modules()

    # Only NOW may the fake go in: `enqueue` late-imports `from database import
    # db` per call, so it picks the fake up, while every module already imported
    # above keeps the real singleton.
    fake_db = _FakeDb()
    monkeypatch.setitem(sys.modules, "database", types.SimpleNamespace(db=fake_db))

    request = ParallelTaskRequest(
        message="run the weekly recon and report",
        model="opus",
        allowed_tools=["mcp__trinity__report", "Read"],
        system_prompt="CALLER-PROMPT",
        timeout_seconds=90,
        max_turns=7,
        async_mode=True,
        save_to_session=True,
        user_message="original user text",
        create_new_session=True,
        chat_session_id="chat-session-row-42",
        resume_session_id="7f0d5a1e-1111-4222-8333-claudesession",
        inject_result=True,
    )
    ok = asyncio.run(
        BacklogService().enqueue(
            agent_name="alpha",
            execution_id="exec-2317",
            request=request,
            effective_timeout=90,
            user_id=7,
            user_email="u@example.com",
            subscription_id="sub-1",
            x_source_agent="beta",
            x_mcp_key_id="key-1",
            x_mcp_key_name="key-name",
            triggered_by="agent",
            collaboration_activity_id="act-1",
            is_self_task=False,
            self_task_activity_id=None,
        )
    )
    assert ok is True
    return json.loads(fake_db.queued["exec-2317"])


# ---------------------------------------------------------------------------
# Consumers, driven off that one fixture
# ---------------------------------------------------------------------------
def _claim_payload(meta: Dict[str, Any]) -> Dict[str, Any]:
    """The §3.1 envelope payload the PULL path serves for this metadata.

    `compose_system_prompt` is stubbed to the identity on `caller_prompt` so the
    parity comparison sees the caller's prompt rather than the platform preamble
    (the composition itself is covered by `test_1081_pull_endpoints.py`).
    """
    _import_backend_modules()
    from services import pull_coordination_service as pcs

    row = {
        "id": "exec-2317",
        "agent_name": "alpha",
        "message": meta["message"],
        "backlog_metadata": json.dumps(meta),
        "triggered_by": meta.get("triggered_by"),
        "model_used": None,
        "source_agent_name": meta.get("x_source_agent"),
        "source_user_id": meta.get("user_id"),
        "lease_expires_at": "2026-08-19T12:00:00+00:00",
        "claim_token": "tok-abc",
        "claimed_by_worker": "alpha#w1",
        "redelivery_count": 0,
    }
    with patch.object(pcs, "_resolve_agent_runtime", return_value="claude-code"), \
         patch.object(pcs, "is_execution_context_enabled", return_value=False), \
         patch.object(pcs, "compose_system_prompt",
                      side_effect=lambda **kw: kw["caller_prompt"]):
        claim = pcs._build_claim_response(row)
    return claim["envelope"]["payload"]


def _drain_request(meta: Dict[str, Any], monkeypatch):
    """The `ParallelTaskRequest` the PUSH drain rebuilds for this metadata.

    `run_async_task` is stubbed in `sys.modules` (the drain late-imports it) so
    nothing is dispatched; we only want the reconstructed request.
    """
    _, BacklogService = _import_backend_modules()

    captured: Dict[str, Any] = {}

    def _fake_run_async_task(**kwargs):
        captured.update(kwargs)

        async def _noop():
            return None

        return _noop()

    stub = types.ModuleType("services.chat_execution_service")
    stub.run_async_task = _fake_run_async_task
    monkeypatch.setitem(sys.modules, "services.chat_execution_service", stub)

    async def _drive():
        await BacklogService()._spawn_drain("alpha", "exec-2317", meta)
        await asyncio.sleep(0)  # let the spawned task run to completion

    asyncio.run(_drive())
    assert captured, "_spawn_drain never called run_async_task"
    return captured["request"]


def _applied_by_pull(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Producer key → value as the PULL path applies it."""
    payload = _claim_payload(meta)
    overrides = payload["task_overrides"]
    return {
        "message": payload["message"],
        "model": overrides.get("model"),
        "allowed_tools": overrides.get("allowed_tools"),
        "system_prompt": overrides.get("system_prompt"),
        "timeout_seconds": overrides.get("timeout_seconds"),
        "max_turns": overrides.get("max_turns"),
        # The envelope's session_id IS the resume target (§2 shared fields); the
        # worker feeds it to execute_headless(resume_session_id=...).
        "resume_session_id": payload["session_id"],
    }


def _applied_by_push(meta: Dict[str, Any], monkeypatch) -> Dict[str, Any]:
    """Producer key → value as the PUSH drain applies it."""
    request = _drain_request(meta, monkeypatch)
    return {
        "message": request.message,
        "model": request.model,
        "allowed_tools": request.allowed_tools,
        "system_prompt": request.system_prompt,
        "timeout_seconds": request.timeout_seconds,
        "max_turns": request.max_turns,
        "resume_session_id": request.resume_session_id,
    }


# ===========================================================================
# Structural guards
# ===========================================================================
class TestProducerKeyClassification:
    def test_producer_keys_are_exhaustively_classified(self, enqueued_metadata):
        """Every key `enqueue` writes is either runtime-facing or backend-only.

        A new key in `backlog_service.enqueue` reds this test until it is added
        to one of the two tuples — which is the point at which someone has to
        decide whether the pull envelope must carry it. Without this, a setting
        can be added to the producer and reach the push path only, silently
        (that is exactly how #2317 shipped).
        """
        assert set(enqueued_metadata) == set(_RUNTIME_KEYS) | set(_BACKEND_ONLY_KEYS)

    def test_envelope_override_keys_are_real_producer_keys(self, enqueued_metadata):
        """The envelope's source list names keys the producer actually writes —
        a typo (or a nested-only key like the `task_overrides` #2317 fixed) would
        resolve to nothing and drop the setting silently."""
        _import_backend_modules()
        from services import pull_coordination_service as pcs

        assert set(pcs._TASK_OVERRIDE_KEYS) <= set(enqueued_metadata)
        # `message` + `resume_session_id` ride the payload itself, not the
        # overrides sub-object; together they are the full runtime-facing set.
        assert set(pcs._TASK_OVERRIDE_KEYS) | {"message", "resume_session_id"} == set(
            _RUNTIME_KEYS
        )


class TestPullPushParity:
    def test_pull_envelope_and_push_drain_apply_the_same_settings(
        self, enqueued_metadata, monkeypatch
    ):
        """THE #2317 guard: one producer-built fixture, two consumers, identical
        result. Pre-fix the pull side was
        `{model: None, allowed_tools: None, max_turns: None, timeout_seconds: None,
        resume_session_id: None}` against a fully-populated push side."""
        pull = _applied_by_pull(enqueued_metadata)
        push = _applied_by_push(enqueued_metadata, monkeypatch)
        assert pull == push

    def test_both_paths_apply_the_producers_recorded_values(
        self, enqueued_metadata, monkeypatch
    ):
        """Stronger than equality: both consumers agree WITH THE PRODUCER, so the
        pair cannot drift together into agreeing on the wrong thing."""
        expected = {key: enqueued_metadata[key] for key in _RUNTIME_KEYS}
        assert _applied_by_pull(enqueued_metadata) == expected
        assert _applied_by_push(enqueued_metadata, monkeypatch) == expected


class TestClaimEnvelopeShape:
    def test_task_overrides_is_populated_not_empty(self, enqueued_metadata):
        """Regression: `task_overrides` used to hold ONLY the composed
        `system_prompt` (#1633), because the flat keys were never read."""
        overrides = _claim_payload(enqueued_metadata)["task_overrides"]
        assert overrides["model"] == "opus"
        assert overrides["allowed_tools"] == ["mcp__trinity__report", "Read"]
        assert overrides["max_turns"] == 7
        assert overrides["timeout_seconds"] == 90

    def test_session_id_is_the_claude_session_not_the_chat_row(
        self, enqueued_metadata
    ):
        """`session_id` must resolve from `resume_session_id`. `chat_session_id`
        is a Trinity DB row id — the worker passes this value straight to
        `execute_headless(resume_session_id=...)`, so sourcing it there would
        resume a session that does not exist."""
        payload = _claim_payload(enqueued_metadata)
        assert payload["session_id"] == enqueued_metadata["resume_session_id"]
        assert payload["session_id"] != enqueued_metadata["chat_session_id"]

    def test_system_prompt_is_still_composed_from_the_flat_caller_prompt(
        self, enqueued_metadata
    ):
        """#1629/#1633 preserved: the platform prompt is composed at claim time
        and the row's caller prompt is threaded in as `caller_prompt` — now read
        from the flat `system_prompt` key the producer writes."""
        _import_backend_modules()
        from services import pull_coordination_service as pcs

        row = {
            "id": "exec-2317", "agent_name": "alpha",
            "message": enqueued_metadata["message"],
            "backlog_metadata": json.dumps(enqueued_metadata),
            "triggered_by": "agent", "model_used": None,
            "lease_expires_at": None, "claim_token": "t",
            "claimed_by_worker": "w", "redelivery_count": 0,
        }
        with patch.object(pcs, "_resolve_agent_runtime", return_value="claude-code"), \
             patch.object(pcs, "is_execution_context_enabled", return_value=False), \
             patch.object(pcs, "compose_system_prompt",
                          return_value="PLATFORM::CALLER") as compose:
            claim = pcs._build_claim_response(row)

        assert compose.call_args.kwargs["caller_prompt"] == "CALLER-PROMPT"
        # ent#243: the row's own model selects the prompt tier.
        assert compose.call_args.kwargs["execution_context"].model == "opus"
        assert (
            claim["envelope"]["payload"]["task_overrides"]["system_prompt"]
            == "PLATFORM::CALLER"
        )

    def test_nested_task_overrides_still_overlays_the_flat_keys(
        self, enqueued_metadata
    ):
        """Forward-compat: nothing writes the §2.2 nested quarantine object today,
        but when a producer does it must WIN over the flat keys (it is the more
        specific, per-task statement)."""
        meta = dict(enqueued_metadata)
        meta["task_overrides"] = {"model": "haiku", "allowed_tools": ["Read"]}
        overrides = _claim_payload(meta)["task_overrides"]
        assert overrides["model"] == "haiku"
        assert overrides["allowed_tools"] == ["Read"]
        # Flat keys the overlay did not mention still survive.
        assert overrides["max_turns"] == 7
        assert overrides["timeout_seconds"] == 90

    def test_absent_settings_are_omitted_not_nulled(self):
        """A minimal row (nothing but a message) must not put `"model": null` &c.
        on the wire — the worker treats absent and null identically, and an empty
        overrides object keeps the envelope honest about what the row asked for."""
        _import_backend_modules()
        from services import pull_coordination_service as pcs

        row = {
            "id": "e1", "agent_name": "alpha", "message": "hi",
            "backlog_metadata": json.dumps({"message": "hi"}),
            "triggered_by": "manual", "model_used": None,
            "lease_expires_at": None, "claim_token": "t",
            "claimed_by_worker": "w", "redelivery_count": 0,
        }
        with patch.object(pcs, "_resolve_agent_runtime", return_value="claude-code"), \
             patch.object(pcs, "is_execution_context_enabled", return_value=False), \
             patch.object(pcs, "compose_system_prompt", return_value=None):
            claim = pcs._build_claim_response(row)

        payload = claim["envelope"]["payload"]
        assert payload["session_id"] is None
        # Only the (None) composed system_prompt — no null model/tools/turn cap.
        assert set(payload["task_overrides"]) == {"system_prompt"}
