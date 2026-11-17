"""#4357 — the MCP server's copy of the depth-reset sentence equals the backend's.

A depth refusal (``inter_agent_depth_exceeded``) carries ``resets``: the
sentence telling the calling model what lowers the depth. The backend owns it
(``services/chat_signals.py::InterAgentDepthExceeded.RESETS``). The MCP server
keeps a copy, ``DEPTH_RESETS`` in ``src/mcp-server/src/client.ts``, and uses it
when a refusal body carries no ``resets`` of its own (an older backend, or an
empty value). Nothing tied the two together, so the backend sentence could be
reworded and the MCP fallback would keep telling a model the old reset rule.

This reads ``client.ts`` as text because the consumer of that copy is the MCP
server, which this island cannot import; the backend side is the live class
attribute. The TypeScript side is executed by ``chat-depth.test.ts``.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services.chat_signals import InterAgentDepthExceeded  # noqa: E402

_CLIENT_TS = _REPO / "src" / "mcp-server" / "src" / "client.ts"


def _ts_depth_resets() -> str:
    """Join the string literals of ``export const DEPTH_RESETS = "…" + "…";``."""
    source = _CLIENT_TS.read_text(encoding="utf-8")
    match = re.search(r"export const DEPTH_RESETS\s*=\s*(.*?);", source, re.S)
    assert match, "DEPTH_RESETS is no longer declared in client.ts"
    literals = re.findall(r'"((?:[^"\\]|\\.)*)"', match.group(1))
    assert literals, "DEPTH_RESETS is no longer built from string literals"
    return "".join(json.loads(f'"{lit}"') for lit in literals)


def test_the_mcp_fallback_reset_sentence_equals_the_backends():
    assert _ts_depth_resets() == InterAgentDepthExceeded.RESETS


def test_the_backend_sends_the_same_sentence_it_declares():
    refusal = InterAgentDepthExceeded("a", "b", depth=9, max_depth=8)
    assert refusal.detail()["resets"] == InterAgentDepthExceeded.RESETS
