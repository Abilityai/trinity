"""ent#126 — bundled-manifest catalog route order in the ASSEMBLED app (Invariant #4).

`tests/unit/test_ent126_manifest_catalog.py` mounts `routers/systems.py` alone on a
bare FastAPI app. That proves the ordering WITHIN the router, but it cannot prove
anything about the real application: `main.py` mounts 60+ routers, and a route's
effective handler is decided by the first FULL match across all of them.

The two collisions this guards are both inside `routers/systems.py`:

  * `GET /api/systems/manifests`          vs  `GET /api/systems/{system_name}`
  * `GET /api/systems/manifests/manifest` vs  `GET /api/systems/{system_name}/manifest`

Declared after their parameterized siblings, each fails *silently and plausibly* —
`/manifests` becomes `get_system(system_name="manifests")` and 404s with
"System 'manifests' not found", which reads like a legitimate empty state rather
than a routing bug.

**OpenAPI is blind to route order** (it is an order-independent path set), so a
schema check cannot catch this; only a match-order assertion can. Same reasoning
and same mechanism as `test_1483_route_order.py`, whose helpers this mirrors.

Self-sufficient and standalone-runnable for the same reason as that module — see
its header for the `sys.modules['utils']` shadow.
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
    "TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent126-routeorder.db")
)
os.environ.setdefault(
    "LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent126-logs")
)

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest
from fastapi.routing import APIRoute
from starlette.routing import Match

# Skip loudly (not error) when the whole-directory sweep has shadowed `utils`;
# the real assertions run standalone. See test_1483_route_order.py.
try:
    import main  # noqa: E402  (must follow the env/path setup above)
except ImportError as _import_main_exc:  # pragma: no cover — polluted sweep only
    pytest.skip(
        "requires pristine sys.modules — `import main` failed "
        f"({_import_main_exc}). NOTE (#2080): this message used to blame the "
        "`tests/utils` shadowing, which no longer exists (that package is now "
        "`tests/testkit`); the actual cause was an undeclared OTel exporter "
        "dependency, so this module skipped ALWAYS — standalone included — "
        "while reading as an environment quirk. Run standalone: "
        "pytest tests/unit/test_ent126_route_order.py",
        allow_module_level=True,
    )


def _flatten_in_match_order(app):
    """Every APIRoute in the app's effective match order (see test_1483)."""
    flat: list[APIRoute] = []
    for entry in app.routes:
        if type(entry).__name__ == "_IncludedRouter":
            flat.extend(
                r for r in entry.original_router.routes if isinstance(r, APIRoute)
            )
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


def test_manifest_list_resolves_to_the_catalog_handler(flat_routes):
    """`GET /api/systems/manifests` must hit `list_manifests`, never `get_system`."""
    route = _first_match(flat_routes, "GET", "/api/systems/manifests")
    assert route is not None, "no route matched /api/systems/manifests"
    assert route.endpoint.__name__ == "list_manifests", (
        f"resolved to {route.endpoint.__name__} — the catalog list route is being "
        "shadowed by a parameterized sibling (Invariant #4)"
    )


def test_manifest_detail_resolves_to_the_catalog_handler(flat_routes):
    """`GET /api/systems/manifests/{id}` must hit `get_manifest`."""
    route = _first_match(flat_routes, "GET", "/api/systems/manifests/default-system")
    assert route is not None
    assert route.endpoint.__name__ == "get_manifest"


def test_manifest_detail_wins_the_second_collision(flat_routes):
    """The subtle one: `/api/systems/manifests/manifest` ALSO matches
    `GET /{system_name}/manifest` with system_name="manifests".

    A manifest whose id is literally "manifest" is unlikely, but the collision is
    structural — it proves the detail route is declared above BOTH parameterized
    siblings, not just above `/{system_name}`.
    """
    route = _first_match(flat_routes, "GET", "/api/systems/manifests/manifest")
    assert route is not None
    assert route.endpoint.__name__ == "get_manifest", (
        f"resolved to {route.endpoint.__name__} — expected the catalog detail route, "
        "not the deployed-system manifest export"
    )


def test_system_detail_route_still_reachable(flat_routes):
    """The guard must not have shadowed the siblings it is declared above."""
    route = _first_match(flat_routes, "GET", "/api/systems/some-real-system")
    assert route is not None
    assert route.endpoint.__name__ == "get_system"


def test_system_manifest_export_route_still_reachable(flat_routes):
    """`GET /{system_name}/manifest` still exports a DEPLOYED system's YAML.

    Confirms `manifests` is a genuine static-before-param precedence rather than a
    total shadow of the export route.
    """
    route = _first_match(flat_routes, "GET", "/api/systems/some-real-system/manifest")
    assert route is not None
    assert route.endpoint.__name__ == "get_system_manifest"


def test_deploy_route_unaffected(flat_routes):
    """`POST /api/systems/deploy` — the pre-existing static route — still resolves."""
    route = _first_match(flat_routes, "POST", "/api/systems/deploy")
    assert route is not None
    assert route.endpoint.__name__ == "deploy_system"
