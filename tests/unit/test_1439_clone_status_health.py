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
import json
import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_STARTUP_SH = _REPO_ROOT / "docker" / "base-image" / "startup.sh"
_INFO_PY = _REPO_ROOT / "docker" / "base-image" / "agent_server" / "routers" / "info.py"


# ---------------------------------------------------------------------------
# 1. Backend aggregation — clone_status=failed => UNHEALTHY + fixed issue
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
        from services import monitoring_service
        from db_models import AgentHealthStatus

        status, issues = monitoring_service.aggregate_health(*_make_inputs(clone_status="failed"))
        assert status == AgentHealthStatus.UNHEALTHY
        assert "Agent identity clone failed" in issues

    def test_clone_ok_is_healthy(self):
        from services import monitoring_service
        from db_models import AgentHealthStatus

        status, issues = monitoring_service.aggregate_health(*_make_inputs(clone_status="ok"))
        assert status == AgentHealthStatus.HEALTHY
        assert issues == []

    def test_clone_none_is_healthy(self):
        # Older agent images omit the key → None → must never flip a healthy agent.
        from services import monitoring_service
        from db_models import AgentHealthStatus

        status, _ = monitoring_service.aggregate_health(*_make_inputs(clone_status=None))
        assert status == AgentHealthStatus.HEALTHY

    def test_issue_string_is_injection_safe(self):
        # The surfaced issue is a fixed server constant — no '; ' that could
        # forge extra rows in the '; '-joined issues serialization (security review).
        from services import monitoring_service

        _, issues = monitoring_service.aggregate_health(*_make_inputs(clone_status="failed"))
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
        gi = (_REPO_ROOT / "src" / "backend" / "services" / "git_service.py").read_text()
        assert ".trinity-clone-tmp/" in gi
