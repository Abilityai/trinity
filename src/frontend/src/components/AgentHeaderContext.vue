<template>
  <!--
    trinity-enterprise#950 — a compact context-window chart for the agent
    header. Same numbers the fleet tile reads (`GET /api/agents/context-stats`
    via `stores/network.js::contextStats`); CPU, memory and cost stay on
    Overview.

    Hidden by the header for a stopped agent. One footprint for every state (principle 4): while stats have not arrived
    the ring is an empty track and the value reads "—", never a 0% ring that
    would claim an idle context nobody measured.
  -->
  <div
    class="inline-flex items-center gap-1.5 text-[12.5px] tabular-nums"
    :title="tooltip"
    role="img"
    :aria-label="ariaLabel"
    data-testid="agent-header-context"
  >
    <svg class="w-5 h-5 -rotate-90 shrink-0" viewBox="0 0 20 20" aria-hidden="true">
      <circle cx="10" cy="10" :r="R" fill="none" stroke-width="3" class="stroke-gray-200 dark:stroke-gray-700" />
      <circle
        v-if="percent !== null"
        cx="10" cy="10" :r="R"
        fill="none"
        stroke-width="3"
        stroke-linecap="round"
        :stroke-dasharray="`${dash} ${CIRC}`"
        :class="['transition-[stroke-dasharray] duration-500 motion-reduce:transition-none', levelStroke]"
        data-testid="agent-header-context-arc"
      />
    </svg>
    <span class="w-9 text-right" :class="percent === null ? 'text-gray-600 dark:text-gray-400' : levelText">
      {{ percent === null ? '—' : `${percent}%` }}
    </span>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  // `{ contextPercent, contextUsed, contextMax }` for this agent, or null
  // while the poll has not answered for it yet.
  stats: {
    type: Object,
    default: null
  }
})

const R = 7.5
const CIRC = 2 * Math.PI * R

// The backend answers 0 for an agent it could not probe (`activityState:
// 'offline'`, a failed or not-yet-run probe), so 0 used tokens is "no reading",
// not "empty context" — showing it as 0% would be the dead empty state the
// issue forbids. A genuinely fresh session reads "—" for the first poll.
const percent = computed(() => {
  const s = props.stats
  const p = s?.contextPercent
  if (typeof p !== 'number' || !Number.isFinite(p)) return null
  if (s.activityState === 'offline') return null
  if (!(typeof s.contextUsed === 'number' && s.contextUsed > 0)) return null
  return Math.max(0, Math.min(100, Math.round(p)))
})

const dash = computed(() => ((percent.value ?? 0) / 100) * CIRC)

// Same thresholds as the fleet tile (AgentTile.vue `contextLevelClass`), so one
// agent never reads "fine" on one surface and "hot" on the other.
const level = computed(() => {
  const p = percent.value ?? 0
  return p >= 85 ? 'hot' : p >= 60 ? 'mid' : 'ok'
})
const levelStroke = computed(() => ({
  hot: 'stroke-status-danger-500 dark:stroke-status-danger-400',
  mid: 'stroke-status-warning-500 dark:stroke-status-warning-400',
  ok: 'stroke-status-info-500 dark:stroke-status-info-400'
}[level.value]))
const levelText = computed(() => ({
  hot: 'text-status-danger-700 dark:text-status-danger-400',
  mid: 'text-status-warning-700 dark:text-status-warning-400',
  ok: 'text-gray-600 dark:text-gray-300'
}[level.value]))

const k = (n) => `${Math.round(n / 1000)}k`
const tooltip = computed(() => {
  if (percent.value === null) return 'Context window — no reading yet'
  const used = props.stats?.contextUsed
  const max = props.stats?.contextMax
  const tokens = typeof used === 'number' && typeof max === 'number' && max > 0
    ? ` — ${k(used)} / ${k(max)} tokens`
    : ''
  return `Context window ${percent.value}% used${tokens}`
})
const ariaLabel = computed(() =>
  percent.value === null ? 'Context window: no reading yet' : `Context window ${percent.value}% used`
)
</script>
