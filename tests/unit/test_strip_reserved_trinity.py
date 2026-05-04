"""
Unit tests for `_strip_reserved_trinity_entry` — closes the gap between the
agent server's auto-inject of `mcpServers.trinity` and the credentials
inject-route validator's `RESERVED_SERVER_NAMES = {"trinity"}` rule.

Without the strip, the credentials Save button is unusable for any agent
that has been started at least once — every loaded .mcp.json contains the
auto-injected trinity entry, and the validator rejects every save with
"MCP server name 'trinity' is reserved by Trinity". The agent re-injects
the canonical trinity entry on next startup, so stripping it before
validation is safe and restores the legitimate "edit other servers" flow.

Module under test:
    src/backend/routers/credentials.py::_strip_reserved_trinity_entry
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Direct module-load: avoid the full router import chain (FastAPI deps,
# database, services), so this test stays a pure unit test.
import importlib.util

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"


def _load_strip_helper():
    """Pull the helper out of routers/credentials.py without importing
    the full module (which would require database, FastAPI, etc.)."""
    src = (_BACKEND / "routers" / "credentials.py").read_text()

    # Slice out the helper plus its dependency on `json` (already imported
    # in the snippet's scope).
    start = src.index("def _strip_reserved_trinity_entry")
    end = src.index("\n\n\n", start)
    helper_src = "import json\n" + src[start:end]

    ns = {}
    exec(compile(helper_src, "<strip-helper>", "exec"), ns)
    return ns["_strip_reserved_trinity_entry"]


@pytest.fixture(scope="module")
def strip():
    return _load_strip_helper()


# ---------------------------------------------------------------------------
# Happy path — the actual user scenario
# ---------------------------------------------------------------------------

def test_strips_trinity_entry_from_typical_loaded_file(strip):
    """The exact shape the agent's inject_trinity_mcp_if_configured()
    writes — http transport, mcp-server URL, Bearer auth header.
    """
    content = json.dumps({
        "mcpServers": {
            "trinity": {
                "type": "http",
                "url": "http://mcp-server:8080/mcp",
                "headers": {
                    "Authorization": "Bearer trinity_mcp_realkeyABCDEF"
                }
            }
        }
    })

    out, did_strip = strip(content)

    assert did_strip is True
    parsed = json.loads(out)
    assert "trinity" not in parsed["mcpServers"]


def test_strips_trinity_keeping_other_servers(strip):
    """User wants to add/edit context7 (or any other server) WHILE the
    auto-injected trinity entry is in the file. Other entries must
    survive the strip unchanged.
    """
    content = json.dumps({
        "mcpServers": {
            "trinity": {"type": "http", "url": "http://mcp-server:8080/mcp"},
            "context7": {
                "command": "npx",
                "args": ["-y", "@upstash/context7-mcp@latest"],
            },
        }
    })

    out, did_strip = strip(content)

    parsed = json.loads(out)
    assert did_strip is True
    assert "trinity" not in parsed["mcpServers"]
    assert parsed["mcpServers"]["context7"]["command"] == "npx"
    assert parsed["mcpServers"]["context7"]["args"] == [
        "-y", "@upstash/context7-mcp@latest"
    ]


def test_strips_trinity_with_corrupted_bearer(strip):
    """The exact symptom the user hit: a corrupted bearer string where
    a Python error message ended up in place of the API key. The strip
    drops the entry regardless of what's inside it.
    """
    content = json.dumps({
        "mcpServers": {
            "trinity": {
                "type": "http",
                "url": "http://mcp-server:8080/mcp",
                "headers": {
                    "Authorization": "Bearer sqlite3.IntegrityError: NOT NULL constraint failed: mcp_api_keys.key_prefix"
                }
            }
        }
    })

    out, did_strip = strip(content)

    assert did_strip is True
    parsed = json.loads(out)
    assert "trinity" not in parsed["mcpServers"]


# ---------------------------------------------------------------------------
# No-op cases — the strip leaves content alone when it shouldn't fire
# ---------------------------------------------------------------------------

def test_no_strip_when_trinity_absent(strip):
    """User config that doesn't include trinity at all — pass through."""
    content = json.dumps({
        "mcpServers": {
            "context7": {"command": "npx", "args": ["-y", "context7"]},
        }
    })

    out, did_strip = strip(content)

    assert did_strip is False
    assert out == content


def test_no_strip_when_no_servers(strip):
    """Empty mcpServers dict — nothing to strip."""
    content = '{"mcpServers": {}}'

    out, did_strip = strip(content)

    assert did_strip is False
    assert out == content


def test_no_strip_when_no_mcpservers_key(strip):
    """Root JSON without mcpServers key (validator will reject this on
    its own merits) — strip leaves content alone for validator to surface."""
    content = '{"foo": "bar"}'

    out, did_strip = strip(content)

    assert did_strip is False


def test_no_strip_when_root_is_array(strip):
    """Pathological case: root is an array, not an object. Pass through
    so the validator can produce its proper error message."""
    content = '[]'

    out, did_strip = strip(content)

    assert did_strip is False
    assert out == content


# ---------------------------------------------------------------------------
# Robustness — malformed input falls through to the validator
# ---------------------------------------------------------------------------

def test_invalid_json_passes_through(strip):
    """Malformed JSON: do NOT crash; return the input unchanged so the
    validator's `validate_mcp_config` can produce a clean
    `.mcp.json is not valid JSON` 400 error.
    """
    content = '{"mcpServers": {trunc'

    out, did_strip = strip(content)

    assert did_strip is False
    assert out == content


def test_servers_value_is_not_object(strip):
    """`mcpServers` field present but with wrong type. The validator
    will reject this; the strip must not coerce or crash."""
    content = '{"mcpServers": "not an object"}'

    out, did_strip = strip(content)

    assert did_strip is False
    assert out == content


# ---------------------------------------------------------------------------
# Output shape — strip should preserve a re-serializable JSON
# ---------------------------------------------------------------------------

def test_stripped_output_is_valid_json(strip):
    content = json.dumps({"mcpServers": {"trinity": {"type": "http", "url": "x"}}})
    out, _ = strip(content)
    json.loads(out)  # raises if invalid


def test_stripped_output_preserves_top_level_keys_other_than_mcpservers(strip):
    """The validator only allows `mcpServers` at the root, but the strip
    must not silently drop other top-level keys — let the validator catch
    them with its own error.
    """
    content = json.dumps({
        "mcpServers": {"trinity": {"type": "http", "url": "x"}},
        "rogue_key": "still here",
    })

    out, did_strip = strip(content)

    parsed = json.loads(out)
    assert did_strip is True
    assert "rogue_key" in parsed
