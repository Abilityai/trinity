# Phase 8: Execution Queue

> ⛔ **Retired — 2026-10-09.** Do not run this phase. The scenario runner skips it.

## Why it was retired

The phase drove a `test-queue` agent and asserted chat replies such as "Job queued… Position in queue" and "Current throughput". No `test-queue` template exists, and those replies were the fixture's own text, not a product surface. The real queue surface (`GET /api/agents/{name}/queue` and the Tasks tab) was never exercised, and the assertions depended on about ninety seconds of timing.

## What covers this now

- **Phase 3** (Tasks & Chat) and **Phase 20** (Live Execution Streaming) cover running a task, the live state and the execution detail page.
- **Phase 31** (Operations) covers the fleet-wide executions list.
- Queue admission and concurrency are backend behaviour, pinned by the API and unit suites under `tests/` — a click-through cannot assert them reliably.

The January text of this phase is in git history (`git log -- docs/testing/phases/PHASE_08_EXECUTION_QUEUE.md`).
