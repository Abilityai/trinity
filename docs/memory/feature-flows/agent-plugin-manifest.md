# Feature: Agent Plugin Manifest — declared, committed, self-healing (#1704)

## Revision History

| Date | Changes |
|------|---------|
| 2026-08-16 | Template-declared `plugins:` block → committed `~/.trinity/plugins.yaml` + boot re-install hook. Runtime-install distill + commit-pinned mode deferred. |

## Overview

Claude Code records an agent's installed marketplace plugins in `~/.claude.json`
(identity + session state + secrets) and copies each plugin's files into
`~/.claude/plugins/cache/<plugin>@<ver>/` (the cache). Both are gitignored —
`.claude.json` correctly, `.claude/plugins/` by #1705 (repo bloat, the #1596
class). Trinity actively steers agents onto plugins at onboarding
(`/plugin marketplace add abilityai/abilities`, `/plugin install trinity@abilityai`),
so losing that selection matters.

**Reframe (the decisive finding).** The issue's literal premise — "a container
recreate loses plugins" — is **not reproducible from the current code**. HOME
(`/home/developer`) IS the durable `agent-{name}-workspace` volume; no recreate
path removes it (`recreate_container_with_updated_config` /
`recreate_missing_container` both reuse it), and startup.sh preserves untracked
files (`.git` present → skip clone). So a plain recreate keeps both the manifest
and the cache. The genuinely-unprotected surface is a **git-based
reconstitution** into a fresh/empty volume or a **new host**, where the
gitignored files are exactly what a clone drops:

- **Agent move / migration** — the #1169 "move an agent" model exports `data/`
  only, never `.claude*`.
- **Fresh-volume rebuild** — after the #834/#1581 hard-purge removes the volume.

#1705 completed this gap by removing the last incidental crutch: before it, the
un-ignored cache was auto-committed, so a fresh clone *accidentally* restored the
plugins. After it, plugin loss on the reconstitution/move path is complete.

The mechanism: make the plugin selection a **first-class, declared, committed,
secret-free, self-healing** piece of agent config — which also trivially covers
recreate/reset/move. This is the **agent-local half** of the incubating global
plugin-management model (trinity-enterprise#192): the same normalized shape a
future per-agent assignment surface would materialize, reconciled on start.

## Requirement Reference

- **Requirement**: §42 Agent Plugin Manifest (`content-files.md`)
- **GitHub Issue**: #1704 (public `abilityai/trinity`, P2, `type-bug`, `theme-infrastructure`)
- **Epic coordination**: trinity-enterprise#192 (global plugin management, incubating)
- **Status**: ✅ template-declared half implemented 2026-08-16 · runtime distill deferred
- **Interacts with**: #1705 (kept intact — cache stays gitignored), #2070 (`_TRINITY_AUTHORED_PATHS`)

## User Story

As an agent author, I want to declare which Claude Code plugins my agent should
have in `template.yaml`, so that the selection is committed, portable, and
re-installed automatically when the agent is reconstituted from git onto a fresh
volume or a new host.

## Entry Points

- **Template**: `plugins:` block in `template.yaml`
- **Materialization**: `git_service.materialize_plugins(name, plugins)` → `~/.trinity/plugins.yaml`
- **Boot hook**: `python3 -m agent_server.plugins_reinstall` (from `startup.sh`)

---

## Manifest shape (`template.yaml plugins:`)

```yaml
plugins:
  marketplaces:
    - name: abilityai
      source: abilityai/abilities        # owner/repo shorthand OR an https:// URL (no userinfo)
  installed:
    - trinity@abilityai                    # plugin@marketplace
  # enabledPlugins:                         # alternative, mirrors Claude's settings.json
  #   trinity@abilityai: true               #   value:false entries are dropped
```

Materialized (nested, `sort_keys=True` for byte-stability) to `~/.trinity/plugins.yaml`:

```yaml
plugins:
  installed:
  - trinity@abilityai
  marketplaces:
  - name: abilityai
    source: abilityai/abilities
```

## Architecture

```
Declaration (creation time)
  template.yaml: plugins → template_service._template_plugins  (normalized + errors)
                            → both catalog builders surface plugins + plugin_errors
    crud._resolve_template → _TemplateResolution.declared_plugins  (all 3 resolver branches:
        github source metadata / local template_data / copy snapshot — the declared_schedules shape)
    crud._materialize_agent_files (opt-in, ghost-skipped, non-fatal in the rollback fence)
        → git_service.materialize_plugins(name, plugins)
            → _write_trinity_yaml_file (shared injection-safe single-quoted heredoc)
            → ~/.trinity/plugins.yaml

Commit (on Push / auto-sync)
  .trinity/plugins.yaml ∈ _TRINITY_AUTHORED_PATHS
    → #2070 `!` re-include (:1325)  +  git rm --cached exemption (_build_rm_cached_ignored_command)
    → COMMITTED  (unlike volume-local persistent-state.yaml / data-paths.yaml)
    → .claude.json + .claude/plugins/ stay gitignored (#1705 intact)

Self-heal (container boot — startup.sh, AFTER credential injection)
  python3 -m agent_server.plugins_reinstall
    load_manifest(~/.trinity/plugins.yaml)  [hardened parse: AliasPolicy.REJECT + re-charset-validate]
    _read_marketplaces()  ← claude plugin marketplace list --json
    _read_installed()     ← claude plugin list --json
    for each declared-but-missing marketplace → claude plugin marketplace add <source>
    for each declared-but-missing plugin      → claude plugin install <plugin>@<mkt> [--yes]
    (`--yes` feature-detected once via `claude plugin install --help` — #2305: 2.1.227
     rejects the flag, 2.1.235+ requires it for non-TTY command-installs)
    (zero subprocesses when all present; GH_TOKEN seeded from GITHUB_PAT env; timeout + stdin=DEVNULL; non-fatal)
```

## Three-Layer Split (Invariant #1)

| Layer | Module | Responsibility |
|-------|--------|----------------|
| Reader (leaf) | `services/template_plugins.py` | Total, never-raises normalizer (`normalize_declared_plugins` / `plugin_shape_errors`) — mirrors `template_schedules.py`. Charset/source validation, dedup, sort. |
| Materialize | `services/git_service.py` | `materialize_plugins()` (nested YAML via `_write_trinity_yaml_file`); `_TRINITY_AUTHORED_PATHS += .trinity/plugins.yaml`. |
| Create wiring | `services/agent_service/crud.py` | `_TemplateResolution.declared_plugins` (3 branches) + `_materialize_agent_files` call. |
| Boot hook (agent) | `docker/base-image/agent_server/plugins_reinstall.py` | Reconcile declared vs installed; `startup.sh` invocation. |

## Security

- **Untrusted manifest.** `plugins.yaml` is on the agent-writable volume — parsed
  with the ent#314 hardened loader (size cap + `AliasPolicy.REJECT`) on the agent
  side too, and every name AND the marketplace `source` re-charset-validated at
  both the backend boundary and the boot hook.
- **The `source` is the dangerous argument** (it points where `marketplace add`
  fetches from): `owner/repo` or `https://` with no `user:token@` userinfo
  (refused via `redact_url_userinfo`), no traversal, no leading `-`
  (argument injection). Passed as subprocess arg-lists, never a shell string.
- **Secret-free.** A private marketplace's git credential comes from the agent's
  `GITHUB_PAT` env at hook time (seeded as `GH_TOKEN`), never the manifest. The
  hook runs AFTER credential injection in startup.sh for exactly this reason.
- **Never hangs / never fatal.** Every subprocess is `timeout`-bounded with
  `stdin=DEVNULL` (a no-TTY prompt hangs), `install` passes `--yes` only when
  the CLI's own `--help` advertises it (#2305 — the flag does not exist in
  every CLI version, and an unadvertised flag withheld EVERY install), and any
  failure is `withheld:<reason>` (the #1929 contract), startup continues.

## Determinism (a correctness property)

`materialize_plugins` uses `sort_keys=True` and the normalizer sorts +
de-duplicates both lists, so a stable plugin set produces a **byte-identical**
manifest. Without this the 15-min auto-sync loop would re-commit a churning file
(a repo-bloat regression, the #1596 class).

## Honest scope / known limitations

- **Runtime-install distill deferred.** Plugins installed after creation (runtime
  `/plugin install`, not in the template) are not captured — that needs an
  agent-side distill of Claude's own `known_marketplaces.json` + `enabledPlugins`,
  whose shapes are undocumented and version-drifting, and may be subsumed by #192.
- **Cornelius / tokenless source-mode agents** cannot push a materialized
  `.trinity/plugins.yaml` back to git, so the boot hook **falls back to reading
  the `template.yaml plugins:` block** the re-cloned template carries (`load_manifest`
  → `_read_plugins_block(template.yaml, BUDGET)`; startup.sh's guard fires on the
  manifest OR a top-level `plugins:` key, so it is reachable). Otherwise their
  plugins survive by volume + boot re-install.
- **Supply chain.** `plugin@marketplace` pins identity, not a commit — a
  re-install re-fetches the marketplace's current content (the #192
  `auto_update: on` behaviour); a commit-pinned (`auto_update: off`) mode is a
  documented follow-up.
- **CLI-shape dependence.** The boot hook reads `claude plugin [marketplace] list
  --json`; these shapes are undocumented (#1704 Step 0). The extractor is
  tolerant, but a wrong shape reads as "already installed" and silently installs
  nothing — so the exact shape MUST be pinned to a real capture in the E2E, and
  the hook emits a `declared N / installed M / skipped K` summary line so an inert
  reconcile is observable, not silent.

## Testing

### Prerequisites
- Backend running; a template declaring a `plugins:` block.
- A rebuilt base image (old images silently skip the boot hook).

### Test Steps
1. **Materialization** — create an agent from a template declaring `plugins:`.
   **Expected**: `~/.trinity/plugins.yaml` is present with the normalized nested
   shape. **Verify**: `docker exec agent-<name> cat /home/developer/.trinity/plugins.yaml`.
2. **Commit** (writable agent) — Push / auto-sync.
   **Expected**: `.trinity/plugins.yaml` is tracked and pushed; `.claude.json` and
   `.claude/plugins/` are NOT. **Verify**: `git ls-files | grep .trinity/plugins.yaml`
   and confirm the two gitignored paths are absent.
3. **Fresh-volume reconstitution** — clone the agent's repo into an empty volume /
   new host and boot. **Expected**: the boot hook re-installs the declared
   marketplaces + plugins. **Verify**: container log `[plugins-reinstall]` lines +
   `claude plugin list`.
4. **Idempotent restart** — restart a volume-persisting container.
   **Expected**: the hook runs **zero** installs (all present). **Verify**: the
   summary line reports `installed 0, skipped N`.

## Verification / gotchas

- **verify-local's agent stage is BLIND to the git-sync branch** — it boots
  `local:test-echo` (no `GITHUB_REPO`), so `configure_push_remote` and any
  git-sync-gated hook never run. To prove the boot hook, extract/exercise
  `python3 -m agent_server.plugins_reinstall` **inside the built image** against a
  fixture manifest + a **fake `claude` on PATH**, and assert the install arg list
  matched (an empty match passes forever). Tie image↔branch via
  `sha256sum /app/agent_server/plugins_reinstall.py` vs the source.
- **Never `--skip-agent`** — this touches `startup.sh` + agent-server.
- **Never touch `.gitignore` from startup.sh** (#953) — the committable manifest
  goes through `_TRINITY_AUTHORED_PATHS` only.

## Related Flows

- [agent-data-volumes.md](agent-data-volumes.md) — #1169 `data_paths` (the materialize-at-creation precedent)
- [persistent-state-allowlist.md](persistent-state-allowlist.md) — #383 S4 (the `.trinity/*.yaml` primitive)
- [github-sync.md](github-sync.md) — the Push / gitignore-merge / rm-cached machinery
