<!--
  Step `sharing` (ent#581) — absorbs FinishSetupCard section 2 (ent#437 /
  ent#12). One step in the sequence, not a dialog that appears afterwards.

  Every sentence of consent is `CONSENT_COPY` in `telemetryConsent.js`, spec'd. "Share" and "Don't ask again" write the server marker; the old
  per-browser "Not now" snooze is the chassis Skip now. The payload preview
  loads on expand — never on a Dashboard load.
-->
<template>
  <div data-testid="first-run-step-sharing">
    <FirstRunStepHeader
      kicker="Privacy"
      :title="decided ? 'Usage sharing' : copy.title"
      :lead="decided ? decidedLead : copy.lead"
      :badge="decided ? 'Done' : 'Optional'"
      schematic="sharing"
    />

    <div class="mt-5 space-y-4">
      <div class="grid grid-cols-1 gap-3 sm:grid-cols-2" data-testid="first-run-sharing-summary">
        <div class="rounded-lg border border-gray-200 dark:border-gray-750 p-3">
          <h3 class="text-sm font-[550] text-gray-900 dark:text-gray-100">What we'd see</h3>
          <ul class="mt-1 space-y-1 text-[13px] text-gray-600 dark:text-gray-300">
            <li>Your Trinity version</li>
            <li>How many agents and runs you have</li>
            <li>How far this setup got</li>
          </ul>
        </div>
        <div class="rounded-lg border border-gray-200 dark:border-gray-750 p-3">
          <h3 class="text-sm font-[550] text-gray-900 dark:text-gray-100">What we never see</h3>
          <ul class="mt-1 space-y-1 text-[13px] text-gray-600 dark:text-gray-300">
            <li>Your messages</li>
            <li>What your agents do</li>
            <li>Agent names or emails</li>
          </ul>
        </div>
      </div>

      <div v-if="!decided" class="flex flex-wrap items-center gap-2">
        <BaseButton
          size="sm"
          data-testid="first-run-sharing-share"
          :loading="store.saving && pending === 'share'"
          loading-label="Turning on…"
          :disabled="store.saving"
          @click="share"
        >
          {{ CONSENT_COPY.shared.share }}
        </BaseButton>
        <BaseButton
          size="sm"
          variant="secondary"
          data-testid="first-run-sharing-dont-ask"
          :loading="store.saving && pending === 'dismiss'"
          :disabled="store.saving"
          @click="dontAsk"
        >
          {{ CONSENT_COPY.shared.dontAsk }}
        </BaseButton>
      </div>
      <p v-else-if="confirmation" role="status" class="text-[12.5px] text-status-success-700 dark:text-status-success-300">
        {{ confirmation }}
      </p>

      <!-- The exact payload, loaded on expand: in plain words, with the raw data under it. -->
      <details class="rounded-md border border-gray-200 dark:border-gray-750" @toggle="onPreviewToggle">
        <summary class="cursor-pointer px-3 py-1.5 text-[12.5px] font-[550] text-gray-700 dark:text-gray-300">
          {{ CONSENT_COPY.shared.previewSummary }}
        </summary>
        <div class="px-3 pb-3 pt-1">
          <p class="mb-2 text-[12.5px] leading-[1.5] text-gray-500 dark:text-gray-400">{{ CONSENT_COPY.shared.detail }}</p>
          <LoadFailed
            v-if="previewView.state === 'failed'"
            title="Couldn't build the preview"
            :detail="store.error"
            retryable
            @retry="loadPreview"
          />
          <div v-else>
            <div
              v-if="previewView.state === 'loading'"
              class="animate-pulse motion-reduce:animate-none space-y-2 rounded bg-gray-50 dark:bg-gray-900 p-2"
              aria-busy="true"
            >
              <div class="h-3 w-3/4 rounded bg-gray-200 dark:bg-gray-800"></div>
              <div class="h-3 w-5/6 rounded bg-gray-100 dark:bg-gray-800/60"></div>
              <div class="h-3 w-2/3 rounded bg-gray-100 dark:bg-gray-800/60"></div>
              <span class="sr-only">Loading…</span>
            </div>
            <template v-else>
              <dl class="divide-y divide-gray-100 dark:divide-gray-750 text-[13px]" data-testid="first-run-sharing-plain">
                <div v-for="row in plainPreview" :key="row.label" class="flex justify-between gap-4 py-1.5">
                  <dt class="text-gray-500 dark:text-gray-400">{{ row.label }}</dt>
                  <dd class="text-right text-gray-900 dark:text-gray-100">{{ row.value }}</dd>
                </div>
              </dl>
              <details class="mt-2">
                <summary class="cursor-pointer text-[12.5px] text-gray-500 dark:text-gray-400">Show the raw data</summary>
                <pre class="mt-1 max-h-48 overflow-auto rounded bg-gray-50 dark:bg-gray-900 p-2 text-xs text-gray-700 dark:text-gray-300"><code>{{ prettyPreview }}</code></pre>
              </details>
            </template>
          </div>
        </div>
      </details>

      <InlineError v-if="actionError" :message="actionError" @dismiss="actionError = ''" />
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useTelemetrySharingStore } from '../../../stores/telemetrySharing'
import { viewState } from '../../../utils/loadingState'
import BaseButton from '../../base/BaseButton.vue'
import InlineError from '../../InlineError.vue'
import LoadFailed from '../../LoadFailed.vue'
import FirstRunStepHeader from '../FirstRunStepHeader.vue'
import { CONSENT_COPY, consentVariant, payloadSummary, persistWarmShown, readWarmShown } from '../telemetryConsent'

const props = defineProps({ ctx: { type: Object, default: () => ({}) } })
const emit = defineEmits(['complete', 'skip'])

const store = useTelemetrySharingStore()

const actionError = ref('')
const pending = ref('')
const confirmation = ref('')

// Read once: the warm copy stays on screen for this visit even after it is
// recorded as seen below.
const warmShown = readWarmShown()
const variant = consentVariant({ firstValue: !!props.ctx.telemetryFirstValue, warmShown })
const copy = CONSENT_COPY[variant]

const decided = computed(() => !!props.ctx.telemetryEnabled || !!props.ctx.telemetryDismissed)
const decidedLead = computed(() =>
  props.ctx.telemetryEnabled
    ? 'Sharing is on. Turn it off any time in Settings → General.'
    : 'You chose not to share. Turn it on any time in Settings → General.'
)

// The warm ask (ent#437) is spent only once it has really been SEEN. This step
// mounts only while it is the one on screen, so mounting is being seen — the
// card-stack case where a hidden card spent it cannot happen here.
onMounted(() => {
  if (variant === 'warm' && !decided.value) persistWarmShown()
})

const previewView = computed(() =>
  viewState({
    loading: store.loading,
    hasLoaded: store.previewLoaded,
    error: store.error,
    count: store.payloadPreview ? 1 : 0,
  })
)
const plainPreview = computed(() => payloadSummary(store.payloadPreview))
const prettyPreview = computed(() =>
  store.payloadPreview ? JSON.stringify(store.payloadPreview, null, 2) : ''
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
  confirmation.value = CONSENT_COPY.shared.shared
  emit('complete')
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
  emit('complete')
}
</script>
