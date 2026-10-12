# Phase 16: Web Terminal

> ⛔ **Retired — 2026-10-09.** Do not run this phase. The scenario runner skips it.

## Why it was retired

The phase's premise is inverted. It expected a Terminal tab that is the default and "replaces the legacy Chat tab". Today Chat is a primary tab, Overview is the default, and the Terminal tab is hidden for every user (`src/frontend/src/utils/agentTabs.js`, marked deprecated). The terminal websocket still exists on the backend, but no UI path reaches it.

## What covers this now

- **Phase 3** (Tasks & Chat) and **Phase 21** (Session Management) cover talking to an agent from its detail page.
- **Phase 29** (Workspace) covers the main chat surface.

If the Terminal tab is ever restored, write a new phase rather than reviving this one.

The January text of this phase is in git history (`git log -- docs/testing/phases/PHASE_16_WEB_TERMINAL.md`).
