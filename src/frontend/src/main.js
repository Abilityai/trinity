import { createApp } from 'vue'
import { createPinia } from 'pinia'
import axios from 'axios'
import router from './router'
import App from './App.vue'
import './style.css'
import { useAuthStore } from './stores/auth'
import { installConsoleBuffer } from './utils/consoleBuffer'

// #1116: capture recent console errors/warnings from the very start so the
// in-app bug reporter can attach them (scrubbed) to a report. Runs before the
// app mounts so early boot errors are caught too.
installConsoleBuffer()

const app = createApp(App)
const pinia = createPinia()

app.use(pinia)
app.use(router)

// Initialize auth state from localStorage/cookies on app startup
const authStore = useAuthStore()
authStore.initializeAuth()

// Setup axios interceptor to handle token expiration
axios.interceptors.response.use(
  response => response,
  error => {
    // If we get a 401 Unauthorized, token is expired or invalid
    if (error.response?.status === 401) {
      // Get the current route
      const currentPath = router.currentRoute.value.path

      // Don't redirect if already on login or setup page, or when an EXTERNAL
      // client holds a verified-email session on the workspace (#138): that
      // surface owns its own session and handles its own 401, so a stale
      // operator JWT must not bounce a signed-in client to /login.
      //
      // ent#357: an INTERNAL user on the workspace is the opposite case — their
      // workspace session IS the platform session, so an expired JWT must
      // bounce them like anywhere else. The discriminator is the portal token,
      // not the path: same URL, two session kinds.
      const onWorkspace = currentPath.startsWith('/workspace') || currentPath.startsWith('/portal')
      const externalClient = !!localStorage.getItem('trinity.portalToken')
      if (currentPath !== '/login' && currentPath !== '/setup' && currentPath !== '/m'
          && !(onWorkspace && externalClient)) {
        console.log('🔐 Session expired - redirecting to login')

        // Clear auth state
        authStore.logout()

        // Redirect to login
        router.push('/login')
      }
    }
    return Promise.reject(error)
  }
)

app.mount('#app')
