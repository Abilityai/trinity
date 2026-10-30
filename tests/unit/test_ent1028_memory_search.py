"""Retrieval memory — search on demand (trinity-enterprise#1028).
Related flow: docs/memory/feature-flows/retrieval-memory.md

Recall injects the top facts into the prompt; this is the read path an agent
(and the owner) can drive by hand: the same store, the same ranking, the same
scope rule, more than the injected tail. Driven at each layer over the unit
conftest's real SQLite with the deterministic fake embedder from the ent#1022
suites:

* ``memory_service.search_records`` — ranked beyond the recall tail, every hit
  journaled as a ``search`` row that carries the query so the owner can see
  what was looked up.
* ``GET  …/retrieval-memory/search`` — the owner door (person-only).
* ``POST …/retrieval-memory/search`` — the agent door: the scope comes off the
  execution row by the whose-memory rule (`memory_subject`), never from the
  caller; an agent key searches only its own agent.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from database import db
from db.memory_facts import EVENT_OPS
from db.write_params import ExecutionResult, TaskExecutionFields
from db_models import UserCreate
from models import MemorySearchRequest, User
from services import anthropic_messages as am
from services import memory_embeddings as me
from services import memory_service as ms

DIM = me.EMBED_DIM
NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)


def hash_embed(texts):
    import numpy as np
    out = []
    for text in texts:
        v = np.zeros(DIM, dtype="<f4")
        for word in re.findall(r"\w+", (text or "").casefold()):
            v[int(hashlib.md5(word.encode()).hexdigest(), 16) % DIM] += 1.0
        norm = float(np.linalg.norm(v)) or 1.0
        out.append((v / norm).tolist())
    return out


@pytest.fixture
def memory_on(monkeypatch):
    db.set_setting(ms.SETTING_ENABLED, "true")
    db.set_setting(ms.SETTING_RECENCY_HALF_LIFE, "")
    monkeypatch.setattr(me, "embed", hash_embed)
    monkeypatch.setattr(me, "availability", lambda: (True, None))
    monkeypatch.setattr(me, "is_loaded", lambda: True)
    monkeypatch.setattr(ms, "_redis", lambda: None)
    monkeypatch.setattr(am, "has_platform_key", lambda: True)
    ms._scope_cache.clear()
    yield
    db.set_setting(ms.SETTING_ENABLED, "false")
    ms._scope_cache.clear()


@pytest.fixture
def agent():
    suffix = uuid.uuid4().hex[:8]
    owner_email = f"owner-{suffix}@example.com"
    db.create_user(UserCreate(username=owner_email, email=owner_email, role="user", password="x"))
    name = f"mem-agent-{suffix}"
    db.register_agent_owner(name, owner_email)
    return SimpleNamespace(name=name, owner_email=owner_email)


def person(agent, *, email=None, mcp_scope=None, agent_name=None):
    # The owner as the real access predicate sees them: registered with the
    # email as username, so `can_user_access_agent` runs for real here.
    return User(id=1, username=agent.owner_email, role="user", email=email or agent.owner_email, mcp_scope=mcp_scope,
                agent_name=agent_name, created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z")


def execution(agent_name, *, triggered_by, source_user_email=None, message="hello"):
    ex = db.create_task_execution(agent_name, message, triggered_by,
                                  fields=TaskExecutionFields(source_user_email=source_user_email))
    db.update_execution_status(ex.id, "success", ExecutionResult(response="done"))
    return ex.id


def seed(agent, text, *, scope="", source="schedule"):
    fid = db.insert_memory_fact(agent_name=agent.name, user_email=scope, text=text, text_hash=ms.fact_hash(text),
                                embedding=me.to_bytes(hash_embed([text])[0]), embedding_model="m",
                                source=source, execution_id=uuid.uuid4().hex)
    ms._scope_cache.clear()
    return fid


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #

class TestSearchService:
    def test_search_ranks_past_the_recall_tail_and_journals_the_query(self, agent, memory_on):
        """Eight facts about deploys; recall injects five, a search for twenty
        returns all eight — every hit a `search` row carrying the query."""
        for i in range(8):
            seed(agent, f"Deploy window {i} is on Friday after the staging check")
        seed(agent, "The bakery on Main Street opens at seven")      # unrelated, must not match
        injected = run(ms.recall_records(agent.name, None, "deploy staging Friday"))
        assert len(injected) == ms.RECALL_K
        exec_id = uuid.uuid4().hex
        hits = run(ms.search_records(agent.name, None, "deploy staging Friday", k=20, execution_id=exec_id))
        assert len(hits) == 8
        assert all("Deploy window" in h["text"] for h in hits)
        assert hits == sorted(hits, key=lambda h: -h["score"])
        assert all(0.0 < h["relevance"] <= 1.0 and h["line"].startswith("(") for h in hits)
        rows = db.list_memory_fact_events(agent.name, execution_id=exec_id, limit=100)
        assert {r["op"] for r in rows} == {"search"} and len(rows) == 8
        assert all(r["candidate_text"] == "deploy staging Friday" for r in rows)
        assert "search" in EVENT_OPS

    def test_search_is_scope_bound_and_empty_when_off(self, agent, memory_on):
        seed(agent, "Prefers invoices as PDF", scope="ann@example.com")
        seed(agent, "The agent deploys on Fridays")
        assert [h["text"] for h in run(ms.search_records(agent.name, "ann@example.com", "invoice PDF", k=10))] == ["Prefers invoices as PDF"]
        assert run(ms.search_records(agent.name, None, "invoice PDF", k=10)) == []
        db.set_setting(ms.SETTING_ENABLED, "false")
        assert run(ms.search_records(agent.name, "ann@example.com", "invoice PDF", k=10)) == []


    def test_an_owners_search_is_a_look_not_a_use(self, agent, memory_on):
        """The recall counter orders the cap-prune (least-recalled first), so an
        owner typing in the search box must not shelter what they searched for;
        an agent's search inside a run counts, as recall does."""
        fid = seed(agent, "Deploys go to staging first")
        count = lambda: next(f["recall_count"] for f in db.list_memory_facts(agent.name, "") if f["id"] == fid)
        assert count() == 0
        run(ms.search_records(agent.name, None, "staging deploy", k=5))
        assert count() == 0
        run(ms.search_records(agent.name, None, "staging deploy", k=5, execution_id=uuid.uuid4().hex))
        assert count() == 1

    def test_a_broken_embedder_is_a_named_refusal_not_an_empty_answer(self, agent, memory_on, monkeypatch):
        from routers.agent_memory import search_retrieval_memory_endpoint
        seed(agent, "Deploys go to staging first")

        def boom(_texts):
            raise RuntimeError("onnx session gone")
        monkeypatch.setattr(me, "embed", boom)
        with pytest.raises(ms.MemorySearchError):
            run(ms.search_records(agent.name, None, "staging deploy", k=5))
        with pytest.raises(HTTPException) as err:
            run(search_retrieval_memory_endpoint(agent_name=agent.name, current_user=person(agent), q="staging deploy", k=5, user_email=None))
        assert err.value.status_code == 503 and err.value.detail["code"] == "memory_unavailable"
        assert "onnx" not in str(err.value.detail)


# --------------------------------------------------------------------------- #
# The owner door
# --------------------------------------------------------------------------- #

class TestOwnerSearchRoute:
    def test_owner_searches_a_scope_and_bad_input_is_named(self, agent, memory_on):
        from routers.agent_memory import search_retrieval_memory_endpoint
        seed(agent, "Prefers invoices as PDF", scope="ann@example.com")
        seed(agent, "Deploys go to staging first")

        def call(q, **kw):
            args = dict(agent_name=agent.name, current_user=person(agent), q=q, k=10, user_email=None)
            args.update(kw)
            return run(search_retrieval_memory_endpoint(**args))

        hits = call("staging deploy")
        assert [h.text for h in hits] == ["Deploys go to staging first"]
        assert hits[0].line.startswith("(") and 0.0 < hits[0].relevance <= 1.0
        assert [h.text for h in call("invoices PDF", user_email="Ann@Example.com")] == ["Prefers invoices as PDF"]
        for q, code in ((" ", "query_empty"),):
            with pytest.raises(HTTPException) as err:
                call(q)
            assert err.value.status_code == 422 and err.value.detail["code"] == code
        with pytest.raises(HTTPException) as err:
            call("x", user_email="not-an-email")
        assert err.value.status_code == 422 and err.value.detail["code"] == "invalid_email"
        db.set_setting(ms.SETTING_ENABLED, "false")
        with pytest.raises(HTTPException) as err:
            call("staging deploy")
        assert err.value.status_code == 422 and err.value.detail["code"] == "memory_off"


# --------------------------------------------------------------------------- #
# The agent door
# --------------------------------------------------------------------------- #

class TestAgentSearchRoute:
    def _call(self, agent, body, user):
        from routers.agent_memory import search_retrieval_memory_agent_endpoint
        return run(search_retrieval_memory_agent_endpoint(agent_name=agent.name, body=body, current_user=user))

    def test_headless_run_searches_the_agent_scope(self, agent, memory_on):
        """The same words match a fact in both scopes; a headless run sees only the agent's."""
        seed(agent, "Invoices as PDF are archived yearly")
        seed(agent, "Prefers invoices as PDF", scope="ann@example.com")
        exec_id = execution(agent.name, triggered_by="schedule")
        out = self._call(agent, MemorySearchRequest(execution_id=exec_id, query="invoices PDF", k=10), person(agent))
        assert out.scope == "agent"
        assert [h.text for h in out.hits] == ["Invoices as PDF are archived yearly"]
        rows = db.list_memory_fact_events(agent.name, execution_id=exec_id, op="search", limit=10)
        assert len(rows) == 1

    def test_person_turn_searches_that_persons_scope_only(self, agent, memory_on):
        """…and a person's turn sees only that person's, never the shared agent scope."""
        seed(agent, "Invoices as PDF are archived yearly")
        seed(agent, "Prefers invoices as PDF", scope="ann@example.com")
        exec_id = execution(agent.name, triggered_by="public", source_user_email="ann@example.com")
        out = self._call(agent, MemorySearchRequest(execution_id=exec_id, query="invoices PDF", k=10), person(agent))
        assert out.scope == "person"
        assert [h.text for h in out.hits] == ["Prefers invoices as PDF"]

    def test_refusals_are_named(self, agent, memory_on):
        seed(agent, "Deploys go to staging first")
        # an agent key naming another agent — refused before anything is read
        with pytest.raises(HTTPException) as err:
            self._call(agent, MemorySearchRequest(execution_id="x", query="deploy"),
                       person(agent, mcp_scope="agent", agent_name="someone-else"))
        assert err.value.status_code == 403
        with pytest.raises(HTTPException) as err:
            self._call(agent, MemorySearchRequest(execution_id="nope", query="deploy"), person(agent))
        assert err.value.status_code == 404
        other = execution(f"{agent.name}-other", triggered_by="schedule")
        with pytest.raises(HTTPException) as err:
            self._call(agent, MemorySearchRequest(execution_id=other, query="deploy"), person(agent))
        assert err.value.status_code == 403
        anonymous = execution(agent.name, triggered_by="public", source_user_email="anonymous (10.0.0.1)")
        with pytest.raises(HTTPException) as err:
            self._call(agent, MemorySearchRequest(execution_id=anonymous, query="deploy"), person(agent))
        assert err.value.status_code == 422 and err.value.detail["code"] == "memory_no_subject"
        assert err.value.detail["reason"] == "no_verified_email"
        with pytest.raises(HTTPException) as err:
            self._call(agent, MemorySearchRequest(execution_id=anonymous, query="   "), person(agent))
        assert err.value.status_code == 422 and err.value.detail["code"] == "query_empty"
        db.set_setting(ms.SETTING_ENABLED, "false")
        headless = execution(agent.name, triggered_by="schedule")
        with pytest.raises(HTTPException) as err:
            self._call(agent, MemorySearchRequest(execution_id=headless, query="deploy"), person(agent))
        assert err.value.status_code == 422 and err.value.detail["code"] == "memory_off"

    def test_k_is_capped_by_the_model(self):
        with pytest.raises(Exception):
            MemorySearchRequest(execution_id="x", query="deploy", k=500)
        assert MemorySearchRequest(execution_id="x", query="deploy").k == 10
