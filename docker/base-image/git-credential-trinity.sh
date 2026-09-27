#!/bin/sh
# git-credential-trinity — Trinity's git credential helper (ent#615).
#
# WHY THIS EXISTS
# ---------------
# Trinity used to persist every agent's git remote as
#   <scheme>://oauth2:<PAT>@<host>/<org>/<repo>.git
# which put the platform's GitHub token in two places that cross the container
# boundary: `.git/config` on the workspace volume (at rest, for the life of the
# container) and the argv of `git-remote-https`, which git expands the stored
# URL into on EVERY fetch/push. From argv it reached `ps`, the orphan sweeper's
# reaped-cmdline logging, Vector, the host log files and the logs API — i.e.
# another agent's LLM context.
#
# Git speaks the credential protocol to a helper over stdin/stdout, which never
# reaches argv and is never persisted. Remote URLs become credential-less; the
# credential is resolved here, per operation.
#
# WHAT THIS DOES NOT CLOSE
# ------------------------
# A prompt-injected agent reading its own credential. `GITHUB_PAT` stays in the
# container env and in `/home/developer/.env`; `printenv`, `cat` and invoking
# this script directly all return it. Root ownership of this file is not
# confidentiality, and it is not a boundary either: `developer` holds
# `NOPASSWD:ALL` (Dockerfile, `usermod -aG sudo developer`), so an agent that
# wants to rewrite this script or `/etc/gitconfig` can `sudo` and do it. What
# root ownership buys is that nothing rewrites them BY ACCIDENT — an errant
# `pip install`, a template's post-create hook, a skill writing to `$HOME`.
# Treating it as a boundary would be the mistake; the structural fix — a
# credential broker outside the container — is trinity-enterprise#558 and is
# deliberately not absorbed here.
#
# RESOLUTION ORDER — `.env` FIRST, baked env second
# -------------------------------------------------
# This is the INVERSE of `startup.sh`, and deliberately so. `startup.sh` runs at
# BOOT, where the baked `Config.Env` is the freshly-recreated truth. This helper
# runs in STEADY STATE, where a rotation (#1967) or a per-agent PAT set after
# creation (#1264) has live-injected the new token into `/home/developer/.env`
# while `Config.Env` is immutable without a container recreate and therefore
# stale. A baked-env-first ladder would authenticate with the old token forever
# and only surface once the old token is revoked.
#
# The third rung is the ent#615 harvest file: the credential rescued from a
# legacy token-bearing remote URL for an agent that has no other source (the
# `POST /{agent}/git/initialize` orphan class). It is LAST so it can only ever
# serve an agent that would otherwise have nothing.
#
# `/home/developer/.env` is agent-writable, so it is PARSED, never sourced or
# eval'd — sourcing it would be arbitrary code execution on every git operation.
# The parse is the same shape `startup.sh` uses, pinned by a test.

set -u

# The agent home. Overridable ONLY so this script and the ent#615 sweep can be
# exercised outside a container by the test suite — it redirects reads to files
# the invoking user already owns, so it grants nothing: an agent that set it
# could only point this helper at its own files, which it can already `cat`.
AGENT_HOME="${TRINITY_AGENT_HOME:-/home/developer}"
ENV_FILE="${AGENT_HOME}/.env"
HARVEST_FILE="${AGENT_HOME}/.trinity/git-credential"

# Git appends the operation as the last argument. Answer `get` only: Trinity
# never wants git to persist a credential, so `store`/`erase` are no-ops and
# nothing ever writes ~/.git-credentials.
op=""
for arg in "$@"; do
    op="${arg}"
done
[ "${op}" = "get" ] || exit 0

# The request arrives on stdin as key=value lines terminated by a blank line.
# Never on argv.
req_protocol=""
req_host=""
while IFS= read -r line; do
    [ -n "${line}" ] || break
    case "${line}" in
        protocol=*) req_protocol="${line#protocol=}" ;;
        host=*) req_host="${line#host=}" ;;
        *) ;;
    esac
done

# Exact protocol + host equality, PORT-INCLUSIVE. Git feeds
# `host=trinity-gitea-dev:3000` for a self-hosted base and `host=github.com`
# for the default, so a bare-hostname comparison silently refuses the
# self-hosted harness. A suffix/substring comparison would be worse — an
# exfiltration primitive (`github.com.evil.tld`).
#
# Git already refuses to route a lookalike host to a host-scoped helper, so
# this check is not what protects a git-mediated request. It matters for the
# case git does NOT mediate: a foreign remote an agent adds by hand, and the
# agent invoking this script directly with hand-written stdin.
base="${TRINITY_GIT_BASE_URL:-https://github.com}"
base="${base%/}"
want_protocol="${base%%://*}"
want_host="${base#*://}"
want_host="${want_host%%/*}"

if [ "${req_protocol}" != "${want_protocol}" ] || [ "${req_host}" != "${want_host}" ]; then
    exit 0
fi

# Parse (never source) a KEY=VALUE line out of a dotenv file.
_read_env_value() {
    _file="$1"
    _key="$2"
    [ -f "${_file}" ] || return 1
    _value=$(grep -m1 "^${_key}=" "${_file}" 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '\r')
    [ -n "${_value}" ] || return 1
    printf '%s' "${_value}"
}

pat=$(_read_env_value "${ENV_FILE}" GITHUB_PAT) || pat=""

if [ -z "${pat}" ]; then
    pat="${GITHUB_PAT:-}"
fi

if [ -z "${pat}" ] && [ -f "${HARVEST_FILE}" ]; then
    pat=$(tr -d '\r\n' < "${HARVEST_FILE}" 2>/dev/null) || pat=""
fi

if [ -z "${pat}" ]; then
    # Emit NOTHING on stdout: git then falls through to the prompt, and with
    # GIT_TERMINAL_PROMPT=0 that is a deterministic
    # "could not read Username for '<url>': terminal prompts disabled" — a
    # shape `git_service._AUTH_PATTERNS` already classifies as AUTH_FAILURE.
    # The stderr marker is the discriminator between "no credential" and
    # "credential rejected". It carries a host, never a value, and no
    # agent-identifying text.
    echo "TRINITY_GIT_NO_CREDENTIAL host=${req_host}" >&2
    exit 0
fi

printf 'username=oauth2\n'
printf 'password=%s\n' "${pat}"
