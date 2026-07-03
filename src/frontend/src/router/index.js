import { createRouter, createWebHistory } from 'vue-router'
import axios from 'axios'
import { useAuthStore } from '../stores/auth'
import { useSessionsStore } from '../stores/sessions'

const routes = [
  {
    path: '/setup',
    name: 'Setup',
    component: () => import('../views/SetupPassword.vue'),
    meta: { requiresAuth: false, isSetup: true, title: 'Setup' }
  },
  {
    path: '/login',
    name: 'Login',
    component: () => import('../views/Login.vue'),
    meta: { requiresAuth: false, title: 'Login' }
  },
  {
    path: '/chat/:token',
    name: 'PublicChat',
    component: () => import('../views/PublicChat.vue'),
    meta: { requiresAuth: false, title: 'Chat' }
  },
  {
    path: '/',
    name: 'Dashboard',
    component: () => import('../views/Dashboard.vue'),
    meta: { requiresAuth: true, title: 'Dashboard' }
  },
  {
    path: '/agents',
    name: 'Agents',
    component: () => import('../views/Agents.vue'),
    meta: { requiresAuth: true, title: 'Agents' }
  },
  // #1109 — Health consolidated into the Operations "Health" tab.
  // Redirect preserves bookmarks; the tab is admin-gated inside Operations.vue.
  {
    path: '/monitoring',
    redirect: { path: '/operations', query: { tab: 'health' } }
  },
  {
    path: '/agents/:name',
    name: 'AgentDetail',
    component: () => import('../views/AgentDetail.vue'),
    // #1418 — the :name param IS the canonical agent identifier in Trinity.
    meta: { requiresAuth: true, title: (to) => to.params.name }
  },
  {
    path: '/agents/:name/workspace',
    name: 'AgentWorkspace',
    component: () => import('../views/AgentWorkspace.vue'),
    meta: { requiresAuth: true, title: (to) => `${to.params.name} · Workspace` },
    beforeEnter: async (to, from) => {
      const sessionsStore = useSessionsStore()
      await sessionsStore.loadFeatureFlags()
      if (!sessionsStore.workspaceAvailable) {
        return { name: 'AgentDetail', params: { name: to.params.name } }
      }
      return true
    },
  },
  {
    // #58/#60 (trinity-enterprise) — Brain Orb: capability-gated per-agent page.
    // The guard enforces BOTH gates so the orb is NEVER launchable on a
    // non-Cornelius agent (not even via a raw URL): the platform flag AND the
    // per-agent `brain-orb` capability from template.yaml. A non-capable agent
    // (or a stopped agent whose /info can't be read) redirects to AgentDetail.
    path: '/agents/:name/brain',
    name: 'AgentBrainOrb',
    component: () => import('../views/AgentBrainOrb.vue'),
    meta: { requiresAuth: true },
    beforeEnter: async (to, from) => {
      const sessionsStore = useSessionsStore()
      const authStore = useAuthStore()
      await sessionsStore.loadFeatureFlags()
      const bail = () => ({ name: 'AgentDetail', params: { name: to.params.name } })
      if (!sessionsStore.brainOrbAvailable) return bail()
      try {
        const r = await axios.get(`/api/agents/${to.params.name}/info`, { headers: authStore.authHeader })
        const caps = Array.isArray(r.data?.capabilities) ? r.data.capabilities : []
        return caps.includes('brain-orb') ? true : bail()
      } catch (_) {
        return bail()
      }
    },
  },
  {
    path: '/agents/:name/executions/:executionId',
    name: 'ExecutionDetail',
    component: () => import('../views/ExecutionDetail.vue'),
    meta: { requiresAuth: true, title: (to) => `${to.params.name} · Execution` }
  },
  // REMOVED: /files route - file management is now per-agent via Files tab in AgentDetail
  // REMOVED: /credentials route - credentials are now managed per-agent only
  // Old global credential management is replaced by per-agent CredentialsPanel
  {
    path: '/templates',
    name: 'Templates',
    component: () => import('../views/Templates.vue'),
    meta: { requiresAuth: true, title: 'Templates' }
  },
  // Legacy redirect: Events consolidated into the Operations Notifications tab (#1109)
  {
    path: '/events',
    redirect: '/operations?tab=notifications'
  },
  // #1109 — Operations: single fleet-operations surface (Needs Response,
  // Notifications, Health [admin], Executions, Resolved). Intentionally NOT
  // requiresAdmin — non-admins reach Ops/Executions; Health is gated at the
  // tab level inside Operations.vue.
  {
    path: '/operations',
    name: 'Operations',
    component: () => import('../views/Operations.vue'),
    meta: { requiresAuth: true, title: 'Operations' }
  },
  // Legacy redirects — function form so existing ?tab= deep links survive
  // the hop (a string/object redirect would drop the incoming query).
  {
    path: '/operating-room',
    redirect: to => ({ path: '/operations', query: to.query })
  },
  {
    path: '/executions',
    redirect: { path: '/operations', query: { tab: 'executions' } }
  },
  {
    // #302 — /api-keys absorbed into /settings as the "MCP Keys" tab.
    // Permanent redirect preserves bookmarks and external doc links.
    path: '/api-keys',
    redirect: { path: '/settings', query: { tab: 'mcp-keys' } }
  },
  {
    path: '/settings',
    name: 'Settings',
    component: () => import('../views/Settings.vue'),
    meta: { requiresAuth: true, requiresAdmin: true, title: 'Settings' }
  },
  // Legacy redirect for /system-agent -> agents page (consolidated)
  {
    path: '/system-agent',
    redirect: '/agents/trinity-system'
  },
  // Legacy redirect for /network -> Dashboard
  {
    path: '/network',
    redirect: '/'
  },
  // #847 Phase 0 — enterprise tab. The Vue source ships in the OSS
  // bundle but routes are gated by `requiresEntitlement` (must be in
  // `enterpriseStore.enterpriseFeatures`). Backend
  // `/api/enterprise/*` returns 404 in OSS-only builds (no submodule
  // mounted), so even a direct URL visit shows the view's empty
  // state cleanly. See `docs/planning/ENTERPRISE_ARCHITECTURE.md`.
  //
  // Landing page uses the special `requiresAnyEntitlement: true`
  // marker — visible whenever ANY enterprise feature is entitled,
  // so OSS users with one of {sso, scim, siem, …} can reach the
  // catalogue overview. Per-feature routes below use the specific
  // `requiresEntitlement: '<id>'` so non-entitled features 302 to
  // the catalogue.
  {
    path: '/enterprise',
    name: 'EnterpriseLanding',
    component: () => import('../views/enterprise/Index.vue'),
    meta: { requiresAuth: true, requiresAnyEntitlement: true, title: 'Enterprise' }
  },
  {
    path: '/enterprise/audit',
    name: 'EnterpriseAudit',
    component: () => import('../views/enterprise/Audit.vue'),
    meta: { requiresAuth: true, requiresEntitlement: 'audit', title: 'Audit Log' }
  },
  // Mobile Admin PWA (MOB-001) — standalone, no NavBar
  {
    path: '/m',
    name: 'MobileAdmin',
    component: () => import('../views/MobileAdmin.vue'),
    meta: { requiresAuth: false, title: 'Mobile' }  // handles its own inline auth
  },
  // Catch-all redirect to dashboard
  {
    path: '/:pathMatch(.*)*',
    redirect: '/'
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

// Cache for setup status check (avoid repeated API calls)
let setupStatusCache = null
let setupStatusCacheTime = 0
const SETUP_CACHE_DURATION = 5000 // 5 seconds

async function checkSetupStatus() {
  const now = Date.now()
  // Use cached value if recent
  if (setupStatusCache !== null && (now - setupStatusCacheTime) < SETUP_CACHE_DURATION) {
    return setupStatusCache
  }

  try {
    const response = await fetch('/api/setup/status')
    const data = await response.json()
    setupStatusCache = data.setup_completed
    setupStatusCacheTime = now
    return setupStatusCache
  } catch (e) {
    console.error('Failed to check setup status:', e)
    // Assume setup is completed if check fails (don't block access)
    return true
  }
}

// Navigation guard
// Vue Router 5 deprecated the `next` callback; guards now signal intent by
// returning a value (true/undefined = proceed, a location = redirect).
router.beforeEach(async (to, from) => {
  const authStore = useAuthStore()

  // Wait for auth initialization to complete (PERF-269: replaced blind 100ms sleep)
  if (authStore.isLoading) {
    await authStore.waitForInit()
  }

  // Check setup status for login and protected routes
  if (!to.meta.isSetup) {
    const setupCompleted = await checkSetupStatus()

    // If setup not completed, redirect to setup page
    if (!setupCompleted) {
      // Allow access to public routes that don't need setup
      if (to.path === '/chat' || to.path.startsWith('/chat/')) {
        return true
      }
      return '/setup'
    }

    // If setup completed and trying to access setup page, redirect to login
    if (to.path === '/setup') {
      return '/login'
    }
  }

  // Check if route requires authentication
  if (to.meta.requiresAuth) {
    if (authStore.isAuthenticated) {
      // #847 — enterprise entitlement guard.
      // Two modes:
      //   * `meta.requiresEntitlement: '<id>'` — gate on the named
      //     feature. Used by `/enterprise/sso` etc.
      //   * `meta.requiresAnyEntitlement: true` — gate on
      //     `hasAnyEnterprise` (the catalogue landing page is
      //     reachable whenever any enterprise feature is entitled).
      // NavBar already hides links to non-entitled routes; the guard
      // catches direct URL visits / bookmarks.
      const entitlement = to.meta.requiresEntitlement
      const requireAny = to.meta.requiresAnyEntitlement
      if (entitlement || requireAny) {
        const { useEnterpriseStore } = await import('../stores/enterprise')
        const enterpriseStore = useEnterpriseStore()
        await enterpriseStore.loadFeatureFlags()
        if (entitlement && !enterpriseStore.isEntitled(entitlement)) {
          // Non-entitled per-feature → bounce to the catalogue if
          // the user can see ANY enterprise feature, else dashboard.
          return enterpriseStore.hasAnyEnterprise ? '/enterprise' : '/'
        }
        if (requireAny && !enterpriseStore.hasAnyEnterprise) {
          return '/'
        }
      }
      return true
    } else {
      // User is not authenticated, redirect to login
      return '/login'
    }
  } else if (to.path === '/login' && authStore.isAuthenticated) {
    // User is authenticated but trying to access login page
    // Redirect to dashboard
    return '/'
  } else {
    // Public route, allow access
    return true
  }
})

// Per-route browser-tab titles (#1418). `meta.title` is a string, or a function
// of the resolved route for dynamic agent-scoped pages (the :name param is the
// canonical agent identifier). afterEach fires once per completed navigation —
// including the final destination of a redirect — so SPA nav updates the tab
// title with no reload. Routes without a title (redirects, catch-all) fall back
// to the branded default that index.html ships as the pre-hydration title.
const BASE_TITLE = 'Trinity'
router.afterEach((to) => {
  const raw = to.meta?.title
  const label = typeof raw === 'function' ? raw(to) : raw
  document.title = label ? `${BASE_TITLE} — ${label}` : `${BASE_TITLE} — Agent Orchestration`
})

// Clear setup cache on successful setup
export function clearSetupCache() {
  setupStatusCache = null
  setupStatusCacheTime = 0
}

export default router
