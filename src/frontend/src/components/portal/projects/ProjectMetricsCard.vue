<!--
  The project's Metrics card (trinity-enterprise#661 v3.1): the declared
  metrics a member picked, each as its value, a small trend and the one stale
  rule's verdict. One of the Overview's cards; the page places it (#3694).
-->
<template>
  <BaseCard data-testid="project-metrics">
    <div class="flex items-center gap-2">
      <h2 class="flex-1 text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Metrics</h2>
      <BaseButton v-if="canContribute" size="sm" variant="ghost" data-testid="project-metric-add" @click="metricOpen = true">Add metric</BaseButton>
    </div>
    <p v-if="!metrics.length" class="mt-2 text-sm text-gray-600 dark:text-gray-300">
      No metrics yet. {{ canContribute ? 'Add the numbers that say whether this project is working, from the metrics its agents record.' : 'Members can add the metrics its agents record.' }}
    </p>
    <ul v-else class="mt-2 grid grid-cols-1 gap-3 sm:grid-cols-2">
      <li
        v-for="m in metrics"
        :key="`${m.agent_name}:${m.name}`"
        class="rounded-lg border border-gray-200 dark:border-gray-750 p-3"
        :data-testid="`project-metric-${m.agent_name}-${m.name}`"
      >
        <div class="flex items-start gap-2">
          <div class="min-w-0 flex-1">
            <p class="truncate text-sm font-medium text-gray-900 dark:text-gray-100" :title="m.description || m.label">{{ m.label || m.name }}</p>
            <p class="truncate text-xs text-gray-500 dark:text-gray-400">{{ m.agent_name }}</p>
          </div>
          <button
            v-if="canContribute"
            type="button"
            class="rounded p-1 text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
            :aria-label="`Remove ${m.label || m.name}`"
            @click="removeMetric(m)"
          >×</button>
        </div>
        <div class="mt-2 flex items-end gap-3">
          <p class="text-xl font-semibold tabular-nums text-gray-900 dark:text-gray-100">
            {{ formatMetricValue(m.latest?.value, m.type) }}<span class="ml-0.5 text-sm font-normal text-gray-500 dark:text-gray-400">{{ metricUnitSuffix(m.latest?.value, m.type, m.unit) }}</span>
          </p>
          <svg
            v-if="trendPoints(m.trend)"
            class="mb-1 h-6 w-24 text-action-primary-500 dark:text-action-primary-400"
            viewBox="0 0 96 24"
            aria-hidden="true"
          ><polyline :points="trendPoints(m.trend)" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" /></svg>
        </div>
        <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
          <BaseBadge v-if="metricStateLabel(m)" :variant="m.stale === true || m.declared === false ? 'warning' : 'neutral'">{{ metricStateLabel(m) }}</BaseBadge>
          <span v-if="m.last_point_at" :class="metricStateLabel(m) ? 'ml-2' : ''" :title="absolute(m.last_point_at)">Measured {{ relative(m.last_point_at) }}</span>
        </p>
      </li>
    </ul>
    <InlineError v-if="error" class="mt-3" :message="error" @dismiss="error = ''" />
    <ProjectMetricModal v-model="metricOpen" :project-id="project.id" />
  </BaseCard>
</template>

<script setup>
import { computed, ref } from 'vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseCard from '@/components/base/BaseCard.vue'
import InlineError from '@/components/InlineError.vue'
import ProjectMetricModal from './ProjectMetricModal.vue'
import { useProjectsStore } from '@/stores/projects'
import { formatRelativeTime, formatLocalDateTime } from '@/utils/timestamps'
import { formatMetricValue, metricUnitSuffix } from '@/utils/metricFormat'
import { trendPoints, metricStateLabel, projectErrorMessage } from './projectsUtils'

const props = defineProps({
  project: { type: Object, required: true },
})

const store = useProjectsStore()
const metricOpen = ref(false)
const error = ref('')
const metrics = computed(() => props.project.metrics || [])
const canContribute = computed(() => props.project.can?.contribute === true && !props.project.archived_at)
const relative = (t) => (t ? formatRelativeTime(t) : '')
const absolute = (t) => (t ? formatLocalDateTime(t) : '')

async function removeMetric(m) {
  error.value = ''
  try {
    await store.unlinkMetric(props.project.id, m.agent_name, m.name)
  } catch (err) {
    error.value = projectErrorMessage(err)
  }
}
</script>
