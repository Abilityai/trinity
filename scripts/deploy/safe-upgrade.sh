#!/usr/bin/env bash
#
# Safe upgrade wrapper for a running Trinity instance.
#
# The guardrails are the point:
#   - keep one stable compose project name,
#   - take a persistent-state backup before changes,
#   - rebuild/recreate only platform services,
#   - never run docker compose down -v or remove data volumes,
#   - verify the running services after the upgrade.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

PROJECT_NAME="${COMPOSE_PROJECT_NAME:-trinity}"
BACKUP_DIR="${PROJECT_ROOT}/backups/persistent-state"
SKIP_BACKUP=0
ALLOW_FRESH=0
BUILD=1
NO_CACHE=0
DRY_RUN=0
ENV_FILE=""
COMPOSE_FILES=()
TARGET_SERVICES=()

usage() {
  cat <<'EOF'
Usage: scripts/deploy/safe-upgrade.sh [options] [-- service ...]

Options:
  --project-name NAME       Docker Compose project name (default: trinity)
  -f, --compose-file FILE   Compose file to use. Repeatable.
  --env-file FILE           Compose env file
  --backup-dir DIR          Persistent-state backup parent directory
  --skip-backup             Do not run backup-persistent-state.sh first
  --allow-fresh             Allow no existing containers/volumes (first install)
  --no-build                Skip docker compose build
  --no-cache                Build platform services with --no-cache
  --dry-run                 Print planned commands without running them
  -h, --help                Show this help

By default the script targets the platform services that exist in the compose
config: redis, vector, logs-init, postgres, backend, scheduler, frontend,
mcp-server, and otel-collector. Agent containers are not removed; their
workspace volumes are backed up by the pre-upgrade backup step.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-name)
      PROJECT_NAME="${2:?--project-name requires a value}"
      shift 2
      ;;
    -f|--compose-file)
      COMPOSE_FILES+=("${2:?--compose-file requires a value}")
      shift 2
      ;;
    --env-file)
      ENV_FILE="${2:?--env-file requires a value}"
      shift 2
      ;;
    --backup-dir)
      BACKUP_DIR="${2:?--backup-dir requires a value}"
      shift 2
      ;;
    --skip-backup)
      SKIP_BACKUP=1
      shift
      ;;
    --allow-fresh)
      ALLOW_FRESH=1
      shift
      ;;
    --no-build)
      BUILD=0
      shift
      ;;
    --no-cache)
      NO_CACHE=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      while [[ $# -gt 0 ]]; do
        TARGET_SERVICES+=("$1")
        shift
      done
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

log() {
  printf '[safe-upgrade] %s\n' "$*"
}

die() {
  printf '[safe-upgrade] ERROR: %s\n' "$*" >&2
  exit 1
}

run() {
  if [[ ${DRY_RUN} -eq 1 ]]; then
    printf '[safe-upgrade] DRY RUN:'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

service_container() {
  docker ps \
    --filter "label=com.docker.compose.project=${PROJECT_NAME}" \
    --filter "label=com.docker.compose.service=$1" \
    --format '{{.Names}}' \
    | head -n 1
}

if git -C "${PROJECT_ROOT}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  export GIT_COMMIT="${GIT_COMMIT:-$(git -C "${PROJECT_ROOT}" rev-parse HEAD)}"
  export GIT_COMMIT_SUBJECT="${GIT_COMMIT_SUBJECT:-$(git -C "${PROJECT_ROOT}" log -1 --pretty=%s)}"
  export GIT_COMMIT_TIMESTAMP="${GIT_COMMIT_TIMESTAMP:-$(git -C "${PROJECT_ROOT}" log -1 --pretty=%cI)}"
  export GIT_BRANCH="${GIT_BRANCH:-$(git -C "${PROJECT_ROOT}" symbolic-ref --short -q HEAD || git -C "${PROJECT_ROOT}" name-rev --name-only --no-undefined HEAD 2>/dev/null || git -C "${PROJECT_ROOT}" rev-parse --short HEAD)}"
fi
export BUILD_DATE="${BUILD_DATE:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"

if [[ ${#COMPOSE_FILES[@]} -eq 0 ]]; then
  if [[ -f "${PROJECT_ROOT}/docker-compose.prod.yml" ]]; then
    COMPOSE_FILES=("${PROJECT_ROOT}/docker-compose.prod.yml")
  else
    COMPOSE_FILES=("${PROJECT_ROOT}/docker-compose.yml")
  fi
fi

COMPOSE_ARGS=(-p "${PROJECT_NAME}")
if [[ -n "${ENV_FILE}" ]]; then
  COMPOSE_ARGS+=(--env-file "${ENV_FILE}")
fi
for compose_file in "${COMPOSE_FILES[@]}"; do
  COMPOSE_ARGS+=(-f "${compose_file}")
done

docker info >/dev/null 2>&1 || die "Docker is not running"

existing_containers="$(docker ps -a --filter "label=com.docker.compose.project=${PROJECT_NAME}" --format '{{.Names}}' || true)"
existing_volumes="$(docker volume ls --format '{{.Name}}' | grep -E "^${PROJECT_NAME}_" || true)"
if [[ -z "${existing_containers}${existing_volumes}" && ${ALLOW_FRESH} -eq 0 ]]; then
  die "No existing containers or volumes for compose project ${PROJECT_NAME}. Use --allow-fresh only for first install."
fi

COMPOSE_SERVICES="$(docker compose "${COMPOSE_ARGS[@]}" config --services)"

service_exists() {
  printf '%s\n' "${COMPOSE_SERVICES}" | grep -qx "$1"
}

if [[ ${#TARGET_SERVICES[@]} -eq 0 ]]; then
  for candidate in redis vector logs-init postgres backend scheduler frontend mcp-server otel-collector; do
    if service_exists "${candidate}"; then
      TARGET_SERVICES+=("${candidate}")
    fi
  done
fi

if [[ ${#TARGET_SERVICES[@]} -eq 0 ]]; then
  die "No target services found in compose config"
fi

log "Compose project: ${PROJECT_NAME}"
log "Compose files: ${COMPOSE_FILES[*]}"
log "Target services: ${TARGET_SERVICES[*]}"

if [[ ${SKIP_BACKUP} -eq 0 ]]; then
  BACKUP_CMD=(
    "${SCRIPT_DIR}/backup-persistent-state.sh"
    --project-name "${PROJECT_NAME}"
    --output-dir "${BACKUP_DIR}"
  )
  if [[ -n "${ENV_FILE}" ]]; then
    BACKUP_CMD+=(--env-file "${ENV_FILE}")
  fi
  run "${BACKUP_CMD[@]}"
else
  log "Skipping backup because --skip-backup was set"
fi

if [[ ${BUILD} -eq 1 ]]; then
  BUILD_SERVICES=()
  for candidate in backend frontend mcp-server scheduler; do
    if service_exists "${candidate}"; then
      BUILD_SERVICES+=("${candidate}")
    fi
  done

  if [[ ${#BUILD_SERVICES[@]} -gt 0 ]]; then
    if [[ ${NO_CACHE} -eq 1 ]]; then
      run docker compose "${COMPOSE_ARGS[@]}" build --no-cache "${BUILD_SERVICES[@]}"
    else
      run docker compose "${COMPOSE_ARGS[@]}" build "${BUILD_SERVICES[@]}"
    fi
  fi
fi

run docker compose "${COMPOSE_ARGS[@]}" up -d "${TARGET_SERVICES[@]}"

if [[ ${DRY_RUN} -eq 1 ]]; then
  log "Dry run complete"
  exit 0
fi

log "Waiting for backend health"
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
curl -fsS http://127.0.0.1:8000/health >/dev/null || die "Backend health check failed"

backend_container="$(service_container backend || true)"
if [[ -n "${backend_container}" ]]; then
  backend_status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "${backend_container}")"
  log "Backend container health: ${backend_status}"
fi

if curl -fsS http://127.0.0.1:8000/api/version >/tmp/trinity-safe-upgrade-version.json 2>/dev/null; then
  log "Running version: $(tr -d '\n' </tmp/trinity-safe-upgrade-version.json)"
  rm -f /tmp/trinity-safe-upgrade-version.json
else
  log "Version endpoint was not reachable; backend health still passed"
fi

log "Safe upgrade complete"
