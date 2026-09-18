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
        <div class="flex min-w-0 flex-1">
          <router-link to="/" class="flex-shrink-0 flex items-center hover:opacity-80 transition-opacity">
            <img src="../assets/trinity-logo.svg" alt="Trinity Logo" class="h-8 w-8 mr-2 dark:hidden" />
            <img src="../assets/trinity-logo-white.svg" alt="Trinity Logo" class="h-8 w-8 mr-2 hidden dark:block" />
            <h1 class="text-xl font-bold text-gray-900 dark:text-white">Trinity</h1>
          </router-link>
          <!-- #1925 — priority+ nav, replacing the #1789 hidden-scrollbar
               scroller. That row kept every link *reachable* but gave no signal
               that more navigation existed: the scrollbar was suppressed inside
               the 64px bar, so an overflowed link was invisible and
               undiscoverable. Here the links that fit render inline and the
               remainder collapse into a counted "N more ▾" disclosure, the same
               measured split `OverflowTabs` performs — the fit arithmetic is
               literally shared (`utils/overflowFit.js`), not re-derived.
               `OverflowTabs` itself cannot be mounted here: its items are
               `<button>`s that emit a selection, and these are `<router-link>`s
               that must keep a real href (middle-click, Workspace's _blank,
               per-link active predicates).

               `min-w-0` stays load-bearing for the same reason as #1789 — a
               flex item defaults to `min-width: auto`, so without it the
               cluster cannot shrink and overflows INTO the right-hand
               controls. `flex-1` here and on the parent cluster is what this
               strip adds, and it decides the measurement: the old row sized
               itself to its content and simply scrolled, whereas a row that
               COLLAPSES has to be told how much space it actually has. Without
               it `clientWidth` reports the row's own min-content — the inner
               nav is `overflow-hidden`, so that is near zero — and the strip
               hides four links on a 1440px display. -->
          <div ref="linksEl" class="hidden sm:ml-6 sm:flex min-w-0 flex-1 relative">
            <nav class="flex items-stretch gap-3 xl:gap-6 min-w-0 overflow-hidden">
              <router-link
                v-for="link in inlineLinks"
                :key="link.id"
                :to="link.to"
                :target="link.target"
                :rel="link.rel"
                :class="[
                  'inline-flex flex-shrink-0 whitespace-nowrap items-center px-1 pt-1 border-b-2 text-sm font-medium transition-colors',
                  link.active
                    ? 'border-action-primary-500 text-gray-900 dark:text-white'
                    : 'border-transparent text-gray-500 dark:text-gray-400 hover:border-gray-300 dark:hover:border-gray-600 hover:text-gray-700 dark:hover:text-gray-200'
                ]"
              >
                {{ link.label }}
                <!-- #2201 — white ink needs a darker ground than the 500 solids:
                     white on urgent-500 measured 2.80:1 and on danger-500
                     3.76:1, both below AA. urgent-700 is 5.18:1 and danger-600
                     4.83:1. The badge is small, high-salience and carries a
                     count someone is meant to read, so AA-normal is the bar —
                     not the 3:1 large-text allowance. -->
                <span
                  v-if="link.badge"
                  class="ml-1 inline-flex items-center justify-center px-1.5 py-0.5 text-xs font-bold leading-none text-white rounded-full"
                  :class="link.badgeCritical ? 'bg-status-danger-600 animate-pulse' : 'bg-status-urgent-700'"
                >{{ link.badge }}</span>
                <span
                  v-if="link.pill"
                  class="ml-1 px-1.5 py-0.5 text-[10px] font-bold leading-none rounded bg-accent-purple-100 text-accent-purple-700 dark:bg-accent-purple-900 dark:text-accent-purple-200"
                >{{ link.pill }}</span>
              </router-link>

              <!-- Counted overflow trigger. Reflects active state when the
                   current route's link is one of the hidden ones, so the bar
                   never reads as "nowhere". -->
              <button
                v-if="hasNavOverflow"
                ref="moreBtnEl"
                type="button"
                data-nav-overflow-trigger
                @click="toggleNavMenu"
                @keydown="onNavTriggerKeydown"
                :aria-expanded="navMenuOpen"
                aria-controls="nav-overflow-menu"
                :class="[
                  'inline-flex flex-shrink-0 whitespace-nowrap items-center gap-1 px-1 pt-1 border-b-2 text-sm font-medium transition-colors',
                  navActiveInOverflow
                    ? 'border-action-primary-500 text-gray-900 dark:text-white'
                    : 'border-transparent text-gray-500 dark:text-gray-400 hover:border-gray-300 dark:hover:border-gray-600 hover:text-gray-700 dark:hover:text-gray-200'
                ]"
              >
                {{ navMoreText }}
                <span
                  v-if="overflowNavBadgeCount > 0"
                  class="inline-flex items-center justify-center px-1.5 py-0.5 text-xs font-bold leading-none text-white rounded-full"
                  :class="overflowNavBadgeCritical ? 'bg-status-danger-600 animate-pulse' : 'bg-status-urgent-700'"
                >{{ overflowNavBadgeCount > 99 ? '99+' : overflowNavBadgeCount }}</span>
                <svg
                  class="w-3.5 h-3.5 transition-transform"
                  :class="navMenuOpen ? 'rotate-180' : ''"
                  fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" aria-hidden="true"
                >
                  <path stroke-linecap="round" stroke-linejoin="round" d="M19 9l-7 7-7-7" />
                </svg>
              </button>
            </nav>

            <!-- Disclosure panel — a sibling of the nav so the row's
                 `overflow-hidden` cannot clip it. Plain links, not a
                 role="menu": Tab traverses, Escape closes and returns focus,
                 outside-pointerdown closes (the OverflowTabs contract). -->
            <div
              v-if="navMenuOpen && hasNavOverflow"
              id="nav-overflow-menu"
              ref="navMenuEl"
              data-nav-overflow-menu
              class="absolute left-0 top-full z-30 mt-px min-w-[12rem] py-1 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-md shadow-lg dark:shadow-gray-900"
              @keydown="onNavTriggerKeydown"
            >
              <router-link
                v-for="link in overflowLinks"
                :key="`o-${link.id}`"
                data-nav-menu-item
                :to="link.to"
                :target="link.target"
                :rel="link.rel"
                @click="closeNavMenu()"
                :class="[
                  'w-full px-4 py-2 text-sm transition-colors flex items-center justify-between gap-2',
                  link.active
                    ? 'bg-action-primary-50 dark:bg-action-primary-900/30 text-action-primary-700 dark:text-action-primary-300 font-medium'
                    : 'text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700'
                ]"
              >
                <span>{{ link.label }}</span>
                <span
                  v-if="link.badge"
                  class="inline-flex items-center justify-center px-1.5 py-0.5 text-xs font-bold leading-none text-white rounded-full"
                  :class="link.badgeCritical ? 'bg-status-danger-600' : 'bg-status-urgent-700'"
                >{{ link.badge }}</span>
                <span
                  v-else-if="link.pill"
                  class="px-1.5 py-0.5 text-[10px] font-bold leading-none rounded bg-accent-purple-100 text-accent-purple-700 dark:bg-accent-purple-900 dark:text-accent-purple-200"
                >{{ link.pill }}</span>
              </router-link>
            </div>

            <!-- Hidden zero-layout mirror row: measures EVERY link at its real
                 width, badge and pill included, plus a worst-case trigger, so a
                 link that currently lives in the menu is still measurable.
                 `visibility: hidden` keeps the boxes measurable where
                 `display: none` would report 0, and the 0x0 clipped wrapper
                 contributes no layout. Anything the visible row draws and this
                 row does not is a link measured narrower than it renders, i.e.
                 a strip that overflows one link too late. -->
            <div
              aria-hidden="true"
              class="pointer-events-none"
              style="position: absolute; top: 0; left: 0; width: 0; height: 0; overflow: hidden; visibility: hidden;"
            >
              <nav ref="measureNavEl" class="flex items-stretch gap-3 xl:gap-6" style="width: max-content;">
                <span
                  v-for="link in navLinks"
                  :key="`m-${link.id}`"
                  data-measure-nav-link
                  class="inline-flex whitespace-nowrap items-center px-1 pt-1 border-b-2 text-sm font-medium"
                >
                  {{ link.label }}
                  <span
                    v-if="link.badge"
                    class="ml-1 inline-flex items-center justify-center px-1.5 py-0.5 text-xs font-bold leading-none rounded-full"
                  >{{ link.badge }}</span>
                  <span
                    v-if="link.pill"
                    class="ml-1 px-1.5 py-0.5 text-[10px] font-bold leading-none rounded"
                  >{{ link.pill }}</span>
                </span>
                <span
                  ref="measureMoreEl"
                  class="inline-flex whitespace-nowrap items-center gap-1 px-1 pt-1 border-b-2 text-sm font-medium"
                >
                  {{ navMoreMeasureText }}
                  <span class="inline-flex items-center justify-center px-1.5 py-0.5 text-xs font-bold leading-none rounded-full">99+</span>
                  <svg class="w-3.5 h-3.5" viewBox="0 0 24 24" aria-hidden="true"><path d="M19 9l-7 7-7-7" /></svg>
                </span>
              </nav>
            </div>
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
                <!-- ent#625: the same picker the Workspace switch uses — one
                     primitive, so the two cannot drift. -->
                <ThemeChoice :theme="themeStore.theme" aria-label="Theme" @select="setTheme" />
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
    <!-- #1923: overlay, Esc, focus trap and focus return come from BaseModal. -->
    <BaseModal
      :model-value="showBuildInfoModal && !!buildInfo.info.value"
      panel-class="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-lg w-full mx-4 p-6"
      aria-label="Build Info"
      @close="showBuildInfoModal = false"
    >
      <div>
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
    </BaseModal>
  </nav>
</template>

<script setup>
import BaseModal from './base/BaseModal.vue'
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { useThemeStore } from '../stores/theme'
import ThemeChoice from './base/ThemeChoice.vue'
import { useNotificationsStore } from '../stores/notifications'
import { useOperatorQueueStore } from '../stores/operatorQueue'
import { useEnterpriseStore } from '../stores/enterprise'
import { useWebSocket } from '../utils/websocket'
import { useBuildInfo } from '../composables/useBuildInfo'
import { buildNavLinks, moreLabel } from '../utils/navLinks'
import { computeInlineCount } from '../utils/overflowFit'

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

const route = router.currentRoute

// Combined Ops badge counts
const combinedOpsCount = computed(() =>
  operatorQueueStore.pendingCount + notificationsStore.pendingCount
)

const hasCriticalOpsItem = computed(() =>
  operatorQueueStore.criticalCount > 0 || notificationsStore.hasUrgentPending
)

// --- #1925 priority+ nav -----------------------------------------------------
// The link SET and its active/badge rules are pure (`utils/navLinks.js`) and the
// fit arithmetic is the tab primitive's own (`utils/overflowFit.js`); what lives
// here is only the DOM half — measure the mirror row, observe the container.

const navLinks = computed(() =>
  buildNavLinks({
    path: route.value.path,
    hasAnyEnterprise: enterpriseStore.hasAnyEnterprise,
    opsCount: combinedOpsCount.value,
    opsCritical: hasCriticalOpsItem.value,
  })
)

const linksEl = ref(null)        // width-driven container (ResizeObserver target)
const measureNavEl = ref(null)   // hidden mirror row
const measureMoreEl = ref(null)  // hidden worst-case trigger
const moreBtnEl = ref(null)      // visible trigger (focus return)
const navMenuEl = ref(null)      // disclosure panel

const linkWidths = ref([])
const navMoreWidth = ref(0)
const navGap = ref(0)
const navContainerWidth = ref(0)
// All-inline before the first measure, so the fits-everything case is correct on
// first paint with no collapse/snap.
const navInlineCount = ref(Number.POSITIVE_INFINITY)
const navMenuOpen = ref(false)

let navRo = null
let navRaf = null
let navLastWidth = -1

const inlineLinks = computed(() => navLinks.value.slice(0, navInlineCount.value))
const overflowLinks = computed(() => navLinks.value.slice(navInlineCount.value))
const hasNavOverflow = computed(() => overflowLinks.value.length > 0)
const navActiveInOverflow = computed(() => overflowLinks.value.some((l) => l.active))
const navMoreText = computed(() => moreLabel(overflowLinks.value.length))
// The mirror measures the WIDEST trigger this strip can ever need (every link
// hidden, worst-case badge), so a count or badge that grows never reflows the
// fit decision and re-hides a link mid-session.
const navMoreMeasureText = computed(() => moreLabel(navLinks.value.length))
// A badge that scrolls into the menu must not take its signal with it — the
// whole point of the old row's failure was that a hidden link was undiscoverable.
const overflowNavBadgeCount = computed(() =>
  overflowLinks.value.some((l) => l.badge) ? combinedOpsCount.value : 0
)
const overflowNavBadgeCritical = computed(() =>
  overflowLinks.value.some((l) => l.badgeCritical)
)

// Re-measure when the link set OR any label/badge/pill changes — widths shift.
// `flush: 'post'` runs after the mirror row has rendered the new content.
const navSignature = computed(() =>
  navLinks.value.map((l) => `${l.id}:${l.label}:${l.badge ?? ''}:${l.pill ?? ''}`).join('|')
  + `#${navMoreMeasureText.value}`
)
watch(navSignature, () => measureNav(), { flush: 'post' })

function measureNav() {
  const nav = measureNavEl.value
  if (!nav) return
  linkWidths.value = Array.from(nav.querySelectorAll('[data-measure-nav-link]'))
    .map((el) => el.getBoundingClientRect().width)
  navMoreWidth.value = measureMoreEl.value
    ? measureMoreEl.value.getBoundingClientRect().width
    : 96
  // `gap-3 xl:gap-6` is a real flex gap, not padding, so it is invisible to the
  // per-item rects — read it off the mirror (which carries the same classes) and
  // hand it to the fit rule. A gap-blind sum hides ~120px on a six-link row.
  navGap.value = parseFloat(getComputedStyle(nav).columnGap) || 0
  recomputeNav()
}

function recomputeNav() {
  if (linkWidths.value.length !== navLinks.value.length) {
    navInlineCount.value = navLinks.value.length
    return
  }
  navInlineCount.value = computeInlineCount({
    containerWidth: navContainerWidth.value,
    itemWidths: linkWidths.value,
    moreWidth: navMoreWidth.value,
    gap: navGap.value,
  })
}

function onNavResize() {
  if (navRaf != null) return
  navRaf = requestAnimationFrame(() => {
    navRaf = null
    const w = linksEl.value ? linksEl.value.clientWidth : 0
    if (w === navLastWidth) return // width-diff guard: ignore height-only jitter
    navLastWidth = w
    navContainerWidth.value = w
    recomputeNav()
  })
}

function openNavMenu() {
  navMenuOpen.value = true
  document.addEventListener('pointerdown', onNavPointerDown)
  nextTick(() => {
    navMenuEl.value?.querySelector('[data-nav-menu-item]')?.focus()
  })
}

function closeNavMenu(returnFocus = false) {
  if (!navMenuOpen.value) return
  navMenuOpen.value = false
  document.removeEventListener('pointerdown', onNavPointerDown)
  if (returnFocus) moreBtnEl.value?.focus()
}

function toggleNavMenu() {
  navMenuOpen.value ? closeNavMenu() : openNavMenu()
}

function onNavPointerDown(e) {
  if (linksEl.value && !linksEl.value.contains(e.target)) closeNavMenu()
}

function onNavTriggerKeydown(e) {
  if (e.key === 'Escape') closeNavMenu(true)
}

// A route change can leave the panel open over the page it navigated away from
// — and a _blank link (Workspace) does not even unmount this component.
watch(() => route.value.fullPath, () => closeNavMenu())

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

  // #1925 — measure the nav strip once mounted, then on every container resize.
  navRo = new ResizeObserver(onNavResize)
  if (linksEl.value) navRo.observe(linksEl.value)
  nextTick(() => {
    navLastWidth = linksEl.value ? linksEl.value.clientWidth : 0
    navContainerWidth.value = navLastWidth
    measureNav()
  })
  // A font swap changes intrinsic text widths but does NOT resize the container,
  // so the ResizeObserver never fires — re-measure explicitly once fonts load.
  document.fonts?.ready?.then(() => {
    if (linksEl.value) {
      navLastWidth = linksEl.value.clientWidth
      navContainerWidth.value = navLastWidth
      measureNav()
    }
  })

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
  document.removeEventListener('pointerdown', onNavPointerDown)
  if (navRo) navRo.disconnect()
  if (navRaf != null) cancelAnimationFrame(navRaf)
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
