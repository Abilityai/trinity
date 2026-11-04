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
        | curl -sS -K - -X "$method" -H "Content-Type: application/json" \
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
