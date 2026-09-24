"""The agent server's file write is atomic and a compare-and-swap (#2915).

The platform's operator-queue write-back re-reads the agent's queue file, then
writes it back with `if_match=<sha256 of what it read>`. Both writers used to
be plain overwrites, so an entry the agent appended between the platform's read
and its write was clobbered — silently. Loaded by path like test_1795 (the
router must stay standalone-importable); the helpers are exercised on a tmp dir
because the handler itself is fenced to /home/developer.
"""
import importlib.util
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "_test2915_agent_files",
        str(_REPO_ROOT / "docker/base-image/agent_server/routers/files.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_hash_is_over_the_utf8_text_the_download_endpoint_returns():
    mod = _load()
    import hashlib
    assert mod.content_sha256("héllo\n") == hashlib.sha256("héllo\n".encode("utf-8")).hexdigest()


def test_if_match_holds_for_the_content_that_was_read_and_refuses_after_a_change(tmp_path):
    mod = _load()
    f = tmp_path / "operator-queue.json"
    f.write_text('{"requests": []}', encoding="utf-8")
    sha = mod.content_sha256(f.read_text(encoding="utf-8"))
    assert mod.if_match_holds(f, sha) is True
    f.write_text('{"requests": [{"id": "appended"}]}', encoding="utf-8")   # the agent wrote in between
    assert mod.if_match_holds(f, sha) is False


def test_no_if_match_and_a_missing_file_both_hold(tmp_path):
    mod = _load()
    assert mod.if_match_holds(tmp_path / "absent.json", None) is True
    assert mod.if_match_holds(tmp_path / "absent.json", "deadbeef") is True


def test_a_file_that_is_not_utf8_matches_the_text_the_download_endpoint_serves(tmp_path):
    """Both sides hash the SAME text: the download endpoint reads with
    `errors='replace'`, so a stray byte must not wedge delivery behind an
    endless 412 — the caller's hash over the served text still matches, and
    a stale hash still does not."""
    mod = _load()
    f = tmp_path / "bin"
    f.write_bytes(b"\xff\xfe\x00 not text")
    served = f.read_text(encoding="utf-8", errors="replace")
    assert mod.if_match_holds(f, mod.content_sha256(served)) is True
    assert mod.if_match_holds(f, "stale") is False


def test_write_is_atomic_and_leaves_no_temp_file(tmp_path):
    mod = _load()
    f = tmp_path / "operator-queue.json"
    f.write_text("old", encoding="utf-8")
    mod.write_text_atomic(f, "new content")
    assert f.read_text(encoding="utf-8") == "new content"
    assert [p.name for p in tmp_path.iterdir()] == ["operator-queue.json"]   # no .tmp-* left behind


def test_the_handler_calls_the_helpers_it_documents():
    """The two guards are wired at the handler, not merely defined (#2826 C1)."""
    src = (_REPO_ROOT / "docker/base-image/agent_server/routers/files.py").read_text()
    handler = src[src.index("async def update_file("):]
    handler = handler[:handler.index("@router.")] if "@router." in handler else handler
    assert "if not if_match_holds(requested_path, if_match):" in handler
    assert "status_code=412" in handler
    assert "write_text_atomic(requested_path, request.content)" in handler
    assert "requested_path.write_text(" not in handler


def test_update_file_containment_is_resolved_path_not_string_prefix():
    """The workspace guard is resolved-path containment (`is_relative_to`, the
    barrier `create_folder` already uses), not a string prefix: a sibling name
    that shares the prefix (`/home/developer2/…`) must be refused like an
    absolute escape or a relative traversal — 403 before any filesystem touch."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    mod = _load()
    app = FastAPI()
    app.include_router(mod.router)
    client = TestClient(app, raise_server_exceptions=True)
    for candidate in ("/home/developer2/x.txt", "/home/developer-old/x.txt",
                      "/etc/passwd", "../../etc/passwd"):
        resp = client.put("/api/files", params={"path": candidate}, json={"content": "x"})
        assert resp.status_code == 403, (candidate, resp.status_code, resp.text)
