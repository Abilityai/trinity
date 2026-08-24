<!--
  Admin sign-in email nudge (#2381).

  Replaces a capture point the same change removed. The first-run wizard asked
  for an admin email, but it only renders on an install with no admin account —
  and #2381 makes every install that boots with ADMIN_PASSWORD set (mandatory in
  prod compose, always present after start.sh) provision its admin at boot. Those
  operators would otherwise never be asked for an email at all.

  Binding one here is strictly better than the wizard did it: the wizard is
  unauthenticated, so on a hosted or unattended install its "admin email" could
  be typed by whoever loaded the page first. This asks the person actually signed
  in as admin.

  Derivation-only, like ActivationChecklist (ent#238): nothing is tracked beyond
  the dismissal, so the nudge disappears the moment an email exists — including
  when it was bound from another tab or another device. Gates nothing.
-->
<template>
  <div
    v-if="visible"
    data-testid="admin-email-nudge"
    class="mx-4 mt-3 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 shadow-sm"
  >
    <div class="flex items-start justify-between gap-3 px-4 py-3">
      <div class="min-w-0">
        <h3 class="text-sm font-medium text-gray-900 dark:text-gray-100">
          Add a sign-in email
        </h3>
        <p class="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
          You sign in as
          <code class="px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-750 text-gray-700 dark:text-gray-300">admin</code>
          with the password from your deployment configuration. Binding an email
          lets you sign in with that instead.
        </p>
      </div>

      <div class="flex flex-none items-center gap-2">
        <BaseButton size="sm" variant="primary" @click="goToSettings">
          Add email
        </BaseButton>
        <button
          @click="dismiss"
          data-testid="admin-email-nudge-dismiss"
          class="p-1 rounded text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
          title="Dismiss"
          aria-label="Dismiss the sign-in email prompt"
        >
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import BaseButton from '@/components/base/BaseButton.vue'

const DISMISS_KEY = 'trinity-admin-email-nudge-dismissed'

const router = useRouter()
const authStore = useAuthStore()

// localStorage can throw outright (private windows, blocked site data), so every
// read and write is guarded and a failure simply means "not dismissed".
const dismissed = ref(readDismissed())

function readDismissed() {
  try {
    return localStorage.getItem(DISMISS_KEY) === 'true'
  } catch (e) {
    return false
  }
}

// `profileVerified` (#2198) is load-bearing, not defensive: `userRole` falls back
// to 'user' and `userEmail` to null until /api/users/me lands, so without it this
// would flash for a non-admin on every page load and then vanish.
const visible = computed(() =>
  authStore.profileVerified &&
  authStore.userRole === 'admin' &&
  !authStore.userEmail &&
  !dismissed.value
)

function dismiss() {
  dismissed.value = true
  try {
    localStorage.setItem(DISMISS_KEY, 'true')
  } catch (e) {
    // Dismissal simply does not persist across reloads. Not worth surfacing.
  }
}

function goToSettings() {
  router.push({ path: '/settings', query: { tab: 'general' } })
}
</script>
