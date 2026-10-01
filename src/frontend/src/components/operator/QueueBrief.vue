<!--
  trinity-enterprise#610 §3g L6 — the E2 render seam: an ask's agent-authored
  brief, beside the decision it explains (the QueueProposal pattern). Two parts,
  because they sit on either side of the options:

    part="lead"      Why now → "{agent} recommends"   (above the options)
    part="fallback"  "If you don't answer by {time}"  (beside the answer row)

  Each section renders only when present (`briefOf`), and agent text goes
  through PortalMarkdown only — DOMPurify is the XSS boundary; this file binds
  no raw HTML of its own. The per-option `impact` is rendered by the card's own option
  buttons (`briefImpactFor`), not here. L8 stores and projects the brief and
  mounts this on the operator's cards; until then only fixtures fill it.
-->
<template>
  <div v-if="sections.length" class="space-y-1.5" :data-testid="`queue-brief-${part}`">
    <div v-for="s in sections" :key="s.key" :data-testid="`queue-brief-${s.testid}`">
      <p class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">{{ s.label }}</p>
      <PortalMarkdown :content="s.text" class="min-w-0 text-[12.5px] text-gray-700 dark:text-gray-300" />
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import PortalMarkdown from '../portal/PortalMarkdown.vue'
import { briefOf, ifNoAnswerLabel } from '../../utils/operatorQueue'

const props = defineProps({
  // The ask / queue item carrying `brief` (and `expires_at` for the fallback).
  item: { type: Object, default: null },
  part: { type: String, default: 'lead', validator: (v) => v === 'lead' || v === 'fallback' },
  // Who recommends — the agent's display name.
  agentLabel: { type: String, default: '' },
})

const sections = computed(() => {
  const b = briefOf(props.item)
  if (!b) return []
  if (props.part === 'fallback') {
    return b.if_no_answer
      ? [{ key: 'if_no_answer', testid: 'if-no-answer', label: ifNoAnswerLabel(props.item?.expires_at), text: b.if_no_answer }]
      : []
  }
  const out = []
  if (b.why) out.push({ key: 'why', testid: 'why', label: 'Why now', text: b.why })
  if (b.recommendation) {
    const who = props.agentLabel || props.item?.agent_name || 'The agent'
    out.push({ key: 'recommendation', testid: 'recommendation', label: `${who} recommends`, text: b.recommendation })
  }
  return out
})
</script>
