<template>
  <!-- #3541 — the page links at phone width. Below `sm` NavBar's link row is
       not displayed, so this menu button and its sheet are the only route to
       Library, Operations, Settings and Workspace. It renders the SAME
       `buildNavLinks` set as the row (label, active state, badge, pill), so the
       two cannot drift. It is shown only below `sm`, where most visitors are
       on touch, so the button has a pressed state and no hover state (a hover
       tint sticks after a tap). -->
  <button
    type="button"
    data-nav-sheet-trigger
    class="sm:hidden relative inline-flex h-11 w-11 flex-shrink-0 items-center justify-center self-center rounded-lg text-gray-600 dark:text-gray-300 active:bg-gray-100 dark:active:bg-gray-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500"
    :aria-expanded="open"
    :aria-label="triggerLabel"
    @click="open = true"
  >
    <svg class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" aria-hidden="true">
      <path stroke-linecap="round" stroke-linejoin="round" d="M4 6h16M4 12h16M4 18h16" />
    </svg>
    <!-- A pending Operations count stays visible while the sheet is shut.
         Same AA-checked grounds as the row's badge (#2201). -->
    <span
      v-if="badge"
      aria-hidden="true"
      data-nav-sheet-badge
      class="absolute right-0.5 top-0.5 inline-flex min-w-[1.125rem] items-center justify-center px-1 py-0.5 text-[10px] font-bold leading-none text-white rounded-full"
      :class="badge.critical ? 'bg-status-danger-600' : 'bg-status-urgent-700'"
    >{{ badge.text }}</span>
  </button>

  <!-- BaseModal owns Esc, the focus trap, focus return and the scroll lock;
       the classes on it only move the panel to the bottom edge (a sheet: the
       phone idiom, and within thumb reach). The panel scrolls on its own so
       every row stays reachable on a short landscape screen. -->
  <BaseModal
    :model-value="open"
    labelledby="nav-sheet-title"
    class="!items-end !p-0"
    panel-class="w-full max-h-[100dvh] overflow-y-auto rounded-t-lg bg-white dark:bg-gray-800 shadow-xl dark:shadow-gray-900"
    data-nav-sheet
    @close="open = false"
  >
    <div class="flex items-center justify-between px-4 pt-3 pb-1">
      <h2 id="nav-sheet-title" class="text-base font-semibold text-gray-900 dark:text-white">Navigation</h2>
      <button
        type="button"
        class="-mr-2 inline-flex h-11 w-11 items-center justify-center rounded-lg text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500"
        aria-label="Close navigation"
        @click="open = false"
      >
        <svg class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" />
        </svg>
      </button>
    </div>
    <!-- The bottom inset clears the home indicator when the page runs
         edge-to-edge (a standalone PWA, `viewport-fit=cover`); in a normal
         browser tab it is 0 and only the 8px remains. -->
    <nav aria-label="Pages" class="px-2 pb-[calc(env(safe-area-inset-bottom,0px)+0.5rem)]">
      <router-link
        v-for="link in links"
        :key="link.id"
        data-nav-sheet-item
        :to="link.to"
        :target="link.target"
        :rel="link.rel"
        :aria-current="link.active ? 'page' : undefined"
        :class="[
          'flex min-h-[48px] items-center justify-between gap-3 rounded-lg px-3 text-base transition-colors',
          link.active
            ? 'bg-action-primary-50 dark:bg-action-primary-900/30 text-action-primary-700 dark:text-action-primary-300 font-medium'
            : 'text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700'
        ]"
        @click="open = false"
      >
        <span class="flex items-center gap-2">
          {{ link.label }}
          <svg
            v-if="link.target === '_blank'"
            class="h-4 w-4 text-gray-400 dark:text-gray-500"
            fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"
            aria-label="opens in a new tab" role="img"
          >
            <path stroke-linecap="round" stroke-linejoin="round" d="M14 5h5v5M19 5l-8 8M10 5H6a1 1 0 00-1 1v12a1 1 0 001 1h12a1 1 0 001-1v-4" />
          </svg>
        </span>
        <!-- Same AA-checked badge grounds as the row (#2201). -->
        <span
          v-if="link.badge"
          class="inline-flex min-w-[1.5rem] items-center justify-center px-1.5 py-0.5 text-xs font-bold leading-none text-white rounded-full"
          :class="link.badgeCritical ? 'bg-status-danger-600' : 'bg-status-urgent-700'"
        >{{ link.badge }}</span>
        <span
          v-else-if="link.pill"
          class="px-1.5 py-0.5 text-[10px] font-bold leading-none rounded bg-accent-purple-100 text-accent-purple-700 dark:bg-accent-purple-900 dark:text-accent-purple-200"
        >{{ link.pill }}</span>
      </router-link>
    </nav>
  </BaseModal>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import BaseModal from './base/BaseModal.vue'
import { menuBadge } from '../utils/navLinks'

const props = defineProps({
  // `buildNavLinks(...)` output — the row's own link set.
  links: { type: Array, required: true },
})

const open = ref(false)
const badge = computed(() => menuBadge(props.links))
const triggerLabel = computed(() =>
  badge.value ? `Open navigation, ${badge.value.text} pending in Operations` : 'Open navigation'
)

// Any navigation closes the sheet, including Back.
const router = useRouter()
watch(() => router.currentRoute.value?.fullPath, () => { open.value = false })

// Widening past `sm` (a rotation, a resized window) brings the link row back
// and hides this button, so the sheet closes too: left open it would cover the
// page and keep BaseModal's scroll lock with no visible way out.
let wide = null
function onWide(e) { if (e.matches) open.value = false }
onMounted(() => {
  if (typeof window.matchMedia !== 'function') return
  wide = window.matchMedia('(min-width: 640px)')
  wide.addEventListener?.('change', onWide)
})
onBeforeUnmount(() => wide?.removeEventListener?.('change', onWide))
</script>
