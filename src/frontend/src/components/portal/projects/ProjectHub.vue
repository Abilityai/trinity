<!--
  The project's hub (trinity-enterprise#661 v3), at the top of Overview.

  Four cards, all from the one project read:
    * Health: the steward's (or creator's) read — on track / at risk / off
      track and a line — and whether an update is due;
    * Where it stands: tasks by status, and what needs attention (to verify,
      stuck, untouched for a week), each opening the Tasks tab;
    * Metrics: the declared metrics a member picked, each as its value, a
      small trend and the one stale rule's verdict;
    * Needs you: my own open asks on this project, answered in place with the
      Inbox's component; everyone else's appear only as a count.
  Guests never see this component.
-->
<template>
  <div class="grid grid-cols-1 gap-4 lg:grid-cols-3" data-testid="project-hub">
    <BaseCard data-testid="project-health">
      <div class="flex items-center gap-2">
        <h2 class="flex-1 text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Health</h2>
        <BaseButton v-if="canSetHealth" size="sm" variant="ghost" data-testid="project-health-update" @click="healthOpen = true">Update</BaseButton>
      </div>
      <template v-if="health">
        <BaseBadge class="mt-2" :variant="HEALTH_BADGE[health.state] || 'neutral'" dot>{{ healthLabel(health.state) }}</BaseBadge>
        <p v-if="health.note" class="mt-2 text-sm text-gray-800 dark:text-gray-200 break-words">{{ health.note }}</p>
        <p class="mt-2 text-xs text-gray-500 dark:text-gray-400">
          {{ authorLabel(health.by, myEmail) }} · <span :title="absolute(health.at)">{{ relative(health.at) }}</span>
        </p>
      </template>
      <p v-else class="mt-2 text-sm text-gray-600 dark:text-gray-300">
        No health update yet. {{ canSetHealth ? 'Say how it is going: on track, at risk or off track.' : 'The steward posts one.' }}
      </p>
      <p v-if="health && rollup.health_due" class="mt-2 text-xs font-medium text-status-warning-700 dark:text-status-warning-300" data-testid="project-health-due">
        Update due: the last one is over two weeks old.
      </p>
    </BaseCard>

    <BaseCard class="lg:col-span-2" data-testid="project-rollup">
      <h2 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Where it stands</h2>
      <p class="mt-2 text-sm text-gray-800 dark:text-gray-200">{{ rollupLine(rollup.by_status) }}</p>
      <ul v-if="attention.length" class="mt-3 space-y-1.5">
        <li v-for="line in attention" :key="line.key">
          <button
            v-if="line.tasks"
            type="button"
            class="text-left text-sm text-action-primary-600 dark:text-action-primary-400 hover:underline"
            :data-testid="`project-attention-${line.key}`"
            @click="$emit('open-tasks')"
          >{{ line.text }}<span v-if="line.ids" class="text-gray-500 dark:text-gray-400"> · {{ line.ids }}</span></button>
          <p v-else class="text-sm text-gray-700 dark:text-gray-300" :data-testid="`project-attention-${line.key}`">{{ line.text }}</p>
        </li>
      </ul>
      <p v-else class="mt-3 text-sm text-gray-600 dark:text-gray-300">Nothing needs attention.</p>
      <p v-if="rollup.last_activity_at" class="mt-3 text-xs text-gray-500 dark:text-gray-400">
        Last activity <span :title="absolute(rollup.last_activity_at)">{{ relative(rollup.last_activity_at) }}</span>
      </p>
    </BaseCard>

    <BaseCard class="lg:col-span-2" data-testid="project-metrics">
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
    </BaseCard>

    <BaseCard data-testid="project-needs-you">
      <h2 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Needs you</h2>
      <p v-if="asks.unavailable" class="mt-2 text-sm text-gray-600 dark:text-gray-300">Couldn't read your Inbox just now. Your asks are still in the Inbox.</p>
      <p v-else-if="!myAskIds.length" class="mt-2 text-sm text-gray-600 dark:text-gray-300">Nothing on this project is waiting on you.</p>
      <PortalAsks v-else class="mt-2" :ask-ids="myAskIds" show-agent pending-only @open-thread="(t) => $emit('open-thread', t)" />
      <p v-if="asks.others_open" class="mt-3 border-t border-gray-100 dark:border-gray-750 pt-3 text-xs text-gray-500 dark:text-gray-400" data-testid="project-asks-others">
        <span class="tabular-nums">{{ asks.others_open }}</span> {{ asks.others_open === 1 ? 'ask is' : 'asks are' }} waiting on other people.
      </p>
    </BaseCard>

    <ProjectHealthModal v-model="healthOpen" :project-id="projectId" :current="health" />
    <ProjectMetricModal v-model="metricOpen" :project-id="projectId" />
    <InlineError v-if="error" class="lg:col-span-3" :message="error" @dismiss="error = ''" />
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseCard from '@/components/base/BaseCard.vue'
import InlineError from '@/components/InlineError.vue'
import PortalAsks from '@/components/portal/PortalAsks.vue'
import ProjectHealthModal from './ProjectHealthModal.vue'
import ProjectMetricModal from './ProjectMetricModal.vue'
import { useProjectsStore } from '@/stores/projects'
import { useClientPortalStore } from '@/stores/clientPortal'
import { formatRelativeTime, formatLocalDateTime } from '@/utils/timestamps'
import { formatMetricValue, metricUnitSuffix } from '@/utils/metricFormat'
import {
  HEALTH_BADGE, healthLabel, rollupLine, attentionLines, trendPoints, metricStateLabel, authorLabel,
  projectErrorMessage,
} from './projectsUtils'

const props = defineProps({
  project: { type: Object, required: true },
  myEmail: { type: String, default: '' },
})
defineEmits(['open-tasks', 'open-thread'])

const store = useProjectsStore()
const portal = useClientPortalStore()
const healthOpen = ref(false)
const metricOpen = ref(false)
const error = ref('')

const projectId = computed(() => props.project.id)
const rollup = computed(() => props.project.rollup || { by_status: {} })
const health = computed(() => rollup.value.health || null)
const metrics = computed(() => props.project.metrics || [])
const asks = computed(() => props.project.asks || { mine: [], others_open: 0 })
const myAskIds = computed(() => asks.value.mine || [])
const archived = computed(() => Boolean(props.project.archived_at))
const canSetHealth = computed(() => props.project.can?.set_health === true)
const canContribute = computed(() => props.project.can?.contribute === true && !archived.value)

// The roll-up's lines, minus the ones this page shows elsewhere (health has its
// own card, asks have theirs); task lines name their ids and open Tasks.
const attention = computed(() => {
  const r = rollup.value
  const ids = (list) => (list || []).slice(0, 3).map((t) => t.id).join(', ') + ((list || []).length > 3 ? '…' : '')
  return attentionLines({ ...r, health_due: false, open_asks: 0 }).map((line) => {
    const list = { verify: r.awaiting_verification, stuck: r.stuck, stale: r.stale }[line.key]
    return list ? { ...line, tasks: true, ids: ids(list) } : line
  })
})

const relative = (t) => (t ? formatRelativeTime(t) : '')
const absolute = (t) => (t ? formatLocalDateTime(t) : '')

async function removeMetric(m) {
  error.value = ''
  try {
    await store.unlinkMetric(projectId.value, m.agent_name, m.name)
  } catch (err) {
    error.value = projectErrorMessage(err)
  }
}

// "Needs you" renders from the Workspace's shared asks list; load it once if
// nothing has yet (the sidebar badge usually has).
onMounted(() => {
  if (!portal.asksLoaded && typeof portal.fetchAsks === 'function') void portal.fetchAsks()
})
</script>
