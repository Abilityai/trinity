# Architecture Decision Records

This directory holds **Architecture Decision Records (ADRs)** — durable write-ups of significant technical decisions, the alternatives weighed, and the rationale for the chosen path.

Each ADR is numbered (`NNNN-slug.md`) and stays as the historical record of a decision even when the decision is later reversed; revisions get a new ADR that links back, rather than rewriting history.

**When to write one:** before changing a load-bearing piece of architecture — runtime engines, persistence layers, auth flows, transport protocols, large-scale refactors. If you'd want a future engineer to know *why* a choice was made (not just *that* it was made), it belongs here. For day-to-day implementation plans use `docs/planning/`; for invariants and the current shape of the system, see `docs/memory/architecture.md`.

| # | Title | Status |
|---|---|---|
| 0001 | [Claude Agent SDK migration evaluation](0001-claude-agent-sdk-migration.md) | Proposed |
