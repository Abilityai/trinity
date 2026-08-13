<template>
  <component
    :is="rendererComponent"
    :payload="payload"
    :meta="meta"
    :load-more="loadMore"
    v-bind="summaryProps"
  />
</template>

<script setup>
import { computed } from 'vue'
import ReportTable from './ReportTable.vue'
import ReportKpiTiles from './ReportKpiTiles.vue'
import ReportMarkdown from './ReportMarkdown.vue'
import ReportTimeline from './ReportTimeline.vue'
import ReportSummary from './ReportSummary.vue'

// Picks a renderer by: explicit display_hint -> report_type prefix -> fallback.
// Each typed renderer requires a minimal payload shape; on mismatch we fall
// back so a malformed payload degrades instead of throwing (review/Codex #10).
//
// #2162: the fallback is now the human-readable ReportSummary rather than the
// raw JSON viewer. `json` is a valid agent-chosen display_hint, so the two ways
// of arriving there are DIFFERENT (a mismatch is a defect; a chosen `json` is
// not) and the summary is told which it was. Raw JSON is still one click away
// for operators, inside ReportSummary's disclosure; a surface that must never
// show a raw payload passes `allow-raw="false"` and the disclosure is gone.
const props = defineProps({
  reportType: { type: String, default: '' },
  displayHint: { type: String, default: null },
  payload: { type: [Object, Array], default: () => ({}) },
  // #1537 paging handles, forwarded to ReportTable; every other renderer
  // ignores them (Vue drops unknown props onto the root, which is harmless for
  // a null default).
  meta: { type: Object, default: null },
  loadMore: { type: Function, default: null },
  // Policy, forwarded to the fallback: may this surface disclose the raw
  // payload at all? Default true keeps every existing call site byte-identical.
  allowRaw: { type: Boolean, default: true },
})

const COMPONENTS = {
  table: ReportTable,
  kpi: ReportKpiTiles,
  markdown: ReportMarkdown,
  timeline: ReportTimeline,
  json: ReportSummary,
}

const VALID_HINTS = Object.keys(COMPONENTS)

function isObj(v) {
  return v && typeof v === 'object' && !Array.isArray(v)
}

// Minimal payload contract per hint (documented in feature-flows/agent-reports.md).
function shapeOk(hint, payload) {
  if (!isObj(payload) && hint !== 'json') return false
  switch (hint) {
    case 'table':
      return Array.isArray(payload.columns) && Array.isArray(payload.rows)
    case 'kpi':
      return Array.isArray(payload.tiles)
    case 'timeline':
      return Array.isArray(payload.events)
    case 'markdown':
      return typeof payload.markdown === 'string'
    case 'json':
    default:
      return true
  }
}

function prefixHint(type) {
  const t = type || ''
  if (t.startsWith('ops.')) return 'kpi'
  if (t.endsWith('.daily_brief') || t.endsWith('.coherence')) return 'markdown'
  if (t.includes('leads')) return 'table'
  if (t.startsWith('recon.')) return 'timeline'
  return 'json'
}

// Resolves the hint AND records how it got there. `json` reached by shape
// mismatch is a malformed report and says so; `json` chosen by the agent is not.
const resolved = computed(() => {
  let hint = props.displayHint
  if (!hint || !VALID_HINTS.includes(hint)) hint = prefixHint(props.reportType)
  if (!shapeOk(hint, props.payload)) return { hint: 'json', mismatch: true }
  return { hint, mismatch: false }
})

const rendererComponent = computed(() => COMPONENTS[resolved.value.hint] || ReportSummary)

// Bound only when the fallback is what renders. Passing these unconditionally
// would land `fallback="false"` on a typed renderer's root element as a stray
// DOM attribute (Vue only drops null/undefined, not false).
const summaryProps = computed(() => (
  rendererComponent.value === ReportSummary
    ? { fallback: resolved.value.mismatch, allowRaw: props.allowRaw }
    : {}
))
</script>
