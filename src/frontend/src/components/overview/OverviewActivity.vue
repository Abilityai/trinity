<script setup>
/**
 * Overview › Activity (trinity-enterprise#1317).
 *
 * Runs per day, stacked by what started them, with failures on their own rail
 * under the baseline (never a segment inside the stack — that would redefine
 * every other segment as "succeeded"; the ExecutionsTile rule).
 *
 * The chart keeps one footprint in every state: a scanline track while there
 * is no data yet, dashed ghost bars when the read succeeded and nothing has
 * run, the failed state when it did not, and the bars. A day with no runs
 * inside an active window is a baseline stub.
 *
 * There is no floating tooltip. The line under the title is reserved and shows
 * the window's summary at rest and one day's breakdown while that day is
 * hovered or reached with the arrow keys — nothing is appended outside the
 * panel, so nothing can stay on screen after the page is left (#3591).
 */
import { computed, ref, watch } from 'vue'
import BaseCard from '../base/BaseCard.vue'
import BaseSegmented from '../base/BaseSegmented.vue'
import LoadFailed from '../LoadFailed.vue'
import ScanlineReveal from '../ScanlineReveal.vue'
import { BUCKET_COLORS } from '@/utils/executionBuckets'
import { activityColumns, activityReadout, dayLabel, ghostColumns, hasRuns } from '@/utils/overviewShape'

const props = defineProps({
  /** 'loading' | 'failed' | 'empty' | 'ready' — from `viewState`. */
  state: { type: String, required: true },
  /** A refresh failed with data on screen. */
  stale: { type: Boolean, default: false },
  stats: { type: Object, default: null },
  window: { type: String, required: true },
  /** The agent has no runs at all, not merely none in this window. */
  never: { type: Boolean, default: false },
})
const emit = defineEmits(['update:window', 'retry'])

const WINDOWS = [
  { value: '7d', label: '7d' },
  { value: '14d', label: '14d' },
  { value: '30d', label: '30d' },
]
// What a new agent's legend teaches before the backend has named any bucket.
const DEFAULT_KEYS = ['Chat/Tasks', 'Scheduled', 'Channels', 'Agent-to-agent']

const quiet = computed(() => props.state === 'empty' || (props.state === 'ready' && !hasRuns(props.stats)))
const shaped = computed(() => activityColumns(props.stats))
const columns = computed(() => shaped.value.columns)
const ghosts = computed(() => ghostColumns(columns.value.length || Number.parseInt(props.window, 10) || 7))

const active = ref(null)
watch(() => [props.window, props.stats], () => { active.value = null })

const readout = computed(() => {
  if (props.state === 'loading') return { head: '—', text: '' }
  if (props.state === 'failed') return { head: '—', text: '' }
  const column = active.value == null ? null : columns.value[active.value] || null
  return activityReadout(props.stats, column, { never: props.never })
})

const legend = computed(() => {
  const names = !quiet.value && props.stats?.buckets?.length ? props.stats.buckets : DEFAULT_KEYS
  return names.map((name) => ({ name, color: colorFor(name) }))
})

const axis = computed(() => {
  const days = columns.value
  if (!days.length) return null
  return { first: dayLabel(days[0].date), mid: dayLabel(days[Math.floor(days.length / 2)].date) }
})

const chartLabel = computed(() => {
  if (quiet.value) return `No runs in the last ${props.window}`
  const s = props.stats || {}
  return `Runs per day over the last ${props.window}: ${s.total_executions || 0} runs, ${s.failed_count || 0} failed. Use the arrow keys to read one day.`
})

function colorFor(bucket) {
  return BUCKET_COLORS[bucket] || BUCKET_COLORS.Other
}

function onKeydown(event) {
  const last = columns.value.length - 1
  if (last < 0) return
  const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key]
  if (event.key === 'Home') active.value = 0
  else if (event.key === 'End') active.value = last
  else if (event.key === 'Escape') active.value = null
  else if (step) active.value = Math.min(last, Math.max(0, (active.value ?? (step > 0 ? -1 : last + 1)) + step))
  else return
  event.preventDefault()
}
</script>

<template>
  <BaseCard data-testid="overview-activity">
    <div class="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
      <h3 class="text-sm font-semibold text-gray-900 dark:text-gray-100">Activity</h3>
      <BaseSegmented
        :model-value="window"
        :options="WINDOWS"
        aria-label="Time window"
        @update:model-value="emit('update:window', $event)"
      />
    </div>

    <!-- The reserved readout: one line, whatever the state. -->
    <p
      class="mt-3 flex min-h-[32px] flex-wrap items-baseline gap-x-2 text-[12.5px] text-gray-500 dark:text-gray-400"
      aria-live="polite"
      data-testid="overview-activity-readout"
    >
      <span class="text-2xl font-bold leading-none tabular-nums text-gray-900 dark:text-gray-100">{{ readout.head }}</span>
      <span class="tabular-nums">{{ readout.text }}</span>
    </p>

    <LoadFailed
      v-if="state === 'failed'"
      class="h-[169px]"
      dense
      title="Couldn't load activity"
      message="The run history did not load. This is not the same as an agent with no runs."
      @retry="emit('retry')"
    />
    <!-- ONE persistent ScanlineReveal; the branch lives inside the slot and
         nothing renders in it while loading (the primitive does not hide its
         slot under the track). The zone and the inner box are both sized: the
         primitive's content wrapper has no height of its own. -->
    <ScanlineReveal v-else class="mt-3 block h-[157px]" :loading="state === 'loading'" :reveal="state === 'ready' && !quiet">
      <template v-if="state !== 'loading'">
        <div class="h-[157px]">
          <!-- Nothing has run: the outline of the chart to come, plainly not data. -->
          <div
            v-if="quiet"
            class="flex h-[146px] items-end gap-[3px] border-b border-gray-200 dark:border-gray-700"
            role="img"
            :aria-label="chartLabel"
            data-testid="overview-activity-ghost"
          >
            <span v-for="(h, i) in ghosts" :key="i" class="flex h-full min-w-0 flex-1 flex-col justify-end">
              <i
                class="mx-auto block w-full max-w-[36px] rounded-t border border-b-0 border-dashed border-gray-300 dark:border-gray-600"
                :style="{ height: h + 'px' }"
              ></i>
            </span>
          </div>
          <div
            v-else
            class="rounded outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
            tabindex="0"
            role="img"
            :aria-label="chartLabel"
            data-testid="overview-activity-plot"
            @keydown="onKeydown"
            @blur="active = null"
            @mouseleave="active = null"
          >
            <div class="relative flex h-[146px] items-end gap-[3px] border-b border-gray-200 dark:border-gray-700">
              <span class="absolute left-0 top-0 text-[11px] tabular-nums text-gray-500 dark:text-gray-400">{{ shaped.max }} a day</span>
              <span
                v-for="(col, i) in columns"
                :key="col.date"
                class="flex h-full min-w-0 flex-1 flex-col justify-end transition-opacity motion-reduce:transition-none"
                :class="active != null && active !== i ? 'opacity-40' : 'opacity-100'"
                data-testid="overview-activity-col"
                @mouseenter="active = i"
              >
                <i v-if="!col.total" class="mx-auto block h-[2px] w-full max-w-[36px] rounded-sm bg-gray-200 dark:bg-gray-700" data-testid="overview-activity-stub"></i>
                <span v-else class="mx-auto flex w-full max-w-[36px] flex-col-reverse gap-[2px]">
                  <i
                    v-for="seg in col.segments"
                    :key="seg.bucket"
                    class="block rounded-sm"
                    :style="{ height: seg.px + 'px', backgroundColor: colorFor(seg.bucket) }"
                  ></i>
                </span>
              </span>
            </div>
            <!-- Failure rail: its own row under the baseline, one slot per day,
                 hanging down from it. The mark is taller the more runs failed
                 that day, so "5 failed" over three marks is readable at a glance. -->
            <div class="flex h-[11px] items-start gap-[3px] pt-[3px]" aria-hidden="true">
              <span v-for="col in columns" :key="col.date" class="flex min-w-0 flex-1 justify-center">
                <i
                  v-if="col.failed"
                  class="block w-full max-w-[36px] rounded-sm bg-status-danger-500"
                  :style="{ height: col.failPx + 'px' }"
                  data-testid="overview-activity-fail"
                ></i>
              </span>
            </div>
          </div>
        </div>
      </template>
    </ScanlineReveal>

    <div class="mt-1 flex h-4 justify-between text-[11px] tabular-nums text-gray-500 dark:text-gray-400">
      <template v-if="axis && !quiet && state === 'ready'">
        <span>{{ axis.first }}</span><span>{{ axis.mid }}</span><span>Today</span>
      </template>
      <template v-else-if="state !== 'failed'">
        <span>{{ window }} ago</span><span>Today</span>
      </template>
    </div>

    <ul class="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[12.5px] text-gray-600 dark:text-gray-300">
      <li v-for="k in legend" :key="k.name" class="inline-flex items-center gap-1.5 whitespace-nowrap">
        <i class="inline-block h-2 w-2 rounded-sm" :style="{ backgroundColor: k.color }"></i>{{ k.name }}
      </li>
      <li class="inline-flex items-center gap-1.5 whitespace-nowrap">
        <i class="inline-block h-1 w-2 rounded-sm bg-status-danger-500"></i>Failed that day, taller for more
      </li>
    </ul>

    <p v-if="stale" class="mt-2 text-[12.5px] text-status-warning-700 dark:text-status-warning-400" data-testid="overview-activity-stale">
      Couldn't refresh. Showing the last activity that loaded.
    </p>
  </BaseCard>
</template>
