# Enterprise Modules — Feature Flow

How Trinity's open-core split actually works at runtime. End-to-end
walk-through with file:line links for every load-bearing piece.
Companion to:

- [`docs/planning/ENTERPRISE_ARCHITECTURE.md`](../../planning/ENTERPRISE_ARCHITECTURE.md) — decision record (the "why")
- [`docs/planning/OSS_ENTERPRISE_SPLIT_RESEARCH.md`](../../planning/OSS_ENTERPRISE_SPLIT_RESEARCH.md) — long-form research
- [`docs/dev/ENTERPRISE_LOCAL_DEV.md`](../../dev/ENTERPRISE_LOCAL_DEV.md) — 15-min clone-to-running guide

## Current state (#910 + #941)

**What ships today:** the entitlement seam + the audit log dashboard.
The original #847 Phase 0 PoC mounted a `/api/enterprise/sso/*` router
with mock OIDC/SAML providers; that scaffold was removed in #910 scope
expansion (which now closes both #847 and #941). SSO returns later with
a real implementation, not a stub.

Currently registered enterprise feature: **`audit`**. The audit log
dashboard at `/enterprise/audit` is the first concrete enterprise UI;
its backend endpoints stay OSS in `routers/audit_log.py` (the
entitlement only flips the OSS-side dashboard ROUTE from hidden to
visible). See [`audit-trail.md`](audit-trail.md) for the dashboard
feature flow.

Historical SSO snippets below are kept for reference on the seam
pattern itself — the mechanism (try/except + `register_module()` +
`enterprise_features` flag) is unchanged.

## Topology

```
┌────────────────────────────────────────────────────────────────┐
│  abilityai/trinity   (public OSS)                              │
│                                                                │
│   ┌──────────────┐         ┌─────────────────────┐             │
│   │ main.py      │ try/    │ EntitlementService  │             │
│   │              │ except  │  ._registered_      │             │
│   │  enterprise  │────────▶│   modules: set()    │             │
│   │  import      │         │  .register_module() │             │
│   └──────────────┘         │  .is_entitled()     │             │
│         ▲                  │  .list_entitled_…() │             │
│         │ mounts at        └──────────┬──────────┘             │
│         │ src/backend/                │                        │
│         │ enterprise/                 ▼                        │
│         │                  ┌────────────────────────────────┐  │
│   ┌─────┴────────┐         │  /api/settings/feature-flags   │  │
│   │ enterprise.  │         │   → enterprise_features: [...] │  │
│   │   backend.   │         └────────────────┬───────────────┘  │
│   │   register   │                          │ (HTTP)           │
│   │   _enterprise│                          ▼                  │
│   └──────────────┘         ┌────────────────────────────────┐  │
│                            │  stores/enterprise.js (Pinia)  │  │
│   ┌──────────────┐         │   .enterpriseFeatures = [...]  │  │
│   │ NavBar.vue   │◀────────│   .isEntitled('sso')           │  │
│   │ Login.vue    │  reads  │   .hasAnyEnterprise            │  │
│   │ router/index │         └────────────────────────────────┘  │
│   └──────────────┘                                             │
└──────────────────────────────┬─────────────────────────────────┘
                               │ git submodule
                               ▼
┌────────────────────────────────────────────────────────────────┐
│  Abilityai/trinity-enterprise   (PRIVATE)                      │
│                                                                │
│   backend/__init__.py                                          │
│   ├── register_enterprise(app)                                 │
│   │   ├── app.include_router(sso_router, prefix=...)           │
│   │   └── entitlement_service.register_module("sso")           │
│   │                                                            │
│   └── sso/ — router + provider ABC + stubs                     │
└────────────────────────────────────────────────────────────────┘
```

Single source of truth at runtime: the in-memory set
`EntitlementService._registered_modules`. Everything else reads from
it (directly on the backend, via HTTP on the frontend).

## The boot chain

### 1. Backend startup — conditional submodule import

[`src/backend/main.py:867-883`](../../../src/backend/main.py)

```python
try:
    from enterprise.backend import register_enterprise
    register_enterprise(app)
    print("Trinity Enterprise modules registered", flush=True)
except ImportError:
    print(
        "Trinity Enterprise submodule not present — OSS-only build "
        "(this is normal; enterprise modules are an optional private submodule)",
        flush=True,
    )
```

Why `print(flush=True)` not `logger.info`: this block runs at module
init, **before** `lifespan` calls `setup_logging()`. Default Python
logging is WARNING — INFO records get dropped silently. Print to
stdout instead so `docker logs` always captures it.

### 2. Private submodule — `register_enterprise(app)`

[`src/backend/enterprise/backend/__init__.py:50-83`](../../../src/backend/enterprise/backend/__init__.py)

```python
def register_enterprise(app) -> None:
    if getattr(app.state, "enterprise_registered", False):
        logger.debug("Enterprise modules already registered; skipping")
        return

    from services.entitlement_service import entitlement_service

    # Audit log dashboard (#941) — entitlement flips the OSS-side
    # dashboard route from hidden to visible. Endpoints live in the
    # public repo (`routers/audit_log.py`); no router mount needed here.
    entitlement_service.register_module("audit")

    app.state.enterprise_registered = True
```

Two side effects per feature: mount the router, register the
feature_id. The registration is what makes the feature visible in the
feature-flags response. Order matters — a router mount without a
register_module call would expose endpoints the frontend can't
discover.

### 3. EntitlementService — registry

[`src/backend/services/entitlement_service.py:42-128`](../../../src/backend/services/entitlement_service.py)

```python
class EntitlementService:
    def __init__(self) -> None:
        self._oss_only = os.getenv("TRINITY_OSS_ONLY", "0").lower() in {"1", "true", "yes"}
        self._registered_modules: set[str] = set()

    def register_module(self, feature_id: str) -> None:
        if feature_id in self._registered_modules:
            return
        self._registered_modules.add(feature_id)
        logger.info(f"[EntitlementService] registered enterprise module: {feature_id!r} ...")

    def is_entitled(self, feature_id: str) -> bool:
        if self._oss_only:
            return False
        return feature_id in self._registered_modules

    def list_entitled_features(self) -> list[str]:
        if self._oss_only:
            return []
        return sorted(self._registered_modules)
```

OSS-only build never calls `register_module` → set stays empty →
both `is_entitled()` and `list_entitled_features()` return
false/empty. `TRINITY_OSS_ONLY=1` is a hard override that empties
the response even when modules ARE registered.

## Request-time gating

### 4. Feature-flags endpoint

[`src/backend/routers/settings.py:107-145`](../../../src/backend/routers/settings.py)

```python
@router.get("/feature-flags")
async def get_public_feature_flags(...):
    from services.entitlement_service import entitlement_service
    return {
        ...
        "enterprise_features": entitlement_service.list_entitled_features(),
    }
```

Reads the registry; does **not** check the filesystem. The folder
existence affects this only indirectly — through whether
`register_enterprise()` ran at boot.

### 5. Per-endpoint gate — `requires_entitlement` dependency

[`src/backend/dependencies.py:201-240`](../../../src/backend/dependencies.py)

```python
def requires_entitlement(feature_id: str):
    def _requires_entitlement():
        from services.entitlement_service import entitlement_service
        if not entitlement_service.is_entitled(feature_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Enterprise feature '{feature_id}' is not licensed for "
                    "this instance. Contact your administrator."
                ),
            )
        return None
    return _requires_entitlement
```

FastAPI dependency factory mirroring `require_role`. Used on
enterprise endpoints — e.g. the SSO router in the private repo
applies `Depends(requires_entitlement("sso"))` per route.

## Frontend wiring

### 6. Pinia store — caches feature flags

[`src/frontend/src/stores/enterprise.js:21-67`](../../../src/frontend/src/stores/enterprise.js)

```js
export const useEnterpriseStore = defineStore('enterprise', {
  state: () => ({
    featureFlagsLoaded: false,
    enterpriseFeatures: [],   // e.g. ['sso', 'scim', 'siem']
  }),
  getters: {
    isEntitled: (state) => (featureId) => state.enterpriseFeatures.includes(featureId),
    hasAnyEnterprise: (state) => state.enterpriseFeatures.length > 0,
  },
  actions: {
    async loadFeatureFlags(force = false) {
      if (this.featureFlagsLoaded && !force) return
      // ... GET /api/settings/feature-flags, populate enterpriseFeatures
    },
  },
})
```

Single fetch per page load. Force-refresh via `force = true` (used
by the route guard).

### 7. NavBar — conditional render

[`src/frontend/src/components/NavBar.vue:69-86`](../../../src/frontend/src/components/NavBar.vue) (template)

```vue
<router-link
  v-if="enterpriseStore.hasAnyEnterprise"
  to="/enterprise"
  class="..."
>
  Enterprise
  <span class="...">PRO</span>
</router-link>
```

[`src/frontend/src/components/NavBar.vue:222,288`](../../../src/frontend/src/components/NavBar.vue) (script)

```js
const enterpriseStore = useEnterpriseStore()
// ... in onMounted:
enterpriseStore.loadFeatureFlags()
```

### 8. Route guard — defense-in-depth for bookmarks

[`src/frontend/src/router/index.js:118-141, 209-232`](../../../src/frontend/src/router/index.js)

```js
// Route definitions
{
  path: '/enterprise',
  name: 'EnterpriseLanding',
  component: () => import('../views/enterprise/Index.vue'),
  meta: { requiresAuth: true, requiresAnyEntitlement: true }
},
{
  path: '/enterprise/sso',
  name: 'EnterpriseSSO',
  component: () => import('../views/enterprise/SSO.vue'),
  meta: { requiresAuth: true, requiresEntitlement: 'sso' }
},

// Guard (inside beforeEach):
const entitlement = to.meta.requiresEntitlement
const requireAny = to.meta.requiresAnyEntitlement
if (entitlement || requireAny) {
  const { useEnterpriseStore } = await import('../stores/enterprise')
  const enterpriseStore = useEnterpriseStore()
  await enterpriseStore.loadFeatureFlags()
  if (entitlement && !enterpriseStore.isEntitled(entitlement)) {
    next(enterpriseStore.hasAnyEnterprise ? '/enterprise' : '/')
    return
  }
  if (requireAny && !enterpriseStore.hasAnyEnterprise) {
    next('/')
    return
  }
}
```

Two modes — `requiresEntitlement: '<id>'` for per-feature pages,
`requiresAnyEntitlement: true` for the catalogue landing. NavBar
hides links to non-entitled routes; the guard catches direct URL
visits / bookmarks.

### 9. Login page — SSO buttons

[`src/frontend/src/views/Login.vue:213-242, 352`](../../../src/frontend/src/views/Login.vue)

```js
const ssoProviders = ref([])
async function loadSSOProviders() {
  try {
    const { data } = await axios.get('/api/enterprise/sso/providers')
    ssoProviders.value = Array.isArray(data) ? data : []
  } catch {
    ssoProviders.value = []
  }
}
// fired from onMounted
loadSSOProviders()
```

```vue
<div v-if="!codeSent && ssoProviders.length > 0" class="...">
  <div class="...">─── or sign in with ───</div>
  <button v-for="p in ssoProviders" :key="p.provider_id" ...>
    <span>{{ ssoIcon(p) }}</span>
    <span>Continue with {{ p.display_name }}</span>
  </button>
</div>
```

`/api/enterprise/sso/providers` is entitlement-gated but **not**
user-gated — the pre-login screen can call it. OSS-only build: the
router isn't mounted → 404 → catch swallows → list empty → section
hidden.

### 10. Catalogue + SSO admin views (OSS-bundled)

The Vue files for enterprise pages live in the public repo:

- [`src/frontend/src/views/enterprise/Index.vue`](../../../src/frontend/src/views/enterprise/Index.vue) — landing with 5 feature cards (SSO Available; SCIM/SIEM/License/Audit "Coming soon")
- [`src/frontend/src/views/enterprise/SSO.vue`](../../../src/frontend/src/views/enterprise/SSO.vue) — provider list + claim-mapping table + session-policy panel + add-provider modal

They ship in the OSS bundle but are unreachable when not entitled
(nav + route guard hide them). Vue components have no algorithmic
IP — see [`ENTERPRISE_ARCHITECTURE.md`](../../planning/ENTERPRISE_ARCHITECTURE.md#why-backend-only-private-frontend-ships-in-oss)
for the rationale.

## Private repo (PoC)

[`Abilityai/trinity-enterprise`](https://github.com/Abilityai/trinity-enterprise) — backend only:

| File | What |
|---|---|
| [`backend/__init__.py`](https://github.com/Abilityai/trinity-enterprise/blob/main/backend/__init__.py) | `register_enterprise(app)` — single integration entry |
| [`backend/sso/router.py`](https://github.com/Abilityai/trinity-enterprise/blob/main/backend/sso/router.py) | `/api/enterprise/sso/{providers,login/{id},claim-mapping,session-policy}` — all stubs |
| [`backend/sso/providers.py`](https://github.com/Abilityai/trinity-enterprise/blob/main/backend/sso/providers.py) | `SSOProvider` ABC + `StubProvider` for the PoC registry |
| `pyproject.toml`, `LICENSE` (proprietary), `README.md` | metadata + docs |

The `/api/enterprise/sso/providers` endpoint seeds two mock
providers at module import (Okta-mock + Azure-AD-mock) so the OSS
admin UI demo renders realistic content. Display names carry
`(Mock — PoC)` so operators can't mistake them for working providers.

## Failure modes

### OSS-only build (submodule absent)

```
docker logs trinity-backend | grep "Trinity Enterprise"
# → "Trinity Enterprise submodule not present — OSS-only build ..."

GET /api/settings/feature-flags  → "enterprise_features": []
GET /api/enterprise/sso/providers → 404 Not Found  (router not mounted)
```

Frontend: nav link hidden, login SSO section hidden, direct URL
visits redirect to `/`. Verified manually by `mv src/backend/enterprise{,.disabled}` + force-recreate backend.

### Hard override (compliance lockdown)

```
echo "TRINITY_OSS_ONLY=1" >> .env
docker compose up -d --force-recreate backend

GET /api/settings/feature-flags  → "enterprise_features": []
GET /api/enterprise/sso/providers → 403 "Enterprise feature 'sso' is not licensed"
```

Submodule may be mounted but every enterprise endpoint denies.
Used for: operators who want the OSS UX even with enterprise present
(testing the deny path); CI builds that exercise the gate.

[`docker-compose.yml`](../../../docker-compose.yml) passes the env
through to the backend container.

## Adding a new enterprise feature — recipe

Say you want to ship SCIM provisioning.

### 1. Private repo (backend logic + IP)
```python
# backend/scim/router.py
from fastapi import APIRouter, Depends
router = APIRouter()
# ... endpoints, gated by Depends(requires_entitlement("scim")) ...

# backend/__init__.py — extend register_enterprise:
from .scim.router import router as scim_router
app.include_router(scim_router, prefix="/api/enterprise/scim", tags=["enterprise-scim"])
entitlement_service.register_module("scim")
```

### 2. Public repo (frontend UI)
```vue
<!-- src/frontend/src/views/enterprise/SCIM.vue -->
<script setup>
import api from '../../api'
// ... fetch /api/enterprise/scim/whatever, render UI ...
</script>
```

```js
// src/frontend/src/router/index.js — add route:
{
  path: '/enterprise/scim',
  name: 'EnterpriseSCIM',
  component: () => import('../views/enterprise/SCIM.vue'),
  meta: { requiresAuth: true, requiresEntitlement: 'scim' }
},

// src/frontend/src/views/enterprise/Index.vue — flip the card:
{ id: 'scim', ..., soon: false },   // was true
```

### 3. Bump submodule pointer in public repo
```bash
cd src/backend/enterprise && git pull
cd ../../../ && git add src/backend/enterprise && git commit -m "chore: bump enterprise submodule (SCIM)"
```

No CI changes needed — the build-without-submodule workflow already
asserts the conditional path works for any feature_id.

## Tests

[`tests/unit/test_847_entitlement_seam.py`](../../../tests/unit/test_847_entitlement_seam.py) — 13 unit tests + 2 skipped (local-only):

| Test | Asserts |
|---|---|
| `test_empty_registry_denies_every_feature` | OSS default state — empty list, all denied |
| `test_register_module_then_entitled` | Post-`register_module("sso")`, `is_entitled` True + listed |
| `test_register_module_is_idempotent` | Double-register doesn't grow list |
| `test_oss_only_denies_every_feature_even_when_registered` | Env override beats registry |
| `test_oss_only_falsy_keeps_registry_behaviour` | Falsy spellings (0/false/no/"") keep registry |
| `test_requires_entitlement_allows_when_entitled` | Dependency `Depends()` returns None on allow |
| `test_requires_entitlement_raises_403_when_denied` | Dependency raises HTTP 403 with feature_id in detail |
| `test_set_for_testing_swaps_singleton` | Test seam for injecting custom service |
| `test_main_py_uses_conditional_enterprise_import` | Static check — main.py has try/except ImportError |

CI also runs [`build-without-submodule.yml`](../../../.github/workflows/build-without-submodule.yml)
which boots the backend with the submodule absent and asserts:

- `/health` responds
- `/api/settings/feature-flags` returns `enterprise_features: []`
- `/api/enterprise/sso/providers` returns 404
- "Trinity Enterprise submodule not present" log line emitted

## Out of scope (open follow-ups)

| Phase | What | Status |
|---|---|---|
| 1 | Ed25519-signed license token + admin License UI | not started |
| 2 | Extract audit log into the private submodule (first real enterprise module beyond SSO PoC) | not started |
| 3 | Prove "core-primitive + enterprise-knob" pattern via #834 (recovery API in OSS, license-capped retention in enterprise) | not started |
| 4 | Real SSO/SAML/OIDC implementation (replaces PoC stubs) | not started |
| — | MCP entitlement edge (`GET /api/internal/entitlements` polled by the TypeScript MCP server) | not started |
| — | Fix repo license-of-record (currently `NOASSERTION`) | owner decision |

Tracking issue: [#847](https://github.com/abilityai/trinity/issues/847).
