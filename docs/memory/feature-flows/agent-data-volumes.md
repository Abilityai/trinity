# Feature: Agent Runtime Data — `data_paths` + Snapshot/Export (#1169)

## Revision History

| Date | Changes |
|------|---------|
| 2026-06-20 | PR1: declaration (`data_paths`) + on-demand export/import over the existing durable home volume. Schema-free, no separate volume. PR2 (scheduled snapshot + SQLite quiesce + retention + rename/purge cascade) deferred. |

## Overview

Agents accumulate runtime data — SQLite DBs, datasets — that can't live in the git-synced template repo (bloat) yet must survive container lifecycle events and be portable to another Trinity instance. The key finding behind this design: `/home/developer` is **already** a persistent named Docker volume (`agent-{name}-workspace`) that survives container recreate, image upgrade, template pulls (a merge or rebase) and a reset to the template's main (`git reset --hard origin/main`, never `git clean -fdx`), and subscription auto-switch. So data under `/home/developer/data` is **already durable** today.

The issue's original premise (a separate `agent-{name}-data` volume for durability) was therefore dropped on verified evidence. The feature reduces to the genuine gap: a **declaration** (`data_paths`) plus a real **snapshot/export/import** capability over the existing volume — **no separate volume, no platform schema change** (snapshots are filesystem artifacts; audit rides `audit_log`, which keeps #1169 decoupled from the in-flight SQLite→Postgres migration #1183).

## Requirement Reference

- **Requirement**: §41 Agent Runtime Data — `data_paths` + Snapshot/Export (#1169)
- **GitHub Issue**: #1169 (public `abilityai/trinity`, P1, `type-feature`, `theme-infrastructure`)
- **Status**: ✅ PR1 implemented 2026-06-20 · PR2 deferred
- **Pillar**: III (Persistent Memory / Portability)

## User Story

As an agent owner, I want to declare which directories hold my agent's runtime data and export/import that data on demand, so that the data survives the container lifecycle, stays out of git, and can travel with the agent when I move it to another Trinity instance.

## Entry Points

- **Template**: `data_paths:` (list of globs under `data/`) in `template.yaml`
- **Owner API**: `POST /api/agents/{name}/data/export` (`?format=stream|base64`), `POST /api/agents/{name}/data/import`
- **MCP tools**: `export_agent_data({ name })`, `import_agent_data({ name, tar_base64 })`
- **Agent-server primitive (reused)**: `POST /api/agent-server/restore` (traversal-guarded restore)

---

## Architecture

```
Declaration (creation time)
  template.yaml: data_paths → template_service._build_template/_build_local_template
    → crud.py → git_service.materialize_data_paths(name, paths)
        ├─ writes ~/.trinity/data-paths.yaml   (key data_paths, quoted heredoc)
        └─ appends data/ + entries to the agent's own .gitignore (grep -qxF)

Export (POST /api/agents/{name}/data/export)
  owner/admin → per-agent Redis op lock (agent:data_op:{name})
    → container_get_archive("/home/developer/data")   [workspace never mounted]
    → stream chunks → temp file /data/agent-data-tmp/*.tar  (cap → 413)
    → append manifest.json (data-paths + agent/version)
    → StreamingResponse  (BackgroundTask deletes temp)
       or, ?format=base64 → JSON {tar_base64} up to the inline cap

Import (POST /api/agents/{name}/data/import)
  owner/admin → Idempotency-Key dedup → per-agent op lock
    → proxy multipart tar to agent-server POST /api/agent-server/restore
        with paths=["data/**"]  →  restore_from_tar enforces allowlist + traversal guard
    → {restored, skipped}
```

## Three-Layer Split (Invariant #1)

| Layer | Module | Responsibility |
|-------|--------|----------------|
| Router | `routers/agent_data.py` | Auth (`OwnedAgentByName`), op lock, size cap, manifest, streaming, audit, idempotency |
| Service (reused) | `services/git_service.py` | `materialize_data_paths` / `_data_paths_for` (+ the shared `materialize_trinity_yaml_list`/`_read_trinity_yaml_list` primitive extracted from S4) |
| Service (reused) | `services/docker_utils.py` | `container_get_archive` export transport |
| Service (reused) | `services/agent_service/helpers.py` | `agent_http_request` proxy to the agent-server restore primitive |
| Agent-server (reused) | `docker/base-image/agent_server/routers/snapshot.py` | `restore_from_tar` — `data/**` allowlist + `..`/absolute traversal guard |

## Design Decisions

- **No separate volume** — the home volume already provides durability; a separate volume bought near-zero marginal durability.
- **Schema-free** — decouples from the SQLite→Postgres migration (#1183). Snapshots are files; audit rides `audit_log`.
- **DRY** — `persistent_state` (S4) and `data_paths` share one `materialize_trinity_yaml_list`/`_read_trinity_yaml_list` primitive; the heredoc delimiter is parameterized so persistent_state keeps its `PSTATE_EOF` marker (existing #383/#384 tests are the regression guard and stay green).
- **Per-agent `.gitignore`** — declared paths are appended to the *agent's own* `.gitignore` (only when `data_paths` is declared), never the fleet-wide `_GITIGNORE_PATTERNS` (which would untrack committed `data/` files across the fleet).
- **Cross-worker op lock** — a Redis SETNX lock (not the in-process `agent_switch_lock`) so export/import serialize across uvicorn workers; fail-open if Redis is down.
- **Export is a read** — naturally idempotent; accepts `Idempotency-Key` for contract consistency but creates no execution. Import (mutating) is deduped through `idempotency_service`.

## Failure Modes

| Codepath | Failure | Handled |
|----------|---------|---------|
| export | multi-GB data → OOM | stream → temp file + `AGENT_DATA_EXPORT_MAX_BYTES` cap → 413 |
| export | stopped agent (no container) | 409 with a clear message |
| export | missing `data/` dir (declared but unused) | manifest-only tar (valid, importable no-op), not a 500 |
| export (base64) | data > inline cap | 413 directing to the streaming download |
| import | path traversal in tar | `restore_from_tar` skips `..`/absolute entries (reported in `skipped`) |
| export/import | concurrent op | per-agent Redis op lock → 409 |

## Not in Scope (PR2 / follow-ups)

- Scheduled background snapshots + retention (`agent_data_snapshot_service.py`).
- `~/.trinity/pre-snapshot` SQLite-quiesce hook (`sqlite3 .backup` staging copy → consistent point-in-time tar, eliminating the hook-vs-tar race).
- Rename/purge snapshot-dir cascade.
- The pre-existing fleet-wide home/public/shared **volume leak-on-purge** + **strand-on-rename** (separate bug).
- System-agent `data_paths` (system agents have no public/shared volumes; `.trinity` is reset).

## Tests

- `tests/unit/test_data_paths_allowlist.py` — shared helper round-trip; `materialize_data_paths` writes yaml + gitignore; empty = no-op; reader fallbacks.
- `tests/unit/test_data_paths_gitignore.py` — real-bash idempotent `.gitignore` append.
- `tests/unit/test_data_paths_template_surface.py` — `data_paths` surfaced by both template builders (default `[]`).
- `tests/unit/test_agent_data_export.py` — cap-drain, manifest append, **export↔import tar round-trip through the real `restore_from_tar`** (data restored, manifest skipped, traversal rejected), op-lock contention + fail-open.
- Regression guard: `tests/unit/test_persistent_state_allowlist.py` / `test_persistent_state_reader.py` / `test_github_init_gitignore.py` stay green after the shared-helper extraction.
