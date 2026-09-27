# Trinity UI Integration Testing - Gap Analysis Report

> **Generated**: 2026-01-14
> **Source**: Cross-reference of USER_STORIES_INVENTORY.md (213 stories) against testing phases (0-18)
> **Purpose**: Identify missing UI test coverage for comprehensive platform validation

---

## Executive Summary

| Category | User Stories | Covered | Gaps | Coverage % |
|----------|-------------|---------|------|------------|
| Authentication & Setup | 8 | 6 | 2 | 75% |
| Agent Management | 12 | 8 | 4 | 67% |
| Agent Execution & Chat | 21 | 10 | 11 | 48% |
| Observability & Monitoring | 13 | 4 | 9 | 31% |
| Agent Configuration | 9 | 1 | 8 | 11% |
| Credential Management | 13 | 0 | 13 | 0% |
| Scheduling & Automation | 8 | 7 | 1 | 88% |
| Agent Sharing & Collaboration | 11 | 2 | 9 | 18% |
| Public Access | 7 | 0 | 7 | 0% |
| Platform Settings | 13 | 6 | 7 | 46% |
| Git Synchronization | 7 | 5 | 2 | 71% |
| MCP API Keys | 6 | 4 | 2 | 67% |
| Multi-Agent Systems | 6 | 0 | 6 | 0% |
| System Agent & Fleet Ops | 12 | 8 | 4 | 67% |
| Templates | 5 | 2 | 3 | 40% |
| File Management | 10 | 5 | 5 | 50% |
| Agent Dashboard | 3 | 0 | 3 | 0% |
| MCP External Tools | 21 | N/A | N/A | N/A |
| Agent Server Internal | 28 | N/A | N/A | N/A |
| **TOTAL (UI-Testable)** | **164** | **68** | **96** | **41%** |

**Critical Finding**: Only 41% of UI-testable user stories have test coverage. Major gaps exist in:
- **Credential Management** (0% - no testing at all)
- **Public Access** (0% - no testing at all)
- **Multi-Agent Systems** (0% - no testing at all)
- **Agent Dashboard** (0% - new feature, no testing)
- **Agent Configuration** (11% - minimal testing)

---

## Detailed Gap Analysis by Category

### 1. Authentication & Setup (75% Coverage)

| ID | User Story | Phase | Status |
|----|------------|-------|--------|
| AUTH-001 | First-time admin password setup | - | ❌ **GAP** |
| AUTH-002 | Block login until setup complete | - | ❌ **GAP** |
| AUTH-003 | Request email verification code | Phase 17 | ✅ Covered |
| AUTH-004 | Enter verification code to login | Phase 17 | ✅ Covered |
| AUTH-005 | Email rate limiting (3/10min) | Phase 17 | ✅ Covered |
| AUTH-006 | Admin password login | Phase 1 | ✅ Covered |
| AUTH-007 | Session persistence across refresh | Phase 1 | ✅ Covered |
| AUTH-008 | JWT token validation | Phase 0/1 | ✅ Covered (implicit) |

**Gaps to Add**:
- **NEW PHASE 19**: First-Time Setup Flow
  - Fresh install scenario (no existing admin)
  - Password wizard completion
  - Block login redirect behavior

---

### 2. Agent Management (67% Coverage)

| ID | User Story | Phase | Status |
|----|------------|-------|--------|
| AGENT-001 | See all accessible agents | Phase 2, 11 | ✅ Covered |
| AGENT-002 | Create agent from template | Phase 2 | ✅ Covered |
| AGENT-003 | View agent details | Phase 2 | ✅ Covered |
| AGENT-004 | Delete agent | Phase 12 | ✅ Covered |
| AGENT-005 | Name sanitization for Docker | Phase 2 | ⚠️ Partial (not explicit) |
| AGENT-006 | Start stopped agent | Phase 2 | ✅ Covered |
| AGENT-007 | Stop running agent | Phase 2 | ✅ Covered |
| AGENT-008 | Visual feedback during start/stop | Phase 2 | ✅ Covered |
| AGENT-009 | Trinity meta-prompt injection | - | ❌ **GAP** |
| AGENT-010 | Status display (running/stopped/error) | Phase 11 | ⚠️ Partial |
| AGENT-011 | Activity state (Active/Idle/Offline) | - | ❌ **GAP** |
| AGENT-012 | Runtime type badge (Claude/Gemini) | - | ❌ **GAP** |

**Gaps to Add**:
- Test name validation error messages (special characters, length)
- Verify Trinity injection via filesystem check
- Test activity state transitions (send message → Active → Idle)
- Verify runtime badge display for Claude vs Gemini agents

---

### 3. Agent Execution & Chat (48% Coverage) - **MAJOR GAP**

| ID | User Story | Phase | Status |
|----|------------|-------|--------|
| EXEC-001 | Send messages to agent | Phase 4, 16 | ✅ Covered |
| EXEC-002 | See conversation history | Phase 4 | ⚠️ Partial |
| EXEC-003 | Start new session | - | ❌ **GAP** |
| EXEC-004 | See context window usage | Phase 3 | ✅ Covered |
| EXEC-005 | Create tasks for agents | Phase 7 | ✅ Covered |
| EXEC-006 | See task execution history | Phase 7 | ✅ Covered |
| EXEC-007 | View detailed execution results | - | ❌ **GAP** |
| EXEC-008 | View full execution transcript | - | ❌ **GAP** |
| EXEC-009 | **Real-time execution logs (SSE)** | - | ❌ **CRITICAL GAP** |
| EXEC-010 | **"Live" indicator for running tasks** | - | ❌ **CRITICAL GAP** |
| EXEC-011 | Auto-scroll during streaming | - | ❌ **GAP** |
| EXEC-012 | **Stop running execution** | - | ❌ **CRITICAL GAP** |
| EXEC-013 | See which executions are running | - | ❌ **GAP** |
| EXEC-014 | See queue status | Phase 8 | ✅ Covered |
| EXEC-015 | Clear queue (admin) | Phase 8 | ⚠️ Partial |
| EXEC-016 | Force release stuck agent | Phase 8 | ⚠️ Partial |
| EXEC-017 | Parallel stateless task execution | - | ❌ **GAP** (API only) |
| EXEC-018 | Persistent chat history | - | ❌ **GAP** |
| EXEC-019 | See all chat sessions | - | ❌ **GAP** |
| EXEC-020 | View specific session messages | - | ❌ **GAP** |
| EXEC-021 | Close a session | - | ❌ **GAP** |

**CRITICAL Gaps to Add** (New Phase Required):
- **NEW PHASE 20**: Live Execution Streaming
  - Navigate to running task → ExecutionDetail page
  - Verify "Live" green pulsing badge appears
  - SSE streaming shows real-time logs
  - Auto-scroll behavior
  - Stop button terminates execution
  - Queue releases after termination

- **NEW PHASE 21**: Session Management
  - List all chat sessions
  - View session transcript
  - Close session
  - New session reset button

---

### 4. Observability & Monitoring (31% Coverage) - **MAJOR GAP**

| ID | User Story | Phase | Status |
|----|------------|-------|--------|
| OBS-001 | View container logs | - | ❌ **GAP** |
| OBS-002 | Search/filter logs | - | ❌ **GAP** |
| OBS-003 | See CPU/memory usage | - | ❌ **GAP** |
| OBS-004 | See context window percentage | Phase 3 | ✅ Covered |
| OBS-005 | See real-time tool calls | - | ❌ **GAP** |
| OBS-006 | See tool call details | - | ❌ **GAP** |
| OBS-007 | See aggregated tool statistics | - | ❌ **GAP** |
| OBS-008 | Cross-agent activity timeline | Phase 11 | ⚠️ Partial |
| OBS-009 | Filter timeline by activity type | - | ❌ **GAP** |
| OBS-010 | Filter timeline by time range | - | ❌ **GAP** |
| OBS-011 | Host CPU/memory/disk display | Phase 14 | ✅ Covered |
| OBS-012 | Aggregate container stats | Phase 14 | ✅ Covered |
| OBS-013 | Execution statistics per agent | Phase 11 | ⚠️ Partial |

**Gaps to Add**:
- **NEW PHASE 22**: Logs & Telemetry
  - Navigate to Logs tab
  - Verify logs display
  - Test log search functionality
  - Verify CPU/memory sparklines in agent header
  - Test auto-refresh behavior

- **Extend Phase 11**: Activity Timeline
  - Use activity type filter dropdown
  - Use time range selector
  - Verify execution stats (tasks count, success %, cost)

---

### 5. Agent Configuration (11% Coverage) - **CRITICAL GAP**

| ID | User Story | Phase | Status |
|----|------------|-------|--------|
| CFG-001 | View agent resource limits | - | ❌ **GAP** |
| CFG-002 | Set memory/CPU limits | - | ❌ **GAP** |
| CFG-003 | Agents restart on limit change | - | ❌ **GAP** |
| CFG-004 | Enable full capabilities mode | - | ❌ **GAP** |
| CFG-005 | View current model | - | ❌ **GAP** |
| CFG-006 | Change LLM model | - | ❌ **GAP** |
| CFG-007 | Enable/disable autonomy mode | Phase 7 | ⚠️ Partial |
| CFG-008 | See autonomy status with counts | - | ❌ **GAP** |
| CFG-009 | Generate SSH credentials | - | N/A (MCP only) |

**Gaps to Add**:
- **NEW PHASE 23**: Agent Configuration
  - Click gear button → Resource limits modal
  - Change memory/CPU limits
  - Verify restart prompt
  - Test full capabilities toggle
  - Change model (sonnet → opus → haiku)
  - Verify autonomy toggle in multiple locations (Dashboard, Agents, AgentDetail)

---

### 6. Credential Management (0% Coverage) - **CRITICAL GAP**

| ID | User Story | Phase | Status |
|----|------------|-------|--------|
| CRED-001 | Add credentials | - | ❌ **GAP** |
| CRED-002 | List credentials | - | ❌ **GAP** |
| CRED-003 | Update credentials | - | ❌ **GAP** |
| CRED-004 | Delete credentials | - | ❌ **GAP** |
| CRED-005 | Bulk import from .env | - | ❌ **GAP** |
| CRED-006 | OAuth provider authentication | - | ❌ **GAP** |
| CRED-007 | See OAuth provider status | - | ❌ **GAP** |
| CRED-008 | See agent credential requirements | - | ❌ **GAP** |
| CRED-009 | Assign credentials to agents | - | ❌ **GAP** |
| CRED-010 | Bulk assign credentials | - | ❌ **GAP** |
| CRED-011 | Reload credentials on running agent | - | ❌ **GAP** |
| CRED-012 | Hot-reload credentials via paste | - | ❌ **GAP** |
| CRED-013 | Check credential file status | - | ❌ **GAP** |

**Gaps to Add**:
- **NEW PHASE 24**: Credential Management
  - Navigate to Credentials page
  - Add new credential
  - List and verify display
  - Bulk import .env text
  - Delete credential
  - Navigate to agent → Credentials tab
  - See requirements vs configured status
  - Assign credential to agent
  - Hot-reload paste test

---

### 7. Scheduling & Automation (88% Coverage) - Good

| ID | User Story | Phase | Status |
|----|------------|-------|--------|
| SCHED-001 | Create scheduled tasks | Phase 7 | ✅ Covered |
| SCHED-002 | List schedules | Phase 7 | ✅ Covered |
| SCHED-003 | Update schedules | Phase 7 | ✅ Covered |
| SCHED-004 | Delete schedules | Phase 7 | ✅ Covered |
| SCHED-005 | Enable/disable schedules | Phase 7 | ✅ Covered |
| SCHED-006 | Manual trigger | Phase 7 | ✅ Covered |
| SCHED-007 | See execution history | Phase 7 | ✅ Covered |
| SCHED-008 | **Make Repeatable (from task)** | - | ❌ **GAP** |

**Gap to Add** (extend Phase 7):
- Click calendar icon on completed task in TasksPanel
- Verify tab switches to Schedules
- Verify message is pre-filled from task

---

### 8. Agent Sharing & Collaboration (18% Coverage) - **CRITICAL GAP**

| ID | User Story | Phase | Status |
|----|------------|-------|--------|
| SHARE-001 | Share agent with users | - | ❌ **GAP** |
| SHARE-002 | See who agent is shared with | - | ❌ **GAP** |
| SHARE-003 | Revoke sharing | - | ❌ **GAP** |
| SHARE-004 | Auto-whitelist shared emails | - | ❌ **GAP** |
| PERM-001 | See which agents can be called | Phase 5 | ⚠️ Partial |
| PERM-002 | Grant permission to call agent | Phase 2 | ✅ Covered |
| PERM-003 | Revoke permission | - | ❌ **GAP** |
| PERM-004 | Set all permissions at once | - | ❌ **GAP** |
| FOLDER-001 | Expose shared folder | - | ❌ **GAP** |
| FOLDER-002 | Consume other agents' folders | - | ❌ **GAP** |
| FOLDER-003 | See available folders | - | ❌ **GAP** |
| FOLDER-004 | See folder consumers | - | ❌ **GAP** |

**Gaps to Add**:
- **NEW PHASE 25**: Agent Sharing
  - Navigate to Sharing tab
  - Share agent with test email
  - Verify email added to whitelist
  - Revoke sharing
  - Test permission-denied scenario (non-shared user)

- **Extend Phase 5** or **NEW PHASE 26**: Shared Folders
  - Enable expose on agent A
  - Enable consume on agent B
  - Verify B can see A's folder
  - Verify files are accessible

---

### 9. Public Access (0% Coverage) - **CRITICAL GAP**

| ID | User Story | Phase | Status |
|----|------------|-------|--------|
| PUB-001 | Create public link | - | ❌ **GAP** |
| PUB-002 | List public links | - | ❌ **GAP** |
| PUB-003 | Update link settings | - | ❌ **GAP** |
| PUB-004 | Revoke public link | - | ❌ **GAP** |
| PUB-005 | Access agent via public link | - | ❌ **GAP** |
| PUB-006 | Email verification for public | - | ❌ **GAP** |
| PUB-007 | Chat via public link | - | ❌ **GAP** |

**Gaps to Add**:
- **NEW PHASE 27**: Public Access
  - Navigate to Public Links tab
  - Create public link (with/without email requirement)
  - Copy link
  - Open in incognito/new browser
  - Verify agent info displays
  - Complete email verification if required
  - Send message via public chat
  - Revoke link
  - Verify access denied after revoke

---

### 10. Platform Settings (46% Coverage)

| ID | User Story | Phase | Status |
|----|------------|-------|--------|
| SET-001 | View API key status | Phase 13 | ✅ Covered |
| SET-002 | Set Anthropic API key | Phase 13 | ✅ Covered |
| SET-003 | Test Anthropic API key | Phase 13 | ✅ Covered |
| SET-004 | Set GitHub PAT | Phase 13 | ⚠️ Partial |
| SET-005 | Test GitHub PAT | Phase 13 | ⚠️ Partial |
| SET-006 | View email whitelist | Phase 13 | ✅ Covered |
| SET-007 | Add emails to whitelist | Phase 13 | ✅ Covered |
| SET-008 | Remove emails from whitelist | Phase 13 | ✅ Covered |
| SET-009 | Set system-wide Trinity prompt | Phase 13 | ⚠️ Partial |
| SET-010 | View ops configuration | - | ❌ **GAP** |
| SET-011 | Update ops settings | - | ❌ **GAP** |
| SET-012 | Reset ops to defaults | - | ❌ **GAP** |
| SET-013 | Enable/disable SSH access | - | ❌ **GAP** |

**Gaps to Add** (extend Phase 13):
- Test GitHub PAT with repository access
- View ops configuration section
- Modify ops thresholds
- Reset to defaults
- Toggle SSH access setting

---

### 11. Git Synchronization (71% Coverage) - Good

| ID | User Story | Phase | Status |
|----|------------|-------|--------|
| GIT-001 | See git status | Phase 18 | ✅ Covered |
| GIT-002 | See git configuration | Phase 18 | ✅ Covered |
| GIT-003 | See recent commits | Phase 18 | ✅ Covered |
| GIT-004 | Push changes to GitHub | Phase 18 | ✅ Covered |
| GIT-005 | Pull changes from GitHub | Phase 18 | ✅ Covered |
| GIT-006 | Initialize GitHub sync | Phase 18 | ⚠️ Partial |
| GIT-007 | Conflict resolution strategies | - | ❌ **GAP** |

**Gap to Add** (extend Phase 18):
- Test conflict scenario
- Test force_push strategy
- Test stash_reapply strategy

---

### 12. MCP API Keys (67% Coverage) - Good

| ID | User Story | Phase | Status |
|----|------------|-------|--------|
| MCP-001 | Create MCP API keys | Phase 13 | ✅ Covered |
| MCP-002 | List API keys | Phase 13 | ✅ Covered |
| MCP-003 | Revoke API key | - | ❌ **GAP** |
| MCP-004 | Delete API key | Phase 13 | ⚠️ Partial |
| MCP-005 | Copy MCP configuration | Phase 13 | ✅ Covered |
| MCP-006 | Ensure default key exists | - | ❌ **GAP** (auto-creation) |

**Gap to Add** (extend Phase 13):
- Test revoke vs delete behavior
- Verify revoked key cannot authenticate

---

### 13. Multi-Agent Systems (0% Coverage) - **CRITICAL GAP**

| ID | User Story | Phase | Status |
|----|------------|-------|--------|
| SYS-001 | Deploy from YAML manifest | - | ❌ **GAP** |
| SYS-002 | Validate manifest (dry run) | - | ❌ **GAP** |
| SYS-003 | List deployed systems | - | ❌ **GAP** |
| SYS-004 | View system details | - | ❌ **GAP** |
| SYS-005 | Restart system | - | ❌ **GAP** |
| SYS-006 | Export system manifest | - | ❌ **GAP** |

**Note**: These are API/MCP-driven features. UI may not exist yet.

**Gaps to Add** (if UI exists):
- **NEW PHASE 28**: Multi-Agent Systems
  - Deploy sample manifest
  - Verify all agents created
  - Verify permissions set
  - List systems
  - Export manifest

---

### 14. System Agent & Fleet Operations (67% Coverage) - Good

| ID | User Story | Phase | Status |
|----|------------|-------|--------|
| FLEET-001 | See system agent status | Phase 15 | ✅ Covered |
| FLEET-002 | Reinitialize system agent | Phase 15 | ✅ Covered |
| FLEET-003 | System agent terminal | Phase 15 | ✅ Covered |
| FLEET-004 | Fleet-wide status | Phase 15 | ✅ Covered |
| FLEET-005 | Fleet health | Phase 15 | ✅ Covered |
| FLEET-006 | Cost metrics | Phase 14 | ✅ Covered |
| FLEET-007 | Restart all agents | Phase 15 | ⚠️ Partial |
| FLEET-008 | Stop all agents | Phase 15 | ⚠️ Partial |
| FLEET-009 | Emergency stop | Phase 15 | ✅ Covered |
| FLEET-010 | List all schedules | - | ❌ **GAP** |
| FLEET-011 | Pause all schedules | - | ❌ **GAP** |
| FLEET-012 | Resume all schedules | - | ❌ **GAP** |

**Gaps to Add** (extend Phase 15):
- Use `/ops/schedules/list` command
- Use `/ops/schedules/pause` command
- Use `/ops/schedules/resume` command
- Verify schedules state change

---

### 15. Templates (40% Coverage)

| ID | User Story | Phase | Status |
|----|------------|-------|--------|
| TPL-001 | Browse available templates | - | ❌ **GAP** |
| TPL-002 | See template details | - | ❌ **GAP** |
| TPL-003 | See credential requirements | - | ❌ **GAP** |
| TPL-004 | Refresh templates | - | ❌ **GAP** |
| TPL-005 | Select template in create flow | Phase 2 | ✅ Covered |

**Gaps to Add**:
- **NEW PHASE 29**: Template Browsing
  - Navigate to Templates page (if exists)
  - OR test template dropdown/cards in create modal
  - View template details
  - See credential requirements
  - Test refresh functionality

---

### 16. File Management (50% Coverage)

| ID | User Story | Phase | Status |
|----|------------|-------|--------|
| FILE-001 | Browse agent files | Phase 9 | ✅ Covered |
| FILE-002 | Download files | Phase 9 | ✅ Covered |
| FILE-003 | Preview files | Phase 9 | ⚠️ Partial |
| FILE-004 | Edit text files | - | ❌ **GAP** |
| FILE-005 | Delete files | Phase 9 | ✅ Covered |
| FILE-006 | Protected file warnings | Phase 9 | ✅ Covered |
| FILE-007 | Dedicated file manager page | - | ❌ **GAP** |
| FILE-008 | Agent selector in file manager | - | ❌ **GAP** |
| FILE-009 | Two-panel layout | - | ❌ **GAP** |
| FILE-010 | Toggle hidden files | - | ❌ **GAP** |

**Gaps to Add** (extend Phase 9 or new phase):
- Navigate to `/files` page
- Test agent selector dropdown
- Test two-panel layout
- Test hidden files toggle
- Test file edit functionality
- Test rich preview (image, video, audio, PDF)

---

### 17. Agent Dashboard (0% Coverage) - **CRITICAL GAP** (New Feature)

| ID | User Story | Phase | Status |
|----|------------|-------|--------|
| DASH-001 | See agent-defined dashboards | - | ❌ **GAP** |
| DASH-002 | Define widgets in dashboard.yaml | - | ❌ **GAP** |
| DASH-003 | Auto-refresh dashboards | - | ❌ **GAP** |

**Gaps to Add**:
- **NEW PHASE 30**: Agent Dashboard
  - Create agent with dashboard.yaml
  - Navigate to Dashboard tab
  - Verify widgets render correctly
  - Test all widget types (metric, status, progress, etc.)
  - Verify auto-refresh behavior

---

### 18. MCP External Tools - N/A for UI Testing

These are API/MCP tools (21 stories). Not applicable for browser UI testing.

---

### 19. Agent Server Internal - N/A for UI Testing

These are internal container APIs (28 stories). Not applicable for browser UI testing.

---

## Priority Recommendations

### P0 - Critical (Create Immediately)

| New Phase | User Stories | Business Impact |
|-----------|--------------|-----------------|
| **Phase 20: Live Execution Streaming** | EXEC-009 to EXEC-013 | Core user workflow - monitoring long tasks |
| **Phase 24: Credential Management** | CRED-001 to CRED-013 | Agents cannot function without credentials |
| **Phase 27: Public Access** | PUB-001 to PUB-007 | External user-facing feature |

### P1 - High (Create Soon)

| New Phase | User Stories | Business Impact |
|-----------|--------------|-----------------|
| **Phase 23: Agent Configuration** | CFG-001 to CFG-008 | Resource management, model selection |
| **Phase 25: Agent Sharing** | SHARE-001 to SHARE-004 | Team collaboration feature |
| **Phase 26: Shared Folders** | FOLDER-001 to FOLDER-004 | Agent-to-agent file collaboration |
| **Phase 30: Agent Dashboard** | DASH-001 to DASH-003 | New feature needs validation |

### P2 - Medium (Extend Existing)

| Existing Phase | Additions | User Stories |
|----------------|-----------|--------------|
| Phase 7 | Make Repeatable flow | SCHED-008 |
| Phase 9 | File Manager page | FILE-007 to FILE-010 |
| Phase 11 | Timeline filters | OBS-009, OBS-010 |
| Phase 13 | Ops settings, SSH toggle | SET-010 to SET-013 |
| Phase 15 | Schedule management | FLEET-010 to FLEET-012 |
| Phase 18 | Conflict resolution | GIT-007 |

### P3 - Low (Nice to Have)

| New Phase | User Stories | Notes |
|-----------|--------------|-------|
| Phase 19: First-Time Setup | AUTH-001, AUTH-002 | Edge case for fresh installs |
| Phase 22: Logs & Telemetry | OBS-001 to OBS-007 | Partial coverage exists |
| Phase 28: Multi-Agent Systems | SYS-001 to SYS-006 | API-driven, may not have UI |
| Phase 29: Template Browsing | TPL-001 to TPL-004 | Lower priority |

---

## Missing Phase: Phase 6

Note: Phase 6 is missing from the testing framework. The dependency chain shows:
- Phase 5 → Phase 7 (skip 6)

Consider reserving Phase 6 for a future feature or renumbering.

---

## Summary

### Test Coverage Status

```
Current Coverage: 68/164 UI-testable stories = 41%
Target Coverage:  150+/164 stories = 90%+

Critical Gaps (0% coverage):
- Credential Management (13 stories)
- Public Access (7 stories)
- Multi-Agent Systems (6 stories)
- Agent Dashboard (3 stories)

Major Gaps (<25% coverage):
- Agent Configuration (11%)
- Agent Sharing & Collaboration (18%)
- Observability & Monitoring (31%)
```

### Action Items

1. **Immediate**: Create Phases 20, 24, 27 (P0 priority)
2. **This Week**: Create Phases 23, 25, 26, 30 (P1 priority)
3. **This Month**: Extend existing phases with P2 additions
4. **Backlog**: Create P3 phases as time permits

### Estimated Effort

| Priority | New Phases | Extensions | Time Estimate |
|----------|------------|------------|---------------|
| P0 | 3 | 0 | ~3 hours |
| P1 | 4 | 0 | ~4 hours |
| P2 | 0 | 6 | ~2 hours |
| P3 | 4 | 0 | ~3 hours |
| **Total** | **11** | **6** | **~12 hours** |

---

*Report generated by gap analysis of USER_STORIES_INVENTORY.md against testing phases 0-18*
