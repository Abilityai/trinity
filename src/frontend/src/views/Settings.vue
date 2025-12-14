<template>
  <div class="min-h-screen bg-gray-100">
    <NavBar />

    <main class="max-w-4xl mx-auto py-6 sm:px-6 lg:px-8">
      <div class="px-4 py-6 sm:px-0">
        <div class="mb-8">
          <h1 class="text-3xl font-bold text-gray-900">Settings</h1>
          <p class="mt-1 text-sm text-gray-500">
            System-wide configuration for the Trinity platform
          </p>
        </div>

        <!-- Loading State -->
        <div v-if="loading" class="bg-white rounded-lg shadow p-8 text-center">
          <div class="animate-spin rounded-full h-12 w-12 border-b-2 border-indigo-600 mx-auto"></div>
          <p class="mt-4 text-gray-500">Loading settings...</p>
        </div>

        <!-- Settings Content -->
        <div v-else class="space-y-6">
          <!-- Trinity Prompt Section -->
          <div class="bg-white shadow rounded-lg">
            <div class="px-6 py-4 border-b border-gray-200">
              <h2 class="text-lg font-medium text-gray-900">Trinity Prompt</h2>
              <p class="mt-1 text-sm text-gray-500">
                Custom instructions that are injected into all agents' CLAUDE.md at startup.
                Changes apply to newly started or restarted agents.
              </p>
            </div>

            <div class="px-6 py-4">
              <div class="space-y-4">
                <div>
                  <label for="trinity-prompt" class="block text-sm font-medium text-gray-700">
                    Custom Instructions
                  </label>
                  <div class="mt-1">
                    <textarea
                      id="trinity-prompt"
                      v-model="trinityPrompt"
                      rows="15"
                      class="shadow-sm focus:ring-indigo-500 focus:border-indigo-500 block w-full sm:text-sm border border-gray-300 rounded-md font-mono"
                      placeholder="Enter custom instructions for all agents...

Example:
- Always use TypeScript for new files
- Follow the project's coding conventions
- Check for security vulnerabilities before committing"
                      :disabled="saving"
                    ></textarea>
                  </div>
                  <p class="mt-2 text-sm text-gray-500">
                    This content will appear under a "## Custom Instructions" section in each agent's CLAUDE.md.
                    Supports Markdown formatting.
                  </p>
                </div>

                <!-- Character Count -->
                <div class="flex justify-between text-sm text-gray-500">
                  <span>{{ trinityPrompt.length }} characters</span>
                  <span v-if="hasChanges" class="text-amber-600">Unsaved changes</span>
                </div>

                <!-- Action Buttons -->
                <div class="flex justify-end space-x-3">
                  <button
                    @click="clearPrompt"
                    :disabled="saving || !trinityPrompt"
                    class="inline-flex items-center px-4 py-2 border border-gray-300 shadow-sm text-sm font-medium rounded-md text-gray-700 bg-white hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    Clear
                  </button>
                  <button
                    @click="savePrompt"
                    :disabled="saving || !hasChanges"
                    class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    <svg v-if="saving" class="animate-spin -ml-1 mr-2 h-4 w-4 text-white" fill="none" viewBox="0 0 24 24">
                      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                    </svg>
                    {{ saving ? 'Saving...' : 'Save Changes' }}
                  </button>
                </div>
              </div>
            </div>
          </div>

          <!-- Info Box -->
          <div class="bg-blue-50 border border-blue-200 rounded-lg p-4">
            <div class="flex">
              <div class="flex-shrink-0">
                <svg class="h-5 w-5 text-blue-400" fill="currentColor" viewBox="0 0 20 20">
                  <path fill-rule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clip-rule="evenodd" />
                </svg>
              </div>
              <div class="ml-3">
                <h3 class="text-sm font-medium text-blue-800">How it works</h3>
                <div class="mt-2 text-sm text-blue-700">
                  <ul class="list-disc list-inside space-y-1">
                    <li>The Trinity Prompt is injected into each agent's CLAUDE.md when the agent starts</li>
                    <li>Existing agents need to be restarted to receive the updated prompt</li>
                    <li>The prompt appears as a "## Custom Instructions" section after the Trinity Planning System section</li>
                    <li>Use Markdown formatting for structured instructions</li>
                  </ul>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- Error Display -->
        <div v-if="error" class="mt-4 bg-red-50 border border-red-200 rounded-lg p-4">
          <div class="flex">
            <div class="flex-shrink-0">
              <svg class="h-5 w-5 text-red-400" fill="currentColor" viewBox="0 0 20 20">
                <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clip-rule="evenodd" />
              </svg>
            </div>
            <div class="ml-3">
              <h3 class="text-sm font-medium text-red-800">Error</h3>
              <p class="mt-1 text-sm text-red-700">{{ error }}</p>
            </div>
          </div>
        </div>

        <!-- Success Message -->
        <div v-if="showSuccess" class="mt-4 bg-green-50 border border-green-200 rounded-lg p-4">
          <div class="flex">
            <div class="flex-shrink-0">
              <svg class="h-5 w-5 text-green-400" fill="currentColor" viewBox="0 0 20 20">
                <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd" />
              </svg>
            </div>
            <div class="ml-3">
              <p class="text-sm font-medium text-green-800">Settings saved successfully!</p>
            </div>
          </div>
        </div>
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { useSettingsStore } from '../stores/settings'
import NavBar from '../components/NavBar.vue'

const router = useRouter()
const authStore = useAuthStore()
const settingsStore = useSettingsStore()

const loading = ref(true)
const saving = ref(false)
const error = ref(null)
const showSuccess = ref(false)

const trinityPrompt = ref('')
const originalPrompt = ref('')

const hasChanges = computed(() => {
  return trinityPrompt.value !== originalPrompt.value
})

async function loadSettings() {
  loading.value = true
  error.value = null

  try {
    await settingsStore.fetchSettings()
    trinityPrompt.value = settingsStore.trinityPrompt || ''
    originalPrompt.value = trinityPrompt.value
  } catch (e) {
    if (e.response?.status === 403) {
      error.value = 'Access denied. Admin privileges required.'
      router.push('/')
    } else {
      error.value = e.response?.data?.detail || 'Failed to load settings'
    }
  } finally {
    loading.value = false
  }
}

async function savePrompt() {
  saving.value = true
  error.value = null
  showSuccess.value = false

  try {
    if (trinityPrompt.value.trim()) {
      await settingsStore.updateSetting('trinity_prompt', trinityPrompt.value)
    } else {
      await settingsStore.deleteSetting('trinity_prompt')
      trinityPrompt.value = ''
    }
    originalPrompt.value = trinityPrompt.value
    showSuccess.value = true
    setTimeout(() => {
      showSuccess.value = false
    }, 3000)
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to save settings'
  } finally {
    saving.value = false
  }
}

async function clearPrompt() {
  trinityPrompt.value = ''
  await savePrompt()
}

onMounted(() => {
  // Check if user is admin
  const userData = authStore.user
  // For now, allow access - backend will reject if not admin
  loadSettings()
})
</script>
