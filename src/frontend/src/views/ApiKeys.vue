<template>
  <div class="min-h-screen bg-gray-100 dark:bg-gray-900">
    <NavBar />

    <main class="max-w-7xl mx-auto py-6 sm:px-6 lg:px-8">
      <div class="px-4 py-6 sm:px-0">
        <div class="flex justify-between items-center mb-8">
          <div>
            <h1 class="text-3xl font-bold text-gray-900 dark:text-white">MCP API Keys</h1>
            <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
              Manage API keys for accessing the Trinity MCP server
            </p>
          </div>
          <button
            @click="showCreateModal = true"
            class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700"
          >
            <svg class="-ml-1 mr-2 h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
            </svg>
            Create API Key
          </button>
        </div>

        <!-- MCP Connection Info -->
        <div class="bg-blue-50 dark:bg-blue-900/30 border border-blue-200 dark:border-blue-800 rounded-lg p-4 mb-6">
          <div class="flex">
            <div class="flex-shrink-0">
              <svg class="h-5 w-5 text-blue-400" fill="currentColor" viewBox="0 0 20 20">
                <path fill-rule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clip-rule="evenodd" />
              </svg>
            </div>
            <div class="ml-3 flex-1">
              <h3 class="text-sm font-medium text-blue-800 dark:text-blue-300">Connect to MCP Server</h3>
              <p class="text-sm text-blue-700 dark:text-blue-400 mt-1">
                Add this to your <code class="bg-blue-100 dark:bg-blue-800 px-1 rounded">.mcp.json</code> configuration:
              </p>
              <pre class="mt-2 bg-blue-100 dark:bg-blue-800 rounded p-3 text-xs overflow-x-auto text-blue-900 dark:text-blue-100">{
  "mcpServers": {
    "trinity": {
      "url": "{{ mcpServerUrl }}",
      "headers": {
        "Authorization": "Bearer YOUR_API_KEY"
      }
    }
  }
}</pre>
            </div>
          </div>
        </div>

        <!-- API Keys List -->
        <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg overflow-hidden">
          <div v-if="loading" class="p-8 text-center">
            <div class="animate-spin rounded-full h-12 w-12 border-b-2 border-indigo-600 mx-auto"></div>
            <p class="mt-4 text-gray-500 dark:text-gray-400">Loading API keys...</p>
          </div>

          <div v-else-if="displayedKeys.length === 0" class="text-center py-12">
            <svg class="mx-auto h-12 w-12 text-gray-400 dark:text-gray-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z" />
            </svg>
            <h3 class="mt-2 text-sm font-medium text-gray-900 dark:text-white">No API keys</h3>
            <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">Create an API key to start using the MCP server.</p>
          </div>

          <ul v-else class="divide-y divide-gray-200 dark:divide-gray-700">
            <li v-for="key in displayedKeys" :key="key.id" class="p-6 hover:bg-gray-50 dark:hover:bg-gray-700">
              <div class="flex items-center justify-between">
                <div class="flex items-center flex-1">
                  <div class="flex-shrink-0">
                    <div class="h-10 w-10 rounded-full flex items-center justify-center"
                         :class="key.is_active ? 'bg-green-100 dark:bg-green-900/50' : 'bg-red-100 dark:bg-red-900/50'">
                      <svg class="h-6 w-6" :class="key.is_active ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z" />
                      </svg>
                    </div>
                  </div>
                  <div class="ml-4 flex-1">
                    <div class="flex items-center">
                      <h3 class="text-sm font-medium text-gray-900 dark:text-white">{{ key.name }}</h3>
                      <span class="ml-3 inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium"
                            :class="key.is_active ? 'bg-green-100 dark:bg-green-900/50 text-green-800 dark:text-green-300' : 'bg-red-100 dark:bg-red-900/50 text-red-800 dark:text-red-300'">
                        {{ key.is_active ? 'Active' : 'Revoked' }}
                      </span>
                      <!-- Scope badge for admin visibility -->
                      <span v-if="key.scope === 'agent'" class="ml-2 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-purple-100 dark:bg-purple-900/50 text-purple-800 dark:text-purple-300">
                        Agent
                      </span>
                      <span v-else-if="key.scope === 'system'" class="ml-2 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-orange-100 dark:bg-orange-900/50 text-orange-800 dark:text-orange-300">
                        System
                      </span>
                    </div>
                    <p v-if="key.description" class="mt-1 text-sm text-gray-500 dark:text-gray-400">{{ key.description }}</p>
                    <div class="mt-1 flex items-center text-xs text-gray-400 dark:text-gray-500 space-x-4">
                      <span>
                        <code class="bg-gray-100 dark:bg-gray-700 px-1.5 py-0.5 rounded font-mono">{{ key.key_prefix }}...</code>
                      </span>
                      <span v-if="key.user_email" class="flex items-center">
                        <svg class="h-3 w-3 mr-1" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
                        </svg>
                        {{ key.user_email }}
                      </span>
                      <span>Created {{ formatDate(key.created_at) }}</span>
                      <span v-if="key.last_used_at">Last used {{ formatDate(key.last_used_at) }}</span>
                      <span class="flex items-center">
                        <svg class="h-3 w-3 mr-1" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
                        </svg>
                        {{ key.usage_count }} requests
                      </span>
                    </div>
                  </div>
                </div>
                <div class="ml-4 flex items-center space-x-2">
                  <button
                    v-if="key.is_active"
                    @click="revokeKey(key.id)"
                    class="inline-flex items-center px-3 py-1.5 border border-yellow-300 dark:border-yellow-600 shadow-sm text-xs font-medium rounded text-yellow-700 dark:text-yellow-300 bg-white dark:bg-gray-700 hover:bg-yellow-50 dark:hover:bg-yellow-900/30"
                  >
                    Revoke
                  </button>
                  <button
                    @click="deleteKey(key.id)"
                    class="inline-flex items-center px-3 py-1.5 border border-red-300 dark:border-red-600 shadow-sm text-xs font-medium rounded text-red-700 dark:text-red-300 bg-white dark:bg-gray-700 hover:bg-red-50 dark:hover:bg-red-900/30"
                  >
                    Delete
                  </button>
                </div>
              </div>
            </li>
          </ul>
        </div>
      </div>
    </main>

    <!-- Create API Key Modal -->
    <div v-if="showCreateModal" class="fixed z-10 inset-0 overflow-y-auto">
      <div class="flex items-center justify-center min-h-screen pt-4 px-4 pb-20 text-center sm:p-0">
        <div class="fixed inset-0 bg-gray-500 dark:bg-gray-900 bg-opacity-75 dark:bg-opacity-75 transition-opacity" @click="showCreateModal = false"></div>

        <div class="inline-block align-bottom bg-white dark:bg-gray-800 rounded-lg text-left overflow-hidden shadow-xl transform transition-all sm:my-8 sm:align-middle sm:max-w-lg sm:w-full">
          <div class="bg-white dark:bg-gray-800 px-4 pt-5 pb-4 sm:p-6 sm:pb-4">
            <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-4">Create MCP API Key</h3>

            <div class="space-y-4">
              <div>
                <label class="block text-sm font-medium text-gray-700 dark:text-gray-300">Name</label>
                <input
                  v-model="newKey.name"
                  type="text"
                  class="mt-1 block w-full border border-gray-300 dark:border-gray-600 rounded-md shadow-sm py-2 px-3 focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 sm:text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500"
                  placeholder="My Claude Code Key"
                />
              </div>

              <div>
                <label class="block text-sm font-medium text-gray-700 dark:text-gray-300">Description (optional)</label>
                <textarea
                  v-model="newKey.description"
                  rows="2"
                  class="mt-1 block w-full border border-gray-300 dark:border-gray-600 rounded-md shadow-sm py-2 px-3 focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 sm:text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500"
                  placeholder="Used for..."
                ></textarea>
              </div>
            </div>
          </div>

          <div class="bg-gray-50 dark:bg-gray-900 px-4 py-3 sm:px-6 sm:flex sm:flex-row-reverse">
            <button
              @click="createKey"
              :disabled="creating || !newKey.name"
              class="w-full inline-flex justify-center rounded-md border border-transparent shadow-sm px-4 py-2 bg-indigo-600 text-base font-medium text-white hover:bg-indigo-700 focus:outline-none sm:ml-3 sm:w-auto sm:text-sm disabled:opacity-50"
            >
              {{ creating ? 'Creating...' : 'Create' }}
            </button>
            <button
              @click="showCreateModal = false"
              class="mt-3 w-full inline-flex justify-center rounded-md border border-gray-300 dark:border-gray-600 shadow-sm px-4 py-2 bg-white dark:bg-gray-700 text-base font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-600 focus:outline-none sm:mt-0 sm:ml-3 sm:w-auto sm:text-sm"
            >
              Cancel
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Show Created Key Modal -->
    <div v-if="showKeyModal" class="fixed z-10 inset-0 overflow-y-auto">
      <div class="flex items-center justify-center min-h-screen pt-4 px-4 pb-20 text-center sm:p-0">
        <div class="fixed inset-0 bg-gray-500 dark:bg-gray-900 bg-opacity-75 dark:bg-opacity-75 transition-opacity"></div>

        <div class="inline-block align-bottom bg-white dark:bg-gray-800 rounded-lg text-left overflow-hidden shadow-xl transform transition-all sm:my-8 sm:align-middle sm:max-w-2xl sm:w-full">
          <div class="bg-white dark:bg-gray-800 px-4 pt-5 pb-4 sm:p-6 sm:pb-4">
            <div class="flex items-center mb-4">
              <div class="flex-shrink-0 flex items-center justify-center h-12 w-12 rounded-full bg-green-100 dark:bg-green-900/50">
                <svg class="h-6 w-6 text-green-600 dark:text-green-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                </svg>
              </div>
              <h3 class="ml-4 text-lg font-medium text-gray-900 dark:text-white">Your MCP API Key is Ready!</h3>
            </div>

            <div class="bg-yellow-50 dark:bg-yellow-900/30 border border-yellow-200 dark:border-yellow-800 rounded-lg p-4 mb-4">
              <div class="flex">
                <svg class="h-5 w-5 text-yellow-400 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
                  <path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd" />
                </svg>
                <div class="ml-3">
                  <p class="text-sm font-medium text-yellow-800 dark:text-yellow-300">
                    Copy the configuration below before closing - the key won't be shown again!
                  </p>
                </div>
              </div>
            </div>

            <div class="space-y-4">
              <!-- MCP Configuration (Primary - Copy this!) -->
              <div>
                <div class="flex items-center justify-between mb-1">
                  <label class="block text-sm font-medium text-gray-700 dark:text-gray-300">
                    MCP Configuration
                    <span class="ml-2 text-xs text-gray-500 dark:text-gray-400">(add to your .mcp.json)</span>
                  </label>
                  <button
                    @click="copyMcpConfig"
                    class="inline-flex items-center px-3 py-1 text-xs font-medium rounded-md"
                    :class="copiedConfig
                      ? 'bg-green-100 dark:bg-green-900/50 text-green-700 dark:text-green-300'
                      : 'bg-indigo-100 dark:bg-indigo-900/50 text-indigo-700 dark:text-indigo-300 hover:bg-indigo-200 dark:hover:bg-indigo-900'"
                  >
                    <svg v-if="!copiedConfig" class="h-3.5 w-3.5 mr-1" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                    </svg>
                    <svg v-else class="h-3.5 w-3.5 mr-1" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                    </svg>
                    {{ copiedConfig ? 'Copied!' : 'Copy Config' }}
                  </button>
                </div>
                <pre class="bg-gray-900 dark:bg-gray-950 rounded-lg p-4 text-xs overflow-x-auto text-green-400 font-mono border border-gray-700">{{ getMcpConfig(createdApiKey) }}</pre>
              </div>

              <!-- Raw API Key (Secondary) -->
              <div>
                <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">API Key Only</label>
                <div class="flex items-center space-x-2">
                  <input
                    :type="showApiKey ? 'text' : 'password'"
                    :value="createdApiKey"
                    readonly
                    class="flex-1 block w-full border border-gray-300 dark:border-gray-600 rounded-md shadow-sm py-2 px-3 bg-gray-50 dark:bg-gray-700 font-mono text-sm focus:outline-none text-gray-900 dark:text-gray-100"
                  />
                  <button
                    @click="showApiKey = !showApiKey"
                    class="p-2 border border-gray-300 dark:border-gray-600 rounded-md hover:bg-gray-50 dark:hover:bg-gray-700"
                    title="Show/hide key"
                  >
                    <svg v-if="!showApiKey" class="h-5 w-5 text-gray-500 dark:text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
                    </svg>
                    <svg v-else class="h-5 w-5 text-gray-500 dark:text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21" />
                    </svg>
                  </button>
                  <button
                    @click="copyApiKey"
                    class="p-2 border border-gray-300 dark:border-gray-600 rounded-md hover:bg-gray-50 dark:hover:bg-gray-700"
                    title="Copy key"
                  >
                    <svg v-if="!copied" class="h-5 w-5 text-gray-500 dark:text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                    </svg>
                    <svg v-else class="h-5 w-5 text-green-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                    </svg>
                  </button>
                </div>
              </div>
            </div>
          </div>

          <div class="bg-gray-50 dark:bg-gray-900 px-4 py-3 sm:px-6">
            <button
              @click="closeKeyModal"
              class="w-full inline-flex justify-center rounded-md border border-transparent shadow-sm px-4 py-2 bg-indigo-600 text-base font-medium text-white hover:bg-indigo-700 focus:outline-none sm:text-sm"
            >
              I've copied the configuration
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Confirm Dialog -->
    <ConfirmDialog
      v-model:visible="confirmDialog.visible"
      :title="confirmDialog.title"
      :message="confirmDialog.message"
      :confirm-text="confirmDialog.confirmText"
      :variant="confirmDialog.variant"
      @confirm="confirmDialog.onConfirm"
    />
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, computed } from 'vue'
import NavBar from '../components/NavBar.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import { useAuthStore } from '../stores/auth'

const authStore = useAuthStore()

const apiKeys = ref([])
const loading = ref(true)
const showCreateModal = ref(false)
const showKeyModal = ref(false)
const creating = ref(false)
const createdApiKey = ref('')
const showApiKey = ref(false)
const copied = ref(false)
const copiedConfig = ref(false)
const isAdmin = ref(false)

const newKey = ref({
  name: '',
  description: ''
})

// Confirm dialog state
const confirmDialog = reactive({
  visible: false,
  title: '',
  message: '',
  confirmText: 'Confirm',
  variant: 'danger',
  onConfirm: () => {}
})

const mcpServerUrl = computed(() => {
  const host = window.location.hostname
  if (host === 'localhost' || host === '127.0.0.1') {
    return 'http://localhost:8080/mcp'
  }
  return `https://${host}/mcp`
})

// Filter out agent-scoped keys for non-admin users
const displayedKeys = computed(() => {
  if (isAdmin.value) {
    return apiKeys.value
  }
  return apiKeys.value.filter(k => k.scope !== 'agent')
})

// Generate MCP config JSON with the given API key
const getMcpConfig = (apiKey) => {
  return JSON.stringify({
    mcpServers: {
      trinity: {
        url: mcpServerUrl.value,
        headers: {
          Authorization: `Bearer ${apiKey}`
        }
      }
    }
  }, null, 2)
}

const fetchUserRole = async () => {
  try {
    const response = await fetch('/api/users/me', {
      headers: {
        'Authorization': `Bearer ${authStore.token}`
      }
    })
    if (response.ok) {
      const userData = await response.json()
      isAdmin.value = userData.role === 'admin'
    }
  } catch (error) {
    console.error('Failed to fetch user role:', error)
  }
}

const fetchApiKeys = async () => {
  try {
    const response = await fetch('/api/mcp/keys', {
      headers: {
        'Authorization': `Bearer ${authStore.token}`
      }
    })
    if (response.ok) {
      apiKeys.value = await response.json()
    }
  } catch (error) {
    console.error('Failed to fetch API keys:', error)
  } finally {
    loading.value = false
  }
}

// Ensure user has a default MCP key on first visit
const ensureDefaultKey = async () => {
  try {
    const response = await fetch('/api/mcp/keys/ensure-default', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${authStore.token}`
      }
    })
    if (response.ok) {
      const data = await response.json()
      // If a key was created, show it to the user
      if (data && data.api_key) {
        createdApiKey.value = data.api_key
        showKeyModal.value = true
        await fetchApiKeys() // Refresh the list
      }
    }
  } catch (error) {
    console.error('Failed to ensure default key:', error)
  }
}

const createKey = async () => {
  creating.value = true

  try {
    const response = await fetch('/api/mcp/keys', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${authStore.token}`
      },
      body: JSON.stringify({
        name: newKey.value.name,
        description: newKey.value.description || null
      })
    })

    if (response.ok) {
      const data = await response.json()
      createdApiKey.value = data.api_key
      showCreateModal.value = false
      showKeyModal.value = true
      newKey.value = { name: '', description: '' }
      await fetchApiKeys()
    } else {
      const error = await response.json()
      alert(`Failed to create API key: ${error.detail}`)
    }
  } catch (error) {
    console.error('Failed to create API key:', error)
    alert('Failed to create API key')
  } finally {
    creating.value = false
  }
}

const revokeKey = (keyId) => {
  confirmDialog.title = 'Revoke API Key'
  confirmDialog.message = 'Are you sure you want to revoke this API key? It will no longer work for authentication.'
  confirmDialog.confirmText = 'Revoke'
  confirmDialog.variant = 'warning'
  confirmDialog.onConfirm = async () => {
    try {
      const response = await fetch(`/api/mcp/keys/${keyId}/revoke`, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${authStore.token}`
        }
      })

      if (response.ok) {
        await fetchApiKeys()
      }
    } catch (error) {
      console.error('Failed to revoke API key:', error)
    }
  }
  confirmDialog.visible = true
}

const deleteKey = (keyId) => {
  confirmDialog.title = 'Delete API Key'
  confirmDialog.message = 'Are you sure you want to permanently delete this API key?'
  confirmDialog.confirmText = 'Delete'
  confirmDialog.variant = 'danger'
  confirmDialog.onConfirm = async () => {
    try {
      const response = await fetch(`/api/mcp/keys/${keyId}`, {
        method: 'DELETE',
        headers: {
          'Authorization': `Bearer ${authStore.token}`
        }
      })

      if (response.ok) {
        await fetchApiKeys()
      }
    } catch (error) {
      console.error('Failed to delete API key:', error)
    }
  }
  confirmDialog.visible = true
}

const copyApiKey = async () => {
  try {
    await navigator.clipboard.writeText(createdApiKey.value)
    copied.value = true
    setTimeout(() => {
      copied.value = false
    }, 2000)
  } catch (error) {
    console.error('Failed to copy:', error)
  }
}

const copyMcpConfig = async () => {
  try {
    await navigator.clipboard.writeText(getMcpConfig(createdApiKey.value))
    copiedConfig.value = true
    setTimeout(() => {
      copiedConfig.value = false
    }, 2000)
  } catch (error) {
    console.error('Failed to copy config:', error)
  }
}

const closeKeyModal = () => {
  showKeyModal.value = false
  createdApiKey.value = ''
  showApiKey.value = false
  copied.value = false
  copiedConfig.value = false
}

const formatDate = (dateString) => {
  if (!dateString) return 'Never'
  const date = new Date(dateString)
  return date.toLocaleDateString() + ' ' + date.toLocaleTimeString()
}

onMounted(async () => {
  await fetchUserRole()
  await fetchApiKeys()
  // After loading keys, ensure user has a default key (for first-time users)
  await ensureDefaultKey()
})
</script>
