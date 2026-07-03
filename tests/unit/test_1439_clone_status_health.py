"""#1439 — GitHub-template clone race fix + silent-failure surfacing.

Three surfaces:
1. Backend health aggregation surfaces a failed identity clone as UNHEALTHY
   with a fixed, server-controlled issue string (never agent-supplied strings).
2. The agent-server `_clone_status` parses the UNTRUSTED `.git-clone-status`
   marker defensively (size-cap, enum-whitelist, absence == ok). Mirrored here
   and drift-guarded against `info.py`.
3. `startup.sh` no longer clones into the live `/home/developer` (the race):
   it clones into a home-volume temp dir and tar-merges, clears stale markers on
   the success/restart/shallow paths, and preserves the PAT-in-logs redaction.
"""
import importlib.util
import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _REPO_ROOT / "src" / "backend"
_STARTUP_SH = _REPO_ROOT / "docker" / "base-image" / "startup.sh"
_INFO_PY = _BACKEND.parent.parent / "docker" / "base-image" / "agent_server" / "routers" / "info.py"

if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


# ---------------------------------------------------------------------------
# Hermetic monitoring_service load (#762 escape hatch).
#
# `from services import monitoring_service` is order-fragile under
# pytest-randomly: sys.modules pollution left by earlier tests corrupts the
# module (or its lazy deps) and the aggregation tests fail intermittently
# (regression-diff #1439). Load monitoring_service.py standalone via importlib —
# the same pattern tests/integration/test_monitoring_service.py uses — stubbing
# its load-time deps and restoring them after, so these pure aggregate_health
# tests are deterministic regardless of test order.
# ---------------------------------------------------------------------------
_STUBBED_MODULE_NAMES = [
    "database",
    "services.agent_auth",
    "services.docker_service",
    "services.docker_utils",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot/restore stubbed names so per-test mutations never leak (#762)."""
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _load_monitoring_service():
    """exec monitoring_service.py under a unique name, isolated from pollution."""
    saved = {k: sys.modules.get(k) for k in _STUBBED_MODULE_NAMES}

    fake_db = types.ModuleType("database")
    fake_db.db = MagicMock()
    sys.modules["database"] = fake_db

    fake_auth = types.ModuleType("services.agent_auth")
    fake_auth.agent_httpx_client = MagicMock()
    sys.modules["services.agent_auth"] = fake_auth

    sys.modules["services.docker_service"] = types.ModuleType("services.docker_service")
    sys.modules["services.docker_utils"] = types.ModuleType("services.docker_utils")

    try:
        spec = importlib.util.spec_from_file_location(
            "monitoring_service_1439_under_test",
            str(_BACKEND / "services" / "monitoring_service.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


_MS = _load_monitoring_service()


# ---------------------------------------------------------------------------
# 1. Backend aggregation — clone_status=failed => UNHEALTHY + fixed issue.
#    Statuses compared as strings (AgentHealthStatus is a str-enum) to avoid
#    any cross-load enum-identity issues.
# ---------------------------------------------------------------------------

def _make_inputs(clone_status=None, runtime_available=True):
    from db_models import DockerHealthCheck, NetworkHealthCheck, BusinessHealthCheck

    now = "2026-07-03T00:00:00Z"
    docker = DockerHealthCheck(agent_name="x", container_status="running", checked_at=now)
    network = NetworkHealthCheck(
        agent_name="x", reachable=True, status_code=200, error=None, checked_at=now
    )
    business = BusinessHealthCheck(
        agent_name="x",
        status="healthy",
        runtime_available=runtime_available,
        claude_available=True,
        clone_status=clone_status,
        checked_at=now,
    )
    return docker, network, business


class TestCloneStatusAggregation:
    def test_clone_failed_is_unhealthy(self):
        status, issues = _MS.aggregate_health(*_make_inputs(clone_status="failed"))
        assert status == "unhealthy"  # AgentHealthStatus is a str-enum (value compare)
        assert "Agent identity clone failed" in issues

    def test_clone_ok_is_healthy(self):
        status, issues = _MS.aggregate_health(*_make_inputs(clone_status="ok"))
        assert status == "healthy"
        assert issues == []

    def test_clone_none_is_healthy(self):
        # Older agent images omit the key → None → must never flip a healthy agent.
        status, _ = _MS.aggregate_health(*_make_inputs(clone_status=None))
        assert status == "healthy"

    def test_issue_string_is_injection_safe(self):
        # The surfaced issue is a fixed server constant — no '; ' that could
        # forge extra rows in the '; '-joined issues serialization (security review).
        _, issues = _MS.aggregate_health(*_make_inputs(clone_status="failed"))
        assert all("; " not in i for i in issues)

    def test_business_model_has_clone_status_field(self):
        from db_models import BusinessHealthCheck

        assert BusinessHealthCheck(agent_name="x", clone_status="failed", checked_at="t").clone_status == "failed"
        # Defaults to None so older images (no key) are treated as healthy.
        assert BusinessHealthCheck(agent_name="x", checked_at="t").clone_status is None


# ---------------------------------------------------------------------------
# 2. Agent-server _clone_status — defensive untrusted-input parsing.
#    Mirror of agent_server.routers.info._clone_status (agent-server uses
#    relative imports that don't resolve on the host), drift-guarded below.
# ---------------------------------------------------------------------------

def _clone_status_mirror(home) -> str:
    path = os.path.join(home, ".git-clone-status")
    try:
        if os.path.getsize(path) > 4096:
            return "ok"
        with open(path, "r") as f:
            data = json.loads(f.read(4096))
    except (OSError, ValueError):
        return "ok"
    if isinstance(data, dict) and data.get("status") == "failed":
        return "failed"
    return "ok"


class TestCloneStatusParsing:
    def test_absent_is_ok(self, tmp_path):
        assert _clone_status_mirror(str(tmp_path)) == "ok"

    def test_explicit_failed(self, tmp_path):
        (tmp_path / ".git-clone-status").write_text('{"status":"failed","repo":"x/y","branch":"main"}')
        assert _clone_status_mirror(str(tmp_path)) == "failed"

    def test_explicit_ok(self, tmp_path):
        (tmp_path / ".git-clone-status").write_text('{"status":"ok"}')
        assert _clone_status_mirror(str(tmp_path)) == "ok"

    def test_malformed_is_ok(self, tmp_path):
        (tmp_path / ".git-clone-status").write_text("not json {{{")
        assert _clone_status_mirror(str(tmp_path)) == "ok"

    def test_oversized_is_ok(self, tmp_path):
        # >4KB untrusted content must not be trusted-as-failed (DoS/forgery guard).
        (tmp_path / ".git-clone-status").write_text('{"status":"failed"}' + "x" * 5000)
        assert _clone_status_mirror(str(tmp_path)) == "ok"

    def test_non_dict_is_ok(self, tmp_path):
        (tmp_path / ".git-clone-status").write_text('"failed"')
        assert _clone_status_mirror(str(tmp_path)) == "ok"

    def test_mirror_matches_source(self):
        """Drift guard: info.py must keep the same defensive properties."""
        src = _INFO_PY.read_text()
        assert "def _clone_status(" in src
        body = src.split("def _clone_status(", 1)[1].split("\n@router", 1)[0]
        assert "4096" in body                       # size cap
        assert ".git-clone-status" in body
        assert 'data.get("status") == "failed"' in body   # explicit-failed only
        assert 'return "ok"' in body and 'return "failed"' in body  # enum whitelist
        # The code must not READ agent-controlled fields into the /health surface
        # (check the code accessors, not the explanatory docstring prose).
        for leaked in ("repo", "branch", "error"):
            assert f'data.get("{leaked}")' not in body
            assert f'["{leaked}"]' not in body
        assert '"clone_status": _clone_status()' in src   # wired into /health


# ---------------------------------------------------------------------------
# 3. startup.sh static regression guards (the clone-race fix)
# ---------------------------------------------------------------------------

class TestStartupShCloneRace:
    def _startup(self):
        return _STARTUP_SH.read_text()

    def test_git_sync_clones_to_temp_not_home(self):
        s = self._startup()
        assert "/home/developer/.trinity-clone-tmp" in s
        # The racy full-history `git clone ... /home/developer` is gone.
        assert 'CLONE_CMD="git clone -b ${CLONE_BRANCH} ${CLONE_URL} /home/developer"' not in s
        assert 'CLONE_CMD="git clone ${CLONE_URL} /home/developer"' not in s

    def test_temp_dir_on_home_volume_not_tmp(self):
        # Disk-backed home volume, not the 512 MB RAM /tmp tmpfs (#1098).
        s = self._startup()
        assert 'CLONE_TMP="/home/developer/.trinity-clone-tmp"' in s
        assert 'CLONE_TMP="/tmp' not in s

    def test_no_destructive_rm_of_home_before_clone(self):
        # The racy `rm -rf /home/developer/*` pre-clean is removed.
        assert "rm -rf /home/developer/* /home/developer/.[!.]*" not in self._startup()

    def test_merge_via_tar(self):
        s = self._startup()
        assert "tar cf - ." in s and "tar xf -" in s

    def test_stale_marker_cleared_on_success_restart_and_shallow(self):
        # git-sync success, git-sync restart, and shallow-success paths each clear it.
        assert self._startup().count("rm -f /home/developer/.git-clone-status") >= 3

    def test_temp_dir_cleaned_up_on_failure(self):
        # No leftover partial clone (and no lingering PAT-bearing .git/config).
        assert 'rm -rf "${CLONE_TMP}"' in self._startup()

    def test_pat_redaction_preserved(self):
        # git errors can echo the credentialed URL — the redaction must remain.
        assert "oauth2:[^@]*@" in self._startup()

    def test_no_unredacted_clone_url_echoed(self):
        s = self._startup()
        assert 'echo "${CLONE_URL}"' not in s
        assert 'echo "${CLONE_CMD}"' not in s

    def test_clone_tmp_gitignored(self):
        # Defense-in-depth: a crash-orphaned temp clone must never be committed.
        gi = (_BACKEND / "services" / "git_service.py").read_text()
        assert ".trinity-clone-tmp/" in gi
