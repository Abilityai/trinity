<template>
  <div
    :class="[
      'flex flex-col bg-white dark:bg-gray-800 border-r border-gray-200 dark:border-gray-700 transition-all duration-300',
      isCollapsed ? 'w-12' : 'w-56'
    ]"
  >
    <!-- Header -->
    <div class="flex items-center justify-between px-3 py-2 border-b border-gray-200 dark:border-gray-700">
      <span v-if="!isCollapsed" class="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider">
        Systems
      </span>
      <button
        @click="toggleCollapse"
        class="p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 rounded transition-colors"
        :title="isCollapsed ? 'Expand sidebar' : 'Collapse sidebar'"
      >
        <svg v-if="isCollapsed" class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 5l7 7-7 7M5 5l7 7-7 7" />
        </svg>
        <svg v-else class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 19l-7-7 7-7m8 14l-7-7 7-7" />
        </svg>
      </button>
    </div>

    <!-- Views List -->
    <div class="flex-1 overflow-y-auto py-2">
      <!-- All Agents Option -->
      <button
        @click="selectView(null)"
        :class="[
          'w-full flex items-center px-3 py-2 text-sm transition-colors',
          !activeViewId
            ? 'bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 border-r-2 border-blue-600'
            : 'text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700/50'
        ]"
      >
        <span class="w-5 h-5 flex items-center justify-center mr-2 text-base">
          {{ isCollapsed ? '○' : '' }}
        </span>
        <span v-if="!isCollapsed" class="truncate">All Agents</span>
      </button>

      <!-- Divider -->
      <div v-if="!isCollapsed && views.length > 0" class="my-2 border-t border-gray-200 dark:border-gray-700"></div>

      <!-- System Views -->
      <div v-if="isLoading && views.length === 0" class="px-3 py-2 text-xs text-gray-400">
        Loading...
      </div>

      <button
        v-for="view in sortedViews"
        :key="view.id"
        @click="selectView(view.id)"
        :class="[
          'w-full flex items-center px-3 py-2 text-sm transition-colors group',
          activeViewId === view.id
            ? 'bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 border-r-2 border-blue-600'
            : 'text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700/50'
        ]"
        :title="isCollapsed ? view.name : ''"
      >
        <!-- Icon or Color Dot -->
        <span
          class="w-5 h-5 flex items-center justify-center mr-2 text-base flex-shrink-0"
          :style="view.color ? { color: view.color } : {}"
        >
          {{ view.icon || '●' }}
        </span>

        <template v-if="!isCollapsed">
          <span class="truncate flex-1 text-left">{{ view.name }}</span>
          <span class="ml-2 text-xs text-gray-400 dark:text-gray-400">{{ view.agent_count }}</span>

          <!-- Edit/Delete on hover -->
          <div class="hidden group-hover:flex items-center ml-1 space-x-1">
            <button
              @click.stop="$emit('edit', view)"
              class="p-0.5 text-gray-400 hover:text-blue-600 dark:hover:text-blue-400"
              title="Edit"
            >
              <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
              </svg>
            </button>
          </div>
        </template>
      </button>
    </div>

    <!-- Create New View Button -->
    <div class="border-t border-gray-200 dark:border-gray-700 p-2">
      <button
        @click="$emit('create')"
        :class="[
          'w-full flex items-center justify-center py-2 text-sm text-gray-600 dark:text-gray-400 hover:text-blue-600 dark:hover:text-blue-400 hover:bg-blue-50 dark:hover:bg-blue-900/20 rounded transition-colors',
          isCollapsed ? 'px-2' : 'px-3'
        ]"
        :title="isCollapsed ? 'New View' : ''"
      >
        <svg class="w-4 h-4" :class="{ 'mr-2': !isCollapsed }" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6v6m0 0v6m0-6h6m-6 0H6" />
        </svg>
        <span v-if="!isCollapsed">New View</span>
      </button>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useSystemViewsStore } from '@/stores/systemViews'
import { storeToRefs } from 'pinia'

const emit = defineEmits(['create', 'edit'])

const systemViewsStore = useSystemViewsStore()
const { views, activeViewId, isLoading, sortedViews } = storeToRefs(systemViewsStore)

// Default to collapsed, but respect localStorage if set
const savedCollapsed = localStorage.getItem('trinity-sidebar-collapsed')
const isCollapsed = ref(savedCollapsed !== null ? savedCollapsed === 'true' : true)

// Watch for collapse changes and persist
function toggleCollapse() {
  isCollapsed.value = !isCollapsed.value
  localStorage.setItem('trinity-sidebar-collapsed', String(isCollapsed.value))
}

function selectView(viewId) {
  systemViewsStore.selectView(viewId)
}

onMounted(async () => {
  systemViewsStore.initialize()
  await systemViewsStore.fetchViews()
})
</script>
