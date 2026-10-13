"""
Chat session marker (#2958).

The Claude chat path resumes its OWN session id (``agent_state.chat_session_id``)
instead of ``--continue``. That id lives in agent memory, so the backend's JSONL
reaper (``session_cleanup_service.py``) cannot see it in any DB table. This leaf
publishes it to a small file the reaper reads over docker exec and adds to its
keep set; without it the chat's own JSONL is swept after an hour idle.

Written atomically after a successful chat turn, cleared on ``reset_session()``
and at agent-server startup (the in-memory id is gone after a restart, so a
leftover marker would only pin a JSONL nothing will ever resume).

The file holds ``{"session_id": <uuid>, "model": <str|null>}`` and nothing else.
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# The backend reader hard-codes the same path (``session_cleanup_service.
# CHAT_SESSION_MARKER_PATH``); tests/unit/test_2958_marker_path_parity.py pins
# the two together.
DEFAULT_MARKER_PATH = "/home/developer/.trinity/chat-session.json"

# TEST-ONLY override. Production never sets it: the backend reads the default
# path, so a divergent path here would silently drop the chat JSONL from the
# keep set.
_ENV_OVERRIDE = "TRINITY_CHAT_SESSION_FILE"


def marker_path() -> Path:
    """Resolved per call so a test's env override takes effect."""
    override = os.environ.get(_ENV_OVERRIDE)
    if override:
        return Path(override)
    return Path(DEFAULT_MARKER_PATH)


def write(session_id: Optional[str], model: Optional[str]) -> bool:
    """Atomically record the chat's session id. Never raises.

    Returns False (and logs a WARNING) when the id is not a strict UUID or the
    write fails. The caller keeps its in-memory id either way (#2958 T-C): a
    later reap of the unprotected JSONL is absorbed by the cold retry.
    """
    # Lazy: headless_executor imports state, which imports this module.
    from .headless_executor import _valid_session_id

    if not _valid_session_id(session_id):
        logger.warning(
            "event=chat_session_marker_write_failed reason=invalid_session_id"
        )
        return False
    path = marker_path()
    tmp_name: Optional[str] = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=".chat-session.", suffix=".tmp"
        )
        with os.fdopen(fd, "w") as fh:
            json.dump({"session_id": session_id, "model": model}, fh)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        tmp_name = None
        return True
    except Exception as exc:  # noqa: BLE001 — continuity must survive a full disk
        logger.warning(
            "event=chat_session_marker_write_failed reason=%s", type(exc).__name__
        )
        return False
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def clear() -> None:
    """Remove the marker. Never raises."""
    try:
        marker_path().unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "event=chat_session_marker_clear_failed reason=%s", type(exc).__name__
        )
