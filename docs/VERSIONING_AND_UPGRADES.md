# Trinity Versioning and Upgrade Guide

## Versioning Philosophy

Trinity follows [Semantic Versioning](https://semver.org/) (SemVer):

```
MAJOR.MINOR.PATCH (e.g., 1.2.3)
```

| Version | When to Increment | Example |
|---------|-------------------|---------|
| **MAJOR** | Breaking changes requiring migration | Database schema changes, API incompatibilities |
| **MINOR** | New features, backward compatible | Gemini runtime support, new MCP tools |
| **PATCH** | Bug fixes, security patches | Fix port allocation bug, UI fixes |

## Current Version: 0.x (Pre-1.0)

During the `0.x` phase:
- API may change between minor versions
- Breaking changes documented in git history and PR descriptions
- Recommended: Pin to specific version tags

**Post-1.0**: Strict semver adherence with deprecation warnings before breaking changes.

---

## Component Versioning

Trinity consists of multiple components that version together:

| Component | Location | Versioning Strategy |
|-----------|----------|---------------------|
| **Backend** | `src/backend/` | Single version with platform |
| **Frontend** | `src/frontend/` | Single version with platform |
| **Base Image** | `docker/base-image/` | Single version with platform |
| **Agent Server** | `docker/base-image/agent_server/` | Single version with platform |
| **MCP Server** | `src/mcp-server/` | Single version with platform |

All components share the same version number for simplicity.

---

## Version Tagging Strategy

### Git Tags

```bash
# Release tags
v0.9.0          # Feature release
v0.9.1          # Patch release
v1.0.0          # Major release

# Pre-release tags
v1.0.0-alpha.1  # Alpha testing
v1.0.0-beta.1   # Beta testing
v1.0.0-rc.1     # Release candidate
```

### Docker Image Tags

```bash
# Production tags
trinity-agent-base:0.9.0      # Specific version (recommended)
trinity-agent-base:0.9        # Latest patch in minor version
trinity-agent-base:latest     # Latest release (not for production)

# Development tags
trinity-agent-base:main       # Latest main branch (CI/CD only)
trinity-agent-base:dev        # Development builds
```

---

## How to upgrade

Run `./scripts/deploy/update.sh` on the host (or `trinity update`). It detects the
install shape, preflights, backs up the database, updates, rebuilds what changed
and verifies the published URL, with `--rollback` to undo. The procedures below
describe what happens underneath; see
[Upgrading](user-docs/guides/deploying/upgrading.md) for the full walkthrough.

## Upgrade Categories

### 1. Non-Breaking Updates (PATCH)

**Examples**: Bug fixes, documentation updates, UI tweaks

**Upgrade Process**:
```bash
git pull origin main
docker compose pull
docker compose up -d
```

**Downtime**: ~30 seconds (container restart)

### 2. Feature Updates (MINOR)

**Examples**: Gemini runtime support, new API endpoints

**Upgrade Process**:
```bash
# 1. Backup (recommended)
./scripts/deploy/backup.sh

# 2. Pull changes
git pull origin main

# 3. Rebuild base image (if changed)
./scripts/deploy/build-base-image.sh

# 4. Restart services
docker compose down
docker compose up -d

# 5. Verify
curl http://localhost:8000/health
```

**Downtime**: 2-5 minutes

**Impact on Running Agents**:
- Existing agents continue running (no rebuild needed)
- New features available only after agent recreation
- For Gemini support: Agents must be recreated with `runtime: gemini-cli`

### 3. Breaking Updates (MAJOR)

**Examples**: Database schema changes, API breaking changes

**Upgrade Process**:
```bash
# 1. Stop all agents
curl -X POST http://localhost:8000/api/ops/stop-all

# 2. Full backup
./scripts/deploy/backup.sh

# 3. Pull changes
git pull origin main

# 4. Run migrations (if any)
./scripts/deploy/migrate.sh

# 5. Rebuild everything
./scripts/deploy/build-base-image.sh
docker compose build

# 6. Start services
docker compose up -d

# 7. Recreate agents (if base image changed)
# Use UI or API to recreate agents from templates
```

**Downtime**: 10-30 minutes

---

## Gemini Runtime Upgrade (v0.9.0)

This is a **MINOR** version update. Here's the specific upgrade path:

### What Changed

| Component | Change Type | Impact |
|-----------|-------------|--------|
| Base Image | Modified | New agents need rebuild |
| Backend | Modified | Restart required |
| Frontend | No change | N/A |
| Database | No change | No migration needed |
| Config | Optional | Add `GOOGLE_API_KEY` if using Gemini |

### Upgrade Steps

```bash
# 1. Pull latest code
git fetch origin
git checkout v0.9.0  # Or: git pull origin main

# 2. (Optional) Add Google API key for Gemini
echo "GOOGLE_API_KEY=your-key" >> .env

# 3. Rebuild base image
./scripts/deploy/build-base-image.sh

# 4. Restart backend
docker compose restart backend

# 5. Verify
curl http://localhost:8000/health
```

### Agent Migration

**Existing agents**: Continue working unchanged (use Claude Code)

**To use Gemini on existing agent**:
1. Note agent's template and configuration
2. Delete the agent
3. Recreate with `runtime: gemini-cli` in template

**New agents**: Can choose runtime at creation time

### Rollback

```bash
# If issues occur
git checkout v0.8.0  # Previous version
./scripts/deploy/build-base-image.sh
docker compose restart backend
```

---

## Recommended Upgrade Practices

### Pre-Upgrade Checklist

- [ ] Review git history for breaking changes (`git log --oneline`)
- [ ] Backup database (`data/trinity.db`)
- [ ] Backup Redis (`data/redis/`)
- [ ] Note running agents and their configurations
- [ ] Schedule maintenance window if production

### Post-Upgrade Verification

```bash
# 1. Health check
curl http://localhost:8000/health

# 2. Test agent creation
# Create test agent via UI

# 3. Test chat
# Send message to test agent

# 4. Verify logs
docker compose logs backend --tail=50
```

### Rollback Plan

Always have a rollback plan:

```bash
# Quick rollback (config/code only)
git checkout <previous-tag>
docker compose up -d

# Full rollback (including data)
./scripts/deploy/restore.sh <backup-timestamp>
```

---

## Version History

| Version | Date | Type | Key Changes |
|---------|------|------|-------------|
| 0.9.0 | 2025-12-28 | MINOR | Gemini CLI runtime support |
| 0.8.x | 2025-12-24 | PATCH | Test suite fixes, bug fixes |
| 0.8.0 | 2025-12-23 | MINOR | First-time setup wizard, API keys management |

See `git log --oneline` for detailed history.

---

## Future: Automated Upgrades

Planned for v1.0+:

1. **Version Check Endpoint**: `/api/version` returns current and latest available
2. **Upgrade Notifications**: UI banner when new version available
3. **One-Click Upgrades**: For non-breaking updates
4. **Migration Scripts**: Automatic database migrations
5. **Agent Auto-Rebuild**: Option to auto-rebuild agents on base image change

---

## Questions?

If you encounter upgrade issues:
1. Check [Known Issues](KNOWN_ISSUES.md)
2. Review [Troubleshooting](onboarding/04-troubleshooting.md)
3. Open an issue on GitHub

