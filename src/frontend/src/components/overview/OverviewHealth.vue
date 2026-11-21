<script setup>
/**
 * Overview › Health (trinity-enterprise#1317).
 *
 * The verdict, a reachability strip, and the few facts worth a glance: latency,
 * restarts, sync. The strip is exactly as long as the checks the history read
 * returned and says how far back that is — it is hours at the default cadence,
 * so it never borrows a "7 days" label it cannot back.
 *
 * With no checks at all (monitoring off, or a brand-new agent) the strip is a
 * row of dashed placeholders at the same footprint, and the card says who can
 * turn monitoring on.
 */
import { computed } from 'vue'
import BaseCard from '../base/BaseCard.vue'
import BaseBadge from '../base/BaseBadge.vue'
import LoadFailed from '../LoadFailed.vue'
import ScanlineReveal from '../ScanlineReveal.vue'
import { formatSyncSummary, syncChip, syncPillClass, syncTextClass } from '@/utils/syncSummary'
import { healthBlocks } from '@/utils/overviewShape'

const props = defineProps({
  /** 'loading' | 'failed' | 'empty' | 'ready' for the health detail read. */
  state: { type: String, required: true },
  /** AgentHealthDetail, or null. */
  health: { type: Object, default: null },
  /** Whether the history read has landed (the strip's own "no data yet"). */
  historyLoaded: { type: Boolean, default: false },
  /** The history read failed before it ever loaded. */
  historyFailed: { type: Boolean, default: false },
  /** Network checks, newest N. */
  checks: { type: Array, default: () => [] },
  /** The `/git/sync-state` payload, or null when unread or failed. */
  sync: { type: Object, default: null },
  isAdmin: { type: Boolean, default: false },
  now: { type: Number, default: () => Date.now() },
})
const emit = defineEmits(['retry'])

const GHOST_BLOCKS = 24
const strip = computed(() => healthBlocks(props.checks, { blocks: GHOST_BLOCKS, now: props.now }))

const verdict = computed(() => {
  const s = (props.health?.aggregate_status || '').toLowerCase()
  if (s === 'healthy') return { label: 'Healthy', variant: 'success' }
  if (s === 'degraded') return { label: 'Degraded', variant: 'warning' }
  if (s === 'unhealthy') return { label: 'Unhealthy', variant: 'danger' }
  return { label: 'Unknown', variant: 'neutral' }
})

const uptime = computed(() => {
  const v = props.health?.uptime_percent_24h
  return v == null ? '—' : `${Math.round(v * 10) / 10}%`
})
const latency = computed(() => {
  const v = props.health?.avg_latency_24h_ms
  return v == null ? '—' : `${Math.round(v)} ms`
})
// Nothing at all is known about reachability: only then does the card say
// monitoring is not recording. A current reading with no history yet is not
// "no health checks".
const unmeasured = computed(() => props.health?.uptime_percent_24h == null && props.health?.avg_latency_24h_ms == null)
const restarts = computed(() => {
  const v = props.health?.docker?.restart_count
  return v == null ? '—' : String(v)
})

// `—` until the backend has an observation (and after a failed read): never
// `ok` for an agent nobody has measured (trinity-enterprise#707).
const syncText = computed(() => formatSyncSummary(props.sync, props.now) ?? '—')
const syncTitle = computed(() => syncChip(props.sync, props.now)?.title ?? 'Sync status unknown')

const BLOCK = {
  ok: 'h-7 bg-status-success-500/80',
  degraded: 'h-4 bg-status-warning-500',
  down: 'h-2 bg-status-danger-500',
  none: 'h-7 border border-dashed border-gray-300 dark:border-gray-600',
}
</script>

<template>
  <BaseCard data-testid="overview-health">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <h3 class="text-sm font-semibold text-gray-900 dark:text-gray-100">Health</h3>
      <div v-if="state === 'ready'" class="flex flex-wrap items-center gap-1.5">
        <BaseBadge :variant="verdict.variant" size="sm" dot data-testid="overview-health-verdict">{{ verdict.label }}</BaseBadge>
        <BaseBadge v-if="health?.circuit_breaker?.open" variant="danger" size="sm">Circuit open, see header</BaseBadge>
        <BaseBadge v-if="health?.docker?.oom_killed" variant="danger" size="sm">Out of memory</BaseBadge>
      </div>
    </div>

    <LoadFailed
      v-if="state === 'failed'"
      class="mt-3"
      dense
      title="Couldn't load health"
      message="The health check did not answer. This says nothing about the agent itself."
      @retry="emit('retry')"
    />

    <template v-else>
      <p class="mt-3 flex items-baseline gap-2">
        <span
          class="text-2xl font-bold leading-none tabular-nums"
          :class="uptime === '—' ? 'text-gray-500 dark:text-gray-400' : 'text-gray-900 dark:text-gray-100'"
          data-testid="overview-health-uptime"
        >{{ uptime }}</span>
        <span class="text-[12.5px] text-gray-500 dark:text-gray-400">reachable, last 24h</span>
      </p>

      <ScanlineReveal class="mt-3 block h-[28px]" :loading="!historyLoaded && !historyFailed" :reveal="!!strip">
        <template v-if="historyLoaded || historyFailed">
          <div
            class="flex h-[28px] items-end gap-[2px]"
            :role="strip ? 'img' : undefined"
            :aria-label="strip ? `Reachability since ${strip.spanLabel}` : undefined"
            :aria-hidden="strip ? undefined : 'true'"
            data-testid="overview-health-strip"
          >
            <template v-if="strip">
              <i v-for="(b, i) in strip.blocks" :key="i" class="block flex-1 rounded-sm" :class="BLOCK[b.state]" :title="b.title"></i>
            </template>
            <template v-else>
              <i v-for="i in GHOST_BLOCKS" :key="i" class="block flex-1 rounded-sm" :class="BLOCK.none"></i>
            </template>
          </div>
        </template>
      </ScanlineReveal>
      <p class="mt-1 flex h-4 justify-between text-[11px] tabular-nums text-gray-500 dark:text-gray-400">
        <template v-if="strip"><span>{{ strip.spanLabel }}</span><span>Now</span></template>
      </p>

      <p v-if="historyFailed" class="mt-3 text-[12.5px] text-gray-500 dark:text-gray-400" data-testid="overview-health-history-failed">
        Couldn't load the reachability history.
      </p>
      <p
        v-else-if="historyLoaded && !strip && unmeasured"
        class="mt-3 text-[12.5px] text-gray-500 dark:text-gray-400"
        data-testid="overview-health-off"
      >
        No health checks recorded for this agent yet, so reachability and latency are not shown.
        <router-link
          v-if="isAdmin"
          :to="{ path: '/operations', query: { tab: 'health' } }"
          class="font-medium text-action-primary-600 hover:underline dark:text-action-primary-400"
        >Turn monitoring on in Operations</router-link>
        <template v-else>An admin can turn monitoring on in Operations.</template>
      </p>

      <div class="mt-4 grid grid-cols-2 gap-3">
        <p>
          <span class="block text-sm font-medium tabular-nums text-gray-900 dark:text-gray-100" data-testid="overview-health-latency">{{ latency }}</span>
          <span class="block text-[11px] text-gray-500 dark:text-gray-400">avg latency, 24h</span>
        </p>
        <p>
          <span class="block text-sm font-medium tabular-nums text-gray-900 dark:text-gray-100">{{ restarts }}</span>
          <span class="block text-[11px] text-gray-500 dark:text-gray-400">restarts</span>
        </p>
      </div>
    </template>

    <!-- Outside the health branch: the sync line has its own read and keeps its
         footprint whatever the health read did. -->
    <p class="mt-3">
      <span
        data-testid="overview-sync"
        :title="syncTitle"
        class="inline-block max-w-full truncate rounded-md px-2.5 py-1 text-xs tabular-nums text-gray-600 dark:text-gray-300"
        :class="syncPillClass(sync) || 'bg-gray-100 dark:bg-gray-700'"
      >Sync: <span data-testid="overview-sync-value" :class="syncTextClass(sync)">{{ syncText }}</span></span>
    </p>
  </BaseCard>
</template>
