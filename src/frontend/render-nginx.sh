#!/bin/sh
# Render the frontend nginx configuration, then run nginx (trinity-enterprise#873).
#
# nginx.conf is a template with exactly ONE substitution: ${TRINITY_PROXY_GATEWAY},
# the bridge gateway address the protected :8082 listener accepts a PROXY
# header's client address from. Nothing else is expanded — every nginx
# `$variable` in the file is left exactly as written — and the value must be a
# literal IPv4 address, so an environment value can never inject configuration.
# A bad value stops the container rather than starting nginx with a guess.
set -eu

TEMPLATE="${TRINITY_NGINX_TEMPLATE:-/etc/nginx/trinity/nginx.conf.template}"
OUTPUT="${TRINITY_NGINX_OUTPUT:-/etc/nginx/generated/default.conf}"
GATEWAY="${TRINITY_PROXY_GATEWAY:-}"

valid_ipv4() {
    case "$1" in
        *[!0-9.]*|.*|*.|*..*) return 1 ;;
    esac
    old_ifs="$IFS"; IFS=.
    # shellcheck disable=SC2086
    set -- $1
    IFS="$old_ifs"
    [ "$#" -eq 4 ] || return 1
    for octet in "$@"; do
        case "$octet" in
            0|[1-9]|[1-9][0-9]|1[0-9][0-9]|2[0-4][0-9]|25[0-5]) ;;
            *) return 1 ;;
        esac
    done
}

if ! valid_ipv4 "$GATEWAY"; then
    echo "render-nginx: TRINITY_PROXY_GATEWAY='${GATEWAY}' is not an IPv4 address; refusing to start" >&2
    exit 1
fi
if [ "$(grep -c '\${TRINITY_PROXY_GATEWAY}' "$TEMPLATE")" != "1" ]; then
    echo "render-nginx: ${TEMPLATE} must contain exactly one \${TRINITY_PROXY_GATEWAY}" >&2
    exit 1
fi

sed "s/\${TRINITY_PROXY_GATEWAY}/${GATEWAY}/" "$TEMPLATE" > "${OUTPUT}.tmp"
mv -f "${OUTPUT}.tmp" "$OUTPUT"

[ "$#" -gt 0 ] || set -- nginx -g "daemon off;"
exec "$@"
