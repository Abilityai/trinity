<template>
  <div v-if="widget.metric" class="mt-2 space-y-1" data-testid="bound-mark">
    <!-- The binding failed: say so instead of showing a number. The backend
         removes `value` for an undeclared metric (a wrong number is worse than
         no number), so without this the widget would render a bare em dash and
         the operator would have no idea the widget names a metric at all. -->
    <p
      v-if="widget.binding_error"
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
    </div>
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
 * `stale` is the backend's verdict (the ONE rule in
 * `services/metric_read_service.freshness`), not a comparison done here. Note
 * this is the PER-METRIC flag on the widget, not the panel's top-level
 * `dashboardData.stale`, which means "served from the dashboard cache" — two
 * different facts that must never be rendered as one.
 */
import { computed } from 'vue'
import { formatRelativeTime, formatLocalDateTime } from '../utils/timestamps'
import { freshnessChip } from '../utils/metricFormat'

const props = defineProps({
  widget: { type: Object, required: true },
})

const chip = computed(() => freshnessChip(props.widget))
const relative = computed(() => formatRelativeTime(props.widget.last_point_at))
const absolute = computed(() => formatLocalDateTime(props.widget.last_point_at))
</script>
