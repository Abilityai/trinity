# Feature: Persistent State Allowlist (S4, #383)

## Overview

Primitive that names which workspace paths must survive a template-level
reset. The list is materialized into `.trinity/persistent-state.yaml`
inside each agent at creation time and read from disk thereafter, so
runtime sync/reset paths never need to re-read `template.yaml`.

> **Status**: primitive only. The reset-preserve-state operation that
> consumes this allowlist is tracked in abilityai/trinity#384 (S3).
> PROTECTED_PATHS / EDIT_PROTECTED_PATHS delete/edit protection is
> intentionally unchanged here.

## User Story

As a Trinity operator, when I recover from a parallel-history deadlock
(proposal §P2), I want a named list of workspace files that the reset
operation will restore — so I don't have to SSH in and hand-craft a
backup/restore each time.

---

## Why materialize at creation time

`template.yaml` is fetched once at agent creation and cached for 10
minutes (`services/template_service.py::_get_cached_metadata`). Sync and
reset paths run later, inside the agent container, where the template
isn't accessible. The allowlist therefore lives in two places by design:

- `template.yaml::persistent_state` — source-of-truth default, read at
  creation only.
- `.trinity/persistent-state.yaml` inside the agent — authoritative
  runtime source; operators may edit it per-agent.

Same split as `.env` / `.env.example`.

---

## Default allowlist

```yaml
persistent_state:
  - workspace/**
  - .trinity/**
  - .mcp.json
  - .claude.json
  - .claude/.credentials.json
```

Defined once in `src/backend/services/git_service.py::DEFAULT_PERSISTENT_STATE`
and mirrored (byte-exact, drift-tested) in
`docker/base-image/agent_server/routers/files.py::_DEFAULT_PERSISTENT_STATE`.

---

## Flow 1: Materialization at agent creation

### Trigger

Normal `POST /api/agents` flow (or `POST /api/system/deploy`). Runs after
the container is created and git config is registered — the call is
non-fatal so a slow container start doesn't block agent creation.

### Backend (`src/backend/services/agent_service/crud.py:622-641`)

```python
# After container start, git config registration:
persistent_state = (
    (template_data or {}).get(
        "persistent_state", git_service.DEFAULT_PERSISTENT_STATE
    )
)
try:
    await git_service.materialize_persistent_state(
        config.name, persistent_state
    )
except Exception as e:
    logger.warning(
        f"[S4] Failed to materialize persistent-state.yaml for "
        f"{config.name}: {e}"
    )
```

### Template surfacing (`src/backend/services/template_service.py::_build_template`)

`_build_template()` now includes `persistent_state` in the returned dict,
reading `metadata.get("persistent_state", DEFAULT_PERSISTENT_STATE)`. The
import is lazy to avoid circular-import risk with `git_service`.

### Writer (`src/backend/services/git_service.py::materialize_persistent_state`)

Writes YAML via a single `execute_command_in_container` call. Command
shape (pinned by `test_default_allowlist_materialized_when_template_missing_key`):

```bash
bash -c "mkdir -p /home/developer/.trinity && \
  cat > /home/developer/.trinity/persistent-state.yaml <<'PSTATE_EOF'
persistent_state:
- workspace/**
- .trinity/**
- .mcp.json
- .claude.json
- .claude/.credentials.json
PSTATE_EOF"
```

The heredoc quotes preserve glob characters verbatim.

---

## Flow 2: Runtime read

### Backend helper (`src/backend/services/git_service.py::_persistent_state_for`)

For platform-side consumers (e.g. the future reset endpoint):

```python
result = await execute_command_in_container(
    container_name=f"agent-{agent_name}",
    command='bash -c "cat /home/developer/.trinity/persistent-state.yaml '
            '2>/dev/null || true"',
    timeout=5,
)
```

Falls back to a fresh copy of `DEFAULT_PERSISTENT_STATE` when the file is
missing, empty, or malformed, or when the `persistent_state:` key is
absent.

### Agent-server helper (`docker/base-image/agent_server/routers/files.py::_read_persistent_state`)

Pure-filesystem read inside the container:

```python
_PERSISTENT_STATE_PATH = Path("/home/developer/.trinity/persistent-state.yaml")

def _read_persistent_state() -> list[str]:
    if not _PERSISTENT_STATE_PATH.exists():
        return list(_DEFAULT_PERSISTENT_STATE)
    try:
        data = yaml.safe_load(_PERSISTENT_STATE_PATH.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return list(_DEFAULT_PERSISTENT_STATE)
    ...
```

Same fallback rules.

---

## Data Layer

No DB schema changes. Everything lives as files:

| Location | Purpose | Edited by |
|----------|---------|-----------|
| `template.yaml` (repo root / agent template repo) | Default list | Template author |
| `.trinity/persistent-state.yaml` (inside each agent) | Runtime source of truth | Materializer + operators |

---

## Side Effects

- A tiny (~6-line) YAML file is written into every newly created agent
  container. No resource-quota impact.
- Failure to materialize is logged at WARN and swallowed — readers fall
  back to `DEFAULT_PERSISTENT_STATE` so the system stays safe even if the
  file was never written (slow container, exec error, etc.).

---

## Error Handling

| Condition | Behaviour |
|-----------|-----------|
| Container slow to start; `docker exec` fails | WARN log, agent creation succeeds, reader falls back to defaults |
| `yaml.safe_load` errors on the on-disk file | Reader returns defaults, no exception |
| `persistent_state` key absent in the YAML | Reader returns defaults |
| `persistent_state: []` empty list | Treated as "use defaults" (matches test `test_agent_server_reader_defaults_on_empty_list`) |

---

## Testing

Seven + seven unit tests in `tests/unit/`:

- `test_persistent_state_allowlist.py` — backend helpers
  - Default constant pinned to the five-pattern spec.
  - Materializer writes exactly one heredoc exec call with the YAML body
    round-tripping to `{"persistent_state": [...]}`.
  - Template override suppresses defaults.
  - Reader returns on-disk list when present.
  - Reader falls back to defaults on missing file, invalid YAML, or
    missing `persistent_state:` key.
  - Fallback returns a fresh list (no shared-constant aliasing).
- `test_persistent_state_reader.py` — agent-server helper
  - Mirror-matches-source drift guard (catches if the agent-server
    helper and the test mirror diverge).
  - PROTECTED_PATHS / EDIT_PROTECTED_PATHS byte-exact snapshot — this
    PR is scoped to add a reader, not to change protection semantics.
  - Reader default-fallback / on-disk-value / invalid-YAML / missing-key
    / empty-list cases.

Run locally:

```bash
python -m pytest tests/unit/test_persistent_state_allowlist.py \
                 tests/unit/test_persistent_state_reader.py -v
```

End-to-end verification (optional, once S0/#387 lands):

1. Create an agent via gitea harness.
2. `docker exec agent-<name> cat /home/developer/.trinity/persistent-state.yaml`
   → returns the default YAML.
3. Edit the file inside the container.
4. `_persistent_state_for("<name>")` returns the edited list.

---

## Out of Scope (Explicit)

Listed so future changes land in the right PR:

- Reset-preserve-state subroutine / API endpoint — #384 / S3.
- Snapshot / restore endpoints — S3.
- `.trinity/last-remote-sha/<branch>` storage — S7.
- Any change to `PROTECTED_PATHS` / `EDIT_PROTECTED_PATHS` semantics —
  `test_protected_paths_lists_unchanged` pins the current lists; the
  first PR to touch delete/edit protection has to update that snapshot.

---

## References

- Upstream issue: [abilityai/trinity#383](https://github.com/abilityai/trinity/issues/383)
- Proposal: `ability-trinity-git-improvements-proposal.md` §S4 (keystone primitive)
- Epic: abilityai/trinity#381
- Related (downstream): abilityai/trinity#384 (S3 reset-preserve-state)
- Related (ancillary): github-sync, github-repo-initialization, agent-lifecycle
- PR: [abilityai/trinity#394](https://github.com/Abilityai/trinity/pull/394)
