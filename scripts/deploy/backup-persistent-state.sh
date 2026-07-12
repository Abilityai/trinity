#!/usr/bin/env bash
#
# Back up the persistent state of a running Trinity instance.
#
# This script is intentionally conservative:
#   - it discovers the running compose project by labels,
#   - it takes a logical PostgreSQL dump when a bundled postgres service exists,
#   - it archives backend /data and agent workspace volumes,
#   - it never stops or removes containers or volumes.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

PROJECT_NAME="${COMPOSE_PROJECT_NAME:-trinity}"
OUTPUT_DIR="${PROJECT_ROOT}/backups/persistent-state"
ENV_FILE="${PROJECT_ROOT}/.env"
INCLUDE_AGENT_WORKSPACES=1
INCLUDE_BACKEND_DATA=1
INCLUDE_ENV=1
ALLOW_EXTERNAL_DB_WITHOUT_DUMP=0

usage() {
  cat <<'EOF'
Usage: scripts/deploy/backup-persistent-state.sh [options]

Options:
  --project-name NAME              Docker Compose project name (default: trinity)
  --output-dir DIR                 Backup parent directory (default: ./backups/persistent-state)
  --env-file FILE                  Env file to copy into the bundle (default: ./.env)
  --skip-agent-workspaces          Do not archive agent-*-workspace Docker volumes
  --skip-backend-data              Do not archive the backend /data mount
  --skip-env                       Do not copy .env into the backup bundle
  --allow-external-db-without-dump Do not fail when DATABASE_URL points at an external PostgreSQL DB
  -h, --help                       Show this help

The backup directory contains secrets if .env is copied. It is chmod 700 and
must remain outside git.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-name)
      PROJECT_NAME="${2:?--project-name requires a value}"
      shift 2
      ;;
    --output-dir)
      OUTPUT_DIR="${2:?--output-dir requires a value}"
      shift 2
      ;;
    --env-file)
      ENV_FILE="${2:?--env-file requires a value}"
      shift 2
      ;;
    --skip-agent-workspaces)
      INCLUDE_AGENT_WORKSPACES=0
      shift
      ;;
    --skip-backend-data)
      INCLUDE_BACKEND_DATA=0
      shift
      ;;
    --skip-env)
      INCLUDE_ENV=0
      shift
      ;;
    --allow-external-db-without-dump)
      ALLOW_EXTERNAL_DB_WITHOUT_DUMP=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

log() {
  printf '[backup] %s\n' "$*"
}

warn() {
  printf '[backup] WARN: %s\n' "$*" >&2
}

die() {
  printf '[backup] ERROR: %s\n' "$*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "$1 is required"
}

service_container() {
  docker ps \
    --filter "label=com.docker.compose.project=${PROJECT_NAME}" \
    --filter "label=com.docker.compose.service=$1" \
    --format '{{.Names}}' \
    | head -n 1
}

archive_volume() {
  local volume="$1"
  local archive_dir="$2"
  local archive_name="$3"

  mkdir -p "${archive_dir}"
  docker run --rm \
    -v "${volume}:/source:ro" \
    -v "${archive_dir}:/backup" \
    alpine:3.20 \
    sh -c 'cd /source && tar -czf "/backup/$1" .' sh "${archive_name}"
}

require_cmd docker
docker info >/dev/null 2>&1 || die "Docker is not running"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="${OUTPUT_DIR%/}/${TIMESTAMP}-${PROJECT_NAME}"
mkdir -p "${RUN_DIR}"
chmod 700 "${RUN_DIR}"

MANIFEST="${RUN_DIR}/manifest.txt"
BACKEND_CONTAINER="$(service_container backend || true)"
POSTGRES_CONTAINER="$(service_container postgres || true)"
REDIS_CONTAINER="$(service_container redis || true)"

{
  echo "trinity_persistent_state_backup=1"
  echo "timestamp_utc=${TIMESTAMP}"
  echo "compose_project=${PROJECT_NAME}"
  echo "git_head=$(git -C "${PROJECT_ROOT}" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  echo "backup_host=$(hostname 2>/dev/null || echo unknown)"
  echo "backend_container=${BACKEND_CONTAINER:-missing}"
  echo "postgres_container=${POSTGRES_CONTAINER:-missing}"
  echo "redis_container=${REDIS_CONTAINER:-missing}"
  echo "env_file=${ENV_FILE}"
  echo "contains_secrets=$([[ ${INCLUDE_ENV} -eq 1 && -f "${ENV_FILE}" ]] && echo yes || echo no)"
  echo
  echo "docker_volumes:"
  docker volume ls --format '{{.Name}}' | sort | sed 's/^/  - /'
  echo
  echo "compose_containers:"
  docker ps -a \
    --filter "label=com.docker.compose.project=${PROJECT_NAME}" \
    --format '  - {{.Names}} {{.Image}} {{.Status}}'
} > "${MANIFEST}"

log "Writing backup bundle: ${RUN_DIR}"

if [[ ${INCLUDE_ENV} -eq 1 ]]; then
  if [[ -f "${ENV_FILE}" ]]; then
    cp "${ENV_FILE}" "${RUN_DIR}/env.backup"
    chmod 600 "${RUN_DIR}/env.backup"
    log "Copied ${ENV_FILE} to env.backup"
  else
    warn "Env file not found at ${ENV_FILE}; skipping env backup"
  fi
fi

DATABASE_URL=""
if [[ -n "${BACKEND_CONTAINER}" ]]; then
  DATABASE_URL="$(docker exec "${BACKEND_CONTAINER}" sh -lc 'printf "%s" "${DATABASE_URL:-}"' 2>/dev/null || true)"
fi

if [[ -n "${POSTGRES_CONTAINER}" ]]; then
  log "Creating PostgreSQL custom-format dump from ${POSTGRES_CONTAINER}"
  docker exec "${POSTGRES_CONTAINER}" sh -lc \
    'export PGPASSWORD="${POSTGRES_PASSWORD:-}"; pg_dump -U "${POSTGRES_USER:-trinity}" -d "${POSTGRES_DB:-trinity}" -Fc' \
    > "${RUN_DIR}/postgres.dump"

  if docker run --rm -v "${RUN_DIR}:/backup:ro" postgres:16-alpine pg_restore -l /backup/postgres.dump >/dev/null 2>&1; then
    log "Verified postgres.dump with pg_restore -l"
    echo "postgres_dump_verified=yes" >> "${MANIFEST}"
  else
    warn "Could not verify postgres.dump with pg_restore; dump file was still written"
    echo "postgres_dump_verified=no" >> "${MANIFEST}"
  fi
elif [[ "${DATABASE_URL}" == postgresql://* || "${DATABASE_URL}" == postgres://* ]]; then
  echo "external_postgres_detected=yes" >> "${MANIFEST}"
  if [[ ${ALLOW_EXTERNAL_DB_WITHOUT_DUMP} -eq 0 ]]; then
    die "Backend uses PostgreSQL but no compose postgres service was found. Take a managed DB snapshot or rerun with --allow-external-db-without-dump after doing that."
  fi
  warn "External PostgreSQL detected; this bundle does not include a database dump"
else
  if [[ -n "${BACKEND_CONTAINER}" ]]; then
    log "No PostgreSQL service detected; archiving SQLite files from backend /data if present"
    docker run --rm \
      --volumes-from "${BACKEND_CONTAINER}:ro" \
      -v "${RUN_DIR}:/backup" \
      alpine:3.20 \
      sh -c 'cd /data && tar -czf /backup/sqlite-data.tgz trinity.db trinity.db-wal trinity.db-shm 2>/dev/null || true'
  else
    warn "No backend container found; skipped SQLite/backend database backup"
  fi
fi

if [[ ${INCLUDE_BACKEND_DATA} -eq 1 ]]; then
  if [[ -n "${BACKEND_CONTAINER}" ]]; then
    log "Archiving backend /data mount"
    docker run --rm \
      --volumes-from "${BACKEND_CONTAINER}:ro" \
      -v "${RUN_DIR}:/backup" \
      alpine:3.20 \
      sh -c 'cd /data && tar -czf /backup/backend-data.tgz .'
  else
    warn "No backend container found; skipped backend /data archive"
  fi
fi

if [[ ${INCLUDE_AGENT_WORKSPACES} -eq 1 ]]; then
  log "Archiving agent workspace volumes"
  AGENT_ARCHIVE_DIR="${RUN_DIR}/agent-workspaces"
  agent_count=0
  while IFS= read -r volume; do
    [[ -n "${volume}" ]] || continue
    agent_count=$((agent_count + 1))
    archive_volume "${volume}" "${AGENT_ARCHIVE_DIR}" "${volume}.tgz"
  done < <(docker volume ls --format '{{.Name}}' | grep -E '^agent-.+-workspace$' | sort || true)
  echo "agent_workspace_archives=${agent_count}" >> "${MANIFEST}"
  log "Archived ${agent_count} agent workspace volume(s)"
fi

if [[ -n "${REDIS_CONTAINER}" ]]; then
  echo "redis_container_present=yes" >> "${MANIFEST}"
fi

du -sh "${RUN_DIR}" | awk '{print "backup_size=" $1}' >> "${MANIFEST}"

log "Backup complete"
log "Manifest: ${MANIFEST}"
log "Bundle size: $(du -sh "${RUN_DIR}" | awk '{print $1}')"
