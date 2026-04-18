#!/usr/bin/env bash
# S7 Layer 3 Tier-1 test — verify that --force-with-lease catches the
# silent-clobber bug from issue #382.
#
# Two agents clone a shared bare repo, land on the same working branch,
# write conflicting workspace state, then both try to push with
# --force-with-lease using the SHA each observed at fetch time. The
# second push MUST be rejected with "stale info" / non-zero exit.
#
# References:
#   - /tmp/trinity-repro/p5_silent_clobber.sh  (demonstrates the bug)
#   - /tmp/trinity-repro/p5_fix_verified.sh    (demonstrates the fix)
#   - docker/base-image/agent_server/routers/git.py  (the site being fixed)
#
# Usage:  bash tests/git-sync/test_p5_branch_ownership.sh
# Exit:   0 on success, 1 on failure.

set -u

ROOT="$(mktemp -d -t trinity-s7-p5-XXXXXX)"
trap 'rm -rf "$ROOT"' EXIT
cd "$ROOT"

echo "=== S7 Layer 3: --force-with-lease rejects silent clobber ==="
echo "  work dir: $ROOT"
echo

git init --bare -q -b main bare.git
git clone -q bare.git seed
(
    cd seed
    git config user.email seed@t.local
    git config user.name Seed
    echo init > README.md
    git add .
    git commit -qm init
    git push -q -u origin main
    git checkout -qb trinity/alpaca/a702560e
    git push -q -u origin trinity/alpaca/a702560e
)
rm -rf seed

for a in agent-a agent-b; do
    git clone -q bare.git "$a"
    (
        cd "$a"
        git config user.email "$a@t.local"
        git config user.name "$a"
        git checkout -q trinity/alpaca/a702560e
    )
done

# Each agent observes the remote SHA at fetch time; that becomes the lease.
LEASE_A=$(cd agent-a && git rev-parse origin/trinity/alpaca/a702560e)
LEASE_B=$(cd agent-b && git rev-parse origin/trinity/alpaca/a702560e)
echo "[agent-a] observed lease SHA: $LEASE_A"
echo "[agent-b] observed lease SHA: $LEASE_B"

# Each agent mutates the branch locally.
(cd agent-a && echo ALPHA > workspace.txt && git add . && git commit -qm "Agent A state")
(cd agent-b && echo BETA  > workspace.txt && git add . && git commit -qm "Agent B state")

echo
echo "[push] agent-a pushes first with lease $LEASE_A"
cd agent-a
git push --force-with-lease=trinity/alpaca/a702560e:"$LEASE_A" origin trinity/alpaca/a702560e 2>&1 | sed 's/^/  agent-a: /'
EXIT_A=${PIPESTATUS[0]}
echo "  agent-a exit: $EXIT_A"
cd ..

echo
echo "[push] agent-b pushes second with STALE lease $LEASE_B — must be rejected"
cd agent-b
PUSH_OUT=$(git push --force-with-lease=trinity/alpaca/a702560e:"$LEASE_B" origin trinity/alpaca/a702560e 2>&1)
EXIT_B=$?
echo "$PUSH_OUT" | sed 's/^/  agent-b: /'
echo "  agent-b exit: $EXIT_B"
cd ..

echo
echo "=== RESULT ==="
FAIL=0
if [ "$EXIT_A" != "0" ]; then
    echo "FAIL: agent-a (lease matched) should have succeeded, got exit $EXIT_A"
    FAIL=1
fi
if [ "$EXIT_B" = "0" ]; then
    echo "FAIL: agent-b (stale lease) should have been REJECTED, got exit 0"
    FAIL=1
fi
if ! echo "$PUSH_OUT" | grep -qiE 'stale info|rejected|non-fast-forward'; then
    echo "FAIL: agent-b rejection message did not mention 'stale info' / 'rejected'"
    FAIL=1
fi

if [ "$FAIL" = "0" ]; then
    echo "OK: --force-with-lease rejects the losing push with stale-ref error."
    echo "    Agent-b now KNOWS it lost, so Trinity can surface a collision"
    echo "    entry to the operator queue."
    exit 0
else
    echo "The S7 Layer-3 invariant is violated."
    exit 1
fi
