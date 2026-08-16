#!/bin/bash
# Pull the newest Trinity database backup ARTIFACT from a GCP instance.
#
# NOTE (#2216): the platform takes its own backups now. The in-process job
# (src/backend/services/db_backup_service.py) produces nightly, verified,
# retention-pruned artifacts under ~/trinity-data/backups/ on the instance —
# that job is the source of truth. This script is only the workstation-side
# GCP pull for the original deploy topology: it downloads the newest artifact,
# it does NOT create backups. (The previous version did a naive live `cp` of
# trinity.db — a torn-copy hazard; that step is gone.)
#
# Usage: ./scripts/deploy/backup-database.sh [backup_dir]
# Requires deploy.config to be set up

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CONFIG_FILE="${PROJECT_ROOT}/deploy.config"

# Check for config file
if [ ! -f "${CONFIG_FILE}" ]; then
    echo "Error: deploy.config not found!"
    echo "Run: cp deploy.config.example deploy.config"
    exit 1
fi

# shellcheck disable=SC1090  # operator-local deploy.config, path not constant
source "${CONFIG_FILE}"

BACKUP_DIR="${1:-${PROJECT_ROOT}/backups}"
# Tilde deliberately quoted (shellcheck SC2088): it must expand on the REMOTE
# host inside the gcloud --command string, never on this workstation.
# shellcheck disable=SC2088
REMOTE_BACKUP_DIR="~/trinity-data/backups"

echo "====================================="
echo "Trinity Database Backup Pull"
echo "====================================="
echo ""

# Create local backup directory
mkdir -p "${BACKUP_DIR}"

echo "Step 1: Locating newest backup artifact on remote..."
# Newest artifact produced by the in-process backup job (day-keyed names).
REMOTE_FILE=$(gcloud compute ssh "${GCP_INSTANCE}" \
    --zone="${GCP_ZONE}" \
    --project="${GCP_PROJECT}" \
    --command="ls -1t ${REMOTE_BACKUP_DIR}/trinity-backup-*.db ${REMOTE_BACKUP_DIR}/trinity-backup-*.dump 2>/dev/null | head -1" \
    | tr -d '\r')

if [ -z "${REMOTE_FILE}" ]; then
    echo ""
    echo "Error: no backup artifacts found in ${REMOTE_BACKUP_DIR} on ${GCP_INSTANCE}."
    echo "The in-process backup job (#2216) produces them nightly at 03:30 UTC."
    echo "Check its status: GET /api/settings/retention → backup block, or the"
    echo "backend logs for '[DBBackup]'. If DB_BACKUP_ENABLED=false, re-enable it."
    exit 1
fi

BACKUP_FILE="$(basename "${REMOTE_FILE}")"
echo "   Found: ${REMOTE_FILE}"

echo ""
echo "Step 2: Downloading backup artifact..."
gcloud compute scp \
    --zone="${GCP_ZONE}" \
    --project="${GCP_PROJECT}" \
    "${GCP_INSTANCE}:${REMOTE_FILE}" "${BACKUP_DIR}/${BACKUP_FILE}"

echo ""
echo "Step 3: Verifying backup..."
case "${BACKUP_FILE}" in
    *.db)
        if command -v sqlite3 &> /dev/null; then
            sqlite3 "${BACKUP_DIR}/${BACKUP_FILE}" "PRAGMA quick_check;" \
                "SELECT COUNT(*) || ' users' FROM users;"
        else
            echo "   (sqlite3 not installed locally, skipping verification —"
            echo "    the artifact was already verified on the instance at creation)"
        fi
        ;;
    *.dump)
        # pg_dump -Fc custom-format archive: check the magic bytes.
        if head -c 5 "${BACKUP_DIR}/${BACKUP_FILE}" | grep -q "PGDMP"; then
            echo "   PGDMP magic OK"
        else
            echo "Error: ${BACKUP_FILE} is not a pg_dump custom-format archive"
            exit 1
        fi
        ;;
esac

echo ""
echo "====================================="
echo "Backup Pull Complete!"
echo "====================================="
echo ""
echo "Backup saved to: ${BACKUP_DIR}/${BACKUP_FILE}"
echo "Size: $(ls -lh "${BACKUP_DIR}/${BACKUP_FILE}" | awk '{print $5}')"
echo ""
echo "To restore: ./scripts/deploy/restore-database.sh ${BACKUP_DIR}/${BACKUP_FILE}"
