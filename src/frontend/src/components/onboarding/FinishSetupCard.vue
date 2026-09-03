<!--
  Finish setup (ent#437) — ONE post-login card for the admin asks the first-run
  wizard can no longer carry, one section per open item:

    1. Sign-in email (#2381). Moved verbatim from AdminEmailNudge.vue: the wizard
       only renders on an install with no admin account, and every
       ADMIN_PASSWORD-provisioned operator would otherwise never be asked.
    2. Usage sharing (ent#437). The wizard's own ask (ent#12) only renders on a
       zero-agent install, which first-run seeding made permanently false, and
       #2385 removed the welcome form on every pre-provisioned-admin install. So
       the consent ask lives here — reachable, prominent, and snooze-first:
       "Not now" is a 14-day per-browser snooze, "Don't ask again" and consent
       write the server marker, and a WARM variant returns once per browser after
       the install's first successful autonomous run.

  One chassis rather than a fifth stacked nudge. Every decision lives in
  `telemetryConsent.js` (pure, spec'd); this file only dispatches over it.
  Section 2 decides from the feature-flags document the page already awaits and
  calls the admin status route only when it will actually render; the payload
  preview loads on expand. Steady-state cost on a Dashboard load: zero.
-->
<template>
  <div v-if="visible" data-testid="finish-setup-card" class="mx-4 mt-3">
    <BaseCard flush>
      <div class="flex items-center justify-between gap-3 px-4 pt-3 pb-1">
        <h3 class="text-sm font-[550] text-gray-900 dark:text-gray-100">Finish setup</h3>
        <span class="text-[12.5px] tabular-nums text-gray-500 dark:text-gray-400">
          {{ openCount }} {{ openCount === 1 ? 'item' : 'items' }}
        </span>
      </div>

      <!-- Section 1: sign-in email (#2381) -->
      <section
        v-if="emailVisible"
        data-testid="admin-email-nudge"
        class="border-t border-gray-200 dark:border-gray-750 px-4 py-3"
      >
        <div class="flex items-start justify-between gap-3">
          <div class="min-w-0">
            <h4 class="text-sm font-[550] text-gray-900 dark:text-gray-100">Add a sign-in email</h4>
            <p class="mt-0.5 text-[12.5px] leading-[1.5] text-gray-500 dark:text-gray-400">
              You sign in as
              <code class="px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-750 text-gray-700 dark:text-gray-300">admin</code>
              with the password from your deployment configuration. Binding an email
              lets you sign in with that instead.
            </p>
          </div>
          <div class="flex flex-none items-center gap-2">
            <BaseButton size="sm" variant="secondary" @click="goToSettings">Add email</BaseButton>
            <BaseButton
              variant="ghost"
              size="sm"
              data-testid="admin-email-nudge-dismiss"
              aria-label="Dismiss the sign-in email prompt"
              title="Dismiss"
              @click="dismissEmail"
            >
              <svg class="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </BaseButton>
          </div>
        </div>
      </section>

      <!-- Section 2: usage sharing (ent#437) -->
      <section
        v-if="consentVisible"
        data-testid="telemetry-consent"
        class="border-t border-gray-200 dark:border-gray-750 px-4 py-3"
      >
        <div class="flex flex-wrap items-center gap-2">
          <h4 class="text-sm font-[550] text-gray-900 dark:text-gray-100">{{ copy.title }}</h4>
          <BaseBadge variant="neutral">Off by default</BaseBadge>
        </div>
        <p class="mt-1 text-sm text-gray-600 dark:text-gray-300">{{ copy.lead }}</p>
        <p class="mt-1 text-[12.5px] leading-[1.5] text-gray-500 dark:text-gray-400">
          {{ CONSENT_COPY.shared.detail }}
        </p>

        <!-- The exact payload, loaded on expand — never on a Dashboard load. -->
        <details class="mt-2 rounded-md border border-gray-200 dark:border-gray-750" @toggle="onPreviewToggle">
          <summary class="cursor-pointer px-3 py-1.5 text-[12.5px] font-[550] text-gray-700 dark:text-gray-300">
            {{ CONSENT_COPY.shared.previewSummary }}
          </summary>
          <div class="px-3 pb-3 pt-1">
            <LoadFailed
              v-if="previewView.state === 'failed'"
              title="Couldn't build the preview"
              :detail="store.error"
              retryable
              @retry="loadPreview"
            />
            <ScanlineReveal v-else :loading="previewView.state === 'loading'" :reveal="previewView.state === 'ready'">
              <pre class="max-h-64 overflow-auto rounded bg-gray-50 dark:bg-gray-900 p-2 text-xs text-gray-700 dark:text-gray-300"><code>{{ prettyPreview }}</code></pre>
            </ScanlineReveal>
          </div>
        </details>

        <InlineError
          v-if="actionError"
          class="mt-2"
          :message="actionError"
          @dismiss="actionError = ''"
        />

        <div class="mt-3 flex flex-wrap items-center gap-2">
          <BaseButton
            size="sm"
            variant="primary"
            data-testid="telemetry-consent-share"
            :loading="store.saving && pending === 'share'"
            loading-label="Turning on…"
            :disabled="store.saving"
            @click="share"
          >
            {{ CONSENT_COPY.shared.share }}
          </BaseButton>
          <BaseButton
            size="sm"
            variant="ghost"
            data-testid="telemetry-consent-snooze"
            :disabled="store.saving"
            @click="snooze"
          >
            {{ CONSENT_COPY.shared.notNow }}
          </BaseButton>
          <button
            type="button"
            data-testid="telemetry-consent-dismiss"
            class="ml-auto text-[12.5px] text-gray-500 dark:text-gray-400 underline hover:text-gray-700 dark:hover:text-gray-200 disabled:opacity-45"
            :disabled="store.saving"
            @click="dontAsk"
          >
            {{ CONSENT_COPY.shared.dontAsk }}
          </button>
        </div>
      </section>

      <!-- Verb confirmation (contract p.18/27): what happened, and where the
           result appears. Timed by the shared notification composable. -->
      <p
        v-if="notification"
        role="status"
        data-testid="finish-setup-status"
        class="border-t border-gray-200 dark:border-gray-750 px-4 py-2 text-[12.5px] text-status-success-700 dark:text-status-success-300"
      >
        {{ notification.message }}
      </p>
    </BaseCard>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import { useSessionsStore } from '../../stores/sessions'
import { useTelemetrySharingStore } from '../../stores/telemetrySharing'
import { useNotification } from '../../composables/useNotification'
import { viewState } from '../../utils/loadingState'
import BaseCard from '../base/BaseCard.vue'
import BaseBadge from '../base/BaseBadge.vue'
import BaseButton from '../base/BaseButton.vue'
import InlineError from '../InlineError.vue'
import LoadFailed from '../LoadFailed.vue'
import ScanlineReveal from '../ScanlineReveal.vue'
import {
  CONSENT_COPY,
  consentVariant,
  isEmailNudgeVisible,
  isTelemetryConsentVisible,
  persistEmailNudgeDismissed,
  persistSnooze,
  persistWarmShown,
  readEmailNudgeDismissed,
  readSnoozedUntil,
  readWarmShown,
} from './telemetryConsent'

const router = useRouter()
const authStore = useAuthStore()
const sessions = useSessionsStore()
const store = useTelemetrySharingStore()
const { notification, showNotification } = useNotification()

// Per-browser state, read once at setup so a change made in another tab this
// session does not pop a section back mid-render.
const emailDismissed = ref(readEmailNudgeDismissed())
const snoozed = ref(readSnoozedUntil())
const warmShown = ref(readWarmShown())
const actionError = ref('')
const pending = ref('')

// `profileVerified` (#2198) is load-bearing, not defensive: `role` falls back
// to 'user' and `userEmail` to null until /api/users/me lands, so without it a
// section would flash for a non-admin on every page load and then vanish.
// The getter is `role`, NOT `userRole` — the latter does not exist on the auth
// store and reads as undefined, which is how the two cards this one replaces
// (#2380, #2381) shipped permanently hidden. `authRoleGetterContract.spec.js`
// pins the name.
const isAdmin = computed(() => authStore.role === 'admin')

const emailVisible = computed(() =>
  isEmailNudgeVisible({
    profileVerified: authStore.profileVerified,
    isAdmin: isAdmin.value,
    hasEmail: !!authStore.userEmail,
    dismissed: emailDismissed.value,
  })
)

const consentVisible = computed(() =>
  isTelemetryConsentVisible({
    flagsLoaded: sessions.featureFlagsLoaded,
    profileVerified: authStore.profileVerified,
    isAdmin: isAdmin.value,
    enabled: sessions.telemetrySharingEnabled,
    hardDisabled: sessions.telemetrySharingHardDisabled,
    dismissed: sessions.telemetrySharingDismissed,
    firstValue: sessions.telemetrySharingFirstValue,
    snoozed: snoozed.value,
    warmShown: warmShown.value,
  })
)

const variant = computed(() =>
  consentVariant({ firstValue: sessions.telemetrySharingFirstValue, warmShown: warmShown.value })
)
const copy = computed(() => CONSENT_COPY[variant.value])

const openCount = computed(() => (emailVisible.value ? 1 : 0) + (consentVisible.value ? 1 : 0))
const visible = computed(() => openCount.value > 0 || !!notification.value)

const previewView = computed(() =>
  viewState({
    loading: store.loading,
    hasLoaded: store.previewLoaded,
    error: store.error,
    count: store.payloadPreview ? 1 : 0,
  })
)
const prettyPreview = computed(() =>
  store.payloadPreview ? JSON.stringify(store.payloadPreview, null, 2) : ''
)

// The admin status route is called only once the section is going to render —
// never on a Dashboard load it will not act on. The warm variant marks itself
// shown the moment it renders, so it is one-shot per browser, and stays on
// screen for this visit (the ref is not flipped until the next load).
watch(
  consentVisible,
  (now) => {
    if (!now) return
    store.load({ preview: false })
    if (variant.value === 'warm') persistWarmShown()
  },
  { immediate: true }
)

function loadPreview() {
  store.load({ preview: true, force: true })
}

function onPreviewToggle(event) {
  if (event?.target?.open && !store.previewLoaded) store.load({ preview: true })
}

async function share() {
  actionError.value = ''
  pending.value = 'share'
  const ok = await store.setConsent(true, 30)
  pending.value = ''
  if (!ok) {
    actionError.value = store.error || 'Could not turn sharing on. Try again.'
    return
  }
  // Flip the flag the predicate reads so the section retires immediately; the
  // next flags fetch confirms it from the server.
  sessions.telemetrySharingEnabled = true
  sessions.telemetrySharingDismissed = true
  showNotification(CONSENT_COPY.shared.shared, 'success', { timeout: 6000 })
}

function snooze() {
  persistSnooze()
  snoozed.value = true
}

async function dontAsk() {
  actionError.value = ''
  pending.value = 'dismiss'
  const ok = await store.dismissAsk()
  pending.value = ''
  if (!ok) {
    actionError.value = store.error || 'Could not save your choice. Try again.'
    return
  }
  sessions.telemetrySharingDismissed = true
}

function dismissEmail() {
  emailDismissed.value = true
  persistEmailNudgeDismissed()
}

function goToSettings() {
  router.push({ path: '/settings', query: { tab: 'general' } })
}

onMounted(() => {
  // Shared, cached, and already awaited by the rest of the page: `once()` means
  // this costs nothing when another consumer got there first.
  sessions.loadFeatureFlags()
})
</script>
