<!--
  The Steward card (trinity-enterprise#896): who moves this project, and the
  state behind that — in place of the bare header label.

  An agent steward acts through its OWN scheduled sweep (Trinity does not run
  it), so the card says honestly what the read carries: the last sweep (the
  agent's last digest read, stamped server-side), its schedule (`none` → "No
  sweep scheduled", `scheduled` → the next run, `hidden` → "Scheduled" without
  a time the viewer may not read, `unknown` → the read failed), and health.
  A backend that predates those fields omits the lines rather than guessing.

  A person steward sees what waits on them (tasks to verify, stuck tasks —
  from the roll-up the page already has) and, when it is me, the way to the
  "I steward" tab.
-->
<template>
  <BaseCard data-testid="project-steward">
    <h2 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Steward</h2>
    <div class="mt-2 flex items-center gap-2">
      <PortalAvatar v-if="steward?.kind === 'agent'" :name="steward.ref" :size="24" />
      <p class="min-w-0 flex-1 truncate text-sm font-medium text-gray-900 dark:text-gray-100">{{ who }}</p>
      <BaseBadge v-if="health" :variant="HEALTH_BADGE[health.state] || 'neutral'" dot>{{ healthLabel(health.state) }}</BaseBadge>
    </div>
    <p v-if="!steward" class="mt-2 text-sm text-gray-600 dark:text-gray-300">The creator stewards it until someone is named.</p>

    <template v-else-if="steward.kind === 'agent'">
      <ul class="mt-2 space-y-1 text-sm text-gray-700 dark:text-gray-300">
        <li v-if="'last_sweep_at' in steward" data-testid="project-steward-sweep">
          <template v-if="steward.last_sweep_at">Last sweep <span :title="absolute(steward.last_sweep_at)">{{ relative(steward.last_sweep_at) }}</span></template>
          <template v-else>No sweep recorded yet</template>
        </li>
        <li v-if="steward.schedule" data-testid="project-steward-schedule">{{ scheduleLine }}<span v-if="scheduleAt" :title="absolute(scheduleAt)"> {{ relative(scheduleAt) }}</span></li>
        <li v-if="!health">No health update yet</li>
        <li v-if="rollup.health_due" class="font-medium text-status-warning-700 dark:text-status-warning-300" data-testid="project-steward-health-due">Health update due</li>
      </ul>
      <p class="mt-3 text-xs text-gray-500 dark:text-gray-400">
        It reads this project's digest on its own schedule, verifies finished tasks and records health. Trinity doesn't run it.
      </p>
    </template>

    <template v-else>
      <ul v-if="waiting.length" class="mt-2 space-y-1 text-sm text-gray-700 dark:text-gray-300">
        <li v-for="line in waiting" :key="line.key" :data-testid="`project-steward-${line.key}`">{{ line.text }}</li>
        <li v-if="rollup.health_due" class="font-medium text-status-warning-700 dark:text-status-warning-300" data-testid="project-steward-health-due">Health update due</li>
      </ul>
      <p v-else class="mt-2 text-sm text-gray-600 dark:text-gray-300">
        {{ rollup.health_due ? 'Health update due.' : 'Nothing is waiting on the steward.' }}
      </p>
      <p class="mt-3 text-xs text-gray-500 dark:text-gray-400">
        The steward verifies finished tasks, unblocks stuck ones and says how it's going.
      </p>
      <BaseButton v-if="isMe" class="mt-3" size="sm" variant="secondary" data-testid="project-steward-open-stewarding" @click="$emit('open-stewarding')">Open I steward</BaseButton>
    </template>
  </BaseCard>
</template>

<script setup>
import { computed } from 'vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseCard from '@/components/base/BaseCard.vue'
import PortalAvatar from '@/components/portal/PortalAvatar.vue'
import { formatRelativeTime, formatLocalDateTime } from '@/utils/timestamps'
import { HEALTH_BADGE, healthLabel, attentionLines, stewardScheduleLine } from './projectsUtils'

const props = defineProps({
  project: { type: Object, required: true },
  myEmail: { type: String, default: '' },
})
defineEmits(['open-stewarding'])

const steward = computed(() => props.project.steward || null)
const rollup = computed(() => props.project.rollup || { by_status: {} })
const health = computed(() => rollup.value.health || null)
const isMe = computed(() => steward.value?.kind === 'person' && Boolean(props.myEmail) && steward.value.ref === props.myEmail)
const who = computed(() => {
  const s = steward.value
  if (!s) return 'Not set'
  if (s.kind === 'agent') return `${s.ref} (agent)`
  return isMe.value ? 'You' : s.ref
})
const scheduleLine = computed(() => stewardScheduleLine(steward.value?.schedule))
const scheduleAt = computed(() => (steward.value?.schedule?.state === 'scheduled' ? steward.value.schedule.next_run_at : null))
// What waits on a person steward: the task lines only — health has its own
// line here, asks have their own card.
const waiting = computed(() => attentionLines({ ...rollup.value, open_asks: 0, health_due: false, quiet: false })
  .filter((l) => l.key === 'verify' || l.key === 'stuck'))

const relative = (t) => (t ? formatRelativeTime(t) : '')
const absolute = (t) => (t ? formatLocalDateTime(t) : '')
</script>
