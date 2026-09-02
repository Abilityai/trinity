# mcp: none — platform admin settings — a grant surface, human-only (Invariant #8 grant-vs-use)
"""System settings routes for the Trinity backend.

Endpoints for system-wide configuration. Admin-only for modification, read
access for all authenticated users.

#1028: this was one 3,529-line module — the largest file in the backend and
more than four times the 800-line critical threshold. It is now a package of
ten domain modules composed onto **one** router here, so the mounted API is
byte-identical to the single-module version and `from routers.settings import
router` is unchanged.

**Inclusion order is load-bearing, not cosmetic** (Invariant #4). `generic`
owns `GET/PUT/DELETE /{key}`, which matches any single path segment. Included
before its siblings it would swallow `/ops/config`, `/brain-orb`,
`/api-keys/anthropic` and every other specific route, answering "setting not
found" for routes that plainly exist — a failure that no unit test of an
individual handler can see, because each handler still works when called
directly. `tests/unit/test_1028_settings_package.py` pins the ordering.
"""
from typing import List

from fastapi import APIRouter

from database import SystemSetting

from . import (
    agent_defaults,
    credentials,
    flags,
    generic,
    integrations,
    mcp_url,
    ops,
    retention,
    templates,
    whitelist,
)

# Re-exported for callers that reach past the router (`routers/connector.py`
# imports `resolve_mcp_url`; tests import the key sets and the repo pattern).
# Keeping them addressable here preserves the pre-split import surface.
from .credentials import mask_api_key  # noqa: F401
from .generic import (  # noqa: F401
    LEGACY_SKILLS_LIBRARY_KEYS,
    SKILLS_AUTOMATION_KEYS,
)
from .mcp_url import MCP_URL_SETTING_KEY, resolve_mcp_url  # noqa: F401
from .templates import _REPO_PATTERN  # noqa: F401

router = APIRouter(prefix="/api/settings", tags=["settings"])

# `GET /api/settings` — registered on the parent because its path is empty and
# a prefix-less sub-router cannot express that (see `generic.get_all_settings`).
router.add_api_route(
    "",
    generic.get_all_settings,
    methods=["GET"],
    response_model=List[SystemSetting],
)

# Specific routes first, in domain order …
router.include_router(flags.router)
router.include_router(retention.router)
router.include_router(credentials.router)
router.include_router(whitelist.router)
router.include_router(templates.router)
router.include_router(mcp_url.router)
router.include_router(agent_defaults.router)
router.include_router(integrations.router)
router.include_router(ops.router)
# … and the `/{key}` catch-all LAST. See the module docstring.
router.include_router(generic.router)

__all__ = ["router"]
