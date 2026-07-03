<template>
  <div class="space-y-6">
    <!-- Header -->
    <div>
      <h3 class="text-lg font-medium text-gray-900 dark:text-white">Playbooks</h3>
      <p class="text-sm text-gray-500 dark:text-gray-400">
        Invoke agent skills directly. Skills are loaded from <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-xs">.claude/skills/</code>
      </p>
    </div>

    <!-- Agent Not Running -->
    <div v-if="agentStatus !== 'running'" class="bg-state-autonomous-50 dark:bg-state-autonomous-900/20 border border-state-autonomous-200 dark:border-state-autonomous-800 rounded-lg p-6 text-center">
      <svg class="mx-auto h-10 w-10 text-state-autonomous-400 mb-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
      </svg>
      <p class="text-state-autonomous-800 dark:text-state-autonomous-300 font-medium">Agent Not Running</p>
      <p class="text-sm text-state-autonomous-700 dark:text-state-autonomous-400 mt-1">Start the agent to view and run playbooks.</p>
    </div>

    <!-- Loading State -->
    <div v-else-if="loading" class="flex justify-center py-12">
      <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-action-primary-600"></div>
    </div>

    <!-- Error State -->
    <div v-else-if="error" class="bg-status-danger-50 dark:bg-status-danger-900/20 border border-status-danger-200 dark:border-status-danger-800 rounded-lg p-4">
      <div class="flex">
        <svg class="h-5 w-5 text-status-danger-400 mt-0.5" fill="currentColor" viewBox="0 0 20 20">
          <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clip-rule="evenodd" />
        </svg>
        <div class="ml-3">
          <h3 class="text-sm font-medium text-status-danger-800 dark:text-status-danger-300">Failed to load playbooks</h3>
          <p class="mt-1 text-sm text-status-danger-700 dark:text-status-danger-400">{{ error }}</p>
          <button
            @click="loadPlaybooks"
            class="mt-2 text-sm text-status-danger-600 dark:text-status-danger-400 hover:text-status-danger-500 underline"
          >
            Try again
          </button>
        </div>
      </div>
    </div>

    <!-- Playbooks Content -->
    <template v-else>
      <!-- Search -->
      <div v-if="skills.length > 0" class="relative">
        <input
          v-model="searchQuery"
          type="text"
          placeholder="Search playbooks..."
          class="block w-full pl-10 pr-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 dark:bg-gray-700 dark:text-white text-sm"
        />
        <svg class="absolute left-3 top-2.5 h-5 w-5 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
        </svg>
      </div>

      <!-- Skills Grid -->
      <div v-if="filteredSkills.length > 0" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <div
          v-for="skill in filteredSkills"
          :key="skill.name"
          class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-4 hover:border-gray-300 dark:hover:border-gray-600 transition-colors"
        >
          <!-- Skill Name -->
          <div class="flex items-start justify-between mb-2">
            <code class="text-sm font-semibold text-action-primary-600 dark:text-action-primary-400">/{{ skill.name }}</code>
            <!-- Automation Badge -->
            <span
              v-if="skill.automation"
              :class="[
                'px-1.5 py-0.5 rounded text-[10px] font-medium uppercase tracking-wider',
                skill.automation === 'autonomous' ? 'bg-state-autonomous-100 dark:bg-state-autonomous-900/30 text-state-autonomous-700 dark:text-state-autonomous-300' :
                skill.automation === 'gated' ? 'bg-accent-purple-100 dark:bg-accent-purple-900/30 text-accent-purple-700 dark:text-accent-purple-300' :
                'bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300'
              ]"
            >
              {{ skill.automation }}
            </span>
          </div>

          <!-- Description -->
          <p v-if="skill.description" class="text-sm text-gray-600 dark:text-gray-400 line-clamp-2 mb-3 min-h-[40px]">
            {{ skill.description }}
          </p>
          <p v-else class="text-sm text-gray-400 dark:text-gray-500 italic mb-3 min-h-[40px]">
            No description available
          </p>

          <!-- Argument Hint -->
          <p v-if="skill.argument_hint" class="text-xs text-gray-400 dark:text-gray-500 font-mono mb-3 truncate">
            {{ skill.argument_hint }}
          </p>

          <!-- Action Buttons -->
          <div class="flex items-center space-x-2 pt-2 border-t border-gray-100 dark:border-gray-700">
            <!-- Run Button -->
            <button
              @click="runSkill(skill)"
              :disabled="!skill.user_invocable || running === skill.name"
              class="flex-1 inline-flex items-center justify-center px-3 py-1.5 border border-transparent text-xs font-medium rounded-md text-white bg-status-success-600 hover:bg-status-success-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-status-success-500 disabled:bg-gray-400 dark:disabled:bg-gray-600 disabled:cursor-not-allowed transition-colors"
            >
              <svg v-if="running === skill.name" class="animate-spin -ml-0.5 mr-1.5 h-3 w-3" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
              </svg>
              <svg v-else class="w-3 h-3 mr-1" fill="currentColor" viewBox="0 0 20 20">
                <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM9.555 7.168A1 1 0 008 8v4a1 1 0 001.555.832l3-2a1 1 0 000-1.664l-3-2z" clip-rule="evenodd" />
              </svg>
              Run
            </button>

            <!-- Edit & Run Button: prefill the command in the Tasks tab so the
                 user can add instructions/arguments, then run. Labeled
                 "Edit & Run" (not "Edit") so it doesn't imply a skill-file
                 editor — it edits the invocation, not the file (#959). -->
            <button
              @click="runWithInstructions(skill)"
              class="inline-flex items-center justify-center px-3 py-1.5 border border-gray-300 dark:border-gray-600 text-xs font-medium rounded-md text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-700 hover:bg-gray-50 dark:hover:bg-gray-600 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-action-primary-500 transition-colors"
              title="Add instructions, then run"
            >
              <svg class="w-3 h-3 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
              </svg>
              Edit &amp; Run
            </button>
          </div>

          <!-- Not User Invocable Notice -->
          <p v-if="!skill.user_invocable" class="text-xs text-gray-400 dark:text-gray-500 mt-2 italic">
            This skill is not user-invocable
          </p>
        </div>
      </div>

      <!-- No Skills Found -->
      <div v-else-if="skills.length === 0" class="text-center py-12">
        <svg class="mx-auto h-12 w-12 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z" />
        </svg>
        <p class="mt-4 text-gray-500 dark:text-gray-400 font-medium">No playbooks found</p>
        <p class="text-sm text-gray-400 dark:text-gray-500 mt-1">
          Skills are loaded from <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded">.claude/skills/</code>
        </p>
      </div>

      <!-- No Search Results -->
      <div v-else-if="searchQuery && filteredSkills.length === 0" class="text-center py-8 text-gray-500 dark:text-gray-400">
        No playbooks found matching "{{ searchQuery }}"
      </div>

      <!-- Footer with skill paths -->
      <div v-if="skillPaths.length > 0" class="pt-4 border-t border-gray-200 dark:border-gray-700">
        <p class="text-xs text-gray-400 dark:text-gray-500">
          Scanned: {{ skillPaths.join(', ') }} ({{ skills.length }} playbook{{ skills.length === 1 ? '' : 's' }})
        </p>
      </div>
    </template>

    <!-- Toast Notifications -->
    <div v-if="successMessage" class="fixed bottom-4 right-4 z-50 bg-status-success-100 dark:bg-status-success-900/50 border border-status-success-400 dark:border-status-success-700 text-status-success-700 dark:text-status-success-300 px-4 py-3 rounded-lg shadow-lg">
      {{ successMessage }}
    </div>
    <div v-if="errorMessage" class="fixed bottom-4 right-4 z-50 bg-status-danger-100 dark:bg-status-danger-900/50 border border-status-danger-400 dark:border-status-danger-700 text-status-danger-700 dark:text-status-danger-300 px-4 py-3 rounded-lg shadow-lg">
      {{ errorMessage }}
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import axios from 'axios'
import { useAuthStore } from '../stores/auth'

const props = defineProps({
  agentName: {
    type: String,
    required: true
  },
  agentStatus: {
    type: String,
    default: 'stopped'
  }
})

const emit = defineEmits(['run-with-instructions'])

const authStore = useAuthStore()

// State
const loading = ref(false)
const error = ref(null)
const searchQuery = ref('')
const running = ref(null) // Currently running skill name
const successMessage = ref('')
const errorMessage = ref('')

// Data
const skills = ref([])
const skillPaths = ref([])

// Computed
const filteredSkills = computed(() => {
  let result = skills.value

  // Filter by search query
  if (searchQuery.value) {
    const query = searchQuery.value.toLowerCase()
    result = result.filter(skill =>
      skill.name.toLowerCase().includes(query) ||
      (skill.description && skill.description.toLowerCase().includes(query))
    )
  }

  return result
})

// Methods
async function loadPlaybooks() {
  if (props.agentStatus !== 'running') {
    return
  }

  loading.value = true
  error.value = null

  try {
    const response = await axios.get(
      `/api/agents/${props.agentName}/playbooks`,
      { headers: authStore.authHeader }
    )

    skills.value = response.data.skills || []
    skillPaths.value = response.data.skill_paths || []
  } catch (e) {
    console.error('Failed to load playbooks:', e)
    error.value = e.response?.data?.detail || 'Failed to load playbooks'
  } finally {
    loading.value = false
  }
}

async function runSkill(skill) {
  if (!skill.user_invocable || running.value) {
    return
  }

  running.value = skill.name

  try {
    const response = await axios.post(
      `/api/agents/${props.agentName}/task`,
      {
        message: `/${skill.name}`,
        parallel: false
      },
      { headers: authStore.authHeader }
    )

    const executionId = response.data.execution_id

    successMessage.value = `Playbook /${skill.name} started`
    setTimeout(() => { successMessage.value = '' }, 3000)

    // Navigate to Tasks tab with highlighted execution
    // Parent will handle this via event or we can use router
    emit('run-with-instructions', `__NAVIGATE_TASKS__:${executionId}`)
  } catch (e) {
    console.error('Failed to run skill:', e)
    errorMessage.value = e.response?.data?.detail || `Failed to run /${skill.name}`
    setTimeout(() => { errorMessage.value = '' }, 3000)
  } finally {
    running.value = null
  }
}

function runWithInstructions(skill) {
  // Emit event to parent with prefill text
  // Parent will switch to Tasks tab and prefill the textarea
  emit('run-with-instructions', `/${skill.name} `)
}

// Lifecycle
onMounted(() => {
  if (props.agentStatus === 'running') {
    loadPlaybooks()
  }
})

// Watch for agent status changes
watch(() => props.agentStatus, (newStatus) => {
  if (newStatus === 'running') {
    loadPlaybooks()
  } else {
    // Clear data when agent stops
    skills.value = []
    skillPaths.value = []
    error.value = null
  }
})

// Watch for agent name changes
watch(() => props.agentName, () => {
  if (props.agentStatus === 'running') {
    loadPlaybooks()
  }
})
</script>

<style scoped>
.line-clamp-2 {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
