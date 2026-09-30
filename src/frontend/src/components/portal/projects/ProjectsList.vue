<!--
  The person's Projects list (trinity-enterprise#661) — the main door, across
  every agent.

  Shows projects I'm a member of plus every Company project. "My chats" counts
  only my own chats. Loading, empty, failed and stale are four different
  states (principle 15): empty needs a succeeded read that returned nothing,
  and a failed refresh keeps the rows under a stale banner.
-->
<template>
  <div class="mx-auto w-full max-w-5xl px-4 sm:px-8 py-6 space-y-4" data-testid="projects-list">
    <header class="flex items-center gap-3">
      <h1 class="text-2xl font-bold text-gray-900 dark:text-gray-100">Projects</h1>
      <span class="flex-1"></span>
      <template v-if="portal.isPlatformSession">
        <BaseButton variant="secondary" data-testid="projects-import" @click="importOpen = true">Import</BaseButton>
        <BaseButton data-testid="projects-new" @click="createOpen = true">New project</BaseButton>
      </template>
    </header>

    <div class="flex flex-wrap items-center gap-3">
      <OverflowTabs v-if="portal.isPlatformSession" class="min-w-0" :tabs="FILTERS" :model-value="filter" dense @update:model-value="filter = $event" />
      <BaseToggle v-model="showArchived" label="Show archived" />
      <span class="flex-1"></span>
      <BaseInput id="projects-search" v-model="query" class="w-full sm:w-64" placeholder="Search by name" aria-label="Search projects" />
    </div>

    <SkeletonLoader v-if="view.state === 'loading'" :count="4" height="4.5rem" gap="0.75rem" />

    <LoadFailed
      v-else-if="view.state === 'failed'"
      title="Couldn't load your projects"
      :message="store.listError"
      :retrying="store.listLoading"
      @retry="store.fetchList()"
    />

    <template v-else>
      <InlineError v-if="view.stale" :message="store.listError" @dismiss="store.fetchList()" />

      <div v-if="view.state === 'empty'" class="py-12 text-center" data-testid="projects-empty">
        <p class="text-base font-semibold text-gray-900 dark:text-gray-100">No projects yet</p>
        <p class="mx-auto mt-1.5 max-w-[48ch] text-sm text-gray-600 dark:text-gray-300">
          A project gathers the chats and rooms about one piece of work, like a launch or a hire, so any agent on it can pick up where things stand.
        </p>
        <template v-if="portal.isPlatformSession">
          <BaseButton class="mt-4" @click="createOpen = true">New project</BaseButton>
          <p class="mt-3 text-xs text-gray-500 dark:text-gray-400">Or open a chat and use the Project button, or import one an agent keeps in its files.</p>
        </template>
        <p v-else class="mt-3 text-xs text-gray-500 dark:text-gray-400">Projects you're invited to appear here.</p>
      </div>

      <p v-else-if="!visible.length" class="py-8 text-center text-sm text-gray-600 dark:text-gray-300">
        No projects match. Try another tab or clear the search.
      </p>

      <ul v-else class="space-y-2">
        <li v-for="p in visible" :key="p.id">
          <button
            type="button"
            class="w-full text-left rounded-lg border border-gray-200 dark:border-gray-750 bg-white dark:bg-gray-800 p-4 hover:border-gray-300 dark:hover:border-gray-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
            :data-testid="`project-row-${p.id}`"
            @click="$emit('open', p.id)"
          >
            <div class="flex flex-wrap items-center gap-2">
              <span class="font-semibold text-gray-900 dark:text-gray-100">{{ p.name }}</span>
              <BaseBadge :variant="STATUS_BADGE[p.status] || 'neutral'" dot>{{ statusLabel(p.status) }}</BaseBadge>
              <BaseBadge v-if="p.guest" variant="info">Guest</BaseBadge>
              <BaseBadge v-else>{{ visibilityLabel(p.visibility) }}</BaseBadge>
              <BaseBadge v-if="p.archived_at" variant="locked">Archived</BaseBadge>
            </div>
            <p class="mt-1 line-clamp-2 text-sm text-gray-600 dark:text-gray-300">{{ p.goal }}</p>
            <p class="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-500 dark:text-gray-400">
              <span>{{ agentSummary(p) }}</span>
              <span><span class="tabular-nums">{{ p.my_chat_count }}</span> of your chats</span>
              <span :title="formatLocalDateTime(p.updated_at)">Updated {{ formatRelativeTime(p.updated_at) }}</span>
            </p>
          </button>
        </li>
      </ul>
      <p v-if="visible.length" class="text-xs text-gray-500 dark:text-gray-400">
        <span class="tabular-nums">{{ visible.length }}</span> {{ visible.length === 1 ? 'project' : 'projects' }}. Other people's chats are never listed.
      </p>
    </template>

    <ProjectImportModal v-model="importOpen" :agents="rosterAgents" @imported="(p) => $emit('open', p.id)" />
    <ProjectCreateModal v-model="createOpen" :agents="rosterAgents" :my-email="portal.clientEmail || ''" @created="(p) => $emit('open', p.id)" />
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseInput from '@/components/base/BaseInput.vue'
import BaseToggle from '@/components/base/BaseToggle.vue'
import OverflowTabs from '@/components/OverflowTabs.vue'
import SkeletonLoader from '@/components/SkeletonLoader.vue'
import LoadFailed from '@/components/LoadFailed.vue'
import InlineError from '@/components/InlineError.vue'
import ProjectCreateModal from './ProjectCreateModal.vue'
import ProjectImportModal from './ProjectImportModal.vue'
import { useProjectsStore } from '@/stores/projects'
import { useClientPortalStore } from '@/stores/clientPortal'
import { viewState } from '@/utils/loadingState'
import { formatRelativeTime, formatLocalDateTime } from '@/utils/timestamps'
import { STATUS_BADGE, statusLabel, visibilityLabel, filterProjects } from './projectsUtils'

const FILTERS = [
  { id: 'all', label: 'All I can see' },
  { id: 'member', label: "I'm a member" },
  { id: 'company', label: 'Company' },
]

defineEmits(['open'])

const store = useProjectsStore()
const portal = useClientPortalStore()
const filter = ref('all')
const query = ref('')
const showArchived = ref(false)
const createOpen = ref(false)
const importOpen = ref(false)

const rosterAgents = computed(() => portal.agents.map((a) => a.name))
const live = computed(() => store.list.filter((p) => showArchived.value || !p.archived_at))
const view = computed(() => viewState({
  hasLoaded: store.listLoaded, error: store.listError, count: live.value.length,
}))
const visible = computed(() => filterProjects(live.value, filter.value, query.value))

function agentSummary(p) {
  const names = (p.agents || []).map((a) => a.agent_name)
  if (!names.length) return 'No agents yet'
  return names.length <= 2 ? names.join(', ') : `${names.slice(0, 2).join(', ')} +${names.length - 2}`
}

onMounted(() => { void store.fetchList() })
</script>
