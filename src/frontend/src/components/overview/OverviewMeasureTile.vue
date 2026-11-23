<script setup>
/**
 * Overview › one measure tile (trinity-enterprise#1317).
 *
 * Label, value, one supporting line, a small line chart — at one footprint in
 * every state. With nothing measured the value is `—` and the chart is a dashed
 * ghost curve; while the first read is out the chart zone is the scanline
 * track. Days with no runs are gaps in the line, never points at zero.
 */
import { computed } from 'vue'
import BaseCard from '../base/BaseCard.vue'
import ScanlineReveal from '../ScanlineReveal.vue'
import { DASH, GHOST_LINE, linePaths } from '@/utils/overviewShape'

const props = defineProps({
  /** 'loading' | 'failed' | 'empty' | 'ready' — from `viewState`. */
  state: { type: String, required: true },
  label: { type: String, required: true },
  windowLabel: { type: String, default: '' },
  value: { type: String, default: DASH },
  sub: { type: String, default: '' },
  /** One value per day; `null` for a day with nothing measured. */
  series: { type: Array, default: () => [] },
  hi: { type: Number, default: 100 },
  /** Draw a dashed guide at this value (same scale as the series). */
  guide: { type: Number, default: null },
  aria: { type: String, default: '' },
})

const HEIGHT = 48
const paths = computed(() => linePaths(props.series, { lo: 0, hi: props.hi, height: HEIGHT }))
const measured = computed(() => props.state === 'ready' && props.value !== DASH && !!paths.value.line)
const face = computed(() => {
  if (props.state === 'loading') return { value: DASH, sub: '' }
  if (props.state === 'failed') return { value: DASH, sub: "couldn't load" }
  return { value: props.value, sub: props.sub }
})
const guideTop = computed(() => (props.guide == null ? null : paths.value.yFor(props.guide)))
</script>

<template>
  <BaseCard :data-testid="`overview-tile-${label.toLowerCase().replace(/\s+/g, '-')}`">
    <div class="flex items-baseline justify-between gap-2">
      <span class="text-[11px] font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400">{{ label }}</span>
      <span class="text-[11px] tabular-nums text-gray-500 dark:text-gray-400">{{ windowLabel }}</span>
    </div>
    <p
      class="mt-1 text-2xl font-bold tabular-nums"
      :class="measured ? 'text-gray-900 dark:text-gray-100' : 'text-gray-500 dark:text-gray-400'"
      data-testid="overview-tile-value"
    >{{ face.value }}</p>
    <p class="min-h-[18px] text-[12.5px] tabular-nums text-gray-500 dark:text-gray-400" data-testid="overview-tile-sub">{{ face.sub }}</p>

    <ScanlineReveal class="mt-3 block h-[48px]" :loading="state === 'loading'" :reveal="measured">
      <template v-if="state !== 'loading'">
        <div class="relative h-[48px]" role="img" :aria-label="measured ? aria : `${label}: no data yet`">
          <template v-if="measured">
            <span
              v-if="guideTop != null"
              class="absolute inset-x-0 border-t border-dashed border-gray-300 dark:border-gray-600"
              :style="{ top: guideTop + 'px' }"
              data-testid="overview-tile-guide"
            ></span>
            <svg class="absolute inset-0 h-full w-full overflow-visible" viewBox="0 0 200 48" preserveAspectRatio="none" aria-hidden="true">
              <path :d="paths.area" class="fill-action-primary-500/10 stroke-none"></path>
              <path
                :d="paths.line"
                class="fill-none stroke-action-primary-500 dark:stroke-action-primary-400"
                stroke-width="2"
                stroke-linejoin="round"
                stroke-linecap="round"
                vector-effect="non-scaling-stroke"
                data-testid="overview-tile-line"
              ></path>
            </svg>
            <span
              class="absolute -ml-1 -mt-1 h-2 w-2 rounded-full bg-action-primary-500 ring-2 ring-white dark:bg-action-primary-400 dark:ring-gray-800"
              :style="{ left: paths.last.xPct + '%', top: paths.last.y + 'px' }"
            ></span>
          </template>
          <!-- Nothing measured: the outline of the chart to come. -->
          <svg
            v-else
            class="absolute inset-0 h-full w-full"
            viewBox="0 0 200 48"
            preserveAspectRatio="none"
            aria-hidden="true"
            data-testid="overview-tile-ghost"
          >
            <path
              :d="GHOST_LINE"
              class="fill-none stroke-gray-300 dark:stroke-gray-600"
              stroke-width="2"
              stroke-dasharray="4 4"
              vector-effect="non-scaling-stroke"
            ></path>
          </svg>
        </div>
      </template>
    </ScanlineReveal>
  </BaseCard>
</template>
