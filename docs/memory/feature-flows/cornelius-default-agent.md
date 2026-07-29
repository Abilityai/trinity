# Feature: Default Cornelius Agent — Auto-Seed on Fresh Install (trinity-enterprise#107)

## Overview
A fresh Trinity install auto-seeds a default **"Cornelius"** second-brain agent with the **Brain Orb enabled**, so a first-run operator lands on a working knowledge-graph agent out-of-the-box — no manual create/clone. The seed is **first-run-only** and **fresh-install-scoped**: it never re-provisions a Cornelius an operator has deleted, and it never fires on an established fleet that already has agents. The Brain Orb was already fully OSS (flag-gated, not entitlement-gated), so nothing needed de-gating — this feature only adds the auto-provision seam (Path B: a local bundled template).

## User Story
As a first-time Trinity operator, I want a ready-to-use second-brain agent with a rendered Brain Orb the moment I finish setup, so I can see the platform's flagship capability without learning template syntax or cloning a repo first.

## Entry Points

> **ent#124 (2026-07-24):** both call sites now go through the first-run
> orchestrator `services/system_seed_service.py::ensure_first_run_seeded()`,
> which resolves ONE persisted freshness verdict (`first_run_fresh` — with
> `cornelius_seeded=="true"` forcing not-fresh) and passes it to
> `ensure_seeded(fresh=...)` before ALSO seeding the default starter fleet.
> The injected verdict replaces this service's internal
> `count_non_system_agents()` check (step 2) so fleet-seeded agents can't
> flip a genuinely-fresh install to "not fresh" on a retry pass;
> `fresh=None` preserves the legacy internal count. Cornelius behavior is
> otherwise unchanged. See `system-manifest.md` → First-Run Default Seed.

- **Setup-completion handler** (fresh installs): `src/backend/routers/setup.py` — right after the admin account is created, schedules `ensure_first_run_seeded()` (Cornelius + starter fleet) as a FastAPI `BackgroundTask` (so setup returns immediately; provisioning runs off the request path).
- **Lifespan safety-net** (upgrades / missed BackgroundTask): `src/backend/main.py` — on boot, gated on `setup_completed`, backgrounds `ensure_first_run_seeded()` via `asyncio.create_task(...)` with a module-level strong task reference (asyncio GC footgun).
- **No operator-facing API change**: no new endpoint, no UI trigger. Provisioning is a platform boot/setup side effect.

## Source Template
`github:Abilityai/cornelius` — the **public upstream repo**, cloned anonymously (source-mode, no PAT) on the trinity-enterprise#123 tokenless path. It carries:

- `template.yaml` with `capabilities: [brain-orb]` (the per-agent gate the Brain Orb route/tab enforce)
- `CLAUDE.md` — agent instructions
- `.trinity/brain-orb/` convention hooks (`scopes`, `scope`, `search`, `action`) so scope control / KB search / writes work
- `resources/agent-visualization/data.json` — a **pre-generated seed graph** so the orb renders immediately on first visit (before the agent has produced its own export)
- `resources/local-brain-search/` — the FAISS semantic tier, so the `semantic_search` capability is real rather than falling back to the keyword scan
- the full `Brain/` vault (~1,000 notes), whose MOC wikilinks resolve and which the seed graph was exported from

**This was a vendored `config/agent-templates/cornelius/` snapshot until #1656.** The bundle was chosen because the dynamic-github create branch then hard-required a system PAT even for a public repo; trinity-enterprise#123 removed that constraint. Vendoring had directly caused two shipped first-run defects — #1646 (declared ~24 skills / 9 sub-agents / a FAISS tier the bundle didn't carry) and #1656 (22 of 23 MOC wikilinks dead, `README.md` advertising ~1,000 notes over a 19-file vault) — both the same failure: a snapshot drifting from the prose describing it. Seeding from source retires that class; template freshness/validation is #1655's job.

**No offline fallback, deliberately.** A bundled fallback only fires on a transient clone failure, and on that path it would seed the known-degraded copy *and* burn the durable `cornelius_seeded` flag — permanently stranding the install on the worse artifact. Leaving the flag unset and retrying next boot (gate 5 below) is strictly better. There is no air-gapped install to protect either: building the agent base image requires apt/npm/Go downloads, and an agent with no route to the Anthropic API is inert regardless.

## Backend Layer
**Service**: `src/backend/services/cornelius_agent_service.py` — `CorneliusAgentService.ensure_seeded()`.

Gates, in order (all fail-open / idempotent — a skip or error never blocks boot or setup):

0. **Docker gate** — `docker_client is None` (demo mode / no Docker) ⇒ skip; the flag is left unset.
1. **Already-seeded gate** — durable `cornelius_seeded` system-setting flag set ⇒ no-op. Set at the END of a successful seed, so the whole operation is idempotent across restarts and an operator who **deletes** Cornelius is **not** re-provisioned.
2. **Fresh-install gate** — `db.count_non_system_agents() > 0` ⇒ skip **and converge the flag** (marks seeded without provisioning) so an upgrade of an established fleet is never surprised by a new agent appearing and we stop re-checking every boot.
3. **Owner-exists gate** — `db.get_user_by_username('admin')` absent ⇒ **defer WITHOUT setting the flag**. On a truly-fresh pre-setup boot the admin row doesn't exist yet, so this skip lets the setup-completion trigger (or a later boot) retry — this is *why* there are two triggers rather than a lifespan-only hook.
4. **Cross-worker lock** — Redis `SETNX cornelius:provision` (fail-open, mirrors the #1464 monitoring leader-lock) so `--workers 2` boots can't both provision. If the lock isn't acquired, the other worker owns it.
5. **Provision** — creates the agent from `github:Abilityai/cornelius` via the ordinary `create_agent_internal(...)`: an anonymous, source-mode clone with no PAT (trinity-enterprise#123). `AgentConfig.source_mode` defaults `True`, which that path requires — `_gate_tokenless_request` 400s a non-source-mode tokenless request. A clone failure (GitHub unreachable, rate-limited) leaves the flag **unset** and retries on the next boot, the same convergence the `create_blocked` path below relies on. `create_agent_internal`'s `request` param was widened to `Optional[Request] = None` because the boot-time caller has no HTTP request (the param was never dereferenced). A `409` converges the flag **only when the agent is actually there** (#1790). The create path raises the same status for conditions under which nothing was created — a leftover unclaimed workspace volume (#1667, what `docker compose down -v` then re-setup leaves behind), volumes still owned by a renamed agent (#1664), a fork destination already bound — so the handler re-checks with the create path's own claim predicate (`crud.agent_name_is_taken`, shared rather than re-derived so the two cannot drift). Nothing claiming the name ⇒ flag left **unset**, result `create_blocked`, next boot retries; the usual cause is reclaimed unattended by the #1581 orphan-volume sweep, so the retry converges without intervention. The probe fails toward retry: a wrong "absent" costs one redundant create attempt, a wrong "present" burns the durable flag and strands the install permanently Cornelius-less while reporting seeded.
6. **Existence-guarded flag enable** — turns on the `brain_orb_enabled` platform flag **only when unset**, so it never clobbers an admin who explicitly set it OFF.
7. **Mark done** — write the `cornelius_seeded` flag. Only a *successful* provision sets it; a genuine failure leaves it unset for the next boot to retry.

**New DB accessor**: `db.count_non_system_agents()` (`db/agents.py` + `database.py` facade) — the fresh-install signal.

## Data Layer
- No DB migration. Both `cornelius_seeded` and `brain_orb_enabled` live in `system_settings` (free-form KV).
- The provisioned agent rows are created by the standard `create_agent_internal` path (`agent_ownership`, container, etc.) — nothing Cornelius-specific in the schema.

## Error Handling
- Every trigger backgrounds `ensure_seeded()` and swallows exceptions (logged) — a provisioning failure (e.g. Docker briefly unreachable) leaves `cornelius_seeded` **unset**, so the next boot's lifespan safety-net retries. Setup never fails because Cornelius didn't seed.
- Concurrent workers: the SETNX lock means only one attempts provisioning; the loser no-ops.

## Known Deviation (AC5 — local bundle, no upstream origin)
Because the default Cornelius is a **LOCAL bundle** (not github-native), it has **no git `origin`** — it will **not** auto-`git pull` upstream template updates the way a `github:` / fork-to-own agent does. Durable, upstream-tracking ownership of the default Cornelius is deferred to **fork-to-own** (trinity-enterprise#109). This is the accepted Path-B tradeoff: instant, offline, credential-free first-run provisioning in exchange for no automatic upstream sync.

## Related Flows
- [brain-orb.md](brain-orb.md) — the capability-gated Brain Orb page this agent lights up
- [fork-to-own](../requirements/core-agent.md) (§4.4) — the durable-ownership path (trinity-enterprise#93/#109)
- [template-processing.md](template-processing.md) — local template discovery & processing
