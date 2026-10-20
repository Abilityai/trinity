#!/usr/bin/env bash
# Resolve the newest `dev` SHA that Tier 2 marked green (#2945).
#
# Two markers, read in order:
#   1. the `dev-green` tag, moved by dev-ci-status.yml on every all-green SHA —
#      trusted only if it is an ancestor of origin/dev (a tag that is not on
#      dev is a stale or hand-moved marker, not a verdict);
#   2. the `dev-ci` commit status, walked back from the dev tip.
#
# Prints the SHA and exits 0; exits 1 with a message on stderr when neither
# marker resolves. Callers: the merge train's Phase 0a, and `pick-task` when it
# creates a worktree (`--base "$(scripts/ci/dev-last-green.sh)"`).
#
# Env: REPO (default abilityai/trinity), REMOTE (default origin), DEPTH (default 60).
set -euo pipefail

REPO="${REPO:-abilityai/trinity}"
REMOTE="${REMOTE:-origin}"
DEPTH="${DEPTH:-60}"

# `+` forces the update: dev-green MOVES, and a plain tag fetch refuses to
# clobber a tag that already exists locally.
git fetch -q "$REMOTE" dev "+refs/tags/dev-green:refs/tags/dev-green" 2>/dev/null \
  || git fetch -q "$REMOTE" dev

tip=$(git rev-parse "$REMOTE/dev")

if tag=$(git rev-parse -q --verify 'refs/tags/dev-green^{commit}' 2>/dev/null); then
  if git merge-base --is-ancestor "$tag" "$tip"; then
    echo "$tag"
    exit 0
  fi
  echo "warning: dev-green ($tag) is not on $REMOTE/dev — falling back to the dev-ci status walk" >&2
fi

for sha in $(git rev-list --max-count="$DEPTH" "$tip"); do
  state=$(gh api "repos/$REPO/commits/$sha/status" \
    --jq '[.statuses[] | select(.context=="dev-ci")] | .[0].state // ""' 2>/dev/null || true)
  if [ "$state" = "success" ]; then
    echo "$sha"
    exit 0
  fi
done

echo "error: no dev-ci success within $DEPTH commits of $REMOTE/dev — Tier 2 has not marked anything green yet, or dev has been red for $DEPTH merges" >&2
exit 1
