import { createRouter, createWebHistory } from 'vue-router'
import { setBaseTitle } from '@/utils/tabTitle'
import axios from 'axios'
import { useAuthStore } from '../stores/auth'
import { isWorkspaceOnlyRole, landingAfterSignIn, operatorRouteRedirect } from '../utils/workspaceOnly'
import { useSessionsStore } from '../stores/sessions'
import { useAgentsStore } from '../stores/agents'

// #1643: browser-tab title for an agent-scoped page. The :name param stays the
// canonical slug (routing/keys); the tab shows the human display name when the
// agents store is warm, falling back to the slug on a cold direct load (the
// store isn't fetched yet). SPA navigation self-heals to the label on the next
// nav. try/catch guards the pre-pinia edge.
function agentTabTitle(slug) {
  try {
    return useAgentsStore().displayNameForSlug(slug)
  } catch {
    return slug
  }
}

// Exported so the route table can be exercised without constructing a
// web-history router (ent#357): the redirect behaviour for the legacy /portal
// links is a property of these definitions, and asserting it through a
// memory-history router is the only way to test the real thing rather than a
// restatement of it.
export const routes = [
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
  // trinity-enterprise#260 — the standalone Agents page is retired; its list
  // lives on as the Dashboard's List mode. Query-preserving function redirect
  // (#1109 /operating-room precedent): `?view=list` is a one-shot,
  // NON-persisting intent the Dashboard applies then strips — it never
  // rewrites the user's saved view selection. Exact-segment matching leaves
  // /agents/:name and deeper routes untouched.
  {
    path: '/agents',
    redirect: to => ({ path: '/', query: { ...to.query, view: 'list' } })
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
    // #1643 — the rendered tab title resolves to the display name (slug fallback).
    // #3446 — hideHelpLauncherOnTabs: the floating help launcher sits in the
    // Chat composer's corner and covered Send. Only that tab, and only the
    // launcher: the rest of the page keeps help, and an open help conversation
    // is not torn down by a tab click. Reads `?tab=`, which the page keeps
    // current (#2900).
    meta: {
      requiresAuth: true,
      title: (to) => agentTabTitle(to.params.name),
      hideHelpLauncherOnTabs: ['chat'],
    }
  },
  {
    // ent#438 — the per-agent workspace is retired. It was a voice orb beside a
    // canvas panel, gated behind WORKSPACE_ENABLED && GEMINI_API_KEY, and by the
    // time this landed it had no capability of its own left: ent#440 put voice
    // conversation inside the Workspace, and the canvas became a durable
    // surface rendered on the agent's Workspace page and Agent Detail. Two
    // things called "workspace" was the confusion this issue opens with.
    //
    // Function form so query and hash survive (the ent#381 shape). `?agent=`
    // is the Workspace's own agent-selection param, so a bookmark to one
    // agent's workspace still lands on that agent rather than a generic index.
    path: '/agents/:name/workspace',
    redirect: to => ({
      path: '/workspace',
      query: { ...to.query, agent: to.params.name },
      hash: to.hash,
    }),
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
    meta: { requiresAuth: true, title: (to) => `${agentTabTitle(to.params.name)} · Execution` }
  },
  // REMOVED: /files route - file management is now per-agent via Files tab in AgentDetail
  // REMOVED: /credentials route - credentials are now managed per-agent only
  // Old global credential management is replaced by per-agent CredentialsPanel
  // ent#263 — Templates page renamed to Library: one surface for installable
  // assets (agent templates + the skills library).
  {
    path: '/library',
    name: 'Library',
    component: () => import('../views/Library.vue'),
    meta: { requiresAuth: true, title: 'Library' }
  },
  // Legacy redirect — function form so query AND hash survive the hop (the
  // #1109 precedent preserves query only; hash carry added here for the
  // Library's in-page section anchors).
  {
    path: '/templates',
    redirect: to => ({ path: '/library', query: to.query, hash: to.hash })
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
  {
    // ent#381: the standalone Sessions page is retired — a multi-agent Workspace
    // chat runs on the same ent#8 rooms substrate, so keeping both was two nav
    // entries for one job. The DATA is untouched; only this UI is gone.
    //
    // Function form so query and hash survive: these URLs were deep-linked and
    // shared, and a room id maps exactly onto the Workspace room route, so a
    // link to a specific room still lands on that conversation rather than
    // dumping the user at a generic index.
    path: '/sessions/:roomId?',
    redirect: to => (
      to.params.roomId
        ? { path: `/workspace/r/${to.params.roomId}`, query: to.query, hash: to.hash }
        : { path: '/workspace', query: to.query, hash: to.hash }
    ),
  },
  {
    // Workspace (ent#357, formerly "Client Portal") — a view of the agents
    // shared with WHOEVER is looking: an external client who signed in with a
    // verified email, or a platform user who is already logged in and arrives
    // in one click. Standalone (no NavBar / platform chrome), no requiresAuth:
    // the external path must stay reachable without an account. Backend 404s in
    // OSS/unentitled builds; the page shows its sign-in / unavailable state.
    // ent#554 — a shared canvas at a stable link. No `requiresAuth` for the
    // same reason the Workspace has none: a `public` share must open with no
    // account at all. An `authorized` share answers 401 from the API and the
    // view offers a sign-in — the SERVER decides which, never the router, since
    // a guard here would turn every public link into a login wall.
    path: '/canvas/s/:token',
    name: 'shared-canvas',
    component: () => import('../views/SharedCanvas.vue'),
    meta: { title: 'Shared canvas', hideHelpWidget: true }
  },
  {
    path: '/workspace',
    name: 'Workspace',
    component: () => import('../views/Portal.vue'),
    // hideHelpWidget: the operator-only help widget would overlap the
    // conversation's Send button (and is meaningless to a client) — off here.
    meta: { title: 'Workspace', hideHelpWidget: true }
  },
  {
    // trinity-enterprise#610: the Inbox — what needs you and what came back,
    // across your agents. The same Portal.vue shell renders it as a stage
    // branch; `bootstrap()` lands a bare `/workspace` here once (D9), while bare
    // `/workspace` itself stays the new-chat stage. Declared before the
    // parameterised Workspace routes by convention; it cannot collide with them
    // (`/c/`, `/r/`, `/a/` are literal prefixes).
    path: '/workspace/inbox',
    name: 'WorkspaceInbox',
    component: () => import('../views/Portal.vue'),
    meta: { title: 'Workspace · Inbox', hideHelpWidget: true }
  },
  {
    // #138: deep-linkable, refresh-safe conversation thread. The same shell as
    // /workspace (new-chat state); the :sessionId opens that thread. Back/forward
    // navigate between new-chat and threads.
    path: '/workspace/c/:sessionId',
    name: 'WorkspaceThread',
    component: () => import('../views/Portal.vue'),
    meta: { title: 'Workspace', hideHelpWidget: true }
  },
  {
    // ent#361: a multi-agent chat is a ROOM, not a portal thread — different
    // substrate, different id space — so it gets its own deep link rather than
    // overloading `/c/`.
    path: '/workspace/r/:roomId',
    name: 'WorkspaceRoom',
    component: () => import('../views/Portal.vue'),
    meta: { title: 'Workspace', hideHelpWidget: true }
  },
  {
    // ent#360: an agent is a destination, so it gets a URL. Deliberately NOT
    // `/workspace/:name` — that would collide with any future literal segment
    // and make every unknown path resolve to an agent page.
    path: '/workspace/a/:agentName',
    name: 'WorkspaceAgent',
    component: () => import('../views/Portal.vue'),
    // ent#556: the one Workspace route whose subject the ROUTE knows, so it is
    // the one that can name it. Keeps the surface in the title — a bare agent
    // name would lose "Workspace" — and reuses `agentTabTitle`, so an operator
    // (warm agents store) sees the display name and a client session, which
    // never populates that store, falls back to the slug. Threads and rooms
    // cannot do this: their subject lives in component state, not the route.
    meta: {
      title: (to) => `Workspace · ${agentTabTitle(to.params.agentName)}`,
      hideHelpWidget: true,
    }
  },
  {
    // ent#661: Projects — the person's list across agents, and one project.
    // Literal `projects` segment, safe beside `/c/`, `/r/`, `/a/` (ent#360).
    path: '/workspace/projects',
    name: 'WorkspaceProjects',
    component: () => import('../views/Portal.vue'),
    meta: { title: 'Workspace · Projects', hideHelpWidget: true }
  },
  {
    path: '/workspace/projects/:projectId',
    name: 'WorkspaceProject',
    component: () => import('../views/Portal.vue'),
    meta: { title: 'Workspace · Projects', hideHelpWidget: true }
  },
  // ent#357 legacy paths. Function form so query AND hash survive the hop —
  // these URLs were handed to real clients by email, and a client landing on a
  // dead link has no way to report it. `/portal/c/:sessionId` keeps the thread.
  {
    path: '/portal',
    redirect: to => ({ path: '/workspace', query: to.query, hash: to.hash })
  },
  {
    path: '/portal/c/:sessionId',
    redirect: to => ({
      path: `/workspace/c/${to.params.sessionId}`,
      query: to.query,
      hash: to.hash,
    })
  },
  // Mobile Admin PWA (MOB-001) — standalone, no NavBar
  {
    path: '/m',
    name: 'MobileAdmin',
    component: () => import('../views/MobileAdmin.vue'),
    // Handles its own inline auth, so not `requiresAuth` — but an operator
    // surface all the same (trinity-enterprise#837): see the guard below.
    meta: { requiresAuth: false, operatorSurface: true, title: 'Mobile' }
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
// #3004: what else the first admin must give ('instance-id' or null). Read by
// SetupPassword.vue at mount — this guard fetched it before /setup rendered, so
// the claim field is there on first paint instead of appearing afterwards.
// `undefined` means unknown (no successful fetch yet); the page then asks itself.
let setupClaimRequired = undefined
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
    setupClaimRequired = data.claim_required ?? null
    setupStatusCacheTime = now
    return setupStatusCache
  } catch (e) {
    setupClaimRequired = undefined
    console.error('Failed to check setup status:', e)
    // Assume setup is completed if check fails (don't block access)
    return true
  }
}

// trinity-enterprise#837 — the role the guard routes on. The STORED role, never
// the `role` getter (it reports 'user' while the profile loads); a stored `user`
// is confirmed with the server first, so an account raised to `operator` since
// its last sign-in is not bounced on a stale cache. Only that case waits.
async function confirmedRole(authStore) {
  if (isWorkspaceOnlyRole(authStore.user?.role) && !authStore.profileVerified) {
    await authStore.fetchUserProfile()
  }
  return authStore.user?.role
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

  // #2381: keep visitors off the setup page once the instance is provisioned.
  // This check must run for the setup route ITSELF, which is why it sits above
  // (and outside) the `!to.meta.isSetup` block below. It used to live inside
  // that block, where it was unreachable dead code by construction — `/setup`
  // carries `meta.isSetup: true`, so the only route the branch tested for was
  // the one route the block skipped. Navigating straight to /setup rendered the
  // full wizard on a completed install; only the backend 403 stopped a submit.
  if (to.meta.isSetup && (await checkSetupStatus())) {
    return '/login'
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
  }

  // trinity-enterprise#837 review: the mobile admin signs in by itself, outside
  // `requiresAuth`, but every call it makes refuses a Workspace-only member —
  // a signed-in member goes to the Workspace instead of a dead app.
  if (to.meta.operatorSurface && authStore.isAuthenticated) {
    const toWorkspace = operatorRouteRedirect(to, await confirmedRole(authStore))
    if (toWorkspace) return toWorkspace
  }

  // Check if route requires authentication
  if (to.meta.requiresAuth) {
    if (authStore.isAuthenticated) {
      // trinity-enterprise#837 — the operator UI starts at `operator`. A
      // Workspace-only member who opens an operator URL lands in the Workspace
      // (an agent page opens that agent's conversation). The API refuses them
      // regardless; this only spares them the wall.
      const toWorkspace = operatorRouteRedirect(to, await confirmedRole(authStore))
      if (toWorkspace) return toWorkspace

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
    // Redirect to dashboard — the Workspace for a Workspace-only member (ent#837)
    return landingAfterSignIn(undefined, await confirmedRole(authStore))
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
  // ent#557: routed THROUGH `setBaseTitle` rather than assigned here. The
  // Workspace's unread count is a prefix on this string and changes on its own
  // schedule, so with two direct writers the last one to fire would erase the
  // other's half — a navigation would drop the count, a count update would drop
  // the label. `utils/tabTitle.js` holds both halves and renders the whole
  // string; this line still owns what the LABEL says.
  setBaseTitle(label ? `${BASE_TITLE} — ${label}` : `${BASE_TITLE} — Agent Orchestration`)
})

export function getSetupClaimRequired() {
  return setupClaimRequired
}

// Clear setup cache on successful setup
export function clearSetupCache() {
  setupStatusCache = null
  setupStatusCacheTime = 0
}

export default router
