# Requirements — Vision, Status Labels, Non-Functional & Out of Scope

> Part of Trinity's requirements set. Index & write-path rule: [requirements.md](../requirements.md).

---

## Vision

Trinity is autonomous agent orchestration and infrastructure — sovereign infrastructure for deploying, orchestrating, and governing fleets of autonomous AI agents on your own hardware.

| Capability | Description | Implementation Status |
|------------|-------------|----------------------|
| **Hierarchical Delegation** | Orchestrator-Worker with context quarantine | ✅ Agent-to-Agent via MCP |
| **Persistent Memory** | Virtual filesystems, memory folding | ✅ Chat persistence, file browser |
| **Agent Configuration** | Template system with CLAUDE.md and credential injection | ✅ Templates with CLAUDE.md |
| **Autonomous Operations** | Scheduling, monitoring, and fleet management | ✅ Cron scheduling, health monitoring |

## Status Labels
- ⏳ Not Started
- 🚧 In Progress
- ✅ Implemented
- ❌ Removed

---

## Non-Functional Requirements

### Performance
- Agent creation: < 30 seconds
- Chat response: < 5 seconds for simple queries
- UI responsiveness: < 100ms for interactions
- WebSocket latency: < 500ms for status updates
- **Task list loading: < 1 second** (PERF-001)

### PERF-001: Task List Performance Optimization
- **Status**: ✅ Implemented (2026-02-21)
- **Requirement ID**: PERF-001
- **Priority**: MEDIUM
- **Description**: Optimize task loading on Agent Detail Tasks tab. Current implementation transfers full `execution_log` (100KB+ per execution) for list views that don't display it.
- **Key Features**:
  - Lightweight `ExecutionSummary` response model excluding heavy fields
  - Optimized SQL query selecting only needed columns
  - Composite index on `(agent_name, started_at DESC)`
  - On-demand detail loading when user expands a task
- **Achieved Impact**: 50-100x reduction in data transfer (10MB → 100KB)
- **Spec**: `docs/requirements/TASK_LIST_PERFORMANCE.md`

### Security
- All credentials encrypted at rest (Redis)
- HTTPS in production (Let's Encrypt)
- Container isolation (network, filesystem)
- Comprehensive audit logging via audit_log table (SEC-001)
- **Encryption key endpoint admin-only** (C-001, 2026-03-09)
- **WebSocket authentication required before accept** (C-002, 2026-03-09; hardened #178, 2026-03-27 — reject before `websocket.accept()`, removed first-message auth)
- **Internal API shared-secret auth** (C-003, 2026-03-09)
- **Agent access control on chat/credential endpoints** (M-004/M-006, 2026-03-09)
- **DOMPurify XSS protection on all v-html** (H-005, 2026-03-09)

### Scalability
- Support 50+ concurrent agents
- Multiple backend workers (uvicorn)
- Stateless backend (Docker as truth)

### Reliability
- Agent state survives backend restart
- SQLite persists across container recreation
- Redis AOF for secret durability

### Testing
- Feature flows include Testing sections
- Manual testing before marking complete
- Automated tests for critical paths only

---

## Out of Scope

- Multi-tenant deployment (single org only)
- ~~Mobile application~~ → See Section 27 (MOB-001) for PWA-based mobile admin
- Fiat/Stripe payment integration (Nevermined handles crypto payments)
- Agent marketplace
