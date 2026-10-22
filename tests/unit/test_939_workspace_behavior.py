"""The Workspace's plain-words indicators on the role card (trinity-enterprise#939).

Pinned: tones only (no rate, count of checks, or score crosses), the run count
the lines rest on, an empty window is `runs: 0` with no lines (never "all
good"), and an unreadable window is None.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest  # noqa: E402

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_harness import db_backend  # noqa: E402,F401

from test_939_indicators_routes import _exec, _grade  # noqa: E402

pytestmark = pytest.mark.unit


def test_tones_and_run_count(db_backend):
    from client_portal import role_card
    for i in range(10):
        _exec(f"r{i}")
        _grade(f"r{i}", fail=i < 4)
    out = role_card.behavior_for("agent-a")
    assert out["days"] == 14 and out["runs"] == 10
    tones = {l["indicator"]: l["tone"] for l in out["lines"]}
    assert tones == {"completion": "good", "checks_passed": "watch",
                     "scope_fidelity": "unknown", "escalation_correctness": "unknown"}
    from client_portal.models import PortalBehavior
    PortalBehavior(**out)  # the response model accepts it as-is


def test_empty_window_is_no_lines_not_all_good(db_backend):
    from client_portal import role_card
    assert role_card.behavior_for("agent-a") == {"days": 14, "runs": 0, "lines": []}


def test_unreadable_window_is_none(db_backend, monkeypatch):
    from client_portal import role_card
    from database import db

    def boom(**kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "evaluation_window_rows", boom)
    assert role_card.behavior_for("agent-a") is None
