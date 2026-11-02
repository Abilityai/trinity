<template>
  <!--
    A schedule's open anomaly incident (trinity-enterprise#74), inside its row:
    what departed from the baseline and by how much, the recent runs with the
    pass's verdict, the output of the first anomalous run, and a person's
    close. It also closes itself after a streak of normal runs.
  -->
  <div
    v-if="incident"
    class="mt-3 -mx-4 -mb-4 px-4 py-4 rounded-b-lg bg-status-warning-50 dark:bg-status-warning-900/20 border-t border-status-warning-200 dark:border-status-warning-800 flex flex-col gap-3"
    :data-testid="`anomaly-incident-${scheduleId}`"
  >
    <div class="flex flex-wrap items-center gap-3">
      <span class="text-sm font-medium text-gray-900 dark:text-white">
        Open incident since {{ shortDate(incident.created_at) }} · {{ incident.occurrences }} anomalous {{ incident.occurrences === 1 ? 'run' : 'runs' }}
      </span>
      <router-link v-if="incident.ask_id" :to="{ path: '/operations', query: { tab: 'needs-response' } }" class="text-[12.5px] text-action-primary-600 dark:text-action-primary-400 hover:underline">Alert in Needs Response</router-link>
      <BaseButton
        class="ml-auto"
        variant="secondary"
        size="sm"
        :loading="store.resolving === incident.id"
        :disabled="!!store.resolving"
        data-testid="anomaly-close"
        @click="store.resolveIncident(incident.id)"
      >Close incident</BaseButton>
    </div>
    <InlineError :message="store.resolveError || ''" @dismiss="store.resolveError = null" />

    <div class="overflow-x-auto rounded-md border border-status-warning-200 dark:border-status-warning-800 bg-white dark:bg-gray-800">
      <table class="w-full text-sm">
        <thead class="bg-gray-100 dark:bg-gray-750">
          <tr class="text-[11px] uppercase tracking-wider text-gray-600 dark:text-gray-300">
            <th scope="col" class="text-left font-semibold px-3 py-2">Metric</th>
            <th scope="col" class="text-right font-semibold px-3 py-2">Latest anomalous run</th>
            <th scope="col" class="text-right font-semibold px-3 py-2">Usual (median)</th>
            <th scope="col" class="text-right font-semibold px-3 py-2">Change</th>
            <th scope="col" class="text-left font-semibold px-3 py-2">Trend</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="m in incident.metrics" :key="m.metric" class="border-t border-gray-200 dark:border-gray-750" data-testid="anomaly-metric">
            <td class="px-3 py-2 text-gray-900 dark:text-gray-100">{{ METRIC_LABELS[m.metric] || m.metric }}</td>
            <td class="px-3 py-2 text-right tabular-nums text-gray-900 dark:text-gray-100">{{ fmtMetric(m.metric, m.value) }}</td>
            <td class="px-3 py-2 text-right tabular-nums text-gray-600 dark:text-gray-300">{{ fmtMetric(m.metric, m.median) }}</td>
            <td class="px-3 py-2 text-right tabular-nums text-status-warning-700 dark:text-status-warning-400">{{ fmtChange(m) }}</td>
            <td class="px-3 py-2 text-gray-600 dark:text-gray-300">{{ TREND_TEXT[incident.evidence?.trend?.[m.metric]] || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="flex flex-wrap gap-4">
      <div class="flex-1 min-w-[16rem] flex flex-col gap-1.5">
        <span class="text-[12.5px] font-medium text-gray-700 dark:text-gray-300">Recent runs</span>
        <ul class="flex flex-col gap-1 text-[12.5px] tabular-nums">
          <li v-for="r in runs" :key="r.id" class="flex flex-wrap gap-3" data-testid="anomaly-run">
            <button type="button" class="font-mono text-action-primary-600 dark:text-action-primary-400 hover:underline" @click="emit('open-execution', r.id)">{{ shortId(r.id) }}</button>
            <span class="text-gray-600 dark:text-gray-300">{{ shortDate(r.started_at) }}</span>
            <span class="text-gray-900 dark:text-gray-100">{{ r.status === 'failed' ? 'failed' : fmtDuration(r.duration_ms) }}</span>
            <span :class="r.anomalous ? 'text-status-warning-700 dark:text-status-warning-400' : 'text-gray-600 dark:text-gray-300'">{{ r.anomalous === true ? 'anomalous' : r.anomalous === false ? 'normal' : 'not scored' }}</span>
          </li>
        </ul>
      </div>
      <div v-if="incident.evidence?.excerpt" class="flex-1 min-w-[16rem] flex flex-col gap-1.5">
        <span class="text-[12.5px] font-medium text-gray-700 dark:text-gray-300">Output of the first anomalous run</span>
        <p class="text-[12.5px] font-mono rounded-md border border-gray-200 dark:border-gray-750 bg-white dark:bg-gray-900 px-2.5 py-2 text-gray-700 dark:text-gray-300 break-words">{{ incident.evidence.excerpt }}</p>
      </div>
    </div>

    <p class="text-[12.5px] text-gray-600 dark:text-gray-300" data-testid="anomaly-incident-foot">{{ foot }}</p>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import BaseButton from '../base/BaseButton.vue'
import InlineError from '../InlineError.vue'
import { useAnomalyDetectionStore } from '../../stores/anomalyDetection'
import { METRIC_LABELS, TREND_TEXT, fmtMetric, fmtChange, fmtDuration, shortDate } from './format'

const props = defineProps({
  scheduleId: { type: String, required: true },
})
const emit = defineEmits(['open-execution'])

const store = useAnomalyDetectionStore()
const entry = computed(() => store.bySchedule[props.scheduleId] || null)
const incident = computed(() => (entry.value?.state === 'off_baseline' ? entry.value.incident : null))
const runs = computed(() => (entry.value?.recent_runs || []).slice(0, 6))
const shortId = (id) => (id && id.length > 10 ? `${id.slice(0, 4)}…${id.slice(-2)}` : id)

const foot = computed(() => {
  const n = store.effective?.resolve_after_normal || 3
  const closes = `Closes on its own after ${n} normal ${n === 1 ? 'run' : 'runs'} in a row.`
  const rem = incident.value?.remediation
  if (rem?.action === 'pause_schedule') {
    return rem.ok
      ? `${closes} Anomaly detection paused this schedule; re-enable it once the cause is fixed.`
      : `${closes} Anomaly detection tried to pause this schedule and could not. Pause it by hand if needed.`
  }
  return store.effective?.remediation === 'pause_schedule'
    ? `${closes} This schedule will be paused after ${store.effective.remediate_after} anomalous runs.`
    : `${closes} Alert only: this schedule has not been paused.`
})
</script>
