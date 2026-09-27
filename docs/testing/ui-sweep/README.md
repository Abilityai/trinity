# UI Sweep — reports and cursor

This folder is written by the `/ui-sweep` skill (`.claude/skills/ui-sweep/`, core-team submodule), the driver for click-through UI testing of a running Trinity instance.

- `state.json` — the round-robin cursor (which lane and slice runs next), phases flagged for refresh, and every finding keyed by fingerprint (first/last seen, severity, issue number, streak). Written only by the skill; never hand-edited mid-run.
- `<date>-<lane>-<slice>.md` — one dated report per run, in the shape of `docs/archive/testing/exploratory-e2e-report-2026-08-14.md`. History lives in git.

Lanes: `scenarios` (a scripted phase from `docs/testing/phases/`), `explore` (unscripted sweep of one UI area), `edges` (boundary and hostile input probing of one area), `refresh` (bring one stale phase file back in line with the code).
