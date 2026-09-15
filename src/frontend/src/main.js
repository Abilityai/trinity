import { createApp } from 'vue'
import { createPinia } from 'pinia'
import axios from 'axios'
import router from './router'
import App from './App.vue'
import './style.css'
import { useAuthStore } from './stores/auth'
import { PORTAL_TOKEN_KEY } from './stores/clientPortal'
import {
  notifyPlatformUnauthorized, readStoredToken, sessionLostVerdict,
  setPlatformUnauthorizedHandler, TOKEN_KEY, tokenOfRequest,
} from './utils/platformSession'
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
// #2791 — ONE implementation of "a 401 came back; what does it mean?", shared by
// all three sites that used to answer it differently: this file's global axios
// interceptor, `api.js`'s instance interceptor, and `portalHttp`'s in
// `stores/clientPortal.js`.
//
// The verdict itself is a pure function (`utils/platformSession.js`) so it can be
// tested without a browser; this is the only part that needs the router and the
// store, which is why it lives here and reaches the other two by callback.
function handlePlatformUnauthorized(error) {
  const path = router.currentRoute.value?.path || window.location.pathname
  const verdict = sessionLostVerdict({
    failedToken: tokenOfRequest(error?.config),
    storedToken: readStoredToken(),
    portalTokenPresent: !!localStorage.getItem(PORTAL_TOKEN_KEY),
    path,
  })

  if (verdict === 'ignore') return

  if (verdict === 'stale') {
    // The credential that failed has already been replaced — by a re-login in
    // this browser, in this tab or another. Destroying the session now would
    // delete the NEW token, which is the reported bug: a Workspace tab left open
    // across a logout/login killed the fresh session within one poll.
    console.log('🔐 Session superseded — adopting the current one instead of logging out')
    authStore.adoptStoredSession()
    return
  }

  console.log('🔐 Session expired - redirecting to login')
  // NOT awaited, deliberately. `logout()` clears local state synchronously
  // before its first `await` (#2258's ordering), so the `/login → /` router
  // guard — which keys on `isAuthenticated` — is already satisfied when the push
  // runs. Awaiting would hold the user on a dead page for the length of the
  // server revoke, and a hung revoke would hold them there indefinitely.
  authStore.logout()
  router.push('/login')
}

setPlatformUnauthorizedHandler(handlePlatformUnauthorized)

// #2791 — every bare-`axios` caller gets the CURRENT credential, per request.
//
// There are ~368 `axios.get/post/...` call sites outside `api.js`, and they used
// to be served by `axios.defaults.headers.common['Authorization']` — an
// in-memory copy written once at login. That is the second credential source
// this issue is about: after another tab logged in or out, a tab was half on the
// old session (these callers) and half on the new one (`api.js`, which re-reads
// localStorage per request).
//
// Rebuilding here rather than rewriting 368 sites is what the AC's second half
// allows ("or is provably never read in preference to the store"), and it is the
// stronger of the two: a call site added tomorrow cannot forget to opt in.
//
// An EXPLICIT header on the config wins. Exactly one caller relies on that — the
// logout revoke, which must carry a token storage has already dropped (#2258's
// ordering) — and the other explicit sites pass `authStore.authHeader`, which
// derives from the same place, so "explicit" and "derived" cannot disagree.
axios.interceptors.request.use((config) => {
  const headers = config.headers || {}
  if (!headers.Authorization && !headers.authorization) {
    const token = readStoredToken()
    if (token) headers.Authorization = `Bearer ${token}`
  }
  config.headers = headers
  return config
})

// #2791 — this interceptor no longer carries a predicate of its own. It used to
// duplicate `api.js`'s (`!onWorkspace || internalSession`), and the two drifted
// from `portalHttp`'s third one; the shared verdict now answers for all three.
axios.interceptors.response.use(
  response => response,
  error => {
    if (error.response?.status === 401) notifyPlatformUnauthorized(error)
    return Promise.reject(error)
  }
)

// #2791 — cross-tab sync (AC #2).
//
// `localStorage` is the durable source of the platform credential and the only
// thing another tab can change, so the `storage` event is how a tab learns that
// a sibling logged in or out. Nothing in `src/frontend/src` listened for it
// before, which is why one browser could hold two live opinions about who was
// signed in.
//
// The event does NOT fire in the tab that made the change, so this is purely
// "somebody else did something".
//
// Neither branch navigates. A background tab pushing `/login` is the noise this
// issue reports; the visible tab converges through the router guard and its next
// request, both of which read the state set here.
window.addEventListener('storage', (event) => {
  if (event.storageArea !== localStorage) return
  if (event.key !== null && event.key !== TOKEN_KEY) return
  // `key === null` is a whole-storage clear, which ends the session too.
  if (readStoredToken()) authStore.adoptStoredSession()
  else authStore.applySessionEndedElsewhere()
})

app.mount('#app')
