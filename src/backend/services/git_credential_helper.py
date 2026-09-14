"""Trinity's git credential helper — the string builders (ent#615).

Trinity used to persist every agent's git remote as
``<scheme>://oauth2:<PAT>@<host>/<org>/<repo>.git``. That put the platform's
GitHub token in ``.git/config`` on the workspace volume (at rest, readable by
the agent's own ``Bash`` tool for the life of the container) and — because git
expands the stored URL into ``git-remote-https``'s argv on EVERY fetch and push
— in the container's process table twice a minute, whether or not the agent ran
any git itself.

The argv half is the one with a platform-side sink: ``ps`` → the agent server's
orphan-sweep reaped-cmdline logging → Vector → the host log files → the logs
API → **another agent's LLM context**. trinity-enterprise#292 closed the last
hop of that chain (it sanitized the log line) and was rated P0; this closes the
cause.

A git **credential helper** is the mechanism because git speaks the credential
protocol to it over stdin/stdout, which never reaches argv and is never
persisted. Remote URLs become credential-less and every producer stops building
a token-bearing one.

**One source, one mirror, one parity test** (Invariant #5). The script itself is
``docker/base-image/git-credential-trinity.sh`` — the agent image ships as its
own image and structurally cannot import from ``src/backend``, so the backend
carries a byte-identical copy here for pushing into containers running an older
base image. ``tests/unit/test_ent615_credential_helper_parity.py`` asserts byte
equality and, per the ent#314 lesson, walks BOTH trees.

**Registered as ``trinity``, never as the filename.** Git prepends
``git-credential-`` to any helper value that is not an absolute path and does
not start with ``!``, so registering ``git-credential-trinity`` resolves to
``git-credential-git-credential-trinity`` — a command that does not exist. The
helper would never run, and combined with token-free URLs that is a silent
fleet-wide fetch/push outage no source-only CI can see. Proven by execution
(git 2.50.1): ``helper = trinity`` runs; ``helper = git-credential-trinity``
yields ``git: 'credential-git-credential-trinity' is not a git command``.

**Registered unscoped, host-checked inside the helper.** ``TRINITY_GIT_BASE_URL``
is a *runtime* value (``startup.sh`` derives the base at container start; the
backend writes it into the agent's env), so a ``credential.<base>.helper``
baked at image-build time could only ever name ``https://github.com`` and a
self-hosted install would get no helper at all. Registering
``credential.helper`` unscoped and resolving the allowed origin *inside* the
helper at request time covers both, and — unlike a runtime ``git config
--global`` registration — writes nothing to ``~/.gitconfig``, which sits in the
agent's repo root (``HOME`` is the repo root) and is not in
``_GITIGNORE_PATTERNS``.

**Installed root-owned, not ``--global``.** ``/usr/local/bin`` +
``/etc/gitconfig`` are the same places the Dockerfile puts them, so old-image
containers and new ones have one story. A ``--global`` copy would live under
``/home/developer``, which is agent-writable — and ``credential_paths.py``
deny-lists ``.gitconfig``/``.git/**`` precisely because a git-executed command
reference there is an execution vector.

**What this does not close:** a prompt-injected agent reading its own
credential. ``GITHUB_PAT`` stays in the container env and ``.env``. Root
ownership of the script is not confidentiality, and — because ``developer``
holds ``NOPASSWD:ALL`` — it is not a boundary either; it buys only that nothing
rewrites the script or ``/etc/gitconfig`` BY ACCIDENT. The structural answer is
a credential broker outside the container — trinity-enterprise#558 (AAuth),
connected and deliberately not absorbed.
"""

import base64
import re
from typing import Dict

# The value registered in git config. NOT the filename — see the module
# docstring (CRIT-1).
HELPER_NAME = "trinity"

# Root-owned, mode 0755. Same path the Dockerfile installs to.
HELPER_PATH = "/usr/local/bin/git-credential-trinity"

# The last rung of the helper's ladder: a credential rescued from a legacy
# token-bearing remote URL for an agent that has no other source. Deliberately
# NOT `.env` — `startup.sh` exports `.env`'s `GITHUB_PAT` as `GH_TOKEN` /
# `GITHUB_TOKEN` (authenticating the whole `gh` CLI and REST API) and
# `configure_push_remote` gates the ent#123 push blackhole on the same name, so
# writing that name would be a privilege GRANT, not a relocation (the ent#162
# class). `.trinity/*` is already ignored contents-only (#2070), so this file is
# never committed.
HARVEST_PATH = "/home/developer/.trinity/git-credential"

# Emitted on stderr by the helper when it resolves nothing. The discriminator
# between "no credential" and "credential rejected"; carries a host, never a
# value.
NO_CREDENTIAL_MARKER = "TRINITY_GIT_NO_CREDENTIAL"

# First token of the remediation script's one-line report.
SCRUB_REPORT_PREFIX = "TRINITY_SCRUB_REPORT"


HELPER_SCRIPT = r"""#!/bin/sh
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
"""


# The remediation sweep. Backend-only (never shipped in the image), so unlike
# HELPER_SCRIPT it has no mirror and no parity test.
#
# Ordering is the whole design: **no path may strip a credential it has not
# already replaced.** The orphan class this exists for —
# `POST /{agent}/git/initialize` writes a git config row and pushes with the
# resolved platform PAT but bakes no git env, persists no per-agent row and
# writes no `.env`, so "its only credential lives in the container's
# `.git/config` origin URL" — is stranded permanently by a sweep that strips
# first and asks later.
#
# The helper-runs probe is by EXIT CODE only. `git credential fill` prints the
# credential on stdout, and this script's output crosses the `docker exec`
# boundary into `logger.warning(result["output"][:200])` and into
# `GitInitResult.error` — so a probe whose stdout escaped would put the token in
# the platform log and possibly an HTTP error body.
SCRUB_SCRIPT = r"""
set -u

# Same override, same reason, as `git-credential-trinity.sh`: a test seam that
# grants nothing. The two MUST agree — the sweep writes what the helper reads.
AGENT_HOME="${TRINITY_AGENT_HOME:-/home/developer}"
ROOT="${1:-${AGENT_HOME}}"
HARVEST_DIR="${AGENT_HOME}/.trinity"
HARVEST="${HARVEST_DIR}/git-credential"

# The one origin this platform serves. Everything below is scoped to it, so a
# foreign remote's credential is neither promoted into the GitHub slot nor
# stripped from a remote the helper cannot serve.
base="${TRINITY_GIT_BASE_URL:-https://github.com}"
base="${base%/}"
want_scheme="${base%%://*}"
want_host="${base#*://}"
want_host="${want_host%%/*}"

scrubbed=0
harvested=0
seeded=0
refused=0
gitmodules_hits=0
helper_ok=0
competing=0
root_readable=0

# `safe.directory=*`: this runs as root against a repo owned by `developer`.
# Non-secret, and `-c` is protected configuration, so git honours it.
g() { git -c "safe.directory=*" "$@"; }

# EXIT CODE ONLY. `git credential fill` PRINTS the credential on stdout, and
# this script's output crosses the `docker exec` boundary into the platform log
# and into `GitInitResult.error` — so stdout is consumed and discarded here.
probe() {
    printf 'protocol=%s\nhost=%s\n\n' "${want_scheme}" "${want_host}" \
        | g -C "${ROOT}" credential fill 2>/dev/null \
        | grep -q '^password=.'
}

U_SCHEME=""; U_USERINFO=""; U_HOSTPORT=""; U_PATH=""
parse_url() {
    U_SCHEME=""; U_USERINFO=""; U_HOSTPORT=""; U_PATH=""
    case "$1" in *://*) ;; *) return 1 ;; esac
    U_SCHEME="${1%%://*}"
    _rest="${1#*://}"
    case "${_rest}" in
        */*) _auth="${_rest%%/*}"; U_PATH="/${_rest#*/}" ;;
        *)   _auth="${_rest}";     U_PATH="" ;;
    esac
    case "${_auth}" in
        *@*) U_USERINFO="${_auth%@*}"; U_HOSTPORT="${_auth##*@}" ;;
        *)   U_USERINFO="";            U_HOSTPORT="${_auth}" ;;
    esac
    return 0
}

in_scope() {
    [ "${U_SCHEME}" = "${want_scheme}" ] && [ "${U_HOSTPORT}" = "${want_host}" ]
}

secret_of_userinfo() {
    case "$1" in
        *:*) printf '%s' "${1#*:}" ;;
        *)   printf '%s' "$1" ;;
    esac
}

TAB=$(printf '\t')
WORK=$(mktemp -d) || exit 90
trap 'rm -rf "${WORK}"' EXIT
CFGS="${WORK}/cfgs"
KEYS="${WORK}/keys"
VALS="${WORK}/vals"
NEWV="${WORK}/newv"
LEFTV="${WORK}/leftv"

GIT_DIR_ABS=$(g -C "${ROOT}" rev-parse --absolute-git-dir 2>/dev/null) || GIT_DIR_ABS="${ROOT}/.git"
[ -d "${GIT_DIR_ABS}" ] || GIT_DIR_ABS="${ROOT}/.git"

# Every config a remote can live in: the worktree's own, plus submodules at
# `.git/modules/<a>/config` AND NESTED submodules at
# `.git/modules/<a>/modules/<b>/config` — which a `.git/modules/*/config` glob
# misses.
# Can this exec SEE the tree at all? An all-zero report is otherwise
# indistinguishable between "already clean" and "could not even look" — and
# the second is the production geometry of `agent_full_capabilities=false`:
# RESTRICTED_CAPABILITIES omits DAC_OVERRIDE, `/home/developer` is 0700
# `developer`-owned, so root here cannot traverse it, `find` enumerates
# nothing and `probe` fails. Without this flag that reports exit 0 and all
# zeros, i.e. success, on exactly the hardened installs that will act on it.
# `test -r/-x` goes through access(2), which honours the missing capability,
# so this is the real answer and not an assumption.
if [ -d "${GIT_DIR_ABS}" ] && [ -r "${GIT_DIR_ABS}" ] && [ -x "${GIT_DIR_ABS}" ]; then
    root_readable=1
fi

find "${GIT_DIR_ABS}" -maxdepth 6 -name config -type f > "${CFGS}" 2>/dev/null || true

: > "${KEYS}"
while IFS= read -r cfg; do
    [ -n "${cfg}" ] || continue
    g config --file "${cfg}" --list --name-only 2>/dev/null | sort -u | while IFS= read -r key; do
        case "${key}" in
            remote.*.url|remote.*.pushurl|url.*.insteadof) ;;
            *) continue ;;
        esac
        printf '%s%s%s\n' "${cfg}" "${TAB}" "${key}" >> "${KEYS}"
    done
done < "${CFGS}"

# Does anything already resolve? Checked BEFORE any strip, never after.
if probe; then helper_ok=1; fi

# The helper's LAST rung. Deliberately not `.env`: `startup.sh` exports `.env`'s
# GITHUB_PAT as GH_TOKEN/GITHUB_TOKEN and `configure_push_remote` gates the
# ent#123 push blackhole on the same name, so writing that name would be a
# privilege GRANT, not a relocation (the ent#162 class). `.trinity/*` is already
# ignored contents-only (#2070), so this file is never committed. Owned by the
# agent because the helper runs as whatever user git runs as.
write_harvest() {
    ( umask 077; mkdir -p "${HARVEST_DIR}"; printf '%s' "$1" > "${HARVEST}" ) 2>/dev/null || return 1
    chmod 600 "${HARVEST}" 2>/dev/null || true
    chown developer:developer "${HARVEST_DIR}" "${HARVEST}" 2>/dev/null || true
    return 0
}

# Seed — a credential the CALLER already holds, handed over in the exec
# ENVIRONMENT (never argv, never base64-on-argv, which would relocate the leak
# rather than remove it). This is what makes `POST /{agent}/git/initialize`
# stop creating orphans: it has the PAT in hand and, before ent#615, persisted
# it nowhere.
if [ "${helper_ok}" -eq 0 ] && [ -n "${TRINITY_SEED_PAT:-}" ]; then
    if write_harvest "${TRINITY_SEED_PAT}"; then
        if probe; then helper_ok=1; seeded=1; fi
    fi
fi

# Harvest — only for an agent that has no other source. This is the orphan
# class `POST /{agent}/git/initialize` created before that seed existed: a git
# config row, a push with the resolved platform PAT, no baked git env, no
# per-agent row, no `.env` — "its only credential lives in the container's
# `.git/config` origin URL".
if [ "${helper_ok}" -eq 0 ]; then
    secret=""
    while IFS="${TAB}" read -r cfg key; do
        [ -n "${key}" ] || continue
        g config --file "${cfg}" --get-all "${key}" 2>/dev/null > "${VALS}" || continue
        while IFS= read -r value; do
            parse_url "${value}" || continue
            in_scope || continue
            [ -n "${U_USERINFO}" ] || continue
            s=$(secret_of_userinfo "${U_USERINFO}")
            [ -n "${s}" ] || continue
            secret="${s}"
            break
        done < "${VALS}"
        [ -z "${secret}" ] || break
    done < "${KEYS}"
    if [ -n "${secret}" ] && write_harvest "${secret}"; then
        if probe; then helper_ok=1; harvested=1; fi
    fi
fi

# Strip — never before the replacement is proven. When it is not proven, count
# what was deliberately left alone and say so; the caller raises an
# operator-queue entry rather than stranding the agent.
while IFS="${TAB}" read -r cfg key; do
    [ -n "${key}" ] || continue
    g config --file "${cfg}" --get-all "${key}" 2>/dev/null > "${VALS}" || continue
    changed=0
    : > "${NEWV}"
    while IFS= read -r value; do
        if parse_url "${value}" && in_scope && [ -n "${U_USERINFO}" ]; then
            changed=$((changed + 1))
            printf '%s://%s%s\n' "${U_SCHEME}" "${U_HOSTPORT}" "${U_PATH}" >> "${NEWV}"
        else
            printf '%s\n' "${value}" >> "${NEWV}"
        fi
    done < "${VALS}"
    [ "${changed}" -gt 0 ] || continue
    if [ "${helper_ok}" -eq 0 ]; then
        refused=$((refused + changed))
        continue
    fi
    g config --file "${cfg}" --unset-all "${key}" 2>/dev/null || true
    while IFS= read -r newvalue; do
        g config --file "${cfg}" --add "${key}" "${newvalue}" 2>/dev/null || true
    done < "${NEWV}"
    # Both writes above are `|| true`, so a config this exec can READ but not
    # WRITE keeps its token and the strip is a no-op. That is the production
    # geometry of `agent_full_capabilities=false`: RESTRICTED_CAPABILITIES omits
    # DAC_OVERRIDE and FOWNER, so root inside the container is subject to
    # ordinary permission checks against `developer`-owned files. Counting
    # `changed` unconditionally would report a scrub that did not happen — and a
    # false `remotes_scrubbed` is strictly worse than a refusal, because the
    # operator acts on it and stops looking. Re-read and count only what is
    # really gone; whatever survived is a refusal, which already alarms.
    left=0
    g config --file "${cfg}" --get-all "${key}" 2>/dev/null > "${LEFTV}" || true
    while IFS= read -r value; do
        if parse_url "${value}" && in_scope && [ -n "${U_USERINFO}" ]; then
            left=$((left + 1))
        fi
    done < "${LEFTV}"
    [ "${left}" -le "${changed}" ] || left="${changed}"
    scrubbed=$((scrubbed + changed - left))
    refused=$((refused + left))
done < "${KEYS}"

# A token also lives in the SUBSECTION of `url.<base>.insteadOf` — the shape
# this product's own deploy path uses. Renaming the section is the only removal.
if [ "${helper_ok}" -eq 1 ]; then
    : > "${WORK}/renamed"
    while IFS="${TAB}" read -r cfg key; do
        case "${key}" in url.*.insteadof) ;; *) continue ;; esac
        sub="${key#url.}"
        sub="${sub%.insteadof}"
        parse_url "${sub}" || continue
        in_scope || continue
        [ -n "${U_USERINFO}" ] || continue
        newsub="${U_SCHEME}://${U_HOSTPORT}${U_PATH}"
        g config --file "${cfg}" --rename-section "url.${sub}" "url.${newsub}" 2>/dev/null \
            || g config --file "${cfg}" --remove-section "url.${sub}" 2>/dev/null \
            || true
        echo x >> "${WORK}/renamed"
    done < "${KEYS}"
    renamed=$(grep -c . < "${WORK}/renamed" 2>/dev/null || true)
    [ -n "${renamed}" ] || renamed=0
    scrubbed=$((scrubbed + renamed))
fi

# A token in the TRACKED `.gitmodules` is already committed and pushed. This
# sweep cannot fix that — it can only report it, which turns "rotate after
# adoption" from advice into a requirement.
if [ -f "${ROOT}/.gitmodules" ]; then
    gitmodules_hits=$(grep -c -E 'url[[:space:]]*=[[:space:]]*[A-Za-z][A-Za-z0-9+.-]*://[^/[:space:]]*@' "${ROOT}/.gitmodules" 2>/dev/null || true)
    [ -n "${gitmodules_hits}" ] || gitmodules_hits=0
fi

# Helper lists COMPOSE: a competing helper answering first masks everything
# behind it, including a probe that then reports a credential this helper never
# produced.
competing=$(g -C "${ROOT}" config --get-all credential.helper 2>/dev/null | grep -v '^trinity$' | grep -c . || true)
[ -n "${competing}" ] || competing=0

printf 'TRINITY_SCRUB_REPORT remotes_scrubbed=%s harvested=%s seeded=%s refused=%s gitmodules_hits=%s helper_ok=%s competing_helpers=%s root_readable=%s\n' \
    "${scrubbed}" "${harvested}" "${seeded}" "${refused}" "${gitmodules_hits}" "${helper_ok}" "${competing}" "${root_readable}"
"""


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def git_auth_env(pat: str) -> Dict[str, str]:
    """Git auth as config-via-env: the header never appears on the argv.

    THE authority for the env form (ent#615). It predates this issue —
    ``fork_to_own`` shipped it for the backend-host clone/push, which is why the
    fleet-PAT report could say the fix "already exists in-repo, generalize it" —
    and it now has three consumers: that path, the ent#109 in-container rebind
    push, and the skills-library clone. A second hand-written copy is how the
    next pair drifts apart (the ent#347 lesson), so there is one.

    Use this for a credential that must reach ONE git invocation. Use the
    credential HELPER for a credential that must serve every later git
    invocation in a container — ``Config.Env`` is immutable without a recreate,
    which is exactly why the helper exists and this does not replace it.

    ⚠️ Env is not confidential to a SAME-UID reader: ``/proc/<pid>/environ`` is
    readable by the process owner for the life of the process. On the backend
    host that is nobody (``_run_git`` is a backend child); inside an agent
    container it is the agent, so an exec carrying a credential the agent must
    not hold runs as ``root``.
    """
    if not pat:
        return {}
    b64 = base64.b64encode(f"x-access-token:{pat}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: basic {b64}",
    }


def install_command() -> str:
    """Shell command that installs + registers the helper. Run as **root**.

    Base64-injected (the `compatibility/collector.py:181-184` precedent) so the
    script's own quoting can never collide with the `bash -c` wrapper.

    ``--replace-all`` rather than a plain set: ``credential.helper`` is
    multi-valued, so a plain set would append a second identical entry on every
    call, and helper lists COMPOSE — a stale or competing helper answering first
    masks everything behind it.

    Written to a temp path and ``mv``'d into place so a partial write can never
    leave a truncated (and therefore silent) helper on the git auth path.
    """
    payload = _b64(HELPER_SCRIPT)
    tmp = f"{HELPER_PATH}.trinity-tmp"
    return (
        f"printf %s {payload} | base64 -d > {tmp} && "
        f"chmod 0755 {tmp} && chown root:root {tmp} && "
        f"mv -f {tmp} {HELPER_PATH} && "
        f"git config --system --replace-all credential.helper {HELPER_NAME}"
    )


# Answers "would git get a credential for our origin in this container?" — by
# EXIT CODE. `git credential fill` prints the credential on stdout, so it is
# piped into `grep -q` and discarded; nothing but the status leaves the
# container. The origin is derived in-container exactly as the helper derives
# it, so the answer cannot disagree with the helper over a self-hosted base the
# backend and the container spell differently.
_PROBE_SCRIPT = r"""
base="${TRINITY_GIT_BASE_URL:-https://github.com}"
base="${base%/}"
p="${base%%://*}"
h="${base#*://}"
h="${h%%/*}"
printf 'protocol=%s\nhost=%s\n\n' "${p}" "${h}" | git credential fill 2>/dev/null | grep -q '^password=.'
"""


def probe_command() -> str:
    """Shell command whose EXIT CODE says whether a credential resolves."""
    return f"printf %s {_b64(_PROBE_SCRIPT)} | base64 -d | sh"


def scrub_command(git_dir: str) -> str:
    """Shell command that installs the helper **then** scrubs token URLs.

    One exec, in that order, because the strip must never run without the
    replacement already in place. Run as **root**.
    """
    payload = _b64(SCRUB_SCRIPT)
    return (
        f"({install_command()}) >/dev/null 2>&1; "
        f"printf %s {payload} | base64 -d | sh -s -- {git_dir}"
    )


_REPORT_RE = re.compile(rf"{SCRUB_REPORT_PREFIX}\s+(?P<fields>[A-Za-z0-9_=\s]+)")

_REPORT_FIELDS = (
    "remotes_scrubbed",
    "harvested",
    "seeded",
    "refused",
    "gitmodules_hits",
    "helper_ok",
    "competing_helpers",
    "root_readable",
)

# The env name the sweep reads a caller-supplied credential from. It travels in
# the Exec Create body, NEVER on argv — an exec's argv is visible in the
# container's process table, which is the leak this whole issue is about, and
# base64-ing a token onto argv relocates it rather than removing it.
SEED_ENV_VAR = "TRINITY_SEED_PAT"


def parse_scrub_report(output: str) -> Dict[str, int]:
    """Parse the sweep's one-line report. All-zero when it did not report.

    The report carries counts only — never a URL, a host or a value — so it is
    safe to log, persist and return through the API.
    """
    report = {field: 0 for field in _REPORT_FIELDS}
    match = _REPORT_RE.search(output or "")
    if not match:
        return report
    for token in match.group("fields").split():
        key, _, value = token.partition("=")
        if key in report:
            try:
                report[key] = int(value)
            except ValueError:
                report[key] = 0
    return report
