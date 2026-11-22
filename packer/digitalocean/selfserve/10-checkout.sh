#!/bin/bash
# Build step 1 of the self-serve snapshot (trinity-enterprise#1347): check out
# ONE commit at /opt/trinity, with the enterprise module at that commit's pin.
#
# 01-provision.sh runs next with TRINITY_CHECKOUT_READY=1, so it uses this
# checkout instead of cloning a release tag.
#
# The enterprise repository is private, so fetching it takes a token. The token
# arrives in the environment and reaches git through GIT_CONFIG_* variables,
# never argv and never a config file. Before this step exits it searches both
# repositories for the token and fails the build if it finds it, because every
# instance created from the snapshot would carry a copy.
#
# Inputs (environment):
#   TRINITY_COMMIT            full 40-character SHA to bake (required)
#   TRINITY_ENTERPRISE_TOKEN  read-only token for the enterprise repository (required)
#   TRINITY_CHECKOUT_DIR      default /opt/trinity
#   TRINITY_REPO_URL          default https://github.com/abilityai/trinity.git
#   TRINITY_ENTERPRISE_URL    default: the submodule URL in .gitmodules, as https
set -euo pipefail

# git@github.com:Owner/repo(.git) or ssh://git@github.com/... → https://github.com/Owner/repo.git.
# The build droplet has no SSH key for the private repository, only the token.
https_url() {
    local u="$1"
    case "$u" in
        git@*:*)       u="${u#git@}"; u="https://${u/://}" ;;
        ssh://git@*)   u="https://${u#ssh://git@}" ;;
    esac
    case "$u" in
        https://*) [[ "$u" == *.git ]] || u="${u}.git" ;;
    esac
    printf '%s\n' "$u"
}

die() { echo "FATAL: $*" >&2; exit 1; }

# Fetch exactly one commit into a fresh repository. With a token, the header
# travels in this process's environment only.
fetch_commit() {
    local dir="$1" url="$2" sha="$3" token="${4:-}"
    git init -q "$dir"
    git -C "$dir" remote add origin "$url"
    if [ -n "$token" ] && [[ "$url" == https://* ]]; then
        GIT_CONFIG_COUNT=1 \
        GIT_CONFIG_KEY_0=http.extraHeader \
        GIT_CONFIG_VALUE_0="Authorization: Basic $(printf 'x-access-token:%s' "$token" | base64 -w0)" \
            git -C "$dir" fetch -q --depth 1 origin "$sha" \
            || die "could not fetch ${sha} from ${url}"
    else
        git -C "$dir" fetch -q --depth 1 origin "$sha" || die "could not fetch ${sha} from ${url}"
    fi
    git -C "$dir" checkout -q --detach FETCH_HEAD
    [ "$(git -C "$dir" rev-parse HEAD)" = "$sha" ] || die "${dir} is not at ${sha}"
}

main() {
    local commit="${TRINITY_COMMIT:-}" token="${TRINITY_ENTERPRISE_TOKEN:-}"
    local dest="${TRINITY_CHECKOUT_DIR:-/opt/trinity}"
    local repo="${TRINITY_REPO_URL:-https://github.com/abilityai/trinity.git}"
    local ent_rel="src/backend/enterprise" pin ent_url ent leaks

    [[ "$commit" =~ ^[0-9a-f]{40}$ ]] || die "TRINITY_COMMIT must be a full 40-character commit SHA (got '${commit}')."
    [ -n "$token" ] || die "TRINITY_ENTERPRISE_TOKEN is empty: the enterprise module cannot be fetched."
    [ ! -e "$dest" ] || die "${dest} already exists; the checkout must start from nothing."

    if ! command -v git >/dev/null 2>&1; then
        apt-get update -q
        apt-get install -y -q git ca-certificates
    fi

    echo "=== self-serve checkout: ${commit} ==="
    fetch_commit "$dest" "$repo" "$commit"

    pin="$(git -C "$dest" ls-tree HEAD "$ent_rel" | awk '$2 == "commit" { print $3 }')"
    [[ "$pin" =~ ^[0-9a-f]{40}$ ]] || die "${commit} pins no enterprise module at ${ent_rel}."
    ent_url="${TRINITY_ENTERPRISE_URL:-$(git -C "$dest" config -f .gitmodules --get "submodule.${ent_rel}.url" || true)}"
    [ -n "$ent_url" ] || die ".gitmodules has no URL for ${ent_rel}."
    ent_url="$(https_url "$ent_url")"

    ent="${dest}/${ent_rel}"
    rm -rf "$ent"   # the gitlink checks out as an empty directory
    echo "=== enterprise module at pin ${pin} ==="
    fetch_commit "$ent" "$ent_url" "$pin" "$token"
    [ -f "${ent}/backend/__init__.py" ] || die "the enterprise module at ${pin} has no backend/__init__.py."

    # The token, or the header built from it, must not be anywhere in either tree.
    leaks="$(grep -rlF -e "$token" -e "$(printf 'x-access-token:%s' "$token" | base64 -w0)" "$dest" 2>/dev/null || true)"
    [ -z "$leaks" ] || die "the enterprise token was written to: ${leaks}"

    echo "=== checkout complete ==="
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
