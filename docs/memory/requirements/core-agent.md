# Requirements — Core Agent — Management, Templates, Chat/Terminal, Activity, Collaboration

> Part of Trinity's requirements set. Index & write-path rule: [requirements.md](../requirements.md).

---

## 1. Core Agent Management

### 1.1 Agent Creation
- **Status**: ✅ Implemented
- **Description**: Create agents from templates (GitHub or local) or from scratch
- **Key Features**: Web UI, REST API, GitHub templates (`github:Org/repo`), local templates, credential schema auto-detection

### 1.2 Agent Start/Stop Toggle
- **Status**: ✅ Implemented (Updated 2026-01-26)
- **Description**: Start and stop agent containers via unified toggle control
- **Key Features**: Toggle switch shows Running/Stopped state, loading spinner during action, consistent UI across Dashboard, Agents page, and Agent Detail page
- **Components**: `RunningStateToggle.vue` - Reusable toggle component with size variants (sm/md/lg)

### 1.3 Agent Rename (RENAME-001)
- **Status**: ✅ Implemented (2026-03-01)
- **Description**: Rename agents via UI or MCP without deleting and recreating
- **Key Features**: Inline editing with pencil icon, `rename_agent` MCP tool, atomic DB updates, Docker container rename, WebSocket broadcast
- **Restrictions**: System agents cannot be renamed, only owners/admins can rename
- **API**: `PUT /api/agents/{name}/rename` with `{new_name: string}`

### 1.3.1 Agent Display Label (ent#181)
- **Status**: 🚧 In Progress
- **Implements**: trinity-enterprise#181 (OSS-core — maintainer decision)
- **Description**: A human-readable label an owner can edit freely, with the
  agent's slug (`agent_name`) left untouched. Renaming a thing you can see is
  the common case; re-keying its identity is not.
- **FR-1 — The slug is the identity, the label is presentation**: everything
  machine-facing keeps using `agent_name` — routes, Docker container/volume
  names + labels, MCP keys, A2A cards, Redis keyspaces, every `agent_name`
  column. The label is rendered, never resolved. This is the whole point: §1.3's
  slug rename must rewrite ~20 tables, rename the container, clear every
  per-agent Redis keyspace, and *still* strands the agent's volumes under the
  old base (Docker can rename neither a volume nor its immutable
  `trinity.agent-name` label) — the root of #1664/#1665/#1667/#1669/#1671. A
  label change touches one column and nothing else.
- **FR-2 — NULL means "use the slug"**: `agent_ownership.display_label TEXT`,
  nullable, no backfill. Every existing agent renders exactly as it does today
  until someone sets a label; clearing the label reverts to the slug. Dual-track
  migration (Invariant #3).
- **FR-3 — One label everywhere a name renders**: agent detail header, dashboard
  cards, grid tiles, pickers/lists. A label applied on some surfaces and not
  others shows one agent under two names with no way to tell which is real —
  worse than no label. Resolution goes through a single helper, not per-site
  `||` chains.
- **FR-4 — The slug stays visible and copyable**: it is what URLs, MCP keys,
  containers and volumes are keyed on, so the UI shows it as secondary text
  wherever the label replaces it. A label that *hides* the identity trades one
  confusion for another.
- **FR-5 — The slug rename is demoted, not removed**: §1.3 stays available
  behind a secondary "advanced" affordance with copy that states what it
  actually does (restart, re-key, volumes stay under the old name). Owners who
  genuinely need it keep it; it stops being the default gesture for "call it
  something else".
- **API**: `GET`/`PUT /api/agents/{name}/label` — owner-only, `{label: string|null}`.
- **FR-6 — Remaining surfaces resolve the label off the agents store, not new
  payloads (#1643)**: operator queue, monitoring, executions, the collaboration
  graph, tab titles and prose/toasts render only a slug in their own payloads.
  Rather than grow a mutable `display_name` on each of those high-volume
  endpoints (staleness risk, N duplicated presentation fields), the frontend
  resolves slug → label off the loaded agents (store getters
  `displayNameForSlug` / `agentRefForSlug`, live via the `agent_label_changed`
  WS handler). An unloaded slug falls back to itself, so nothing regresses on a
  cold surface. Render rule by class: **dense operational tables** (executions,
  operator/monitoring rows, RACI matrix) keep the **slug primary** and surface
  the label as a hover tooltip (`agentNameTooltip`); **prose / toasts** use the
  label alone (`agentDisplayName`); the **collaboration graph** renders the
  label but keeps `data.label` = slug as the action key (`router.push` /
  toggles). `AgentAvatar` always receives the slug. Tab titles resolve the
  label on warm SPA nav and fall back to the slug on a cold direct load (the
  store isn't fetched yet); the next navigation self-heals. Comma-joined agent
  lists (e.g. the GitHub-PAT propagation failure list) keep the slug — long
  labels make them unreadable.
- **FR-7 — Findable by display name: pickers, search, sort (#1642)**: the
  picker surface class carries the slug **inline** — `<option>`s render
  `Display name (slug)` via `agentOptionLabel` (else the bare slug), and the
  `<option>` **value stays the slug** so filtering/selection never keys on the
  label. Six dropdowns: `ExecutionsPanel`, `ReportsPanelFleet`, operator
  `QueueList` + `NotificationsPanel`, `FileManager`, `Settings` (subscription
  assignment). `Agents.vue` name search matches **both** the slug and the
  display name (case-insensitive) — otherwise typing "TOM" against a
  `tom-marketing-ops` slug returns nothing. **Sort-key decision (AC):** the
  "Name (A-Z / Z-A)" sort orders by the **display name when set, else the slug**
  (`agentDisplayName`, in the store's `_getSortedAgents`) — sorting by the slug
  while the row renders the label would order the list by an invisible key. Every
  per-agent lookup (`getActivityState`/tags/stats/router actions) still keys on
  `agent.name`; only the option label, the search predicate, and the sort
  comparator changed. No store-shape change — the label is resolved off the
  loaded agents (FR-6 resolvers), so `agentNames`/`availableAgents` stay
  slug-string arrays.

### 1.4 Agent Deletion
- **Status**: ✅ Implemented
- **Description**: Delete agents and cleanup resources
- **Key Features**: Container cleanup, network cleanup, cascade delete sharing records

### 1.5 Agent Logs Viewing
- **Status**: ✅ Implemented
- **Description**: View container logs for debugging
- **Key Features**: Logs tab, fixed-height scrollable container, auto-refresh, smart auto-scroll

### 1.6 Agent Live Telemetry
- **Status**: ✅ Implemented
- **Description**: Real-time container metrics in agent header
- **Key Features**: CPU/memory usage, network I/O, uptime display, auto-refresh every 10 seconds

---

## 4. Template System

### 4.1 Local Templates
- **Status**: ✅ Implemented
- **Description**: Auto-discovery from `config/agent-templates/`
- **Create-time resolution contract (#1793 + #1759)**: `local:<name>` is resolved against the curated catalog first, then the deploy-local store (`/data/deployed-templates`, #950). A well-formed but **unresolvable** id fails with a named **404 `UNKNOWN_LOCAL_TEMPLATE`** (#1793) raised **before any side effect** (no container, no MCP key, no volume, nothing to roll back) — completing the loud-reject contract #843 opened for *unprefixed* template strings. An empty / non-mapping / unparseable `template.yaml` fails in the same pre-side-effect band with **400 `LOCAL_TEMPLATE_INVALID`** (#1759), matching the strictness the listing surface (`GET /api/templates`) already applied; without it a *present* but malformed template reached the identical blank-agent-at-200 outcome through a broad `except Exception`. The traversal barrier keeps precedence: a malformed name is still 400 `INVALID_LOCAL_TEMPLATE_NAME`. `template: null` / `""` (Blank Agent) never enter this branch and are unaffected. Hidden templates (`hidden: true`) are **omitted from the listing but remain creatable by id** — the resolver never reads the flag.
  - The error is **one identical sentence whichever root missed**, carrying no filesystem path and no root name — deploy-local templates are named after *agent* names, so a root-distinguishing message would let a `creator`-role caller probe another user's agents (#186 adjacency).
  - Manifest deploys surface it per agent via the ent#125 `failed[]` report (`status_code: 400`), so one typo'd template no longer sinks a whole system.
  - The curated root falls back to the in-repo `config/agent-templates/` when the container bind mount is absent, so the gate is live in source-run backends too (aligning create with the listing surface, which has had that fallback since #843).
- **Read-path resolution contract (#1900)**: `GET /api/templates/{id}` resolves `local:<name>` through the **same two-step barrier** the create path has had since #950 — a name-shape allowlist (`^[a-zA-Z0-9][a-zA-Z0-9_.-]*$`, no `..`) followed by `resolve()` + `is_relative_to(root)` containment against the live root (`template_service.contained_template_dir`). A name failing either step returns `None` → **404 `Template not found`**, byte-identical to an unknown template: no error code, no filesystem path, no root name (the #1759 non-disclosure rule — the read path is the *same* enumeration oracle the create path's single-sentence 404 exists to close). Before this the id was joined onto the root unvalidated, so `local:../<x>`, `local:/<abs>/<x>` and a root-escaping symlink each read `<escaped-dir>/template.yaml` and echoed its `display_name` / `description` / `resources` / `skills` / `capabilities` / `use_cases` / `data_paths` / `required_credentials` values — arbitrary YAML subtrees, not just strings — to any authenticated caller of any role, including other tenants' uploads under `/data/deployed-templates`.
  - Hidden templates (`hidden: true`) stay resolvable by id: the barrier reads the *name*, never the flag (#1513 contract preserved).
  - **Known, deliberate asymmetry:** `get_local_templates()` still enumerates by `iterdir()`, which follows symlinks, so a root-escaping symlink *planted inside the root* can appear in the listing yet 404 on detail. That matches the create path, which has rejected it since #950; the listing is the outlier, and planting one needs local filesystem write access, not a request.
- **Catalog intent is declared, not defaulted (#1931)**: every directory bundled under `config/agent-templates/` declares its catalog intent explicitly in `template.yaml` — `hidden: true` (internal fixture, system agent, or demo fleet) or `hidden: false` (a starter we stand behind as a user's first agent). The **runtime default is unchanged** (an absent `hidden:` still lists — flipping it would turn a forgotten key into a *silent absence*, a worse failure than the visible-demo-template one this fixes); the requirement is enforced at CI time by `tests/unit/test_1931_catalog_intent.py`, which also pins the shipped visible set to `sage`, `scout`, `scribe`. The sibling guard in `tests/unit/test_local_templates_listing.py` additionally refuses a *visible* `test-` / `demo-` / `dd-`-prefixed directory.
- **Demo fleets ship hidden and stay deployable (#1931)**: the 11-agent `dd-*` VC due-diligence demo fleet is `hidden: true` and reached deliberately through the bundled manifest `config/manifests/vc-due-diligence.yaml` (`POST /api/systems/deploy`) — same pattern as the already-hidden `demo-researcher`/`demo-analyst` pair reached via `config/manifests/research-network.yaml`. A manifest deploy resolves a `local:` id through `crud._resolve_local_template`, which never reads `hidden`. The manifest's system name and short names are load-bearing: deployed names are `f"{manifest.name}-{short}"` and `dd-lead/CLAUDE.md` hardcodes its nine-specialist roster as `vc-due-diligence-dd-*`, so renaming either silently yields a fleet that cannot talk to itself (guarded by `tests/unit/test_1931_manifest_roster.py`).

### 4.1.1 Bundled-Template Hygiene Contract (#1908)
- **Status**: ✅ Implemented
- **Description**: Trinity grades every agent against `docs/agent-validation-spec.md` (§42, `lifecycle-observability.md`) but shipped no gate on the templates it ships itself — all **14 visible** bundled templates failed the same four HARD security checks at birth (`sage`/`scout`/`scribe` shipped no `.gitignore` at all; the 11 `dd-*` shipped a two-line one covering none of them). Every non-hidden bundled template under `config/agent-templates/` must now ship a `.gitignore` that satisfies the `.gitignore`-decidable **HARD** checks — `S-001` (`.env`), `S-002` (`.mcp.json`), `S-004` (`.claude/projects/`), `S-005` (`.trinity/`) — and must **not** trip `G-001` (a wholesale `.claude/` exclusion is forbidden; Claude Code's `commands/`, `skills/`, `agents/` must stay committed).
- **Hiding is not fixing**: `hidden: true` scopes the *catalog listing* only — the resolver never reads the flag (§4.1) and `crud.py` has no hidden gate, so a hidden template stays creatable by id and still births the same findings. The contract is therefore anchored to an explicit guarded set, not to a visibility predicate.
- **Content**: the `.gitignore` mirrors `git_service._GITIGNORE_PATTERNS` — the single source of truth the platform re-appends on every git sync. Shipping it in the template moves protection from *first sync* to *first boot*, the window a freshly created agent's compatibility report observes, and covers `local:` agents that never sync at all. Both the sync merge and the `#668` auto-fix are append-if-missing on exact-line matches, so with every canonical pattern already present they are **no-ops** on this file — birth-state is already post-sync/post-auto-fix state. The file is generated from the guide's canonical ```gitignore``` block, which is a *superset* of `_GITIGNORE_PATTERNS` (the doc-parity test asserts only `canonical ⊆ doc`); the delta is pinned and reviewed in the guard's `ALLOWED_NON_CANONICAL` (today the two `!.env.example` / `!.mcp.json.template` negations), so an unrelated doc edit cannot silently change what every new agent excludes. A template's own pre-existing exclusions are preserved in a trailing `# Template-specific` section (today: `outputs/` on the `dd-*` fleet).
- **Second-order effect on the Trinity repo itself**: a `.gitignore` inside `config/agent-templates/<name>/` also governs **this** repository's view of that directory. A future template author adding `<name>/content/sample.md`, `<name>/notes.log`, `<name>/data.db` or a `*.local.md` file will find `git add` silently skipping it. Use `git add -f`, or (better) don't ship files matching the canonical list from a template. Verified at introduction: no tracked or untracked file under `config/agent-templates/` became newly ignored.
- **Shipping committed `.trinity/` hooks**: the canonical list excludes `.trinity/` wholesale. A template that ships a *committed* `.trinity/` hook (a `pre-check`, #454; Brain-Orb hooks, trinity-enterprise#76) must instead use the `.trinity/*` + `!.trinity/<dir>/` form — `S-005` accepts the star form for exactly this reason. No bundled template needs this today.
- **Enforcement**: `tests/unit/test_1908_bundled_template_gitignore.py`, run in the `backend-unit-test` per-PR workflow. It is both the guard and the **regenerator** (`python tests/unit/test_1908_bundled_template_gitignore.py --regenerate`), so a new `_GITIGNORE_PATTERNS` entry is a one-command change however many templates are guarded. The guard evaluates the real `static_checks.run_static` (never a re-implementation of the rules), names its coverage in a `GUARDED_TEMPLATES` constant, fails if a **new visible** template is added outside that set, and applies one universal assertion (`G-001`) to **every** bundled directory, hidden included.
- **Known gap**: `T-004`/`T-005` (`resources.cpu`/`resources.memory` in `template.yaml`) still fail for the three starters, so those report 2 HARD findings rather than 0 (the 11 `dd-*` reach 0 — they already pin `resources`). Pinning `resources` in a bundled template is an existing catalog convention, but it *overrides* the admin's fleet-wide default (RES-001, `PUT /api/settings/agent-defaults/resources`) — so whether the default starters should pin or inherit is a **product decision**, and arguably these two checks should `skip` rather than `fail` when a template deliberately inherits. Tracked as a follow-up; the guard waives exactly those two ids and fails if the waiver goes stale.
- **Not retroactive**: `startup.sh` copies `/template` only when `/home/developer/.trinity-initialized` is absent, so agents already created keep their existing `.gitignore`. They are served by the per-agent auto-fix (`POST /api/agents/{name}/compatibility/fix`) and the sync-time merge.

### 4.1.2 Deploy-Local Integrity Contract (#2060)
- **Status**: ✅ Implemented
- **Description**: `POST /api/agents/deploy-local` (and its MCP tool `deploy_local_agent` / the `trinity deploy` CLI) verifies the deployed content against an **embedded manifest** and refuses to deploy silently-incomplete agents. Before this, the archive rode the calling model's own turn as a base64 tool argument with **zero** integrity verification — a pruned-but-well-formed archive (extra tar `--exclude`s, paste truncation, macOS AppleDouble pollution, dereferenced symlinks) deployed `status: "success"`.
- **Embedded manifest** (`.trinity-manifest.json`): a JSON array the caller computes **from the disk tree** and writes into the agent directory, so the tar carries it as an ordinary member. Entry schema (`DeployManifestEntry`): `{path, sha256?, link_target?}` — regular files carry `sha256`, symlinks carry `link_target` (exactly one of the two), directories omitted; paths relative to the agent root. Embedding (not a request field) is load-bearing: a 5000-file manifest as a JSON tool argument would blow the same output-token ceiling as the archive and recreate the bug one level up; embedding costs zero extra transport and works identically on the base64 arm, the CLI, and the future upload arm (FU-1). Parse bounds (400 `MANIFEST_INVALID`): file read cap 5 MB, ≤ `MAX_FILES` entries, path ≤ 1024 chars, no duplicates, no absolute/`..` paths, `sha256` XOR `link_target` per entry. The manifest file itself is excluded from verification (cannot self-hash; its transport integrity is the gzip's) and from the response counts; it lands in the workspace as inert metadata (future F4 reconciliation input).
- **Verification points** (fail-closed 400 `MANIFEST_DRIFT` naming `missing`/`altered`/`extra`/`link_mismatch` paths, each list capped at 50 + full counts): (1) **post-extract**, before ANY side effect (precedes quota, stop-previous-version, and the copy — the #2006 gate-ordering rationale), with extras counted as drift; (2) **post-copy** into `/data/deployed-templates/<version>`, immediately after `copytree` and **before** the request-credentials `.env` merge mutates the tree (ordering load-bearing — the merge would otherwise false-drift `.env`). Post-`put_archive` verification is deliberately skipped: the volume copy is a local `tar.add` from the just-verified `dest_path` and `put_archive` failures already raise. The `MANIFEST_DRIFT` recovery text directs the caller to rebuild without extra excludes / regenerate the manifest / use the CLI — it deliberately does NOT suggest removing entries from the manifest (that would teach consistent pruning).
- **Layered requiredness**: the MCP tool's `execute()` unconditionally sets `require_manifest: true` in the POST body (tool *code*, not a model-controlled parameter) → flag set + no manifest in archive = 400 `MANIFEST_REQUIRED` carrying the generation snippet. On the raw HTTP API `require_manifest` defaults to `false`: manifest-less legacy deploys (shipped PyPI CLI, abilities plugin) still succeed with `status: "success"` but `verified: false` + a warning — flipping `status` would make every legacy deploy *report* failure after succeeding (the shipped CLI hard-fails on `status != "success"`). The in-repo CLI computes the manifest during its archive walk, injects it into the tar in-memory (never mutates the user's source dir), and sets `require_manifest: true`.
- **Honesty note (accident-proof, not adversary-proof)**: the manifest is computed by a command walking the FULL disk tree, so every *accident* class diverges from a pruned archive and is refused loudly; a passing incomplete deploy requires the caller to consistently edit both the tar and the manifest commands — deliberate evasion, visible in the calling transcript, out of this bug's scope. The tool argument is also **token-bound** (~100–200 KB of base64 per model turn in practice), so large agents must deploy via the turn-bypassing transports that already exist: the `trinity` CLI or `curl` from bash (MCP keys are valid Bearer tokens). The integrated direct-upload channel (removing the payload from the turn entirely) is the FU-1 follow-up.
- **Symlink contract** (matrix; escape refusals pre-date #2060 and are regression-pinned):

  | Case | Contract |
  |---|---|
  | Absolute symlink target | 400 `INVALID_ARCHIVE` naming path + target (unchanged) |
  | Symlink resolving outside the extraction root | 400 `INVALID_ARCHIVE` naming path + target (unchanged; non-strict resolve covers dangling-escaping) |
  | Chain where any hop exits the root | each exiting link is itself a member → refused individually (unchanged, now chain-pinned) |
  | In-root symlink, target present | **preserved as a symlink end to end** (extract → `/data/deployed-templates` → prepop tar → workspace volume) via `copytree(symlinks=True)`; counted in `symlinks_deployed` |
  | Dangling in-root symlink | **preserved + named warning** (`dangling symlink preserved: {path} -> {target}`); a pruned *target* still listed in the manifest is refused as `missing` (the pruning signal at the right layer). Rationale: links to runtime-created dirs (`content/`, `data/`) are legitimate |
  | Hardlink | contained-or-refused (unchanged); manifest treats as a regular file |

- **Layering rule (load-bearing)**: *security validation* (containment, link targets, member types — `_validate_tar_member` over `tar.getmembers()`) runs strictly **before any extraction**, exactly as before; *drift verification* (manifest matching) runs post-extract. Moving containment checks post-extract would reopen the tar-slip class. `extractall` is pinned to `filter='tar'` (Py3.14 flips the unpinned default to `'data'`, changing symlink/metadata semantics under us; `'tar'` is behavior-stable, strips setuid/setgid/sticky as defense-in-depth, and leaves `_validate_tar_member` the single authoritative link barrier).
- **Caps** (every rejection carries `observed` + `limit`): `MAX_ARCHIVE_SIZE` 50 MB compressed (unchanged — 50 MB decoded ≈ 67 MB base64 JSON body; raising it without a real byte channel is FU-1's call), `MAX_FILES` 10000 (was 1000 — a Cornelius-class KB agent exceeds 1000 members; byte caps are the true resource bound), **new** `MAX_EXTRACTED_SIZE` 500 MB summed from member headers pre-extraction (400 `ARCHIVE_EXTRACTED_TOO_LARGE`; closes the gzip-bomb hole), manifest read cap 5 MB. macOS AppleDouble `._*` members are skipped with a warning (and `COPYFILE_DISABLE=1` documented in the tool description) so they neither pollute the workspace nor false-drift the manifest.
- **Evidence-bearing response**: `DeployLocalResponse` gains `verified` (true only when a manifest was present and both verification points passed), `files_expected` (manifest file entries), `files_deployed` / `symlinks_deployed` (counted at `dest_path` at verification time, manifest member excluded), and `compatibility_hard_count` — a post-create #668 STATIC-only report (fail-open: `None` + warning when the report is unavailable; never blocks a deploy).
- **Idempotency + concurrency**: the endpoint accepts an optional `Idempotency-Key` (Invariant #18; scope `agent_deploy:{user_id}`, mirroring `agent_create` including the #2040-F3 staleness branch — a completed replay is honored only while the recorded `versioning.new_version` is live; in-flight duplicate → 409 `DEPLOY_IN_FLIGHT`). The MCP tool derives a deterministic key over `[userId, tool, name, archive]` — this protects transport-level retries (same args ⇒ same key, closing the retry-double-fork); a re-run bash pipeline produces new gzip bytes ⇒ new key ⇒ a visible version fork, correct by design (content-derived keys would false-replay intentional identical-content redeploys). A per-base-name Redis lock (`agent:deploy_op:{base_name}` — the shared `redis_breaker_util.SingleFlightLock` #1920: SETNX + 10-min TTL, per-acquire token, compare-and-delete release; fail-open on Redis down, 409 `DEPLOY_IN_PROGRESS` on contention) closes the concurrent same-version-name race; registered in `agent_runtime_state.EXEMPT_KEYSPACES`.
- **Residue + compensation**: `dest_created` is assigned *before* the rmtree/`copytree` pair so a mid-copy failure is cleaned by `_remove_partial_deploy` (#2006 class; the copy failure itself is a named 500 `TEMPLATE_COPY_FAILED`, replacing the opaque `shutil.Error` 500). The prepopulated workspace volume is tracked and removed best-effort on failure (label + unattached double-guard, #1581 shape). A pre-existing volume under the new version name is removed-and-recreated when unattached; **attached** → 409 `WORKSPACE_VOLUME_IN_USE` (never `put_archive` into a mounted volume — an attached volume here means a concurrent/zombie deploy). A previous version stopped by step 7 is best-effort **restarted on any failed deploy** (including a `create_agent_fn` raise — crud rollback + ent#313 reclaim remove the failed container first; log-only on restart failure, never masks the original error). The compensation window **closes when `create_agent_fn` returns**: from that point the new version is live, and a late failure (response construction) must not restart the previous version alongside it — one base name running two live versions is the F5 double-run hazard. The final catch-all 500 carries `code: "DEPLOY_FAILED"`.
- **Out of scope (follow-ups named at ship)**: FU-1 direct-upload transport (staged owner-bound handle; carries AC 1), FU-2 redeploy-in-place (F5; carries AC 7), `.env` value quoting (#2023 / PR #2030).

### 4.2 GitHub Templates
- **Status**: ✅ Implemented
- **Description**: Clone via `github:Org/repo` format with PAT authentication

### 4.2.1 Admin-Configurable GitHub Templates (TMPL-001)
- **Status**: ✅ Implemented
- **Description**: Admin can configure which GitHub repos appear as agent templates via Settings UI. All metadata (display name, description, resources, MCP servers) is fetched from each repo's `template.yaml` via GitHub API (cached 10 min).
- **Key Features**: `config.py` holds the default repo list (no metadata) — **empty since #1931**: the shipped list had gone stale (a pre-2026 repo set, last pushed Dec-2025/Jan-2026) and no install had ever written an override row, so every operator browsed the same dead list. Curation is now an explicit operator act, not a bundled default. `system_settings` table (`github_templates` key) stores admin overrides, `GET/PUT/DELETE /api/settings/github-templates` endpoints, Settings UI with add/remove/save/reset.
- **Behavior**: `None` (key missing) = use defaults, `[]` = no GitHub templates, `[{...}]` = custom list. **Since trinity-enterprise#14 "defaults" means the remote template registry, else the bundled (empty) list — see §4.2.2; a DB override still wins outright and suppresses the registry fetch entirely.** Admin-provided display_name overrides repo's template.yaml value. With the default empty, `None` and `[]` produce the same **catalog**; they still differ in the `source: defaults | settings` badge the Settings panel renders. Individual repos remain creatable at any time via `template: github:owner/repo` (`get_github_template` resolves any well-formed `github:` id whether or not it is in the configured list) — emptying the list removes a *browse* surface, never a *create* capability. Side-effect (#1931): with no default repos, `GET /api/templates` makes **zero** outbound GitHub calls on a cold metadata cache, where it previously blocked on up to six.

### 4.2.2 Remote Template Registry (TMPL-002, trinity-enterprise#14)
- **Status**: ✅ Implemented
- **Description**: The GitHub half of the template catalog can be sourced at **runtime** from a `registry.yaml` fetched over HTTPS, so curating which starter agents an install offers is a file edit by the vendor rather than a Trinity release. Purely **additive** to §4.2.1: it fills the branch that has been empty since #1931 (`DEFAULT_GITHUB_TEMPLATE_REPOS = []`), and it is not fetched at all on an install that has curated its own list.
- **Precedence ladder (deliberate)**: **admin DB override (`github_templates`) → remote registry → bundled `DEFAULT_GITHUB_TEMPLATE_REPOS`**. An admin who has curated a list never has it silently replaced by a vendor registry — TMPL-001's "DB-configured list takes full precedence" contract survives byte-for-byte, and a curated install makes **zero** registry requests. The same ladder is resolved by **both** `get_all_templates()` (list) and `get_github_template()` (detail **and the agent-creation path**, `crud._resolve_github_repo_and_pat`), so a registry-sourced template keeps its display name, description and priority on all three surfaces. Two resolvers of one list is the `learnings.md` 2026-07-10 *"the create path is never one call site"* class; pinned by `tests/unit/test_ent14_catalog_failopen.py`.
- **Document schema (v1)**:
  ```yaml
  version: 1                      # absent ⇒ 1; unknown/greater ⇒ whole document REFUSED
  templates:
    - repo: Abilityai/cornelius   # required, ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$
      display_name: Cornelius     # optional, ≤200 chars (truncated, not rejected)
      description: Your second…   # optional, ≤1000 chars (truncated, not rejected)
      priority: 20                # optional int, lower sorts earlier
  ```
  `version:` is the **only** forward-compat mechanism — deliberately not a versioned URL path (unlike `OPERATOR_INTAKE_URL`'s `/v1/`), because the URL default is baked into `config.py` and bumping it would cost a release, which is precisely the cost this feature exists to remove.
- **Fail-open is structural, not an `except` branch**: `get_all_templates()` returns `local + github`. `local` is read from disk with no network and no registry involvement; `github` is empty by default. The registry can only ever *add* to `github`, so **every** failure mode — unreachable, 5xx, timeout, malformed, alias-bombed, oversize, redirected, empty — reduces `github` toward `[]`, which is the already-shipping default state of the product. No registry failure can make the catalog worse than a default install, and none can make `GET /api/templates` return anything but 200. The `except` layers (`get_registry_templates()` never raises; its call site is *additionally* fenced; each entry still passes through `_safe_build_github_template`) are a second layer, not the mechanism. Proven mode-by-mode by `tests/unit/test_ent14_catalog_failopen.py` — the assertion is always on the *catalog output*, never on an internal call count.
- **Allowlisted parse — the blast-radius bound**: a registry entry is parsed into a frozen four-field record (`repo`, `display_name`, `description`, `priority`) and **never splatted** into the template dict. Unknown keys are ignored, not merged, so a registry cannot assert `fork_to_own`, `credentials`, `credential_setup`, `data_paths`, `persistent_state`, `schedules`, `resources`, `skills`, `hidden` or `id` — every one of those is a claim about a repo the registry does not own, and every one has a creation-path consequence. `github_repo` and `id` are both computed by `_build_template` from the same `repo`, so a card can never display a repo path different from the one it would clone.
- **`repo` is a capability pointer — stated plainly, because the comfortable version is false**: it is *literally* true that the four allowlisted fields only change which repos are listed and how they are labelled and ordered. As a **security** statement that is materially misleading. By choosing `repo`, the registry chooses **which `template.yaml` Trinity fetches and trusts**, and that document declares `mcp_servers`, `credentials`/`credential_setup`, `schedules`, `data_paths`, `persistent_state`, `resources`, `skills` and `fork_to_own`. The registry does not set those fields; it **selects the document that does**. The allowlist bounds the *direct* blast radius to display and order. It does not bound the indirect one. (Same distinction ent#123 draws for tokenless public clones: the platform trusts a repo it did not author.)
- **What a hostile registry can and cannot do**: **cannot** reach an eval/exec/deserialize sink, write to the database, make the catalog fail, or make a card's displayed repo differ from the one it clones. **Can** cause `evil/repo` to appear with a trustworthy-looking name — but any `creator`-role user can already create from `github:evil/repo` by typing it, so the registry grants **no new permission**; it grants **persuasion**. Mitigations shipped: the default URL is vendor-controlled HTTPS, changeable only by an admin **human**; the catalog card always renders `github_repo` under the display name; two independent off-switches.
- **Named residuals (recorded so the next phase inherits the question rather than rediscovers it)**: (1) **no signature/provenance verification** of the registry document; (2) **no allowlist** on which repo owners a registry may list; (3) **DNS rebinding** is not closed — URL validation pre-resolves, so there is a TOCTOU between validate and connect (accepted for v1: the URL is admin-and-human-set, the response is parsed into a display-only allowlisted record, and the body never reaches a deserialize path). (1) and (2) belong to a private/per-customer-catalog phase where the trust model is genuinely different.
- **Two off-switches, deliberately asymmetric**:
  | Switch | Where | Semantics |
  |---|---|---|
  | `TEMPLATE_REGISTRY_ENABLED` | env → `config.py` | **Hard** kill switch. `false` ⇒ never fetch, and no DB row can turn it back on. The air-gap / policy answer. |
  | `template_registry_enabled` | `system_settings` row | Admin toggle, **default true when absent**. Composed with the hard switch at the consumer. |
  Both are injected into `backend.environment:` in **`docker-compose.yml` and `docker-compose.prod.yml`**, and documented in `.env.example`. This is not boilerplate — prod compose launches **standalone** (no base merge, no `env_file:`) and is what every deploy path uses, so an unwired var is inert on every real instance while still working on a laptop. Omitting it shipped the hard kill switch dead in review: default-ON outbound egress with no way to stop it, the #1039/#1056 packaging-gap class for the **sixth** time (after #1056 `VOIP_*`, ent#31 `LOG_*`, #1039, #1871 `AGENT_LOG_*`, #411 `CANARY_*`). Pinned by `tests/unit/test_ent14_registry_env_packaging.py`, which also holds `${VAR:-true}` (a hardcoded `true` passes a presence check while re-breaking the switch) and the URL's full non-empty default (a bare `:-` arrives set-but-empty and shadows the code default, #1076 — worse than absent, because it looks wired).
  Deliberately **not** implemented via `settings_service._resolve_bool_flag`: that helper's env leg is *opt-in only* (`"true"/"1"/"yes"` → True, anything else falls through to `default`), so with `default=True` it would **silently ignore `TEMPLATE_REGISTRY_ENABLED=false`** — an inert kill switch, the #1039 "inert by obscurity" class. Worth stating that the flag then shipped inert *anyway*, via the compose gap above, until review caught it: avoiding a known failure mode in the layer you are looking at does not avoid it in the layer you are not. Uses the `OPERATOR_INTAKE_ENABLED` / `TELEMETRY_SHARING_ENABLED` shape instead (config-level boolean computed at import, composed with the DB row at the consumer, `hard_disabled: true` rendered in the panel). **Not** coupled to `DO_NOT_TRACK`: those two honour it because they *send* data about the operator; a registry fetch sends nothing — it is a package-index read (npm and Homebrew do not disable their default registries under DNT). It is still outbound egress on a default install, which is a real behavioural change and carries a release note.
- **Security envelope**:
  - **YAML**: `AliasPolicy.REJECT` via the named `utils.safe_yaml.load_template_registry_yaml()` helper (policy pinned at the `utils/` layer so it is never relitigated at a call site). A four-scalar-field schema has no legitimate anchor, and this is the most exposed document in the system — network-fetched, unsigned, process-cached, fanned out to `/api/templates` for every authenticated user. `template.yaml` gets BUDGET while being *less* exposed. Duplicate-key rejection matters here specifically: a registry with two `templates:` keys would silently last-wins, i.e. show one catalog to the human editing the file and serve another to Trinity.
  - **Byte cap, two layers, transport is the load-bearing one**: the fetch streams and aborts the moment a running byte ceiling is crossed. A `Content-Length` check is **not** sufficient — it is absent on chunked responses and trivially lied about, and `resp.text` on a 10 GB body OOMs the worker before any parse-time cap can act. `max_bytes=REGISTRY_MAX_BYTES` (256 KiB) is passed to the parser as a belt so the cap survives a future refactor of the fetch layer. **The ceiling counts WIRE bytes** (`iter_raw()`) and any `Content-Encoding` is refused before the body is read (`encoding_refused`), with `Accept-Encoding: identity` sent as the polite half. Counting `iter_bytes()` — *decoded* chunks, with httpx's default `Accept-Encoding: gzip, deflate` — let a wire body that passed the `Content-Length` abort inflate ~1030:1 before the running total was consulted: 458 MB of transient allocation on the event-loop thread from a 199 KiB response. The refusal was still correct (`too_large`); what failed was the bound, because the resource under attack is the peak, not the decoded total. Asking for `identity` is a request, not a control — a hostile server compresses regardless — so the refusal is what makes the ceiling meaningful.
  - **SSRF gate** (`utils.url_validation.validate_template_registry_url`): HTTPS only; **no userinfo** (`user:token@host` rejected outright, never redacted, so a credential can never be stored in a settings row or echoed into a status payload); resolve-and-reject private/loopback/link-local/reserved destinations **plus RFC 6598 shared address space (`100.64.0.0/10`)**, which Python's `ipaddress` reports as neither `is_private` nor `is_reserved` and which several cloud providers use for internal endpoints (not reachable in Trinity's own `172.28/16` + `172.29/16` topology, a `10.0.0.0/8`-shaped hole anywhere that does use it); **`follow_redirects=False`** — a validated URL that redirects is an SSRF bypass and `raw.githubusercontent.com` does not redirect for a valid path, so a redirect is a fetch failure that degrades to the floor.
  - **`repo` charset**: every entry's `repo` is matched against `^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$` and dropped on failure, because it is interpolated into `https://api.github.com/repos/{repo}/contents/template.yaml` **and** into the template id `github:{repo}` a user can hand to agent creation. A fourth copy of a pattern that already exists three times, carrying the obligation that convention brings: a **behavioural** parity test over a fixture corpus (`tests/unit/test_ent14_repo_pattern_parity.py`), not a source-string comparison — the existing copies already differ in character-class ordering while denoting the same set.
  - **Field caps**: `display_name` ≤ 200, `description` ≤ 1000 — truncated, not rejected, so the entry stays useful. These strings land in the catalog response, the logs and the DOM; Vue interpolation (never `v-html`) covers XSS, the caps cover "a 10 MB description in every catalog response".
  - **No `str()` coercion, ever** — type-guard first. `str()` on a container from untrusted YAML walks the graph and pays the amplification cost *before* any cap can act (`template_service._clean_field`). Moot under `AliasPolicy.REJECT`; kept as discipline.
- **Tolerant reader (the ent#128/ent#89 contract — read paths degrade, never raise)**: top-level not a mapping / unknown `version` / `templates` missing or not a list ⇒ the **whole document** is refused and the catalog degrades to the floor. `templates: []` is a **success** with zero entries, reported `ok` and distinct from a failure. Over the cap ⇒ truncated with a named error (the `MAX_DECLARED_SCHEDULES` precedent). Per-entry: non-mapping, missing/non-string/pattern-failing `repo`, and duplicate `repo` all drop **that entry** with a named error; non-string display fields fall through to `template.yaml`; a non-int (or `bool`) `priority` is ignored. Errors surface on the **settings status** endpoint, never on the catalog — an operator debugging their registry needs them, a user browsing templates does not.
- **Caching — own cache, never a share of `_metadata_cache`** (whose value type is a raw metadata dict with no status, no staleness and no invalidation hook):
  - **TTL 3600 s ± jitter, deliberately NOT aligned with the 600 s per-repo `_CACHE_TTL`.** Aligning them is not a rhythm, it is a **correlated thundering herd**: on expiry one worker fires a registry fetch *and* N per-repo GitHub fetches in the same instant, and with `--workers 2` both workers drift into phase. The registry is an index that changes on a human's git commit and does not need 10-minute propagation. The longer TTL is also **doing real security work, not tidiness** — it is the cheapest lever on the GitHub call budget below.
  - **Serve-stale-on-failure, capped at `REGISTRY_MAX_STALE_SECONDS` (7 days).** Unbounded stale is not safe: the registry is a *trust pointer* to repos, so an unbounded stale copy keeps a de-curated, renamed or compromised repo in the product indefinitely while the operator has no signal — the catalog still renders. Past the cap the entry is dropped and the install degrades to the floor, which is the documented contract.
  - **Negative cache**: a failed fetch with no prior good parse is remembered for 60 s, so a dead URL costs one bounded request per minute per worker rather than one per catalog load.
  - **Cross-worker invalidation via a generation counter, not per-process.** A per-process `invalidate_registry_cache()` clears only the *calling* worker, so with `--workers 2` an admin who repoints the registry sees it apply on roughly half of their page loads — a nondeterministic setting, which is worse than a slow one. Every settings write bumps a `template_registry_generation` row; a cached entry stamped with a stale generation is discarded on read. **The TTL raise and the generation counter are coupled and must not be split** — the counter is mandatory at a 1-hour TTL, not nice-to-have.
  - **Durable last-known-good** (`template_registry_lkg`, sanitized *parsed* JSON — never raw YAML — carrying `source_url`, `entries`, `fetched_at`, `schema_version`, `parser_version`, `sha256`). Written **only when the normalized content changes**, so a steady-state fetch writes no row. Invalidated by a URL change, either off-switch, a parser-version bump, or the same max-stale cap. It ships because the registry is **default-ON and primary**: without it a first boot during a registry outage shows a fresh operator the bundled floor — the exact first-screen problem this feature exists to fix, now with a network dependency in front of it.
- **Catalog payload shape is unchanged** — no registry provenance on `/api/templates` entries; provenance lives on `GET /api/settings/template-registry`. This is a *design constraint*, not a lucky accident: it is what buys both "no MCP `list_templates` change" (Invariant #13 satisfied without code) and "no `Library.vue` change" (registry entries are `source: "github"` and land in the existing grid). Pinned by a test asserting the key set of a registry-sourced entry equals an admin-override-sourced one.
- **Ordering is curatable**: `_build_template` prefers a valid int `override["priority"]` before the repo's own `template.yaml`, feeding the router's `(priority, display_name)` sort. Backward compatible — TMPL-001's `GitHubTemplateEntry` has no `priority` field, so admin entries resolve `None` and behave byte-identically. "Deprecate" needs no mechanism: removing an entry stops it listing, and `github:owner/repo` still creates it. (Adding `priority` to the admin entry model is an out-of-scope follow-up.)
- **Admin surface**: `GET/PUT/DELETE /api/settings/template-registry`, registered **before** the `/{key}` catch-all (Invariant #4) like `/skills-library` and `/brain-orb`. Writes are `assert_admin` **+ `reject_agent_principal`** — a role gate answers *what role*, never *is this a human*, and `get_current_user` resolves an agent-scoped MCP key to its owner **carrying the owner's role**, so on a default admin-owned install any agent's injected `TRINITY_MCP_API_KEY` satisfies a bare admin gate (trinity-ops-agent#232 class). Here the consequence is direct: an agent could otherwise repoint the platform's template registry at a URL it controls. Both keys are additionally **422-blocked on the generic `PUT /api/settings/{key}`** (which takes an unvalidated `Dict[str, str]`) — without that the SSRF gate is one generic PUT away from being bypassed. Validate at the boundary **and** at the sink (#1525).
- **Status is part of the contract**: `GET` returns `{last_fetch_at, last_status: ok|failed|disabled|never, last_error_code, template_count, stale, errors[]}`. This is what makes fail-open **visible** rather than silent — an operator whose registry 404s must see that from the panel, not by grepping logs (ent#236's "the panel must be able to show a *failing* auto-sync"). `last_error_code` is a fixed lowercase vocabulary (`unreachable`, `timeout`, `http_error`, `too_large`, `encoding_refused`, `parse_refused`, `unsupported_version`, `bad_shape`, `invalid_url`, `redirect`) — **never a raw exception string**, so a hostile server's response text cannot reach the panel.
- **Honest cost — the GitHub cold-cache call count returns**: #1931's side-effect was "zero outbound GitHub calls on a cold metadata cache". A non-empty registry re-introduces one `template.yaml` fetch per listed repo per cold cache. At the 600 s per-repo TTL and `--workers 2` the steady state is `workers × windows/hr × entries` — `12 × entries` per hour at a 10-minute window. On an install with **no platform PAT** GitHub's anonymous limit is 60 req/hr per IP, so above ~5 entries the metadata fetch is rate-limited some of the time. Three things make this acceptable: it degrades gracefully by design (a 403 returns `{}` and `_build_template` falls back to the **registry-supplied** display fields, so the card still renders and only derived chips go empty — the concrete payoff of reusing the `admin_override` shape); it is not new (an admin curating 25 repos via TMPL-001 hits the same wall today); and a platform PAT raises the limit to 5000/hr and is already a first-class settings surface. `MAX_REGISTRY_TEMPLATES = 25` is sized against this, and the 3600 s registry TTL keeps the *registry's own* fetch off that budget entirely. **Operators listing more than ~5 repos without a platform PAT should configure one.**
- **`fork_to_own` fails closed on unreadable metadata (the fix that gates default-ON)**: graceful display degradation is true for display fields and **false** for `fork_to_own`. `_fetch_template_yaml_result` distinguishes "no `template.yaml`" from "could not read it" and the catalog wrapper used to throw the reason away, so a rate-limited (403) fetch produced `fork_to_own: None` and `crud._apply_fork_to_own`'s `== "required"` test never fired — creating the agent **bound to the shared upstream template repo instead of a user-owned copy** (the ent#162 class, reached with no attacker). Pre-existing, but this feature converts it from unreachable to *expected*: it re-introduces per-repo fetches on a **default** install, ships default-ON, and its own arithmetic puts `N > 5` over the anonymous budget — while ent#137's curated fleet is very likely to include the `fork_to_own: required` agent. The creation path now **refuses** (`503`, `TEMPLATE_METADATA_UNAVAILABLE`) rather than treating unreadable as absent. **Which read decides is the whole fix**: both the availability verdict and the `fork_to_own` value come from the CREATION-path read (`crud._read_source_template` → `fetch_template_metadata_result_for_create`) — the PAT that will actually clone, at the requested ref, cache-bypassed — not from the catalog dict, whose `metadata_unavailable`/`fork_to_own` come from the global-platform-PAT, default-branch, 600 s-cached `_get_cached_metadata_result`. Reading the catalog failed in both directions: GitHub answers **404, not 403**, for a repo a token cannot see, so a *private* `fork_to_own: required` template readable only by the creator's per-user PAT (ent#162) classified as ABSENT and the gate passed — the precise outcome it exists to prevent, no attacker involved — while a shared 403 in the cache 503'd every non-forking `github:` create for the full TTL. Fixing only the availability half would not have closed the false pass: the *value* must come from the correctly-credentialed read too. Costs zero extra GitHub calls (it consumes the read ent#89 already makes for `schedules:`), and `required` declared by **either** read enforces, so the change can only remove a false pass, never add one. A clean **HTTP 404 is still "absent"**, so a repo that genuinely ships no `template.yaml` creates exactly as before. The trade is deliberate and is the `learnings.md` 2026-07-15 direction-of-failure rule applied to a gate: agent creation now depends on GitHub API reachability where it previously depended only on `git clone`, and a loud retryable refusal beats a silent wrong-repo binding. Regression-tested by `tests/unit/test_ent14_fork_to_own_failclosed.py`.
  - **Known adjacent defect, deliberately not fixed here**: `_get_cached_metadata`/`_fetch_all_metadata` write `_metadata_cache[repo] = (…, metadata, reason)` **unconditionally**, so a `{}` from a transient 403 overwrites a previously-good entry and is served for a full 600 s TTL. That is what turns the window from one request into ten minutes. It is strictly wider than the registry — the shared metadata cache serves every template path — so it is tracked separately. It no longer reaches the fail-closed gate at all: since the gate moved to the creation-path read, a poisoned cache entry degrades display fields and cannot make creation either refuse or pass wrongly. (Caching the *reason* alongside the empty metadata still keeps `metadata_unavailable` honest for the catalog surface.)
  - **`mcp_servers` and `resources` degrade the same way** on an unreadable `template.yaml`, silently falling back to defaults. Only `fork_to_own` has a security consequence, so only it fails closed — but the pattern is named here so the next reader does not have to rediscover it.
- **`hidden` is inert on the GitHub half — a real local/github asymmetry**: `_build_local_template` sets it and `get_local_templates` filters on it, but `_build_template` never emits it and `_safe_build_github_template` never filters on it. So a registry cannot mark an entry hidden, *and* a listed repo whose own `template.yaml` says `hidden: true` is still shown. Neither is exploitable; the divergence is recorded so it is not rediscovered as a bug.
- **Ship prerequisite (not code)**: a valid document — possibly empty (`version: 1` / `templates: []`) — must exist at the default URL before or with the release. Then day-one behaviour is "fetch succeeds, zero entries, catalog unchanged, no warnings", and publishing the curated fleet (ent#137) becomes a pure content edit, which is the whole point.
- **Out of scope (v1)**: hosted-intake routing / private per-customer catalogs; the registry **content** itself (ent#137, a separate issue and repo); search/filter over the catalog (the inherited ent#108 leg — premature over a 3-entry catalog); signature verification (above); any change to agent creation, `template.yaml` parsing, or the `github:owner/repo[@branch]` create path, which is preserved untouched as the OSS escape hatch.

### 4.3 Template Metadata
- **Status**: ✅ Implemented
- **Description**: Read template.yaml for display name, description, resources, credentials
- **`credentials.mcp_servers` template lookup resolves from the validated path, not from `name:` (#1900)**: `generate_credential_files` used to locate the shipped `.mcp.json` by joining the template.yaml's own `name:` field onto a hard-coded curated root — an unvalidated join whose result is read into the new agent's `.mcp.json`. Two things were wrong with it: `name:` is **untrusted** (through `deploy_local_agent` any `creator` supplies it, so `name: ../../data/deployed-templates/<victim>` read another tenant's credential-bearing `.mcp.json` into the attacker's own agent), and it is **not a directory name at all** — it is a display string in 5 shipped templates ("Test Echo Agent"). The create path now passes the directory it already validated via `_safe_local_template_path` (extracted as `crud._resolve_local_template_dir`, the single ladder shared by the resolver, the `/template` bind decision and the credential stager), so the derivation is gone from the live path. The residual `template_base_path is None` arm (no caller today; `github:` templates never reach this function, their `template_data` stays empty) is fail-closed and contained through the same barrier as the id, which also absorbs a non-string `name:` that previously raised `TypeError` out of agent creation as an uncaught 500.
  - **Consequence worth knowing (stated precisely, because the flattering version is false):** a deploy-local template that both declares `credentials.mcp_servers` and ships a `.mcp.json` now has that file **staged at all**, where the old curated-root lookup always missed — and the staged copy **wins** over the archive's raw one, because `startup.sh` copies `/generated-creds/.mcp.json` unconditionally and *after* the template-copy block (gated on `.trinity-initialized`). It is **not** true that such templates "now get `${VAR}` substitution": the sole production caller, `crud._stage_config_files`, passes an **empty** `agent_credentials` map (CRED-002 — real values are injected after creation, not at staging), so every `${VAR}` is rewritten to `""` while hardcoded entries survive verbatim. That mirrors the `.env` arm of the same function, which has always blanked an un-supplied `credentials.env_file` variable; the durable record of a server's required variables is `.mcp.json.template` (compatibility check S-009), pre-populated untouched.
- **Per-variable credential setup metadata (ent#128)**: `credentials:` declares
  variable NAMES (frozen — it will never accept per-variable objects, so an
  older Trinity reading an enriched template cannot break while writing the
  agent's `.env`); the optional sibling `credential_setup:` describes each one
  (title / description / required / secret / format / setup_url / default) and is
  joined back BY NAME, so it can only decorate a declared variable and the pair
  cannot drift. Surfaced as `credential_requirements` on every catalog entry.
  A legacy bare name normalizes to `required: "unknown"` — no authorial intent.
  See `docs/memory/requirements/credentials.md` §3.5 and
  [`docs/schemas/trinity-agent-credentials.schema.json`](../../schemas/trinity-agent-credentials.schema.json).

### 4.4 Fork-to-Own Templates (trinity-enterprise#93)
- **Status**: ✅ Implemented (2026-07-06)
- **Description**: A GitHub template can declare `fork_to_own: required` in its `template.yaml`; creating an agent from it copies the template into a repo the **user owns** (private by default) and the agent's `origin` points there — captures, operator Push, and auto-sync write to the user's repo, never the shared upstream template. Cornelius is the first user; the mechanism is template-generic.
- **Key Features**:
  - `POST /api/agents` accepts an optional `fork_to_own` block: `{destination_repo: "owner/name", github_pat (SecretStr), private: true}`. The copy (repo creation + push of the template's default branch with full history) runs under the **user's PAT** — the platform PAT is read-only for the template clone.
  - Backend enforces `fork_to_own: required` (400 `FORK_TO_OWN_REQUIRED` without the block) so MCP/CLI paths can't silently create upstream-pointed agents. `@branch` template syntax and `local:` templates are rejected with the block (400).
  - Privacy: destination repo is **private by default**; public requires an explicit `private: false` the UI gates behind a loud warning.
  - The user PAT is persisted as the agent's per-agent PAT (#347, AES-256-GCM) so recreates re-bake it — the agent never falls back to the platform PAT.
  - Destination collision handling: non-empty repo → 409 `FORK_DESTINATION_EXISTS`, unless its only branch head matches the template tip (retry-safe reuse); repo already bound to a live agent → 409 `FORK_DESTINATION_IN_USE`; empty repo (incl. pre-created without README) is reused.
  - `upstream` remote auto-added in the agent workspace (credential-less, public templates) so `git pull upstream main` adopts template improvements; `GIT_UPSTREAM_REPO` env var baked at creation.
  - Fork-to-own agents are pinned to source mode (origin main = the brain) with the 15-min auto-sync heartbeat enabled (pushing to your own main is the point).
  - Create Agent modal renders templates carrying `fork_to_own` as **featured cards** (tagline surfaced from template.yaml) with destination/PAT/visibility fields.
- **Out of scope (v1)**: MCP `create_agent` tool does not accept `fork_to_own` (tool args are audit-logged — a PAT arg would persist in plaintext); PAT expiry/rotation UX (sync-health alerts detect push failures); upstream-update UI affordance.

### 4.5 Library Page (trinity-enterprise#263)
- **Status**: ✅ Implemented (2026-07-31)
- **Description**: The Templates page is renamed **Library** (`/library`) — one surface for installable assets: an **Agent Templates** section (the existing Starter/GitHub/Custom card grids) and a **Skills** section (fleet-level browse over the shared skills library — see skills.md §22.3). `/templates` redirects (function-form, query **and** hash preserved) so old bookmarks and deep links keep working.
- **Key Features**:
  - Stacked sections with in-page jump anchors ("Agent templates · Skills") — deliberately NO kind-filter pills and no `?kind=` query machinery (two disjoint section shapes)
  - Per-section failure isolation: a templates fetch error never blanks the skills section and vice versa; each section owns its loading/error/empty states
  - Per-kind empty states teach the next action, **per viewer role**. **Templates (#1931)**: the GitHub Templates section renders whenever the catalog is non-empty — with zero GitHub entries it shows a placeholder card instead of silently disappearing. The card leads with the **recommended** path (the `abilityai/abilities` marketplace and its `create-agent` wizards — the front door `CLAUDE.md`/`README.md` already name), then offers the *secondary* "I already have a repo" action: a **Create from a GitHub repository** button (both roles) that opens the Create-Agent dialog on its free-form `owner/repo` path, plus a role-branched curation hint — admin → *Settings → GitHub Templates*; non-admin → *"ask an admin"*. Same `useRole()` convention as the Skills section on the same page; the two halves must not diverge. Precedence: the page-level *"No templates configured"* state owns `templates.length === 0` (local **and** github), the GitHub placeholder owns *"catalog non-empty, GitHub empty"* — mutually exclusive by construction, so they can never stack. Skills: the 4-state discriminator in skills.md §22.3
  - Naming rule (AC#4 reading): page **identity** is Library — nav label, route path/name, `meta.title`, h1, e2e title assertions. The word "template" survives as the asset-kind noun (Starter/GitHub Templates section headers, Use Template buttons, `GET /api/templates` untouched)
  - Zero backend change — the skills half is a *view over* the skills.md §21 machinery (`GET /api/skills/library` + `/status`, admin `POST .../sync`); no new endpoints, no schema change
- **Not Built**: fleet-level assignment visibility (which agents carry each skill) — needs an aggregate read (e.g. `GET /api/skills/assignments`); cards link to the per-agent Skills tab via the agents list instead

---

## 5. Agent Chat & Terminal

### 5.1 Agent Terminal
- **Status**: ✅ Implemented (2025-12-25)
- **Description**: Browser-based xterm.js terminal with Claude Code TUI
- **Key Features**: PTY forwarding, mode toggle (Claude/Gemini/Bash), resize support
- **Flow**: `docs/memory/feature-flows/agent-terminal.md`

### 5.2 Chat via Backend API
- **Status**: ✅ Implemented
- **Description**: `/api/agents/{name}/chat` endpoint with stream-json output parsing

### 5.3 Conversation History
- **Status**: ✅ Implemented
- **Description**: Persistent chat history per agent stored in database

### 5.4 Context Window Tracking
- **Status**: ✅ Implemented
- **Description**: Token usage display (e.g., "45.5K / 200K") with color-coded progress bar

### 5.5 Session Cost Tracking
- **Status**: ✅ Implemented
- **Description**: Cumulative cost display across conversation

### 5.6 Authenticated Chat Tab
- **Status**: ✅ Implemented (2026-02-19)
- **Description**: Dedicated Chat tab in Agent Detail with simple bubble UI for authenticated users
- **Key Features**: Session selector dropdown, New Chat button, Dashboard activity tracking (uses `/task` endpoint), shared components with PublicChat
- **Spec**: `docs/requirements/AUTHENTICATED_CHAT_TAB.md`
- **Flow**: `docs/memory/feature-flows/authenticated-chat-tab.md`

### 5.7 Dynamic Thinking Status (THINK-001)
- **Status**: ✅ Implemented (2026-03-03, extended 2026-03-04)
- **Description**: Real-time status labels in Chat tab and Public Chat reflecting agent activity (replaces static "Thinking...")
- **Key Features**: SSE stream subscription, tool-name-to-label mapping, 500ms anti-flicker, 10s heartbeat timeout, async_mode task execution with session persistence
- **Scope**: Authenticated Chat tab + Public Chat links (both use async_mode + SSE streaming)
- **Persistence hardening (#1444)**: `async_mode` + `save_to_session` chat-session persistence is **fail-loud** (a write error logs at ERROR with a stack trace and a `chat_persist_failed` marker on the sync response; never silently swallowed, never 500s a billed turn) and **owner-checks** a caller-supplied `chat_session_id` (IDOR fix). Guarded on a SUCCESS terminal only (FAILED/CANCELLED turns write no session). Covered by a **fast unit regression guard** (`tests/unit/test_1444_chat_session_persistence.py`) — the slow `requires_agent` integration tests (`test_dynamic_thinking_status.py::TestAsyncModeSessionPersistence`) now also assert the execution reached `success` before demanding a session, disambiguating an execution failure from a persistence failure.
- **Spec**: `docs/requirements/DYNAMIC_THINKING_STATUS.md`
- **Flow**: `docs/memory/feature-flows/authenticated-chat-tab.md`

### 5.8 Session Tab — `--resume`-default Chat Surface (SESSION_TAB_2026-04)
- **Status**: ✅ Implemented (2026-05-01), GA (2026-05-04)
- **Requirement ID**: SESSION_TAB_2026-04
- **GitHub Issue**: #651
- **Description**: New Agent Detail tab that lives alongside the existing Chat tab. Each turn reattaches to the same Claude Code session via `claude --print --resume <uuid>`, preserving tool-result memory, mid-skill state, and reasoning state across messages — strictly more capable than Chat's stateless text-replay model.
- **Key Features**:
  - New `agent_sessions` and `agent_session_messages` tables, strictly parallel to `chat_sessions`/`chat_messages` (no shared state, no FK between them)
  - Six endpoints under `/api/agents/{name}/sessions*` (create, list, get, message, reset, delete)
  - `SessionPanel.vue` + `stores/sessions.js` reuse Chat sub-components for visual parity
  - Stream-json parser fix recognises `{"type":"system","subtype":"init"}` (Phase 1.3)
  - `persist_session` flag plumbed through `ParallelTaskRequest → AgentRuntime → ClaudeCodeRuntime`
  - Resume-failure fallback: clears cache, retries cold once on missing JSONL (Anthropic upstream #39667 / #53417)
  - Per-`(agent, claude_uuid)` Redis lock (`SET NX EX 300s`, 30s wait ceiling) prevents JSONL corruption (Anthropic #20992)
  - Per-user ownership returns 404 on mismatch (does not leak session-id existence — E6)
  - JSONL cleanup service: synchronous best-effort reap on reset/delete + 6h periodic sweep with 1h race guard
  - JSONL-side fallback recovery for stdout pipe race + JSONL-side compact event capture
  - Cross-session contamination empirical gate (`test_session_cross_contamination.py`, Anthropic #26964)
- **Default**: ON (`session_tab_enabled` flag flipped to True for GA on 2026-05-04, PR #652)
- **Spec**: `docs/planning/SESSION_TAB_2026-04.md`
- **Flow**: `docs/memory/feature-flows/session-tab.md`
- **Unified Chat tab (#1112)**: the separate Session tab is collapsed into the single
  **Chat** tab, which carries a **Session-mode toggle** (default ON, persisted
  per-user in `localStorage['trinity.chatMode']`). ON → `SessionPanel`; OFF →
  legacy `ChatPanel`. The toggle is hidden and the tab falls back to legacy when
  `session_tab_enabled` is off or the runtime lacks `--resume` (Codex) — never
  zero chat surfaces. `?tab=session` aliases to the Chat tab; execution-resume
  (`resumeSessionId`) forces legacy for that landing without changing the saved
  preference. See architecture → Session Tab.
- **Surface retired (5.9)**: the Session surface no longer renders on Agent Detail.
  The tables, endpoints, and the `--resume` engine all stay — the Workspace owns
  the surface now. See 5.9.

### 5.9 Workspace absorbs the Session surface
- **Status**: ✅ Implemented (2026-08-12)
- **Requirement ID**: WORKSPACE_SESSION_ABSORB
- **GitHub Issue**: abilityai/trinity-enterprise#358
- **Description**: Trinity had two overlapping continuous-conversation surfaces —
  the Agent Detail Session mode and Workspace chat. The Workspace becomes the one.
  The Session **surface** is removed from Agent Detail; the Session **engine**
  (`claude --print --resume <uuid>`, the per-`(agent, uuid)` resume lock, the
  cold-retry fallback, the JSONL reaper) is not removed — it is what Workspace
  chat now runs on.
- **Continuity is the contract, not the redirect.** Before this change, Workspace
  chat was a stateless `execute_task` with the last N messages replayed as a text
  prompt prefix: conversational recall only, no tool-result memory, no mid-skill
  state, no reasoning state. Absorbing the Session surface into that would have
  been a silent downgrade for every owner who used it. So parity comes first —
  a Workspace thread resumes exactly the way a Session did.
- **Key Features**:
  - `enterprise_portal_sessions` gains `cached_claude_session_id`,
    `last_resume_at`, `consecutive_resume_failures` — the same three fields that
    make `agent_sessions` resumable, on the thread that replaces it
  - Workspace turns run through the shared resumable-turn service: cached UUID →
    resume lock → `execute_task(persist_session=True, resume_session_id=…)` →
    single cold retry on a missing JSONL → cache the real UUID
  - History replay is **suppressed on a resume turn** — real session memory
    replaces it. The prompt prefix survives only where it is still the only
    continuity there is: a cold turn, and a runtime without `--resume` (Codex)
  - The JSONL reaper keep-set is the **union** of `agent_sessions` and
    `enterprise_portal_sessions` cached UUIDs. Without the union the 6h sweep
    deletes live Workspace JSONLs one hour after they are written, and continuity
    breaks with no error anywhere
  - `?tab=session` and legacy session deep links redirect to
    `/workspace?agent=<name>`, query-preserving, and deliberately **same-tab**
    (a URL rewrite of an in-flight navigation, not an entry point); the two
    *click* entry points open a new tab instead (ent#456, §5.16)
  - Existing `agent_sessions` rows stay readable — endpoints, store, and data are
    untouched; only the Agent Detail entry point goes away
- **Non-goal**: streaming. The Session surface never streamed (a synchronous POST
  plus a reattach poller, #1376/#759), so absorbing it into a non-streaming
  Workspace is not a regression. Workspace streaming is tracked separately in
  abilityai/trinity-enterprise#286 and is **not** a prerequisite of this change.
- **Turn bound = the agent's own timeout (#2214, 2026-08-15)**: a Workspace turn
  is bounded by the agent's `execution_timeout_seconds` (TIMEOUT-001, #665), not
  a flat 300s constant. The engine owns the read —
  `session_turn_service.resolve_turn_timeout(agent_name)`, beside
  `resolve_lock_ttl` — read-side clamped to TIMEOUT-001's own range [60, 7200]
  (the #506 stray-row pattern), fail-open to the platform default 3600 (the
  *default*, deliberately not the lock's fallback-to-*cap*: an over-TTL lock is a
  harmless auto-expiring key, an over-long turn is billable work).
  `start_portal_turn` resolves **once per turn** and threads the same value to
  the marker TTL, the 202 `wait_budget_seconds`, and the dispatch, so the three
  cannot disagree; the derived bounds (`portal_attempt_ceiling_seconds` =
  timeout + 10 + `_AUTO_RETRY_MAX_TIMEOUT_S` (imported, never copied);
  `portal_max_turn_seconds` = 2 × ceiling + 60 — the cold retry re-runs the whole
  turn) are pure functions of it, and the old module constants are deleted so a
  missed consumer fails loudly at import. **No Workspace clamp below the agent
  cap** — a clamp under 7200 re-introduces the silent-override bug for exactly
  the upper half of the range TIMEOUT-001 sells; the accepted cost is a bigger
  orphaned-marker window (hard-kill only — graceful shutdown clears the marker in
  `finally`), absolute worst `portal_max_turn_seconds(7200)` = 15,080s, precedent
  the Session surface's own ≤7230s in-flight sentinel. Operator recovery for an
  orphaned marker: `DEL portal_inflight:{session}` (the same manual-DEL escape
  the engine documents for its lock keys). A reloading client's wait budget rides
  the history response (`in_flight_wait_budget_seconds` = the marker's remaining
  TTL; fail-open to the full per-agent budget on an unreadable TTL), so reattach
  respects the same bound. A turn that hits the bound 504s naming the agent's
  limit. Configurability is by derivation: operators set the bound where they
  already set the agent timeout (`PUT /api/agents/{name}/timeout`). Long-timeout
  **headless** integrators should prefer the streaming route — the synchronous
  `POST .../chat` holds a byte-silent HTTP response for the whole turn, which is
  proxy read-timeout territory at hour scale.
- **Flow**: `docs/memory/feature-flows/session-tab.md`

### 5.10 Workspace sidebar IA — agents block, starred chats, unread badges
- **Status**: ✅ Implemented (2026-08-12)
- **Requirement ID**: WORKSPACE_SIDEBAR_IA
- **GitHub Issue**: abilityai/trinity-enterprise#359
- **Description**: The sidebar is restructured around what an agent now is. The
  roster moves to the top of the scroll region as its own surface, and chats
  follow with starred ones pinned above the date groups.
- **Why the roster gets its own surface**: once the Workspace became the only
  continuous-conversation surface (5.9), an agent stopped being an entry in a
  new-chat menu and became a destination. Rendering roster and chat history with
  identical visual weight is what made the old sidebar read as one
  undifferentiated list.
- **Key Features**:
  - Agents block renders **first**, on its own surface (card + ring); each row
    carries avatar, name, description, and a count badge when that agent has
    replies the viewer has not read
  - **A row may also carry an availability state (#2196).** Roster membership is
    a DB fact (`agent_ownership` / `agent_sharing`); whether the agent's
    container currently exists and runs is a Docker fact **projected onto** the
    card as `availability` — `ready` | `stopped` | `unavailable` | `unknown` —
    and is **never** a membership filter. `stopped` and `unavailable` render a
    chip explaining the state and naming the next action ("ask *{owner}* to
    start it"); `ready` and `unknown` render as before. Nothing is hidden and no
    control is disabled: a client whose agents are all stopped must still get a
    Workspace they can read, star and search. The field's footprint is reserved
    so a row does not reflow as an agent starts or stops, and the roster is not
    re-sorted by it (rows would jump)
  - **`unknown` is the fail-open default, deliberately inverted** from the other
    roster capability bits (`voice_available`, `multi_agent_chat_available`),
    which fail closed. Those bits' bug is *promising an affordance that cannot
    work*; this field's bug is *denying a working agent* — and at scale
    *emptying a paying customer's roster over an infrastructure fault*, since
    every Docker read in the platform collapses "no container" and "Docker could
    not be asked" into the same falsy value. When Docker is unreadable every
    card reads `unknown` and the roster renders exactly as it does today
  - The aggregate "waiting on you" count sits on the **wordmark**, since the
    agents block now occupies the top of a scrolling region
  - Starred chats are **lifted out of** the date groups, not copied above them —
    a starred chat appears exactly once
  - Star / unstar from the chat row **and** from the chat header (1:1 and room)
  - A multi-agent chat row shows every participant's avatar (capped at 3 + a
    "+N" chip), so a room is visually distinct from a 1:1
  - Clicking an agent that is waiting on you opens the conversation it is
    waiting in; with nothing unread it starts a new chat as before
  - Search **filters the agents block in place** and swaps the chat lists for
    results (ent#402, §5.16); an empty roster still ends in a next action
    (create an agent / ask whoever invited you)
- **Per-viewer state**: `enterprise_portal_chat_state`
  `(client_email, chat_kind, chat_id) → starred_at, last_read_at`. Deliberately
  **not** a column on the chat row: a room is shared between participants, so a
  star stored there would be one person's bookmark rendered in everyone else's
  sidebar, and rooms live in the private submodule while threads do not. The
  caller's email is the primary-key prefix, so the row **is** the tenant scope.
- **Unread is defined relative to a cursor**: a thread with no `last_read_at`
  reports nothing unread rather than reporting its whole history. Treating
  "never read" as "all unread" would have badged every historical conversation
  in every install the day this shipped. A cursor is written the first time the
  viewer opens or sends in a thread.
- **Endpoints**: `GET /api/enterprise/client-portal/sessions` (#2198 — the whole
  sidebar list in ONE viewer-scoped call, replacing one per-agent call per rostered
  agent; roster-scoped by the same set the per-agent gate enforces, no cap and no
  `total`, rate-limited per viewer), `GET /api/enterprise/client-portal/chat-state`,
  `PUT|DELETE .../chat-state/{kind}/{id}/star`, `POST .../chat-state/{kind}/{id}/read`.
  No roster gate (every row is keyed by the caller's own email) and no existence
  check on the id — a 404 for an unknown chat would be an enumeration oracle
  (invariant #8); two per-viewer caps bound the write instead. A total-row cap
  (abuse) and a separate **starred**-row cap: read cursors accrue from ordinary
  use, so a single cap would be spent by activity the user cannot undo, making
  the 409's "unstar some first" advice false. Unstarring a chat that carries no
  read cursor deletes its row.
- **Known gap**: rooms report `unread: 0`. A room keeps its own seq cursor, and
  reconciling the two cursor models is follow-up work; stars work for both kinds.
- **Not this issue**: opening an agent's own **page** (a destination with its own
  content rather than a chat) is abilityai/trinity-enterprise#360.
- **Flow**: `docs/memory/feature-flows/workspace-sidebar-ia.md`

### 5.11 Workspace agent page
- **Status**: ✅ Implemented (2026-08-13)
- **Requirement ID**: WORKSPACE_AGENT_PAGE
- **GitHub Issue**: abilityai/trinity-enterprise#360
- **Description**: Each agent gets a page in the Workspace — identity, health,
  recent work, reports, files, what it can do, and the place where the agent
  surfaces what it needs from the user. A roster row opens it; **Start a chat**
  is an explicit button there.
- **It reports; it does not configure.** No schedules, no skill editing, no
  logs, no costs. Model and plan are not shown at all — the AC permits them
  "informational and visibility-gated", and the cheapest way to satisfy a gate
  is to not open the door. Building agents stays operator-side.
- **The viewer may be an external client.** The same page serves a portal-token
  client and a platform user, so exclusions are enforced by **projection in the
  service**, never by filtering in the template: a field that never leaves the
  service cannot be surfaced by a later UI edit. Three that matter —
  `recent_work` drops `message`/`cost`/`model_used`/`source_user_email`, and for
  a CLIENT drops loop-triggered rows entirely (#2423 — a client can neither open
  a loop, read what it produced, nor stop one, so reporting the count without
  the output was activity it could only misread; operators keep every row); `asks`
  admits only agent-authored `approval`/`question` items (never platform
  `alert`s) and never their `context` (free-form agent JSON, a known
  credential-leak surface); report reads are agent-scoped, since report ids are
  global and the roster gate only proves the caller may reach *this* agent.
- **One field crosses deliberately (#2161)**: a row's **schedule name**. Without
  it every scheduled row rendered the identical three words. It is a short label,
  never the schedule's `message` — that is a prompt, and prompts are what this
  page exists not to show. Resolved by one **projected** query (`SELECT id, name`
  — the prompt is never loaded, so the exclusion is structural rather than a
  review invariant) into a map built from *this* agent's schedules, so a foreign
  id misses by construction; a failing read costs the labels, not the rows. It is
  **not assumed to be human-written** — schedule creation is `AuthorizedAgent`, so
  an agent-scoped key can author it — and is therefore capped and escaped.
- **Reports are rendered, never dumped (#2162)**: the Reports tab drives the shared
  `components/reports/` renderer set (`display_hint` → `report_type` prefix → shape check),
  the same dispatch Agent Detail uses — reused, not forked, because those renderer keys are
  CI-pinned as the canonical contract (`test_1535_report_prompt_guidance.py`). It shipped
  dumping `JSON.stringify(payload)` at an external client, which is the *same* disclosure this
  section already refuses for an ask's `context`: a typed renderer reads only the keys its hint
  declares, so this strictly narrows what crosses. The one deliberate divergence from the
  operator surfaces is the fallback: they keep the raw JSON viewer (useful when you are
  debugging an agent's own output), while this surface passes `:fallback-component` and an
  unrecognised payload gets a bounded, humanised key-value summary with credential-shaped tokens
  redacted and no raw payload reachable behind it. Honest limit — a summary still names every top-level
  key; it bounds and humanises the residual rather than removing it. A `table` payload is
  fetched a window at a time (`rows_offset`/`rows_limit`) so a large report never transfers
  whole, and the tab grows by an explicit "Load more" rather than a nested scroll region.
- **Key Features**:
  - Header: avatar, name, description, health, last active
  - Stats strip: tasks in window, completed rate, first-try rate, window selector
    (shown only on the tabs the window drives)
  - Tabs: Overview · Reports · Files · What it can do · Activity
  - Overview: an **unconditional 50/50 top row** — the activity chart (the shared
    `StackedBarChart`, #1107 — bounded, not a full-bleed strip) on the left,
    recent work on the right — then **open asks** full width below it, then this
    user's chats. The row splits at `xl`, not `lg`, and stacks below that: with
    the Workspace's 288px sidebar a 1024px viewport leaves each column 332px,
    where a 30-day x-axis truncates to nothing (#2169). The column count is
    independent of the data — both occupants own an empty state, so the split
    never collapses; keying it off `asks.length` was the #2169 defect
  - **#2161's "asks stay first in DOM order so the mobile stack keeps the
    priority" is superseded by #2169**, deliberately and on instruction:
    below `xl` asks are now third. Recorded rather than dropped, because the
    rationale was real. The residual is bounded — the Overview tab's ask-count
    badge sits in the header, outside the page scroller, so a narrow viewport
    still shows the count at every scroll position; only the ask text moves
    below the fold. What #2161 decided about the asks *card* is untouched:
    they stay on the Overview (not a tab), contained in place, no nested scroll
  - `PortalAvatar` carries a 1px `border-strong` edge in both themes (#2169), so
    an image avatar with light edges does not bleed into the surface behind it.
    One shared component, fourteen call sites; `box-sizing: border-box` keeps
    every outer footprint unchanged
  - Everything DB-sourced, so an agent that cannot currently run renders
    degraded, not empty: health `unknown` (monitoring is default-OFF, so
    "unhealthy" would be a lie), empty sections, and a failing data source
    degrades that section only
  - **The two non-running states are distinct and both render (#2196)**: (a) a
    **stopped** agent — container exists, not running — and (b) an agent with
    **no container at all**, which #1747 documents as a *routine* state (an
    agent's identity lives in `agent_ownership`, not Docker; #834 Phase 1c
    recovery reaches it by design, as does a `docker system prune` or a crash
    mid-create). Neither is hidden from the roster or the page. The decision
    recorded for #2196's AC #2: **the ownership row is authoritative for
    membership; container state is projected onto the card**. The header renders
    availability as its **own labelled fact beside health**, never folded into
    the health dot — health is the last persisted `agent_health_checks` row and
    is stale by design, availability is a live read, and one widget carrying two
    freshness semantics tells the viewer neither
  - This is also why the Workspace roster and `GET /api/agents` legitimately
    disagree (#2196 AC #4): the fleet list iterates Docker and so omits these
    agents entirely, while the roster lists them and says why. Making the two
    agree literally would require rewriting `/api/agents`, which #1747 argues
    against; the difference is documented rather than papered over
  - Endpoints: `GET /agents/{name}/page?window=`, `.../reports`,
    `.../reports/{id}` (optional `rows_offset`/`rows_limit` window a tabular payload,
    #2162 — two query params on the existing route, not a second route) under the
    client-portal prefix, all roster-gated
- **The AC's rating tally** was initially **not met** — nothing in Trinity
  produced ratings, so it had no data source and was omitted rather than
  invented. ent#366 then shipped that source (a Workspace thumb writes to
  `agent_evaluations` under `evaluator = workspace:<email>`), and the page
  projects the up/down counts through `_rating_tally`. The AC is now met; this
  bullet claimed otherwise for two releases after the fact (corrected in #2423
  review). The **first-try rate** beside it IS real: successes
  with `retry_count` 0, distinct from the success rate (which counts a
  retried-then-succeeded execution as a success).
- **Two of #2161's own ACs were deliberately overridden** — recorded so they are
  not "fixed" back later. Its AC #3 asked for a **message summary** in recent
  work: rejected, no prompt text reaches this surface; the schedule name answers
  the same need for schedule-backed rows, and other rows keep trigger/duration/
  time (AC #3 met for scheduled rows only). Its AC #4 asked for a **dedicated
  asks tab**: rejected, since an agent reaching you when no chat is open is the
  page's reason to exist and a tab is somewhere you must go — the defect was that
  asks were unbounded, so they are contained in place (compact, clamped, first
  five plus a counted toggle, no nested scroll per #2101).
- **The stage escape is fail-closed (#2161)**: "Start a chat" did nothing because
  the guard enumerated route params and `/workspace/a/:agentName` was added after
  it was written — the third time that list went stale (#2128 was the second).
  `shouldEscapeStage` tests route *shape*, so a future stage route cannot
  silently re-break it.
- **Supersedes**: ent#359's interim roster-click behaviour (a row with unread
  opened the unread chat). The page resolves that properly — its Overview lists
  the chats the agent belongs to, unread counts included.
- **Flow**: `docs/memory/feature-flows/workspace-agent-page.md`

### 5.12 Workspace multi-agent chats — @mention escalates a 1:1
- **Status**: ✅ Implemented (2026-08-13)
- **Requirement ID**: WORKSPACE_MENTION_TO_GROUP
- **GitHub Issue**: abilityai/trinity-enterprise#361
- **Description**: @mentioning another agent turns a conversation into a group
  discussion — from a **1:1** (which creates a room containing both agents and
  carries the message into it) and from **inside a room** (which adds the
  mentioned agent as a participant).
- **The AC was a feature request wearing a regression guard.** ent#361 AC#4 asks
  that "@mention of a non-participant *still works* and adds them (existing path
  preserved)", and its Context says a chat could already become multi-agent that
  way. Neither was true: `resolve_mentions` matched only names already in the
  room and documented that a mention "can never reach outside", and the portal
  had no mention handling at all. There was nothing to preserve.
- **Two halves, deliberately in different layers**:
  - **In-room** is engine-side (`shared_sessions.post_message` →
    `_join_mentioned_newcomers`), because agent replies flow through the engine
    and membership is its concern.
  - **1:1 → room** is a UI act: the Workspace resolves the mention against the
    roster it already holds and uses the existing `POST /api/rooms` +
    `POST /api/rooms/{id}/messages`. OSS must not import the private module, and
    routing it through the rooms API keeps `create_room`'s per-agent ACL as the
    single enforcement point rather than adding a second one.
- **Safety properties** (both halves): an @name that is not an agent the caller
  can reach stays **plain text and is never an error** — a "no such agent" reply
  would answer, for any string typed, whether an agent by that name exists;
  **only a human** may recruit (an agent that could pull agents into a room is a
  spend amplifier and a prompt-injection lever); the participant cap is
  re-checked per addition; a closed room admits nobody.
- **Mirrored pattern**: the Workspace regex mirrors the engine's `_MENTION_RE`
  so a handle that looks like a mention in the composer is one to the engine.
  Pinned on the Workspace side by tests; drift would build a room around a name
  the engine then renders as text.
- **Gating**: escalation is gated on the same rooms capability as the picker
  (#2128) — without it there is nowhere to escalate to, so an @mention stays
  ordinary text.

### 5.13 Workspace composer typeahead — `/` playbooks, `@` agents
- **Status**: ✅ Implemented (2026-08-13)
- **Requirement ID**: WORKSPACE_COMPOSER_TYPEAHEAD
- **GitHub Issue**: abilityai/trinity-enterprise#392
- **Description**: The composer's two invocation syntaxes become discoverable.
  Typing `/` at a token boundary opens a bounded list of the active agent's
  `playbooks[]` (title + description) and selecting one **splices its
  `starter_prompt` into the composer without sending** — the §5.11 briefing-card
  prefill contract, now reachable after turn 1, where the cards are gone.
  Typing `@` opens a bounded list of reachable agents, filtered on **slug and
  display label** (the roster shows labels; the parser keys on slugs), and
  selecting one inserts a token `mentionedAgents()` resolves (§5.12).
- **OSS-core by decision (ent#392): deliberately ungated** — no
  `requires_entitlement`, logic stays in the OSS tree. Recorded explicitly
  because CLAUDE.md's default for an enterprise-tracker feature is *gated unless
  ruled otherwise*, so the ruling must never be inferred later from the mere
  fact that it merged (the ent#326 / ent#384 discipline). Rationale: it extends
  a surface that is already OSS-core (the Workspace, ent#356) over data the
  client already holds — no new endpoint, no new table, no migration.
- **The trigger rule is deliberately STRICTER than the parser.** §5.12's
  `MENTION_RE` is unanchored, so `user@example.com` *parses* as `@example`; the
  typeahead only fires on a trigger char at a token start (preceded by a
  **non-word** char — not merely whitespace, or it cannot fire after CJK, an
  emoji or punctuation). Asymmetric in the only safe direction: the popup can
  never open on something the parser would not see, so `50/50`, `and/or` and an
  email address are left alone, and no offered token can fail to resolve.
- **Un-mentionable slugs are excluded, so a selected mention can never degrade
  to plain text** (the AC's central property). `sanitize_agent_name` keeps `.`
  and imposes no length cap, while the mention grammar allows neither — so
  `data.scout` is an ordinary agent whose mention resolves to nothing. Nothing
  offers such names today, which is why the failure is invisible; a list that
  included them would *manufacture* it. The predicate is **derived by asking
  `mentionedAgents` itself**, never a second copy of the grammar.
- **No implicit selection.** The roving index starts at "nothing chosen", and a
  plain Enter accepts **only** with an explicit selection — otherwise it sends.
  Tab accepts the top row. The harm is asymmetric: an accidental accept destroys
  typed work (a popup that merely happens to be open — a paste, or prose like
  "check /status of the deploy" — would splice up to 500 characters over the
  message), while an accidental send is what the user was reaching for. Esc
  dismisses and keeps the popup shut while the same token is still being typed.
- **`@` is hidden without the rooms capability** (#2128) — in the popup *and* in
  the placeholder, since a placeholder promising a capability the build lacks is
  the same dead end in text form. The placeholder is the only part of this that
  reaches a user who does not already know the feature exists.
- **Honest empty state.** A source with nothing in it shows one line; a query
  that matches nothing **closes** the popup. The copy never claims what the
  client cannot observe: `_agent_briefing` returns `[]` for a stopped or slow
  agent exactly as it does for one with no playbooks, and the briefing arrives
  AFTER the roster (§5.16, #2163) — so "no playbooks exposed" would be a false
  claim about operator configuration for the ordinary state of an idle fleet.
  The typeahead self-heals when playbooks arrive late (its source is a computed
  over the card), which is what makes the deferred hydration invisible to it. "No peers" and
  "peers exist but none is mentionable" are separate statements.
- **Scope**: `/` and `@` in the 1:1 composer; **`@` in the room composer**,
  scoped to the room's **agent participants**. That scope was established by
  *observing the running server*, not by reading the private rooms engine:
  `POST /api/rooms/{id}/messages` answered `woke: ["<participant>"]` for a
  participant mention and `woke: []` for a non-participant one, so the list
  offers only names a pick is known to wake — offering the roster would put
  names in front of the user with no evidence that choosing one does anything.
  It is deliberately **not** claimed that a non-participant mention has no
  effect: §5.12 records an engine-side newcomer-join path from ent#361, and two
  empty response fields do not disprove it. If that path is live, this list is
  narrower than the engine allows, and recruiting stays with the explicit
  "+ Add agent" control — the honest home for an action that spends money on
  another agent. **`/` in a room is deferred**: a room has N participants and no
  active agent, so "whose playbooks?" has no answer without inventing a picker
  this issue does not specify.
- **Flow**: [workspace-composer-typeahead.md](../feature-flows/workspace-composer-typeahead.md)

### 5.14 Stopping an in-flight turn — Escape and a Stop control (ent#155)
- **Status**: ✅ Implemented (2026-08-25)
- **Description**: A sent message that is still processing can be stopped from
  every conversation surface, and the text comes back into the composer so it
  can be edited and re-sent. Previously the only options were to wait for the
  turn or for its timeout, and the words were gone either way.
- **Scope**: the Agent Detail **Chat** tab, the **public link** chat, and the
  **Workspace**. The issue's AC named Session mode as a fourth; ent#358 retired
  that surface (`SessionPanel.vue` is deleted and `?tab=session` redirects), so
  the Workspace — its successor — takes its place in the list rather than the
  AC being dropped.
- **The machinery is not new, and is deliberately not re-implemented**: the
  cancel path (backend terminate → agent-server process-registry SIGINT →
  CANCELLED terminal, #679/#1332, CAS-guarded and neutral for the dispatch
  breaker) already existed and was already used by the Tasks panel. What ent#155
  adds is a trigger on the three surfaces, two new routes to reach it from the
  two credentials that are not a JWT, and the restore rule.
- **Authorization is per surface, because the principal differs**:
  - Agent Detail uses the existing `POST /api/agents/{name}/executions/{id}/terminate`
    with the operator's JWT.
  - The public link gets `POST /api/public/executions/{token}/{id}/terminate` —
    the token is the credential, exactly as for the `status` and `stream` routes
    it sits beside, and scoping is per LINK because a public link has no
    per-visitor identity to check against. Anyone holding the link can already
    watch a turn's stream — but reading is passive and cancelling destroys work, so
    that is NOT the same authority (review finding). The route additionally
    requires `triggered_by == "public"`, so a link-holder cannot reach a
    scheduled run, an operator's chat, or a Workspace turn on the same agent.
  - The Workspace gets `POST /api/enterprise/client-portal/agents/{name}/executions/{id}/terminate`
    behind the **same three gates as its stream route**, using the same
    `execution_belongs_to_caller` function rather than a second copy of the
    predicate: roster, execution-belongs-to-agent, and **started-by-this-caller**.
    The third is load-bearing — executions are agent-scoped, so without it a
    client of a shared agent could stop another client's turn by guessing an id.
- **`terminate_execution` became principal-agnostic**: `current_user` is optional
  and the activity row records an `actor_kind` (`operator` / `public_link` /
  `workspace_client`). A public visitor and a Workspace client are real people
  with no `users` row, so a NULL `user_id` is correct and the kind is what keeps
  it legible.
- **Escape is conservative by construction**: it cancels only when a turn is in
  flight, no cancel is already running, the key is not a composed IME candidate,
  no other handler has claimed it, and nothing else currently owns Escape.
  What owns it is declared PER SURFACE and declared generously — Agent Detail
  lists the voice overlay and the session menu, the Workspace lists the composer
  typeahead, the agent picker and dictation — because a missed cancel costs one
  click on Stop while a wrong one destroys work the user is still waiting for.
  Escape with nothing running is a no-op that never clears the input.
- **Restoring the words never destroys a draft**: the cancelled text is
  prepended to whatever was typed while waiting, and the merge is idempotent, so
  pressing Escape and then Stop cannot stack two copies.
- **Honest status**: a successful cancel renders as cancelled, not as an error;
  a cancel that lost the race to a finished turn answers `already_terminal` / `already_finished` and
  says nothing at all, because the reply is already on screen; a *refused*
  terminate leaves the input untouched and says the turn is still running —
  restoring the text there would imply a stop that did not happen.
- **Rules are pure** (`utils/turnCancel.js`) and shared by all three surfaces,
  because `vitest.config.js` runs `environment: 'node'` with no mount harness: a
  rule decided inside an SFC is a rule no test can reach. The Stop control lives
  in the shared `ChatInput` for the two chat surfaces — one control, not a
  second hand-built copy (#2370's lesson).
- **OSS-core by decision (ent#155)**: deliberately ungated — no
  `requires_entitlement`, logic stays in the OSS tree. Recorded explicitly
  because CLAUDE.md's default for an enterprise-tracker feature is *gated unless
  ruled otherwise*, so the ruling must never be inferred later from the mere
  fact that it merged. Rationale: two of the three surfaces are OSS-core chat,
  the cancel machinery is OSS-core, and Workspace ships OSS-core throughout.
- **Flow**: [chat-turn-cancellation.md](../feature-flows/chat-turn-cancellation.md)

---

### 5.14 Workspace deliverables — reports gain an audience and a place to appear (trinity-enterprise#365)

**Description**: An agent's structured reports (#918) become **deliverables**: output
addressed to a specific Workspace user, listed on that agent's page for them, and
rendered as a card in the chat that produced it. A deliverable is not a new store — it
is existing output gaining an audience.

- **FR-1 — The audience is a validated column**: `agent_reports.addressed_to_email`
  (nullable; NULL = operator-only, which is what every report published before it
  meant). The MCP `report` tool takes `audience_email`, and the create route checks it
  against the publishing agent's own roster (`db.email_has_agent_access`) — the same
  predicate the #848 inline-auth path gates on, so "can be addressed" cannot drift from
  "can reach". An address the agent does not already talk to is refused with a **named
  400** that says how to fix it; an unreadable roster is a **503**, never a publish. The
  address is deliberately not a key inside `payload`: that is agent-authored free-form
  JSON, so an audience buried there would let a prompt-injected agent decide whose
  Workspace its output appears in (the ent#364 rule, restated for a bigger blob).
- **FR-2 — The Workspace read is scoped to the reader**: `agent_page.reports` asks
  `db.get_reports_for_client(agent, email)`. It previously called the OPERATOR accessor
  (`get_reports_for_agent`), so **every rostered client of an agent saw every report it
  had ever published**, including reports produced for a different client — the same
  defect ent#428 fixed on the sibling ask surface, over a larger payload. The detail
  read carries the same gate (`get_report_for_client`) **in addition to** the ent#360
  agent check, and `client_email` is a **required** keyword: a default would make the
  gate fail open, which is the defect itself.
- **FR-3 — Unaddressed output stays operator-only**: NULL-audience reports no longer
  appear in the Workspace at all. This is a deliberate behaviour change — an install
  whose agents have not adopted `audience_email` shows an empty Workspace Reports tab
  rather than another client's deliverables. The operator surfaces (Agent Detail, the
  fleet Reports view) are untouched.
- **FR-4 — The chat is resolved server-side**: `agent_reports.portal_session_id` is
  filled from the *publishing turn* — the agent passes `execution_id`, the backend
  confirms the execution belongs to that agent (`resolve_and_validate_execution`, the
  MEM-001 rule) and reads the session from the ent#286 in-flight reverse marker. The
  agent never names a conversation, so it cannot post a card into a chat it was not part
  of. Absent, expired, or a non-portal turn ⇒ NULL: the deliverable still lists on the
  agent page, it simply has no card.
- **FR-5 — One rendering layer**: cards render through the shared `components/reports/`
  dispatch (Technical Notes: "do not build a second rendering layer"), with the #2162
  client rule — `:fallback-component="ReportSummary"`, so an unknown shape degrades to a
  bounded humanised summary and never a raw payload dump.
- **FR-6 — Read after a turn, not on a timer**: a turn is the only thing that can
  produce a deliverable in a chat, so the card list re-reads exactly then. No poll.
- **FR-7 — Files**: file scoping is unchanged in this pass, as the issue directs — the
  per-agent inbox boundary stays where it is (it is where the last two portal security
  bugs lived). Shared files therefore keep listing per agent and are not yet addressable.
- **FR-8 — Ratings deferred**: AC #6 asks deliverable cards to carry the rating
  affordance from trinity-enterprise#366, which is not built. The card is the surface it
  will attach to; nothing here pre-empts its shape.
- **Migrations**: dual-track — `db/migrations.py::_migrate_report_audience` +
  Alembic `0046_report_audience`, both nullable-with-no-default, plus two indexes
  (`(addressed_to_email, agent_name, created_at)` and `(portal_session_id, created_at)`)
  because the Workspace reads by audience on a table retention lets grow to 90 days.
- **Flow**: `docs/memory/feature-flows/workspace-deliverables.md`

### 5.15 Workspace ratings — thumbs on a message, Useful on a deliverable (trinity-enterprise#366)

**Description**: One-click feedback in the Workspace. Thumbs up/down on an agent
message, Useful / Not what I needed on a deliverable card (the affordance §5.14
left that card as the surface for). A negative rating opens an optional comment
box; the words are recorded either way and handed to the agent's
`capture-feedback` skill when it has one.

- **FR-1 — A rating is a platform primitive, not a skill**: it writes to
  `agent_evaluations` (§ ent#206's referee surface) under an evaluator of
  `workspace:<email>`. A capture-feedback skill runs *inside* the agent, so it
  can summarise charitably, omit, or fail silently — and a user rating is the one
  score that must not pass through the thing being scored. The rated agent has no
  write path to this table; ent#366 **amends** that write fence to admit a
  Workspace principal rather than widening it for anyone else.
- **FR-2 — The target is checked against the reader**: message and report ids are
  global, so an id alone proves nothing. A **message** must belong to this agent
  AND this client and be the *agent's* message (rating your own is refused — it
  would put a self-rating in the agent's tally); a **deliverable** reuses §5.14's
  audience gate, so "can rate" and "was addressed to you" are one question
  answered in one place. A target that fails either check returns the **same 404**
  a missing one does (Invariant #8).
- **FR-3 — Idempotent per person per target**: `UNIQUE(evaluator, target_kind,
  target_id) WHERE target_id IS NOT NULL`. A second thumb is a correction, which
  is what makes a tally count **people rather than clicks**. Partial, so the
  graded-run rows a Tier-0 pass writes (no target) are untouched.
- **FR-4 — A raw tally, never a percentage** (agent page): one thumbs-down out of
  one rating renders as "100% negative" — a number that looks like evidence and
  is not. Both counts cross to the page; an unreadable tally is flagged
  `unavailable` so it cannot render as a real zero.
- **FR-5 — The rated agent reads tallies, never the words** (the issue's open
  grooming question, decided): `_redact_for_agent_principal` strips `comment` for
  an agent-scoped caller and sets `comment_withheld`, so a reader can tell "no
  comment" from "not yours to read". Two reasons: a score an agent can read is a
  loop it may optimise for, and the comment is untrusted text written by an
  annoyed stranger — handing it verbatim to the agent being criticised is a
  prompt-injection path into it. Operator surfaces see the text.
- **FR-6 — Degrades without the skill**: the rating and comment are durable
  **before** anything is dispatched. With the skill, a background turn runs
  `capture-feedback` with the client's words **fenced as data** (the
  `routers/webhooks.py` framing) and never in the client's own thread; without
  it, the response says `skill_not_installed` and the UI thanks the person for
  words that were recorded rather than promising a follow-up.
- **FR-7 — A failed rating is shown, not swallowed**: unlike the fail-soft
  deliverables read, the store surfaces the error next to the control — a rating
  that silently did not record leaves the person believing they were heard.
- **Not NPS**: a promoter percentage from a handful of users is a number that
  looks like evidence and is not. The free text was the valuable part.
- **Migrations**: dual-track — `_migrate_workspace_ratings` + Alembic
  `0047_workspace_ratings`; four nullable columns (`target_kind`, `target_id`,
  `comment`, `updated_at`) and the partial UNIQUE above.
- **Flow**: `docs/memory/feature-flows/workspace-ratings.md`

### 5.16 Workspace roster latency floor — briefing hydration off the critical path (#2163)

- **Status**: ✅ Implemented · **ID**: `WORKSPACE_ROSTER_BRIEFING_DEFERRED`
- **Description**: `GET /my-agents` fanned `_agent_briefing` across every card
  and awaited `asyncio.gather`, which waits for ALL — so the Workspace's first
  paint was bounded by the SLOWEST agent in the fleet, for every user, on every
  sign-in, regardless of fleet size. The briefing is now hydrated after the
  roster, and every briefing that still runs is bounded.

- **AC-1 — one unresponsive agent does not delay the roster**: `get_roster`
  awaits no agent HTTP at all (two SQL reads and one Docker list). Pinned by a
  stub that never resolves: under the old code the call could not return.
- **AC-2 — the briefing still renders hints, never silently empty**: an explicit
  loading treatment, then a terminal that is hints, an honest "no hints" line,
  or an honest "couldn't load" line (ent#380's "no dead chrome").
- **AC-3 — measured before/after with a deliberately unresponsive agent**: the
  wedged case is *container running, server not answering* (`kill -STOP` on
  `agent-server.py` inside the container). `docker pause` measures nothing —
  a non-`running` container reads `availability="stopped"`, which the briefing
  skips before any HTTP.
- **AC-4 — the standard first-load motion**: three `ScanlineReveal` zones, each
  keyed on its own "no data yet" — the stage, the conversation body, and the
  briefing hint zone. The static "Opening this conversation…" / "Loading…" lines
  are gone. A background refetch never re-enters loading.

- **The bound (option 2, the belt)**: `_BRIEFING_HTTP_TIMEOUT_SECONDS = 2.0`
  (httpx, PER PHASE) and `_BRIEFING_BUDGET_SECONDS = 3.0` (wall clock, via
  `_bounded_briefing`). The literal `5.0` it replaces was never a ceiling — two
  sequential GETs, each with a per-phase timeout. Constants, not settings and
  not env vars (`SAMPLE_INTERVAL_SECONDS` precedent #1644; an unforwarded env
  read is inert while reading as configurable, #1039). Both values confirmed
  against the healthy-busy tail at verification: `GET /api/skills` is a
  synchronous directory scan on the agent-server's own event loop, so a healthy
  agent mid-turn can legitimately exceed a second.
- **`briefing_state` is a SERVER-owned tri-state** on the card —
  `pending | ready | unavailable`, default `"ready"` so an older payload reads
  as resolved-inline. A bound trip reports `unavailable`; it must never pass for
  an agent that genuinely has no hints, and a headless ent#83 client must not
  have to reinvent the third value from empty fields. `ready` means THE AGENT
  ANSWERED inside the budget, NOT "returned data". A data-state marker, never a
  capability — #2128's rule (the roster payload is the portal capability
  channel) is untouched.
- **The verdict follows REACHABILITY, not the door the failure exited by.**
  Measured at verification and fixed before ship: `_agent_briefing` swallows
  HTTP failures in a `try/except` per GET leg AND an outer one, so a wedged
  agent (httpx `ReadTimeout`) and a missing container (`ConnectError`) — the two
  commonest unreachable shapes — returned an ordinary empty briefing well inside
  the budget and were published as `ready`. Only the tarpit shape, which trips
  the wall clock, was correct. That is the hint-less-agent state this field
  exists to prevent, and it is unrecoverable in-session because
  `shouldRequestBriefing` retries only `unavailable`. Reachability is therefore
  reported separately from content: every exit of `_agent_briefing` that got no
  answer out of the agent (the availability skip, both legs failing at the
  transport layer, a failure before the first request) returns the `_UNREACHED`
  sentinel, read by IDENTITY in `_bounded_briefing` — equality would sweep up
  the empty briefing a healthy agent legitimately produces. A response of ANY
  status counts as reached (a 500 is the agent talking; retrying returns the
  same 500), and ONE leg answering is enough, because the client renders
  `unavailable` INSTEAD of the fields and a half-answered briefing must not
  discard the description it did get. Both doors — `get_agent_card` and
  `GET /briefings` — inherit it from `_bounded_briefing`, so they cannot
  disagree about the same agent.
- **Route**: `GET /api/enterprise/client-portal/briefings[?agents=a,b]`,
  viewer-scoped like `/sessions`. Scope is the roster and the ROSTER's strings
  are what is iterated, so a crafted name cannot steer the agent HTTP target;
  unknown names are dropped (no existence oracle, Invariant #8). No `?agents=`
  briefs the whole roster; a filter briefs the active agent so its hints arrive
  at its own speed. Per-viewer rate limits, the unfiltered form far tighter
  (10/min vs 60/min) because one call costs one bounded agent request per
  rostered agent. No Docker read. No MCP tool (read-only, portal-principal-only,
  no operator consumer — Invariant #13); no `Idempotency-Key` (a read, not a
  trigger boundary); no DB change, no migration.
- **Client**: the background batch fires from the store's roster-SUCCESS branch
  (both "Try again" buttons bypass `bootstrap()`) at >= 1 pending card; the
  active agent's single is driven by `Portal.vue`'s `activeAgent` watcher and is
  never coalesced into the batch. A hydrated card survives a roster refetch;
  `unavailable` is re-armed as `pending` by an explicit refetch and retried at
  most once per session otherwise.
- **Out of scope, deliberately**: a server-side briefing cache (needs Redis +
  invalidation under `--workers 2`; off the critical path the per-agent cost is
  no longer user-visible), bounding the roster's Docker read, and the sweep of
  the remaining bespoke Workspace indicators (`PortalFilesPanel`'s spinner,
  `PortalSidebar`'s skeleton) — those stay on #1921.
- **Flow**: `docs/memory/feature-flows/workspace-roster-briefing.md`
### 5.17 Workspace thread & sidebar — code blocks and copy (#2515), new-tab entry (trinity-enterprise#456), agent search (trinity-enterprise#402)

**Description**: Three changes to the same OSS-core Workspace surface. A fenced
code block in an agent's reply now reads as code and can be copied; the two
console entry points to the Workspace open a new tab; and the sidebar search
filters the agent roster, not only the chat list.

**OSS-core by decision (ent#456 / ent#402): deliberately ungated** — no
`requires_entitlement`, no registry read, logic stays in the OSS tree. Recorded
explicitly because CLAUDE.md's default for an enterprise-tracker feature is
*gated unless ruled otherwise*, so the ruling must never be inferred later from
the mere fact that it merged (the ent#326 / ent#384 / ent#392 discipline). Both
change a surface that moved to OSS core in ent#356 and carry no gate-able
capability: a link target, and a client-side filter over a roster the caller
already holds.

#### Code blocks read as code, and the thread can be copied (#2515)

- **FR-1 — One markdown body, one stylesheet, one copy handler**:
  `components/portal/PortalMarkdown.vue` is the single home of the rendered
  assistant body — the one `v-html`, the one `.prose-portal` block, the one
  delegated copy handler. `PortalAgentBubble.vue` is the chat chrome around it
  and both transcripts (`PortalConversation.vue`, `PortalRoom.vue`) mount that.
  The previous shape had the same stylesheet copied into two SFCs *specifically
  so the two could not drift* — which is the drift this closes rather than
  restates. A future consumer that renders agent markdown outside a chat bubble
  (the ent#486 Files tab) mounts `PortalMarkdown.vue` and inherits render, style
  and copy as one unit instead of re-copying two of the three.
- **FR-2 — Decoration is opt-in per consumer**: `renderMarkdown` has twelve
  consumers (dashboards, queue cards, reports, executions, loops, compatibility,
  Agent Detail chat, both portal transcripts). A global `marked` renderer
  override for the `code` token would sprout a Workspace copy control on all of
  them, so the code-block treatment is a **separate export**,
  `renderMarkdownWithCodeBlocks`, and `renderMarkdown`'s body is unchanged.
- **FR-3 — Decoration runs BEFORE sanitization, and forged markers are stripped
  first**: the pipeline is `marked → stripCodeBlockMarkers → decorateCodeBlocks
  → DOMPurify.sanitize → v-html`, so every byte that reaches `v-html` has passed
  the one DOMPurify policy (H-005 stays literally true). marked passes raw HTML
  in markdown through unescaped and DOMPurify keeps `data-*` and `style`, so an
  agent could otherwise emit a *forged* wrapper whose Copy button resolves to a
  hidden `<pre style="display:none">` — pastejacking. Agent-supplied
  `data-code-block` / `data-copy-code` markers are therefore removed from the
  input before decoration, so only decorator-built wrappers ever carry them, and
  the handler reads `:scope > pre` (the wrapper's own child) and nothing else.
  The decorator additionally refuses any opener marked would not have written —
  the `<code>` tag must carry nothing but an optional `class` — because DOMPurify
  keeps `hidden` and `style`, so a raw `<pre><code hidden>` would otherwise be
  handed a real Copy button over a block that renders empty: the same pastejack
  through the opener rather than through a forged wrapper.
  The only non-constant byte the decorator injects is the language label, which
  is charset-validated (`^[a-z0-9][a-z0-9_+#.-]{0,23}$`, so it cannot contain
  `<>&"'`) and falls back to a neutral "code".
- **FR-4 — Wrap at the edge, never a horizontal scroller**: a block wraps
  (`white-space: pre-wrap`, `overflow-wrap: anywhere`) and never widens the
  bubble or the column at any width. Copied text is still exact — the copy reads
  `textContent`, so wrapping is a display property only. Accepted cost: ASCII
  tables and box-drawing inside a block lose their alignment on a narrow column.
- **FR-5 — Two copy controls, both keyboard-reachable**: a per-block **Copy** in
  the block's own bar (always visible — it is chrome, not a hover overlay, so it
  is discoverable on touch with no `@media` rule) copying that block's text, and
  a per-message **Copy message** in an action row beneath the bubble copying the
  raw markdown. Both are native `<button>`s (Enter/Space work), both carry an
  `aria-label`, and feedback is mirrored into an `aria-live="polite"` region.
- **FR-6 — Clipboard failure is named, never silent, and has a working
  fallback**: `utils/clipboard.js::copyText` returns a result and never throws or
  logs the copied text. `navigator.clipboard` is undefined on an insecure origin
  — plain `http://<lan-or-tailscale-ip>` is a first-class Trinity topology — so a
  missing `writeText` falls back to a temporary off-screen `<textarea>` +
  `execCommand('copy')`. Only when both fail does the control say so: "Copy
  unavailable" / "Copy blocked" (a denied permission) / "Copy failed", for ~2 s,
  in text **and** colour. `writeText` is the first await in the click task
  (Safari's transient-activation rule), and the control's label and `aria-label`
  are restored from constants after the window — never from a captured previous
  value, so two clicks inside the window cannot freeze it on "Copied".

#### The console's Workspace links open a new tab (trinity-enterprise#456)

- **FR-7 — Two links, `target="_blank" rel="noopener"`**: the NavBar
  **Workspace** entry and Agent Detail's **Continue in Workspace →** link. Vue
  Router's `guardEvent` skips interception on `_blank` and on modified clicks, so
  `<router-link>` still resolves the `href` while the browser owns the click —
  **no `window.open`**, and cmd/ctrl/shift-click keep their native behaviour.
- **FR-8 — The `?tab=session` redirect stays same-tab**: it is a `router.replace`
  rewrite of an in-flight navigation, not an entry point; a redirect that spawned
  a tab would leave the user's original tab on a URL they never asked for.
- **Not in scope**: any preference for the behaviour — the new tab is simply the
  default.

#### Sidebar search filters agents, not just chats (trinity-enterprise#402)

- **FR-9 — One matching rule, reused**: agent matching is
  `filterAgentCandidates` (the ent#392 composer rule), called through
  `searchAgents` with **`requireMentionable: false`** — the flag lives inside
  that helper so a caller cannot forget it. A dotted slug like `data.scout` is
  openable even though it is not @mentionable; excluding it would hide a real
  agent from a search for its own name. No second hand-rolled predicate.
- **FR-10 — An ask-bearing match is never hidden by the results window**:
  results are bounded by `visibleAgentRows` — the same #2424 rule the steady
  state uses — so an agent waiting on you cannot be collapsed out of its own
  search result, and the same single persistent "Show all (N)" toggle expands
  both modes (#2159: alternating two `v-if` buttons drops keyboard focus).
  Search reaches agents beyond the collapse limit.
- **FR-11 — Agents first, in a labelled section, with the steady state's row**:
  the row markup, badges, availability chip and `open-agent` emit are written
  once and reused in both modes, so they are inherited rather than copied.
- **FR-12 — Per-section honest states, and loading is not empty**: "nothing
  matched at all" (both lines + a next-action hint) is distinguishable from
  "agents matched, no chats" (the chat line alone); neither line ever stands in
  for the other. The agent half answers for itself: it is a client-side filter
  over a roster already in hand, so it states its own emptiness even while the
  chat request is still in flight — that request's flag is set on every
  keystroke, so gating the agents line on it would withhold the sentence for the
  whole time someone is typing. While the roster has not loaded the skeleton
  stays — a two-character query on a slow roster must never read "No agents
  match." over a roster that has not arrived. The placeholder says agents
  **and** chats.
  *Known limitation*: a failed chat-search request is swallowed into `[]` by the
  view, so it currently reads as "No chats match."; fixing that is a change to
  `views/Portal.vue` and is tracked separately.

- **Flows**: `docs/memory/feature-flows/workspace-thread-code-blocks.md`,
  `workspace-sidebar-ia.md`, `workspace-absorbs-session.md`
### 5.18 Agent canvas — a durable surface an agent renders onto (trinity-enterprise#438)

- **Status**: ✅ Implemented (2026-09-02)
- **Requirement ID**: AGENT_CANVAS
- **GitHub Issue**: abilityai/trinity-enterprise#438
- **Description**: Every agent gets a **canvas** — a named, durable surface it
  writes structured blocks onto and *updates over time*. Reports (§5.14) are the
  immutable half: a thing published once, addressed to a person, accumulating as
  a record. A canvas is the living half: one addressable surface per topic that
  the agent keeps current. Before this, the only canvas Trinity had was
  `VoiceSession.panel_state` — in-memory, written only by the Gemini Live voice
  tools, on one page, gated behind `WORKSPACE_ENABLED && GEMINI_API_KEY`, and
  gone when the session ended.
- **OSS-core by decision (ent#438): deliberately ungated** — no
  `requires_entitlement`, logic stays in the OSS tree. Recorded explicitly
  because the default for an enterprise-tracker feature is *gated unless ruled
  otherwise*, so the ruling must never be inferred later from the mere fact that
  it merged (the ent#326 / ent#384 / ent#392 discipline). Rationale, on operator
  instruction: Workspace and everything around it is OSS.

- **FR-1 — One workspace, not two** (AC 1): `/agents/:name/workspace` — the
  voice-orb-plus-panel page — is **deleted**, and the route becomes a
  query-preserving redirect to `/workspace?agent=<name>`. It is safe to delete
  because ent#440 already put voice conversation *inside* the Workspace, so once
  the canvas moves the page has no capability of its own left. Same shape as the
  §5.9 (ent#358) and ent#381 retirements: the surface goes, the route keeps
  working.
- **FR-2 — The canvas is a row, addressed by (agent, canvas_id)**:
  `agent_canvases` with a composite primary key, so "update it over time" is an
  upsert and addressability is structural rather than a convention. `canvas_id`
  is agent-chosen and charset-validated (`^[A-Za-z0-9._-]{1,64}$`) — the same
  guard the #919 pipeline ids carry, for the same reason: it lands in a URL.
  Survives reload and agent restart because it is a row and not process state
  (AC 5).
- **FR-3 — Blocks are typed, and the renderer is the one that already exists**
  (AC 4): a canvas is an ordered list of blocks, each
  `{kind, title?, payload}`. `table` / `kpi` / `markdown` / `timeline` / `json`
  delegate to the shared `components/reports/` dispatch — *reused, not forked*,
  because those renderer keys are CI-pinned as the canonical contract
  (`test_1535_report_prompt_guidance.py`), and forking them is what §5.11 and
  §5.14 both refused. The canvas adds two kinds that dispatch cannot serve:
  `chart` (over the existing `TrendLineChart`) and `html` (DOMPurify-sanitised
  at render, H-005). The report `display_hint` enum is deliberately **not**
  widened: a canvas is a superset of a report's rendering, not a change to what
  a report is.
- **FR-4 — Visibility is an explicit agent act, defaulting to operator-only**
  (AC 8): each canvas carries `audience` ∈ `operator` (default) | `roster`.
  `operator` is visible only on Agent Detail; `roster` additionally appears on
  the agent's Workspace page to anyone already rostered on that agent. Default
  operator-only is the fail-closed direction and is what makes "a canvas never
  widens who can see the agent's output" true by construction — publishing to a
  client is a thing the agent has to *say*, mirroring §5.14's rule that an
  unaddressed report stays operator-only. The audience is a validated column,
  never a key inside `blocks`, for the ent#364 reason: `blocks` is agent-authored
  and a prompt-injected agent must not be able to decide who reads it.
- **FR-5 — Staleness is derived, not a clock** (AC 7): the canvas always renders
  `updated_at`, and is marked **may be out of date** when the agent has had a
  terminal execution *after* the canvas was last written — i.e. it did work and
  did not refresh this surface. An arbitrary age threshold was rejected: a canvas
  has no inherent freshness expectation, so a clock would either cry wolf on a
  monthly report or stay silent on a minute-by-minute one, whereas "the agent has
  run since" is a fact about *this* canvas. `updated_by_execution_id` records
  which run wrote it, so the claim is checkable.
- **FR-6 — Writes are self-gated, bounded, and provenance-stamped**: the write
  routes take `AuthorizedAgentByName` **plus** the #918 self-check
  (`current_user.agent_name == name` for an agent-scoped key), so a sibling agent
  an owner also shares cannot paint on this agent's canvas. Per-agent rate limit
  and a byte cap on the serialized blocks (413 over cap), both reusing the #918
  primitives. `execution_id` is validated through `resolve_and_validate_execution`
  (the MEM-001 rule) rather than trusted.
- **FR-7 — The voice panel becomes the canvas**: `gemini_voice`'s
  `show_markdown` / `update_panel` / `append_to_panel` / `clear_panel` now write
  the durable canvas (`canvas_id="voice"`) instead of session memory, so AC 2 is
  met by the capability *moving* rather than by being dropped with a stated
  reason. They inherit `audience="operator"`, which is what a voice session on an
  operator-authenticated page always was.
- **FR-8 — Empty state offers the next action** (AC 6): a canvas-less agent
  renders what a canvas is and the one-line tool call that creates one on the
  operator surface; on the Workspace it says the agent has not published one and
  offers the chat, since a client has no way to write one and a dead panel would
  be the §5.11 blank-panel defect.
- **Cascade + retention**: `agent_canvases` is registered in `AGENT_REFS`
  (CASCADE) so rename re-keys and the #834 hard purge wipes it — CI-blocking via
  `test_agent_cleanup_parity`. Deliberately **no** retention window: a canvas is
  bounded by construction (one row per `(agent, canvas_id)`, replaced on write),
  unlike the append-only tables `RETENTION_OPS_KEYS` governs.
- **Migrations**: dual-track — `db/migrations.py::agent_canvases_table` + Alembic
  `0050_agent_canvases`.
- **Flow**: `docs/memory/feature-flows/agent-canvas.md`

## 6. Activity Monitoring

### 6.1 Unified Activity Panel
- **Status**: ✅ Implemented
- **Description**: Real-time tool execution tracking with `--output-format stream-json --verbose`

### 6.2 Tool Chips with Counts
- **Status**: ✅ Implemented
- **Description**: Visual counts per tool type, sorted by frequency

### 6.3 Expandable Timeline
- **Status**: ✅ Implemented
- **Description**: List of all tool calls with timestamps and durations

### 6.4 Unified Activity Stream
- **Status**: ✅ Implemented (2025-12-02)
- **Description**: Centralized `agent_activities` table for all runtime activities
- **Flow**: `docs/memory/feature-flows/activity-stream.md`

---

## 9. Agent Collaboration

### 9.1 Agent-to-Agent Communication
- **Status**: ✅ Implemented (2025-11-29)
- **Description**: Agents communicate via Trinity MCP with agent-scoped API keys
- **Flow**: `docs/memory/feature-flows/agent-to-agent-collaboration.md`

### 9.2 Agent Permissions
- **Status**: ✅ Implemented (2025-12-10, Updated 2026-02-19)
- **Description**: Explicit permission model controlling which agents can call which
- **Key Features**: Permissions tab in UI, restrictive default (no auto-grant), explicit opt-in
- **Flow**: `docs/memory/feature-flows/agent-permissions.md`

### 9.3 Agent Shared Folders
- **Status**: ✅ Implemented (2025-12-13)
- **Description**: File-based collaboration via shared Docker volumes
- **Key Features**: Expose/consume toggles, permission-gated mounting
- **Flow**: `docs/memory/feature-flows/agent-shared-folders.md`

### 9.4 Collaboration Dashboard
- **Status**: ✅ Implemented (2025-12-02)
- **Description**: Real-time visual graph showing agents and animated connections
- **Key Features**: Vue Flow, draggable nodes, context progress bars, replay mode
- **Flow**: `docs/memory/feature-flows/agent-network.md`

### 9.5 Dashboard Timeline View
- **Status**: ✅ Implemented (2026-01-10)
- **Description**: Graph/Timeline mode toggle with execution visualization
- **Key Features**: Execution boxes (color-coded by trigger), collaboration arrows, live streaming
- **Flow**: `docs/memory/feature-flows/dashboard-timeline-view.md`

### 9.6 Replay Timeline Component
- **Status**: ✅ Implemented (2026-01-04)
- **Description**: Waterfall-style timeline visualization of agent activities
- **Key Features**: Zoom controls (50%-2000%), agent rows, activity bars, communication arrows
- **Flow**: `docs/memory/feature-flows/replay-timeline.md`

### 9.7 Task DAG System
- **Status**: ❌ Removed (2025-12-23)
- **Reason**: Individual agent planning deferred to orchestrator-level. Claude Code handles task management internally.

### 9.8 Dashboard Grid View (trinity-enterprise#47)
- **Status**: ✅ Implemented (2026-07-06)
- **Description**: One of three dashboard modes (Timeline / Grid / List; Timeline default — the legacy Graph mode was decommissioned in #1689, and the List mode landed in trinity-enterprise#260, §9.9) — a magnetic tile canvas: rich 384×216 landscape agent tiles snapping to a sparse, unbounded lattice the operator arranges freely, on the same pan/zoom dotted-canvas language as the graph view. Not the default (Timeline remains default for new users); selection persists to localStorage.
- **Key Features**: iPhone-style drag with live socket preview + swap-with-preview; Tidy up / Reset; keyboard arrow reorder; per-user layout (`agent → {col,row}`, localStorage v1, self-healing); five-zone tile (identity with half-out avatar, adaptive chip strip with live working timer, Activity·14d stacked-by-trigger + Context·7d trend charts, success micro-meter + stats, Run/Auto toggles); system agent keeps its purple treatment; `prefers-reduced-motion` honored.
- **Performance (first-class)**: skeleton-first render from `/api/agents`; per-tile analytics hydrate lazily (viewport-gated, concurrency-capped) into the existing `(agent, window)` cache with stale-while-revalidate; batch endpoints for chip data (sync-health, operator-queue) on a visibility-aware poll that tears down when the mode is inactive; viewport culling for 50+ fleets. **No new backend endpoints** — reads `/api/agents/{name}/analytics` (#1107), fleet context/execution/slot stats, `/api/agents/sync-health` (#389), operator-queue pending.
- **Out of scope (follow-ups)**: fleet KPI strip; "Needs your attention" + live-activity right rail.
- **Flow**: `docs/memory/feature-flows/dashboard-grid-view.md`

### 9.9 Dashboard List View — Agents-page consolidation (trinity-enterprise#260)
- **Status**: ✅ Implemented (2026-07-30)
- **Description**: Third dashboard mode **List** (Timeline / Grid / List) that replaces the standalone Agents page — the dashboard is the single canonical fleet surface. The Agents page's row list (three responsive layouts, per-row toggles, bulk tag ops, filters, empty states) is extracted into `components/AgentListPanel.vue`, mounted through the existing view-mode machinery (`VIEW_MODES` + `localStorage['trinity-dashboard-view']` — selection persists per user like the other modes). `views/Agents.vue` is deleted.
- **Key Features**:
  - **Full Agents-page parity** (28-item inventory audited, zero silent losses): name search (slug + display label, #1642) and status filter live in the List toolbar under NEW persisted keys `trinity-dashboard-list-filter-name` / `-status` (a clean break — the old page-scoped `trinity-agents-filter-*` keys are no longer read); sort dropdown bound to `agentsStore.sortBy` with the comparator extracted to `utils/agentSort.js` (system rows pinned first; `success_desc` gains a no-data-to-bottom tiebreak); row checkboxes + sticky bulk toolbar with bulk Add/Remove Tag; avatar-half-out rows with SYSTEM/GHOST/Shared badges in the name cell and the subscription-pressure badge plus a **non-default-runtime** badge on the row's secondary line beside the slug, activity + sync-health dots, success-rate bar, exec/schedule stats, CapacityMeter (#2358 — at `lg` the header and every row are items of ONE CSS grid (subgrid), so columns resolve in one sizing context, and the label leads with the slug following as selectable secondary text per §1.3.1 FR-4); filtered-empty ("No matching agents" + Clear all) and chassis-level true-empty ("Get started" → onboarding wizard) states; toast feedback.
  - **Filters migrated to chassis controls**: the page's single-tag dropdown and owner dropdown are superseded by the dashboard's existing quick-tag filter (multi-tag, server-side, counts) and owner filter, which apply to all three views; the List's Clear-all clears both layers (local name/status + chassis tags/owner via a `clear-chassis-filters` emit). The "X/Y" badge counts Y as the full fleet.
  - **Create Agent moved to the chassis header** — available in all three modes (previously the Agents page was the only persistent create surface); modal close refreshes the fleet.
  - **System-row Run guard adopted from the grid**: the List hides the Run toggle on system rows (the grid tile already refused it); stopping the system agent remains available on its Agent Detail page.
  - **Redirect**: `/agents` → `/?view=list` (query-preserving function redirect; `/agents/:name` and deeper untouched). The `?view=` intent is applied via a route watch as a one-shot, NON-persisting mode change, then stripped from the URL — a stale bookmark never rewrites the user's saved view selection. `?view=` doubles as a general non-persisting deep-link for all modes.
  - **NavBar consolidation**: the Agents entry is removed; the Dashboard link highlights on `/` and on `/agents/:name` pages (successor to the old `isAgentSection` highlight).
- **Performance**: zero per-row HTTP — `tags` and `read_only_enabled` ride every `GET /api/agents` row, so both Agents-page N+1 mount loops (per-agent tags + read-only fetches) are deleted (also more correct: the per-agent read-only GET 404'd on stopped containers and was coerced to `false`). One mounted-only loop: 60s visibility-aware sync-health refresh while List is active. **No new backend endpoints, zero backend changes.**
- **Seam (ent#261)**: the store-level `visibleAgents` computed in `stores/network.js` (server-side tag filter ∘ client-side owner filter) feeds Grid + List props — the type-to-filter predicate landed in that one place (§9.10), which also switched the Timeline onto the same computed via ReplayTimeline's `:agents` prop. The node-rebuild call sites deliberately do NOT read the seam (rewiring `convertAgentsToNodes` through it was rejected as timeline-mutation risk) — they read the pre-query `ownerFilteredAgents` (§9.10).
- **Flow**: `docs/memory/feature-flows/dashboard-list-view.md`

### 9.10 Dashboard Type-to-Filter (trinity-enterprise#261)
- **Status**: ✅ Implemented (2026-07-31)
- **Description**: Hotkey-activated, non-intrusive live type-to-filter across all three dashboard modes (Timeline / Grid / List). Press `/` anywhere on the Dashboard (outside editable fields and modals) → a small floating filter pill appears over the pane area; typing filters agents live in whichever view is active. An accelerator, not a takeover: **nothing is persisted** — a reload always starts unfiltered, and navigating away clears the query (Dashboard unmount). Purely client-side over the already-loaded fleet list; **zero backend changes**.
- **Key Features**:
  - **Activation**: `/` on a Dashboard-scoped document keydown listener. Guards, in order: `defaultPrevented`/`repeat` → non-`/` key (layout-produced — de-DE Shift+7 works; `shiftKey` NOT excluded) → Ctrl/Meta/Alt chords → IME composition (`isComposing`) → editable targets (INPUT / TEXTAREA / SELECT / `isContentEditable`) → open modals (onboarding wizard, System View editor, Create Agent modal). Then `preventDefault` (blocks Firefox quick-find) + open pill + focus input.
  - **Predicate**: case-insensitive substring over slug AND display label via `agentDisplayName()` (#1642 house rule, §1.3.1 FR-3) — typing `TOM` finds an agent labelled TOM whose slug is `tom-marketing-ops`. Layered inside the store `visibleAgents` seam (`stores/network.js`): `ownerFilteredAgents` (tag ∘ owner) → `visibleAgents` (∘ query), so Grid + List filter with zero pane rewiring; the Timeline joins by switching its `:agents` prop from raw `agents` to `visibleAgents` (rows, communication arrows, and schedule markers all derive from the prop). Description/tags matching is a recorded follow-up.
  - **Node invariant**: every `convertAgentsToNodes` call site reads the **pre-query** `ownerFilteredAgents` — a transient query must never degrade timeline-row node enrichment (system-first sort, purple treatment) after Esc. (The 30s refresh poll previously rebuilt nodes from the RAW list, ignoring even the owner filter — fixed to the same pre-query collection.)
  - **Honest state (pill)**: floating pill anchored to the non-scrolling chassis column, rendered whenever open OR a query is applied (an applied-but-hidden filter is the dishonest state this prevents). Live **"X of Y match"** count (X = post-query, Y = the set the view would show without the query but with tag/owner filters; secondary per-view filters — timeline "Active only", List panel name/status — may prune rendered rows below X by design: the pill claims *matching*, not *rendering*). Esc hint + × button; wrapper `role="search"`, input stays `type="text"`.
  - **Esc layering**: input-scoped Esc (clear + close + blur, `.stop` shields modal handlers) plus a document-level backstop so "Esc to clear" stays true after focus wanders — gated on filter-open/active, skipped while a modal is open, while the tag dropdown is open (that Esc closes the dropdown and KEEPS the filter), and while focus sits in another editable field (input/textarea/select/contenteditable — Esc there belongs to that control, e.g. the List panel's search box; the pill input is unaffected since its own handler stops propagation). Enter blurs the input and keeps the filter (GitHub convention).
  - **Query-empty state**: ONE chassis-level overlay ("No agents match "q"" + Esc-to-clear + Clear button) covering whichever pane is active; panes stay **MOUNTED** underneath (a transient zero-match while typing must never unmount ReplayTimeline/FleetGrid — zoom/scroll/layout state would reset). The true-empty onboarding CTA branches are guarded `&& !filterActive`, so "Get started" is unreachable while a query is active.
  - **Discoverability**: a clickable `<kbd>/</kbd>` hint button in the header controls (tooltip "Filter agents (press /)") that **toggles** — opens when closed, clears+closes when active — giving mouse/touch parity so the feature is not hotkey-only.
  - **List-mode composition**: the chassis query AND-stacks with the List panel's own persisted name/status filters; the panel's "N/M" count badge is suppressed while the chassis query is active so two disagreeing denominators never render simultaneously. The chassis query-empty overlay precedes the panel's filtered-empty state.
- **Behavior change (deliberate, release-noted)**: switching the Timeline's `:agents` prop onto `visibleAgents` makes the timeline honor the **owner filter** for the first time (previously grid-only — a latent inconsistency). `filterOwner` is persisted, so a user carrying a stale owner filter will see timeline rows narrow on upgrade day.
- **Flow**: folded into `dashboard-grid-view.md`, `dashboard-timeline-view.md`, `dashboard-list-view.md` (no standalone flow doc).

### 9.11 Grid Org Overlay — Department Zones + Reporting Lines (trinity-enterprise#305)
- **Status**: ✅ Implemented (2026-07-31) · OSS-core (explicit decision — no entitlement gate)
- **Description**: Organizational layer over the Grid view. **Departments** are `dept-<name>` tags rendered as derived hull frames ("zones") around member tiles wherever they sit — membership is the tag, geometry is computed, nothing is persisted per zone. **Reporting lines** are `reports-to-<agent>` tags stored on the REPORT agent (direction = which row carries the tag), rendered as manager→report arrows. Storage is namespaced tags — no schema change; a dedicated field can supersede losslessly.
- **Key Features**: zones with live rollups (count/running, viewer-scoped) + per-tile dept ribbons (stable hash → 8 themed palette slots); bottom connect port (drag from manager onto report; live "X will report to Y" pill; undo toast); click-line removal with undo; hover chain/line highlighting; drop-into-zone reassigns dept (re-validated at drop, undo toast); zone-header block move with per-tile target sockets and invalid-spring-back; "Group by dept" dense arrange + zone-aware Tidy (`tidyByDept`); zone-aware newcomer placement; "New department" affordance (named validation + click-to-assign mode); Zones/Lines toggles persisted per user.
- **Bootstrap fallback**: while NO agent carries a `dept-*` tag, an agent's first plain tag counts as its department (day-one zones on tag-organized fleets) — those zones are READ-ONLY (never drop-assigned) and the fallback switches off fleet-wide at the first explicit `dept-*`.
- **Guardrails / integrity**: org namespaces are **human-only at both writers** — the tags router rejects agent-principal writes to `dept-*`/`reports-to-*` (mirrors the #1578 reserved event namespace) and the system-manifest validator rejects org-prefixed manifest tags; tag edits broadcast a **thin** `agent_tags_changed` trigger (`{type, agent_name}` only — `/ws` is SCOPE_ALL/unfiltered, so tag values on the wire would leak the org chart cross-tenant; listeners refetch per-agent) so all browsers converge; `GET /api/tags` hides org prefixes from non-admins; dept assignment is an atomic set-list PUT; agent **rename** rewrites `reports-to-<old>` values fleet-wide inside the rename transaction (PK-collision-safe); hard **purge** deletes dangling `reports-to-<name>` values (soft-delete keeps them; render skips missing agents). Generic tag surfaces (Dashboard quick-tags, List-view chips (`AgentListPanel`), SystemViewEditor, network-store grouping) hide org namespaces via `isOrgTag`; the AgentDetail tag editor shows all.
- **Spacing contract**: lattice gaps (GAP_X 40 / GAP_Y 50) absorb the zone frame chrome (22/10/34/10), so adjacent-row/column departments never collide and the arrange needs no spacer cells — pinned by a unit test.
- **Out of scope (follow-ups)**: line routing around tiles; live re-anchor mid-drag; drag-out-of-zone to clear dept; touch port affordance; suggestions from `agent_permissions`/spawn provenance; behavioral consumers of reporting lines (escalation routing).
- **Flow**: `docs/memory/feature-flows/dashboard-grid-view.md` (§ Org overlay)

### 9.12 Grid Info Tile — Recent Failures (trinity-enterprise#100)
- **Status**: ✅ Implemented (2026-08-12) · OSS-core (explicit decision — no entitlement gate)
- **Description**: The first **data** info tile on the Grid's widget chassis (trinity-enterprise#325): the newest failed executions across every accessible agent, with the 24h failure total in the header meta. Failures previously required opening Operations → Executions and applying a filter. Default-on; toggled in the Tiles ▾ menu like any other tile.
- **Data sources — no backend change**: `GET /api/executions?status=failed&hours=24&limit=4` (rows) and `GET /api/executions/stats?hours=24` (`failed_count`). Both existing, paginated, filtered and access-scoped. Both ride `stores/fleetGrid.js::refreshBatchData()` — the ONE visibility-aware 60s batch poll the Grid already runs — gated on the tile being enabled. **No new endpoint, no schema change, no new timer.** The tile never fetches: viewport culling *unmounts* tiles, so a fetch in `onMounted` would re-issue on every pan.
- **Honest empty state (the load-bearing requirement)**: "No failures in 24h ✓" is a **positive claim** about the fleet, on the fleet's own monitoring surface, so it requires positive evidence and is unreachable from any of the three faults that would otherwise manufacture it — (1) a failed rows GET (principle 15 / #1926), (2) a failed `/stats` GET (the 24h total is a second request; unknown ≠ zero, and it is never inferred from `rows.length`), (3) an **unenumerable fleet** — `accessible_agent_names` resolves through `docker_service.list_all_agents_fast()`, which returns `[]` on *any* Docker fault, so a non-admin gets HTTP 200 + zeros and a green all-clear invented by an infrastructure failure. A non-empty roster is the client-side enumerability signal. A fourth route is closed in the store: `GET /api/executions` answers a bare array, so a 200 that is not one is treated as a failed cycle rather than coerced to `[]` (which is byte-identical to a healthy empty fleet). The rule lives in `utils/executionFailure.js::failuresTileState` as a pure function so it is unit-assertable under the node-environment suite. An empty roster is deliberately worded as *"Fleet list is empty"* naming both possible causes, since the tile cannot distinguish a fresh install from a failed enumeration — it refuses the ✓ either way without asserting a fault.
- **Counted-but-not-listed**: `/stats` counts `status IN ('failed','error')` while the list endpoint filters ONE status, so a fleet whose only recent failures are legacy `'error'` rows renders an explanatory line rather than "3 in 24h" beside a green ✓.
- **AC deviation (named, not silent)**: the error-code taxonomy bullet is **not** met. `TaskExecutionErrorCode` is an in-memory enum on `TerminalEnvelope`; `schedule_executions` has no `error_code` column, so the code is discarded at the terminal write and `error_summary` is a 200-char truncation. The tile READS a `[code]` marker when the platform emitted one and renders `null` otherwise — it never guesses. A JS re-classifier was rejected: `services/failure_classifier.py` is a byte-identity-mirrored pair with `src/scheduler/failure_classifier.py` and a third, unenforced copy guessing at a truncated string would be worse than no label. Today the only writer of that marker is the dark pull path, so the chip is absent on every current install and the row spends its width on the real message. Persisting `error_code` is a follow-up; the chip then appears with zero UI churn.
- **Chassis contract touched**: info tiles now receive the **unfiltered** roster (`orgAgents || agents`, the #305 seam) so the ent#261 type-to-filter cannot degrade a fleet tile's labels to raw slugs per keystroke; and the Grid's shared 1s tick is passed only to catalog entries declaring `wantsTick`, so a tile rendering no clock is not re-rendered every second. `InfoTile` sets `inheritAttrs: false`.
- **Out of scope**: WS-driven early refresh (v1 rides the poll); the error-code column; the sibling **Next schedules** tile (trinity-enterprise#99), held.
- **Flow**: `docs/memory/feature-flows/dashboard-grid-view.md` (§ Info tiles)

### 9.13 Grid Info Tile — Executions (trinity-enterprise#96)
- **Status**: ✅ Implemented (2026-08-14) · OSS-core (the epic's gating decision — the Grid ships OSS and these tiles summarize data the OSS operator already has)
- **Description**: Fleet executions over the last 24h as 24 hourly columns stacked by trigger bucket, with a failure rail, headline totals, and live running/queued chips. Per-agent tiles already carry 14d activity; there was no fleet-level execution chart anywhere on the dashboard. Default-on; toggled in the Tiles ▾ menu like any other tile.
- **One request, two dimensions (`split=trigger`, extends ent#326)**: the stack needs hour × trigger, and `GET /api/executions/timeline` grouped one dimension at a time — so the endpoint gained an optional `split=trigger` rather than the tile issuing one call per bucket name. Each bucket gains `by_trigger: {label: {total, failed}}` and the response carries `trigger_order`. **Per-bucket totals are re-summed from the split rows server-side**, so a column and its segments cannot disagree; gap-filled intervals carry `{}` rather than a missing key, so a chart never distinguishes "no runs" from "no field". `split` is a named 422 over a categorical `group_by` (splitting `trigger` by trigger is a tautology) — the ent#326 rule that an axis the caller did not ask for is a quietly wrong chart, applied to one they asked for and did not get.
- **One vocabulary, one order (AC1)**: bucket names come from `_TRIGGER_BUCKETS` and their stack order is **served by the backend** (`trigger_order` ← `_BUCKET_ORDER`), so tile, legend and the #1107 Overview chart cannot name or order the same buckets differently. A bucket present in the data but absent from the order is **appended**, never dropped — otherwise it would count toward a column total while missing from its stack.
- **Failures beside the stack, not inside it (AC2)**: failures render as a rail beneath each column on their own scale, not as a stack segment. A "Failed" segment would have to be subtracted from its trigger's segment to keep the column honest, which silently redefines every other segment as "succeeded"; the rail keeps the column equal to runs while making failures visible. Per-label `failed` still rides in the payload, so the hover breakdown names which trigger failed.
- **Honest states**: `successRate` is terminal-based and reports `—` (not 0%) when nothing has terminated; "No executions in the last 24h" requires a successful read (the ent#100 manufactured-green rule); a failed background refresh keeps the last good chart with a `24h · stale` stamp; the running/queued chips degrade to absent rather than to zero, since a failed `/stats` is not evidence that nothing is running.
- **No new timer, no new poll**: both GETs ride `stores/fleetGrid.js::refreshBatchData()`, gated on the tile being enabled. The tile never fetches on mount — viewport culling unmounts tiles, so a fetch there re-issues on every pan.
- **Tokens**: seven new `--gv-bk-*` bucket colours defined in BOTH theme blocks (`gridTokens.spec.js`). `AgentTile` collapses ten buckets to three because a 60px sparkline cannot carry ten; the fleet tile stacks all ten, so each needs its own hue.
- **Out of scope**: WS-driven early refresh (rides the poll); a window selector (24h fixed, as filed).
- **Flow**: `docs/memory/feature-flows/dashboard-grid-view.md` (§ Info tiles)

---

---

## Brain Orb — The Self-Rendering Mind (trinity-enterprise#58)

**Description**: A capability-gated per-agent page that renders a Cornelius-class agent's live
3D knowledge-graph orb from data the agent produces in its own container, with live scope control
and a client-held voice tile. **Shipped: static render (Phase 1, FR-1…5) + scope mount/unmount →
re-export → live rebuild (Phase 2, FR-6) + client-held Gemini Live voice tile + read-only KB search
(Phase 3, FR-7) + owner-gated KB-write actions capture/link (Phase 4a, FR-8) + voice-transcript
capture & configurable post-session processing (Phase 4b, FR-9, #66).** Only `run_skill` (arbitrary
headless exec from the orb) remains out of scope. Default OFF — no impact on other agents or the UI.
See [feature-flows/brain-orb.md](../feature-flows/brain-orb.md).

- **FR-1 — First-party CSP-clean assets**: the orb ships as verbatim first-party frontend assets
  (`public/brain-orb/`), with `three`/`marked`/`DOMPurify`/font vendored locally and the inline
  module externalized, so it runs under prod `script-src 'self'`/`font-src 'self'` with no nginx
  change. Only mechanical orb edits (externalize, vendor, repoint data fetch, neutralize the
  deferred voice proxy, hide deferred panels). Note bodies are DOMPurify-sanitized (H-005).
- **FR-2 — Capability gating**: a `/agents/:name/brain` route (lazy + `beforeEnter` platform-flag
  guard) and a Brain tab shown only when `brain_orb_available` (runtime-resolved platform flag —
  admin setting → `BRAIN_ORB_ENABLED` env fallback, default OFF; FR-11) **AND** the agent's
  `template.yaml capabilities` list contains the generalizable
  `brain-orb` token (surfaced by `/api/agents/{name}/info`) — never a hardcoded agent name.
- **FR-3 — Same-origin iframe host**: `views/AgentBrainOrb.vue` embeds the first-party page in a
  same-origin iframe (not agent-origin → avoids the #979 CSP trap, no Vue rewrite of the renderer).
- **FR-4 — Auth via postMessage, standard Bearer**: the host hands the user's JWT to the iframe via
  origin-pinned `postMessage` (never in a URL); the data route uses standard `AuthorizedAgentByName`
  Bearer auth — no new ticket primitive. A `brain-orb:error` message shows an empty state.
- **FR-5 — Read-only proxy (agent owns generation)**: `GET /api/agents/{name}/brain-orb/data`
  (`AuthorizedAgentByName`) proxies via `agent_httpx_client` (#1159) to the agent-server
  `GET /api/brain-orb/data`, which streams `~/resources/agent-visualization/data.json`. Byte
  pass-through (no re-serialize of the multi-MB JSON); 404 when the flag is off / no export,
  503/504 unreachable, 502 agent error. Trinity never runs `export_data.py` (Invariant #8).
- **FR-6 — Live scope control (Phase 2)**: the orb's scope panel mounts/unmounts vault scopes,
  driving an agent re-export → live in-place rebuild (no reload). `GET /api/agents/{name}/brain-orb/scopes`
  (`AuthorizedAgentByName`, read) lists selectable + active scopes; **`POST .../brain-orb/scope`
  (`OwnedAgentByName` — owner/admin)** mutates the set. The agent provides two executable convention
  hooks (`~/.trinity/brain-orb/{scopes,scope}`, mirrors `~/.trinity/pre-check`); the agent-server runs
  them via hardened async subprocess (timeout-kill, output cap, JSON-parse + non-zero-exit guards) and
  404s when absent. The agent owns scope state + the re-export (Invariant #8); Trinity only brokers.
  Replaces the local voice proxy's per-start `X-Orb-Token` with the platform JWT + owner gate.
- **FR-7 — Client-held Gemini Live voice tile + read-only KB search (Phase 3, #60)**: the orb's voice
  tile holds its own Gemini Live session **client-side** — the browser connects DIRECTLY to Gemini
  Live (mic capture + playback in the same-origin iframe), Trinity never proxies the audio.
  Deliberately distinct from Trinity's backend-proxied workspace voice (VOICE-001), to keep the
  voice→tool→orb loop in-browser. **Ephemeral-credential broker**: `POST /api/agents/{name}/brain-orb/
  voice-token` (`AuthorizedAgentByName`; per-(user,agent) rate-limited) mints a short-lived,
  **config-locked** Gemini Live ephemeral token via `auth_tokens.create` (`live_connect_constraints`
  pins model + the whole config incl. the tool surface; `uses=1`; ~60s new-session window; expiry =
  `VOICE_MAX_DURATION`). Built with a dedicated **v1alpha** genai client (NOT the cached voice
  singleton). The token is minted by the orb page (which holds the JWT) and relayed to the nested
  voice iframe over `postMessage` — the JWT never enters the voice iframe or a URL; the voice iframe
  only ever sees the single-use Google token. Response field is `ephemeral_token` (never `token`, which
  would flip the deferred write surface on). **Visual-only tools** (`highlight_related_notes`,
  `navigate_to_note`, `list_converged_topics`, …) run in-browser via the existing `orb-tool`
  postMessage bridge. **Scope-by-voice reuses Phase 2** (`mount_scope`/`unmount_scope` → the FR-6
  `/scope` broker — no new mutation surface). **Read-only KB search**: `POST /api/agents/{name}/
  brain-orb/tool` (`AuthorizedAgentByName`) → agent-server runs the agent's `~/.trinity/brain-orb/
  search` convention hook (scope-aware, read-only; 404 when absent). **Writes stay off by
  construction**: the locked tool manifest declares only read/visual/scope tools; the browser cannot
  widen it, and orb.js's `ACTIONS` write surface stays disabled (no `/session` route). **Gating**: a
  new `brain_orb_voice_available` flag (`BRAIN_ORB_VOICE_ENABLED && GEMINI_API_KEY`, default OFF) —
  distinct from the static `brain_orb_available` — AND the agent's `brain-orb` capability, enforced by
  BOTH the route guard and the tab (the orb is never launchable on a non-Cornelius agent, even via a
  raw URL — the `beforeEnter` guard reads `/info` capabilities and redirects otherwise, #60). CSP-clean:
  `connect-src` already allows `wss:`; the Gemini JS client is hand-rolled (no SDK), the voice logic
  and mic worklet are externalized same-origin files (script-src 'self'); the standalone page's
  hardcoded key is stripped; its p5.js audio-reactive voice orb is **vendored locally** (not CDN) so
  the speech animation is retained CSP-clean. The outer host iframe carries `allow="microphone"`.

- **FR-8 — Owner-gated KB-write actions: capture + link (Phase 4a, #61)**: the orb's action panel
  (`#actions`, `A` key) + inspector connect are un-hidden and rewired from the dead standalone voice
  proxy to the platform broker. Two owner/admin-only write verbs — **capture** (a note into the
  agent's inbox) and **link** (`[[wikilink]]` two notes). `POST /api/agents/{name}/brain-orb/action`
  (`OwnedAgentByName`) enum-validates the verb (run_skill/capture_transcript → 400, Phase 4b), body-caps
  (413), rate-limits per (user, agent, action), audit-logs (`brain_orb_capture`/`brain_orb_link`), and
  dedups via `Idempotency-Key` (Invariant #18, key folded per verb — NOT the #1084 effect_guard, which is
  execution_id-scoped and has no execution here); `GET .../brain-orb/actions` (`OwnedAgentByName`) reports
  `{enabled, skills}` so the orb un-hides the panel only for owners (403/404 otherwise). Both proxy to the
  agent-server, which runs the agent's `~/.trinity/brain-orb/action` convention hook via the hardened
  `_run_hook` (agent owns the write, Invariant #8; 404 when absent). **Voice write tools are owner-gated**:
  the mint route computes `can_write` (owner + flag) and only then folds `capture_note`/`link_notes` into
  the **locked** manifest — shared-user sessions keep the read-only Phase-3 manifest, and the `/action`
  route is the hard gate regardless. Own kill-switch `BRAIN_ORB_WRITE_ENABLED` (env, default OFF; distinct
  from `BRAIN_ORB_ENABLED` so writes disable without downing read/voice) → `brain_orb_write_available` in
  feature-flags. No DB change, no migration.
- **FR-9 — Voice-transcript capture + configurable post-session processing (Phase 4b, #66)**: mirrors the
  original `cornelius-internal/resources/agent-visualization/voice/` (client captures, agent renders/saves).
  The mint adds `input_audio_transcription`/`output_audio_transcription` to the **locked** `LiveConnectConfig`,
  so the constrained ephemeral token returns per-turn transcription. `voice.js` buffers input/output
  transcription into conversation events (`session_start`/`user_turn`/`model_turn`/`tool_call`/`session_end`)
  and, on `endConversation` (the correct flush seam — `onclose` early-returns on `wsClosedByUs`), relays them
  to `orb.js`, which POSTs `capture_transcript {session_id, events, process}` (session-id = `Idempotency-Key`
  → a double session-end saves one transcript). The `action` hook renders a markdown transcript into
  `resources/inbox/Voice Conversations/` (ported `transcript_io`). **Post-session processing** (`process_transcript`,
  or `capture_transcript {process:true}`): if the agent ships `~/.trinity/brain-orb/voice-postprocess.md` (the
  "formulated prompt config" — configuring it is the opt-in), the hook runs that prompt over the transcript via
  a **detached** `claude -p` (transcript piped on **stdin** — no shell string → no command injection), writing a
  processed note. Owner-only (`OwnedAgentByName` + `ACTIONS.enabled`), body cap raised to 1 MiB (backend +
  agent-server) for whole conversations. No DB change. **Confirmed on localhost**: constrained-token mint accepts
  the transcription config, and synthetic voice events render + save; full live-audio transcription streaming is a
  manual voice-session check.
- **FR-10 — Write → graph refresh loop + visible integration (#67, #68)**: closes the gap where captured notes /
  links landed in the inbox but never appeared on the orb. `POST /api/agents/{name}/brain-orb/refresh`
  (`OwnedAgentByName`, 200s timeout mirroring `/scope`, audited `brain_orb_refresh`) → agent-server
  `POST /api/brain-orb/refresh` → the `action` hook's `refresh` verb reindexes + re-exports `data.json` (folds inbox
  notes + `_links.md` edges into the graph; the agent owns generation, Invariant #8). `orb.js` `refreshGraph()`
  refetches `/data` and rebuilds **in place** (same machinery as `setScope`), auto-triggered after capture/link
  (voice writes debounced ~4s so a burst coalesces into one rebuild), plus a visible **"↻ integrate & refresh"**
  control, an "integrating…" state, and a "graph updated · +N notes, +M links" confirmation toast (#68). No DB
  change. **Confirmed on localhost**: capture → refresh folds the note in as a real graph node (`1072 → 1079`),
  and the UI control rebuilds with the confirmation toast.
- **FR-11 — Admin-configurable platform flags (trinity-enterprise#85)**: the three platform flags
  (`brain_orb_enabled`, `brain_orb_voice_enabled`, `brain_orb_write_enabled`) are **runtime-resolved**,
  not import-time env constants: `system_settings` row ("true"/"false", wins in both directions) →
  `BRAIN_ORB_*` env var honored as **opt-in** fallback → default OFF (the `workspace_enabled` idiom via
  one shared `_resolve_bool_flag` helper). Resolvers are fail-open (a settings-read failure falls back
  to the env/default leg — a raise would 500 `feature-flags` and zero every flag in the frontend store)
  and deliberately uncached (`--workers 2` cross-worker consistency, #506 rationale). All route gates in
  `routers/agent_brain_orb.py` and the three `feature-flags` values read the resolvers, so an admin flip
  applies without restart; the voice-token mint additionally composes with the base flag
  (`base ∧ voice`, closing the base-OFF mint gap) and `brain_orb_voice_available = base ∧ voice ∧
  GEMINI_API_KEY`. **Admin surface**: `GET/PUT /api/settings/brain-orb` (admin-only, registered before
  the `/{key}` catch-all) — GET returns per-flag `{value, source: override|env|default}` +
  `gemini_key_configured`; PUT takes partial booleans and/or `clear: [flag,…]` to **revert a flag to its
  env/default** (the env var is otherwise dead once a DB override exists), audit-logged with per-flag
  old→new values. Settings → General hosts the panel (per-flag source display, write-surface warning,
  post-save `loadFeatureFlags(force)`; other open sessions pick the change up on next page load).
  GEMINI_API_KEY stays env-only (secret). No migration (`system_settings` KV).

**Still out of scope**: `run_skill` (arbitrary allow-listed headless exec from the orb) — the full exec surface
with a `template.yaml` allow-list ceiling + #1083 detached-execution integration remains unbuilt; open a fresh
issue if it's ever wanted. Also deferred: `data.json` caching/streaming.

---

## Default Cornelius Agent — Auto-Seed on Fresh Install (trinity-enterprise#107)

- **Status**: ✅ Implemented (2026-07-07)
- **Description**: A fresh Trinity install auto-seeds a default "Cornelius" second-brain agent with the
  Brain Orb enabled, so a first-run operator lands on a working knowledge-graph agent out-of-the-box
  (no manual create/clone). Provisioned by
  `services/cornelius_agent_service.py::CorneliusAgentService.ensure_seeded()`.
- **Key Features**:
  - **Public source template** (#1656): provisioned via the ordinary `create_agent_internal` from
    `github:Abilityai/cornelius` — an anonymous, source-mode clone with **no PAT**, on the
    trinity-enterprise#123 tokenless public-repo path (`AgentConfig.source_mode` defaults `True`, which
    that path requires). Carries `capabilities: [brain-orb]`, `CLAUDE.md`, `.trinity/brain-orb/` hooks,
    a pre-generated `resources/agent-visualization/data.json` seed graph so the orb renders immediately,
    `resources/local-brain-search/` (so `semantic_search` is real, not a keyword fallback), and the full
    `Brain/` vault the seed graph was exported from. Was a vendored
    `config/agent-templates/cornelius/` snapshot until #1656; that snapshot drifted from its own prose
    and caused #1646 and #1656, so the bundle was deleted rather than re-vendored. **No offline
    fallback** — a fallback would only fire on a transient clone failure and would burn the durable
    `cornelius_seeded` flag on the degraded copy; leaving the flag unset to retry next boot is safer.
  - **First-run-only**: a durable `cornelius_seeded` system-setting flag gates the seed — an operator who
    deletes Cornelius is **not** re-provisioned.
  - **Fresh-install-scoped**: skipped when any non-system agent already exists (`db.count_non_system_agents()`),
    so upgrades of established fleets aren't surprised by a new agent.
  - **Existence-guarded flag enable**: turns on the `brain_orb_enabled` platform flag only when unset —
    never clobbers an admin who set it OFF.
  - **Triggers**: the setup-completion handler (`routers/setup.py`, fresh installs, FastAPI BackgroundTask)
    + a `main.py` lifespan safety-net gated on `setup_completed && !cornelius_seeded` (upgrades). A Redis
    SETNX lock (`cornelius:provision`, fail-open, mirrors the #1464 leader-lock) guards the `--workers 2` race.
- **Known deviation (local bundle)**: the default Cornelius is a LOCAL bundle, not github-native, so it has
  **no git origin** — it won't auto-`git pull` upstream template updates. Durable ownership is deferred to
  fork-to-own (trinity-enterprise#109). No DB migration (`system_settings` is free-form KV). The Brain Orb was
  already fully OSS (flag-gated, not entitlement-gated), so no de-gating was needed.
- **Flow**: `docs/memory/feature-flows/cornelius-default-agent.md`
