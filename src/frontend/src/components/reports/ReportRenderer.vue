<template>
  <component :is="rendererComponent" :payload="payload" />
</template>

<script setup>
import { computed } from 'vue'
import ReportTable from './ReportTable.vue'
import ReportKpiTiles from './ReportKpiTiles.vue'
import ReportMarkdown from './ReportMarkdown.vue'
import ReportTimeline from './ReportTimeline.vue'
import ReportJson from './ReportJson.vue'

// Picks a renderer by: explicit display_hint -> report_type prefix -> JSON.
// Each typed renderer requires a minimal payload shape; on mismatch we fall
// back to the JSON viewer so a malformed payload degrades instead of throwing
// (review/Codex #10).
const props = defineProps({
  reportType: { type: String, default: '' },
  displayHint: { type: String, default: null },
  payload: { type: [Object, Array], default: () => ({}) },
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

const resolvedHint = computed(() => {
  let hint = props.displayHint
  if (!hint || !VALID_HINTS.includes(hint)) hint = prefixHint(props.reportType)
  if (!shapeOk(hint, props.payload)) hint = 'json'
  return hint
})

const rendererComponent = computed(() => COMPONENTS[resolvedHint.value] || ReportJson)
</script>
