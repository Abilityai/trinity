<!--
  "I steward" (trinity-enterprise#661 v3.4): what needs me across the projects
  I steward — tasks to verify, stuck or untouched tasks, open asks, a health
  update due, a project gone quiet. The server lists only live projects I can
  open. An agent steward reads the same digest through MCP.
-->
<template>
  <div data-testid="projects-stewarding">
    <SkeletonLoader v-if="view.state === 'loading'" :count="3" height="4.5rem" gap="0.75rem" />
    <LoadFailed
      v-else-if="view.state === 'failed'"
      title="Couldn't load the projects you steward"
      :message="state.error"
      :retrying="state.loading"
      @retry="store.fetchStewarding()"
    />
    <template v-else>
      <InlineError v-if="view.stale" :message="state.error" @dismiss="store.fetchStewarding()" />
      <div v-if="view.state === 'empty'" class="py-12 text-center" data-testid="stewarding-empty">
        <p class="text-base font-semibold text-gray-900 dark:text-gray-100">You don't steward any projects</p>
        <p class="mx-auto mt-1.5 max-w-[48ch] text-sm text-gray-600 dark:text-gray-300">
          A project's steward keeps it moving: verifies finished tasks, unblocks stuck ones and says how it's going. You're the steward of projects you create, unless you name someone else.
        </p>
      </div>
      <ul v-else class="space-y-2">
        <li v-for="d in rows" :key="d.project.id">
          <button
            type="button"
            class="w-full text-left rounded-lg border border-gray-200 dark:border-gray-750 bg-white dark:bg-gray-800 p-4 hover:border-gray-300 dark:hover:border-gray-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
            :data-testid="`stewarding-row-${d.project.id}`"
            @click="$emit('open', d.project.id)"
          >
            <div class="flex flex-wrap items-center gap-2">
              <span class="font-semibold text-gray-900 dark:text-gray-100">{{ d.project.name }}</span>
              <BaseBadge v-if="d.health" :variant="HEALTH_BADGE[d.health.state] || 'neutral'" dot>{{ healthLabel(d.health.state) }}</BaseBadge>
              <span class="flex-1"></span>
              <span v-if="lines(d).length" class="text-xs font-medium tabular-nums text-status-warning-700 dark:text-status-warning-300">
                {{ lines(d).length }} to act on
              </span>
            </div>
            <ul v-if="lines(d).length" class="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-sm text-gray-700 dark:text-gray-300">
              <li v-for="line in lines(d)" :key="line.key" :data-testid="`stewarding-${d.project.id}-${line.key}`">{{ line.text }}</li>
            </ul>
            <p v-else class="mt-2 text-sm text-gray-600 dark:text-gray-300">Nothing needs you here.</p>
          </button>
        </li>
      </ul>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted } from 'vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import SkeletonLoader from '@/components/SkeletonLoader.vue'
import LoadFailed from '@/components/LoadFailed.vue'
import InlineError from '@/components/InlineError.vue'
import { useProjectsStore } from '@/stores/projects'
import { viewState } from '@/utils/loadingState'
import { HEALTH_BADGE, healthLabel, attentionLines } from './projectsUtils'

defineEmits(['open'])

const store = useProjectsStore()
const state = computed(() => store.stewarding)
const rows = computed(() => state.value.rows)
const view = computed(() => viewState({ hasLoaded: state.value.loaded, error: state.value.error, count: rows.value.length }))
const lines = (d) => attentionLines(d)

onMounted(() => { void store.fetchStewarding() })
</script>
