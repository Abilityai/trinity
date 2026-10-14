<!--
  "Where it stands" (trinity-enterprise#661 v3.3): tasks by status, and what
  needs attention (to verify, stuck, untouched for a week), each opening the
  Tasks tab. One of the Overview's cards; the page places it (#3694).
-->
<template>
  <BaseCard data-testid="project-rollup">
    <h2 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Where it stands</h2>
    <p class="mt-2 text-sm text-gray-800 dark:text-gray-200">{{ rollupLine(rollup.by_status) }}</p>
    <ul v-if="attention.length" class="mt-3 space-y-1.5">
      <li v-for="line in attention" :key="line.key">
        <button
          v-if="line.tasks"
          type="button"
          class="text-left text-sm text-action-primary-600 dark:text-action-primary-400 hover:underline"
          :data-testid="`project-attention-${line.key}`"
          @click="$emit('open-tasks')"
        >{{ line.text }}<span v-if="line.ids" class="text-gray-500 dark:text-gray-400"> · {{ line.ids }}</span></button>
        <p v-else class="text-sm text-gray-700 dark:text-gray-300" :data-testid="`project-attention-${line.key}`">{{ line.text }}</p>
      </li>
    </ul>
    <p v-else class="mt-3 text-sm text-gray-600 dark:text-gray-300">Nothing needs attention.</p>
    <p v-if="rollup.last_activity_at" class="mt-3 text-xs text-gray-500 dark:text-gray-400">
      Last activity <span :title="absolute(rollup.last_activity_at)">{{ relative(rollup.last_activity_at) }}</span>
    </p>
  </BaseCard>
</template>

<script setup>
import { computed } from 'vue'
import BaseCard from '@/components/base/BaseCard.vue'
import { formatRelativeTime, formatLocalDateTime } from '@/utils/timestamps'
import { rollupLine, attentionLines } from './projectsUtils'

const props = defineProps({
  project: { type: Object, required: true },
})
defineEmits(['open-tasks'])

const rollup = computed(() => props.project.rollup || { by_status: {} })

// The roll-up's lines, minus the ones the Overview shows elsewhere (health has
// its own card, asks have theirs); task lines name their ids and open Tasks.
const attention = computed(() => {
  const r = rollup.value
  const ids = (list) => (list || []).slice(0, 3).map((t) => t.id).join(', ') + ((list || []).length > 3 ? '…' : '')
  return attentionLines({ ...r, health_due: false, open_asks: 0 }).map((line) => {
    const list = { verify: r.awaiting_verification, stuck: r.stuck, stale: r.stale }[line.key]
    return list ? { ...line, tasks: true, ids: ids(list) } : line
  })
})

const relative = (t) => (t ? formatRelativeTime(t) : '')
const absolute = (t) => (t ? formatLocalDateTime(t) : '')
</script>
