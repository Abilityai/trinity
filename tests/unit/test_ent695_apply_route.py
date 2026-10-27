"""trinity-enterprise#695: `POST /api/systems/apply`, the HTTP surface of apply mode.

The request model, the gates, the status mapping, and the dry run's write
boundary proved through the REAL authenticated path (no dependency override:
a seeded user-scoped MCP key goes through the real `get_current_user`).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from pydantic import ValidationError  # noqa: E402

from models import SystemApplyRequest, SystemDeployRequest  # noqa: E402

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The request model
# ---------------------------------------------------------------------------

def test_apply_previews_unless_told_otherwise():
    """`dry_run` defaults TRUE on apply (a verb that changes live agents), while
    create keeps its `False` (AC6: `SystemDeployRequest` is untouched)."""
    assert SystemApplyRequest(manifest="x").dry_run is True
    assert SystemDeployRequest(manifest="x").dry_run is False


@pytest.mark.parametrize("stray", [{"mode": "apply"}, {"strict": True}, {"mode": "create"}])
def test_an_unknown_key_is_refused_not_dropped(stray):
    """A silently dropped key is how `mode: "apply"` on an older `/deploy` runs
    CREATE and suffixes the fleet; apply refuses one instead."""
    with pytest.raises(ValidationError):
        SystemApplyRequest(manifest="x", **stray)
