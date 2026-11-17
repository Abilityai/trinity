#!/bin/bash
# Shared by the marketplace scripts: one Vendor Portal call, and droplet cleanup.
MP_API="https://api.digitalocean.com/api/v1/vendor-portal/apps"

# usage: mp_api METHOD URL OUT_FILE [BODY_FILE] — prints the HTTP status.
# The token reaches curl on stdin, because argv is readable in `ps`.
mp_api() {
    local method="$1" url="$2" out="$3" body="${4:-}"
    local data=()
    [ -z "$body" ] || data=(--data-binary "@${body}")
    printf 'header = "Authorization: Bearer %s"\n' "$DIGITALOCEAN_TOKEN" \
        | curl -sS -K - --max-time 60 -X "$method" -H "Content-Type: application/json" \
            ${data[@]+"${data[@]}"} -o "$out" -w '%{http_code}' "$url"
}

# usage: reap_tag TAG — deletes the droplets on a tag and confirms they are gone.
# A failed list counts as not gone: an empty answer from a dead network is no proof.
reap_tag() {
    local tag="$1" left="" _
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
        if left="$(doctl compute droplet list --tag-name "$tag" --format ID --no-header 2>/dev/null)"; then
            if [ -z "$left" ]; then
                doctl compute tag delete "$tag" --force >/dev/null 2>&1
                return 0
            fi
            doctl compute droplet delete --tag-name "$tag" --force >/dev/null 2>&1
        fi
        sleep "${TRINITY_BOOT_CHECK_POLL:-5}"
    done
    echo "[marketplace] droplet ${left:-unknown} on tag ${tag} may still exist. Check: doctl compute droplet list --tag-name ${tag}" >&2
    return 1
}

# usage: use_ssh_key — points TRINITY_SSH_KEY_FILE at the private key for this run.
# TRINITY_SSH_KEY_OP is a 1Password secret reference (op://vault/item/field): the key
# is read into a temp file that drop_ssh_key deletes, so call that from the exit trap.
# Without it, TRINITY_SSH_KEY_FILE is used as given and left alone.
# `op run` resolves every op:// value in the environment, so run these scripts outside it.
KEY_TMP=""
use_ssh_key() {
    if [ -n "${TRINITY_SSH_KEY_OP:-}" ]; then
        KEY_TMP="$(mktemp "${TMPDIR:-/tmp}/trinity-key.XXXXXX")" || return 1
        if ! op read "$TRINITY_SSH_KEY_OP" > "$KEY_TMP"; then
            echo "[marketplace] could not read the SSH key from 1Password" >&2
            return 1
        fi
        TRINITY_SSH_KEY_FILE="$KEY_TMP"
    fi
    [ -r "${TRINITY_SSH_KEY_FILE:-}" ] && return 0
    echo "[marketplace] set TRINITY_SSH_KEY_OP (a 1Password reference) or TRINITY_SSH_KEY_FILE (a readable private key)" >&2
    return 1
}
drop_ssh_key() { [ -z "$KEY_TMP" ] || rm -f "$KEY_TMP"; }
