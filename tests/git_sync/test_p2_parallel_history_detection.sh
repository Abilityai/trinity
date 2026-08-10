#!/usr/bin/env bash
# P2 — Parallel-history detection test.
#
# Sets up the P2 scenario (agent working branch and origin/main share only a
# stale common ancestor) and asserts the agent-server's get_git_status() now
# surfaces `common_ancestor_sha` and `common_ancestor_age_days` so the
# frontend can detect parallel history before rendering the two-button modal.
#
# Runs the real get_git_status() code by invoking the FastAPI endpoint
# in-process via python. No Docker, no network.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ROOT=/tmp/trinity-p2-detection
rm -rf "$ROOT"
mkdir -p "$ROOT"
cd "$ROOT"

PYTHON_BIN="${PYTHON_BIN:-${REPO_ROOT}/.venv-test/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
  # Fallback: let the user set PYTHON_BIN to any python with fastapi.
  PYTHON_BIN=python3
fi

echo "=== TEST P2 DETECTION: common_ancestor_sha in git status ==="
echo "Using python: $PYTHON_BIN"
echo

# Pre-compute ISO 8601 dates (Apple Git chokes on 'N days ago' relative dates).
BASE_DATE="$(date -u -v-90d +%Y-%m-%dT%H:%M:%S)"
X_DATE="$(date -u -v-60d +%Y-%m-%dT%H:%M:%S)"
XPRIME_DATE="$(date -u -v-1d +%Y-%m-%dT%H:%M:%S)"

# 1. Bare repo + seed history: base -> X
git init --bare -q -b main bare.git

git clone -q bare.git seed
(
  cd seed
  git config user.email seed@t.local; git config user.name Seed
  git config commit.gpgsign false
  echo "base" > README.md
  git add .
  GIT_AUTHOR_DATE="$BASE_DATE" GIT_COMMITTER_DATE="$BASE_DATE" \
      git commit -qm "base"
  echo "v1 config" > trader.conf
  git add .
  GIT_AUTHOR_DATE="$X_DATE" GIT_COMMITTER_DATE="$X_DATE" \
      git commit -qm "Add polymarket trader configuration"
  git push -q -u origin main

  git checkout -qb trinity/polygon-vybe/abc12345
  git push -q -u origin trinity/polygon-vybe/abc12345
)
rm -rf seed

# 2. Rewriter force-pushes main to a parallel commit X' (same parent = base).
git clone -q bare.git rewriter
(
  cd rewriter
  git config user.email admin@t.local; git config user.name Admin
  git config commit.gpgsign false
  git checkout -q main
  git reset --hard HEAD~1   # drop X, keep base
  echo "v2 config with extra fields" > trader.conf
  git add .
  GIT_AUTHOR_DATE="$XPRIME_DATE" GIT_COMMITTER_DATE="$XPRIME_DATE" \
      git commit -qm "Add polymarket trader configuration"
  git push --force -q origin main
)
rm -rf rewriter

# 3. Agent container workspace: clone working branch + add unpushed commit.
git clone -q -b trinity/polygon-vybe/abc12345 bare.git home
(
  cd home
  git config user.email a@t.local; git config user.name AgentVybe
  git config commit.gpgsign false
  echo "trade history + brier log" > workspace.txt
  git add .
  git commit -qm "Trinity sync: workspace state"
  git fetch -q origin
)

# 4. Invoke the real get_git_status() handler in-process and assert on the
#    new fields. The handler reads from /home/developer by default — we
#    monkeypatch that path to our throwaway repo.
export TEST_HOME="$ROOT/home"
PYTHONPATH="$REPO_ROOT/docker/base-image:${PYTHONPATH:-}" \
"$PYTHON_BIN" - <<'PYEOF'
import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

test_home = Path(os.environ["TEST_HOME"])

# Pre-import to resolve deps before we patch Path
from agent_server.routers import git as git_router

with patch.object(git_router, "Path") as PathMock:
    def path_ctor(arg):
        if arg == "/home/developer":
            return test_home
        return Path(arg)
    PathMock.side_effect = path_ctor
    result = asyncio.run(git_router.get_git_status())

print(json.dumps(result, indent=2, default=str))

assert result.get("git_enabled") is True, "git_enabled must be True"
assert "common_ancestor_sha" in result, "missing common_ancestor_sha field"
assert "common_ancestor_age_days" in result, "missing common_ancestor_age_days field"

sha = result["common_ancestor_sha"]
age = result["common_ancestor_age_days"]

assert isinstance(sha, str) and len(sha) == 40, \
    f"common_ancestor_sha must be a 40-char sha, got {sha!r}"
assert isinstance(age, (int, float)), \
    f"common_ancestor_age_days must be numeric, got {type(age).__name__}"
assert age >= 30, \
    f"common_ancestor_age_days must be >= 30 for parallel-history scenario, got {age}"

assert "ahead" in result and "behind" in result, "ahead/behind must remain top-level"
assert result["ahead"] >= 1, f"expected ahead >= 1, got {result['ahead']}"
assert result["behind"] >= 1, f"expected behind >= 1, got {result['behind']}"

assert "pull_branch" in result, "pull_branch must be returned so UI copy can avoid hardcoding 'main'"
assert result["pull_branch"] == "main", \
    f"pull_branch for trinity/* working branch should be 'main', got {result['pull_branch']!r}"

print("\n[OK] common_ancestor_sha surfaced, age >= 30d, ahead/behind preserved, pull_branch echoed")
PYEOF

echo
echo "=== PASS ==="
