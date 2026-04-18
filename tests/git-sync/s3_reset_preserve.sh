#!/usr/bin/env bash
# S3 reset-preserve-state regression (pure git, no Trinity stack).
#
# Mirror of /tmp/trinity-repro/s3_fix_verified.sh, pinned in-repo so the
# shell-level mechanics the Python implementation relies on can be
# regressed with zero Python / Docker / network dependencies.
#
# Reproduces the parallel-history scenario from the git-improvements
# proposal: `main` and `trinity/demo/abc` both have an "init" commit with
# different SHAs and conflicting content, then `main` advances with a
# conflicting change. Runs the S3 shell equivalent (snapshot → reset →
# overlay → commit → force-with-lease push) and asserts the remote ends
# up with main's baseline plus the worker's workspace state.
#
# Usage: ./tests/git-sync/s3_reset_preserve.sh
# Expected last line: "S3 pure-git regression: OK"

set -euo pipefail

workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT
cd "$workdir"

# 1. Bare origin ----------------------------------------------------------

git init -q --bare -b main origin.git

# 2. Pristine template -> origin/main -------------------------------------

git clone -q origin.git pristine
cd pristine
echo "v1" > template.conf
mkdir workspace
echo "initial" > workspace/state
git -c user.email=t@e -c user.name=t add -A
git -c user.email=t@e -c user.name=t commit -q -m "init"
git push -q origin main
cd ..

# 3. Divergent worker on trinity/demo/abc ---------------------------------

git clone -q origin.git worker
cd worker
git checkout -q -b trinity/demo/abc
echo "v1-fork" > template.conf
mkdir -p workspace
echo "instance" > workspace/state
git -c user.email=t@e -c user.name=t add -A
git -c user.email=t@e -c user.name=t commit -q -m "init"
echo "more" >> workspace/state
git -c user.email=t@e -c user.name=t add -A
git -c user.email=t@e -c user.name=t commit -q -m "accumulated"
git push -q origin trinity/demo/abc

# 4. origin/main advances with a conflicting change -----------------------

cd ../pristine
echo "v2" > template.conf
git -c user.email=t@e -c user.name=t add -A
git -c user.email=t@e -c user.name=t commit -q -m "upstream-update"
git push -q origin main

# 5. Run the S3 routine in the worker clone -------------------------------

cd ../worker
git fetch -q origin main

ts="$(date -u +%Y-%m-%dT%H%M%SZ)"
mkdir -p ".trinity/backup/${ts}"
# Allowlist for this scenario: workspace/** only.
tar -cf ".trinity/backup/${ts}/snapshot.tar" workspace

git reset -q --hard origin/main
tar -xf ".trinity/backup/${ts}/snapshot.tar"
git -c user.email=t@e -c user.name=t add -A
git -c user.email=t@e -c user.name=t commit -q -m "Adopt main baseline, preserve state"
git push -q --force-with-lease origin HEAD:trinity/demo/abc

# 6. Assertions ------------------------------------------------------------

cd ..
git clone -q -b trinity/demo/abc origin.git verify
cd verify

[[ "$(cat template.conf)" == "v2" ]] || { echo "FAIL: template.conf != v2"; exit 1; }
echo "PASS template.conf=v2 adopted from main"

grep -q "^instance$" workspace/state || { echo "FAIL: workspace/state missing 'instance'"; exit 1; }
echo "PASS workspace/state preserved ('instance')"

grep -q "^more$" workspace/state || { echo "FAIL: workspace/state missing 'more'"; exit 1; }
echo "PASS workspace/state preserved ('more')"

subject="$(git log -1 --pretty=%s)"
[[ "$subject" == "Adopt main baseline, preserve state" ]] || {
    echo "FAIL: commit subject is '$subject', expected 'Adopt main baseline, preserve state'"
    exit 1
}
echo "PASS commit message matches spec"

echo "S3 pure-git regression: OK"
