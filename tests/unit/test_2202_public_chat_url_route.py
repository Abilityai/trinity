"""#2202 — `GET /api/settings/public-chat-url`, the route that makes *unset* an
answer instead of an error.

The Settings page read this key through the generic `GET /api/settings/{key}`,
which 404s for a key that was never written. The store already treated 404 as
"unset", so nothing was broken — what was lost was the SIGNAL: every visit to
every Settings tab logged a failed request, so a real failure on that call was
indistinguishable from the ordinary case, and no client-side handling can
suppress the browser's own network log.

Locked behaviour:

  * an unset key answers 200 with `value: None` — never 404;
  * a configured key answers its value;
  * the generic route's 404 for other keys is UNCHANGED (this is an addition,
    not a widening — that contract is depended on outside this repo);
  * the route is admin-gated, matching the getter it replaces for this key;
  * it is declared ABOVE `/{key}` (Invariant #4), or the literal path is
    swallowed as a setting named "public-chat-url".

The two source-reading checks deliberately take NO fixture: they must run on a
machine without the backend venv, because route-declaration order and the
generic route's 404 are exactly the properties a reviewer needs checked
everywhere, not only where FastAPI imports.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent
_BACKEND = _REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# #1028 split routers/settings.py into a package; the route lives beside
# /mcp-url, and Invariant #4 is now an INCLUSION order in the package __init__.
SETTINGS_PKG = _BACKEND / "routers" / "settings"
MCP_URL_PY = SETTINGS_PKG / "mcp_url.py"
GENERIC_PY = SETTINGS_PKG / "generic.py"
INIT_PY = SETTINGS_PKG / "__init__.py"


@pytest.fixture
def settings_router():
    try:
        import routers.settings.mcp_url as mod
    except ImportError:
        pytest.skip("backend venv required")
    return mod


def _admin():
    user = MagicMock()
    user.role = "admin"
    user.username = "admin"
    user.mcp_scope = None
    user.agent_name = None
    return user


def _call(mod, user):
    import asyncio
    return asyncio.get_event_loop().run_until_complete(
        mod.get_public_chat_url(request=MagicMock(), current_user=user)
    )


def test_unset_key_answers_none_not_404(settings_router):
    """The whole point: absent is a value, not an error."""
    with patch.object(settings_router, "db") as db, \
         patch.object(settings_router, "assert_admin"):
        db.get_setting_value.return_value = None
        body = _call(settings_router, _admin())
    assert body["value"] is None
    assert body["key"] == "public_chat_url"


def test_configured_key_answers_its_value(settings_router):
    with patch.object(settings_router, "db") as db, \
         patch.object(settings_router, "assert_admin"):
        db.get_setting_value.return_value = "https://chat.example.com"
        body = _call(settings_router, _admin())
    assert body["value"] == "https://chat.example.com"


def test_reads_the_same_key_the_writer_writes(settings_router):
    """A dedicated reader for a key the generic PUT still writes is only correct
    while both name the same key."""
    assert settings_router.PUBLIC_CHAT_URL_SETTING_KEY == "public_chat_url"
    with patch.object(settings_router, "db") as db, \
         patch.object(settings_router, "assert_admin"):
        db.get_setting_value.return_value = None
        _call(settings_router, _admin())
        db.get_setting_value.assert_called_once_with("public_chat_url")


def test_route_is_admin_gated(settings_router):
    """Same gate as the generic getter it replaces for this key — the addition
    must not be a way around `assert_admin`."""
    sentinel = RuntimeError("admin gate ran")
    with patch.object(settings_router, "db"), \
         patch.object(settings_router, "assert_admin", side_effect=sentinel):
        with pytest.raises(RuntimeError, match="admin gate ran"):
            _call(settings_router, _admin())


def test_declared_above_the_catch_all():
    """Invariant #4. Included after the `generic` sub-router that owns `/{key}`,
    the literal path would be captured as a setting key named "public-chat-url"
    and answer 404 — reintroducing the exact bug this route removes, silently.
    Since #1028 the order is the package __init__'s `include_router` sequence."""
    assert '@router.get("/public-chat-url")' in MCP_URL_PY.read_text()
    assert '@router.get("/{key}")' in GENERIC_PY.read_text()
    init = INIT_PY.read_text()
    specific = init.index("router.include_router(mcp_url.router)")
    catch_all = init.index("router.include_router(generic.router)")
    assert specific < catch_all, "mcp_url.router must be included before generic.router"


def test_generic_getter_still_404s_for_an_unset_key():
    """This is an ADDITION, not a widening. The generic 404 is the documented
    contract for every other key and is read outside this repo."""
    src = GENERIC_PY.read_text()
    body = src[src.index('@router.get("/{key}")'):]
    body = body[:body.index("\n@router.") if "\n@router." in body[1:] else len(body)]
    assert re.search(r"status_code=404", body), (
        "the generic getter no longer 404s for an unset key — that contract was "
        "not in scope for #2202"
    )
