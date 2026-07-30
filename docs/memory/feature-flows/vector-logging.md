# Feature: Vector Logging

## Overview

Centralized log aggregation system that captures all Docker container stdout/stderr via Vector, routing platform service logs and agent logs to separate daily-rotated JSON files for queryability.

## User Story

As a platform operator, I want all container logs automatically captured and organized so that I can debug issues, monitor agent activity, and audit platform operations without manual log collection.

## Entry Points

- **Infrastructure**: Automatic - Vector captures all Docker container logs via Docker socket
- **Query**: CLI via `docker exec trinity-vector` with `jq`/`grep`
- **Health**: `GET http://localhost:8686/health` (Vector API)
- **Admin API**: `GET /api/logs/stats`, `GET /api/logs/retention`, `POST /api/logs/archive`

## Architecture

```
Container stdout/stderr
        |
        v
   Docker Engine
        |
   Docker Socket (/var/run/docker.sock)
        |
        v
   Vector (timberio/vector:0.43.1-alpine)
        |
   VRL Transform (enrich, parse JSON, extract level)
        |
   +-----------+-----------+
   |                       |
   v                       v
route_platform         route_agents
(is_platform=true)     (is_agent=true)
   |                       |
   v                       v
/data/logs/platform-YYYY-MM-DD.json   /data/logs/agents-YYYY-MM-DD.json
        |                                       |
        +---------------+-----------------------+
                        |
                        v
              LogArchiveService (nightly)
                        |
                        v
              /data/archives/*.json.gz
```

## Data Flow

### 1. Log Capture

**Source**: `config/vector.yaml:18-23`

```yaml
sources:
  docker_logs:
    type: docker_logs
    # Automatically discovers all containers via Docker socket
    # Streams logs in real-time from container start events
    # NOTE: Does NOT read historical logs - only new logs after Vector starts
```

Vector connects to Docker socket and streams all container logs in real-time.

### 2. Enrichment Transform

**Source**: `config/vector.yaml:26-56`

VRL (Vector Remap Language) transform enriches each log entry:

```vrl
# Add useful metadata
.service = .container_name
.is_agent = starts_with(string!(.container_name), "agent-")
.is_platform = !.is_agent && starts_with(string!(.container_name), "trinity-")

# Parse JSON logs if present (Python/Node often log JSON)
if is_string(.message) {
  parsed, err = parse_json(.message)
  if err == null {
    .parsed = parsed
  }
}

# Extract log level if present
if exists(.parsed.level) {
  .level = .parsed.level
} else if exists(.parsed.levelname) {
  .level = .parsed.levelname
} else if match(string!(.message), r'(?i)\b(error|err)\b') {
  .level = "error"
} else if match(string!(.message), r'(?i)\b(warn|warning)\b') {
  .level = "warning"
} else {
  .level = "info"
}
```

### 3. Routing

**Source**: `config/vector.yaml:58-74`

Two filter transforms route logs based on container naming:

| Filter | Condition | Output |
|--------|-----------|--------|
| `route_platform` | `.is_platform == true` | Platform service logs |
| `route_agents` | `.is_agent == true` | Agent container logs |

**Container Naming Convention**:
- Platform: `trinity-*` (backend, frontend, redis, mcp-server, vector, otel-collector)
- Agents: `agent-*` (all Trinity-managed agent containers)

### 4. File Sinks (Daily Rotation)

**Source**: `config/vector.yaml:76-96`

| Sink | Input | Path Pattern | Format |
|------|-------|--------------|--------|
| `platform_logs` | `route_platform` | `/data/logs/platform-%Y-%m-%d.json` | JSON |
| `agent_logs` | `route_agents` | `/data/logs/agents-%Y-%m-%d.json` | JSON |

Files rotate automatically at midnight UTC using Vector's date interpolation.

## Backend Layer

### Structured Logging Configuration

**File**: `src/backend/logging_config.py`

The backend uses a custom JSON formatter for Vector-compatible output:

```python
class JsonFormatter(logging.Formatter):
    """Format logs as JSON for easy parsing by Vector."""

    def format(self, record):
        log_entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add extra fields if present
        if hasattr(record, "event_type"):
            log_entry["event_type"] = record.event_type
        if hasattr(record, "agent_name"):
            log_entry["agent_name"] = record.agent_name
        if hasattr(record, "user_id"):
            log_entry["user_id"] = record.user_id
        if hasattr(record, "user_email"):
            log_entry["user_email"] = record.user_email
        if hasattr(record, "action"):
            log_entry["action"] = record.action
        if hasattr(record, "result"):
            log_entry["result"] = record.result
        if hasattr(record, "details"):
            log_entry["details"] = record.details

        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry)
```

### Logging Setup

**File**: `src/backend/main.py:164`

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Set up structured JSON logging (captured by Vector)
    setup_logging()
```

### Logger Usage

**File**: `src/backend/logging_config.py:70-72`

```python
def get_logger(name: str) -> logging.Logger:
    """Get a Trinity logger instance."""
    return logging.getLogger(f"trinity.{name}")
```

**Example usage in services**:

```python
from logging_config import get_logger

logger = get_logger("agents")

logger.info(
    "Agent created",
    extra={
        "event_type": "agent_management",
        "action": "create",
        "agent_name": name,
        "user_id": current_user.id
    }
)
```

## Docker Compose Configuration

**File**: `docker-compose.yml:184-205`

```yaml
vector:
  image: timberio/vector:0.43.1-alpine
  container_name: trinity-vector
  restart: unless-stopped
  volumes:
    - /var/run/docker.sock:/var/run/docker.sock:ro
    - ./config/vector.yaml:/etc/vector/vector.yaml:ro
    - trinity-logs:/data/logs
  ports:
    - "8686:8686"  # Vector API (health checks)
  networks:
    - trinity-network
  security_opt:
    - no-new-privileges:true
  cap_drop:
    - ALL
  healthcheck:
    test: ["CMD", "wget", "-q", "-O", "-", "http://127.0.0.1:8686/health"]
    interval: 30s
    timeout: 10s
    retries: 3
    start_period: 10s  # Give Vector time to start before health checks
```

**Backend Dependencies**: `docker-compose.yml:55-56`

```yaml
depends_on:
  vector:
    condition: service_healthy  # Ensure Vector is ready before backend starts agents
```

**Volumes**: `docker-compose.yml:234-235`

```yaml
volumes:
  trinity-logs:  # Vector log storage (active daily files)
  trinity-archives:  # Compressed log archives
```

**Backend Volume Mounts**: `docker-compose.yml:50-51`

```yaml
volumes:
  - trinity-logs:/data/logs:ro  # Read-only access to Vector logs
  - trinity-archives:/data/archives  # Log archives storage
```

## Log Structure

### Platform Log Entry

```json
{
  "timestamp": "2026-01-23T12:00:00.000Z",
  "container_name": "trinity-backend",
  "container_id": "abc123...",
  "message": "The log message",
  "level": "info",
  "is_agent": false,
  "is_platform": true,
  "service": "trinity-backend"
}
```

### Parsed JSON Log Entry

When the backend logs structured JSON, Vector parses it:

```json
{
  "timestamp": "2026-01-23T12:00:00.000Z",
  "container_name": "trinity-backend",
  "message": "{\"level\": \"INFO\", ...}",
  "parsed": {
    "level": "INFO",
    "logger": "trinity.agents",
    "message": "Agent created",
    "agent_name": "test-agent",
    "user_id": "user_123"
  },
  "level": "INFO",
  "is_platform": true,
  "service": "trinity-backend"
}
```

## Query Interface

Full documentation: `docs/QUERYING_LOGS.md`

### Accessing Logs

```bash
# Today's date for filename
TODAY=$(date +%Y-%m-%d)

# Via Vector container (today's logs)
docker exec trinity-vector cat /data/logs/platform-$TODAY.json | tail -100

# Via temporary container
docker run --rm -v trinity-logs:/logs alpine cat /logs/platform-$TODAY.json | tail -100

# List all log files
docker exec trinity-vector ls -la /data/logs/
```

### Common Queries

```bash
TODAY=$(date +%Y-%m-%d)

# Find errors in today's platform logs
docker exec trinity-vector sh -c "cat /data/logs/platform-$TODAY.json | tail -100" | jq 'select(.level == "error")'

# Filter by agent (today's logs)
docker exec trinity-vector sh -c "grep 'agent-ruby' /data/logs/agents-$TODAY.json | tail -50" | jq -r '.message'

# Live tail platform logs (today's file)
docker exec trinity-vector sh -c "tail -f /data/logs/platform-$TODAY.json" | jq .

# Search across all platform log files
docker exec trinity-vector sh -c "cat /data/logs/platform-*.json" | grep 'trinity-backend' | tail -50 | jq .

# Authentication events (all files)
docker exec trinity-vector sh -c "cat /data/logs/platform-*.json" | grep -i 'login\|auth' | tail -50 | jq .

# Logs from a specific date
docker exec trinity-vector cat /data/logs/platform-2026-01-20.json | tail -100 | jq .
```

### Health Check

```bash
# Vector health
curl http://localhost:8686/health

# Vector topology
curl http://localhost:8686/api/v1/topology
```

## Log Retention & Archival

### Overview

Automated log retention system that:
1. Rotates logs daily to date-stamped files (handled by Vector)
2. Archives old logs with compression to local storage (handled by LogArchiveService)
3. Deletes originals after successful archive

### Configuration

Environment variables in `docker-compose.yml`:

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_RETENTION_DAYS` | `90` | Days to keep logs before archival |
| `LOG_ARCHIVE_ENABLED` | `true` | Enable automated archival |
| `LOG_CLEANUP_HOUR` | `3` | Hour (UTC) to run nightly archival |
| `LOG_ARCHIVE_PATH` | `/data/archives` | Local path for archived logs |

### Daily Rotation

Vector writes logs to date-stamped files using path interpolation:
- `platform-2026-01-23.json` (today's platform logs)
- `agents-2026-01-23.json` (today's agent logs)

Files rotate automatically at midnight UTC.

### Automated Archival

**Service**: `src/backend/services/log_archive_service.py`

The backend runs a nightly job (default: 3 AM UTC) using APScheduler:

1. **Finds old files**: Identifies logs older than retention period via filename date parsing
2. **Compresses**: Gzip level 9 (~90% size reduction) to `/tmp/archives/`
3. **Verifies**: SHA256 integrity check (decompresses and compares checksums)
4. **Stores locally**: Moves to archive volume via `LocalArchiveStorage`
5. **Writes metadata**: JSON sidecar file with original size, compression ratio, timestamp
6. **Cleans up**: Deletes original files after successful archive

### Storage Backend

**Service**: `src/backend/services/archive_storage.py`

Abstract `ArchiveStorage` interface with `LocalArchiveStorage` implementation:

```python
class LocalArchiveStorage(ArchiveStorage):
    """Archives stored to /data/archives (Docker volume)."""

    async def store_archive(self, source_path: Path, metadata: Dict) -> Dict
    def list_archives(self, prefix: str = "") -> List[Dict]
    def delete_archive(self, archive_name: str)
```

### API Endpoints

**Router**: `src/backend/routers/logs.py`

Admin-only endpoints for log management:

```bash
# Get log statistics
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/logs/stats

# Get retention configuration
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/logs/retention

# Update retention (runtime only - reschedules APScheduler job)
curl -X PUT -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"retention_days": 30, "archive_enabled": true, "cleanup_hour": 3}' \
  http://localhost:8000/api/logs/retention

# Manually trigger archival
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"retention_days": 90, "delete_after_archive": true}' \
  http://localhost:8000/api/logs/archive

# Check archival service health
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/logs/health
```

### Response Examples

**GET /api/logs/stats**:
```json
{
  "log_files": [
    {"name": "platform-2026-01-23.json", "size": 15728640, "date": "2026-01-23"}
  ],
  "archive_files": [
    {"name": "platform-2025-10-15.json.gz", "size": 1572864}
  ],
  "total_log_size": 157286400,
  "total_log_size_mb": 150.0,
  "total_archive_size": 15728640,
  "total_archive_size_mb": 15.0,
  "oldest_log": "2025-10-25",
  "newest_log": "2026-01-23",
  "log_file_count": 90,
  "archive_file_count": 10
}
```

**GET /api/logs/health**:
```json
{
  "scheduler_running": true,
  "archive_enabled": true,
  "archive_path": "/data/archives",
  "retention_days": 90,
  "cleanup_hour": 3
}
```

### Custom Backup Strategies

Trinity archives logs locally by default, keeping all data within your infrastructure (sovereign deployment). You can implement custom backup strategies using standard Docker volume management:

#### Option 1: Mount NAS/NFS for Archives

Mount a network storage device to the archives volume:

```yaml
# In docker-compose.yml
services:
  backend:
    volumes:
      - type: bind
        source: /mnt/nas/trinity-archives  # Your NAS mount point
        target: /data/archives
```

#### Option 2: rsync to Backup Server

Create a cron job to sync archives to another server:

```bash
# /etc/cron.daily/trinity-archive-sync
#!/bin/bash
rsync -avz --delete \
  /var/lib/docker/volumes/trinity-archives/_data/ \
  backup-server:/backups/trinity-logs/
```

#### Option 3: Docker Volume Backup

Use Docker's built-in volume backup:

```bash
# Backup archives volume to tarball
docker run --rm \
  -v trinity-archives:/data \
  -v $(pwd):/backup \
  alpine tar czf /backup/trinity-archives-$(date +%Y%m%d).tar.gz /data

# Restore from tarball
docker run --rm \
  -v trinity-archives:/data \
  -v $(pwd):/backup \
  alpine tar xzf /backup/trinity-archives-20260105.tar.gz -C /
```

#### Option 4: Cross-Instance Volume Copy

Copy archives between Docker instances:

```bash
# Export from instance A
docker run --rm \
  -v trinity-archives:/source \
  -v $(pwd):/dest \
  alpine sh -c "cd /source && tar czf - ." > trinity-archives.tar.gz

# Import to instance B (transfer trinity-archives.tar.gz via scp/rsync)
docker run --rm \
  -v trinity-archives:/dest \
  -v $(pwd):/source \
  alpine sh -c "cd /dest && tar xzf /source/trinity-archives.tar.gz"
```

### Storage Locations

| Location | Contents | Volume | Purpose |
|----------|----------|--------|---------|
| `/data/logs/` | Active log files | `trinity-logs` | Daily logs (current + retention period) |
| `/data/archives/` | Compressed archives | `trinity-archives` | Local storage of archived logs |
| `/tmp/archives/` | Staging area | tmpfs | Temporary compression before storage |

### Archive File Structure

Each archived log generates two files:
- `platform-2025-10-15.json.gz` - Compressed log data
- `platform-2025-10-15.json.gz.meta` - JSON metadata sidecar

Metadata sidecar example:
```json
{
  "original_size": "157286400",
  "compressed_size": "15728640",
  "archived_at": "2026-01-23T03:00:00.000000",
  "retention_days": "90",
  "original_file": "platform-2025-10-15.json"
}
```

### Disk Space Management

Example with 90-day retention:
- Daily log size: ~150 MB
- 90 days uncompressed: ~13.5 GB
- 90 days compressed: ~1.35 GB (10% of original)
- Active logs stay at ~13.5 GB (stable after 90 days)

## Side Effects

- **File Output**: Logs written to Docker volume `trinity-logs` with daily rotation
- **Compressed Archives**: Old logs archived to Docker volume `trinity-archives`
- **Metadata Sidecars**: JSON metadata files alongside each archive
- **Scheduler**: APScheduler job runs nightly for archival
- **No Database**: Logs are file-based, not persisted to SQLite
- **No WebSocket**: No real-time log streaming to UI (query only)

## Error Handling

| Error Case | Impact | Resolution |
|------------|--------|------------|
| Docker socket unavailable | No log capture | Check Vector container permissions |
| Volume full | Log writes fail | Clear old logs, increase volume size |
| Vector container down | Logs lost during downtime | Restart Vector service |
| Invalid VRL transform | Logs dropped | Check Vector logs for parse errors |
| Pre-existing container | No logs until restart | Restart the container: `docker restart agent-<name>` |

### Known Limitation: Historical Logs

Vector's `docker_logs` source **only captures logs from after it starts watching** a container. This is a known upstream limitation ([GitHub #7358](https://github.com/vectordotdev/vector/issues/7358)).

**Mitigation in Trinity:**
- Backend depends on Vector being healthy before starting (`condition: service_healthy`)
- This ensures all newly created agents have their logs captured from the start

### Interaction with Docker Log Rotation (#1871)

Since #1871 every container's raw Docker log is bounded (`json-file` `max-size`/`max-file`, default `10m` × 3) — platform services via the `x-logging` anchor in both compose files, agent containers via `AGENT_LOG_CONFIG` (see [container-capabilities.md](container-capabilities.md)). Two consequences for this flow, both verified empirically:

- **Vector is unaffected.** Rotation does **not** interrupt log collection. Measured across ~10 forced rotations with zero gaps on all three paths: the `docker_logs` source (production), the `file` source used by the Docker Desktop override (#1432), and the underlying Docker follow endpoint. Vector streams continuously, so it behaves like a live follower, not like post-hoc history.
- **`docker logs` history shortens; the aggregate does not.** Rotation discards the oldest file, so a post-hoc `docker logs <container>` reaches back only as far as the retained set (~4 days for a busy agent). This does **not** shrink the aggregate at `/data/logs`, which is the primary queryable copy and keeps its own `LOG_RETENTION_DAYS`. It also does not widen the limitation above: `docker_logs` already never read historical logs.
- Pre-existing containers (from before Vector started) need to be restarted to begin log capture

## Security Considerations

- Docker socket mounted **read-only** (`/var/run/docker.sock:ro`)
- `no-new-privileges` security option enabled
- All capabilities dropped (`cap_drop: ALL`)
- No sensitive data in log messages (credentials masked at source)
- Log files on Docker volume (not host filesystem by default)

## Troubleshooting

### Logs Not Appearing

1. Check Vector is running:
   ```bash
   docker ps | grep vector
   ```

2. Check Vector logs:
   ```bash
   docker logs trinity-vector --tail 50
   ```

3. Verify Docker socket access:
   ```bash
   docker exec trinity-vector ls -la /var/run/docker.sock
   ```

### Large Log Files

Monitor file sizes:
```bash
docker exec trinity-vector ls -lh /data/logs/
```

With daily rotation, old files are automatically archived. To manually clear today's logs:
```bash
TODAY=$(date +%Y-%m-%d)
docker exec trinity-vector sh -c "echo '' > /data/logs/platform-$TODAY.json"
docker exec trinity-vector sh -c "echo '' > /data/logs/agents-$TODAY.json"
```

### Archival Not Running

1. Check scheduler status:
   ```bash
   curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/logs/health
   ```

2. Verify LOG_ARCHIVE_ENABLED is true:
   ```bash
   docker exec trinity-backend env | grep LOG_
   ```

3. Check backend logs for archival errors:
   ```bash
   docker logs trinity-backend 2>&1 | grep -i archiv
   ```

## Related Flows

- **Upstream**: All container stdout/stderr (backend, frontend, agents)
- **Downstream**: None (terminal sink - query only)
- **Replaced**: `audit-logger` service (fire-and-forget HTTP calls, 173+ call sites removed)
- **Related**: [opentelemetry-integration.md](opentelemetry-integration.md) - OTel metrics (separate from logs)

## Testing

### Prerequisites
- Trinity platform running (`./scripts/deploy/start.sh`)
- Vector container healthy

### Test Steps

1. **Verify Vector Running**
   - Action: `docker ps | grep vector`
   - Expected: `trinity-vector` container running
   - Verify: Container healthy (no restart loops)

2. **Check Log Files Created**
   - Action: `docker exec trinity-vector ls -la /data/logs/`
   - Expected: `platform-YYYY-MM-DD.json` and `agents-YYYY-MM-DD.json` exist
   - Verify: Files have non-zero size

3. **Verify Platform Log Capture**
   - Action: Make API request (e.g., `curl http://localhost:8000/api/health`)
   - Expected: Log appears in today's platform file
   - Verify: `docker exec trinity-vector sh -c "tail -5 /data/logs/platform-$(date +%Y-%m-%d).json" | jq .`

4. **Verify Agent Log Capture**
   - Action: Start an agent, wait for activity
   - Expected: Logs appear in today's agents file
   - Verify: `docker exec trinity-vector sh -c "tail -5 /data/logs/agents-$(date +%Y-%m-%d).json" | jq .`

5. **Verify JSON Parsing**
   - Action: Check for `.parsed` field in backend logs
   - Expected: Structured Python logs have `parsed` object
   - Verify: `docker exec trinity-vector sh -c "grep trinity-backend /data/logs/platform-$(date +%Y-%m-%d).json | tail -1" | jq '.parsed'`

6. **Verify Health Endpoint**
   - Action: `curl http://localhost:8686/health`
   - Expected: Returns health status
   - Verify: Non-error HTTP response

7. **Verify Log Management API**
   - Action: `curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/logs/stats`
   - Expected: Returns log file statistics
   - Verify: Response includes `log_files` array with date-stamped filenames

8. **Verify Archival Service**
   - Action: `curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/logs/health`
   - Expected: `scheduler_running: true`
   - Verify: `archive_enabled: true`

### Edge Cases
- Container with non-trinity prefix: Should not appear in either log file
- Invalid JSON in message: Should still capture raw message
- Very long log lines: Captured but may be truncated
- Date rollover at midnight: New files created automatically

### Cleanup
No cleanup required - logs are automatically archived after retention period.

### Status
- [x] Vector service deployed (2025-12-31)
- [x] Audit logger removed
- [x] Structured logging in backend
- [x] Query documentation complete
- [x] Daily log rotation via Vector path interpolation
- [x] Automated archival with APScheduler
- [x] Admin API for log management

## Revision History

| Date | Change |
|------|--------|
| 2026-01-23 | Updated documentation: corrected line numbers, added API response examples, updated query examples for date-stamped files, added archival troubleshooting |
| 2026-01-06 | Refactored to sovereign architecture - removed external S3 dependency, added pluggable storage interface |
| 2026-01-05 | Added log retention, rotation, and archival capabilities |
| 2025-12-31 | Initial implementation - replaced audit-logger with Vector |
