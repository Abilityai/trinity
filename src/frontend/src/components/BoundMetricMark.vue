<template>
  <div v-if="widget.metric" class="mt-2 space-y-1" data-testid="bound-mark">
    <!-- The binding failed: say so instead of showing a number. The backend
         removes `value` for an undeclared metric (a wrong number is worse than
         no number), so without this the widget would render a bare em dash and
         the operator would have no idea the widget names a metric at all. -->
    <!-- ent#730: a selector that matched no recent series is often not an
         author error (the channel has not reported yet, or reports rarely),
         so it is a calm footer row, not warning ink: chip, the selector, "no
         recent data", then a hint written from the backend's facts. -->
    <template v-if="notFound">
      <div class="flex flex-wrap items-center gap-2 text-xs">
        <span
          class="inline-flex items-center rounded-full bg-gray-100 px-2 py-0.5 font-mono text-gray-600 dark:bg-gray-700 dark:text-gray-300"
          :title="`Bound to the declared metric '${widget.metric}' — the value comes from the registry, not from dashboard.yaml.`"
          data-testid="bound-chip"
        >{{ widget.metric }}</span>
        <span
          v-if="selectorText"
          class="min-w-0 break-words font-mono text-gray-500 dark:text-gray-400"
          title="The series named by dims in dashboard.yaml."
          data-testid="bound-series"
        >{{ selectorText }}</span>
        <span
          class="text-gray-500 dark:text-gray-400"
          data-testid="bound-not-found"
        >no recent data</span>
      </div>
      <p
        v-if="hint"
        class="text-xs text-gray-500 dark:text-gray-400 break-words"
        data-testid="bound-error-hint"
      >{{ hint }}</p>
    </template>
    <p
      v-else-if="widget.binding_error"
      class="text-xs text-status-warning-700 dark:text-status-warning-300"
      data-testid="bound-error"
    >
      {{ widget.binding_error }}
    </p>
    <div v-else class="flex flex-wrap items-center gap-2 text-xs">
      <span
        class="inline-flex items-center rounded-full bg-gray-100 px-2 py-0.5 font-mono text-gray-600 dark:bg-gray-700 dark:text-gray-300"
        :title="`Bound to the declared metric '${widget.metric}' — the value comes from the registry, not from dashboard.yaml.`"
        data-testid="bound-chip"
      >{{ widget.metric }}</span>
      <span
        v-if="widget.last_point_at"
        class="text-gray-500 dark:text-gray-400"
        :title="absolute"
        data-testid="bound-point-time"
      >{{ relative }}</span>
      <span
        v-if="chip"
        class="inline-flex items-center rounded-full px-2 py-0.5 font-medium"
        :class="chip.tone"
        :title="chip.title"
        data-testid="bound-freshness-chip"
      >{{ chip.label }}</span>
      <span
        v-else-if="!widget.last_point_at"
        class="text-gray-500 dark:text-gray-400"
        data-testid="bound-no-points"
      >declared, no points yet</span>
      <!-- ent#730: the threshold verdict (bound `metric` tiles only). On a
           judgeable tile its footprint is always present — `ok` is an
           invisible placeholder — so crossing a threshold swaps it in place.
           Every verdict word sits in the SAME grid cell and only the current
           one is visible, so the badge is as wide as the wider word in
           whatever font renders it: no state change resizes it. -->
      <BaseBadge
        v-if="verdict"
        :variant="verdict.variant"
        dot
        :class="verdict.reserved ? 'invisible' : ''"
        :aria-hidden="verdict.reserved ? 'true' : undefined"
        :title="verdict.title"
        data-testid="bound-verdict"
      ><span class="inline-grid justify-items-center"><span
        v-for="word in VERDICT_WORDS"
        :key="word"
        class="col-start-1 row-start-1"
        :class="word === verdict.label ? '' : 'invisible'"
        :aria-hidden="word === verdict.label ? undefined : 'true'"
        :data-testid="word === verdict.label ? 'bound-verdict-label' : undefined"
      >{{ word }}</span></span></BaseBadge>
      <!-- ent#730: which series the number is. Its own line (`w-full`), so a
           long label wraps under chip · time · freshness instead of pushing
           the freshness chip onto a later line. Mono on the `k=v` part only. -->
      <span
        v-if="seriesNote"
        class="w-full min-w-0 break-words"
        :class="seriesNote.tone === 'secondary' ? 'text-gray-600 dark:text-gray-300' : 'text-gray-500 dark:text-gray-400'"
        :title="seriesNote.title"
        data-testid="bound-series"
      >{{ seriesNote.text }}<span v-if="seriesNote.dims" class="font-mono">{{ seriesNote.dims }}</span></span>
    </div>
    <a
      v-if="dimsRefusal"
      :href="DIMS_DOCS_URL"
      target="_blank"
      rel="noopener noreferrer"
      class="inline-block text-xs text-action-primary-600 dark:text-action-primary-400 hover:underline"
      data-testid="bound-docs-link"
    >How dims: selects a series</a>
  </div>
</template>

<script setup>
/**
 * The freshness footer of a `dashboard.yaml` widget bound to a declared metric
 * (ent#479 C7).
 *
 * A bound widget's value comes from the metric registry rather than from the
 * number the author wrote in the file, so it must state WHEN that value was
 * recorded — the panel's own header timestamp describes when the dashboard was
 * fetched, which for a bound widget says nothing about the number on it.
 *
 * Its own component rather than three copies inline: the metric, status and
 * progress arms all take the binding, and `DashboardPanel.vue` is already a
 * 735-line #1031 candidate.
 *
 * ent#730: it also captions WHICH series a bound number is (from the
 * backend's `bound_series` facts, copy in `utils/metricFormat.js`), renders a
 * `metric_series_not_found` refusal as a neutral footer row with a hint built
 * from `binding_detail` facts, and links the docs on every `dims:` refusal.
 *
 * `stale` is the backend's verdict (the ONE rule in
 * `services/metric_read_service.freshness`), not a comparison done here. Note
 * this is the PER-METRIC flag on the widget, not the panel's top-level
 * `dashboardData.stale`, which means "served from the dashboard cache" — two
 * different facts that must never be rendered as one.
 */
import { computed } from 'vue'
import BaseBadge from './base/BaseBadge.vue'
import { formatRelativeTime, formatLocalDateTime } from '../utils/timestamps'
import {
  DIMS_DOCS_URL,
  DIMS_REFUSAL_CODES,
  VERDICT_WORDS,
  boundSeriesNote,
  formatDims,
  freshnessChip,
  refusalHint,
  verdictBadge,
} from '../utils/metricFormat'

const props = defineProps({
  widget: { type: Object, required: true },
})

const chip = computed(() => freshnessChip(props.widget))
const relative = computed(() => formatRelativeTime(props.widget.last_point_at))
const absolute = computed(() => formatLocalDateTime(props.widget.last_point_at))
const seriesNote = computed(() => boundSeriesNote(props.widget))
const notFound = computed(() => props.widget.binding_error_code === 'metric_series_not_found')
const selectorText = computed(() => formatDims(props.widget.binding_detail?.selector))
const hint = computed(() => refusalHint(props.widget))
const verdict = computed(() => verdictBadge(props.widget))
const dimsRefusal = computed(() => DIMS_REFUSAL_CODES.has(props.widget.binding_error_code))
</script>
