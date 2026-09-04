<template>
  <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
    <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
      <h2 class="text-lg font-medium text-gray-900 dark:text-white">Usage sharing</h2>
      <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
        Optionally share <span class="font-medium">anonymous, aggregate</span> usage
        so we can see whether the platform works outside our own instance, and so you
        can see how your setup compares to the fleet. Off by default, reversible any
        time — nothing is shared until you turn this on. (ent#12, ent#437)
      </p>
    </div>

    <div class="p-6 space-y-5">
      <div v-if="store.error" class="text-sm text-status-danger-600 dark:text-status-danger-400">
        {{ store.error }}
      </div>

      <!-- Hard-disabled by config -->
      <div
        v-if="store.status.hard_disabled"
        class="rounded-md border border-status-warning-100 dark:border-status-warning-500/30 bg-status-warning-100/60 dark:bg-status-warning-500/16 px-4 py-3 text-sm text-status-warning-700 dark:text-status-warning-300"
      >
        Sharing is disabled by configuration
        (<code class="text-xs">TELEMETRY_SHARING_ENABLED=false</code> or
        <code class="text-xs">DO_NOT_TRACK</code>). The toggle stays off and nothing leaves the box.
      </div>

      <!-- Toggle -->
      <div class="flex items-start justify-between gap-4">
        <div>
          <p class="text-sm font-medium text-gray-900 dark:text-gray-100">Share anonymous usage</p>
          <p class="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
            <template v-if="store.status.enabled">
              On since {{ fmt(store.status.consent_at) }}.
              <template v-if="store.status.last_shared_at">Last delivered {{ fmt(store.status.last_shared_at) }}.</template>
              <template v-else>Nothing delivered yet.</template>
            </template>
            <template v-else>Currently off — no egress.</template>
          </p>
        </div>
        <button
          type="button"
          :disabled="store.saving || store.status.hard_disabled"
          @click="toggle"
          :class="[
            'relative inline-flex h-6 w-11 flex-shrink-0 rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-action-primary-500 focus:ring-offset-2 dark:focus:ring-offset-gray-800 disabled:opacity-40',
            store.status.enabled ? 'bg-action-primary-600' : 'bg-gray-300 dark:bg-gray-600',
          ]"
          role="switch"
          :aria-checked="store.status.enabled"
        >
          <span
            :class="['inline-block h-5 w-5 transform rounded-full bg-white transition-transform mt-0.5',
                     store.status.enabled ? 'translate-x-5' : 'translate-x-0.5']"
          ></span>
        </button>
      </div>

      <!-- Backfill selection (only meaningful when turning on) -->
      <div v-if="!store.status.enabled && !store.status.hard_disabled" class="flex items-center gap-2 text-sm">
        <label class="text-gray-600 dark:text-gray-300">On consent, also share the last</label>
        <select v-model.number="backfillDays" class="text-sm rounded-md border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100">
          <option :value="7">7 days</option>
          <option :value="30">30 days</option>
          <option :value="90">90 days</option>
          <option :value="0">no history</option>
        </select>
        <span class="text-gray-600 dark:text-gray-300">of local history, so your benchmarks are accurate.</span>
      </div>

      <!-- Share identity (ent#437) -->
      <div class="text-xs text-gray-500 dark:text-gray-400">
        <span class="font-medium text-gray-700 dark:text-gray-300">Share id:</span>
        <code v-if="store.status.sharing_id" class="ml-1 rounded bg-gray-100 dark:bg-gray-750 px-1 py-0.5 text-gray-700 dark:text-gray-300">{{ store.status.sharing_id }}</code>
        <span v-else class="ml-1">none — a random id is minted when you turn sharing on.</span>
        <span class="block mt-0.5">
          It is discarded when you turn sharing off and a new one is minted next time, so a
          revoke forgets you locally. It is never your install id and is never sent beside it.
          Anything already sent stays with the receiver until a deletion signal exists.
        </span>
      </div>

      <!-- What's shared / inspectable preview -->
      <details class="rounded-md border border-gray-200 dark:border-gray-700">
        <summary class="cursor-pointer px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300">
          Exactly what would be shared (inspect before you consent)
        </summary>
        <div class="px-4 pb-4 pt-1">
          <p class="text-xs text-gray-500 dark:text-gray-400 mb-2">
            Anonymized aggregates only — release version, platform, edition, install lane,
            feature list, agent &amp; execution <span class="font-medium">counts</span>,
            activation-funnel counts, and an outcome mix (how runs ended, by trigger and
            by status, plus provider rate-limit and auth failure counts).
            <span class="font-medium">No PII, no content, no prompts, no emails, no agent
            names.</span> Keyed by the random share id above.
          </p>
          <pre class="max-h-72 overflow-auto rounded bg-gray-50 dark:bg-gray-900 p-3 text-xs text-gray-700 dark:text-gray-300"><code>{{ prettyPreview }}</code></pre>
        </div>
      </details>

      <!-- Recent sends (ent#437) — inspect AFTER the fact -->
      <div>
        <p class="text-sm font-medium text-gray-900 dark:text-gray-100">Recent sends</p>
        <p class="mt-0.5 text-xs text-gray-500 dark:text-gray-400">{{ receiverLine }}</p>
        <p
          v-if="!store.status.recent_sends.length"
          class="mt-2 text-xs text-gray-500 dark:text-gray-400"
        >
          Nothing sent yet — nothing leaves the box until sharing is on. The last
          {{ RECENT_LIMIT }} attempts, successes and failures alike, appear here.
        </p>
        <ul v-else class="mt-2 divide-y divide-gray-200 dark:divide-gray-750 rounded-md border border-gray-200 dark:border-gray-750">
          <li v-for="(send, i) in store.status.recent_sends" :key="i" class="px-3 py-2 text-xs">
            <details>
              <summary class="cursor-pointer flex flex-wrap items-center gap-2 text-gray-700 dark:text-gray-300">
                <BaseBadge :variant="send.ok ? 'success' : 'warning'" dot>{{ send.ok ? 'delivered' : 'not delivered' }}</BaseBadge>
                <span class="tabular-nums" :title="send.sent_at">{{ fmt(send.sent_at) }}</span>
                <span class="text-gray-500 dark:text-gray-400">
                  {{ send.backfill ? 'backfill' : 'heartbeat' }} · {{ send.window_days }}d window
                  <template v-if="send.http_status"> · HTTP {{ send.http_status }}</template>
                  <template v-else-if="send.error"> · {{ send.error }}</template>
                </span>
              </summary>
              <pre class="mt-2 max-h-56 overflow-auto rounded bg-gray-50 dark:bg-gray-900 p-2 text-gray-700 dark:text-gray-300"><code>{{ pretty(send.payload) }}</code></pre>
            </details>
          </li>
        </ul>
      </div>

      <p class="text-xs text-gray-400 dark:text-gray-500">
        Reversible: turn this off any time and egress stops immediately.
        <a href="https://github.com/abilityai/trinity/blob/main/docs/PRODUCT_EVENTS.md" target="_blank" rel="noopener" class="underline">Payload schema &amp; details</a>.
      </p>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useTelemetrySharingStore } from '../../stores/telemetrySharing'
import BaseBadge from '../base/BaseBadge.vue'
import { receiverCopy } from '../onboarding/telemetryConsent'

const RECENT_LIMIT = 5

const store = useTelemetrySharingStore()
const backfillDays = ref(30)

const prettyPreview = computed(() =>
  store.payloadPreview ? JSON.stringify(store.payloadPreview, null, 2) : '(load to preview)'
)

const receiverLine = computed(() =>
  receiverCopy(store.status.receiver_hint, store.status.share_url)
)

function pretty(obj) {
  try { return JSON.stringify(obj, null, 2) } catch { return String(obj) }
}

function fmt(iso) {
  if (!iso) return '—'
  try { return new Date(iso).toLocaleString() } catch { return iso }
}

async function toggle() {
  const enabling = !store.status.enabled
  const ok = await store.setConsent(enabling, enabling ? backfillDays.value : null)
  if (ok) store.load({ preview: true, force: true })
}

onMounted(() => {
  store.load({ preview: true })
})
</script>
