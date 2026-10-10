# Phase 7: Scheduling & Autonomy

> ⛔ **Retired — 2026-10-09.** Do not run this phase. The scenario runner skips it.

## Why it was retired

The phase drove a `test-scheduler` agent by typing natural-language commands into the agent Terminal tab and expected scripted replies such as "Schedule ID: sched-…". No `test-scheduler` template exists in `config/agent-templates/`, the Terminal tab is hidden for every user (`src/frontend/src/utils/agentTabs.js`), and the phase never touched the real feature — the Schedules tab and `/api/agents/{name}/schedules`. It also waited five to six minutes of wall clock for a one-time schedule.

## What covers this now

- **Phase 33** (Agent detail tabs) creates, toggles and deletes a schedule through the Schedules tab on a fixture agent.
- Autonomy (AUTO / Manual) is covered by **Phase 23** (Agent Configuration).

The January text of this phase is in git history (`git log -- docs/testing/phases/PHASE_07_SCHEDULING.md`).
