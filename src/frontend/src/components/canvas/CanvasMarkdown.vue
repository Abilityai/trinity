<template>
  <div class="space-y-3">
    <template v-for="(seg, i) in segments" :key="i">
      <!-- Prose goes through the shared report markdown renderer (DOMPurify). -->
      <ReportRenderer
        v-if="seg.type === 'markdown'"
        display-hint="markdown"
        :payload="{ markdown: seg.text }"
      />
      <!-- Figures use the SAME leaves the standalone kinds use — never a
           second renderer. This component deliberately has no dependency on
           the block dispatcher: fences do not nest, and a cycle here would be
           the fork this file exists to avoid. -->
      <CanvasChart v-else-if="seg.type === 'chart' && chartFor(seg)" :model="chartFor(seg)" />
      <CanvasDiagram v-else-if="seg.type === 'diagram'" :source="seg.payload.mermaid" />
      <ReportRenderer
        v-else-if="seg.type === 'kpi' || seg.type === 'table'"
        :display-hint="seg.type"
        :payload="seg.payload"
      />
    </template>
  </div>
</template>

<script setup>
/**
 * A markdown block with figures fenced inside it (ent#536) — the explainer
 * pattern: one write, a narrative page with charts in it. The split is
 * `canvasUtils.splitRichFences` (pure, tested); this file only maps segments
 * onto components.
 */
import ReportRenderer from '../reports/ReportRenderer.vue'
import CanvasChart from './CanvasChart.vue'
import CanvasDiagram from './CanvasDiagram.vue'
import { chartModel } from './canvasUtils'

defineProps({
  segments: { type: Array, required: true },
})

function chartFor(seg) {
  return chartModel(seg.payload)
}
</script>
