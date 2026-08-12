<template>
  <div v-if="show" class="fixed inset-0 z-50 overflow-y-auto" aria-labelledby="modal-title" role="dialog" aria-modal="true">
    <div class="flex items-end justify-center min-h-screen pt-4 px-4 pb-20 text-center sm:block sm:p-0">
      <!-- Background overlay -->
      <div class="fixed inset-0 bg-gray-500 bg-opacity-75 dark:bg-gray-900 dark:bg-opacity-75 transition-opacity" @click="$emit('update:show', false)"></div>

      <!-- Modal panel -->
      <div class="inline-block align-bottom bg-white dark:bg-gray-800 rounded-lg px-4 pt-5 pb-4 text-left overflow-hidden shadow-xl transform transition-all sm:my-8 sm:align-middle sm:max-w-lg sm:w-full sm:p-6">
        <div>
          <div class="mx-auto flex items-center justify-center h-12 w-12 rounded-full bg-action-primary-100 dark:bg-action-primary-900/50">
            <svg class="h-6 w-6 text-action-primary-600 dark:text-action-primary-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2zM9 9h6v6H9V9z" />
            </svg>
          </div>
          <div class="mt-3 text-center sm:mt-5">
            <h3 class="text-lg leading-6 font-medium text-gray-900 dark:text-white" id="modal-title">
              Configure Resource Allocation
            </h3>
            <p class="mt-2 text-sm text-gray-500 dark:text-gray-400">
              Adjust memory and CPU allocation for this agent.
            </p>
          </div>
        </div>

        <!-- Warning Banner -->
        <div class="mt-4 p-3 bg-state-autonomous-50 dark:bg-state-autonomous-900/30 border border-state-autonomous-200 dark:border-state-autonomous-800 rounded-lg">
          <div class="flex">
            <svg class="h-5 w-5 text-state-autonomous-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
            </svg>
            <p class="ml-3 text-sm text-state-autonomous-700 dark:text-state-autonomous-300">
              If the agent is running, it will be automatically restarted to apply changes.
            </p>
          </div>
        </div>

        <!-- Load-error banner (#1126): the GET failed, so the selects below may
             show stale defaults rather than the agent's real values. -->
        <div v-if="resourceLimits.error" class="mt-4 p-3 bg-status-danger-50 dark:bg-status-danger-900/30 border border-status-danger-200 dark:border-status-danger-800 rounded-lg">
          <p class="text-sm text-status-danger-700 dark:text-status-danger-300">
            Could not load current resource limits — showing defaults. {{ resourceLimits.error }}
          </p>
        </div>

        <!-- Resource Configuration Form -->
        <div class="mt-5 space-y-4">
          <BaseSelect
            label="Memory"
            :model-value="resourceLimits.memory ?? resourceLimits.current_memory"
            :disabled="loading"
            @update:model-value="$emit('update:memory', $event || null)"
          >
            <option value="">Inherit default ({{ resourceLimits.current_memory || '4g' }})</option>
            <option value="1g">1 GB</option>
            <option value="2g">2 GB</option>
            <option value="4g">4 GB</option>
            <option value="8g">8 GB</option>
            <option value="16g">16 GB</option>
            <option value="32g">32 GB</option>
            <option value="64g">64 GB</option>
          </BaseSelect>
          <BaseSelect
            label="CPU Cores"
            :model-value="resourceLimits.cpu ?? resourceLimits.current_cpu"
            :disabled="loading"
            @update:model-value="$emit('update:cpu', $event || null)"
          >
            <option value="">Inherit default ({{ resourceLimits.current_cpu || '2' }})</option>
            <option value="1">1 Core</option>
            <option value="2">2 Cores</option>
            <option value="4">4 Cores</option>
            <option value="8">8 Cores</option>
            <option value="16">16 Cores</option>
          </BaseSelect>
        </div>

        <!-- Modal Actions -->
        <div class="mt-5 sm:mt-6 sm:grid sm:grid-cols-2 sm:gap-3 sm:grid-flow-row-dense">
          <button
            type="button"
            @click="$emit('save')"
            :disabled="loading"
            class="w-full inline-flex justify-center rounded-lg border border-transparent shadow-sm px-4 py-2 bg-action-primary-600 text-base font-medium text-white hover:bg-action-primary-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-action-primary-500 sm:col-start-2 sm:text-sm disabled:opacity-50"
          >
            {{ loading ? 'Saving...' : 'Save Changes' }}
          </button>
          <button
            type="button"
            @click="$emit('update:show', false)"
            class="mt-3 w-full inline-flex justify-center rounded-lg border border-gray-300 dark:border-gray-600 shadow-sm px-4 py-2 bg-white dark:bg-gray-700 text-base font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-600 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-action-primary-500 sm:mt-0 sm:col-start-1 sm:text-sm"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import BaseSelect from './base/BaseSelect.vue'

defineProps({
  show: Boolean,
  resourceLimits: {
    type: Object,
    default: () => ({})
  },
  loading: Boolean
})

defineEmits(['update:show', 'update:memory', 'update:cpu', 'save'])
</script>
