<!--
  A project's runs (trinity-enterprise#1105): every execution attributed to the
  project — from its chats, its tasks, its steward and the work they delegated
  — newest first, with per-agent cost tiles on top. A run the viewer cannot
  open shows only its agent and status (the backend sends nothing else); when
  any row is fenced the cost tile says it covers only the runs you can open.
  Filters refetch; "Load more" follows `next_cursor`. Bounded scroll region,
  viewState for loading / empty / failed / stale (principles 15, 28).
-->
<template>
  <div class="space-y-4" data-testid="project-runs">
    <div class="flex flex-wrap items-end justify-between gap-3">
      <h2 class="text-base font-semibold text-gray-900 dark:text-gray-100">Runs</h2>
      <div class="flex flex-wrap items-end gap-2">
        <BaseSelect id="runs-agent" v-model="agent" label="Agent" size="sm" class="w-40" data-testid="project-runs-agent">
          <option value="">All agents</option>
          <option v-for="a in agentOptions" :key="a" :value="a">{{ a }}</option>
        </BaseSelect>
        <BaseSelect id="runs-status" v-model="status" label="Status" size="sm" class="w-36" data-testid="project-runs-status">
          <option value="">Any status</option>
          <option v-for="s in STATUSES" :key="s" :value="s">{{ statusText(s) }}</option>
        </BaseSelect>
        <BaseSelect id="runs-window" v-model="windowDays" label="Window" size="sm" class="w-32" data-testid="project-runs-window">
          <option v-for="d in WINDOWS" :key="d" :value="String(d)">Last {{ d }} days</option>
        </BaseSelect>
      </div>
    </div>

    <SkeletonLoader v-if="view.state === 'loading'" :count="4" height="3rem" gap="0.5rem" />
    <LoadFailed v-else-if="view.state === 'failed'" title="Couldn't load the runs" :message="state.error" dense data-testid="project-runs-failed" @retry="load" />
    <template v-else>
      <InlineError v-if="view.stale" :message="state.error" @dismiss="load" />

      <!-- Per-agent cost tiles: one fixed-height row that scrolls sideways (principle 30). -->
      <div v-if="totals" class="flex gap-2 overflow-x-auto pb-1" data-testid="project-runs-tiles">
        <BaseCard class="min-w-[11rem] flex-shrink-0" data-testid="project-runs-total">
          <p class="text-xs text-gray-500 dark:text-gray-400" data-testid="project-runs-cost-label">{{ costLabel }}</p>
          <p class="text-lg font-semibold tabular-nums text-gray-900 dark:text-gray-100">{{ money(totals.cost) }}</p>
          <p class="text-xs tabular-nums text-gray-500 dark:text-gray-400">{{ runsLine(totals) }}</p>
        </BaseCard>
        <BaseCard v-for="a in totals.by_agent || []" :key="a.agent_name" class="min-w-[11rem] flex-shrink-0" :data-testid="`project-runs-tile-${a.agent_name}`">
          <p class="truncate text-xs text-gray-500 dark:text-gray-400" :title="a.agent_name">{{ a.agent_name }}</p>
          <p class="text-lg font-semibold tabular-nums text-gray-900 dark:text-gray-100">{{ money(a.cost) }}</p>
          <p class="text-xs tabular-nums text-gray-500 dark:text-gray-400">{{ runsLine(a) }}</p>
        </BaseCard>
      </div>

      <p v-if="view.state === 'empty'" class="rounded-lg border border-dashed border-gray-300 dark:border-gray-700 p-6 text-center text-sm text-gray-600 dark:text-gray-300" data-testid="project-runs-empty">
        No runs on this project yet. Runs started from its chats, its tasks or its steward show here.
      </p>
      <ol v-else class="max-h-[32rem] space-y-2 overflow-y-auto pr-1" aria-label="Runs" data-testid="project-runs-list">
        <li
          v-for="(r, i) in state.rows"
          :key="r.execution_id || `fenced-${i}`"
          class="rounded-lg border px-3 py-2.5"
          :class="r.fenced ? 'border-dashed border-gray-200 dark:border-gray-750 bg-gray-50 dark:bg-gray-900 opacity-70' : 'border-gray-200 dark:border-gray-750 bg-white dark:bg-gray-800'"
          :data-testid="r.fenced ? 'project-run-fenced' : 'project-run'"
        >
          <div class="flex flex-wrap items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
            <BaseBadge :variant="statusVariant(r.status)" size="sm" dot>{{ statusText(r.status) }}</BaseBadge>
            <span class="font-medium text-gray-800 dark:text-gray-200">{{ r.agent_name }}</span>
            <template v-if="!r.fenced">
              <span class="font-mono">{{ r.model_used || 'unknown' }}</span>
              <span class="tabular-nums" data-testid="project-run-cost">{{ money(r.cost) }}</span>
              <span class="tabular-nums" title="Queue age" data-testid="project-run-queue">queued {{ ms(r.queue_age_ms) }}</span>
              <span class="tabular-nums" title="Duration">ran {{ ms(r.duration_ms) }}</span>
              <span v-if="r.started_at || r.queued_at" :title="formatLocalDateTime(r.started_at || r.queued_at)">{{ formatRelativeTime(r.started_at || r.queued_at) }}</span>
            </template>
            <span v-else class="italic">A run you can't open</span>
          </div>
          <p v-if="!r.fenced && r.task" class="mt-1 truncate text-sm text-gray-800 dark:text-gray-200" data-testid="project-run-task">
            <span class="font-mono text-xs text-gray-500 dark:text-gray-400">{{ r.task.id }}</span> {{ r.task.title }}
          </p>
        </li>
      </ol>

      <div v-if="state.rows.length" class="flex flex-wrap items-center justify-between gap-2 text-xs text-gray-500 dark:text-gray-400">
        <span><span class="tabular-nums">{{ state.rows.length }}</span> {{ state.rows.length === 1 ? 'run' : 'runs' }} shown<template v-if="state.retention_days"> · runs are kept for {{ state.retention_days }} days</template></span>
        <BaseButton v-if="state.next_cursor" variant="secondary" size="sm" :loading="state.loading" loading-label="Loading…" data-testid="project-runs-more" @click="more">Load more</BaseButton>
      </div>
    </template>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseCard from '@/components/base/BaseCard.vue'
import BaseSelect from '@/components/base/BaseSelect.vue'
import SkeletonLoader from '@/components/SkeletonLoader.vue'
import LoadFailed from '@/components/LoadFailed.vue'
import InlineError from '@/components/InlineError.vue'
import { useProjectsStore } from '@/stores/projects'
import { viewState } from '@/utils/loadingState'
import { formatRelativeTime, formatLocalDateTime } from '@/utils/timestamps'

const props = defineProps({
  projectId: { type: String, required: true },
  // The agents on the project, for the agent filter (names or {agent_name}).
  agents: { type: Array, default: () => [] },
})

const STATUSES = ['queued', 'running', 'success', 'failed', 'error', 'cancelled', 'skipped']
const STATUS_VARIANT = Object.freeze({
  queued: 'neutral', running: 'info', success: 'success', failed: 'danger', error: 'danger', cancelled: 'warning', skipped: 'neutral',
})
const WINDOWS = [7, 30, 90]

const store = useProjectsStore()
const agent = ref('')
const status = ref('')
const windowDays = ref('30')

const state = computed(() => store.runs[props.projectId] || { rows: [], totals: null, next_cursor: null, loaded: false, loading: false, error: null })
const view = computed(() => viewState({ hasLoaded: state.value.loaded, error: state.value.error, count: state.value.rows.length }))
const totals = computed(() => state.value.totals)
const costLabel = computed(() => ((totals.value?.fenced_runs || 0) > 0 ? 'Cost of runs you can open' : 'Cost'))
const agentOptions = computed(() => {
  const names = props.agents.map((a) => (typeof a === 'string' ? a : a?.agent_name)).filter(Boolean)
  return [...new Set(names)].sort()
})

function filters() {
  const since = new Date(Date.now() - Number(windowDays.value) * 86400000).toISOString()
  return { since, agent: agent.value || undefined, status: status.value || undefined }
}
function load() { return store.fetchRuns(props.projectId, filters()) }
function more() { return store.fetchRuns(props.projectId, state.value.filters || filters(), { more: true }) }
watch([() => props.projectId, agent, status, windowDays], load, { immediate: true })

function statusVariant(s) { return STATUS_VARIANT[s] || 'neutral' }
function statusText(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : 'Unknown' }
function money(v) { return typeof v === 'number' ? `$${v.toFixed(v < 1 ? 3 : 2)}` : '—' }
function ms(v) {
  if (typeof v !== 'number') return '—'
  if (v < 1000) return `${v} ms`
  const s = v / 1000
  if (s < 60) return `${s.toFixed(1)} s`
  const m = Math.floor(s / 60)
  return m < 60 ? `${m}m ${Math.round(s % 60)}s` : `${Math.floor(m / 60)}h ${m % 60}m`
}
function runsLine(t) {
  const n = t.runs || 0
  const f = t.fenced_runs || 0
  return `${n} ${n === 1 ? 'run' : 'runs'}${f ? ` · ${f} you can't open` : ''}`
}
</script>
