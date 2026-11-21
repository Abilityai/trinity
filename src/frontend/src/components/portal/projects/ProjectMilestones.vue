<!--
  The goal's milestones on the Overview (trinity-enterprise#1233).

  Directly under the goal, one row per milestone in the goal's order: its
  state (not started · in progress · reached), the number against its target
  (the milestone's worst target when it has several), how fresh that reading
  is, its tasks done, and what on it waits on the viewer. A reading older than
  twice its cadence is greyed and says it is stale — never drawn as current.
  The latest health update sits above the list, greyed when it is overdue.
  A row opens the Tasks tab filtered to that milestone (`open-milestone`).

  No track across the top: milestones run side by side, so there is no
  implied sequence beyond the order the creator chose. The list is bounded
  (it scrolls inside itself past a dozen rows).

  Only a person who can change the goal (`can.edit`) adds, edits or marks a
  milestone reached; the server enforces it. This component only asks: the
  dialog lives on the page (`edit-milestone`, with null for a new one).
  Marking reached is always a person's act — targets met only suggest it.
-->
<template>
  <section class="space-y-3" aria-labelledby="project-milestones-title" data-testid="project-milestones">
    <div
      v-if="health"
      class="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm"
      :data-overdue="healthDue ? 'true' : null"
      data-testid="project-milestones-health"
    >
      <BaseBadge :variant="healthDue ? 'neutral' : (HEALTH_BADGE[health.state] || 'neutral')" dot size="sm">{{ healthLabel(health.state) }}</BaseBadge>
      <span v-if="health.note" class="min-w-0 break-words" :class="healthDue ? 'text-gray-500 dark:text-gray-400' : 'text-gray-800 dark:text-gray-200'">{{ health.note }}</span>
      <span class="text-xs text-gray-500 dark:text-gray-400">
        {{ authorLabel(health.by, myEmail) }} · <span :title="absolute(health.at)">{{ relative(health.at) }}</span>
        <template v-if="healthDue"> · Update overdue</template>
      </span>
    </div>

    <div class="flex items-center gap-2">
      <h2 id="project-milestones-title" class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Milestones</h2>
      <span class="text-xs tabular-nums text-gray-500 dark:text-gray-400" data-testid="project-milestones-count">{{ reachedCount }} of {{ milestones.length }} reached</span>
      <span class="flex-1"></span>
      <BaseButton v-if="canEdit" size="sm" variant="ghost" data-testid="project-milestone-add" @click="$emit('edit-milestone', null)">Add milestone</BaseButton>
    </div>

    <ol
      class="max-h-[34rem] overflow-y-auto divide-y divide-gray-100 rounded-lg border border-gray-200 bg-white dark:divide-gray-750 dark:border-gray-750 dark:bg-gray-800"
      data-testid="project-milestone-list"
    >
      <li v-for="row in rows" :key="row.m.id" class="flex items-center gap-2 pr-2" :data-testid="`project-milestone-${row.m.id}`">
        <button
          type="button"
          class="flex min-w-0 flex-1 items-center gap-3 px-3 py-2.5 text-left hover:bg-gray-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:hover:bg-gray-750"
          :aria-label="`${row.m.name} — ${row.state.label}. Open its tasks`"
          :data-testid="`project-milestone-open-${row.m.id}`"
          @click="$emit('open-milestone', row.m.id)"
        >
          <BaseBadge class="flex-none" :variant="row.state.badge" dot size="sm" :data-testid="`project-milestone-state-${row.m.id}`">{{ row.state.label }}</BaseBadge>
          <span class="min-w-0 flex-1">
            <span class="block truncate text-sm font-[550] text-gray-900 dark:text-gray-100">{{ row.m.name }}</span>
            <span v-if="row.m.reached_means" class="block truncate text-xs text-gray-500 dark:text-gray-400" :title="row.m.reached_means">{{ row.m.reached_means }}</span>
          </span>
          <span
            v-if="row.line"
            class="flex-none text-right"
            :data-reading="row.line.reading"
            :data-testid="`project-milestone-number-${row.m.id}`"
          >
            <span
              class="block text-sm tabular-nums"
              :class="row.line.greyed ? 'text-gray-500 dark:text-gray-400' : 'text-gray-900 dark:text-gray-100'"
              :title="row.line.at ? `Measured ${absolute(row.line.at)}` : ''"
            >{{ row.line.value }} <span class="text-gray-500 dark:text-gray-400">{{ row.line.relation }} {{ row.line.target }}</span><span v-if="row.line.unit" class="text-gray-500 dark:text-gray-400"> {{ row.line.unit }}</span></span>
            <span class="block max-w-[12rem] truncate text-xs text-gray-500 dark:text-gray-400">{{ row.line.metric }}<template v-if="row.line.more"> · +{{ row.line.more }} more</template></span>
            <span
              v-if="row.line.note"
              class="block text-xs text-gray-500 dark:text-gray-400"
              :data-testid="`project-milestone-reading-${row.m.id}`"
            >{{ row.line.note }}</span>
          </span>
          <span class="hidden w-28 flex-none text-right text-xs tabular-nums text-gray-600 dark:text-gray-300 sm:block" :data-testid="`project-milestone-tasks-${row.m.id}`">{{ row.tasks || 'No tasks yet' }}</span>
          <BaseBadge v-if="row.needsYou" class="flex-none" size="sm" variant="warning" :data-testid="`project-milestone-needs-you-${row.m.id}`">{{ row.needsYou }}</BaseBadge>
        </button>
        <BaseButton
          v-if="canEdit && row.m.suggest_reached"
          size="sm"
          variant="secondary"
          :loading="reaching === row.m.id"
          loading-label="Marking…"
          :data-testid="`project-milestone-reach-${row.m.id}`"
          @click="markReached(row.m)"
        >Mark reached</BaseButton>
        <BaseButton
          v-if="canEdit"
          size="sm"
          variant="ghost"
          :aria-label="`Edit ${row.m.name}`"
          :data-testid="`project-milestone-edit-${row.m.id}`"
          @click="$emit('edit-milestone', row.m)"
        >Edit</BaseButton>
      </li>
    </ol>
    <InlineError v-if="error" :message="error" @dismiss="error = ''" />
  </section>
</template>

<script setup>
import { computed, ref } from 'vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import InlineError from '@/components/InlineError.vue'
import { useProjectsStore } from '@/stores/projects'
import { formatRelativeTime, formatLocalDateTime } from '@/utils/timestamps'
import {
  HEALTH_BADGE, healthLabel, authorLabel, projectErrorMessage,
  milestoneState, milestoneNumberLine, milestoneTasksLine, milestoneNeedsYou,
} from './projectsUtils'

const props = defineProps({
  project: { type: Object, required: true },
  myEmail: { type: String, default: '' },
})
defineEmits(['open-milestone', 'edit-milestone'])

const store = useProjectsStore()
const milestones = computed(() => props.project.milestones || [])
const canEdit = computed(() => props.project.can?.edit === true && !props.project.archived_at)
const rows = computed(() => milestones.value.map((m) => ({
  m,
  state: milestoneState(m),
  line: milestoneNumberLine(m),
  tasks: milestoneTasksLine(m),
  needsYou: milestoneNeedsYou(m, props.project),
})))
const reachedCount = computed(() => milestones.value.filter((m) => m.state === 'reached').length)
const rollup = computed(() => props.project.rollup || {})
const health = computed(() => rollup.value.health || null)
const healthDue = computed(() => rollup.value.health_due === true)
const relative = (t) => (t ? formatRelativeTime(t) : '')
const absolute = (t) => (t ? formatLocalDateTime(t) : '')

const reaching = ref('')
const error = ref('')
async function markReached(m) {
  error.value = ''
  reaching.value = m.id
  try {
    await store.updateMilestone(props.project.id, m.id, { reached: true })
  } catch (err) {
    error.value = projectErrorMessage(err)
  } finally {
    reaching.value = ''
  }
}
</script>
