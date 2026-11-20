<!--
  Systems: which agents the dashboard shows. A dropdown in the dashboard's
  toolbar, in place of the column that used to run down the page's left edge:
  the choice is made now and then, so it no longer takes width from every pane.
-->
<template>
  <div ref="root" class="relative" @keydown.escape="isOpen = false">
    <button
      type="button"
      data-testid="system-views-menu"
      :aria-expanded="isOpen ? 'true' : 'false'"
      aria-haspopup="true"
      :title="activeView ? `Showing the system ${activeView.name}` : 'Showing all agents'"
      :class="[
        'flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium whitespace-nowrap transition-colors max-w-[11rem]',
        activeView
          ? 'bg-action-primary-600 text-white hover:bg-action-primary-700'
          : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
      ]"
      @click="isOpen = !isOpen"
    >
      <span
        v-if="activeView"
        class="flex-none w-3 text-center"
        :style="activeView.color ? { color: activeView.color } : {}"
        aria-hidden="true"
      >{{ activeView.icon || '●' }}</span>
      <svg v-else class="w-3 h-3 flex-none" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" aria-hidden="true">
        <path stroke-linecap="round" stroke-linejoin="round" d="M4 6h16M4 12h16M4 18h10" />
      </svg>
      <span class="truncate">{{ activeView ? activeView.name : 'All agents' }}</span>
      <svg class="w-3 h-3 flex-none" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
      </svg>
    </button>

    <div
      v-if="isOpen"
      class="absolute left-0 top-full mt-1 z-50 w-60 py-1 rounded-lg bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 shadow-lg"
      data-testid="system-views-list"
    >
      <p class="px-3 pt-1 pb-1.5 text-[11px] font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400">Systems</p>
      <button
        type="button"
        :class="rowClass(!activeViewId)"
        @click="choose(null)"
      >
        <span class="w-4 flex-none text-center" aria-hidden="true">○</span>
        <span class="flex-1 truncate text-left">All agents</span>
      </button>

      <div
        v-if="isLoading && views.length === 0"
        class="px-3 py-1.5 space-y-2"
        aria-busy="true"
      >
        <div v-for="n in 2" :key="n" class="h-4 rounded bg-gray-100 dark:bg-gray-700 animate-pulse motion-reduce:animate-none"></div>
        <span class="sr-only">Loading…</span>
      </div>

      <div v-else class="max-h-72 overflow-y-auto">
        <div
          v-for="view in sortedViews"
          :key="view.id"
          class="group flex items-center"
        >
          <button
            type="button"
            :class="rowClass(activeViewId === view.id)"
            :data-testid="`system-view-${view.id}`"
            @click="choose(view.id)"
          >
            <span
              class="w-4 flex-none text-center"
              :style="view.color ? { color: view.color } : {}"
              aria-hidden="true"
            >{{ view.icon || '●' }}</span>
            <span class="flex-1 truncate text-left">{{ view.name }}</span>
            <span class="text-[11px] tabular-nums text-gray-500 dark:text-gray-400">{{ view.agent_count }}</span>
          </button>
          <button
            type="button"
            class="flex-none mr-1 p-1 rounded text-gray-400 hover:text-action-primary-600 dark:hover:text-action-primary-400 opacity-0 group-hover:opacity-100 focus:opacity-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500"
            :aria-label="`Edit ${view.name}`"
            title="Edit"
            @click="edit(view)"
          >
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
            </svg>
          </button>
        </div>
      </div>

      <div class="mt-1 pt-1 border-t border-gray-200 dark:border-gray-700">
        <button
          type="button"
          class="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-gray-600 dark:text-gray-300 hover:text-action-primary-700 dark:hover:text-action-primary-400 hover:bg-gray-50 dark:hover:bg-gray-700/50"
          data-testid="system-view-create"
          @click="create"
        >
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6v12m6-6H6" />
          </svg>
          New system view
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { useSystemViewsStore } from '@/stores/systemViews'

const emit = defineEmits(['create', 'edit'])

const systemViewsStore = useSystemViewsStore()
const { views, activeViewId, isLoading, sortedViews } = storeToRefs(systemViewsStore)
const activeView = computed(() => views.value.find((v) => v.id === activeViewId.value) || null)

const isOpen = ref(false)
const root = ref(null)

const rowClass = (active) => [
  'flex-1 min-w-0 w-full flex items-center gap-2 px-3 py-1.5 text-xs transition-colors',
  active
    ? 'text-action-primary-700 dark:text-action-primary-300 font-medium bg-action-primary-50 dark:bg-action-primary-500/16'
    : 'text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700/50',
]

function choose(id) {
  systemViewsStore.selectView(id)
  isOpen.value = false
}
function edit(view) {
  isOpen.value = false
  emit('edit', view)
}
function create() {
  isOpen.value = false
  emit('create')
}

const onOutside = (e) => {
  if (isOpen.value && root.value && !root.value.contains(e.target)) isOpen.value = false
}
onMounted(async () => {
  document.addEventListener('mousedown', onOutside)
  systemViewsStore.initialize()
  await systemViewsStore.fetchViews()
})
onBeforeUnmount(() => document.removeEventListener('mousedown', onOutside))
</script>
