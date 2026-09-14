import { createApp } from 'vue'
import { createPinia } from 'pinia'
import axios from 'axios'
import router from './router'
import App from './App.vue'
import './style.css'
import { useAuthStore } from './stores/auth'
import { setPlatformSessionLostHandler } from './stores/clientPortal'
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

// #2261 — workspace requests run on their own axios instance now
// (`stores/clientPortal.js`), so the global 401 interceptor below no longer sees
// them. The operator bounce for that surface is registered here, where the router
// and the auth store already live, and the instance's interceptor calls it ONLY
// when the workspace session is the platform one — never for a client's 401 on a
// browser that happens to hold an operator's JWT.
setPlatformSessionLostHandler(() => {
  console.log('🔐 Workspace: platform session expired - redirecting to login')
  authStore.logout()
  router.push('/login')
})

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
      // Who gets bounced is decided by the PLATFORM token, not the portal one
      // (/review I1). Reading the portal token here made the answer depend on
      // timing: `fetchRoster`'s 401 handler calls `signOut()`, which removes it,
      // so a second concurrent 401 saw no portal token and threw an external
      // client onto the operator /login instead of the workspace sign-in form.
      // "Does this browser hold a platform session that just expired?" is the
      // actual question, and it has a stable answer.
      const onWorkspace = currentPath.startsWith('/workspace') || currentPath.startsWith('/portal')
      const internalSession = !!localStorage.getItem('token')
      if (currentPath !== '/login' && currentPath !== '/setup' && currentPath !== '/m'
          && (!onWorkspace || internalSession)) {
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
