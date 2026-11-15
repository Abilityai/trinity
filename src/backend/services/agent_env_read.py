"""What an agent's ``GET /api/credentials/read`` said about its ``.env``, for a
caller about to rewrite the whole file (trinity-enterprise#1073).

The agent reports a file in one of three ways: ``files`` (UTF-8 text),
``files_b64`` (bytes that are not UTF-8), or ``missing`` (it proved there is no
file, since ent#1066). A path in none of them was NOT read: a read error, a
link out of the home, or an agent image too old to report ``missing``.

A whole-file writer replaces the file with whatever it read plus its own
change. Treating anything but proven absence as an empty file deletes every
other credential on the agent while the action reports success — the bug on
the Nevermined bind and revoke and on the per-agent GitHub PAT write. So there
is one rule, here, for every such writer: text is text, proven absence is
empty, and everything else is refused by the caller with its own named reason.
"""
from __future__ import annotations

from typing import Optional, Tuple

TEXT = "text"
ABSENT = "absent"
NOT_UTF8 = "not_utf8"
UNCONFIRMED = "unconfirmed"


def classify_env_read(body: object, path: str = ".env") -> Tuple[str, Optional[str]]:
    """``(TEXT, content)``, ``(ABSENT, "")``, ``(NOT_UTF8, None)`` or
    ``(UNCONFIRMED, None)`` for ``path`` in a credentials-read response body.

    Raises ``ValueError`` for a body that is not the read route's shape; the
    caller treats that like an unreachable agent."""
    if not isinstance(body, dict):
        raise ValueError("credentials read response is not an object")
    files = body.get("files") or {}
    binary = body.get("files_b64") or {}
    missing = body.get("missing") or []
    if not isinstance(files, dict) or not isinstance(binary, dict) or not isinstance(missing, (list, tuple)):
        raise ValueError("credentials read response has an unexpected shape")
    content = files.get(path)
    if isinstance(content, str):
        return TEXT, content
    if path in binary:
        return NOT_UTF8, None
    if path in missing:
        return ABSENT, ""
    return UNCONFIRMED, None
