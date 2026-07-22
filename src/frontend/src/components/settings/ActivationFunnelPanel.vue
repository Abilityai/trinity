<template>
  <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
    <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between gap-3">
      <div>
        <h2 class="text-lg font-medium text-gray-900 dark:text-white">Activation funnel</h2>
        <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
          First-run activation and first-value events, recorded
          <span class="font-medium">locally on this instance only</span> — nothing
          leaves the box. (ent#184)
        </p>
      </div>
      <select
        v-model.number="windowDays"
        @change="load"
        class="text-sm rounded-md border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100"
        aria-label="Time window"
      >
        <option :value="7">Last 7 days</option>
        <option :value="30">Last 30 days</option>
        <option :value="90">Last 90 days</option>
        <option :value="0">All time</option>
      </select>
    </div>

    <div class="p-6">
      <div v-if="loading" class="text-sm text-gray-500 dark:text-gray-400">Loading…</div>

      <div v-else-if="error" class="text-sm text-status-danger-600 dark:text-status-danger-400">
        {{ error }}
      </div>

      <template v-else>
        <!-- Honest empty state -->
        <div
          v-if="isEmpty"
          class="rounded-md border border-dashed border-gray-300 dark:border-gray-600 p-6 text-center text-sm text-gray-500 dark:text-gray-400"
        >
          No activation events recorded yet. Complete the first-run wizard or
          create an agent, then check back — events are captured locally from the
          start.
        </div>

        <template v-else>
          <!-- Setup funnel -->
          <h3 class="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-3">Setup funnel</h3>
          <ul class="space-y-2">
            <li v-for="(step, i) in funnel" :key="step.key" class="flex items-center gap-3">
              <div class="w-40 shrink-0 text-sm text-gray-700 dark:text-gray-300">{{ step.label }}</div>
              <div class="flex-1 h-6 rounded bg-gray-100 dark:bg-gray-700 overflow-hidden">
                <div
                  class="h-full bg-action-primary-500 dark:bg-action-primary-600"
                  :style="{ width: barWidth(step.count) + '%' }"
                ></div>
              </div>
              <div class="w-24 shrink-0 text-right text-sm tabular-nums text-gray-900 dark:text-gray-100">
                {{ step.count }}
                <span v-if="i > 0 && dropOff(i) !== null" class="text-xs text-gray-400 ml-1">
                  ({{ dropOff(i) }}%↓)
                </span>
              </div>
            </li>
          </ul>

          <!-- First-value events -->
          <h3 class="text-sm font-semibold text-gray-700 dark:text-gray-300 mt-6 mb-3">
            First-value events
          </h3>
          <div class="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <div
              v-for="fv in firstValueRows"
              :key="fv.key"
              class="rounded-md border border-gray-200 dark:border-gray-700 p-3 text-center"
            >
              <div class="text-2xl font-semibold text-gray-900 dark:text-gray-100 tabular-nums">
                {{ fv.count }}
              </div>
              <div class="mt-1 text-xs text-gray-500 dark:text-gray-400">{{ fv.label }}</div>
            </div>
          </div>
        </template>

        <p class="mt-6 text-xs text-gray-400 dark:text-gray-500">
          Install {{ installationId }} · local-only, zero network egress.
        </p>
      </template>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import api from '../../api'

// Labels for the setup-funnel steps (order = funnel order). Mirrors the
// backend allow-list; the enterprise endpoint returns counts keyed by these.
const FUNNEL_STEPS = [
  { key: 'setup_started', label: 'Opened wizard' },
  { key: 'setup_step_create', label: 'Picked intent' },
  { key: 'setup_step_credential', label: 'Created first agent' },
  { key: 'setup_completed', label: 'Completed setup' },
]

const FIRST_VALUE = [
  { key: 'first_agent_created', label: 'First agent' },
  { key: 'first_chat', label: 'First chat' },
  { key: 'first_schedule_created', label: 'First schedule' },
  { key: 'first_channel_connected', label: 'First channel' },
]

const windowDays = ref(30)
const loading = ref(false)
const error = ref('')
const funnelCounts = ref({})
const firstValueCounts = ref({})
const installationId = ref('')

const funnel = computed(() =>
  FUNNEL_STEPS.map((s) => ({ ...s, count: funnelCounts.value[s.key] || 0 }))
)
const firstValueRows = computed(() =>
  FIRST_VALUE.map((s) => ({ ...s, count: firstValueCounts.value[s.key] || 0 }))
)

const isEmpty = computed(
  () =>
    funnel.value.every((s) => s.count === 0) &&
    firstValueRows.value.every((s) => s.count === 0)
)

// Bar width relative to the top of the funnel (setup_started).
function barWidth(count) {
  const top = funnel.value[0]?.count || 0
  if (!top) return 0
  return Math.round((count / top) * 100)
}

// Drop-off % from the previous step to this one.
function dropOff(i) {
  const prev = funnel.value[i - 1]?.count || 0
  const cur = funnel.value[i]?.count || 0
  if (!prev) return null
  return Math.round(((prev - cur) / prev) * 100)
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const r = await api.get('/api/enterprise/telemetry/funnel', {
      params: { window_days: windowDays.value },
    })
    funnelCounts.value = r.data?.funnel || {}
    firstValueCounts.value = r.data?.first_value || {}
    installationId.value = r.data?.installation_id || ''
  } catch (e) {
    error.value =
      e?.response?.data?.detail ||
      'Failed to load activation data. This view requires the telemetry entitlement.'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>
