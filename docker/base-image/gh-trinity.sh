#!/bin/sh
# gh wrapper (trinity-enterprise#1278), installed ahead of /usr/bin/gh on PATH.
#
# `gh` reads its token from GH_TOKEN and never asks git's credential helper.
# A broker-mode container has no baked token, so this asks the same helper git
# uses, once per `gh` call, and hands the answer to the real `gh`. The token
# is short-lived and scoped to this agent's own repo.
#
# Outside broker mode, or when a token is already set, it runs `gh` untouched.
set -u

if [ "${TRINITY_GIT_TOKEN_BROKER:-}" = "1" ] && [ -z "${GH_TOKEN:-}" ] && [ -z "${GITHUB_TOKEN:-}" ]; then
    _base="${TRINITY_GIT_BASE_URL:-https://github.com}"
    _base="${_base%/}"
    _host="${_base#*://}"
    _token=$(printf 'protocol=%s\nhost=%s\n\n' "${_base%%://*}" "${_host%%/*}" \
        | "${TRINITY_GIT_HELPER:-/usr/local/bin/git-credential-trinity}" get 2>/dev/null | sed -n 's/^password=//p')
    if [ -n "${_token}" ]; then
        GH_TOKEN="${_token}"
        export GH_TOKEN
    fi
fi

exec "${TRINITY_REAL_GH:-/usr/bin/gh}" "$@"
