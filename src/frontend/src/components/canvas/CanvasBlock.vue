<template>
  <section class="mb-4 last:mb-0">
    <h4
      v-if="block.title"
      class="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400"
    >{{ block.title }}</h4>

    <!-- The canvas's own kinds (ent#438 / ent#536). Each leaf is the SAME
         component the rich fences inside a markdown block use, so a figure
         renders identically whether it stands alone or sits in prose. A payload
         that cannot make its kind falls through to the JSON renderer below
         rather than mounting an empty chart or a broken image — "no data" is a
         claim we have not earned. -->
    <CanvasChart v-if="block.kind === 'chart' && chart" :model="chart" />

    <CanvasDiagram v-else-if="block.kind === 'diagram' && diagramSource" :source="diagramSource" />

    <CanvasImage
      v-else-if="block.kind === 'image' && image"
      :image="image"
      :agent-name="agentName"
    />

    <CanvasMarkdown v-else-if="block.kind === 'markdown' && richSegments" :segments="richSegments" />

    <!-- markdown without figures — canvas prose (ent#537): the report
         markdown renderer's twin under the canvas-mode sanitiser, so raw kit
         markup inside the markdown keeps its `ck-*` classes and nothing else. -->
    <CanvasProse v-else-if="block.kind === 'markdown'" :markdown="block.payload?.markdown || ''" />

    <!-- html — the kind the voice panel's update_panel writes. Sanitised
         through the shared DOMPurify path, never raw: this is agent-authored
         markup and, on a `roster` canvas, it reaches a customer's browser.
         Canvas mode (ent#537): only the design kit's classes and a bounded
         width survive; `<style>` and `id` are dropped. -->
    <div
      v-else-if="block.kind === 'html'"
      class="prose prose-sm dark:prose-invert max-w-none"
      v-html="safeHtml"
    ></div>

    <!-- Everything else delegates to the SHARED report dispatch — reused, not
         forked, because those renderer keys are CI-pinned as the canonical
         contract (test_1535_report_prompt_guidance.py). -->
    <ReportRenderer
      v-else
      :display-hint="delegatedHint"
      :payload="block.payload"
    />
  </section>
</template>

<script setup>
import { computed } from 'vue'
import ReportRenderer from '../reports/ReportRenderer.vue'
import CanvasChart from './CanvasChart.vue'
import CanvasDiagram from './CanvasDiagram.vue'
import CanvasImage from './CanvasImage.vue'
import CanvasMarkdown from './CanvasMarkdown.vue'
import CanvasProse from './CanvasProse.vue'
import { sanitizeCanvasHtml } from '../../utils/markdown'
import {
  chartModel,
  imageSource,
  REPORT_DELEGATED_KINDS,
  splitRichFences,
} from './canvasUtils'

const props = defineProps({
  block: { type: Object, required: true },
  // The agent whose canvas this is — needed only to fetch a workspace-file
  // image through the authenticated preview route. Null on a surface that
  // cannot reach it, and the image block then says so instead of 401-ing.
  agentName: { type: String, default: null },
})

const chart = computed(() => (props.block?.kind === 'chart' ? chartModel(props.block?.payload) : null))

const diagramSource = computed(() => {
  if (props.block?.kind !== 'diagram') return ''
  const src = props.block?.payload?.mermaid
  return typeof src === 'string' && src.trim() ? src : ''
})

const image = computed(() => (props.block?.kind === 'image' ? imageSource(props.block?.payload) : null))

// A markdown block with at least one renderable fence splits into prose and
// figures; one without takes the report path byte-for-byte, as before.
const richSegments = computed(() => {
  if (props.block?.kind !== 'markdown') return null
  const segments = splitRichFences(props.block?.payload?.markdown)
  return segments.some((s) => s.type !== 'markdown') ? segments : null
})

const safeHtml = computed(() => sanitizeCanvasHtml(props.block?.payload?.html || ''))

// A canvas kind whose payload could not make one, and any kind the report
// dispatch does not know, both land on `json` — the reader still sees the data.
const delegatedHint = computed(() =>
  REPORT_DELEGATED_KINDS.includes(props.block?.kind) ? props.block.kind : 'json',
)
</script>
