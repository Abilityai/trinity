<template>
  <!-- `data-canvas-block` / `data-canvas-kind` name the block for the gallery
       spec (#2583), which measures every block against its column. -->
  <section
    class="mb-4 last:mb-0 min-w-0"
    :data-canvas-block="block.id || null"
    :data-canvas-kind="block.kind"
  >
    <h4
      v-if="block.title"
      class="mb-1.5 break-words text-[11px] font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400"
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
         markup inside the markdown keeps its `ck-*` classes and nothing else.
         Only for a string: `marked()` throws on anything else, and a thrown
         render used to take the whole block with it (#2583) — a non-string
         falls through to the report dispatch, whose shape check lands on JSON. -->
    <CanvasProse v-else-if="block.kind === 'markdown' && markdownText !== null" :markdown="markdownText" />

    <!-- html — the kind the voice panel's update_panel writes. Sanitised
         through the shared DOMPurify path, never raw: this is agent-authored
         markup and, on a `roster` canvas, it reaches a customer's browser.
         Canvas mode (ent#537): only the design kit's classes and a bounded
         width survive; `<style>` and `id` are dropped. -->
    <div
      v-else-if="block.kind === 'html' && !renderFailed"
      class="prose prose-sm dark:prose-invert max-w-none min-w-0 [overflow-wrap:anywhere]"
      v-html="safeHtml"
    ></div>

    <!-- Everything else delegates to the SHARED report dispatch — reused, not
         forked, because those renderer keys are CI-pinned as the canonical
         contract (test_1535_report_prompt_guidance.py). `table` and
         `timeline` are the unbounded kinds among them, so on a canvas they get
         the bounded viewport the kit's own `ck-table-wrap` has (principle
         28): 200 agent-written rows, or forty events, scroll inside the block
         instead of making the surface 8,000px tall (#2583). -->
    <div v-else :class="BOUNDED_KINDS.includes(delegatedHint) ? 'max-h-[420px] overflow-auto' : null">
      <ReportRenderer
        :display-hint="delegatedHint"
        :payload="block.payload"
      />
    </div>
  </section>
</template>

<script setup>
import { computed, onErrorCaptured, ref } from 'vue'
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

// The delegated kinds that get a bounded viewport on a canvas (template above).
const BOUNDED_KINDS = ['table', 'timeline']

// The one failure a canvas must not have is a silently dropped block (the
// `blockRenderer` rule). A leaf that throws while rendering — a library fed a
// shape it refuses, a payload no guard above anticipated — would otherwise
// unmount this whole section and leave the surface looking complete while
// missing content. Catch it here and show the payload as JSON instead: the
// reader still sees the data, and the error stays inside this block (#2583).
const renderFailed = ref(false)
onErrorCaptured((err) => {
  if (renderFailed.value) return false
  renderFailed.value = true
  if (typeof console !== 'undefined') console.warn('[canvas] block render failed, showing JSON', props.block?.id, err)
  return false
})

const chart = computed(() => (!renderFailed.value && props.block?.kind === 'chart' ? chartModel(props.block?.payload) : null))

const diagramSource = computed(() => {
  if (renderFailed.value || props.block?.kind !== 'diagram') return ''
  const src = props.block?.payload?.mermaid
  return typeof src === 'string' && src.trim() ? src : ''
})

const image = computed(() => (!renderFailed.value && props.block?.kind === 'image' ? imageSource(props.block?.payload) : null))

// The markdown source as a string, or null when the payload holds none.
const markdownText = computed(() => {
  const md = props.block?.payload?.markdown
  return typeof md === 'string' ? md : null
})

// A markdown block with at least one renderable fence splits into prose and
// figures; one without takes the report path byte-for-byte, as before.
const richSegments = computed(() => {
  if (renderFailed.value || props.block?.kind !== 'markdown') return null
  const segments = splitRichFences(markdownText.value)
  return segments.some((s) => s.type !== 'markdown') ? segments : null
})

const safeHtml = computed(() => sanitizeCanvasHtml(props.block?.payload?.html || ''))

// A canvas kind whose payload could not make one, any kind the report
// dispatch does not know, and a block whose leaf threw, all land on `json` —
// the reader still sees the data.
const delegatedHint = computed(() =>
  !renderFailed.value && REPORT_DELEGATED_KINDS.includes(props.block?.kind) ? props.block.kind : 'json',
)
</script>
