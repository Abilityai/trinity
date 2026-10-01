<!--
  The rail's Projects tab (trinity-enterprise#661): the projects the chat's
  agent can work on (active, or waiting for its owner), a filter of the
  person's Projects list. The chat's own project is marked. Reads the shared
  projects store; the list is refreshed when the tab mounts.
-->
<template>
  <div class="space-y-3" data-testid="portal-rail-projects">
    <SkeletonLoader v-if="view.state === 'loading'" :count="3" height="4rem" gap="0.5rem" />

    <LoadFailed
      v-else-if="view.state === 'failed'"
      title="Couldn't load projects"
      :message="store.listError"
      :retrying="store.listLoading"
      dense
      @retry="store.fetchList()"
    />

    <template v-else>
      <InlineError v-if="view.stale" :message="store.listError" @dismiss="store.fetchList()" />

      <div v-if="view.state === 'empty'" class="py-6 text-center" data-testid="portal-rail-projects-empty">
        <p class="text-sm font-semibold text-gray-900 dark:text-gray-100">No projects with {{ who }} yet</p>
        <p class="mx-auto mt-1.5 max-w-[36ch] text-xs text-gray-500 dark:text-gray-400">
          Add {{ who }} to a project from its Members &amp; access, or make this chat a project from the chat header.
        </p>
        <BaseButton size="sm" variant="secondary" class="mt-4" @click="$emit('open-projects', null)">All projects</BaseButton>
      </div>

      <ul v-else class="space-y-2">
        <li v-for="p in rows" :key="p.id">
          <button
            type="button"
            class="w-full text-left rounded-lg border p-2.5 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
            :class="p.id === currentProjectId
              ? 'border-action-primary-300 bg-action-primary-50 dark:border-action-primary-700 dark:bg-action-primary-500/16'
              : 'border-gray-200 dark:border-gray-750 hover:border-gray-300 dark:hover:border-gray-700'"
            @click="$emit('open-projects', p.id)"
          >
            <div class="flex items-center gap-2">
              <span class="min-w-0 flex-1 truncate text-sm font-medium text-gray-900 dark:text-gray-100">{{ p.name }}</span>
              <BaseBadge :variant="STATUS_BADGE[p.status] || 'neutral'">{{ statusLabel(p.status) }}</BaseBadge>
            </div>
            <p class="mt-0.5 line-clamp-2 text-xs text-gray-600 dark:text-gray-300">{{ p.goal }}</p>
            <p class="mt-1 text-[11px] text-gray-500 dark:text-gray-400">
              <template v-if="p.id === currentProjectId">This chat · </template>
              <template v-if="agentState(p) === 'pending'">Waiting for {{ who }}'s owner · </template>
              <span class="tabular-nums">{{ p.my_chat_count }}</span> of your chats
            </p>
          </button>
        </li>
      </ul>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted } from 'vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import SkeletonLoader from '@/components/SkeletonLoader.vue'
import LoadFailed from '@/components/LoadFailed.vue'
import InlineError from '@/components/InlineError.vue'
import { useProjectsStore } from '@/stores/projects'
import { viewState } from '@/utils/loadingState'
import { STATUS_BADGE, statusLabel } from './projectsUtils'

const props = defineProps({
  participants: { type: Array, default: () => [] },
  currentProjectId: { type: String, default: null },
})
defineEmits(['open-projects'])

const store = useProjectsStore()
const agents = computed(() => new Set(props.participants))
const who = computed(() => (props.participants.length === 1 ? props.participants[0] : 'these agents'))

function agentState(p) {
  const hit = (p.agents || []).find((a) => agents.value.has(a.agent_name))
  return hit ? hit.state : null
}

const rows = computed(() => store.list.filter((p) =>
  !p.archived_at && (p.id === props.currentProjectId || ['active', 'pending'].includes(agentState(p)))))
const view = computed(() => viewState({
  hasLoaded: store.listLoaded, error: store.listError, count: rows.value.length,
}))

onMounted(() => { void store.fetchList() })
</script>
