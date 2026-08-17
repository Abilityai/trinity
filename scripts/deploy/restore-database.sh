#!/bin/bash
# Restore Trinity SQLite database to GCP
#
# #2216 corrections over the original:
# - stops backend AND scheduler (BOTH write the DB; stopping only the backend
#   left a live writer holding the file during the copy)
# - removes stale -wal/-shm/-journal sidecars beside the target before copying
#   the artifact in (a stale hot journal or WAL beside a restored .db is a
#   corruption hazard; harmless if absent)
#
# Usage: ./scripts/deploy/restore-database.sh <backup_file>
# Requires deploy.config to be set up

set -e

if [ -z "$1" ]; then
    echo "Usage: ./scripts/deploy/restore-database.sh <backup_file>"
    echo "Example: ./scripts/deploy/restore-database.sh ./backups/trinity-backup-20260816.db"
    exit 1
fi

BACKUP_FILE="$1"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CONFIG_FILE="${PROJECT_ROOT}/deploy.config"

# Check for config file
if [ ! -f "${CONFIG_FILE}" ]; then
    echo "Error: deploy.config not found!"
    exit 1
fi

# shellcheck disable=SC1090  # operator-local deploy.config, path not constant
source "${CONFIG_FILE}"

if [ ! -f "${BACKUP_FILE}" ]; then
    echo "Error: Backup file not found: ${BACKUP_FILE}"
    exit 1
fi

echo "====================================="
echo "Trinity Database Restore"
echo "====================================="
echo ""
echo "WARNING: This will overwrite the production database!"
echo "Backup file: ${BACKUP_FILE}"
echo "Target: ${GCP_INSTANCE}"
echo ""
read -p "Are you sure? (yes/no): " confirm

if [ "$confirm" != "yes" ]; then
    echo "Restore cancelled."
    exit 0
fi

echo ""
echo "Step 1: Uploading backup to server..."
gcloud compute scp \
    --zone="${GCP_ZONE}" \
    --project="${GCP_PROJECT}" \
    "${BACKUP_FILE}" "${GCP_INSTANCE}:/tmp/trinity_restore.db"

echo ""
echo "Step 2: Stopping backend and scheduler (both write the DB)..."
gcloud compute ssh "${GCP_INSTANCE}" \
    --zone="${GCP_ZONE}" \
    --project="${GCP_PROJECT}" \
    --command="cd ${REMOTE_DIR} && sudo docker compose -f docker-compose.prod.yml stop backend scheduler"

echo ""
echo "Step 3: Restoring database (removing stale journal sidecars first)..."
gcloud compute ssh "${GCP_INSTANCE}" \
    --zone="${GCP_ZONE}" \
    --project="${GCP_PROJECT}" \
    --command="rm -f ~/trinity-data/trinity.db-wal ~/trinity-data/trinity.db-shm ~/trinity-data/trinity.db-journal && cp /tmp/trinity_restore.db ~/trinity-data/trinity.db && rm /tmp/trinity_restore.db"

echo ""
echo "Step 4: Starting backend and scheduler..."
gcloud compute ssh "${GCP_INSTANCE}" \
    --zone="${GCP_ZONE}" \
    --project="${GCP_PROJECT}" \
    --command="cd ${REMOTE_DIR} && sudo docker compose -f docker-compose.prod.yml start backend scheduler"

echo ""
echo "Step 5: Waiting for backend..."
sleep 10

echo ""
echo "====================================="
echo "Restore Complete!"
echo "====================================="
