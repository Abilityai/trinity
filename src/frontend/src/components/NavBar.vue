<template>
  <nav class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
      <!-- #1789 — `gap-4` guarantees breathing room between the two clusters at
           the width where they finally meet, so a link clipped by the scroll
           boundary never butts up against the connection dot. -->
      <div class="flex justify-between h-16 gap-4">
        <!-- #1789 — `min-w-0` is load-bearing. A flex item defaults to
             `min-width: auto`, so without it this cluster cannot shrink below
             its min-content width: once logo + links + the right-hand controls
             exceed the `max-w-7xl` cap the two clusters stop compressing and
             overflow INTO each other, which `justify-between` parks in the
             middle of the bar (Connected landing on top of Enterprise PRO). -->
        <div class="flex min-w-0">
          <router-link to="/" class="flex-shrink-0 flex items-center hover:opacity-80 transition-opacity">
            <img src="../assets/trinity-logo.svg" alt="Trinity Logo" class="h-8 w-8 mr-2 dark:hidden" />
            <img src="../assets/trinity-logo-white.svg" alt="Trinity Logo" class="h-8 w-8 mr-2 hidden dark:block" />
            <h1 class="text-xl font-bold text-gray-900 dark:text-white">Trinity</h1>
          </router-link>
          <!-- #1789 — the link row is the elastic side: it scrolls when the
               full set can't fit, so every link stays reachable instead of
               being clipped or overlapping the controls. Gaps tighten below
               `xl` to buy the ~150px that keeps a 7-link entitled build
               scroll-free at the capped container width. -->
          <div class="hidden sm:ml-6 sm:flex sm:space-x-3 xl:space-x-6 min-w-0 overflow-x-auto nav-links-scroll">
            <!-- trinity-enterprise#260 — the Agents entry is gone (the page is
                 now the Dashboard's List mode); Dashboard inherits its
                 agent-detail highlight, so /agents/:name pages light this up. -->
            <router-link
              to="/"
              class="border-transparent text-gray-500 dark:text-gray-400 hover:border-gray-300 dark:hover:border-gray-600 hover:text-gray-700 dark:hover:text-gray-200 inline-flex flex-shrink-0 whitespace-nowrap items-center px-1 pt-1 border-b-2 text-sm font-medium"
              :class="{ 'border-blue-500 dark:border-blue-400 text-gray-900 dark:text-white': $route.path === '/' || isAgentSection }"
            >
              Dashboard
            </router-link>
            <router-link
              to="/library"
              class="border-transparent text-gray-500 dark:text-gray-400 hover:border-gray-300 dark:hover:border-gray-600 hover:text-gray-700 dark:hover:text-gray-200 inline-flex flex-shrink-0 whitespace-nowrap items-center px-1 pt-1 border-b-2 text-sm font-medium"
              :class="{ 'border-blue-500 dark:border-blue-400 text-gray-900 dark:text-white': $route.path.startsWith('/library') }"
            >
              Library
            </router-link>
            <!-- #1109 — single Operations entry replaces the former
                 Health / Ops / Executions links. One unified badge carries
                 the actionable signal (pending operator-queue + notifications);
                 running-execution count lives inside the Executions tab. -->
            <router-link
              to="/operations"
              class="border-transparent text-gray-500 dark:text-gray-400 hover:border-gray-300 dark:hover:border-gray-600 hover:text-gray-700 dark:hover:text-gray-200 inline-flex flex-shrink-0 whitespace-nowrap items-center px-1 pt-1 border-b-2 text-sm font-medium relative"
              :class="{ 'border-blue-500 dark:border-blue-400 text-gray-900 dark:text-white': $route.path === '/operations' }"
            >
              Operations
              <span
                v-if="combinedOpsCount > 0"
                class="ml-1 inline-flex items-center justify-center px-1.5 py-0.5 text-xs font-bold leading-none text-white rounded-full"
                :class="hasCriticalOpsItem ? 'bg-status-danger-500 animate-pulse' : 'bg-status-urgent-500'"
              >
                {{ combinedOpsCount > 99 ? '99+' : combinedOpsCount }}
              </span>
            </router-link>
            <!-- HIDDEN: Processes nav link - Process Engine de-emphasized from top nav (Issue #50) -->
            <!-- REMOVED: Credentials nav link - credentials are now managed per-agent only -->
            <!-- REMOVED: Keys top-level link — MCP key management moved to Settings → MCP Keys tab (#302) -->
            <!--
              Settings is visible to ALL authenticated users (#302) — non-admin users
              see only the MCP Keys tab inside Settings. Admin sees all 5 tabs.
            -->
            <router-link
              to="/settings"
              class="border-transparent text-gray-500 dark:text-gray-400 hover:border-gray-300 dark:hover:border-gray-600 hover:text-gray-700 dark:hover:text-gray-200 inline-flex flex-shrink-0 whitespace-nowrap items-center px-1 pt-1 border-b-2 text-sm font-medium"
              :class="{ 'border-blue-500 dark:border-blue-400 text-gray-900 dark:text-white': $route.path === '/settings' }"
            >
              Settings
            </router-link>
            <!-- #847 Phase 0 — Enterprise catalogue landing. Visible
                 iff ANY enterprise feature is entitled
                 (`hasAnyEnterprise`). The landing lists each enterprise
                 feature as a card with status; non-entitled and
                 Coming-soon features render disabled. OSS-only builds
                 have an empty `enterpriseFeatures` list so this link
                 is hidden entirely. The store's `featureFlagsLoaded`
                 guard means the link doesn't flicker on first paint
                 (loadFeatureFlags fires in onMounted below). -->
            <!-- ent#381: the Sessions entry is gone. Rooms did not go away —
                 the ent#8 substrate is what a multi-agent Workspace chat now
                 runs on (ent#361/#362) — but two nav entries for one job was
                 the overlap this epic exists to remove. `/sessions` redirects
                 into the Workspace. -->
            <!-- Workspace (ent#357) — the persistent entry point that makes it
                 ONE CLICK from the platform. The surface is the same one an
                 external client reaches by email OTP; for a signed-in user the
                 session is simply the platform session, so no sign-in step
                 stands between this link and the view.

                 UNGATED (ent#356): this used to carry
                 `v-if="enterpriseStore.isEntitled('client_portal')"`, correct
                 while the module was entitled. ent#356 moved client_portal into
                 OSS core — main.py mounts it unconditionally and nothing calls
                 register_module('client_portal') any more — so that predicate is
                 now false on every OSS build, and the one-click entry this issue
                 exists to add would never render. It survived only because the
                 enterprise submodule has not yet dropped its registration
                 (trinity-enterprise#374), i.e. it was already relying on a
                 registration that is about to disappear. -->
            <!-- ent#456: a new tab, because the Workspace is a place you go and
                 stay — following it from the console used to throw away whatever
                 you were looking at. `target` is enough: Vue Router's guardEvent
                 declines to intercept a _blank (and a modified) click, so the
                 link still resolves its href and the browser owns the rest.
                 Hand-rolling window.open would take that back. -->
            <router-link
              to="/workspace"
              target="_blank"
              rel="noopener"
              class="border-transparent text-gray-500 dark:text-gray-400 hover:border-gray-300 dark:hover:border-gray-600 hover:text-gray-700 dark:hover:text-gray-200 inline-flex flex-shrink-0 whitespace-nowrap items-center px-1 pt-1 border-b-2 text-sm font-medium"
              :class="{ 'border-blue-500 dark:border-blue-400 text-gray-900 dark:text-white': $route.path.startsWith('/workspace') }"
            >
              Workspace
            </router-link>
            <router-link
              v-if="enterpriseStore.hasAnyEnterprise"
              to="/enterprise"
              class="border-transparent text-gray-500 dark:text-gray-400 hover:border-gray-300 dark:hover:border-gray-600 hover:text-gray-700 dark:hover:text-gray-200 inline-flex flex-shrink-0 whitespace-nowrap items-center px-1 pt-1 border-b-2 text-sm font-medium"
              :class="{ 'border-blue-500 dark:border-blue-400 text-gray-900 dark:text-white': $route.path.startsWith('/enterprise') }"
            >
              Enterprise
              <span class="ml-1 px-1.5 py-0.5 text-[10px] font-bold leading-none rounded bg-purple-100 text-purple-700 dark:bg-purple-900 dark:text-purple-200">PRO</span>
            </router-link>
          </div>
        </div>
        <!-- #1789 — `flex-shrink-0`: these are the bar's controls (docs, theme,
             and the user menu that owns Sign out). They must never be
             compressed or pushed past the viewport edge, so the link row above
             absorbs all the width pressure instead. -->
        <div class="flex flex-shrink-0 items-center space-x-4">
          <!-- WebSocket Status — dot only (#1789). The word cost ~80px of a
               budget the bar does not have at the `max-w-7xl` cap; the state is
               conveyed by colour, a tooltip, and the sr-only label, and a lost
               connection additionally pulses. -->
          <span
            class="flex items-center text-sm text-gray-500 dark:text-gray-400"
            :title="isConnected ? 'Connected' : 'Disconnected'"
          >
            <span
              class="inline-block h-2 w-2 rounded-full"
              :class="isConnected ? 'bg-status-success-400' : 'bg-status-warning-500 animate-pulse'"
            ></span>
            <span class="sr-only">{{ isConnected ? 'Connected' : 'Disconnected' }}</span>
          </span>

          <!-- Build Info Chip (#926) — small muted version label; click opens detail modal.
               Hidden below `xl` (#1789) — it is the widest non-interactive item in
               the bar and the same data is one click away in the Build Info modal
               and on Settings. -->
          <button
            v-if="buildInfo.info.value"
            @click="showBuildInfoModal = true"
            class="hidden xl:block text-xs text-gray-400 dark:text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 font-mono whitespace-nowrap"
            :title="`Click for build info — commit ${buildInfo.info.value.git_commit_short}`"
          >
            v{{ buildInfo.displayVersion.value }}<span
              v-if="buildInfo.info.value.git_commit_short && buildInfo.info.value.git_commit_short !== 'unknown'"
              class="ml-1 opacity-70"
            >· {{ buildInfo.info.value.git_commit_short }}</span>
          </button>

          <!-- Docs link (trinity-enterprise#53) — always-available external
               link to the public documentation site. opens in a new tab. -->
          <a
            href="https://docs.ability.ai"
            target="_blank"
            rel="noopener noreferrer"
            class="p-2 rounded-lg text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-500"
            title="Documentation (opens docs.ability.ai)"
            aria-label="Documentation"
          >
            <!-- Question-mark-in-circle icon -->
            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8.228 9c.549-1.165 2.03-2 3.772-2 2.21 0 4 1.343 4 3 0 1.4-1.278 2.575-3.006 2.907-.542.104-.994.54-.994 1.093m0 3h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
          </a>

          <!-- Theme Toggle Button -->
          <button
            @click="cycleTheme"
            class="p-2 rounded-lg text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-500"
            :title="themeTitle"
          >
            <!-- Sun icon for light mode -->
            <svg v-if="themeStore.theme === 'light'" class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" />
            </svg>
            <!-- Moon icon for dark mode -->
            <svg v-else-if="themeStore.theme === 'dark'" class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" />
            </svg>
            <!-- Computer/System icon for system mode -->
            <svg v-else class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
            </svg>
          </button>

          <!-- User Menu -->
          <div class="relative" ref="userMenuRef">
            <button
              @click="toggleUserMenu"
              class="flex items-center space-x-2 focus:outline-none"
            >
              <!-- User Avatar -->
              <div
                v-if="authStore.userPicture && !avatarError"
                class="w-8 h-8 rounded-full overflow-hidden border-2 border-gray-200 dark:border-gray-600 hover:border-blue-400 dark:hover:border-blue-500 transition-colors"
              >
                <img
                  :src="authStore.userPicture"
                  :alt="authStore.userName"
                  class="w-full h-full object-cover"
                  @error="avatarError = true"
                />
              </div>
              <div
                v-else
                class="w-8 h-8 rounded-full bg-blue-500 text-white flex items-center justify-center text-sm font-medium border-2 border-gray-200 dark:border-gray-600 hover:border-blue-400 dark:hover:border-blue-500 transition-colors"
              >
                {{ authStore.userInitials }}
              </div>
            </button>

            <!-- Dropdown Menu -->
            <div
              v-if="showUserMenu"
              class="absolute right-0 mt-2 w-56 rounded-lg bg-white dark:bg-gray-800 shadow-lg ring-1 ring-black ring-opacity-5 dark:ring-gray-700 py-1 z-50"
            >
              <div class="px-4 py-3 border-b border-gray-100 dark:border-gray-700">
                <p class="text-sm font-medium text-gray-900 dark:text-white truncate">{{ authStore.userName }}</p>
                <p class="text-xs text-gray-500 dark:text-gray-400 truncate">{{ authStore.userEmail }}</p>
              </div>
              <!-- Theme Selector in Menu -->
              <div class="px-4 py-2 border-b border-gray-100 dark:border-gray-700">
                <p class="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-2">Theme</p>
                <div class="flex space-x-1">
                  <button
                    @click="setTheme('light')"
                    class="flex-1 px-2 py-1.5 text-xs rounded-md flex items-center justify-center space-x-1"
                    :class="themeStore.theme === 'light' ? 'bg-blue-100 dark:bg-blue-900 text-blue-700 dark:text-blue-300' : 'text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700'"
                  >
                    <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" />
                    </svg>
                    <span>Light</span>
                  </button>
                  <button
                    @click="setTheme('dark')"
                    class="flex-1 px-2 py-1.5 text-xs rounded-md flex items-center justify-center space-x-1"
                    :class="themeStore.theme === 'dark' ? 'bg-blue-100 dark:bg-blue-900 text-blue-700 dark:text-blue-300' : 'text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700'"
                  >
                    <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" />
                    </svg>
                    <span>Dark</span>
                  </button>
                  <button
                    @click="setTheme('system')"
                    class="flex-1 px-2 py-1.5 text-xs rounded-md flex items-center justify-center space-x-1"
                    :class="themeStore.theme === 'system' ? 'bg-blue-100 dark:bg-blue-900 text-blue-700 dark:text-blue-300' : 'text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700'"
                  >
                    <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
                    </svg>
                    <span>Auto</span>
                  </button>
                </div>
              </div>
              <!-- Documentation link (trinity-enterprise#53) -->
              <a
                href="https://docs.ability.ai"
                target="_blank"
                rel="noopener noreferrer"
                @click="showUserMenu = false"
                class="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center"
              >
                <svg class="w-4 h-4 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8.228 9c.549-1.165 2.03-2 3.772-2 2.21 0 4 1.343 4 3 0 1.4-1.278 2.575-3.006 2.907-.542.104-.994.54-.994 1.093m0 3h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                Documentation
              </a>
              <button
                @click="handleLogout"
                class="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center"
              >
                <svg class="w-4 h-4 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
                </svg>
                Sign out
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Build Info Modal (#926) — click-out to dismiss -->
    <div
      v-if="showBuildInfoModal && buildInfo.info.value"
      class="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50"
      @click.self="showBuildInfoModal = false"
    >
      <div class="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-lg w-full mx-4 p-6">
        <div class="flex justify-between items-start mb-2">
          <h2 class="text-lg font-semibold text-gray-900 dark:text-white">Build Info</h2>
          <button
            @click="showBuildInfoModal = false"
            class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
            aria-label="Close"
          >
            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
        <p class="text-xs text-gray-500 dark:text-gray-400 mb-4">
          Commit, branch, and build date the running platform was built from.
        </p>
        <div
          v-if="buildInfo.isMissing.value"
          class="mb-4 p-3 rounded bg-gray-50 dark:bg-gray-900 text-xs text-gray-600 dark:text-gray-400"
        >
          Build metadata not available — rebuild with
          <code class="font-mono">scripts/deploy/start.sh</code> to populate.
        </div>
        <dl class="space-y-2 text-sm">
          <div class="flex justify-between">
            <dt class="text-gray-500 dark:text-gray-400">Version</dt>
            <dd class="font-mono text-gray-900 dark:text-white">{{ buildInfo.displayVersion.value }}</dd>
          </div>
          <div class="flex justify-between">
            <dt class="text-gray-500 dark:text-gray-400">Branch</dt>
            <dd class="font-mono text-gray-900 dark:text-white">{{ buildInfo.info.value.git_branch }}</dd>
          </div>
          <div class="flex justify-between">
            <dt class="text-gray-500 dark:text-gray-400">Commit</dt>
            <dd class="font-mono text-gray-900 dark:text-white text-right break-all">
              <span>{{ buildInfo.info.value.git_commit_short }}</span>
              <div class="text-xs opacity-60">{{ buildInfo.info.value.git_commit }}</div>
            </dd>
          </div>
          <div class="border-t border-gray-200 dark:border-gray-700 pt-2">
            <dt class="text-gray-500 dark:text-gray-400 mb-1">Commit subject</dt>
            <dd class="text-gray-900 dark:text-white break-words">{{ buildInfo.info.value.git_commit_subject }}</dd>
          </div>
          <div class="flex justify-between">
            <dt class="text-gray-500 dark:text-gray-400">Commit timestamp</dt>
            <dd class="font-mono text-gray-900 dark:text-white text-xs">{{ buildInfo.info.value.git_commit_timestamp }}</dd>
          </div>
          <div class="flex justify-between">
            <dt class="text-gray-500 dark:text-gray-400">Build date</dt>
            <dd class="font-mono text-gray-900 dark:text-white text-xs">{{ buildInfo.info.value.build_date }}</dd>
          </div>
        </dl>
      </div>
    </div>
  </nav>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { useThemeStore } from '../stores/theme'
import { useNotificationsStore } from '../stores/notifications'
import { useOperatorQueueStore } from '../stores/operatorQueue'
import { useEnterpriseStore } from '../stores/enterprise'
import { useWebSocket } from '../utils/websocket'
import { useBuildInfo } from '../composables/useBuildInfo'

const router = useRouter()
const authStore = useAuthStore()
const themeStore = useThemeStore()
const notificationsStore = useNotificationsStore()
const operatorQueueStore = useOperatorQueueStore()
// #847 Phase 0 — feature-flags load is fired on mount below; the
// `Enterprise` nav link template is `v-if="enterpriseStore.hasAnyEnterprise"`.
const enterpriseStore = useEnterpriseStore()
const { isConnected } = useWebSocket()

// #926: cached fetch of /api/version (singleton across NavBar + Settings)
const buildInfo = useBuildInfo()
const showBuildInfoModal = ref(false)

// #2198: read the role from the auth store instead of issuing a second
// GET /api/users/me — `auth.js::fetchUserProfile` already merges the identical
// response into `authStore.user`, and it runs on both session restore and
// admin login. In-repo precedent: `MonitoringPanel.vue:278` reads the store
// "rather than a duplicate /api/users/me round-trip" (#1109).
//
// `profileVerified` is required, not decorative. `initializeAuth()` restores
// `user` — role included — synchronously from localStorage, which is
// user-editable, so `user?.role === 'admin'` alone would fail OPEN on a forged
// value. Requiring a successful server fetch keeps exactly today's posture,
// where this gate only ever reflected a real response.
//
// A computed, never a read-once: the store reports `user` before
// /api/users/me lands (see Library.vue:474), so the nav must become admin
// reactively when it arrives.
const isAdmin = computed(
  () => authStore.profileVerified && authStore.user?.role === 'admin'
)

// Check if currently in agent section (for highlighting nav)
const route = router.currentRoute
const isAgentSection = computed(() => {
  const path = route.value.path
  return path.startsWith('/agents')
})

// Combined Ops badge counts
const combinedOpsCount = computed(() =>
  operatorQueueStore.pendingCount + notificationsStore.pendingCount
)

const hasCriticalOpsItem = computed(() =>
  operatorQueueStore.criticalCount > 0 || notificationsStore.hasUrgentPending
)

// Theme management
const themeTitle = computed(() => {
  const titles = {
    light: 'Light mode (click to switch)',
    dark: 'Dark mode (click to switch)',
    system: 'System theme (click to switch)'
  }
  return titles[themeStore.theme]
})

const cycleTheme = () => {
  themeStore.toggleTheme()
}

const setTheme = (theme) => {
  themeStore.setTheme(theme)
}

// User menu state
const showUserMenu = ref(false)
const userMenuRef = ref(null)
const avatarError = ref(false)

onMounted(() => {
  // Add click outside listener
  document.addEventListener('click', handleClickOutside)

  // Start polling for notifications
  notificationsStore.startPolling(60000)

  // #926: kick off the cached build-info fetch — failures are non-fatal
  // (chip is hidden if fetch fails; e.g., unauthenticated brief window).
  buildInfo.load().catch(() => {})

  // #847 Phase 0 — load enterprise entitlements. Fires once per page
  // load (the store gates on `featureFlagsLoaded`). The Enterprise nav
  // link is hidden until this resolves.
  enterpriseStore.loadFeatureFlags()
})

onUnmounted(() => {
  document.removeEventListener('click', handleClickOutside)
  notificationsStore.stopPolling()
})

const toggleUserMenu = () => {
  showUserMenu.value = !showUserMenu.value
}

const handleClickOutside = (event) => {
  if (userMenuRef.value && !userMenuRef.value.contains(event.target)) {
    showUserMenu.value = false
  }
}

const handleLogout = () => {
  showUserMenu.value = false

  // Clear local auth state
  authStore.logout()

  // Redirect to login
  router.push('/login')
}
</script>

<style scoped>
/*
 * #1789 — the nav link row is an `overflow-x-auto` container so an
 * over-wide link set scrolls instead of overlapping the controls. A
 * classic scrollbar inside a 64px bar would eat vertical space and sit
 * under the active-link underline, so the gutter is suppressed. The row
 * remains scrollable by wheel, trackpad, touch, and keyboard focus
 * (focusing an off-screen link scrolls it into view) — the affordance is
 * hidden, not the capability.
 */
.nav-links-scroll {
  scrollbar-width: none; /* Firefox */
  -ms-overflow-style: none; /* legacy Edge */
}

.nav-links-scroll::-webkit-scrollbar {
  display: none; /* Chromium, WebKit */
}
</style>
