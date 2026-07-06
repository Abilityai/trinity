"""
Tests for #926 — `GET /api/version` exposes build-time git provenance.

Exercises `main._build_version_payload`, the pure dict-builder extracted
from the FastAPI handler so this test doesn't need to pull main.py's
full router graph (opentelemetry, slack_sdk, twilio, …) into the venv.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


def _load_builder():
    """Pull `_build_version_payload` out of main.py without executing the
    rest of the module. Reads the source, slices out the function block,
    and execs it into an isolated namespace — the function only depends
    on stdlib `os` and `pathlib`, so the exec is self-sufficient.
    """
    src_path = _BACKEND / "main.py"
    if not src_path.exists():
        pytest.skip("backend source not present")
    text = src_path.read_text()
    marker = "def _build_version_payload"
    start = text.find(marker)
    if start == -1:
        pytest.fail(f"_build_version_payload not found in {src_path}")
    # Slice from def → next top-level def
    rest = text[start:]
    end = rest.find("\n\n\n")  # function ends before the two blank lines
    snippet = rest[: end if end != -1 else len(rest)]
    # The function references `__file__` for the VERSION fallback path.
    # Inject the real main.py path so `Path(__file__).parent.parent.parent`
    # resolves to repo root, where the VERSION file actually lives.
    ns: dict = {"__file__": str(src_path)}
    exec(snippet, ns)
    return ns["_build_version_payload"]


def test_version_payload_includes_git_provenance_fields(monkeypatch):
    """All five #926 fields appear in the response when env vars are set."""
    monkeypatch.setenv("GIT_COMMIT", "abc123de" + "0" * 32)
    monkeypatch.setenv("GIT_COMMIT_SUBJECT", "feat(#926): build info surface")
    monkeypatch.setenv("GIT_COMMIT_TIMESTAMP", "2026-05-25T15:00:00+00:00")
    monkeypatch.setenv("GIT_BRANCH", "feature/926-version-build-info")
    monkeypatch.setenv("BUILD_DATE", "2026-05-25T15:05:00Z")

    build = _load_builder()
    payload = build(voice_enabled=False, edition="oss", enterprise_features=[])

    assert payload["git_commit"].startswith("abc123de")
    # Short SHA is the first 8 chars.
    assert payload["git_commit_short"] == "abc123de"
    assert payload["git_commit_subject"] == "feat(#926): build info surface"
    assert payload["git_commit_timestamp"] == "2026-05-25T15:00:00+00:00"
    assert payload["git_branch"] == "feature/926-version-build-info"
    assert payload["build_date"] == "2026-05-25T15:05:00Z"


def test_version_payload_falls_back_to_unknown_when_env_missing(monkeypatch):
    """Local-dev / volume-mount workflows leave the env vars unset.
    Response stays well-typed with `"unknown"` placeholders instead of
    `None` or KeyError. Caller (frontend chip) can suppress display."""
    for var in (
        "GIT_COMMIT",
        "GIT_COMMIT_SUBJECT",
        "GIT_COMMIT_TIMESTAMP",
        "GIT_BRANCH",
        "BUILD_DATE",
    ):
        monkeypatch.delenv(var, raising=False)

    build = _load_builder()
    payload = build(voice_enabled=False, edition="oss", enterprise_features=[])

    assert payload["git_commit"] == "unknown"
    assert payload["git_commit_short"] == "unknown"
    assert payload["git_commit_subject"] == "unknown"
    assert payload["git_commit_timestamp"] == "unknown"
    assert payload["git_branch"] == "unknown"
    assert payload["build_date"] == "unknown"


def test_version_payload_prefers_version_env(monkeypatch):
    """#993 — the build-stamped VERSION env var (e.g. `0.9.0+g4c640b6e`)
    takes precedence over the VERSION file so dev (bind-mount) and prod
    (build-arg) agree for the same commit."""
    monkeypatch.setenv("VERSION", "0.9.0+gdeadbeef")

    build = _load_builder()
    payload = build(voice_enabled=False, edition="oss", enterprise_features=[])

    assert payload["version"] == "0.9.0+gdeadbeef"
    # Version flows into the component descriptors too.
    assert payload["components"]["backend"] == "0.9.0+gdeadbeef"
    assert payload["components"]["base_image"] == "trinity-agent-base:0.9.0+gdeadbeef"


def test_version_payload_empty_version_env_falls_back_to_file(monkeypatch):
    """An empty VERSION env var must not shadow the file fallback —
    `os.getenv("VERSION") or None` treats "" as unset (#993)."""
    monkeypatch.setenv("VERSION", "")

    build = _load_builder()
    payload = build(voice_enabled=False, edition="oss", enterprise_features=[])

    # Repo VERSION file is read via the injected __file__ path.
    assert payload["version"] != ""
    assert payload["version"] != "unknown"  # file present in repo


def test_version_payload_preserves_existing_fields(monkeypatch):
    """Pre-#926 keys (version, platform, components, runtimes, voice_enabled)
    must still be present so existing callers don't break."""
    build = _load_builder()
    payload = build(voice_enabled=True, edition="oss", enterprise_features=[])

    for key in ("version", "platform", "components", "runtimes", "voice_enabled"):
        assert key in payload, f"pre-#926 field missing: {key!r}"
    assert payload["platform"] == "trinity"
    assert payload["voice_enabled"] is True
    assert "backend" in payload["components"]
    assert "agent_server" in payload["components"]
    assert "base_image" in payload["components"]



# -----------------------------------------------------------------------------
# #1443 — edition + enterprise_features surface
# -----------------------------------------------------------------------------


def test_version_payload_passes_edition_and_features_through():
    """#1443: `edition` and `enterprise_features` are pure passthrough —
    the builder must not recompute or normalize them (derivation lives in
    the handler, from entitlement_service)."""
    build = _load_builder()

    oss = build(voice_enabled=False, edition="oss", enterprise_features=[])
    assert oss["edition"] == "oss"
    assert oss["enterprise_features"] == []

    ent = build(
        voice_enabled=False,
        edition="enterprise",
        enterprise_features=["module-a", "module-b"],
    )
    assert ent["edition"] == "enterprise"
    assert ent["enterprise_features"] == ["module-a", "module-b"]

    # Deliberately MISMATCHED pair: if the builder re-derived edition from
    # the features list (instead of passing it through), this would flip to
    # "enterprise" and fail — pinning the pure-passthrough property the
    # docstring claims (review F5).
    mismatched = build(
        voice_enabled=False, edition="oss", enterprise_features=["module-a"]
    )
    assert mismatched["edition"] == "oss"


def test_version_payload_runtimes_lists_all_shipped_runtimes():
    """#1187/#1443 drive-by: the hardcoded runtimes list had gone stale
    (missing codex). Keep it matching the shipped runtime set."""
    build = _load_builder()
    payload = build(voice_enabled=False, edition="oss", enterprise_features=[])
    assert payload["runtimes"] == ["claude-code", "gemini-cli", "codex"]


def test_version_handler_imports_entitlement_service_function_locally():
    """#1443 static guard: `_set_for_testing` rebinds the MODULE GLOBAL in
    services/entitlement_service.py. A top-level
    `from services.entitlement_service import entitlement_service` in
    main.py would freeze the boot-time instance and silently bypass test
    stubs (and any future runtime rebinding). The import must live inside
    the get_version handler body — same pattern as routers/settings.py."""
    src = (_BACKEND / "main.py").read_text(encoding="utf-8")

    # No module-top-level (column-0) import of the singleton.
    for line in src.splitlines():
        assert not line.startswith(
            "from services.entitlement_service import"
        ), "entitlement_service must not be imported at main.py top level"

    # The handler itself must import it function-locally.
    idx = src.find("async def get_version")
    assert idx != -1, "get_version handler not found in main.py"
    handler_window = src[idx : idx + 2000]
    assert (
        "from services.entitlement_service import entitlement_service"
        in handler_window
    ), "get_version must import entitlement_service function-locally"
