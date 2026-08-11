<template>
  <InfoTile
    scope="Fleet"
    title="Fleet summary"
    :stamp="`${agents.length} agent${agents.length === 1 ? '' : 's'}`"
    :state="agents.length ? 'ready' : 'empty'"
    empty-title="No agents yet"
    empty-hint="Create an agent and it appears on this board."
    :link-to="{ name: 'Dashboard', query: { view: 'list' } }"
    link-label="Open the fleet →"
  >
    <template #icon>
      <svg viewBox="0 0 24 24" width="18" height="18">
        <rect x="3" y="3" width="7" height="7" rx="1.5"></rect>
        <rect x="14" y="3" width="7" height="7" rx="1.5"></rect>
        <rect x="3" y="14" width="7" height="7" rx="1.5"></rect>
        <rect x="14" y="14" width="7" height="7" rx="1.5"></rect>
      </svg>
    </template>

    <dl class="fs">
      <div v-for="s in stats" :key="s.label" class="fs-item">
        <dt>{{ s.label }}</dt>
        <dd :class="s.tone">{{ s.value }}</dd>
      </div>
    </dl>
  </InfoTile>
</template>

<script setup>
import { computed } from 'vue'
import InfoTile from '../InfoTile.vue'

/**
 * Fleet summary — the chassis's REFERENCE tile (trinity-enterprise#325).
 *
 * Its job is to prove the chassis, not to be the interesting tile: it reads
 * the `agents` array `FleetGrid` already holds and issues no request, so the
 * widget foundation carries no data dependency of its own and could not be
 * blocked behind the sibling `GET /api/executions/timeline` work (ent#326).
 *
 * The real catalog arrives one sub-issue at a time (#95 host resources, #96
 * executions, #97 Cornelius mind, #98 cost, #99 next schedules, #100 recent
 * failures, #101 tokens, #259 subscription pressure).
 */
const props = defineProps({
  agents: { type: Array, default: () => [] },
})

const stats = computed(() => {
  const total = props.agents.length
  const running = props.agents.filter((a) => a.status === 'running').length
  const autonomous = props.agents.filter((a) => a.autonomy_enabled).length
  return [
    { label: 'Running', value: `${running}/${total}`, tone: running ? 'ok' : 'muted' },
    { label: 'Autonomous', value: String(autonomous), tone: 'muted' },
    { label: 'Stopped', value: String(total - running), tone: total - running ? 'warn' : 'muted' },
  ]
})
</script>

<style scoped>
.fs {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
  margin: 0;
}
.fs-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.fs-item dt {
  font-size: 11px;
  color: var(--gv-muted, #6b7280);
  white-space: nowrap;
}
.fs-item dd {
  margin: 0;
  font-size: 22px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  color: var(--gv-text, #111827);
}
.fs-item dd.ok { color: var(--gv-green-text, #16a34a); }
.fs-item dd.warn { color: var(--gv-yellow-text, #a16207); }
.fs-item dd.muted { color: var(--gv-text, #111827); }
</style>
