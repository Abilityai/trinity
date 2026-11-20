<script setup>
/**
 * Overview › Activity (trinity-enterprise#1317).
 *
 * Runs per day as one curve, with the day's failures as a second curve on the
 * same scale beneath it, so "how busy" and "how much of it failed" are read off
 * one picture. What started the runs is a part of the WINDOW, not of a day, so
 * it is one split bar under the chart with a count and share per source (a
 * day's own split is in the readout on hover).
 *
 * The chart keeps one footprint in every state: a scanline track while there
 * is no data yet, a dashed ghost curve when the read succeeded and nothing has
 * run, the failed state when it did not, and the curves.
 *
 * The line under the title is the window's summary and stays put. One day's
 * breakdown, hovered or reached with the arrow keys, is a label beside the
 * guide INSIDE the plot — nothing is appended outside the panel, so nothing can
 * stay on screen after the page is left (#3591), and reading a day moves
 * nothing else on the page.
 */
import { computed, ref, useId, watch } from 'vue'
import BaseCard from '../base/BaseCard.vue'
import BaseSegmented from '../base/BaseSegmented.vue'
import LoadFailed from '../LoadFailed.vue'
import ScanlineReveal from '../ScanlineReveal.vue'
import { BUCKET_COLORS } from '@/utils/executionBuckets'
import { CHART_HEIGHT, DASH, GHOST_ACTIVITY, activityColumns, activityReadout, dayLabel, hasRuns, sourceSplit } from '@/utils/overviewShape'

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
const viewBox = `0 0 1000 ${CHART_HEIGHT}`
const gradientId = useId()
// A dot per day is legible up to two weeks; beyond that only the hovered day gets one.
const showDots = computed(() => columns.value.length <= 14)
const activeColumn = computed(() => (active.value == null ? null : columns.value[active.value] || null))

const active = ref(null)
watch(() => [props.window, props.stats], () => { active.value = null })

// The line under the title is the WINDOW's summary and does not change while a
// day is read: the day's own figures sit in a label beside the guide, so
// moving across the chart moves nothing else on the page.
const readout = computed(() => {
  if (props.state === 'loading' || props.state === 'failed') return { head: '—', text: '' }
  return activityReadout(props.stats, null, { never: props.never })
})

const dayNote = computed(() => {
  const col = activeColumn.value
  if (!col) return null
  return {
    label: col.label,
    runs: col.total ? `${col.total} ${col.total === 1 ? 'run' : 'runs'} · ${col.failed ? `${col.failed} failed` : 'none failed'}` : 'no runs',
    sources: col.segments.map((s) => `${s.count} ${s.bucket}`).join(' · '),
    // Opens away from the nearer edge so it never leaves the plot.
    side: col.xPct > 50 ? 'left' : 'right',
    xPct: col.xPct,
  }
})

const split = computed(() => (quiet.value || props.state !== 'ready' ? [] : sourceSplit(props.stats)))
const legend = computed(() => {
  if (split.value.length) {
    return split.value.map((s) => ({ name: s.bucket, color: colorFor(s.bucket), figure: `${s.count} · ${s.pct}%` }))
  }
  return DEFAULT_KEYS.map((name) => ({ name, color: colorFor(name), figure: DASH }))
})
const splitLabel = computed(() => split.value.map((s) => `${s.bucket} ${s.count}`).join(', '))

// The 7d window (eight UTC days) fits a name each; longer windows get first, middle and Today.
const axis = computed(() => {
  const days = columns.value
  if (!days.length) return null
  if (days.length <= 8) {
    return { each: days.map((d, i) => (i === days.length - 1 ? 'Today' : d.label.split(',')[0])) }
  }
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
            class="relative h-[146px] border-b border-gray-200 dark:border-gray-700"
            role="img"
            :aria-label="chartLabel"
            data-testid="overview-activity-ghost"
          >
            <svg class="absolute inset-0 h-full w-full text-gray-300 dark:text-gray-600" :viewBox="viewBox" preserveAspectRatio="none" aria-hidden="true">
              <path :d="GHOST_ACTIVITY" fill="none" stroke="currentColor" stroke-width="1.5" stroke-dasharray="5 5" vector-effect="non-scaling-stroke" />
            </svg>
          </div>
          <div
            v-else
            class="relative h-[146px] rounded border-b border-gray-200 outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:border-gray-700"
            tabindex="0"
            role="img"
            :aria-label="chartLabel"
            data-testid="overview-activity-plot"
            @keydown="onKeydown"
            @blur="active = null"
            @mouseleave="active = null"
          >
            <span class="absolute left-0 top-0 text-[11px] tabular-nums text-gray-500 dark:text-gray-400">{{ shaped.max }} a day</span>
            <svg
              class="absolute inset-0 h-full w-full text-action-primary-500 dark:text-action-primary-400"
              :viewBox="viewBox"
              preserveAspectRatio="none"
              aria-hidden="true"
            >
              <defs>
                <linearGradient :id="gradientId" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0" stop-color="currentColor" stop-opacity="0.26" />
                  <stop offset="1" stop-color="currentColor" stop-opacity="0" />
                </linearGradient>
              </defs>
              <path :d="shaped.area" :fill="`url(#${gradientId})`" />
              <path :d="shaped.line" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" vector-effect="non-scaling-stroke" />
              <g class="text-status-danger-500">
                <path :d="shaped.failArea" fill="currentColor" fill-opacity="0.14" />
                <path :d="shaped.failLine" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" vector-effect="non-scaling-stroke" />
              </g>
            </svg>
            <i
              v-if="activeColumn"
              class="pointer-events-none absolute inset-y-0 w-px bg-gray-300 dark:bg-gray-600"
              :style="{ left: activeColumn.xPct + '%' }"
            ></i>
            <template v-for="(col, i) in columns" :key="col.date">
              <i
                v-if="showDots ? col.total : active === i"
                class="pointer-events-none absolute h-[7px] w-[7px] -translate-x-1/2 -translate-y-1/2 rounded-full bg-action-primary-500 ring-2 ring-white dark:bg-action-primary-400 dark:ring-gray-800"
                :style="{ left: col.xPct + '%', top: col.y + 'px' }"
                data-testid="overview-activity-dot"
              ></i>
              <i
                v-if="col.failed"
                class="pointer-events-none absolute h-[7px] w-[7px] -translate-x-1/2 -translate-y-1/2 rounded-full bg-status-danger-500 ring-2 ring-white dark:ring-gray-800"
                :style="{ left: col.xPct + '%', top: col.failY + 'px' }"
                data-testid="overview-activity-fail"
              ></i>
            </template>
            <div
              class="pointer-events-none absolute top-0 z-10"
              :style="dayNote ? { left: dayNote.xPct + '%' } : { left: 0 }"
              aria-live="polite"
            >
              <div
                v-if="dayNote"
                class="absolute top-0 whitespace-nowrap rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-[12.5px] shadow-sm dark:border-gray-700 dark:bg-gray-800"
                :class="dayNote.side === 'left' ? 'right-2' : 'left-2'"
                data-testid="overview-activity-day"
              >
                <p class="font-medium text-gray-900 dark:text-gray-100">{{ dayNote.label }}</p>
                <p class="tabular-nums text-gray-600 dark:text-gray-300">{{ dayNote.runs }}</p>
                <p v-if="dayNote.sources" class="tabular-nums text-gray-500 dark:text-gray-400">{{ dayNote.sources }}</p>
              </div>
            </div>
            <!-- One hit slot per day, the full height of the plot. -->
            <div class="absolute inset-0 flex">
              <span
                v-for="(col, i) in columns"
                :key="col.date"
                class="h-full min-w-0 flex-1"
                data-testid="overview-activity-col"
                @mouseenter="active = i"
              ></span>
            </div>
          </div>
        </div>
      </template>
    </ScanlineReveal>

    <div class="mt-1 flex h-4 justify-between text-[11px] tabular-nums text-gray-500 dark:text-gray-400">
      <template v-if="axis && !quiet && state === 'ready'">
        <template v-if="axis.each">
          <span v-for="(name, i) in axis.each" :key="i" class="min-w-0 flex-1 text-center">{{ name }}</span>
        </template>
        <template v-else>
          <span>{{ axis.first }}</span><span>{{ axis.mid }}</span><span>Today</span>
        </template>
      </template>
      <template v-else-if="state !== 'failed'">
        <span>{{ window }} ago</span><span>Today</span>
      </template>
    </div>

    <!-- What started the window's runs: one bar, the same footprint with or without data. -->
    <div class="mt-4" data-testid="overview-activity-sources">
      <div class="flex items-center justify-between gap-3 text-[11px] text-gray-500 dark:text-gray-400">
        <span>What started them</span>
        <span class="flex items-center gap-3">
          <span class="inline-flex items-center gap-1.5"><i class="inline-block h-0.5 w-3 rounded bg-action-primary-500 dark:bg-action-primary-400"></i>Runs</span>
          <span class="inline-flex items-center gap-1.5"><i class="inline-block h-0.5 w-3 rounded bg-status-danger-500"></i>Failed</span>
        </span>
      </div>
      <div v-if="split.length" class="mt-1.5 flex h-2 gap-[2px]" role="img" :aria-label="splitLabel" data-testid="overview-activity-split">
        <i
          v-for="s in split"
          :key="s.bucket"
          class="block h-2 min-w-[3px] basis-0 rounded-sm"
          :style="{ flexGrow: s.count, backgroundColor: colorFor(s.bucket) }"
        ></i>
      </div>
      <div v-else class="mt-1.5 h-2 rounded-sm border border-dashed border-gray-300 dark:border-gray-600" aria-hidden="true"></div>
      <ul class="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[12.5px] text-gray-600 dark:text-gray-300">
        <li v-for="k in legend" :key="k.name" class="inline-flex items-center gap-1.5 whitespace-nowrap">
          <i class="inline-block h-2 w-2 rounded-sm" :style="{ backgroundColor: k.color }"></i>{{ k.name }}
          <span class="tabular-nums text-gray-500 dark:text-gray-400">{{ k.figure }}</span>
        </li>
      </ul>
    </div>

    <p v-if="stale" class="mt-2 text-[12.5px] text-status-warning-700 dark:text-status-warning-400" data-testid="overview-activity-stale">
      Couldn't refresh. Showing the last activity that loaded.
    </p>
  </BaseCard>
</template>
