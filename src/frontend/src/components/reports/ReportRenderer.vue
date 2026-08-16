<template>
  <component
    :is="rendererComponent"
    :payload="payload"
    :meta="meta"
    :load-more="loadMore"
    v-bind="fallbackProps"
  />
</template>

<script setup>
import { computed } from 'vue'
import ReportTable from './ReportTable.vue'
import ReportKpiTiles from './ReportKpiTiles.vue'
import ReportMarkdown from './ReportMarkdown.vue'
import ReportTimeline from './ReportTimeline.vue'
import ReportJson from './ReportJson.vue'

// Picks a renderer by: explicit display_hint -> report_type prefix -> fallback.
// Each typed renderer requires a minimal payload shape; on mismatch we fall
// back to the JSON viewer so a malformed payload degrades instead of throwing
// (review/Codex #10).
//
// #2162: the fallback is OVERRIDABLE per surface, and the operator default is
// unchanged. A raw JSON dump is a feature when you are debugging an agent's own
// output and a defect when the reader is that agent's customer — so the
// Workspace passes `ReportSummary` and every operator call site passes nothing.
// The override covers an agent-chosen `display_hint: "json"` as well as a shape
// mismatch, which matters: `json` is a valid value in the MCP tool's enum, so a
// portal that only replaced the mismatch path would still dump on request.
const props = defineProps({
  reportType: { type: String, default: '' },
  displayHint: { type: String, default: null },
  payload: { type: [Object, Array], default: () => ({}) },
  // #1537 paging handles, forwarded to ReportTable; every other renderer
  // ignores them (Vue drops unknown props onto the root, which is harmless for
  // a null default).
  meta: { type: Object, default: null },
  loadMore: { type: Function, default: null },
  // Rendered instead of ReportJson wherever the dispatch lands on `json` —
  // by mismatch OR by the agent asking for it. Null default keeps every
  // operator call site byte-identical.
  fallbackComponent: { type: [Object, Function], default: null },
})

const COMPONENTS = {
  table: ReportTable,
  kpi: ReportKpiTiles,
  markdown: ReportMarkdown,
  timeline: ReportTimeline,
  json: ReportJson,
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

const rendererComponent = computed(() => {
  if (resolved.value.hint === 'json') return props.fallbackComponent || ReportJson
  return COMPONENTS[resolved.value.hint] || props.fallbackComponent || ReportJson
})

// Bound only when a custom fallback is what renders. Passing it unconditionally
// would land `fallback="false"` on a typed renderer's root element as a stray
// DOM attribute (Vue only drops null/undefined, not false), and ReportJson
// declares no such prop.
const fallbackProps = computed(() => (
  props.fallbackComponent && rendererComponent.value === props.fallbackComponent
    ? { fallback: resolved.value.mismatch }
    : {}
))
</script>
