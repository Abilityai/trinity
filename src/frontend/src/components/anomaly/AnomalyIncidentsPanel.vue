<template>
  <!--
    Operations › Anomalies (trinity-enterprise#74, ruled 2026-10-29): every
    agent's anomaly incidents in one list, so an admin no longer opens agents
    one by one. Each row names what departed from its baseline and links to
    the agent's Schedules tab, where the evidence is; an open one can be
    closed here. Data from stores/anomalyDetection.js (admin + person +
    entitlement at the API).
  -->
  <div class="max-w-7xl mx-auto" data-testid="anomaly-incidents-panel">
    <div class="flex items-center gap-3 mb-4 min-h-[44px]">
      <BaseSelect
        :model-value="store.fleetStatus"
        size="sm"
        aria-label="Incident status"
        data-testid="anomaly-incidents-status"
        @update:model-value="store.loadFleet($event)"
      >
        <option value="open">Open</option>
        <option value="resolved">Resolved</option>
      </BaseSelect>
      <span class="text-xs text-gray-500 dark:text-gray-400 tabular-nums" data-testid="anomaly-incidents-total">{{ totalText }}</span>
      <div class="ml-auto">
        <BaseButton variant="ghost" size="sm" :loading="store.fleetLoading" data-testid="anomaly-incidents-refresh" @click="store.loadFleet()">Refresh</BaseButton>
      </div>
    </div>

    <InlineError
      v-if="store.fleetError && view.state !== 'failed'"
      class="mb-4"
      :message="staleBannerMessage('incidents', null)"
      :detail="store.fleetError"
      retryable
      @retry="store.loadFleet()"
    />
    <InlineError :message="store.resolveError || ''" class="mb-4" @dismiss="store.resolveError = null" />

    <div class="min-h-[12rem]">
      <div v-if="view.state === 'loading'" aria-busy="true" data-testid="anomaly-incidents-loading">
        <SkeletonLoader variant="rows" :count="4" height="2rem" />
        <span class="sr-only">Loading incidents…</span>
      </div>

      <LoadFailed
        v-else-if="view.state === 'failed'"
        title="Couldn't load incidents"
        message="The incident list didn't load. Check your connection and try again."
        :detail="store.fleetError"
        :retrying="store.fleetLoading"
        data-testid="anomaly-incidents-failed"
        @retry="store.loadFleet()"
      />

      <div v-else-if="view.state === 'empty'" class="text-center py-16" data-testid="anomaly-incidents-empty">
        <h3 class="text-lg font-medium text-gray-900 dark:text-gray-100">{{ store.fleetStatus === 'open' ? 'No open incidents' : 'No resolved incidents yet' }}</h3>
        <p class="mt-1 text-sm text-gray-600 dark:text-gray-300">
          An incident opens when a watched schedule's run departs from its baseline. Turn watching on per agent in its Schedules tab.
        </p>
      </div>

      <div v-else class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-750 rounded-lg max-h-[36rem] overflow-y-auto" data-testid="anomaly-incidents-ready">
        <table class="w-full text-sm">
          <thead class="sticky top-0 bg-gray-100 dark:bg-gray-750">
            <tr class="text-[11px] uppercase tracking-wider text-gray-600 dark:text-gray-300">
              <th scope="col" class="text-left font-semibold px-4 py-2">Agent · schedule</th>
              <th scope="col" class="text-left font-semibold px-4 py-2">What moved</th>
              <th scope="col" class="text-right font-semibold px-4 py-2">Runs</th>
              <th scope="col" class="text-left font-semibold px-4 py-2">{{ store.fleetStatus === 'open' ? 'Opened' : 'Closed' }}</th>
              <th scope="col" class="px-4 py-2"><span class="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="inc in store.fleetIncidents"
              :key="inc.id"
              class="border-t border-gray-200 dark:border-gray-750 align-top"
              :data-testid="`anomaly-incident-row-${inc.id}`"
            >
              <td class="px-4 py-2">
                <span class="text-gray-900 dark:text-gray-100">{{ inc.agent_name }}</span>
                <span class="block text-xs text-gray-500 dark:text-gray-400">{{ inc.schedule_name || 'schedule removed' }}</span>
              </td>
              <td class="px-4 py-2 text-gray-700 dark:text-gray-300" data-testid="anomaly-incident-moved">{{ moved(inc) }}</td>
              <td class="px-4 py-2 text-right tabular-nums text-gray-700 dark:text-gray-300">{{ inc.occurrences }}</td>
              <td class="px-4 py-2 text-gray-700 dark:text-gray-300" :title="formatLocalDateTime(when(inc))">
                {{ formatRelativeTime(when(inc)) }}
                <span v-if="store.fleetStatus === 'resolved'" class="block text-xs text-gray-500 dark:text-gray-400">{{ closedBy(inc) }}</span>
              </td>
              <td class="px-4 py-2">
                <div class="flex items-center justify-end gap-2 whitespace-nowrap">
                  <router-link
                    :to="{ path: `/agents/${inc.agent_name}`, query: { tab: 'schedules' } }"
                    class="text-[12.5px] text-action-primary-600 dark:text-action-primary-400 hover:underline"
                    data-testid="anomaly-incident-open"
                  >Open the schedule →</router-link>
                  <BaseButton
                    v-if="inc.status === 'open'"
                    variant="secondary"
                    size="sm"
                    :loading="store.resolving === inc.id"
                    :disabled="!!store.resolving"
                    data-testid="anomaly-incident-close"
                    @click="store.resolveIncident(inc.id)"
                  >Close</BaseButton>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted } from 'vue'
import BaseButton from '../base/BaseButton.vue'
import BaseSelect from '../base/BaseSelect.vue'
import InlineError from '../InlineError.vue'
import LoadFailed from '../LoadFailed.vue'
import SkeletonLoader from '../SkeletonLoader.vue'
import { useAnomalyDetectionStore, FLEET_LIMIT } from '../../stores/anomalyDetection'
import { viewState, staleBannerMessage } from '../../utils/loadingState'
import { formatRelativeTime, formatLocalDateTime } from '../../utils/timestamps'
import { METRIC_LABELS, fmtChange } from './format'

const store = useAnomalyDetectionStore()

const view = computed(() => viewState({
  loading: store.fleetLoading,
  hasLoaded: store.fleetHasLoaded,
  error: store.fleetError,
  count: store.fleetIncidents.length,
}))

const totalText = computed(() => {
  if (!store.fleetHasLoaded) return ''
  const n = store.fleetIncidents.length
  const base = `${n} ${store.fleetStatus}`
  return n >= FLEET_LIMIT ? `${base} · newest ${FLEET_LIMIT} shown` : base
})

function moved(inc) {
  const parts = (inc.metrics || []).map((m) => `${METRIC_LABELS[m.metric] || m.metric} ${fmtChange(m)}`)
  return parts.length ? parts.join(' · ') : '—'
}

const when = (inc) => (store.fleetStatus === 'resolved' ? inc.resolved_at || inc.updated_at : inc.created_at)

function closedBy(inc) {
  const by = inc.resolved_by || ''
  // The pass closes an incident itself after a streak of normal runs.
  if (by === 'platform:normal') return 'closed after normal runs'
  if (!by) return ''
  return `by ${by.replace(/^user:/, '')}`
}

onMounted(() => {
  if (!store.fleetHasLoaded) store.loadFleet()
})
</script>
