# Trinity Testing Structure - Modular Phases

**Comprehensive guide to the new phase-based testing framework**

---

## What's Changed

### Before (Monolithic)
- Single large `UI_INTEGRATION_TEST.md` file (800+ lines)
- All tests in one document
- Difficult to track progress across phases
- Hard to automate (no clear phase boundaries)

### After (Modular)
- 12 individual phase files in `docs/testing/phases/`
- Each phase: 5-10 minutes of focused testing
- Clear prerequisites for each phase
- Designed for automated sub-agent execution
- Better progress tracking and error documentation

---

## New File Structure

```
docs/testing/
├── phases/                          # NEW: Modular phase directory
│   ├── README.md                   # Quick start guide
│   ├── INDEX.md                    # Full overview & dependencies
│   ├── PHASE_00_SETUP.md           # Setup & prerequisites
│   ├── PHASE_01_AUTHENTICATION.md  # Login & session
│   ├── PHASE_02_AGENT_CREATION.md  # GitHub template agents
│   ├── PHASE_03_CONTEXT_VALIDATION.md  # CRITICAL: Context % tracking
│   ├── PHASE_04_STATE_PERSISTENCE.md   # File I/O & counter
│   ├── PHASE_05_AGENT_COLLABORATION.md # Trinity MCP & delegation
│   ├── PHASE_06_WORKPLAN_SYSTEM.md     # Task DAGs (stub)
│   └── ... (Phases 7-12 stubs)
│
├── UI_INTEGRATION_TEST.md          # KEPT: Original comprehensive reference
├── MODULAR_TESTING_STRUCTURE.md    # THIS FILE
└── run_integration_test.py          # Automated test runner (existing)
```

**What Stayed**:
- `UI_INTEGRATION_TEST.md` - Kept for reference (800+ lines with all details)
- `run_integration_test.py` - Automated test script (existing)

**What's New**:
- `phases/` directory - 12 modular phase files
- `phases/README.md` - Quick reference for testers
- `phases/INDEX.md` - Complete phase overview and dependencies

---

## How It Works

### For Manual Testing

1. **Start**: Read `phases/README.md` (2 minutes)
2. **Phase 0**: Follow `PHASE_00_SETUP.md` (5 minutes)
3. **Phase 1**: Follow `PHASE_01_AUTHENTICATION.md` (5 minutes)
4. **Phase 2**: Follow `PHASE_02_AGENT_CREATION.md` (30 minutes)
5. **Phase 3**: Follow `PHASE_03_CONTEXT_VALIDATION.md` (10 minutes)
6. ... (continue through phases 4-12)
7. **Cleanup**: `PHASE_12_CLEANUP.md` (always do this)

**Total Time**: ~2h 45min for full suite

### For Automated Testing (Sub-Agent)

```bash
# Sub-agent runs phases sequentially
/run @agent-ui-integration-tester all

# Or specific phase
/run @agent-ui-integration-tester phase-3

# Or phase range
/run @agent-ui-integration-tester phases 2-5
```

**Sub-agent process**:
1. Reads phase file from `docs/testing/phases/PHASE_XX.md`
2. Executes test steps (browser automation)
3. Takes screenshots for verification
4. Checks success criteria
5. If PASSED: loads next phase automatically
6. If FAILED: stops and documents issue
7. Generates test report

---

## Phase Overview

| Phase | Test | GitHub | Context | Status |
|-------|------|--------|---------|--------|
| 0 | Setup | - | - | ✅ Ready |
| 1 | Auth | - | - | ✅ Ready |
| 2 | Create 8 Agents | **REQUIRED** | - | ✅ Ready |
| 3 | Context Tracking | - | **CRITICAL BUG** | ✅ Ready |
| 4 | State (counter) | - | Grows | ✅ Ready |
| 5 | Collaboration | - | Separate | ✅ Ready |
| 6 | Workplan | - | During tasks | 🚧 Stub |
| 7 | Scheduling | - | Per schedule | 🚧 Stub |
| 8 | Queue | - | Per execution | 🚧 Stub |
| 9 | Files | - | During ops | 🚧 Stub |
| 10 | Errors | - | Tracking | 🚧 Stub |
| 11 | Dashboard | All 8 | Independent | 🚧 Stub |
| 12 | Cleanup | - | - | ✅ Ready |

---

## Critical Validation Points

### GitHub Templates (Every Phase After 1)
```bash
# MUST show: github:abilityai/test-agent-*
docker inspect agent-test-echo --format='{{index .Config.Labels "trinity.template"}}'

# NOT: local:test-echo ❌ (phase fails)
```

### Context Tracking (Phase 3 - CRITICAL)
- Context % MUST increase with messages
- Progress bar MUST fill visually
- Colors must change (Green → Yellow → Orange → Red)
- If stuck at 0%: **Known bug - document and continue**

### Task Progress (Phase 6+)
- Tasks should show name: "Task 1/5"
- Not stuck at "—"
- Check Plans tab if not visible

---

## Phase Dependencies

```
Phase 0 (Setup)
  ↓ All services running
Phase 1 (Authentication)
  ↓ Logged in, have token
Phase 2 (Agent Creation)
  ↓ 8 agents created (GitHub templates)
Phase 3 (Context Validation)
  ↓ Context status known (bug or working)
Phase 4 (State Persistence)
  ↓ test-counter verified
Phase 5 (Agent Collaboration)
  ↓ test-delegator verified
Phase 6 (Workplan System)
  ↓ test-worker verified
Phase 7 (Scheduling)
  ↓ test-scheduler verified
Phase 8 (Execution Queue)
  ↓ test-queue verified
Phase 9 (File Browser)
  ↓ test-files verified
Phase 10 (Error Handling)
  ↓ test-error verified
Phase 11 (Multi-Agent Dashboard)
  ↓ All features verified
Phase 12 (Cleanup)
  ✅ Done
```

**Each phase assumes all previous phases PASSED**

---

## Phase File Template

Each phase file follows this structure:

```markdown
# Phase X: Feature Name

> **Purpose**: What this tests
> **Duration**: Estimated time
> **Assumes**: Previous phase(s) PASSED
> **Output**: What should be verified

## Prerequisites
- ✅ Item 1
- ✅ Item 2

## Test Steps
### Step 1: Action
- Action: Do X
- Expected: See Y
- Verify: [ ] Z

### Step 2: Action
...

## Critical Validations
- [ ] Validation 1
- [ ] Validation 2

## Success Criteria
Phase X is PASSED when:
- ✅ Criterion 1
- ✅ Criterion 2

## Troubleshooting
Issue 1: Solution
Issue 2: Solution

## Next Phase
Phase X+1: Name
```

---

## How Sub-Agent Uses Phases

The `ui-integration-tester` sub-agent processes phases:

```python
# Pseudo-code
for phase_num in range(0, 13):
    phase_file = f"docs/testing/phases/PHASE_{phase_num:02d}_*.md"

    # Load phase instructions
    phase = load_phase(phase_file)

    # Execute test steps
    results = execute_phase(phase)

    # Check success
    if results.passed:
        print(f"✅ Phase {phase_num} PASSED")
        continue  # Next phase
    elif results.partial:
        print(f"⚠️ Phase {phase_num} BUG FOUND")
        # Document bug, continue anyway
        continue
    else:
        print(f"❌ Phase {phase_num} FAILED")
        break  # Stop, requires investigation
```

**Key Features**:
- Reads markdown phase file
- Parses step instructions
- Executes with browser automation
- Takes screenshots
- Verifies success criteria
- Logs all results

---

## Results Reporting

After each phase, records:

```markdown
## Phase X: Feature Name

**Status**: ✅ PASSED / ⚠️ BUG FOUND / ❌ FAILED
**Duration**: 10 minutes
**Timestamp**: 2025-12-09T14:30:00Z

### Tests Passed
- [x] Step 1: Action → Expected → Verified
- [x] Step 2: ...
- [x] Success criterion 1
- [x] Success criterion 2

### Issues Found
1. [Issue description if any]

### Evidence
- Screenshot: /test-screenshots/phase-X-01.png
- Log: [Any relevant logs]
- API Response: [If applicable]
```

### Aggregated Report
```markdown
# Full Test Run - 2025-12-09

**Phases Completed**: 0-5 (6 phases)
**Duration**: 1h 45min
**Success Rate**: 5/6 PASSED (83%)

## Summary
- ✅ Phase 0: PASSED
- ✅ Phase 1: PASSED
- ✅ Phase 2: PASSED
- ⚠️ Phase 3: BUG FOUND (Context stuck at 0%)
- ✅ Phase 4: PASSED
- ✅ Phase 5: PASSED

## Critical Issues
1. Context Progress Bar Stuck at 0% (Phase 3)
   - Status: Known bug
   - Impact: Users cannot see token usage
   - Resolution: Pending fix

## Recommendations
1. Fix context calculation (URGENT)
2. Continue testing with remaining phases
3. Re-run Phase 3 after fix
```

---

## Migrating from Monolithic to Modular

**Old way** (still available):
```bash
# Read everything in one file
Open docs/testing/UI_INTEGRATION_TEST.md
```

**New way** (recommended):
```bash
# Read quick start
Open docs/testing/phases/README.md

# Start testing
Follow Phase 0: PHASE_00_SETUP.md
Then Phase 1: PHASE_01_AUTHENTICATION.md
...
```

**Both still available**:
- `UI_INTEGRATION_TEST.md` - Comprehensive reference (kept for completeness)
- `phases/` - Modular structure (recommended for testing)

---

## Phase Naming Convention

All phase files follow format:
```
PHASE_{NUMBER:02d}_{NAME}.md

PHASE_00_SETUP.md
PHASE_01_AUTHENTICATION.md
PHASE_02_AGENT_CREATION.md
...
PHASE_12_CLEANUP.md
```

**Never**:
- `Phase0Setup.md` (wrong case)
- `PHASE_0_SETUP.md` (needs zero-padding)
- `PHASE_01_setup.md` (wrong case)

---

## Known Issues by Phase

| Phase | Issue | Status | Impact |
|-------|-------|--------|--------|
| 2 | Template pre-selection UI bug | Known | Minor - use API |
| 3 | **Context stuck at 0%** | **CRITICAL** | **Blocks token tracking** |
| 3 | Task indicator stuck at "—" | Under investigation | UI doesn't show tasks |
| 5 | Tab switching broken | Known | Use API for testing |

---

## Quick Reference

### Run All Phases
```bash
/run @agent-ui-integration-tester all
```

### Run Single Phase
```bash
/run @agent-ui-integration-tester phase-3
```

### Read Quick Start
```bash
cat docs/testing/phases/README.md
```

### Check Phase Dependencies
```bash
cat docs/testing/phases/INDEX.md
```

### View All Phases
```bash
ls docs/testing/phases/PHASE_*.md
```

---

## Future Enhancements

### Planned
- [ ] Complete Phase 6-12 stubs with full instructions
- [ ] Create `run_test_phases.py` automated runner
- [ ] Generate HTML test reports
- [ ] Parallel phase execution (if independent)
- [ ] Performance benchmarking per phase

### Optional
- [ ] CI/CD integration (run phases on every commit)
- [ ] Dashboard for live test results
- [ ] Video recordings of test execution
- [ ] Slack notifications on phase completion

---

## Testing Checklist

Before running phases:

- [ ] Read `phases/README.md` (2 min)
- [ ] Verify services running (Phase 0 prerequisites)
- [ ] Have GitHub PAT available (for Phase 2)
- [ ] Allocate 2-3 hours for full suite
- [ ] Close other browser tabs (cleaner testing)
- [ ] Start with Phase 0

After running phases:

- [ ] Review all phase results
- [ ] Document any bugs found
- [ ] Check if context bug exists (Phase 3)
- [ ] Verify all 8 agents use GitHub templates (Phase 2)
- [ ] Run cleanup (Phase 12)
- [ ] Generate report

---

## Version History

| Date | Change |
|------|--------|
| 2025-12-08 | Original monolithic `UI_INTEGRATION_TEST.md` created |
| 2025-12-09 | Split into 12 modular phase files |
| 2025-12-09 | Created `phases/README.md` and `phases/INDEX.md` |
| 2025-12-09 | Phases 0-5 complete, 6-12 as stubs |
| 2025-12-09 | This guide created |

---

## Help & Support

- **Quick Start**: `docs/testing/phases/README.md`
- **Full Overview**: `docs/testing/phases/INDEX.md`
- **Specific Phase**: `docs/testing/phases/PHASE_XX_NAME.md`
- **Original Comprehensive Guide**: `docs/testing/UI_INTEGRATION_TEST.md`

---

**Status**: 🟢 Modular structure ready for testing
**Last Updated**: 2025-12-09
