#!/usr/bin/env bash
# Classify WHY a `git submodule update` failed, so the printed remedy matches the
# actual cause (#2246).
#
# The dev deploy has been shipping OSS-only since at least 2026-07-29 — every run
# in the retained history carries `Enterprise submodule init failed`. The clone was
# dying at SSH host-key verification, one layer BEFORE authentication, while the
# workflow's diagnostic said:
#
#     ENTERPRISE: confirm the dev VM has read access to Abilityai/trinity-enterprise
#     ENTERPRISE: (deploy key, PAT in a credential helper, or org membership all work)
#
# Every one of those remedies is an authorization fix. Anyone who followed the hint
# would install a deploy key, redeploy, and get the identical warning — which is a
# plausible reason this sat unfixed for weeks. A diagnostic that names the wrong
# layer is worse than none: it spends someone's afternoon proving the hint wrong.
#
# Usage:
#     bash scripts/ci/classify-submodule-failure.sh <file-with-captured-output>
#     … | bash scripts/ci/classify-submodule-failure.sh -
#
# Prints a `CLASS: <name>` line followed by remedy lines, and exits 0 always — a
# classifier must never be the thing that fails a deploy. The caller decides.
#
# Classes are ordered most-specific-first and the FIRST match wins, because a
# failing clone can emit several of these strings at once: a host-key rejection
# also prints "Could not read from remote repository", and git's own
# "Failed to clone … a second time, aborting" wraps whatever the real cause was.
set -uo pipefail

SRC="${1:--}"
if [ "$SRC" = "-" ]; then
    OUTPUT="$(cat)"
else
    OUTPUT="$(cat "$SRC" 2>/dev/null || true)"
fi

emit() { printf '%s\n' "$@"; }

# ---------------------------------------------------------------------------
# 1. Host key — the layer this issue was actually about.
# ---------------------------------------------------------------------------
if printf '%s' "$OUTPUT" | grep -qE 'Host key verification failed|REMOTE HOST IDENTIFICATION HAS CHANGED|No (ED25519|RSA|ECDSA) host key is known'; then
    emit "CLASS: host-key" \
         "CAUSE: SSH refused the server's identity before any credential was offered." \
         "       The deploying user has no github.com entry in ~/.ssh/known_hosts (or a stale one)." \
         "NOTE:  This is NOT an access problem — authentication was never attempted, so this" \
         "       failure tells you nothing yet about whether the VM's credentials would work." \
         "FIX A: ssh-keyscan github.com >> ~/.ssh/known_hosts   (clears this layer only)" \
         "FIX B (preferred): switch this submodule to HTTPS so no known_hosts entry is needed —" \
         "       git config submodule.src/backend/enterprise.url \\" \
         "         https://github.com/Abilityai/trinity-enterprise.git" \
         "       plus a credential helper or the ENT_SUBMODULE_PAT secret; see docs/ENTERPRISE.md" \
         "       (\"Dev VM / CI hosts\"). Resolves host-key AND authorization in one step." \
         "WHY B: .gitmodules uses SSH for src/backend/enterprise and HTTPS for .claude, so only" \
         "       this submodule can fail this way. HTTPS removes the asymmetry."
    exit 0
fi

# ---------------------------------------------------------------------------
# 2. Authorization — what the old diagnostic assumed unconditionally.
# ---------------------------------------------------------------------------
if printf '%s' "$OUTPUT" | grep -qE 'Permission denied \(publickey|Repository not found|could not read Username|Authentication failed|remote: (Invalid username or password|Write access|Support for password authentication)|HTTP Basic: Access denied|fatal: unable to access .*: The requested URL returned error: (401|403)'; then
    emit "CLASS: authorization" \
         "CAUSE: The transport connected and the credential was rejected (or absent)." \
         "FIX:   Give the deploying user read access to Abilityai/trinity-enterprise —" \
         "       a deploy key, a PAT in a credential helper, the ENT_SUBMODULE_PAT secret," \
         "       or org membership all work. See docs/ENTERPRISE.md."
    exit 0
fi

# ---------------------------------------------------------------------------
# 3. Network / DNS — nothing to fix on the repo side.
# ---------------------------------------------------------------------------
if printf '%s' "$OUTPUT" | grep -qE 'Could not resolve host|Connection timed out|Connection refused|Network is unreachable|Operation timed out|Failed to connect to github.com'; then
    emit "CLASS: network" \
         "CAUSE: github.com was unreachable from the deploying host." \
         "FIX:   Transient or egress-blocked. Re-run; if it persists, check the VM's" \
         "       outbound 443/22 and DNS rather than any repo credential."
    exit 0
fi

# ---------------------------------------------------------------------------
# 4. Nothing matched. Say so plainly and hand back the evidence — a wrong guess
#    is what this script exists to stop, so it does not guess here.
# ---------------------------------------------------------------------------
if [ -z "${OUTPUT//[[:space:]]/}" ]; then
    emit "CLASS: no-output" \
         "CAUSE: The submodule update produced no output, yet the tree is unpopulated." \
         "FIX:   Check that .gitmodules still lists src/backend/enterprise, and that" \
         "       'git config submodule.src/backend/enterprise.update' is 'checkout' —" \
         "       an 'update = none' submodule (#1443) is SKIPPED and exits 0 silently."
    exit 0
fi

emit "CLASS: unknown" \
     "CAUSE: Unrecognised failure — classifying it as access would be a guess." \
     "FIX:   Read the captured output below and, if it is a recurring shape," \
     "       add it to scripts/ci/classify-submodule-failure.sh (#2246)." \
     "--- captured output (last 20 lines) ---"
printf '%s\n' "$OUTPUT" | tail -20
exit 0
