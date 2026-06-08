# Research: Splitting Trinity into Open-Source and Enterprise Editions

> **Status**: Research / opinion. Not a committed plan. Branch:
> `research/oss-enterprise-edition-split`.
> **Author**: investigation requested 2026-05-18.
> **Scope**: how to split the codebase, where to gate features, how to
> control *who* and *for how long* can use enterprise features, and an
> honest recommendation with risks.

---

## TL;DR

**The recommendation in one sentence:** keep one public codebase, move
enterprise code into a private git submodule (the pattern Trinity
already runs for `.claude`), gate it at runtime through a single
`EntitlementService` driven by an offline signed license — and fix the
repo's undefined license *before* writing any of it.

### The six decisions

| # | Decision | Why |
|---|---|---|
| 1 | **Open-core, not a fork.** Enterprise code = a private submodule at `src/backend/enterprise/` + `src/frontend/src/enterprise/`. | Reuses the trusted `.claude` submodule pattern. Two diverging repos is how open-core dies. |
| 2 | **One `EntitlementService`, not scattered `if`s.** It plugs into seams that already exist: the FastAPI dependency layer (`require_role`), the settings resolver (`settings_service.py`), and `/api/settings/feature-flags`. | The hooks are already there. A central service keeps 53 routers clean. |
| 3 | **License = offline Ed25519-signed token.** No license server. Claims: customer, edition, feature set, seat/agent caps, `expires_at`, grace window. | "Sovereign infra on your own hardware" — phone-home breaks the pitch *and* air-gapped buyers. |
| 4 | **Tier it Community / Team / Enterprise**, not free/paid binary. Keep ≥1 channel integration free. | Charge for governance / scale / compliance — what an *org* pays for. Don't paywall the adoption funnel. |
| 5 | **Accept soft enforcement.** Don't build DRM. | The moat is signed updates + support + the legal license, not obfuscation of readable code. |
| 6 | **Fix the license-of-record first.** Repo currently reports `license: NOASSERTION`. | Open-core is a *legal* structure before a code one. Blocks everything (see §6). |

### What the stress test sharpened (see §8)

Testing the approach against 8 other open issues found it **holds but is
not one pattern — it is four**, and exposed two real gaps the rest of
this doc now accounts for:

- **The MCP server can't see the EntitlementService.** It's a separate
  TypeScript process. Any feature gated at the MCP-tool layer (e.g.
  #846, A2A) needs a small read-only entitlement endpoint the MCP
  server polls. *This was the one outright break.*
- **Some things must never be gated.** Auth, security, schema/data
  capture, and CI/build are explicit non-gates — without naming them,
  the model gets misapplied (the inventory pass already over-classified
  Slack).

### Do this first

1. **Owners decide** (not engineering): OSS license (Apache vs
   source-available), per-instance vs per-seat, tier of each ambiguous
   feature.
2. **Build the seam in the public repo, move nothing yet**:
   `EntitlementService` (stub = all-entitled) + the MCP entitlement
   endpoint + conditional `register_enterprise()` + a CI job that builds
   with the submodule absent.
3. **Extract one true leaf first** (audit log), then use **#834** to
   prove the harder "core-primitive + enterprise-knob" pattern.

---

## 1. Current state — what the investigation found

Three independent code investigations (gating infra, feature inventory,
build/packaging) converge on the same picture:

**There is no edition/license/tier/premium concept anywhere.** Grep
across `src/`, `config/`, `docs/` found nothing. The split is greenfield.

**But the gating primitives all exist already:**

| Primitive | Location | What it gives us |
|---|---|---|
| Role hierarchy | `src/backend/dependencies.py` — `ROLE_HIERARCHY = ["user","operator","creator","admin"]`, `require_role(min)`, `require_admin` | Per-endpoint dependency gate. The natural place to add `requires_entitlement(...)`. |
| Centralized settings | `src/backend/services/settings_service.py` — single resolver, DB→env→default, TTL cache precedent (`_PLATFORM_MODEL_CACHE_TTL`) | One place to resolve "is feature X licensed?" with caching already proven. |
| Feature-flags API | `routers/settings.py` `GET /api/settings/feature-flags` (`session_tab_enabled`, `workspace_available`, `voice_available`) consumed by the frontend on load | Frontend already conditionally renders off a backend flag fetch. Enterprise UI hiding is a 1-flag extension. |
| Settings store | `system_settings` (key/value), admin-writable via `PUT /api/settings/{key}`, migrations idempotent | A natural home for the license blob and cached entitlement state. |
| Quota enforcement | `settings_service.py` `get_agent_quota_for_role()` enforced in `agent_service/crud.py` (429 `QUOTA_EXCEEDED`) | Proves the codebase already does numeric caps with a clean error contract — reusable shape for seat/agent license caps. |
| Subscription model | `subscription_credentials` table, `agent_ownership.subscription_id` FK | Prior art for "an entitlement attached to an owner," though it's about Claude tokens, not licensing. |
| Private submodule | `.gitmodules` → `.claude` = private `Abilityai/trinity-dev`, auto-synced | **The distribution precedent for the whole strategy.** |

**Weaknesses that matter for the split:**

- **Static router registration.** `main.py` imports all ~53 routers
  unconditionally. Enterprise routers must become *conditionally
  registered*. This is the single biggest code change.
- **Distributed gating.** No central checkpoint. If we don't introduce
  one `EntitlementService`, the split will smear license `if`s across
  53 routers and rot.
- **Backend Dockerfile copies explicitly** (`COPY routers/ services/
  ...`). An enterprise build needs the submodule present at build time;
  the OSS build must work *without* it.
- **No SSO/SAML/SCIM exists.** The single most-requested enterprise
  auth feature is not built. The split plan should reserve the seam,
  not pretend it exists.

---

## 2. Distribution model — the decision that constrains everything

### Options considered

| Model | What it is | Verdict |
|---|---|---|
| **A. Two repos** | Public OSS repo, private full repo, cherry-pick between | ❌ Highest maintenance; merge drift; the classic open-core failure mode. |
| **B. Single repo, edition `if`s** | All code public, enterprise behind a license check | ❌ Enterprise source is fully public → zero IP protection, trivial to unlock. Only viable if you *don't care* about source secrecy (some companies don't — see "Honest take" §7). |
| **C. Open-core + private submodule** ✅ | Public core repo; `src/backend/enterprise/` and `src/frontend/src/enterprise/` are a private submodule (like `.claude`) | ✅ **Recommended.** One history per layer, clean public/private boundary, reuses a shipped & trusted pattern, OSS build works with the submodule absent. |
| **D. Plugin marketplace** | Enterprise features as separately-installed signed plugins | ⚠️ Cleanest long-term, heaviest to build now. Good *evolution* of C, not a starting point. |

### Recommended: Model C

```
trinity/                         (PUBLIC — open-source edition)
├── src/backend/
│   ├── main.py                  ← conditional enterprise registration
│   ├── routers/ services/ ...   ← core, OSS
│   └── enterprise/              ← GIT SUBMODULE → Abilityai/trinity-enterprise (PRIVATE)
│       ├── routers/             (audit, canary, sso, fleet-gov, ...)
│       ├── services/
│       └── entitlements/        ← license verify + EntitlementService
├── src/frontend/src/
│   └── enterprise/              ← submodule, enterprise Vue views/components
└── .gitmodules                  ← adds the enterprise submodule (private URL)
```

- **OSS clone**: submodule not initialized → `src/backend/enterprise/`
  absent → `main.py` registers core routers only → fully functional
  Community edition. This must be a first-class, tested path (CI job
  that builds with the submodule *removed*).
- **Enterprise build**: `git submodule update --init` pulls the private
  repo → enterprise routers register → license gates them at runtime.
- **`main.py` change** (the crux):

  ```python
  # core routers: unconditional (unchanged)
  app.include_router(agents_router)
  ...
  # enterprise: present only when the submodule is checked out
  try:
      from enterprise import register_enterprise   # submodule package
      register_enterprise(app, entitlements)        # gates per-feature
  except ModuleNotFoundError:
      logger.info("Community edition — enterprise modules absent")
  ```

  Two independent locks: **code presence** (submodule) *and* **runtime
  entitlement** (license). An OSS user never has the code; an
  enterprise user without a valid license has the code but it 403s.

> Precedent strength: the project already documents the `.claude`
> submodule setup (`git submodule update --init --recursive`,
> `submodule.recurse true`, `fetchRecurseSubmodules`). The enterprise
> submodule is the *same operational story* the team already runs.

---

## 3. The gating architecture

### 3.1 One service, three consumers

Add `enterprise/entitlements/service.py` → `EntitlementService`:

```python
class EntitlementService:
    def is_entitled(self, feature_key: str) -> bool: ...
    def assert_entitled(self, feature_key: str) -> None:   # raises 402/403
    def status(self) -> LicenseStatus:                     # for /feature-flags + admin UI
    def caps(self) -> dict                                 # seat/agent numeric limits
```

Resolution order (mirrors the proven `settings_service` pattern, same
TTL-cache shape):

1. Verified license token (signed, see §4) → entitled feature set + caps
2. No/expired license → Community feature set + grace handling (§5)
3. Cached for `ENTITLEMENT_CACHE_TTL` (e.g. 300s) to avoid per-request
   crypto; invalidated on license update (same hook as
   `platform_default_model` cache invalidation).

**Three consumers, no fourth:**

1. **Backend endpoints** — a dependency twin of `require_role`:

   ```python
   def requires_entitlement(feature_key: str):
       def dep(user: User = Depends(get_current_user)):
           entitlements.assert_entitled(feature_key)   # 402 if not licensed
           return user
       return dep
   # usage on an enterprise router:
   router = APIRouter(dependencies=[Depends(requires_entitlement("audit_log"))])
   ```

   Router-level `dependencies=[...]` means **one line per enterprise
   router**, not per endpoint — minimal, auditable, no smear.

2. **Frontend** — extend `GET /api/settings/feature-flags` with an
   `entitlements: {audit_log: true, sso: false, ...}` block. The
   frontend already fetches and conditionally renders off this exact
   endpoint; enterprise nav/routes hide when false. (UI hiding is UX,
   not security — the backend dependency is the real gate.)

3. **MCP server** — enterprise MCP tools (e.g. `monitoring.ts`,
   `subscriptions.ts`) call backend endpoints that are already gated;
   they fail closed with the backend's 402. Optionally filter the tool
   list by entitlement so they don't even advertise.

### 3.2 Failure semantics — pick deliberately

| Mode | Behavior on missing/expired license | Use when |
|---|---|---|
| **Hard block** | Enterprise endpoints 402; UI hidden | Default for never-licensed |
| **Grace** | Full function + visible "license expired, N days" banner; then degrade | Expiry of a *paying* customer — don't break prod on renewal lag |
| **Degrade-to-Community** | Enterprise silently off, core keeps working | Strongly preferred over "platform won't boot" |

**Never make the platform refuse to start on a license problem.** A
sovereign self-host that bricks itself on an expired token is a
support nightmare and a trust violation. Core must always run.

---

## 4. Licensing — *who* and *for how long*

### 4.1 Mechanism: offline-verifiable signed license token

- **Format**: compact JWS/PASETO-style token, **Ed25519**-signed.
  Anthropic-style: a base64 blob the customer pastes into
  Settings → License (admin-only `PUT /api/settings/license`, stored in
  `system_settings` — precedent exists — or a dedicated `license`
  table for auditability).
- **Public key** baked into the enterprise submodule. Verification is
  pure-offline; no network required. (Air-gapped customers are a
  first-class case for "on your own hardware.")
- **Claims**:

  ```jsonc
  {
    "lic_id": "uuid",
    "customer": "ACME GmbH",
    "edition": "enterprise",            // community | team | enterprise
    "features": ["audit_log","sso","canary","fleet_gov", ...],
    "caps": { "max_agents": 200, "max_seats": 50 },
    "issued_at": "2026-05-18T00:00:00Z",
    "not_before": "2026-06-01T00:00:00Z",
    "expires_at": "2027-06-01T00:00:00Z",
    "grace_days": 30,
    "support_tier": "gold"
  }
  ```

- **"For how long"** = `expires_at` + `grace_days`. After expiry: grace
  window with loud banners (reuse the operator-queue / notification
  surfaces that already exist), then degrade-to-Community. Clock-tamper
  resistance: persist a monotonic "max timestamp ever seen" in
  `system_settings`; if wall clock < that, treat as tamper → grace.
- **"Who"** = identity model decision (§4.2).

### 4.2 What does a "seat" mean here? (decide explicitly)

Trinity is self-hosted and multi-user (`users` table, 4-tier roles).
Options for the licensed unit:

1. **Per instance** — one license = one deployment, unlimited users.
   Simplest, matches "sovereign box," easiest to sell, hardest to
   price-discriminate. **Recommended starting point.**
2. **Per seat** — cap on `users` rows (or active users / 30d). Enforce
   at user-create, same shape as the existing `QUOTA_EXCEEDED` 429.
   More revenue granularity, more friction, more support load.
3. **Per agent** — cap on owned agents. The codebase *already* enforces
   per-role agent quotas; a license-driven `caps.max_agents` is a
   near-trivial extension of `get_agent_quota_for_role()`.

Recommendation: **per-instance editions + a soft agent cap** to start
(reuses existing quota machinery, lowest friction), add per-seat later
if sales needs it. Don't build seat metering before a customer asks.

### 4.3 Issuance & revocation

- **Issuance**: an internal license-signing CLI (private, holds the
  Ed25519 private key) — a tiny tool, not a service. Sales/ops runs it.
- **Revocation** (the hard part offline): three pragmatic levers,
  ranked by how much they fit the sovereignty pitch:
  1. **Short expiry + renewal** (e.g. 13-month tokens). Natural
     revocation = don't reissue. Zero infra. **Primary mechanism.**
  2. **Optional online heartbeat** — opt-in; if the customer allows
     egress, daily check against a revocation list. Off by default;
     never blocks core.
  3. **Update gating** — revoked customers stop getting signed
     releases/support. This is the *real* commercial leverage for
     self-hosted software anyway.

---

## 5. What is Community vs Team vs Enterprise

Open-core principle: **the free tier must be good enough to win the
solo dev and the POC, or the funnel dies.** Charge for what an
*organization* needs and an *individual* never will: governance,
compliance, scale, multi-channel-at-scale, monetization.

The feature inventory maps cleanly onto three tiers:

### Community (free, OSS, MIT/Apache — the adoption engine)
Everything needed to run a real single-owner fleet:
- Agent CRUD, chat, **Session/resume**, terminal, files, rename, SSH
- Scheduling + executions + pre-check
- Credentials (CRED-002 encrypted), templates, MCP keys
- Capacity/slots/backlog, cleanup, event bus, WebSocket realtime
- Basic auth (email OTP + admin), the 4-tier role model *as-is*
- Activity timeline, agent logs, single-agent observability
- **One channel integration** (pick Slack — it's the POC magnet)

### Team (paid, low tier — "more than one human")
Collaboration & light governance:
- Agent sharing & cross-channel access control (#311)
- **All** channel integrations (Telegram, WhatsApp, multi-Slack)
- Tags, saved system views, system manifests (multi-agent deploy)
- Skills library, image gen, avatars, voice/workspace
- Git-sync for GitHub-native agents (#383/#389)

### Enterprise (top tier — compliance, scale, money)
The things only an org will pay real money for:
- **Platform audit log + hash-chain + retention** (SEC-001 #20) —
  the #1 enterprise/compliance ask
- **SSO / SAML / SCIM** — *not built; reserve the seam* (auth.py).
  This is the single biggest revenue feature you don't have yet.
- Canary invariant harness (CANARY-001 #411)
- Fleet monitoring (MON-001), Operating Room (OPS-001), fleet
  sync-audit (#390), telemetry, OTel observability, log archival
- Soft-delete + retention governance (#834)
- Monetization stack: Nevermined x402 (NVM-001), subscription
  credential mgmt (SUB-002) — selling *this* is itself enterprise
- Advanced RBAC beyond the 4 built-in roles; org/workspace model
  (also not built — reserve)

> **Calibration risk to flag:** the agents classified Slack/Telegram/
> WhatsApp as "enterprise." I disagree for the *first* channel —
> integrations are how people fall in love with the product. Gate the
> *fleet/governance* features hard; keep at least one channel free.

---

## 6. Prerequisite: fix the license of record

`gh api` reports the repo as `license: NOASSERTION`. Open-core is a
**legal** structure before it is a code structure. Decide and commit,
in this order, *before* coding the gate:

1. **OSS core license.** Apache-2.0 (patent grant, enterprise-friendly,
   permissive — best for adoption) **or** a source-available license
   (BSL 1.1 / Elastic-style / Fair-source) if you want to prevent
   competitors from reselling Trinity-as-a-service. Permissive vs
   source-available is a *business* decision (adoption vs. moat) — it
   must be made by the owners, not defaulted.
2. **Commercial/enterprise license** for the private submodule
   (proprietary EULA, ties to the signed token).
3. **CLA/DCO** for outside contributors so you retain relicensing
   rights on the core.
4. A `LICENSE`, `LICENSE-ENTERPRISE`, and `licensing.md` explaining the
   split to users (and what telemetry/heartbeat, if any, exists — be
   transparent; the audience is sovereignty-minded).

---

## 7. Honest take / risks

- **Self-hosted DRM is theatre if over-built.** Source is readable in
  the core; the gate is one dependency. A customer *can* patch it.
  That's acceptable — your leverage is updates+support+legal, not
  bytecode. Invest in the *entitlement model and licensing ops*, cap
  the anti-tamper at "honest, clock-aware, hard to do by accident."
- **The sovereignty pitch and license enforcement are in tension.**
  Every enforcement choice (phone-home, hard expiry, boot refusal)
  spends trust with exactly the buyer Trinity targets. Default to the
  least-coercive option that still gets paid: offline tokens, generous
  grace, degrade-don't-brick, update-gating as the real stick.
- **Open-core line will be re-litigated forever.** Every new feature →
  "is this core or paid?" Write the *principle* down now ("org-only
  governance/compliance/scale is paid; anything a solo dev needs to
  fall in love is free") so it's not re-argued per PR.
- **Biggest revenue gap is unbuilt.** SSO/SAML/SCIM and a real
  org/workspace RBAC model are the classic enterprise wedge and they
  don't exist. The split is partly an argument to *build* these as the
  first paid features, not just to fence existing ones.
- **CI must prove the OSS build.** Add a pipeline that builds & tests
  with the enterprise submodule **absent**. Without it, the Community
  edition will silently break and you won't notice until a community
  user files an issue.
- **Don't split prematurely.** If there's no enterprise customer yet,
  the cheapest correct move is: (a) fix the license of record, (b) add
  the `EntitlementService` + conditional registration seam in the
  *public* repo (no features moved yet), (c) move code into the
  submodule only when a deal needs it. The seam is cheap and reversible;
  a repo split is neither.

---

## 8. Stress test — does the approach survive other open issues?

The approach was pressure-tested against a representative spread of open
feature issues, chosen to hit different *shapes* (not to confirm it).
Legend: ✅ holds cleanly · ⚠️ strains, needs a refinement · ❌ breaks an
assumption.

> Note: the team already filed **#847 — "Spike: enterprise edition
> architecture (SSO/SCIM/SIEM, private module)"**. It independently
> arrives at private-module + the SSO/SCIM/SIEM wedge, matching this
> doc — useful corroboration, and the natural home for this research.

| # | Feature | Shape | Gate point / where code lands | Verdict |
|---|---|---|---|---|
| **#847** | SSO/SAML, SCIM, SIEM export | The compliance wedge | SCIM = clean submodule router; SIEM = log-tap consumer (clean). **SSO is *not* clean** — it rewires token issuance in `auth.py`/`dependencies.py` | ⚠️ |
| **#846** | per-agent MCP exposure flag | New **MCP-server** tool surface | Gate must live in the TypeScript MCP server — a separate process that cannot import the Python `EntitlementService` | ❌ |
| **#772** | execution-log retention/pruning | Cross-cutting like #834 | Sweep + schema = core; the configurable/long window = enterprise knob via the resolver clamp + license `caps` | ✅ |
| **#868** | per-schedule execution analytics | Capture + aggregation | Capture must be core (history can't be backfilled); dashboards + long-window queries = enterprise surface | ✅ |
| **#848** | MCP inline email auth | Auth-path change, adoption | Core, **not gateable** — an auth/security primitive *and* a funnel feature; paywalling auth is dangerous | ⚠️ |
| **#866** | SITE-002 public agent page | Pure funnel | Keep free (§5 principle). SITE-001 was reverted (#867) → re-architected anyway; decide tier deliberately now | ✅ |
| **#736 / #738** | A2A outbound + Trinity↔Trinity federation | Multi-surface + interop standard | Federation governance = enterprise, but the gate spans backend *and* MCP tool; gating an interop standard kills its network effect | ⚠️ |
| **#851 / #717** | CI pipeline / settings-tab refactor | Not product features | Must sit entirely *outside* the entitlement model | ✅ (as exclusion) |

### 8.1 What the stress test forced into the design

**R1 — The `EntitlementService` needs a second form factor (the one
outright break, from #846).** §3 assumed one Python service with three
consumers. The MCP server is a separate TypeScript process and *cannot
import it*. Every feature gated at the MCP-tool layer (#846, half of
A2A #736, future MCP-exposed agents) needs the MCP server to read
entitlements over the wire — a small `GET /api/internal/entitlements`
(or an extension of the existing `/feature-flags`) fetched at MCP
startup and refreshed periodically. **Without this, an entire class of
features cannot be gated.** This must be built in Phase 0 alongside the
service itself, not retrofitted.

**R2 — Add an explicit "not gateable" category.** #851 (CI), #717
(refactor), #848 (auth path), and #758 (vendor telemetry) must be
*named* as non-gates. The inventory pass already over-classified Slack
as enterprise — the same failure mode. Hard rule: **never gate auth,
security, schema/data-capture, or build/CI.** Anything in this bucket is
out of scope for the license entirely.

**R3 — "Capture is always core" is a sequencing rule, not just a code
rule** (confirmed by #834, #772, #868). Corollary that affects the
*roadmap*: for any audit/analytics/retention feature, the data capture
must ship in OSS at least one release *before* its paid surface —
otherwise Enterprise has zero history on day one and the feature looks
broken. Extraction order is therefore constrained, not free.

**R4 — SSO is #834-shaped, not audit-log-shaped.** #847 implicitly
treats SSO as a clean private-module drop-in. It is not — it changes
core token issuance. The genuinely clean leaves in #847 are **SCIM** (a
provisioning router) and **SIEM export** (a log consumer). Extract those
first; treat SSO as the harder "core auth seam + enterprise provider
plugin" variant (the #834 pattern), scheduled accordingly.

**R5 — Do not gate interop standards.** A2A's value *is* the network
effect; fencing outbound A2A behind Enterprise removes the reason to
adopt it. The enterprise surface is *federation governance* (managing a
fleet of Trinities), not the protocol. This sharpens the §5 funnel
principle: protocols and integrations that drive adoption stay free even
when their org-scale management is paid.

### 8.2 The refined model — four patterns, not two

§3 described two patterns; the stress test shows there are four. Every
gateable feature is exactly one of these:

1. **Clean leaf** → submodule router + one `requires_entitlement`
   dependency line. *Examples: audit log, SCIM, SIEM export, the #834
   recovery API.*
2. **Core primitive + enterprise knob** → the mechanism and schema stay
   in core; a parameter is clamped by the license caps via the settings
   resolver. *Examples: #834 retention, #772 log retention, #868
   analytics window.*
3. **Multi-surface gate with an MCP edge** → needs R1's entitlement
   endpoint; the gate is replicated into the MCP process. *Examples:
   #846, A2A tools.*
4. **Not gateable** → auth, security, schema/capture, CI, refactors,
   vendor telemetry. Explicitly excluded from the model. *Examples:
   #848, #851, #717, #758.*

**Verdict:** the open-core + `EntitlementService` direction survives the
stress test. The corrections are additive, not structural: build the
MCP entitlement edge in Phase 0 (R1), publish the non-gate list (R2),
and respect capture-before-surface sequencing (R3). The doc's §3/§4
remain correct for patterns 1–2; patterns 3–4 are the additions this
section introduces.

---

## 9. Concrete next steps (if pursuing)

1. **Decision (owners, not engineering):** OSS license choice;
   per-instance vs per-seat; which tier each ambiguous feature lands in.
2. **Phase 0 — seam, no split (public repo, ~1 sprint):**
   `EntitlementService` (stub: everything entitled), `requires_entitlement`
   dependency, `/feature-flags` extension, conditional
   `register_enterprise()` in `main.py` with the `ModuleNotFoundError`
   fallback, the CI "build without submodule" job, **and the R1 MCP
   entitlement edge** (`GET /api/internal/entitlements` + MCP-server
   poll). R1 is Phase 0, not a later add — pattern-3 features can't be
   gated without it. Publish the **R2 non-gate list** as a written rule
   in the same phase.
3. **Phase 1 — licensing:** Ed25519 token format, verify path, signing
   CLI, admin License UI, grace/clock-tamper handling.
4. **Phase 2 — extract the clean leaves:** create private
   `trinity-enterprise` submodule; move *audit log* first (cleanest
   boundary, highest enterprise value, self-contained:
   `routers/audit_log.py` + `services/platform_audit_service.py` + its
   tables), then **SCIM and SIEM export** (also clean leaves per R4).
   Validate the whole machine end-to-end on audit log before moving the
   rest. Respect R3: any feature whose value is captured history must
   already be capturing in OSS.
5. **Phase 3 — the #834 pattern:** prove "core-primitive + enterprise-knob"
   end-to-end (recovery API + license-capped retention), since most
   features (incl. SSO) take this shape, not the clean-leaf shape.
6. **Phase 4 — build SSO/SAML** as the first net-new paid feature, using
   the Phase 3 pattern (core auth seam + enterprise provider plugin —
   it is *not* a clean drop-in; see R4).

---

*End of research. No code moved, no license changed — this documents
options and a recommendation only.*
