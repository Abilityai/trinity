<template>
  <!-- The assistant is docked on the left of every page: a slim rail when
       collapsed, a panel when open. The page is padded to sit beside it, so it
       covers nothing; below the `xl` width the open panel lies over the page
       instead of squeezing it. The panel's width is the person's
       (dragged at its edge); `--assistant-w` carries it here. -->
  <div
    id="app"
    class="min-h-screen bg-gray-100 dark:bg-gray-900 transition-[padding] duration-200 motion-reduce:transition-none"
    :class="[
      showAssistant ? (assistantStore.panelOpen ? 'md:pl-12 xl:pl-[var(--assistant-w,24rem)]' : 'md:pl-12') : '',
      { '!transition-none select-none': assistantStore.resizing },
    ]"
  >
    <!-- AgentDetail is cached across navigation. #2198 re-examined whether it
         still should be, because the stated reason had gone stale: the Terminal
         tab is hidden for all users, `TerminalPanelContent` is imported but
         never rendered, and `terminalRef` is bound to nothing — so there is no
         terminal WebSocket to preserve.

         It stays, for reasons that ARE live and were measured:
           - `e2e/schedules-toggle-scroll.spec.js` names this caching as a
             load-bearing premise; without it two shipped regression tests pass
             even with the fix they guard deleted.
           - `ChatPanel` is `v-show` (so mounted on every tab) and its
             `onUnmounted` calls `closeSSE()` and stops an active voice session.
             Un-caching makes navigating away kill an in-flight chat stream and
             end a live voice call.
           - A link back in that names no `?tab=` (the dashboard, the fleet
             list) would reset the user to Overview; the cached view keeps the
             tab and writes it back to the URL (#2900).
           - It is not even a win on requests: measured on a running agent,
             removing it saved 6 on first load and cost 19 on every revisit
             (27 -> 47), and `learnings.md:118-120` records the cached-revisit
             path as "the common path".
         The duplicate fetches it caused are fixed at their source instead.

         'SystemAgent' matched no component in the codebase — the `/system-agent`
         route redirects to `/agents/trinity-system`, i.e. to AgentDetail — so
         removing it is a no-op, verified by grep. -->
    <router-view v-slot="{ Component }">
      <KeepAlive :include="['AgentDetail']">
        <component :is="Component" />
      </KeepAlive>
    </router-view>

    <!-- Help chat widget (authenticated users only; hidden on standalone
         client-facing surfaces like the portal where it overlaps the composer) -->
    <!-- An entitled admin gets the assistant in the same corner; everyone
         else keeps the help widget unchanged. -->
    <AssistantWidget
      v-if="showAssistant"
      :hide-launcher="helpLauncherHidden"
    />
    <HelpChatWidget
      v-else-if="authStore.isAuthenticated && !route.meta.hideHelpWidget && !cornerUndecided"
      :hide-launcher="helpLauncherHidden"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useAuthStore } from './stores/auth'
import { readStoredToken } from './utils/platformSession'
import { useThemeStore } from './stores/theme'
import { useWebSocket } from './utils/websocket'
import HelpChatWidget from './components/HelpChatWidget.vue'
import AssistantWidget from './components/assistant/AssistantWidget.vue'
import { useEnterpriseStore } from './stores/enterprise'
import { FEATURE_ID as ASSISTANT_FEATURE, useAssistantStore } from './stores/assistant'

const route = useRoute()
const authStore = useAuthStore()
const enterpriseStore = useEnterpriseStore()
const assistantStore = useAssistantStore()
// The role in the store can be edited in localStorage, so it only counts once
// the profile has been read from the server (#2198).
const showAssistant = computed(() =>
  authStore.isAuthenticated &&
  !route.meta.hideHelpWidget &&
  authStore.profileVerified &&
  authStore.user?.role === 'admin' &&
  enterpriseStore.isEntitled(ASSISTANT_FEATURE) &&
  assistantStore.available
)
// Whether the assistant is offered is the instance's to say, so it is asked
// once an admin on an entitled build is known. Until it answers, and whenever
// the answer is no, the help widget stays.
const mayHaveAssistant = computed(() =>
  authStore.isAuthenticated &&
  authStore.profileVerified &&
  authStore.user?.role === 'admin' &&
  enterpriseStore.isEntitled(ASSISTANT_FEATURE)
)
watch(mayHaveAssistant, (may) => {
  if (may && !assistantStore.status && !assistantStore.absent) assistantStore.loadStatus()
}, { immediate: true })
// One button in the corner, never one swapped for another. While it is not yet
// known which this person gets (their profile, the build's entitlements, the
// instance's answer about the assistant), the corner stays empty. A read that
// never answers must not cost anyone the help button, so the wait is bounded.
const CORNER_WAIT_MS = 4000
const cornerWaitOver = ref(false)
let cornerTimer = null
watch(() => authStore.isAuthenticated, (signedIn) => {
  clearTimeout(cornerTimer)
  cornerWaitOver.value = false
  if (signedIn) cornerTimer = setTimeout(() => { cornerWaitOver.value = true }, CORNER_WAIT_MS)
}, { immediate: true })
const cornerUndecided = computed(() =>
  !cornerWaitOver.value && (
    !authStore.profileVerified ||
    !enterpriseStore.featureFlagsLoaded ||
    (mayHaveAssistant.value && !assistantStore.status && !assistantStore.absent && !assistantStore.statusError)
  )
)
// Other parts of the page arrange themselves by it (the getting-started steps
// live in the assistant when there is one, and in the systems rail when not).
watch([showAssistant, cornerUndecided], ([assistant, undecided]) => {
  assistantStore.corner = assistant ? 'assistant' : undecided ? 'undecided' : 'help'
}, { immediate: true })
// #3446: a route may name tabs on which the floating launcher would cover one
// of the page's own controls (the agent Chat composer's Send).
const helpLauncherHidden = computed(() =>
  (route.meta.hideHelpLauncherOnTabs || []).includes(route.query.tab)
)
const themeStore = useThemeStore()
const { followToken } = useWebSocket()

// #3445: the real-time socket follows the platform session, not the page load.
// It used to connect once, here on mount, and only if a token was already
// stored — so a sign-in without a reload left it (and the header dot)
// disconnected, and a sign-out left the previous session's socket open. Every
// sign-in path, a sibling tab's, a sign-out and a session ended elsewhere all
// go through `authStore.token`.
watch(() => authStore.token, (token) => followToken(token), { immediate: true })

onMounted(async () => {
  // Initialize theme immediately to prevent flash
  themeStore.initTheme()

  // Seed the store SYNCHRONOUSLY from storage so router guards keying on
  // `isAuthenticated` are satisfied before `initializeAuth`'s first await lands;
  // `initializeAuth` (dispatched from main.js) restores the user and verifies
  // the profile. This block used to ALSO write
  // `axios.defaults.headers.common['Authorization']` — the second credential
  // source #2791 removes. Axios merges that default into every request BEFORE
  // the interceptor chain runs, so the copy written here won over storage for
  // the life of the tab and the per-request interceptor in `main.js` was inert
  // on every boot with a token. Requests get their credential from
  // `applyRequestCredential` now; nothing writes the default.
  // Setting the token here also opens the real-time socket (the watch above).
  const token = readStoredToken()
  if (token) {
    authStore.token = token
    authStore.isAuthenticated = true
  }
})
</script>
