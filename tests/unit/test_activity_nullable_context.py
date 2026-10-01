"""Regression for #3173: unknown context usage must not block chat completion."""
import ast
import asyncio
import json
import math
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def broadcast_method():
    # Exercise the real production method without importing the database singleton.
    source = Path(__file__).resolve().parents[2] / "src/backend/services/activity_service.py"
    tree = ast.parse(source.read_text())
    service = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ActivityService")
    method = next(node for node in service.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_broadcast_activity_event")
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), method], type_ignores=[])
    namespace = {"json": json, "math": math, "utc_now_iso": lambda: "2026-01-01T00:00:00Z"}
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    return namespace[method.name]


@pytest.mark.parametrize("used,maximum", [(None, 200000), (1, None), (0, 0), ("1", 200000), (True, 200000), (-1, 200000), (1, -1), (float("inf"), 200000)])
def test_unknown_or_invalid_context_still_broadcasts_completion(used, maximum):
    websocket, filtered = AsyncMock(), AsyncMock()
    service = SimpleNamespace(websocket_manager=websocket, filtered_websocket_manager=filtered)
    asyncio.run(broadcast_method()(service, "test-agent", "activity-test", "chat_start", "completed", "updated", details={"context_used": used, "context_max": maximum, "execution_id": "execution-test"}))
    event = json.loads(websocket.broadcast.call_args.args[0])
    assert event["activity_state"] == "completed"
    assert event["details"]["execution_id"] == "execution-test"
    assert "context" not in event["details"]
    assert filtered.broadcast_filtered.call_args.args[0]["activity_state"] == "completed"


@pytest.mark.parametrize("used,maximum,percentage", [(0, 200000, 0), (50000, 200000, 25), (1, 3, 33.33)])
def test_known_context_preserves_percentage(used, maximum, percentage):
    websocket = AsyncMock()
    service = SimpleNamespace(websocket_manager=websocket, filtered_websocket_manager=None)
    asyncio.run(broadcast_method()(service, "test-agent", "activity-test", "chat_start", "completed", "updated", details={"context_used": used, "context_max": maximum}))
    event = json.loads(websocket.broadcast.call_args.args[0])
    assert event["details"]["context"] == {"used": used, "max": maximum, "percentage": percentage}
