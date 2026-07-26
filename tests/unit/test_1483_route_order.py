"""
#1483 AC #3 — route-registration order guard (Invariant #4).

The split of ``routers/chat.py`` MUST NOT disturb the effective route-matching
order. The load-bearing collision is **cross-router**:

  * ``chat.py``     ``GET /{name}/executions/running``       (static two-segment)
  * ``schedules.py`` ``GET /{name}/executions/{execution_id}`` (param two-segment)

``main.py`` includes ``chat_router`` (line ~913) *before* ``schedules_router``
(line ~916), so ``/executions/running`` resolves to the chat handler
(``get_agent_running_executions``). If a split reordered the includes — or moved
the executions endpoints into a sub-router included *after* schedules — a GET to
``/executions/running`` would be captured as ``execution_id="running"`` and hit
schedules' ``get_execution`` → wrong handler / 404.

**OpenAPI is blind to route order** (it is an order-independent path set), so
this is the *only* guard for the invariant (plan §5, RD-E8).

This module imports the full ``main`` app (cross-router order needs it), so it is
written to be **self-sufficient** and run standalone:
``pytest tests/unit/test_1483_route_order.py`` — dodging the repo-root ``config/``
namespace-package collection abort that trips the whole ``tests/unit`` sweep
(``test_1073``, see verify-local notes).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# --- self-sufficient env + path (before `import main`) --------------------
# config.py raises at import without creds; log_archive_service mkdirs
# LOG_ARCHIVE_PATH (default /data, read-only on the host); database.py inits a
# SQLite DB at import. Pin all three to writable/dummy values.
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)
os.environ.setdefault(
    "TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-1483-routeorder.db")
)
os.environ.setdefault("LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-1483-logs"))

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest
from fastapi.routing import APIRoute
from starlette.routing import Match

# ---------------------------------------------------------------------------
# Standalone-only collection guard (#1483 / PR #1695).
#
# This module imports the assembled ``main`` app at module scope (the
# cross-router order check needs the whole app). In the *whole-directory*
# ``tests/unit`` collection an earlier module binds ``sys.modules['utils']`` to
# the repo's ``tests/utils`` package (``pythonpath`` lists ``tests`` before
# ``src/backend``), which has no ``password_validation`` submodule — so
# ``import main`` (via ``routers/setup.py``'s
# ``from utils.password_validation import ...``) dies with ``ModuleNotFoundError``
# and the module ERRORS collection. Standalone, ``utils`` resolves to
# ``src/backend/utils`` and the import is clean.
#
# Skip *loudly* (not error) when the shadow is present, so CI's whole-collection
# run stays green; the real assertions run standalone:
#     pytest tests/unit/test_1483_route_order.py
try:
    import main  # noqa: E402  (must follow the env/path setup above)
except ImportError as _import_main_exc:  # pragma: no cover — polluted sweep only
    pytest.skip(
        "requires pristine sys.modules — `import main` failed under the full "
        f"tests/unit collection ({_import_main_exc}); the repo's tests/utils "
        "package shadows src/backend/utils. Run standalone: "
        "pytest tests/unit/test_1483_route_order.py (see PR #1695).",
        allow_module_level=True,
    )


def _flatten_in_match_order(app):
    """Return every APIRoute in the app's effective match order.

    FastAPI 0.115.x wraps each ``include_router`` call in an ``_IncludedRouter``
    (a lazy-matching optimization); ``original_router.routes`` preserves the
    per-router registration order, and the order of the ``_IncludedRouter``
    wrappers in ``app.routes`` preserves the ``include_router`` call order. The
    first APIRoute (in this flattened order) whose ``.matches(scope)`` is FULL is
    the handler Starlette dispatches to — this mirrors real matching.
    """
    flat: list[APIRoute] = []
    for entry in app.routes:
        if type(entry).__name__ == "_IncludedRouter":
            flat.extend(r for r in entry.original_router.routes if isinstance(r, APIRoute))
        elif isinstance(entry, APIRoute):
            flat.append(entry)
    return flat


def _first_match(flat, method: str, path: str):
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [],
        "query_string": b"",
        "path_params": {},
    }
    for route in flat:
        matched, _ = route.matches(scope)
        if matched == Match.FULL:
            return route
    return None


@pytest.fixture(scope="module")
def flat_routes():
    return _flatten_in_match_order(main.app)


def test_executions_running_resolves_to_chat_handler(flat_routes):
    """`GET /executions/running` must hit chat's `get_agent_running_executions`,
    never schedules' `get_execution` (`{execution_id}` catch-all)."""
    route = _first_match(flat_routes, "GET", "/api/agents/x/executions/running")
    assert route is not None, "no route matched /executions/running"
    assert route.endpoint.__name__ == "get_agent_running_executions"


def test_executions_param_resolves_to_schedules_handler(flat_routes):
    """A real execution id still resolves to schedules' `get_execution` — proves
    `running` is a genuine static-before-param precedence, not a total shadow."""
    route = _first_match(flat_routes, "GET", "/api/agents/x/executions/abc-123")
    assert route is not None
    assert route.endpoint.__name__ == "get_execution"


def test_executions_stream_resolves_to_chat_stream(flat_routes):
    """`GET /executions/{id}/stream` (three-segment) still resolves to chat's
    `stream_execution_log`."""
    route = _first_match(flat_routes, "GET", "/api/agents/x/executions/abc-123/stream")
    assert route is not None
    assert route.endpoint.__name__ == "stream_execution_log"


def test_chat_router_precedes_schedules_router_in_include_order():
    """Cross-router guard: the chat router's include index is strictly before the
    schedules router's — the precondition that makes `/executions/running` win.

    Robust to both route-table shapes: current FastAPI (0.115.x/0.124.x) flattens
    ``include_router`` into plain ``APIRoute`` entries in ``app.routes`` (include
    order = positional order), while older/future versions wrap each include in an
    ``_IncludedRouter``. Either way the chat handler's first occurrence must
    precede the schedules handler's.
    """
    def _include_index(endpoint_name: str) -> int:
        for idx, entry in enumerate(main.app.routes):
            # Flattened shape: the APIRoute lives directly in app.routes.
            if isinstance(entry, APIRoute) and entry.endpoint.__name__ == endpoint_name:
                return idx
            # Legacy wrapped shape: an _IncludedRouter carrying original_router.
            if type(entry).__name__ == "_IncludedRouter":
                for r in entry.original_router.routes:
                    if isinstance(r, APIRoute) and r.endpoint.__name__ == endpoint_name:
                        return idx
        raise AssertionError(f"endpoint {endpoint_name} not found in the app route table")

    chat_idx = _include_index("get_agent_running_executions")
    sched_idx = _include_index("get_execution")
    assert chat_idx < sched_idx, (
        f"chat router (idx {chat_idx}) must be included before schedules "
        f"(idx {sched_idx}) so /executions/running resolves to chat"
    )
