"""Binding guards for #1900 — template-id path traversal, at the two seams the
service-level tests structurally cannot reach.

**Why this file exists rather than an addition to `tests/test_templates.py`.**
`docs/memory/learnings.md` 2026-07-30: every gating CI job runs
`cd tests && pytest unit/`, so the root-level test files — including
`tests/test_templates.py` — are collected by NO workflow. A router-level guard
placed there would never go red in CI. The binding guard must live in
`tests/unit/`.

**Why not fold it into `test_local_templates_listing.py`.** That module (and
`test_ent128a_catalog_resilience.py`) deliberately load `template_service`
standalone via `importlib` with no backend deps. This file needs FastAPI +
`TestClient` + `dependency_overrides` + the real `crud`; a NEW file owns its own
imports at collection time rather than dragging heavy imports into a fast module
(learnings 2026-07-12).

Two seams:

1. **Router** — proves the traversal is unreachable over genuine HTTP routing,
   including that the `{template_id:path}` converter still routes legitimate
   ids, and that the 404 discloses no filesystem path (#1759 non-disclosure).

2. **The crud seam** — `_stage_config_files` must actually PASS the validated
   template directory to `generate_credential_files`. Every service-level test
   in `test_ent128a_catalog_resilience.py` calls that function directly, so if
   the ladder is extracted but wired wrong (e.g. left as
   `template_base_path=github_template_path`, which is always `None`), all of
   them still pass while deploy-local `.mcp.json` resolution silently regresses
   into the fallback arm. These tests are the only thing that catches it.
"""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest

_BACKEND = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Path-disclosure detection.
#
# Copied VERBATIM from `tests/unit/test_1759_local_template_not_found.py`
# (its `_ABS_PATH_RE` / `_SENSITIVE_ROOT_TOKENS` / `_leaked_paths`) rather than
# re-rolled: learnings 2026-07-26 records that a whitespace-anchored variant
# missed 8 of 9 real leak forms, including this codebase's own `!r` quoting
# style, which wraps a path in single quotes.
# ---------------------------------------------------------------------------

_ABS_PATH_RE = re.compile(r"(?<![A-Za-z0-9_.\-/])/[A-Za-z0-9_.\-/]+")
_SENSITIVE_ROOT_TOKENS = ("deployed-templates", "agent-configs")


def _leaked_paths(msg: str) -> list[str]:
    hits = [p for p in _ABS_PATH_RE.findall(msg) if not p.startswith("/api/")]
    hits += [t for t in _SENSITIVE_ROOT_TOKENS if t in msg]
    return hits


def _seed(parent: Path, name: str, body: str) -> Path:
    d = parent / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "template.yaml").write_text(body)
    return d


# ---------------------------------------------------------------------------
# Seam 1 — the router
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Minimal app with the real templates router + overridden auth."""
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import routers.templates as templates_router
    except ImportError:  # pragma: no cover - venv-dependent
        pytest.skip("backend venv required")

    # Key the override off the ROUTER's own reference, never a fresh
    # `from dependencies import get_current_user`. `dependency_overrides` is
    # keyed by callable IDENTITY, and this suite's conftest restores a
    # `sys.modules` baseline between tests: re-importing `dependencies` can
    # hand back a NEW function object while the cached `routers.templates`
    # still holds the original, so the override key misses and the request
    # 401s — intermittently, depending on test order (learnings 2026-07-12).
    # This reference is by construction the one the routes were built with.
    get_current_user = templates_router.get_current_user

    root = tmp_path / "templates"
    root.mkdir()
    _seed(root, "sage", body="name: sage\ndisplay_name: Sage Advisor")
    # Planted OUTSIDE the root, with a real template.yaml, so an unguarded join
    # returns a 200 rather than a vacuous 404.
    _seed(tmp_path, "outside", body="name: outside\ndisplay_name: SECRET OUTSIDE")

    # Redirect the templates root on the MODULE THE ROUTER'S FUNCTION ACTUALLY
    # LIVES IN, via its own `__globals__` — not on a fresh
    # `import services.template_service`.
    #
    # `routers/templates.py` binds `get_local_template` by value at import
    # time, and this suite is randomized (`pytest-randomly`) with a conftest
    # that restores a `sys.modules` baseline; the #1484 characterization
    # harness additionally MagicMocks `services.template_service`. So a later
    # `import services.template_service` can hand back a DIFFERENT module
    # object than the one whose function the router holds, leaving the patch on
    # a module nobody calls — the fixture then silently reads the REAL shipped
    # catalog and `local:sage` 404s. Observed: green in isolation, red at
    # certain random orderings in the full suite.
    #
    # `__globals__` is by construction the namespace the invoked function
    # resolves `_local_templates_dir` from, so this cannot drift.
    monkeypatch.setitem(
        templates_router.get_local_template.__globals__,
        "_local_templates_dir",
        lambda: root,
    )

    app = FastAPI()
    # NO `prefix=` here: the router already declares `prefix="/api/templates"`
    # (routers/templates.py:14). Passing it again mounts the routes at
    # `/api/templates/api/templates/...`, which makes EVERY request 404 — and a
    # traversal test that asserts 404 then passes because routing is broken,
    # not because containment works. Verified: with the duplicate prefix,
    # `test_1900_router_rejects_traversal_id` was green on unpatched code.
    app.include_router(templates_router.router)
    app.dependency_overrides[get_current_user] = lambda: types.SimpleNamespace(
        id=1, username="plain-user", role="user", agent_name=None,
        email="user@example.com",
    )
    c = TestClient(app)
    c._root = root
    c._base = tmp_path
    return c


def test_1900_router_rejects_traversal_id(client):
    """404, not 200, for every escape shape that survives the wire.

    RFC 3986 dot-segment removal (applied by httpx, and therefore by both
    TestClient and the integration client) drops only a segment that is exactly
    `..`, so single-level `../` and the absolute form travel intact; the
    multi-level form needs percent-encoding to reach the handler at all — a
    literal `../../` test would silently exercise nothing.
    """
    for path in [
        "/api/templates/local:../outside",              # single level — no trick
        "/api/templates/local:%2E%2E%2Foutside",        # encoded single level
        "/api/templates/local:%2E%2E%2F%2E%2E",         # encoded multi level
        f"/api/templates/local:{client._base / 'outside'}",  # absolute
    ]:
        resp = client.get(path)
        assert resp.status_code == 404, (path, resp.status_code, resp.text)
        assert "SECRET OUTSIDE" not in resp.text, path


def test_1900_router_still_serves_a_real_template(client):
    """Anti-over-blocking at the router layer — and proof the `:path`
    converter still routes a legitimate id."""
    resp = client.get("/api/templates/local:sage")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == "local:sage"
    assert body["display_name"] == "Sage Advisor"


def test_1900_router_404_leaks_no_path(client):
    """The rejection must be byte-identical to an unknown template.

    A distinct error code, the attempted path, or the root name would each turn
    the fix into a NEW enumeration oracle — precisely what #1759's
    single-sentence 404 exists to close.
    """
    rejected = client.get("/api/templates/local:%2E%2E%2Foutside")
    unknown = client.get("/api/templates/local:no-such-template")

    assert rejected.status_code == unknown.status_code == 404
    assert rejected.json() == unknown.json()
    assert _leaked_paths(rejected.text) == []
