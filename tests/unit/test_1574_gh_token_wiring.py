"""#1574 — the managed GitHub PAT must also authenticate the `gh` CLI + REST API.

Trinity injects the resolved PAT as GITHUB_PAT (git). This wires the SAME token as
GH_TOKEN/GITHUB_TOKEN (what `gh` + the REST API read) at every point GITHUB_PAT is
injected, installs `gh` in the base image, and keeps the credential sanitizer
covering the new vars.

- `_patch_env_github_pat` (the no-restart .env propagation) is unit-tested directly.
- The create / recreate / startup / Dockerfile wiring is asserted at source level
  (static guards) so a future refactor can't silently drop a site.
"""
import os
import sys
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


class TestEnvPropagationPatcher:
    """The no-restart .env patch mirrors the PAT onto GH_TOKEN/GITHUB_TOKEN."""

    def _patch(self):
        from services.github_pat_propagation_service import _patch_env_github_pat
        return _patch_env_github_pat

    def test_adds_gh_token_vars_when_absent(self):
        patch = self._patch()
        out = patch('FOO="bar"\nGITHUB_PAT="ghp_old"\n', "ghp_new")
        assert 'GITHUB_PAT="ghp_new"' in out
        assert 'GH_TOKEN="ghp_new"' in out
        assert 'GITHUB_TOKEN="ghp_new"' in out
        assert 'FOO="bar"' in out            # unrelated lines preserved
        assert 'ghp_old' not in out

    def test_replaces_all_three_in_place(self):
        patch = self._patch()
        env = 'GITHUB_PAT="a"\nGH_TOKEN="a"\nGITHUB_TOKEN="a"\nKEEP="1"\n'
        out = patch(env, "b")
        assert out.count('="b"') == 3
        assert '"a"' not in out
        assert 'KEEP="1"' in out

    def test_does_not_touch_lookalike_keys(self):
        patch = self._patch()
        out = patch('SOME_GITHUB_PAT_LIKE="not-this"\nGITHUB_PAT="old"\n', "new")
        assert 'SOME_GITHUB_PAT_LIKE="not-this"' in out
        assert 'GITHUB_PAT="new"' in out

    def test_value_is_mirrored_not_diverged(self):
        patch = self._patch()
        out = patch('GITHUB_PAT="old"\n', "tok123")
        for key in ("GITHUB_PAT", "GH_TOKEN", "GITHUB_TOKEN"):
            assert f'{key}="tok123"' in out


class TestWiringSites:
    """Every GITHUB_PAT injection point also sets the gh vars (static guard)."""

    def test_create_path_sets_gh_vars(self):
        src = (_BACKEND / "services" / "agent_service" / "crud.py").read_text()
        assert "env_vars['GH_TOKEN']" in src
        assert "env_vars['GITHUB_TOKEN']" in src

    def test_recreate_path_sets_gh_vars(self):
        src = (_BACKEND / "services" / "agent_service" / "lifecycle.py").read_text()
        assert "GH_TOKEN" in src and "GITHUB_TOKEN" in src

    def test_startup_exports_gh_vars_from_pat(self):
        sh = (_ROOT / "docker" / "base-image" / "startup.sh").read_text()
        assert 'export GH_TOKEN="${GITHUB_PAT}"' in sh
        assert 'export GITHUB_TOKEN="${GITHUB_PAT}"' in sh

    def test_base_image_installs_gh(self):
        df = (_ROOT / "docker" / "base-image" / "Dockerfile").read_text()
        assert "cli.github.com/packages" in df
        assert re.search(r"install -y[^\n]*\bgh\b", df), "gh not apt-installed in base image"


class TestSanitizerCoversNewVars:
    """The credential sanitizer must redact GH_TOKEN / GITHUB_TOKEN values."""

    def test_token_pattern_matches_new_vars(self):
        from utils.credential_sanitizer import SENSITIVE_KEY_PATTERNS
        compiled = [re.compile(p, re.IGNORECASE) for p in SENSITIVE_KEY_PATTERNS]
        for var in ("GH_TOKEN", "GITHUB_TOKEN"):
            assert any(rx.fullmatch(var) or rx.match(var) for rx in compiled), \
                f"{var} not covered by the credential sanitizer key patterns"
