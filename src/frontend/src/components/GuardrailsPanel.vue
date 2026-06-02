<template>
  <div class="p-6">
    <div class="mb-6">
      <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-2">Guardrails</h3>
      <p class="text-sm text-gray-500 dark:text-gray-400 mb-4">
        Per-agent execution caps. Leave a field blank to inherit the platform default.
        Changes require an agent restart to take effect.
      </p>

      <!-- Loading State -->
      <div v-if="loading" class="text-center py-4">
        <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-action-primary-500 mx-auto"></div>
        <p class="mt-2 text-sm text-gray-500 dark:text-gray-400">Loading guardrails...</p>
      </div>

      <form v-else @submit.prevent="save" class="space-y-5 max-w-md">
        <div>
          <label for="gr-max-turns-chat" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
            Max turns (chat)
          </label>
          <input
            id="gr-max-turns-chat"
            v-model.number="maxTurnsChat"
            type="number"
            :min="MIN_TURNS"
            :max="MAX_TURNS"
            :placeholder="`${DEFAULT_TURNS} (default)`"
            :disabled="saving"
            class="w-full px-3 py-2 text-sm rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:ring-action-primary-500 focus:border-action-primary-500 disabled:opacity-50"
          />
          <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
            Maximum Claude turns per interactive chat message ({{ MIN_TURNS }}–{{ MAX_TURNS }}).
          </p>
        </div>

        <div>
          <label for="gr-max-turns-task" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
            Max turns (task)
          </label>
          <input
            id="gr-max-turns-task"
            v-model.number="maxTurnsTask"
            type="number"
            :min="MIN_TURNS"
            :max="MAX_TURNS"
            :placeholder="`${DEFAULT_TURNS} (default)`"
            :disabled="saving"
            class="w-full px-3 py-2 text-sm rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:ring-action-primary-500 focus:border-action-primary-500 disabled:opacity-50"
          />
          <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
            Maximum Claude turns per scheduled/cron task ({{ MIN_TURNS }}–{{ MAX_TURNS }}). Raise this for long-running jobs.
          </p>
        </div>

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
import { ref, onMounted, watch } from 'vue'
import { useAgentsStore } from '../stores/agents'

const props = defineProps({
  agentName: {
    type: String,
    required: true
  },
  // Toast callback from the parent view (matches Read-Only / Resource limit pattern)
  notify: {
    type: Function,
    default: null
  }
})

const MIN_TURNS = 1
const MAX_TURNS = 500
const DEFAULT_TURNS = 50

const agentsStore = useAgentsStore()

const loading = ref(false)
const saving = ref(false)
const errorMessage = ref('')
const maxTurnsChat = ref(null)
const maxTurnsTask = ref(null)
// Preserve out-of-scope overrides (execution_timeout_sec, *_deny lists) set via API
const otherKeys = ref({})

function showToast(message, type = 'success') {
  if (props.notify) props.notify(message, type)
}

async function load() {
  if (!props.agentName) return
  loading.value = true
  errorMessage.value = ''
  try {
    const result = await agentsStore.getGuardrails(props.agentName)
    const g = result?.guardrails || {}
    maxTurnsChat.value = g.max_turns_chat ?? null
    maxTurnsTask.value = g.max_turns_task ?? null
    const { max_turns_chat, max_turns_task, ...rest } = g
    otherKeys.value = rest
  } catch (err) {
    console.error('Failed to load guardrails:', err)
    errorMessage.value = err.response?.data?.detail || 'Failed to load guardrails'
  } finally {
    loading.value = false
  }
}

function validateField(value, label) {
  if (value === null || value === '' || value === undefined) return null
  if (!Number.isInteger(value) || value < MIN_TURNS || value > MAX_TURNS) {
    throw new Error(`${label} must be a whole number between ${MIN_TURNS} and ${MAX_TURNS}`)
  }
  return value
}

async function save() {
  if (saving.value) return
  errorMessage.value = ''

  let chat, task
  try {
    chat = validateField(maxTurnsChat.value, 'Max turns (chat)')
    task = validateField(maxTurnsTask.value, 'Max turns (task)')
  } catch (err) {
    errorMessage.value = err.message
    return
  }

  // Merge managed keys over preserved out-of-scope overrides. A blank field
  // clears that key (inherit platform default).
  const payload = { ...otherKeys.value }
  if (chat !== null) payload.max_turns_chat = chat
  else delete payload.max_turns_chat
  if (task !== null) payload.max_turns_task = task
  else delete payload.max_turns_task

  saving.value = true
  try {
    await agentsStore.setGuardrails(props.agentName, payload)
    showToast('Guardrails saved — restart agent to apply', 'success')
    await load()
  } catch (err) {
    const detail = err.response?.data?.detail || 'Failed to save guardrails'
    errorMessage.value = detail
    showToast(detail, 'error')
  } finally {
    saving.value = false
  }
}

onMounted(load)
watch(() => props.agentName, load)
</script>
