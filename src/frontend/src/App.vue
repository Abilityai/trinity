<template>
  <div id="app" class="min-h-screen bg-gray-100 dark:bg-gray-900">
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
           - `activeTab` is never URL-synced, so every revisit would reset the
             user to Overview.
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
    <HelpChatWidget v-if="authStore.isAuthenticated && !route.meta.hideHelpWidget" />
  </div>
</template>

<script setup>
import { onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { useAuthStore } from './stores/auth'
import { readStoredToken } from './utils/platformSession'
import { useThemeStore } from './stores/theme'
import { useWebSocket } from './utils/websocket'
import HelpChatWidget from './components/HelpChatWidget.vue'

const route = useRoute()
const authStore = useAuthStore()
const themeStore = useThemeStore()
const { connect } = useWebSocket()

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
  const token = readStoredToken()
  if (token) {
    authStore.token = token
    authStore.isAuthenticated = true
    // Connect to WebSocket for real-time updates
    connect()
  }
})
</script>
