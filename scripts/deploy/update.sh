#!/usr/bin/env bash
#
# update.sh — update a self-hosted Trinity install in one command (#3007).
#
#   ./scripts/deploy/update.sh                 # newest release tag
#   ./scripts/deploy/update.sh --to v1.0.0     # a specific tag (or branch, or sha)
#   ./scripts/deploy/update.sh --check         # preflight only; changes nothing
#   ./scripts/deploy/update.sh --verify        # published-URL verification only
#   ./scripts/deploy/update.sh --rollback      # back to the version before the last update
#
# First update of an install older than this command: run a copy from outside
# the checkout with --dir (docs/user-docs/guides/deploying/upgrading.md).
#
# Also reachable as `trinity update` from the CLI, which runs this file.
#
# Why this exists. Upgrading had no single entry point, and the one the docs
# pointed at (`start.sh`) has no production mode: on a source-built host it
# either refuses (#2528) or — at older tags — recreated the backend from the dev
# file set WITHOUT docker-compose.prod.enterprise.yml, so an entitled instance
# booted OSS-only and nothing said so. Separately, every probe ran against
# localhost or the container, so a change in how a service is PUBLISHED
# (#2280 made FRONTEND_PORT live after months of being inert) shipped a 502
# admin UI behind a green /health. This script closes both:
#
#   1. SHAPE      read from the running backend's own compose label (the exact
#                 file set it was started with) — never assumed, never asked.
#   2. PREFLIGHT  everything that can be checked before anything is touched is
#                 checked first; a failure changes no container, image, checkout
#                 or .env.
#   3. BACKUP     a verified database copy through the running backend.
#   4. CHECKOUT   the target ref, and on prod-enterprise the enterprise tree at
#                 the pin THAT REF records (never a pin from release notes).
#   5. PORTS      the target file set is rendered and every published host port
#                 compared with what is running — a change stops here, before
#                 any image is built, and the checkout is put back.
#   6. IMAGES     platform images rebuilt (or, hosted, pulled via start.sh
#                 --hosted); the agent base image only when the range touched it.
#   7. VERIFY     /health, the enterprise registration line, then the PUBLISHED
#                 URL: real HTML, a hashed asset, and the API through the same
#                 origin. That — not "containers up" — is the definition of done.
#   8. ROLLBACK   the previous version is recorded before anything moves; a
#                 failure prints (and on a terminal, offers) --rollback.
#
# Requirements: docs/memory/requirements/infrastructure.md §8.12 (UPD-001..009).
# Tests: tests/unit/test_3007_update_command.py (executes this file with stub
# docker/curl against a real throwaway git repository).
#
# Bash 3.2-compatible (macOS): no associative arrays, no mapfile, and every
# possibly-empty array is expanded as ${a[@]+"${a[@]}"} so `set -u` holds.

UPDATE_SH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$UPDATE_SH_DIR/../.." && pwd)"

ENT_REL="src/backend/enterprise"
ENT_MARKER="$ENT_REL/backend/__init__.py"
ENT_REGISTERED="Trinity Enterprise modules registered"   # main.py, a declared CI-grep string (#2068)
ENT_FAILED="Trinity Enterprise registration FAILED"
PLATFORM_SERVICES="backend frontend mcp-server scheduler"

# --- output ------------------------------------------------------------------
say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
warn() { printf '⚠️  %s\n' "$*" >&2; }
die()  { printf '❌ %s\n' "$*" >&2; exit 1; }

# --- .env + compose-project helpers -------------------------------------------
# ONE implementation of each Compose-owned rule (#2390): loaded verbatim out of
# start.sh rather than copied, so the two scripts cannot drift apart. The
# `^name() {` ... `^}` shape is already pinned by test_2390's extractor.
load_start_sh_helpers() {
    local fn src="$ROOT/scripts/deploy/start.sh"
    for fn in env_value compose_project_name; do
        eval "$(awk -v start="${fn}() {" '$0 == start {on=1} on {print} on && $0 == "}" {exit}' "$src")"
        declare -F "$fn" >/dev/null || die "could not load ${fn}() from scripts/deploy/start.sh"
    done
    # shellcheck source=env-file.sh source-path=SCRIPTDIR
    . "$ROOT/scripts/deploy/env-file.sh"
}

# --- shape ----------------------------------------------------------------------
# Pure: the comma-separated compose config_files label → a shape name.
shape_from_config_files() {
    local files=",$1,"
    case "$files" in
        *"/docker-compose.hosted.yml,"*|*",docker-compose.hosted.yml,"*) echo hosted ;;
        *"/docker-compose.prod.enterprise.yml,"*|*",docker-compose.prod.enterprise.yml,"*) echo prod-enterprise ;;
        *"/docker-compose.prod.yml,"*|*",docker-compose.prod.yml,"*) echo prod ;;
        *"/docker-compose.yml,"*|*",docker-compose.yml,"*) echo dev ;;
        *) echo "" ;;
    esac
}

running_config_files() {
    docker inspect -f '{{index .Config.Labels "com.docker.compose.project.config_files"}}' \
        trinity-backend 2>/dev/null || true
}

enterprise_tree_present() { [ -f "$ROOT/$ENT_MARKER" ]; }

data_path() {
    local p="${TRINITY_DATA_PATH:-$(env_value TRINITY_DATA_PATH)}"
    p="${p:-./trinity-data}"
    case "$p" in /*) ;; *) p="$ROOT/${p#./}" ;; esac
    printf '%s' "$p"
}

# Sets SHAPE and SHAPE_SOURCE; LABEL_SHAPE is what the running container says.
detect_shape() {
    LABEL_SHAPE=""
    if [ -n "$OPT_SHAPE" ]; then
        case "$OPT_SHAPE" in dev|prod|prod-enterprise|hosted) ;; *)
            die "--shape must be one of: dev, prod, prod-enterprise, hosted (got '$OPT_SHAPE')" ;;
        esac
        SHAPE="$OPT_SHAPE"; SHAPE_SOURCE="--shape"
        return 0
    fi
    local label
    label="$(running_config_files)"
    LABEL_SHAPE="$(shape_from_config_files "$label")"
    if [ -n "$LABEL_SHAPE" ]; then
        SHAPE="$LABEL_SHAPE"; SHAPE_SOURCE="running backend's compose label"
        # A prod host whose running backend lacks the overlay while the
        # enterprise tree IS present is the #3007 failure-1 aftermath: the
        # backend was recreated OSS-only. Put it back on the overlay.
        if [ "$SHAPE" = "prod" ] && enterprise_tree_present; then
            SHAPE="prod-enterprise"
            warn "the running backend was started without the enterprise overlay"
            warn "(docker-compose.prod.enterprise.yml) although the enterprise tree is present —"
            warn "it is running OSS-only. This update brings it back onto the overlay."
        fi
        return 0
    fi
    # No backend container to ask: fall back to what is on disk.
    local bind=0 devvol=0 pinned=0
    # Only prod and hosted bind-mount the data dir; a PostgreSQL install has no
    # trinity.db there but still keeps backups and agent files in it.
    [ -n "$(ls -A "$(data_path)" 2>/dev/null)" ] && bind=1
    docker volume inspect "$(compose_project_name)_trinity-data" >/dev/null 2>&1 && devvol=1
    [ -n "$(env_value TRINITY_IMAGE_TAG)" ] && pinned=1
    if [ "$pinned" = 1 ] && [ "$devvol" = 0 ]; then
        SHAPE=hosted
    elif [ "$bind" = 1 ] && [ "$devvol" = 0 ]; then
        if enterprise_tree_present; then SHAPE=prod-enterprise; else SHAPE=prod; fi
    elif [ "$devvol" = 1 ] && [ "$bind" = 0 ] && [ "$pinned" = 0 ]; then
        SHAPE=dev
    else
        printf '❌ Cannot tell how this install was deployed, so it will not guess.\n' >&2
        printf '   No trinity-backend container to read the compose file set from, and the\n' >&2
        printf '   disk evidence is ambiguous: bind-mounted data=%s, dev volume=%s, TRINITY_IMAGE_TAG pinned=%s.\n' \
            "$bind" "$devvol" "$pinned" >&2
        printf '   Re-run with --shape dev|prod|prod-enterprise|hosted.\n' >&2
        exit 1
    fi
    SHAPE_SOURCE="on-disk evidence (no running backend)"
}

set_compose_files() {
    COMPOSE_FILES=()
    case "$SHAPE" in
        dev) ;;  # no -f: compose merges docker-compose.yml + override, as start.sh does
        prod) COMPOSE_FILES=(-f docker-compose.prod.yml) ;;
        prod-enterprise) COMPOSE_FILES=(-f docker-compose.prod.yml -f docker-compose.prod.enterprise.yml) ;;
        hosted)
            COMPOSE_FILES=(-f docker-compose.hosted.yml)
            # start.sh --hosted appends the override explicitly (#1432); match it.
            if [ -f "$ROOT/docker-compose.override.yml" ]; then
                COMPOSE_FILES+=(-f docker-compose.override.yml)
            fi
            ;;
    esac
}

dc() { docker compose ${COMPOSE_FILES[@]+"${COMPOSE_FILES[@]}"} "$@"; }

# --- git ------------------------------------------------------------------------
g() { git -C "$ROOT" "$@"; }

latest_release_tag() {
    g tag -l 'v*' --sort=-v:refname | grep -vE -- '-' | head -1 || true
}

# Resolves OPT_TO (or the newest release tag) to TARGET_REF / TARGET_SHA.
resolve_target() {
    TARGET_REF="${OPT_TO:-}"
    if [ -z "$TARGET_REF" ]; then
        TARGET_REF="$(latest_release_tag)"
        [ -n "$TARGET_REF" ] || die "no release tag (v*) found — pass --to <tag|branch>"
    fi
    TARGET_IS_BRANCH=0
    if g rev-parse -q --verify "refs/remotes/origin/$TARGET_REF" >/dev/null; then
        TARGET_IS_BRANCH=1
        TARGET_SHA="$(g rev-parse "refs/remotes/origin/$TARGET_REF")"
    elif TARGET_SHA="$(g rev-parse -q --verify "${TARGET_REF}^{commit}" 2>/dev/null)"; then
        :
    else
        die "target '$TARGET_REF' does not resolve to a commit (after fetching origin)"
    fi
}

describe() { g describe --tags --always "$1" 2>/dev/null || printf '%s' "${1:0:12}"; }

checkout_target() {
    if [ "$TARGET_IS_BRANCH" = 1 ]; then
        g checkout -q --no-recurse-submodules "$TARGET_REF"
        g merge -q --ff-only "refs/remotes/origin/$TARGET_REF"
    else
        g checkout -q --no-recurse-submodules --detach "$TARGET_SHA"
    fi
}

# Puts the checkout (and enterprise tree) back and proves it. Non-zero with the
# reason on stderr when it did not land: the callers must not rebuild "the old
# version" out of whatever the tree holds now, or report it as restored.
restore_checkout() {
    local failed=0 head
    if [ -n "${PREV_BRANCH:-}" ]; then
        g checkout -q --no-recurse-submodules "$PREV_BRANCH" && g reset -q --hard "$PREV_SHA" || failed=1
    else
        g checkout -q --no-recurse-submodules --detach "$PREV_SHA" || failed=1
    fi
    head="$(g rev-parse HEAD 2>/dev/null || true)"
    if [ "$failed" = 1 ] || [ "$head" != "$PREV_SHA" ]; then
        printf '❌ could not put the checkout back at %s (it is at %s).\n' "${PREV_SHA:0:12}" "${head:0:12}" >&2
        failed=1
    fi
    if [ "$SHAPE" = prod-enterprise ] && [ -n "${PREV_ENT_SHA:-}" ]; then
        git -C "$ROOT/$ENT_REL" checkout -q --detach "$PREV_ENT_SHA" || failed=1
        head="$(git -C "$ROOT/$ENT_REL" rev-parse HEAD 2>/dev/null || true)"
        if [ "$head" != "$PREV_ENT_SHA" ] || [ "$failed" = 1 ]; then
            printf '❌ could not put the enterprise tree back at %s (it is at %s).\n' "${PREV_ENT_SHA:0:12}" "${head:0:12}" >&2
            failed=1
        fi
    fi
    return "$failed"
}

# The enterprise pin recorded by a commit (empty when it records none).
enterprise_pin_at() { g rev-parse -q --verify "$1:$ENT_REL" 2>/dev/null || true; }

ensure_enterprise_commit() {
    local want="$1" ent="$ROOT/$ENT_REL"
    git -C "$ent" cat-file -e "${want}^{commit}" 2>/dev/null && return 0
    git -C "$ent" fetch -q origin 2>/dev/null || true
    git -C "$ent" cat-file -e "${want}^{commit}" 2>/dev/null
}

# --- ports ----------------------------------------------------------------------
# "service container ip:port ip:port ..." per line, from the rendered file set.
# Exit 2 when the file set cannot be rendered to JSON or parsed — the caller
# must treat that as "unknown", never as "no change" (fail closed).
rendered_ports() {
    local json
    json="$(dc config --format json 2>/dev/null)" || return 2
    printf '%s' "$json" | python3 -c '
import json, sys
try:
    doc = json.load(sys.stdin)
except Exception:
    sys.exit(2)
for name, svc in sorted((doc.get("services") or {}).items()):
    ports = sorted({"%s:%s" % (p.get("host_ip") or "0.0.0.0", p.get("published"))
                    for p in (svc.get("ports") or [])
                    if isinstance(p, dict) and p.get("published") not in (None, "")})
    print(name, svc.get("container_name") or "-", *ports)
'
}

# A binding docker records with no HostIp listens on every interface, which is
# what the rendered side spells 0.0.0.0.
running_ports() {
    docker inspect -f '{{range $p, $b := .HostConfig.PortBindings}}{{range $b}}{{.HostIp}}:{{.HostPort}} {{end}}{{end}}' \
        "$1" 2>/dev/null | tr ' ' '\n' | grep -v '^$' | sed 's/^:/0.0.0.0:/' | sort -u | tr '\n' ' ' | sed 's/ $//'
}

# One line per service whose bindings would change, tagged by what it means:
#   MOVED  a binding the running container has is gone or re-addressed. Anything
#          pointed at it (a host proxy, a firewall rule, a tunnel) stops reaching it.
#   ADDED  only new bindings. Nothing that works today stops working.
# Returns 2 when the comparison could not be made at all.
port_drift() {
    command -v python3 >/dev/null 2>&1 || return 2
    local rendered name container want have b gone added
    rendered="$(rendered_ports)" || return 2
    while read -r name container want; do
        [ -n "$name" ] && [ "$container" != "-" ] || continue
        docker inspect "$container" >/dev/null 2>&1 || continue   # not running: nothing to compare
        have="$(running_ports "$container")"
        gone="" added=""
        for b in $have; do case " $want " in *" $b "*) ;; *) gone="$gone $b" ;; esac; done
        for b in $want; do case " $have " in *" $b "*) ;; *) added="$added $b" ;; esac; done
        if [ -n "$gone" ]; then
            printf 'MOVED %s: published binding(s) [%s] -> [%s]\n' "$name" "${have:-none}" "${want:-none}"
        elif [ -n "$added" ]; then
            printf 'ADDED %s: new published binding(s)%s\n' "$name" "$added"
        fi
    done <<EOF_PORTS
$rendered
EOF_PORTS
}

# --- verification -----------------------------------------------------------------
wait_for_health() {
    local deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
    while :; do
        curl -fsS -m 5 -o /dev/null "http://localhost:8000/health" 2>/dev/null && return 0
        [ "$(date +%s)" -lt "$deadline" ] || return 1
        sleep 3
    done
}

check_enterprise_registration() {
    local since log
    since="$(docker inspect -f '{{.State.StartedAt}}' trinity-backend 2>/dev/null || true)"
    if [ -n "$since" ]; then
        log="$(docker logs trinity-backend --since "$since" 2>&1 || true)"
    else
        log="$(docker logs trinity-backend --tail 2000 2>&1 || true)"
    fi
    # A here-string, never `printf | grep -q` under pipefail: once the log outgrows
    # the pipe buffer grep exits first, printf dies of SIGPIPE, and a healthy
    # install reads as OSS-only.
    if grep -q "$ENT_FAILED" <<<"$log"; then
        printf '❌ Enterprise registration FAILED — the backend is running OSS-only (entitled routes 403).\n' >&2
        { grep -A 10 "$ENT_FAILED" <<<"$log" | head -20 >&2; } || true
        return 1
    fi
    if ! grep -q "$ENT_REGISTERED" <<<"$log"; then
        printf "❌ The backend never printed '%s' — it is running OSS-only.\n" "$ENT_REGISTERED" >&2
        return 1
    fi
    say "✓ enterprise modules registered"
}

published_url() {
    if [ -n "$OPT_URL" ]; then printf '%s' "${OPT_URL%/}"; return; fi
    local u port
    u="$(env_value FRONTEND_URL)"
    if [ -n "$u" ]; then printf '%s' "${u%/}"; return; fi
    port="${FRONTEND_PORT:-$(env_value FRONTEND_PORT)}"
    port="${port:-80}"
    if [ "$port" = 80 ]; then printf 'http://localhost'; else printf 'http://localhost:%s' "$port"; fi
}

# One attempt at loading the entry point a USER reaches: real HTML, a hashed asset
# that HTML references, and the API through the same origin. ~The check that would
# have caught #3007 failure 2 (host proxy on :80, frontend moved to :3000). Leaves
# the reason in PUBLISHED_ERR rather than printing it, because the caller may retry.
published_attempt() {
    local base="$1" tmp="$2" asset
    PUBLISHED_ERR=""
    if ! curl -fsS -m 15 -o "$tmp/index" "$base/" 2>/dev/null; then
        PUBLISHED_ERR="$base/ did not answer 200."
    elif ! grep -qi '<html' "$tmp/index"; then
        PUBLISHED_ERR="$base/ answered, but not with HTML (a proxy error page?)."
    else
        asset="$(grep -oE '(src|href)="[^"]*/assets/[^"]+-[A-Za-z0-9_-]{6,}\.(js|css)"' "$tmp/index" \
            | head -1 | sed -E 's/^(src|href)="//; s/"$//' || true)"
        case "$asset" in http*) ;; /*) asset="$base$asset" ;; ?*) asset="$base/$asset" ;; esac
        if [ -z "$asset" ]; then
            PUBLISHED_ERR="$base/ is HTML but references no hashed /assets/ bundle — not the Trinity UI build."
        elif ! curl -fsS -m 15 -o /dev/null "$asset" 2>/dev/null; then
            PUBLISHED_ERR="the UI bundle $asset did not answer 200."
        elif ! curl -fsS -m 15 -o "$tmp/api" "$base/api/auth/mode" 2>/dev/null; then
            PUBLISHED_ERR="$base/api/auth/mode did not answer 200 — the API is not reachable through this origin."
        elif ! grep -q '^[[:space:]]*{' "$tmp/api"; then
            PUBLISHED_ERR="$base/api/auth/mode answered, but not with JSON — the proxy is not routing /api."
        fi
    fi
    [ -z "$PUBLISHED_ERR" ]
}

# $1: how many seconds to keep trying. A recreated frontend's nginx is not
# listening yet when `up` returns, so the first refusal right after a restart says
# nothing; only the verdict at the deadline counts, as with wait_for_health.
verify_published() {
    local budget="${1:-0}" base tmp deadline ok=0
    base="$(published_url)"
    tmp="$(mktemp -d)"
    say "Verifying the published entry point: $base"
    if [ -z "$OPT_URL" ] && [ -z "$(env_value FRONTEND_URL)" ]; then
        say "  (no FRONTEND_URL in .env — this is the local port only; pass --url https://your-domain"
        say "   to verify what users actually load)"
    fi
    deadline=$(( $(date +%s) + budget ))
    while :; do
        if published_attempt "$base" "$tmp"; then ok=1; break; fi
        [ "$(date +%s)" -lt "$deadline" ] || break
        sleep 2
    done
    rm -rf "$tmp"
    if [ "$ok" = 1 ]; then
        say "✓ $base serves the UI (HTML + hashed asset) and the API through the same origin"
        return 0
    fi
    printf '❌ %s\n' "$PUBLISHED_ERR" >&2
    return 1
}

# --- state ----------------------------------------------------------------------
state_file() {
    local dir="${TRINITY_UPDATE_STATE_DIR:-$HOME/.trinity/update}"
    printf '%s/%s.env' "$dir" "$(cd "$ROOT" && compose_project_name)"
}

write_state() {
    local f; f="$(state_file)"
    mkdir -p "$(dirname "$f")"
    {
        printf 'PREV_SHA=%s\n' "$PREV_SHA"
        printf 'PREV_BRANCH=%s\n' "${PREV_BRANCH:-}"
        printf 'PREV_ENT_SHA=%s\n' "${PREV_ENT_SHA:-}"
        printf 'PREV_IMAGE_TAG=%s\n' "${PREV_IMAGE_TAG:-}"
        printf 'ROLLBACK_IMAGE_TAG=%s\n' "${ROLLBACK_IMAGE_TAG:-}"
        printf 'SHAPE=%s\n' "$SHAPE"
        printf 'TARGET_SHA=%s\n' "$TARGET_SHA"
        printf 'BACKUP=%s\n' "${BACKUP_PATH:-}"
        printf 'SCHEMA_BEFORE=%s\n' "${SCHEMA_BEFORE:-}"
        printf 'STARTED_AT=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } > "$f"
}

read_state_key() { grep -E "^$1=" "$(state_file)" 2>/dev/null | tail -1 | cut -d= -f2- || true; }

# The tag rollback returns to on a hosted install. `latest` (or no tag, which
# compose reads as latest) is not one: after the update it names the NEW release,
# so re-pulling it would print "back at <prev>" over the same images. Read the
# version off the running image and accept it only if it is a release this checkout
# knows; both `1.2.3` and `v1.2.3` are published for one digest. Empty = none.
concrete_running_tag() {
    local pinned="$1" v
    case "$pinned" in ""|latest) ;; *) printf '%s' "$pinned"; return 0 ;; esac
    v="$(docker inspect -f '{{index .Config.Labels "org.opencontainers.image.version"}}' trinity-backend 2>/dev/null || true)"
    case "$v" in
        v[0-9]*|[0-9]*) ;;
        *) return 0 ;;
    esac
    if g rev-parse -q --verify "refs/tags/v${v#v}" >/dev/null 2>&1; then printf 'v%s' "${v#v}"; fi
    return 0
}

# --- preflight --------------------------------------------------------------------
PREFLIGHT_FAILS=0
pf_fail() { printf '❌ preflight: %s\n' "$*" >&2; PREFLIGHT_FAILS=$((PREFLIGHT_FAILS + 1)); }
pf_ok()   { say "✓ $*"; }

preflight() {
    step "Preflight (nothing is changed until every check passes)"
    docker info >/dev/null 2>&1 || die "Docker is not reachable — start it (or run as a user in the docker group) and re-run."
    [ -f "$ROOT/.env" ] || die "no .env in $ROOT — this does not look like an installed Trinity."
    detect_shape
    set_compose_files
    say "Install shape: $SHAPE  (from the $SHAPE_SOURCE)"
    say "Compose files: ${COMPOSE_FILES[*]:-(default: docker-compose.yml + override)}"

    # ent#435: with cleartext secrets present the backend refuses to boot
    # without the key — discovered there, the instance is already down.
    if [ -n "${CREDENTIAL_ENCRYPTION_KEY:-$(env_value CREDENTIAL_ENCRYPTION_KEY)}" ]; then
        pf_ok "CREDENTIAL_ENCRYPTION_KEY is set"
    else
        pf_fail "CREDENTIAL_ENCRYPTION_KEY is not set in .env. The backend refuses to start without it once
   credentials are stored encrypted, so the update would take this instance down mid-way.
   Restore the key this install was created with (from your secrets store or an old .env).
   Only if this install never had one: generate it with 'openssl rand -hex 32', add
   CREDENTIAL_ENCRYPTION_KEY=<value> to .env, and re-run."
    fi

    local dirty
    dirty="$(g status --porcelain --untracked-files=no --ignore-submodules=all 2>/dev/null || true)"
    if [ -n "$dirty" ]; then
        pf_fail "tracked files have local changes, which a checkout would refuse or carry across:
$(printf '%s\n' "$dirty" | sed 's/^/     /')
   Commit or stash them (git stash push -m pre-update), then re-run."
    else
        pf_ok "checkout has no local changes to tracked files"
    fi

    local min_gb="${TRINITY_UPDATE_MIN_FREE_GB:-5}" free_kb
    free_kb="$(df -Pk "$ROOT" 2>/dev/null | awk 'NR==2 {print $4}')"
    if [ -n "$free_kb" ] && [ "$free_kb" -lt $(( min_gb * 1024 * 1024 )) ]; then
        pf_fail "only $(( free_kb / 1024 / 1024 )) GB free under $ROOT; an update needs at least ${min_gb} GB
   (TRINITY_UPDATE_MIN_FREE_GB). Free space (docker image prune) and re-run."
    else
        pf_ok "disk space"
    fi

    if dc config -q >/dev/null 2>"$WORK/config.err"; then
        pf_ok "current compose file set renders"
    else
        pf_fail "the current compose file set does not render:
$(sed 's/^/     /' "$WORK/config.err")"
    fi

    # Target.
    # --force: a tag re-pointed upstream (dev-green moves on every green dev) would
    # otherwise fail the whole fetch and print a false alarm below.
    g fetch -q --force --tags --no-recurse-submodules origin 2>/dev/null \
        || warn "git fetch from origin failed — resolving the target from refs already on this host"
    resolve_target
    PREV_SHA="$(g rev-parse HEAD)"
    PREV_BRANCH="$(g symbolic-ref -q --short HEAD 2>/dev/null || true)"
    say "Current: $(describe "$PREV_SHA")   Target: $TARGET_REF ($(describe "$TARGET_SHA"))"

    # The default target is the newest RELEASE tag, so an install on a release
    # candidate or a branch past it would otherwise be "updated" backwards.
    if [ "$PREV_SHA" != "$TARGET_SHA" ] && g merge-base --is-ancestor "$TARGET_SHA" "$PREV_SHA" 2>/dev/null; then
        if [ "$OPT_ALLOW_DOWNGRADE" = 1 ]; then
            warn "--allow-downgrade: $TARGET_REF is OLDER than this checkout. A migration already applied"
            warn "here may not be understood by that release."
        else
            pf_fail "$TARGET_REF is older than this checkout ($(describe "$PREV_SHA")), so this would move the install
   backwards. Nothing newer to update to? There is nothing to do. Meant to downgrade? Re-run with
   --allow-downgrade."
        fi
    fi
    if [ "$OPT_ALLOW_DOWNGRADE" = 1 ] && ! g cat-file -e "$TARGET_SHA:scripts/deploy/update.sh" 2>/dev/null; then
        warn "$TARGET_REF does not contain update.sh: it leaves the checkout with this update, and --rollback"
        warn "afterwards needs the first-time step in docs/user-docs/guides/deploying/upgrading.md."
    fi

    if [ "$SHAPE" = prod-enterprise ]; then
        local want ent="$ROOT/$ENT_REL"
        want="$(enterprise_pin_at "$TARGET_SHA")"
        if ! enterprise_tree_present; then
            pf_fail "this is an enterprise install, but the enterprise tree ($ENT_REL) is not checked out.
   Updating now would boot the backend OSS-only (every entitled route 403). Populate it first —
   docs/ENTERPRISE.md — then re-run. (Deliberately going OSS-only? Re-run with --shape prod.)"
        elif [ -z "$want" ]; then
            pf_fail "$TARGET_REF records no enterprise pin ($ENT_REL) — it cannot be deployed as enterprise."
        elif [ -n "$(git -C "$ent" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
            pf_fail "the enterprise tree has local changes; commit or discard them and re-run."
        elif ! ensure_enterprise_commit "$want"; then
            pf_fail "the enterprise pin ${want:0:12} recorded by $TARGET_REF is not in the local enterprise checkout
   and could not be fetched. Deploying anyway would run new platform code on the OLD enterprise
   tree. Check the enterprise remote's access (docs/ENTERPRISE.md) and re-run."
        else
            PREV_ENT_SHA="$(git -C "$ent" rev-parse HEAD)"
            TARGET_ENT_SHA="$want"
            pf_ok "enterprise pin ${want:0:12} (read from $TARGET_REF) is available"
        fi
    fi

    if [ "$SHAPE" = hosted ]; then
        PREV_IMAGE_TAG="$(env_value TRINITY_IMAGE_TAG)"
        ROLLBACK_IMAGE_TAG="$(concrete_running_tag "$PREV_IMAGE_TAG")"
        if [ -z "$ROLLBACK_IMAGE_TAG" ]; then
            warn "no concrete image tag to come back to: TRINITY_IMAGE_TAG is '${PREV_IMAGE_TAG:-unset}' and the"
            warn "running image names no release this checkout knows. --rollback will refuse, because"
            warn "':-latest' now means the new release. To be able to roll back, pin TRINITY_IMAGE_TAG first."
        fi
        if [ "$TARGET_IS_BRANCH" = 1 ]; then
            pf_fail "a hosted install runs published images, which exist per release tag — '$TARGET_REF' is a
   branch. Pass --to <release tag> (e.g. the newest at https://github.com/abilityai/trinity/releases)."
        else
            say "Image tag: ${PREV_IMAGE_TAG:-unset} -> $TARGET_REF"
        fi
    fi

    if [ "$PREFLIGHT_FAILS" -gt 0 ]; then
        printf '\n❌ %s preflight check(s) failed. Nothing was changed.\n' "$PREFLIGHT_FAILS" >&2
        exit 1
    fi
    pf_ok "preflight passed"
}

# --- database (through the running backend) ------------------------------------------
# Runs scripts/deploy/update-db.py inside trinity-backend; prints its last line.
db_helper() {
    local out rc=0
    out="$(docker exec -i trinity-backend python - "$1" < "$UPDATE_SH_DIR/update-db.py" 2>&1)" || rc=$?
    printf '%s\n' "$out" | tail -1
    return "$rc"
}

json_field() { printf '%s' "$2" | sed -nE "s/.*\"$1\": *\"([^\"]+)\".*/\1/p"; }

# The schema position: a hash over every migration the database records. Empty
# when it cannot be read — which rollback treats as "moved" (fail closed).
schema_position() {
    local out
    out="$(db_helper schema)" || return 0
    [ "$(json_field status "$out")" = ok ] || return 0
    json_field position "$out"
}

# --- backup -------------------------------------------------------------------------
backup() {
    BACKUP_PATH=""
    if [ "$OPT_SKIP_BACKUP" = 1 ]; then
        warn "--skip-backup: no pre-update database copy will be taken"
        return 0
    fi
    step "Backup"
    local out status
    if ! docker inspect trinity-backend >/dev/null 2>&1; then
        die "no running trinity-backend to take the pre-update backup through. Start it, or take
   your own copy and re-run with --skip-backup."
    fi
    out="$(db_helper backup)" || true
    out="$(printf '%s\n' "$out" | tail -1)"
    status="$(json_field status "$out")"
    BACKUP_PATH="$(json_field path "$out")"
    case "$status" in
        ok) say "✓ database copied to $BACKUP_PATH (inside the backend container's /data)" ;;
        *)
            printf '❌ pre-update backup failed: %s\n' "$out" >&2
            printf '   Nothing was changed. Fix the cause, or take your own copy and re-run with --skip-backup.\n' >&2
            exit 1
            ;;
    esac
}

# --- the update -------------------------------------------------------------------
abort_before_containers() {
    printf '❌ %s\n' "$1" >&2
    [ "$SHAPE" = hosted ] && [ -n "${PREV_IMAGE_TAG+x}" ] && (cd "$ROOT" && set_env_key TRINITY_IMAGE_TAG "$PREV_IMAGE_TAG")
    if restore_checkout; then
        printf '   The checkout was put back at %s. No image was built and no container was recreated.\n' \
            "$(describe "$PREV_SHA")" >&2
    else
        printf '   No image was built and no container was recreated, but the tree needs fixing by hand.\n' >&2
    fi
    exit 1
}

check_ports() {
    local drift rc=0
    drift="$(port_drift)" || rc=$?
    if [ "$rc" != 0 ]; then
        if [ "$OPT_ACCEPT_PORTS" = 1 ]; then
            warn "published ports could not be compared (needs python3 and 'docker compose config --format json'); proceeding under --accept-port-change"
            return 0
        fi
        abort_before_containers "could not compare published ports: the target file set did not render to
   JSON (or python3 is missing). This check is what catches a proxy losing its upstream after an
   update, so it does not pass silently. Install python3 / update Docker Compose, or re-run with
   --accept-port-change once you have checked the ports yourself."
    fi
    local moved added
    moved="$(printf '%s\n' "$drift" | sed -n 's/^MOVED //p')"
    added="$(printf '%s\n' "$drift" | sed -n 's/^ADDED //p')"
    if [ -n "$added" ]; then
        warn "the target publishes something new (nothing that works today stops working):"
        printf '%s\n' "$added" | sed 's/^/     /' >&2
    fi
    [ -n "$moved" ] || { say "✓ no published binding moves"; return 0; }
    if [ "$OPT_ACCEPT_PORTS" = 1 ]; then
        warn "published bindings change (accepted with --accept-port-change):"
        printf '%s\n' "$moved" | sed 's/^/     /' >&2
        return 0
    fi
    abort_before_containers "the target would change where services are published:
$(printf '%s\n' "$moved" | sed 's/^/     /')
   Anything in front of the old binding (a host nginx/Caddy, a firewall rule, a tunnel) would stop
   reaching it. Usually a .env key (e.g. FRONTEND_PORT) has started to apply. Fix .env so the binding
   stays, or update the proxy and re-run with --accept-port-change."
}

build_and_start() {
    step "Build"
    if git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        export GIT_COMMIT GIT_COMMIT_SUBJECT GIT_COMMIT_TIMESTAMP GIT_BRANCH VERSION BUILD_DATE
        GIT_COMMIT="$(g rev-parse HEAD)"
        GIT_COMMIT_SUBJECT="$(g log -1 --pretty=%s)"
        GIT_COMMIT_TIMESTAMP="$(g log -1 --pretty=%cI)"
        GIT_BRANCH="$(g rev-parse --abbrev-ref HEAD)"
        VERSION="$(cat "$ROOT/VERSION" 2>/dev/null || echo unknown)+g$(g rev-parse --short=8 HEAD)"
        BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    fi
    # shellcheck disable=SC2086  # PLATFORM_SERVICES is a word list
    dc build $PLATFORM_SERVICES || return 1
    local why=""
    if [ "$OPT_REBUILD_BASE" = 1 ]; then
        why="--rebuild-base"
    elif [ "$OPT_SKIP_BASE" != 1 ] && [ -n "${FROM_SHA:-}" ] \
        && ! g diff --quiet "$FROM_SHA" HEAD -- docker/base-image 2>/dev/null; then
        why="docker/base-image changed in this range"
    fi
    if [ -n "$why" ]; then
        say "Rebuilding the agent base image ($why)"
        "$ROOT/scripts/deploy/build-base-image.sh" || return 1
        BASE_REBUILT=1
    fi
    step "Restart"
    dc up -d || return 1
}

start_hosted() {
    step "Pull and restart (start.sh --hosted)"
    (cd "$ROOT" && set_env_key TRINITY_IMAGE_TAG "$1")
    (cd "$ROOT" && ./scripts/deploy/start.sh --hosted --unattended)
}

verify_all() {
    step "Verify"
    if wait_for_health; then
        say "✓ backend /health answers"
    else
        printf '❌ backend /health did not answer within %ss.\n' "$HEALTH_TIMEOUT" >&2
        return 1
    fi
    if [ "$SHAPE" = prod-enterprise ]; then
        check_enterprise_registration || return 1
    fi
    verify_published "$PUBLISHED_TIMEOUT"
}

offer_rollback() {
    printf '\n❌ The update did not verify. The previous version is recorded; to return to it:\n' >&2
    printf '       ./scripts/deploy/update.sh --rollback\n' >&2
    if [ "$OPT_YES" != 1 ] && [ -t 0 ]; then
        local ans=""
        read -r -p "Roll back to $(describe "$PREV_SHA") now? [y/N] " ans || true
        case "$ans" in y|Y|yes) exec bash "$ROOT/scripts/deploy/update.sh" --rollback --yes ;; esac
    fi
    exit 1
}

do_update() {
    preflight
    if [ "$PREV_SHA" = "$TARGET_SHA" ] && { [ "$SHAPE" != hosted ] || [ "${PREV_IMAGE_TAG:-}" = "$TARGET_REF" ]; } \
        && { [ "$SHAPE" != prod-enterprise ] || [ "${PREV_ENT_SHA:-}" = "${TARGET_ENT_SHA:-}" ]; } \
        && { [ -z "$LABEL_SHAPE" ] || [ "$LABEL_SHAPE" = "$SHAPE" ]; }; then
        say ""
        say "Already on $TARGET_REF — nothing to update. (Run --verify to check the published URL.)"
        return 0
    fi
    if [ "$OPT_CHECK" = 1 ]; then
        say ""
        say "--check: preflight passed; would update $(describe "$PREV_SHA") -> $TARGET_REF ($SHAPE). Nothing was changed."
        return 0
    fi

    backup
    SCHEMA_BEFORE="$(schema_position)"
    if [ -n "$SCHEMA_BEFORE" ]; then
        say "✓ schema position recorded ($SCHEMA_BEFORE)"
    else
        warn "could not read the schema position through the backend. If this update runs a"
        warn "migration you will not be able to roll the code back on its own: --rollback will"
        warn "require restoring the database first (--db-restored)."
    fi
    write_state
    say "Rollback point recorded: $(state_file)"

    step "Checkout $TARGET_REF"
    FROM_SHA="$PREV_SHA"
    checkout_target || abort_before_containers "checkout of $TARGET_REF failed"
    if [ "$SHAPE" = prod-enterprise ]; then
        git -C "$ROOT/$ENT_REL" checkout -q --detach "$TARGET_ENT_SHA" \
            || abort_before_containers "could not check out the enterprise pin ${TARGET_ENT_SHA:0:12}"
        say "✓ enterprise tree at ${TARGET_ENT_SHA:0:12} (the pin $TARGET_REF records)"
    fi
    dc config -q >/dev/null 2>"$WORK/config.err" \
        || abort_before_containers "the target compose file set does not render (a new required .env key?):
$(sed 's/^/     /' "$WORK/config.err")"
    check_ports

    if [ "$SHAPE" = hosted ]; then
        if ! start_hosted "$TARGET_REF"; then
            (cd "$ROOT" && set_env_key TRINITY_IMAGE_TAG "$PREV_IMAGE_TAG")
            restore_checkout || true   # says so itself; the guidance below stands either way
            printf '❌ start.sh --hosted failed. TRINITY_IMAGE_TAG was put back to %s.\n' "${PREV_IMAGE_TAG:-unset}" >&2
            printf '   If it failed after pulling, new images may have run a migration. To return, run\n' >&2
            printf '       ./scripts/deploy/update.sh --rollback\n' >&2
            printf '   which checks the schema first. Starting the old images directly does not.\n' >&2
            printf '   If it refused a SQLite database, this checkout is back on the old release, which has no\n' >&2
            printf '   copy command. Follow docs/migrations/SQLITE_TO_POSTGRES.md from the target release.\n' >&2
            exit 1
        fi
    else
        build_and_start || offer_rollback
    fi

    verify_all || offer_rollback
    report
}

report() {
    step "Done"
    say "Updated $(describe "$FROM_SHA") -> $TARGET_REF ($SHAPE)."
    [ -n "${BACKUP_PATH:-}" ] && say "Pre-update database copy: $BACKUP_PATH"
    if [ "${BASE_REBUILT:-0}" = 1 ] || [ "$SHAPE" = hosted ]; then
        say "Agents adopt the new base image on their next cold stop/start."
    fi
    say "Undo with: ./scripts/deploy/update.sh --rollback"
}

do_rollback() {
    step "Rollback"
    [ -f "$(state_file)" ] || die "no recorded update to roll back ($(state_file) does not exist)."
    docker info >/dev/null 2>&1 || die "Docker is not reachable."
    PREV_SHA="$(read_state_key PREV_SHA)"
    PREV_BRANCH="$(read_state_key PREV_BRANCH)"
    PREV_ENT_SHA="$(read_state_key PREV_ENT_SHA)"
    PREV_IMAGE_TAG="$(read_state_key PREV_IMAGE_TAG)"
    SHAPE="${OPT_SHAPE:-$(read_state_key SHAPE)}"
    BACKUP_PATH="$(read_state_key BACKUP)"
    [ -n "$PREV_SHA" ] && [ -n "$SHAPE" ] || die "the recorded state in $(state_file) is incomplete."
    set_compose_files
    FROM_SHA="$(g rev-parse HEAD)"
    # restore_checkout ends in `reset --hard`, which would destroy local edits.
    local dirty
    dirty="$(g status --porcelain --untracked-files=no --ignore-submodules=all 2>/dev/null || true)"
    if [ -n "$dirty" ]; then
        die "tracked files have local changes, which the rollback would destroy:
$(printf '%s\n' "$dirty" | sed 's/^/     /')
   Commit or stash them (git stash push -m pre-rollback), then re-run."
    fi
    if [ "$SHAPE" = prod-enterprise ] && [ -n "$(git -C "$ROOT/$ENT_REL" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
        die "the enterprise tree has local changes, which the rollback would destroy; commit or discard them and re-run."
    fi
    local image_tag=""
    if [ "$SHAPE" = hosted ]; then
        image_tag="$(read_state_key ROLLBACK_IMAGE_TAG)"
        case "$image_tag" in ""|latest)
            die "no concrete image tag was recorded to return to (TRINITY_IMAGE_TAG was '${PREV_IMAGE_TAG:-unset}').
   Re-pulling 'latest' would bring back the same new release while reporting a rollback. Nothing was
   changed. Pick the release yourself: ./scripts/deploy/update.sh --to <tag> --allow-downgrade" ;;
        esac
    fi
    local schema_before schema_now
    schema_before="$(read_state_key SCHEMA_BEFORE)"
    if [ "$OPT_DB_RESTORED" = 1 ]; then
        warn "--db-restored: trusting that the database was restored to the pre-update copy"
    else
        schema_now="$(schema_position)"
        if [ -z "$schema_before" ] || [ -z "$schema_now" ] || [ "$schema_before" != "$schema_now" ]; then
            refuse_code_only_rollback "$schema_before" "$schema_now"
        fi
        say "✓ schema unchanged since the update ($schema_now) — rolling back the code alone is safe"
    fi
    say "Returning to $(describe "$PREV_SHA") ($SHAPE)"
    restore_checkout || die "the rollback stopped before rebuilding anything: the tree is not at the recorded version."
    if [ "$SHAPE" = hosted ]; then
        start_hosted "$image_tag" || die "start.sh --hosted failed during rollback."
    else
        build_and_start || die "rebuild during rollback failed."
    fi
    verify_all || die "the rolled-back version did not verify either — see the output above."
    step "Rolled back"
    say "Code is back at $(describe "$PREV_SHA")."
    [ "$SHAPE" != hosted ] || say "Images are back at $image_tag."
    if [ ! -f "$ROOT/scripts/deploy/update.sh" ]; then
        say "This version predates update.sh, so the script is gone with the rollback. To update again, use the"
        say "first-time step in docs/user-docs/guides/deploying/upgrading.md (on the release you are moving to)."
    fi
    if [ "$OPT_DB_RESTORED" = 1 ]; then
        say "The database is the restored pre-update copy."
    else
        say "The database was not touched: no migration ran during the update, so the previous code"
        say "runs on it as it is."
    fi
}

# The schema moved (or could not be read), so the previous code may not run on
# the database as it now is. Rolling the code back alone would hand the old
# release a schema it does not know — refuse, name the backup, print the steps.
# The database is NEVER restored automatically: a restore discards every write
# made since the backup, and that is the operator's decision.
refuse_code_only_rollback() {
    local before="$1" now="$2" dcmd
    dcmd="docker compose${COMPOSE_FILES[*]:+ ${COMPOSE_FILES[*]}}"
    {
        printf '❌ Refusing to roll back the code alone.\n'
        if [ -z "$before" ]; then
            printf '   No schema position was recorded before the update, so there is no way to know\n'
            printf '   whether a migration ran.\n'
        elif [ -z "$now" ]; then
            printf '   The schema position cannot be read now (is trinity-backend running?), so there is\n'
            printf '   no way to know whether a migration ran (recorded before: %s).\n' "$before"
        else
            printf '   A migration ran during the update: schema position %s -> %s.\n' "$before" "$now"
        fi
        printf '   The previous release cannot be assumed to run on the newer schema. To roll back\n'
        printf '   completely, restore the database first. THIS DISCARDS EVERY WRITE MADE SINCE THE BACKUP.\n\n'
        if [ -n "${BACKUP_PATH:-}" ]; then
            printf '   Pre-update backup (inside the backend container): %s\n\n' "$BACKUP_PATH"
        else
            printf '   No pre-update backup was taken (--skip-backup); use your own most recent copy as <BACKUP>.\n\n'
        fi
        local b="${BACKUP_PATH:-<BACKUP>}"
        printf '   1. Stop the writers:\n'
        printf '        %s stop backend scheduler\n' "$dcmd"
        printf '   2. Restore the copy (in a one-off backend container with the same /data mount):\n'
        case "$b" in
            *.dump)
                # Empty the schema first: --clean drops only what the dump holds, so tables from
                # the newer migrations would survive while alembic_version goes back, and the next
                # update's CREATE TABLE crash-loops the backend. Enterprise tables live here too.
                printf '        %s run --rm --no-deps --entrypoint sh backend -c \\\n' "$dcmd"
                printf "          'U=\"\$(echo \"\$DATABASE_URL\" | sed -E \"s#^postgresql[+][a-z0-9]+://#postgresql://#\")\" && psql -X -v ON_ERROR_STOP=1 -d \"\$U\" -c \"DROP SCHEMA public CASCADE; CREATE SCHEMA public;\" && pg_restore --no-owner --dbname \"\$U\" %s'\n" "$b"
                printf '      (keep your own pg_dump of the current database first if you may need it)\n'
                ;;
            *)
                printf '      (the current database is kept beside it as /data/trinity.db.before-rollback)\n'
                printf '        %s run --rm --no-deps --entrypoint sh backend -c \\\n' "$dcmd"
                # The WAL can hold writes the main file does not: it goes into the safety copy
                # under the name SQLite looks for beside it, and only then is it deleted.
                printf "          'cp /data/trinity.db /data/trinity.db.before-rollback && for s in -wal -shm; do [ ! -f /data/trinity.db\$s ] || cp /data/trinity.db\$s /data/trinity.db.before-rollback\$s || exit 1; done && rm -f /data/trinity.db-wal /data/trinity.db-shm /data/trinity.db-journal && cp %s /data/trinity.db'\n" "$b"
                ;;
        esac
        printf '   3. Roll the code back and start it:\n'
        printf '        ./scripts/deploy/update.sh --rollback --db-restored\n'
    } >&2
    exit 1
}

usage() {
    sed -n '3,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

Options:
  --to <ref>              target release tag (default: newest v* tag that is not a pre-release),
                          or a branch / commit
  --allow-downgrade       allow a target older than this checkout (refused otherwise)
  --dir <path>            the checkout to update, when this copy runs from outside it
  --check                 run the preflight and stop; changes nothing
  --verify                verify the published URL only
  --rollback              return to the version recorded before the last update
  --url <url>             published URL to verify (default: FRONTEND_URL in .env,
                          else http://localhost:$FRONTEND_PORT)
  --shape <shape>         override detection: dev | prod | prod-enterprise | hosted
  --accept-port-change    proceed although a published binding moves or disappears
  --skip-backup           do not take the pre-update database copy
  --skip-base-image       never rebuild the agent base image
  --rebuild-base          always rebuild the agent base image (source shapes)
  --db-restored           with --rollback: the database was restored to the pre-update copy,
                          so roll the code back without the schema check
  -y, --yes               never prompt (a failed update prints --rollback instead of offering it)

Environment: TRINITY_UPDATE_MIN_FREE_GB (5), TRINITY_UPDATE_HEALTH_TIMEOUT (300 s),
TRINITY_UPDATE_PUBLISHED_TIMEOUT (120 s), TRINITY_UPDATE_STATE_DIR (~/.trinity/update).
EOF
}

main() {
    set -euo pipefail
    OPT_TO="" OPT_URL="" OPT_SHAPE="" OPT_CHECK=0 OPT_VERIFY=0 OPT_ROLLBACK=0
    OPT_SKIP_BACKUP=0 OPT_SKIP_BASE=0 OPT_REBUILD_BASE=0 OPT_ACCEPT_PORTS=0 OPT_YES=0
    OPT_DB_RESTORED=0 OPT_ALLOW_DOWNGRADE=0 OPT_DIR=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --to) OPT_TO="${2:?--to needs a ref}"; shift ;;
            --url) OPT_URL="${2:?--url needs a URL}"; shift ;;
            --shape) OPT_SHAPE="${2:?--shape needs a value}"; shift ;;
            --dir) OPT_DIR="${2:?--dir needs a path}"; shift ;;
            --allow-downgrade) OPT_ALLOW_DOWNGRADE=1 ;;
            --check) OPT_CHECK=1 ;;
            --verify) OPT_VERIFY=1 ;;
            --rollback) OPT_ROLLBACK=1 ;;
            --skip-backup) OPT_SKIP_BACKUP=1 ;;
            --skip-base-image) OPT_SKIP_BASE=1 ;;
            --rebuild-base) OPT_REBUILD_BASE=1 ;;
            --db-restored) OPT_DB_RESTORED=1 ;;
            --accept-port-change) OPT_ACCEPT_PORTS=1 ;;
            -y|--yes) OPT_YES=1 ;;
            -h|--help) usage; exit 0 ;;
            *) usage >&2; die "unknown option: $1" ;;
        esac
        shift
    done
    HEALTH_TIMEOUT="${TRINITY_UPDATE_HEALTH_TIMEOUT:-300}"
    PUBLISHED_TIMEOUT="${TRINITY_UPDATE_PUBLISHED_TIMEOUT:-120}"
    if [ -n "$OPT_DIR" ]; then
        ROOT="$(cd "$OPT_DIR" 2>/dev/null && pwd)" || die "--dir: no such directory: $OPT_DIR"
        if [ ! -f "$ROOT/scripts/deploy/start.sh" ] || ! git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
            die "--dir: $ROOT is not a Trinity checkout."
        fi
    fi
    WORK="$(mktemp -d)"
    trap 'rm -rf "$WORK"' EXIT
    cd "$ROOT"
    load_start_sh_helpers

    if [ "$OPT_VERIFY" = 1 ]; then
        verify_published
    elif [ "$OPT_ROLLBACK" = 1 ]; then
        do_rollback
    else
        do_update
    fi
}

# Sourced (tests) → functions only; executed → run.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
