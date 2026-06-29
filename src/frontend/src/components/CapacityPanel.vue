<template>
  <div class="p-6">
    <div class="mb-6">
      <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-2">Parallel Capacity</h3>
      <p class="text-sm text-gray-500 dark:text-gray-400 mb-4">
        Maximum number of tasks this agent runs at once. The upper bound is the
        fleet-wide ceiling set by an administrator.
      </p>

      <!-- Loading State -->
      <div v-if="loading" class="text-center py-4">
        <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-action-primary-500 mx-auto"></div>
        <p class="mt-2 text-sm text-gray-500 dark:text-gray-400">Loading capacity...</p>
      </div>

      <form v-else @submit.prevent="save" class="space-y-5 max-w-md">
        <div>
          <label for="cap-max-parallel" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
            Max parallel tasks
          </label>
          <input
            id="cap-max-parallel"
            v-model.number="maxParallelTasks"
            type="number"
            :min="MIN"
            :max="ceiling"
            :disabled="saving"
            class="w-full px-3 py-2 text-sm rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:ring-action-primary-500 focus:border-action-primary-500 disabled:opacity-50"
          />
          <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
            Between {{ MIN }} and {{ ceiling }} (fleet ceiling). Currently
            using {{ activeSlots }} / {{ effectiveMax }} slots.
          </p>
        </div>

        <!-- Stored value exceeds the current ceiling -->
        <p
          v-if="exceedsCeiling"
          class="text-xs text-status-warning-600 dark:text-status-warning-400"
        >
          Your setting ({{ stored }}) exceeds the current fleet ceiling
          ({{ ceiling }}); effective limit is {{ ceiling }}.
        </p>

        <p v-if="errorMessage" class="text-sm text-status-danger-600 dark:text-status-danger-400">
          {{ errorMessage }}
        </p>

        <div class="flex items-center space-x-3">
          <button
            type="submit"
            :disabled="saving"
            class="px-4 py-2 text-sm font-medium rounded-lg bg-action-primary-600 hover:bg-action-primary-700 text-white disabled:opacity-50"
          >
            {{ saving ? 'Saving...' : 'Save' }}
          </button>
        </div>
      </form>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useAgentsStore } from '../stores/agents'

const props = defineProps({
  agentName: {
    type: String,
    required: true
  },
  // Toast callback from the parent view (matches Guardrails / Read-Only pattern)
  notify: {
    type: Function,
    default: null
  }
})

const MIN = 1

const agentsStore = useAgentsStore()

const loading = ref(false)
const saving = ref(false)
const errorMessage = ref('')
const maxParallelTasks = ref(null)
const stored = ref(null)
const ceiling = ref(10)
const effectiveMax = ref(null)
const activeSlots = ref(0)

const exceedsCeiling = computed(
  () => stored.value !== null && ceiling.value !== null && stored.value > ceiling.value
)

function showToast(message, type = 'success') {
  if (props.notify) props.notify(message, type)
}

async function load() {
  if (!props.agentName) return
  loading.value = true
  errorMessage.value = ''
  try {
    const result = await agentsStore.getAgentCapacity(props.agentName)
    stored.value = result?.max_parallel_tasks ?? null
    maxParallelTasks.value = result?.max_parallel_tasks ?? null
    ceiling.value = result?.ceiling ?? 10
    effectiveMax.value = result?.effective_max_parallel_tasks ?? result?.max_parallel_tasks ?? null
    activeSlots.value = result?.active_slots ?? 0
  } catch (err) {
    console.error('Failed to load capacity:', err)
    errorMessage.value = err.response?.data?.detail || 'Failed to load capacity'
  } finally {
    loading.value = false
  }
}

async function save() {
  if (saving.value) return
  errorMessage.value = ''

  const value = maxParallelTasks.value
  if (!Number.isInteger(value) || value < MIN || value > ceiling.value) {
    errorMessage.value = `Max parallel tasks must be a whole number between ${MIN} and ${ceiling.value}`
    return
  }

  saving.value = true
  try {
    await agentsStore.setAgentCapacity(props.agentName, value)
    showToast('Capacity saved', 'success')
    await load()
  } catch (err) {
    const detail = err.response?.data?.detail || 'Failed to save capacity'
    errorMessage.value = detail
    showToast(detail, 'error')
  } finally {
    saving.value = false
  }
}

onMounted(load)
watch(() => props.agentName, load)
</script>
