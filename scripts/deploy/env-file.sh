#!/bin/bash
# Shared `.env` writer. Sourced, never executed.
#
# Extracted so `start.sh --provision` and `set-domain.sh` write `.env` the same
# way. It is small enough to have been copied, which is exactly how the two
# provisioning implementations this repo just collapsed came to disagree.

# Write (or rewrite) one key in `.env`, relative to the caller's working
# directory (the repo root in both callers).
#
# Rewrites the LINE rather than substituting into it: `sed "s|^k=.*|k=$value|"`
# is wrong here because the replacement half is user-controlled — `&` expands to
# the whole match, `\` escapes, and the delimiter ends the expression. The
# failure is the silent kind (a mangled password in `.env` while the summary says
# the operator's own is in effect). Rewriting needs no escaping of the value at
# all. `cat >` rather than `mv` so `.env` keeps its own inode and mode.
set_env_key() {
    local key="$1" value="$2" tmp
    tmp="$(mktemp)"
    grep -vE "^${key}=" .env > "$tmp" || true
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
    cat "$tmp" > .env
    rm -f "$tmp"
}
