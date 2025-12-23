<template>
  <div class="space-y-6">
    <!-- Header -->
    <div class="flex items-center justify-between">
      <div>
        <h3 class="text-lg font-medium text-gray-900 dark:text-white">Public Links</h3>
        <p class="text-sm text-gray-500 dark:text-gray-400">
          Generate shareable links that allow anyone to chat with this agent.
        </p>
      </div>
      <button
        @click="showCreateModal = true"
        class="inline-flex items-center px-3 py-2 border border-transparent text-sm font-medium rounded-md text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 dark:focus:ring-offset-gray-800"
      >
        <svg class="w-4 h-4 mr-1.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
        </svg>
        Create Link
      </button>
    </div>

    <!-- Loading state -->
    <div v-if="loading" class="text-center py-8">
      <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-indigo-500 mx-auto"></div>
      <p class="mt-2 text-sm text-gray-500 dark:text-gray-400">Loading links...</p>
    </div>

    <!-- Empty state -->
    <div v-else-if="links.length === 0" class="text-center py-12 bg-gray-50 dark:bg-gray-900/50 rounded-lg border border-dashed border-gray-300 dark:border-gray-700">
      <svg class="mx-auto h-12 w-12 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1" />
      </svg>
      <h3 class="mt-2 text-sm font-medium text-gray-900 dark:text-white">No public links</h3>
      <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">Create a link to share this agent with others.</p>
      <button
        @click="showCreateModal = true"
        class="mt-4 inline-flex items-center px-3 py-2 text-sm font-medium text-indigo-600 dark:text-indigo-400 hover:text-indigo-700 dark:hover:text-indigo-300"
      >
        <svg class="w-4 h-4 mr-1.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
        </svg>
        Create your first link
      </button>
    </div>

    <!-- Links list -->
    <div v-else class="space-y-3">
      <div
        v-for="link in links"
        :key="link.id"
        class="bg-gray-50 dark:bg-gray-900/50 rounded-lg border border-gray-200 dark:border-gray-700 p-4"
      >
        <div class="flex items-start justify-between">
          <div class="flex-1 min-w-0">
            <!-- Link name and status -->
            <div class="flex items-center space-x-2">
              <h4 class="text-sm font-medium text-gray-900 dark:text-white truncate">
                {{ link.name || 'Unnamed Link' }}
              </h4>
              <span
                :class="[
                  'inline-flex items-center px-2 py-0.5 rounded text-xs font-medium',
                  link.enabled
                    ? 'bg-green-100 dark:bg-green-900/30 text-green-800 dark:text-green-300'
                    : 'bg-gray-100 dark:bg-gray-700 text-gray-800 dark:text-gray-300'
                ]"
              >
                {{ link.enabled ? 'Active' : 'Disabled' }}
              </span>
              <span
                v-if="link.require_email"
                class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-blue-100 dark:bg-blue-900/30 text-blue-800 dark:text-blue-300"
              >
                Email Required
              </span>
            </div>

            <!-- URL preview -->
            <div class="mt-2 flex items-center space-x-2">
              <code class="flex-1 text-xs text-gray-500 dark:text-gray-400 bg-gray-100 dark:bg-gray-800 px-2 py-1 rounded truncate">
                {{ link.url }}
              </code>
              <button
                @click="copyLink(link)"
                class="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 rounded hover:bg-gray-200 dark:hover:bg-gray-700"
                title="Copy link"
              >
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                </svg>
              </button>
            </div>

            <!-- Stats -->
            <div v-if="link.usage_stats" class="mt-2 flex items-center space-x-4 text-xs text-gray-500 dark:text-gray-400">
              <span>{{ link.usage_stats.total_messages || 0 }} messages</span>
              <span>{{ link.usage_stats.unique_users || 0 }} users</span>
              <span v-if="link.usage_stats.last_used_at">
                Last used: {{ formatDate(link.usage_stats.last_used_at) }}
              </span>
            </div>

            <!-- Expiration -->
            <div v-if="link.expires_at" class="mt-2 text-xs text-gray-500 dark:text-gray-400">
              <span v-if="isExpired(link.expires_at)" class="text-red-500 dark:text-red-400">
                Expired {{ formatDate(link.expires_at) }}
              </span>
              <span v-else>
                Expires {{ formatDate(link.expires_at) }}
              </span>
            </div>
          </div>

          <!-- Actions -->
          <div class="flex items-center space-x-2 ml-4">
            <button
              @click="toggleLink(link)"
              :disabled="actionLoading === link.id"
              class="p-1.5 rounded hover:bg-gray-200 dark:hover:bg-gray-700 disabled:opacity-50"
              :title="link.enabled ? 'Disable link' : 'Enable link'"
            >
              <svg v-if="link.enabled" class="w-4 h-4 text-gray-400 hover:text-yellow-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M18.364 18.364A9 9 0 005.636 5.636m12.728 12.728A9 9 0 015.636 5.636m12.728 12.728L5.636 5.636" />
              </svg>
              <svg v-else class="w-4 h-4 text-gray-400 hover:text-green-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
            </button>
            <button
              @click="editLink(link)"
              class="p-1.5 rounded hover:bg-gray-200 dark:hover:bg-gray-700"
              title="Edit link"
            >
              <svg class="w-4 h-4 text-gray-400 hover:text-indigo-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
              </svg>
            </button>
            <button
              @click="confirmDelete(link)"
              :disabled="actionLoading === link.id"
              class="p-1.5 rounded hover:bg-gray-200 dark:hover:bg-gray-700 disabled:opacity-50"
              title="Delete link"
            >
              <svg class="w-4 h-4 text-gray-400 hover:text-red-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
              </svg>
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Create/Edit Modal -->
    <div v-if="showCreateModal || editingLink" class="fixed inset-0 z-50 overflow-y-auto">
      <div class="flex items-center justify-center min-h-screen px-4 pt-4 pb-20 text-center sm:p-0">
        <div class="fixed inset-0 transition-opacity" @click="closeModal">
          <div class="absolute inset-0 bg-gray-500 dark:bg-gray-900 opacity-75"></div>
        </div>

        <div class="relative inline-block w-full max-w-md bg-white dark:bg-gray-800 rounded-lg text-left overflow-hidden shadow-xl transform transition-all sm:my-8">
          <form @submit.prevent="saveLink">
            <div class="px-6 pt-5 pb-4">
              <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-4">
                {{ editingLink ? 'Edit Public Link' : 'Create Public Link' }}
              </h3>

              <div class="space-y-4">
                <!-- Name -->
                <div>
                  <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Name (optional)
                  </label>
                  <input
                    v-model="formData.name"
                    type="text"
                    placeholder="e.g., Customer Support Demo"
                    class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-white focus:ring-indigo-500 focus:border-indigo-500"
                  />
                </div>

                <!-- Require email -->
                <div class="flex items-center">
                  <input
                    id="require-email"
                    v-model="formData.require_email"
                    type="checkbox"
                    class="h-4 w-4 text-indigo-600 focus:ring-indigo-500 border-gray-300 dark:border-gray-600 rounded"
                  />
                  <label for="require-email" class="ml-2 block text-sm text-gray-700 dark:text-gray-300">
                    Require email verification
                  </label>
                </div>
                <p v-if="formData.require_email" class="text-xs text-gray-500 dark:text-gray-400 -mt-2 ml-6">
                  Users will need to verify their email before chatting.
                </p>

                <!-- Expiration -->
                <div>
                  <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Expiration (optional)
                  </label>
                  <input
                    v-model="formData.expires_at"
                    type="datetime-local"
                    class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-white focus:ring-indigo-500 focus:border-indigo-500"
                  />
                </div>

                <!-- Enabled (only for edit) -->
                <div v-if="editingLink" class="flex items-center">
                  <input
                    id="enabled"
                    v-model="formData.enabled"
                    type="checkbox"
                    class="h-4 w-4 text-indigo-600 focus:ring-indigo-500 border-gray-300 dark:border-gray-600 rounded"
                  />
                  <label for="enabled" class="ml-2 block text-sm text-gray-700 dark:text-gray-300">
                    Link enabled
                  </label>
                </div>
              </div>

              <!-- Error message -->
              <div v-if="formError" class="mt-4 p-3 bg-red-100 dark:bg-red-900/30 border border-red-200 dark:border-red-800 rounded-md">
                <p class="text-sm text-red-600 dark:text-red-400">{{ formError }}</p>
              </div>
            </div>

            <div class="px-6 py-3 bg-gray-50 dark:bg-gray-900/50 flex justify-end space-x-3">
              <button
                type="button"
                @click="closeModal"
                class="px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white"
              >
                Cancel
              </button>
              <button
                type="submit"
                :disabled="formLoading"
                class="px-4 py-2 text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 rounded-md disabled:bg-indigo-400"
              >
                <span v-if="formLoading" class="flex items-center">
                  <svg class="animate-spin -ml-1 mr-2 h-4 w-4 text-white" fill="none" viewBox="0 0 24 24">
                    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                  </svg>
                  Saving...
                </span>
                <span v-else>{{ editingLink ? 'Save Changes' : 'Create Link' }}</span>
              </button>
            </div>
          </form>
        </div>
      </div>
    </div>

    <!-- Delete Confirmation Modal -->
    <div v-if="deletingLink" class="fixed inset-0 z-50 overflow-y-auto">
      <div class="flex items-center justify-center min-h-screen px-4 pt-4 pb-20 text-center sm:p-0">
        <div class="fixed inset-0 transition-opacity" @click="deletingLink = null">
          <div class="absolute inset-0 bg-gray-500 dark:bg-gray-900 opacity-75"></div>
        </div>

        <div class="relative inline-block w-full max-w-sm bg-white dark:bg-gray-800 rounded-lg text-left overflow-hidden shadow-xl transform transition-all sm:my-8">
          <div class="px-6 pt-5 pb-4">
            <div class="flex items-center">
              <div class="flex-shrink-0 w-10 h-10 bg-red-100 dark:bg-red-900/30 rounded-full flex items-center justify-center">
                <svg class="w-6 h-6 text-red-600 dark:text-red-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                </svg>
              </div>
              <div class="ml-3">
                <h3 class="text-lg font-medium text-gray-900 dark:text-white">Delete Link</h3>
                <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                  Are you sure you want to delete this link? All sessions will be invalidated.
                </p>
              </div>
            </div>
          </div>

          <div class="px-6 py-3 bg-gray-50 dark:bg-gray-900/50 flex justify-end space-x-3">
            <button
              type="button"
              @click="deletingLink = null"
              class="px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white"
            >
              Cancel
            </button>
            <button
              @click="deleteLink"
              :disabled="actionLoading === deletingLink?.id"
              class="px-4 py-2 text-sm font-medium text-white bg-red-600 hover:bg-red-700 rounded-md disabled:bg-red-400"
            >
              Delete
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Copy notification -->
    <div
      v-if="copyNotification"
      class="fixed bottom-4 right-4 px-4 py-2 bg-green-600 text-white rounded-lg shadow-lg text-sm z-50"
    >
      Link copied to clipboard!
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, watch } from 'vue'
import axios from 'axios'
import { useAuthStore } from '../stores/auth'

const props = defineProps({
  agentName: {
    type: String,
    required: true
  }
})

const authStore = useAuthStore()

// State
const loading = ref(false)
const links = ref([])
const showCreateModal = ref(false)
const editingLink = ref(null)
const deletingLink = ref(null)
const actionLoading = ref(null)
const copyNotification = ref(false)

// Form
const formData = ref({
  name: '',
  require_email: false,
  expires_at: '',
  enabled: true
})
const formLoading = ref(false)
const formError = ref(null)

// Load links
const loadLinks = async () => {
  loading.value = true
  try {
    const response = await axios.get(`/api/agents/${props.agentName}/public-links`, {
      headers: authStore.authHeader
    })
    links.value = response.data
  } catch (err) {
    console.error('Failed to load public links:', err)
  } finally {
    loading.value = false
  }
}

// Save link (create or update)
const saveLink = async () => {
  formLoading.value = true
  formError.value = null

  try {
    const payload = {
      name: formData.value.name || null,
      require_email: formData.value.require_email
    }

    if (formData.value.expires_at) {
      payload.expires_at = new Date(formData.value.expires_at).toISOString()
    }

    if (editingLink.value) {
      payload.enabled = formData.value.enabled
      await axios.put(
        `/api/agents/${props.agentName}/public-links/${editingLink.value.id}`,
        payload,
        { headers: authStore.authHeader }
      )
    } else {
      await axios.post(
        `/api/agents/${props.agentName}/public-links`,
        payload,
        { headers: authStore.authHeader }
      )
    }

    closeModal()
    await loadLinks()
  } catch (err) {
    console.error('Failed to save link:', err)
    formError.value = err.response?.data?.detail || 'Failed to save link'
  } finally {
    formLoading.value = false
  }
}

// Toggle link enabled/disabled
const toggleLink = async (link) => {
  actionLoading.value = link.id
  try {
    await axios.put(
      `/api/agents/${props.agentName}/public-links/${link.id}`,
      { enabled: !link.enabled },
      { headers: authStore.authHeader }
    )
    await loadLinks()
  } catch (err) {
    console.error('Failed to toggle link:', err)
  } finally {
    actionLoading.value = null
  }
}

// Edit link
const editLink = (link) => {
  editingLink.value = link
  formData.value = {
    name: link.name || '',
    require_email: link.require_email,
    expires_at: link.expires_at ? link.expires_at.slice(0, 16) : '',
    enabled: link.enabled
  }
}

// Confirm delete
const confirmDelete = (link) => {
  deletingLink.value = link
}

// Delete link
const deleteLink = async () => {
  if (!deletingLink.value) return

  actionLoading.value = deletingLink.value.id
  try {
    await axios.delete(
      `/api/agents/${props.agentName}/public-links/${deletingLink.value.id}`,
      { headers: authStore.authHeader }
    )
    deletingLink.value = null
    await loadLinks()
  } catch (err) {
    console.error('Failed to delete link:', err)
  } finally {
    actionLoading.value = null
  }
}

// Copy link to clipboard
const copyLink = async (link) => {
  try {
    await navigator.clipboard.writeText(link.url)
    copyNotification.value = true
    setTimeout(() => {
      copyNotification.value = false
    }, 2000)
  } catch (err) {
    console.error('Failed to copy link:', err)
  }
}

// Close modal
const closeModal = () => {
  showCreateModal.value = false
  editingLink.value = null
  formData.value = {
    name: '',
    require_email: false,
    expires_at: '',
    enabled: true
  }
  formError.value = null
}

// Format date
const formatDate = (dateStr) => {
  if (!dateStr) return ''
  const date = new Date(dateStr)
  return date.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit'
  })
}

// Check if expired
const isExpired = (dateStr) => {
  if (!dateStr) return false
  return new Date(dateStr) < new Date()
}

// Watch for agent name changes
watch(() => props.agentName, () => {
  loadLinks()
}, { immediate: true })

onMounted(() => {
  loadLinks()
})
</script>
