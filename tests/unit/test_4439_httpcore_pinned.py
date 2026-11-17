"""`patch.dict(sys.modules, ...)` must not be able to evict httpcore (#4439).

httpx 0.28 imports httpcore lazily and caches its exception map in a module
global (`httpx._transports.default.HTTPCORE_EXC_MAP`) on the first request. A
`patch.dict(sys.modules, ...)` block rebuilds `sys.modules` from its entry
snapshot on exit, so if httpcore was first imported INSIDE such a block it is
dropped on exit and re-imported later as a second module. The cached map then
names the first module's classes, the second module's `ConnectError` matches
none of them, and it escapes every `except httpx.HTTPError` raw.

That is how `test_ent926_runtime_turn_scope::test_an_unreachable_platform_is_no_answer`
failed on 3 of 4 `dev` runs (#4439): green alone, red only when an earlier test
on the same xdist worker had primed the map inside a sys.modules patch. The
root conftest pins httpcore before any test can snapshot `sys.modules`.
"""
import asyncio
import sys
from unittest.mock import patch

import httpx
import pytest

pytestmark = pytest.mark.unit


async def _hit_closed_port():
    async with httpx.AsyncClient(timeout=2) as client:
        await client.get("http://127.0.0.1:9/")


def test_httpcore_is_loaded_before_any_test_can_snapshot_sys_modules():
    assert "httpcore" in sys.modules


def test_a_connect_error_is_still_an_httpx_error_after_a_sys_modules_patch():
    # Prime httpx's exception map inside the patch, exactly as the polluting
    # tests do, then fail a connect after the patch has been unwound.
    with patch.dict(sys.modules, {"_trinity_4439_probe": object()}):
        with pytest.raises(httpx.HTTPError):
            asyncio.run(_hit_closed_port())
    with pytest.raises(httpx.HTTPError):
        asyncio.run(_hit_closed_port())
