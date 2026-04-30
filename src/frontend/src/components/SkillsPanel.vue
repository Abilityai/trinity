<template>
  <div class="p-6">
    <!-- Skills Assignment Section -->
    <div class="space-y-4">
      <!-- Header -->
      <div class="flex items-center justify-between">
        <div>
          <h3 class="text-lg font-medium text-gray-900 dark:text-white">Assigned Skills</h3>
          <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
            Select skills to enable for this agent. Skills are injected when the agent starts.
          </p>
        </div>
        <div class="flex gap-2">
          <button
            v-if="agentStatus === 'running'"
            @click="injectSkills"
            :disabled="injecting || assignedSkills.length === 0"
            class="inline-flex items-center px-3 py-1.5 border border-transparent text-sm font-medium rounded-md text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <svg v-if="injecting" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
            </svg>
            {{ injecting ? 'Injecting...' : 'Inject to Agent' }}
          </button>
          <button
            @click="saveAssignments"
            :disabled="saving || !hasChanges"
            class="inline-flex items-center px-3 py-1.5 border border-transparent text-sm font-medium rounded-md text-white bg-status-success-600 hover:bg-status-success-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <svg v-if="saving" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
            </svg>
            {{ saving ? 'Saving...' : 'Save' }}
          </button>
        </div>
      </div>

      <!-- Loading State -->
      <div v-if="loading" class="flex justify-center py-8">
        <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-indigo-600"></div>
      </div>

      <!-- Library Not Configured -->
      <div v-else-if="!libraryStatus.configured" class="bg-state-autonomous-50 dark:bg-state-autonomous-900/20 border border-state-autonomous-200 dark:border-state-autonomous-800 rounded-lg p-4">
        <div class="flex">
          <svg class="h-5 w-5 text-state-autonomous-400 mt-0.5" fill="currentColor" viewBox="0 0 20 20">
            <path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd" />
          </svg>
          <div class="ml-3">
            <h3 class="text-sm font-medium text-state-autonomous-800 dark:text-state-autonomous-300">Skills Library Not Configured</h3>
            <p class="mt-1 text-sm text-state-autonomous-700 dark:text-state-autonomous-400">
              Configure the skills library URL in Settings to enable skills management.
            </p>
          </div>
        </div>
      </div>

      <!-- Skills Grid -->
      <div v-else-if="availableSkills.length > 0" class="space-y-4">
        <!-- Search -->
        <div class="relative">
          <input
            v-model="searchQuery"
            type="text"
            placeholder="Search skills..."
            class="block w-full pl-10 pr-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 dark:bg-gray-700 dark:text-white text-sm"
          />
          <svg class="absolute left-3 top-2.5 h-5 w-5 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
          </svg>
        </div>

        <!-- Skills List -->
        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          <div
            v-for="skill in filteredSkills"
            :key="skill.name"
            class="relative flex items-start p-3 border rounded-lg cursor-pointer transition-colors"
            :class="[
              selectedSkills.has(skill.name)
                ? 'border-indigo-500 bg-indigo-50 dark:bg-indigo-900/30 dark:border-indigo-600'
                : 'border-gray-200 dark:border-gray-700 hover:border-gray-300 dark:hover:border-gray-600'
            ]"
            @click="toggleSkill(skill.name)"
          >
            <div class="flex items-center h-5">
              <input
                type="checkbox"
                :checked="selectedSkills.has(skill.name)"
                class="h-4 w-4 text-indigo-600 border-gray-300 dark:border-gray-600 rounded focus:ring-indigo-500"
                @change.stop="toggleSkill(skill.name)"
              />
            </div>
            <div class="ml-3 flex-1">
              <label class="font-medium text-sm text-gray-900 dark:text-white cursor-pointer">
                {{ skill.name }}
              </label>
              <p v-if="skill.description" class="text-xs text-gray-500 dark:text-gray-400 mt-0.5 line-clamp-2">
                {{ skill.description }}
              </p>
            </div>
          </div>
        </div>

        <!-- No Results -->
        <div v-if="filteredSkills.length === 0" class="text-center py-8 text-gray-500 dark:text-gray-400">
          No skills found matching "{{ searchQuery }}"
        </div>
      </div>

      <!-- No Skills Available -->
      <div v-else class="text-center py-8">
        <svg class="mx-auto h-12 w-12 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z" />
        </svg>
        <p class="mt-2 text-gray-500 dark:text-gray-400">No skills available in the library.</p>
        <p class="text-sm text-gray-400 dark:text-gray-500">Sync the library in Settings to load skills.</p>
      </div>

      <!-- Library Status Footer -->
      <div v-if="libraryStatus.configured" class="mt-6 pt-4 border-t border-gray-200 dark:border-gray-700">
        <div class="flex items-center justify-between text-xs text-gray-500 dark:text-gray-400">
          <div class="flex items-center gap-4">
            <span>
              Library: <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded">{{ libraryStatus.url || 'Not configured' }}</code>
            </span>
            <span v-if="libraryStatus.commit_sha">
              Commit: <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded">{{ libraryStatus.commit_sha }}</code>
            </span>
          </div>
          <span v-if="libraryStatus.last_sync">
            Last synced: {{ formatDate(libraryStatus.last_sync) }}
          </span>
        </div>
      </div>
    </div>

    <!-- Success Message -->
    <div v-if="successMessage" class="fixed bottom-4 right-4 z-50 bg-status-success-100 dark:bg-status-success-900/50 border border-status-success-400 dark:border-status-success-700 text-status-success-700 dark:text-status-success-300 px-4 py-3 rounded-lg shadow-lg">
      {{ successMessage }}
    </div>

    <!-- Error Message -->
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

const authStore = useAuthStore()

// State
const loading = ref(true)
const saving = ref(false)
const injecting = ref(false)
const searchQuery = ref('')
const successMessage = ref('')
const errorMessage = ref('')

// Data
const availableSkills = ref([])
const assignedSkills = ref([])
const selectedSkills = ref(new Set())
const originalSelection = ref(new Set())
const libraryStatus = ref({
  configured: false,
  url: null,
  branch: 'main',
  last_sync: null,
  commit_sha: null,
  skill_count: 0
})

// Computed
const filteredSkills = computed(() => {
  if (!searchQuery.value) return availableSkills.value
  const query = searchQuery.value.toLowerCase()
  return availableSkills.value.filter(skill =>
    skill.name.toLowerCase().includes(query) ||
    (skill.description && skill.description.toLowerCase().includes(query))
  )
})

const hasChanges = computed(() => {
  if (selectedSkills.value.size !== originalSelection.value.size) return true
  for (const skill of selectedSkills.value) {
    if (!originalSelection.value.has(skill)) return true
  }
  return false
})

// Methods
async function loadData() {
  loading.value = true
  try {
    // Load library status, available skills, and assigned skills in parallel
    const [statusRes, skillsRes, assignedRes] = await Promise.all([
      axios.get('/api/skills/library/status', { headers: authStore.authHeader }),
      axios.get('/api/skills/library', { headers: authStore.authHeader }).catch(() => ({ data: [] })),
      axios.get(`/api/agents/${props.agentName}/skills`, { headers: authStore.authHeader })
    ])

    libraryStatus.value = statusRes.data
    availableSkills.value = skillsRes.data
    assignedSkills.value = assignedRes.data

    // Initialize selection from assigned skills
    selectedSkills.value = new Set(assignedSkills.value.map(s => s.skill_name))
    originalSelection.value = new Set(selectedSkills.value)
  } catch (e) {
    console.error('Failed to load skills data:', e)
    errorMessage.value = 'Failed to load skills data'
    setTimeout(() => { errorMessage.value = '' }, 3000)
  } finally {
    loading.value = false
  }
}

function toggleSkill(skillName) {
  if (selectedSkills.value.has(skillName)) {
    selectedSkills.value.delete(skillName)
  } else {
    selectedSkills.value.add(skillName)
  }
  // Force reactivity
  selectedSkills.value = new Set(selectedSkills.value)
}

async function saveAssignments() {
  saving.value = true
  try {
    await axios.put(
      `/api/agents/${props.agentName}/skills`,
      { skills: Array.from(selectedSkills.value) },
      { headers: authStore.authHeader }
    )

    originalSelection.value = new Set(selectedSkills.value)
    successMessage.value = 'Skills saved successfully'
    setTimeout(() => { successMessage.value = '' }, 3000)
  } catch (e) {
    console.error('Failed to save skills:', e)
    errorMessage.value = e.response?.data?.detail || 'Failed to save skills'
    setTimeout(() => { errorMessage.value = '' }, 3000)
  } finally {
    saving.value = false
  }
}

async function injectSkills() {
  injecting.value = true
  try {
    const response = await axios.post(
      `/api/agents/${props.agentName}/skills/inject`,
      {},
      { headers: authStore.authHeader }
    )

    const result = response.data
    if (result.success) {
      successMessage.value = `Injected ${result.skills_injected} skills`
    } else {
      successMessage.value = `Injected ${result.skills_injected} skills, ${result.skills_failed} failed`
    }
    setTimeout(() => { successMessage.value = '' }, 3000)
  } catch (e) {
    console.error('Failed to inject skills:', e)
    errorMessage.value = e.response?.data?.detail || 'Failed to inject skills'
    setTimeout(() => { errorMessage.value = '' }, 3000)
  } finally {
    injecting.value = false
  }
}

function formatDate(dateString) {
  if (!dateString) return 'Never'
  const date = new Date(dateString)
  const now = new Date()
  const diffInMs = now - date
  const diffInMins = Math.floor(diffInMs / (1000 * 60))

  if (diffInMins < 1) return 'Just now'
  if (diffInMins < 60) return `${diffInMins}m ago`
  if (diffInMins < 1440) return `${Math.floor(diffInMins / 60)}h ago`
  return date.toLocaleDateString()
}

// Lifecycle
onMounted(() => {
  loadData()
})

// Reload when agent changes
watch(() => props.agentName, () => {
  loadData()
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
