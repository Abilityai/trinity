<!--
  The project's Health card (trinity-enterprise#661 v3.3): the steward's (or
  creator's) read — on track / at risk / off track and a line — and whether an
  update is due. One of the Overview's cards; the page places it (#3694).
-->
<template>
  <BaseCard data-testid="project-health">
    <div class="flex items-center gap-2">
      <h2 class="flex-1 text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Health</h2>
      <BaseButton v-if="canSetHealth" size="sm" variant="ghost" data-testid="project-health-update" @click="healthOpen = true">Update</BaseButton>
    </div>
    <template v-if="health">
      <BaseBadge class="mt-2" :variant="HEALTH_BADGE[health.state] || 'neutral'" dot>{{ healthLabel(health.state) }}</BaseBadge>
      <p v-if="health.note" class="mt-2 text-sm text-gray-800 dark:text-gray-200 break-words">{{ health.note }}</p>
      <p class="mt-2 text-xs text-gray-500 dark:text-gray-400">
        {{ authorLabel(health.by, myEmail) }} · <span :title="absolute(health.at)">{{ relative(health.at) }}</span>
      </p>
    </template>
    <p v-else class="mt-2 text-sm text-gray-600 dark:text-gray-300">
      No health update yet. {{ canSetHealth ? 'Say how it is going: on track, at risk or off track.' : 'The steward posts one.' }}
    </p>
    <p v-if="health && rollup.health_due" class="mt-2 text-xs font-medium text-status-warning-700 dark:text-status-warning-300" data-testid="project-health-due">
      Update due: the last one is over two weeks old.
    </p>
    <ProjectHealthModal v-model="healthOpen" :project-id="project.id" :current="health" />
  </BaseCard>
</template>

<script setup>
import { computed, ref } from 'vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseCard from '@/components/base/BaseCard.vue'
import ProjectHealthModal from './ProjectHealthModal.vue'
import { formatRelativeTime, formatLocalDateTime } from '@/utils/timestamps'
import { HEALTH_BADGE, healthLabel, authorLabel } from './projectsUtils'

const props = defineProps({
  project: { type: Object, required: true },
  myEmail: { type: String, default: '' },
})

const healthOpen = ref(false)
const rollup = computed(() => props.project.rollup || { by_status: {} })
const health = computed(() => rollup.value.health || null)
const canSetHealth = computed(() => props.project.can?.set_health === true)
const relative = (t) => (t ? formatRelativeTime(t) : '')
const absolute = (t) => (t ? formatLocalDateTime(t) : '')
</script>
