import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const routes = [
  {
    path: '/setup',
    name: 'Setup',
    component: () => import('../views/SetupPassword.vue'),
    meta: { requiresAuth: false, isSetup: true }
  },
  {
    path: '/login',
    name: 'Login',
    component: () => import('../views/Login.vue'),
    meta: { requiresAuth: false }
  },
  {
    path: '/chat/:token',
    name: 'PublicChat',
    component: () => import('../views/PublicChat.vue'),
    meta: { requiresAuth: false }
  },
  {
    path: '/',
    name: 'Dashboard',
    component: () => import('../views/Dashboard.vue'),
    meta: { requiresAuth: true }
  },
  {
    path: '/agents',
    name: 'Agents',
    component: () => import('../views/Agents.vue'),
    meta: { requiresAuth: true }
  },
  {
    path: '/agents/:name',
    name: 'AgentDetail',
    component: () => import('../views/AgentDetail.vue'),
    meta: { requiresAuth: true }
  },
  {
    path: '/credentials',
    name: 'Credentials',
    component: () => import('../views/Credentials.vue'),
    meta: { requiresAuth: true }
  },
  {
    path: '/templates',
    name: 'Templates',
    component: () => import('../views/Templates.vue'),
    meta: { requiresAuth: true }
  },
  {
    path: '/api-keys',
    name: 'ApiKeys',
    component: () => import('../views/ApiKeys.vue'),
    meta: { requiresAuth: true }
  },
  {
    path: '/settings',
    name: 'Settings',
    component: () => import('../views/Settings.vue'),
    meta: { requiresAuth: true, requiresAdmin: true }
  },
  {
    path: '/system-agent',
    name: 'SystemAgent',
    component: () => import('../views/SystemAgent.vue'),
    meta: { requiresAuth: true, requiresAdmin: true }
  },
  // Legacy redirect for /network -> Dashboard
  {
    path: '/network',
    redirect: '/'
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
router.beforeEach(async (to, from, next) => {
  const authStore = useAuthStore()

  // Wait for auth initialization to complete
  if (authStore.isLoading) {
    // Give it a moment to initialize
    await new Promise(resolve => setTimeout(resolve, 100))
  }

  // Check setup status for login and protected routes
  if (!to.meta.isSetup) {
    const setupCompleted = await checkSetupStatus()

    // If setup not completed, redirect to setup page
    if (!setupCompleted) {
      // Allow access to public routes that don't need setup
      if (to.path === '/chat' || to.path.startsWith('/chat/')) {
        next()
        return
      }
      next('/setup')
      return
    }

    // If setup completed and trying to access setup page, redirect to login
    if (to.path === '/setup') {
      next('/login')
      return
    }
  }

  // Check if route requires authentication
  if (to.meta.requiresAuth) {
    if (authStore.isAuthenticated) {
      // User is authenticated, allow access
      next()
    } else {
      // User is not authenticated, redirect to login
      next('/login')
    }
  } else if (to.path === '/login' && authStore.isAuthenticated) {
    // User is authenticated but trying to access login page
    // Redirect to dashboard
    next('/')
  } else {
    // Public route, allow access
    next()
  }
})

// Clear setup cache on successful setup
export function clearSetupCache() {
  setupStatusCache = null
  setupStatusCacheTime = 0
}

export default router
