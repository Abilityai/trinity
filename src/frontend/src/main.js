import { createApp } from 'vue'
import { createPinia } from 'pinia'
import axios from 'axios'
import router from './router'
import App from './App.vue'
import './style.css'
import { useAuthStore } from './stores/auth'
import { useClientPortalStore } from './stores/clientPortal'
import {
  applyRequestCredential, notifyPlatformUnauthorized, reactToPlatformUnauthorized,
  reactToStorageEvent, setPlatformUnauthorizedHandler,
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
// #2791 — ONE implementation of "a 401 came back; what does it mean, and what do
// we do?", shared by all three sites that used to answer it differently: this
// file's global axios interceptor, `api.js`'s instance interceptor, and
// `portalHttp`'s in `stores/clientPortal.js`.
//
// Both the verdict and the reaction live in `utils/platformSession.js` as
// functions of their collaborators, so they are executed by unit tests with
// fakes; this file only supplies the real ones (router, store) and is asserted
// by source guards for the wiring, nothing more.
function handlePlatformUnauthorized(error) {
  // Which client session, if any, owns THIS TAB — read from the per-tab store,
  // not from shared localStorage (#2791 review W1): a client signing in in
  // another tab writes the shared key, and an operator on the Workspace here
  // would otherwise be told to `ignore` an expired JWT and stranded on a dead
  // page with no login prompt. The portalHttp site gates on the same store.
  let portalTokenPresent = false
  try {
    portalTokenPresent = !!useClientPortalStore().portalToken
  } catch {
    /* Pinia not active yet — no client session can own a tab that has no store */
  }
  const { navigation } = reactToPlatformUnauthorized(error, {
    path: router.currentRoute.value?.path || window.location.pathname,
    portalTokenPresent,
    adoptStoredSession: () => authStore.adoptStoredSession(),
    logout: () => authStore.logout(),
    goToLogin: () => router.push('/login'),
  })
  // RETURNED, so `notifyPlatformUnauthorized` can absorb a rejected redundant
  // navigation. The first version of this handler dropped the promise on the
  // floor and the absorber never engaged (review C4).
  return navigation
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
// `applyRequestCredential` is where the rule lives; nothing may write the
// defaults copy (a tree-wide source guard), because axios merges defaults into
// the config BEFORE this runs and they would win over storage for the whole tab.
axios.interceptors.request.use((config) => applyRequestCredential(config))

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

// #2791 — cross-tab sync (AC #2). `localStorage` is the durable source of the
// platform credential and the only thing another tab can change, so the
// `storage` event is how a tab learns that a sibling logged in or out. Nothing in
// `src/frontend/src` listened for it before, which is why one browser could hold
// two live opinions about who was signed in. The rule is `reactToStorageEvent`.
window.addEventListener('storage', (event) => {
  reactToStorageEvent(event, {
    storage: localStorage,
    adoptStoredSession: () => authStore.adoptStoredSession(),
    applySessionEndedElsewhere: () => authStore.applySessionEndedElsewhere(),
  })
})

app.mount('#app')
