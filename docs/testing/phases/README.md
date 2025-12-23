# Trinity UI Integration Test Phases

**New modular testing structure for comprehensive platform validation**

---

## What Changed?

The monolithic `UI_INTEGRATION_TEST.md` has been **split into individual phase files** for:
- ✅ Clearer step-by-step instructions
- ✅ Better for sub-agent automation
- ✅ Easy to run single phase
- ✅ Progress tracking across phases
- ✅ Focus on GitHub agent validation
- ✅ Explicit context/task tracking

---

## Quick Start

### Run All Phases (Automated)
```bash
# Using the test runner agent
/run @agent-ui-integration-tester all
```

### Run Specific Phase (Manual)
1. Open `PHASE_XX_NAME.md`
2. Follow instructions
3. Record results
4. Proceed to next phase

### Run Specific Phase (Automated)
```bash
# Run only Phase 3 (Context Validation)
/run @agent-ui-integration-tester phase-3
```

---

## Phase Sequence

| # | Name | Time | What's Tested |
|---|------|------|---|
| 0 | Setup | 5 min | Services, clean slate |
| 1 | Authentication | 5 min | Login, session, token |
| 2 | Agent Creation | 30 min | **GitHub templates**, 8 agents, **default permissions** |
| 3 | Context Validation | 10 min | **CRITICAL: context %, progress bar** |
| 4 | State Persistence | 10 min | File I/O, counter.txt, state |
| 5 | Agent Collaboration + Permissions | 25 min | Trinity MCP, delegation, Pillar II, **permissions UI + enforcement** |
| 7 | Scheduling | 15 min | Cron, execution history, autonomy |
| 8 | Execution Queue | 15 min | Concurrency, 429, queue ordering |
| 9 | File Browser | 10 min | Tree structure, download, security |
| 10 | Error Handling | 10 min | Failures, recovery, cascading |
| 11 | Multi-Agent | 10 min | Dashboard with 8 agents |
| 12 | Cleanup | 5 min | Delete all agents, clean slate |

**Total**: ~175 minutes (~3 hours) for full suite

---

## Critical Validations (Every Phase)

### GitHub Templates (MANDATORY)
All agents MUST be created from `github:abilityai/test-agent-*`:
```bash
docker inspect agent-test-echo --format='{{index .Config.Labels "trinity.template"}}'
# Expected: github:abilityai/test-agent-echo ✅
# NOT: local:test-echo ❌ (PHASE FAILS)
```

### Context Progress Bar (CRITICAL BUG)
Context % MUST increase as messages are sent:
- ✅ If increasing: Bug fixed, continue testing
- ❌ If stuck at 0%: Critical bug exists, document and continue

### Task Progress (IMPORTANT)
Task indicator should show actual task name:
- ✅ If shows "Task 1/5": Working correctly
- ❌ If stuck at "—": Known issue, check Plans tab

---

## Phase Files

All files in `docs/testing/phases/`:

| File | Purpose |
|------|---------|
| **INDEX.md** | Overview, dependencies, templates |
| **README.md** | This file - quick reference |
| **PHASE_00_SETUP.md** | Prerequisites, services, templates |
| **PHASE_01_AUTHENTICATION.md** | Login, session, token, user profile |
| **PHASE_02_AGENT_CREATION.md** | Create 8 agents from GitHub |
| **PHASE_03_CONTEXT_VALIDATION.md** | Context %, progress bar (CRITICAL) |
| **PHASE_04_STATE_PERSISTENCE.md** | File I/O, counter operations |
| **PHASE_05_AGENT_COLLABORATION.md** | Trinity MCP, delegation, **Permissions system** |
| **PHASE_07_SCHEDULING.md** | Cron, execution history, autonomy |
| **PHASE_08_EXECUTION_QUEUE.md** | Concurrency, 429, queue ordering |
| **PHASE_09_FILE_BROWSER.md** | Tree structure, download, security |
| **PHASE_10_ERROR_HANDLING.md** | Failures, recovery, cascading |
| **PHASE_11_MULTI_AGENT_DASHBOARD.md** | Dashboard with 8 agents |
| **PHASE_12_CLEANUP.md** | Delete all agents, clean slate |

---

## How Sub-Agent Runs Phases

The `ui-integration-tester` sub-agent:

1. **Loads phase file** from `docs/testing/phases/PHASE_XX.md`
2. **Executes test steps** in order
3. **Takes screenshots** for each action
4. **Verifies success criteria**
5. **If PASSED**: Automatically loads next phase
6. **If FAILED**: Stops, documents issue, awaits decision
7. **Generates report** with all results

```
PHASE_00 ✅ PASSED
   ↓
PHASE_01 ✅ PASSED
   ↓
PHASE_02 ✅ PASSED (GitHub templates verified)
   ↓
PHASE_03 ⚠️ BUG FOUND: Context stuck at 0%
   ↓ (continues despite bug)
PHASE_04 ✅ PASSED
   ↓
... (continues through remaining phases)
```

---

## Prerequisites Enforcement

Each phase **requires previous phases to PASS**:

```
Phase 0 → All services running
Phase 1 → Logged in with valid token
Phase 2 → 8 agents created (GitHub templates)
Phase 3 → Context tracking status known
Phase 4 → test-counter working
Phase 5 → test-delegator working
...
```

If skipping phases, tester must **manually verify** prerequisites.

---

## Success Criteria Checklist

Each phase file contains a "Success Criteria" section.

Example (Phase 3: Context Validation):
```
Phase 3 is PASSED when:
- ✅ Context % increased from 0%
- ✅ Progress bar visually filled
- ✅ Tooltip shows "X / 200K tokens"
- ✅ Colors change: Green → Yellow → Orange
- ✅ Multiple agents show independent context
```

---

## Results Tracking

After each phase, document:

```markdown
## Phase X: [Name]
**Status**: ✅ PASSED / ❌ FAILED / ⚠️ BUG FOUND

**Results**:
- [x] Test step 1
- [x] Test step 2
- [x] Success criteria met

**Issues Found**:
- None (or list any bugs)

**Evidence**:
- Screenshot: /test-screenshots/phase-XX-01.png
- API response: [Details]
```

---

## Common Issues by Phase

| Phase | Issue | Solution |
|-------|-------|----------|
| 0 | Services not running | `./scripts/deploy/start.sh` |
| 1 | Invalid login | Check admin credentials in `.env` |
| 2 | Agent stuck on "Starting" | Wait 30 sec, check logs |
| 2 | Local template instead of GitHub | Delete and recreate with correct template |
| 3 | **Context stuck at 0%** | **Known bug - document and continue** |
| 4 | Counter wrong value | Check counter.txt: `docker exec agent-test-counter cat /home/developer/workspace/counter.txt` |
| 5 | Delegation times out | Wait longer (15-20 sec normal) |

---

## GitHub Template Validation

**CRITICAL**: Every agent MUST be from GitHub

Check each agent:
```bash
# test-echo
docker inspect agent-test-echo --format='{{index .Config.Labels "trinity.template"}}'
# Expected: github:abilityai/test-agent-echo ✅

# test-counter
docker inspect agent-test-counter --format='{{index .Config.Labels "trinity.template"}}'
# Expected: github:abilityai/test-agent-counter ✅

# All others...
```

**If any show `local:`**: Phase 2 FAILS - requires GitHub templates.

---

## Running Phase 3 Multiple Times

Phase 3 (Context Validation) is designed to run multiple times:

1. **Run 1**: Context stuck at 0% (bug confirmed)
   - Document issue
   - Continue to Phase 4 (for now)

2. **After Fix**: Re-run Phase 3
   - Context % now increases
   - Progress bar fills
   - Bug resolution confirmed

3. **Record Both Results**:
   - Before fix: ❌ FAILED (but expected)
   - After fix: ✅ PASSED

---

## Skipping Phases (Advanced)

If time-constrained, skip optional phases:

**REQUIRED** (do not skip):
- Phase 0 (Setup)
- Phase 1 (Authentication)
- Phase 2 (Agent Creation)
- Phase 3 (Context) - must document status

**Optional** (can skip):
- Phase 4-11 (specific features)
- Phase 12 (Cleanup - always do this)

---

## Testing with Different Environments

### Local Development
- URL: `http://localhost:3000`
- Backend: `http://localhost:8000`
- Credentials: `admin / YOUR_PASSWORD` (from `.env`)

### Staging/Production
- Update URLs in phase files
- Adjust timings (may be slower)
- Use production credentials

---

## Automated Test Runner (TBD)

Future `run_test_phases.py` script will:

```bash
# Run all phases
python3 docs/testing/run_test_phases.py --all

# Run phase range
python3 docs/testing/run_test_phases.py --from 2 --to 5

# Run single phase with prerequisites check
python3 docs/testing/run_test_phases.py --phase 5

# Skip prerequisites check (advanced)
python3 docs/testing/run_test_phases.py --phase 6 --skip-prerequisites

# Generate HTML report
python3 docs/testing/run_test_phases.py --all --report html
```

Not yet implemented - use sub-agent for now.

---

## Questions?

Refer to specific phase file for detailed instructions:
- `PHASE_00_SETUP.md` - Troubleshooting services
- `PHASE_02_AGENT_CREATION.md` - GitHub template details
- `PHASE_03_CONTEXT_VALIDATION.md` - Context bug documentation
- `INDEX.md` - Full overview and dependencies

---

**Status**: 🟢 All 13 phases ready (0-12)
**Last Updated**: 2025-12-10 (Added Agent Permissions testing to Phase 2 and Phase 5)
