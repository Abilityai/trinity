import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const routes = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('../views/Login.vue'),
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

// Navigation guard
router.beforeEach(async (to, from, next) => {
  const authStore = useAuthStore()

  // Wait for auth initialization to complete
  if (authStore.isLoading) {
    // Give it a moment to initialize
    await new Promise(resolve => setTimeout(resolve, 100))
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

export default router
