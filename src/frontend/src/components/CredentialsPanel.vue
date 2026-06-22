<template>
  <div class="p-6 space-y-6">
    <!-- Loading State -->
    <div v-if="loading" class="text-center py-8">
      <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-action-primary-500 mx-auto"></div>
      <p class="text-gray-500 dark:text-gray-400 mt-2">Loading credentials...</p>
    </div>

    <template v-else>
      <!-- Credential Files Section -->
      <div class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700">
        <div class="px-4 py-3 border-b border-gray-200 dark:border-gray-700">
          <h3 class="text-lg font-medium text-gray-900 dark:text-white">Credential Files</h3>
          <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">
            Manage credential files in the agent workspace
          </p>
        </div>

        <div class="divide-y divide-gray-200 dark:divide-gray-700">
          <div
            v-for="file in credentialFiles"
            :key="file.name"
            class="px-4 py-3 flex items-center justify-between"
          >
            <div class="flex items-center space-x-3">
              <span
                :class="[
                  'inline-flex items-center justify-center w-8 h-8 rounded-full',
                  file.exists
                    ? 'bg-status-success-100 dark:bg-status-success-900/50 text-status-success-600 dark:text-status-success-400'
                    : 'bg-gray-100 dark:bg-gray-700 text-gray-400 dark:text-gray-500'
                ]"
              >
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path v-if="file.exists" stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                  <path v-else stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </span>
              <div>
                <p class="font-mono text-sm text-gray-900 dark:text-white">{{ file.name }}</p>
                <p v-if="file.exists" class="text-xs text-gray-500 dark:text-gray-400">
                  {{ formatFileSize(file.size) }} &middot; Modified {{ formatDate(file.modified) }}
                </p>
                <p v-else class="text-xs text-gray-400 dark:text-gray-500">
                  Not present
                </p>
              </div>
            </div>
            <div class="flex items-center space-x-2">
              <button
                v-if="file.exists"
                @click="viewFile(file.name)"
                :disabled="agentStatus !== 'running'"
                class="text-sm text-blue-600 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                View
              </button>
              <button
                @click="editFile(file.name)"
                :disabled="agentStatus !== 'running'"
                class="text-sm text-action-primary-600 hover:text-action-primary-700 dark:text-action-primary-400 dark:hover:text-action-primary-300 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {{ file.exists ? 'Edit' : 'Add' }}
              </button>
            </div>
          </div>
        </div>

        <!-- Export/Import Buttons -->
        <div class="px-4 py-3 bg-gray-50 dark:bg-gray-900/50 border-t border-gray-200 dark:border-gray-700 flex items-center justify-between">
          <div class="flex items-center space-x-3">
            <button
              @click="exportToGit"
              :disabled="agentStatus !== 'running' || exporting || !hasCredentialFiles"
              class="inline-flex items-center px-3 py-1.5 text-sm font-medium rounded-md text-white bg-status-success-600 hover:bg-status-success-700 disabled:bg-gray-400 disabled:cursor-not-allowed"
            >
              <svg v-if="exporting" class="animate-spin -ml-0.5 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
              </svg>
              <svg v-else class="w-4 h-4 mr-1.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
              </svg>
              {{ exporting ? 'Exporting...' : 'Export to Git' }}
            </button>
            <button
              @click="importFromGit"
              :disabled="agentStatus !== 'running' || importing || !hasEncryptedFile"
              class="inline-flex items-center px-3 py-1.5 text-sm font-medium rounded-md text-gray-700 dark:text-gray-200 bg-white dark:bg-gray-700 border border-gray-300 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-600 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <svg v-if="importing" class="animate-spin -ml-0.5 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
              </svg>
              <svg v-else class="w-4 h-4 mr-1.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
              </svg>
              {{ importing ? 'Importing...' : 'Import from Git' }}
            </button>
          </div>
          <p class="text-xs text-gray-500 dark:text-gray-400">
            <span v-if="hasEncryptedFile" class="text-status-success-600 dark:text-status-success-400">
              .credentials.enc exists
            </span>
            <span v-else class="text-gray-400 dark:text-gray-500">
              No encrypted backup
            </span>
          </p>
        </div>
      </div>

      <!-- Quick Inject Section -->
      <div class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700">
        <div class="px-4 py-3 border-b border-gray-200 dark:border-gray-700">
          <h3 class="text-lg font-medium text-gray-900 dark:text-white">Quick Inject</h3>
          <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">
            Paste KEY=VALUE pairs to add credentials to .env
          </p>
        </div>

        <div class="p-4 space-y-4">
          <div class="relative">
            <textarea
              v-model="quickInjectText"
              :disabled="agentStatus !== 'running' || quickInjectLoading"
              rows="5"
              placeholder="OPENAI_API_KEY=sk-...&#10;ANTHROPIC_API_KEY=sk-ant-...&#10;&#10;# Lines starting with # are ignored"
              class="w-full font-mono text-sm border border-gray-300 dark:border-gray-600 rounded-lg p-3 bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-100 dark:disabled:bg-gray-800 disabled:cursor-not-allowed"
            ></textarea>
            <span
              v-if="agentStatus !== 'running'"
              class="absolute top-2 right-2 px-2 py-1 text-xs bg-status-warning-100 dark:bg-status-warning-900/50 text-status-warning-700 dark:text-status-warning-300 rounded"
            >
              Agent must be running
            </span>
          </div>

          <div class="flex items-center justify-between">
            <span class="text-sm text-gray-500 dark:text-gray-400">
              {{ quickInjectText ? countCredentials(quickInjectText) : 0 }} credential(s) detected
            </span>
            <button
              @click="quickInject"
              :disabled="agentStatus !== 'running' || quickInjectLoading || !quickInjectText.trim()"
              class="inline-flex items-center px-4 py-2 text-sm font-medium rounded-md text-white bg-blue-600 hover:bg-blue-700 disabled:bg-gray-400 disabled:cursor-not-allowed"
            >
              <svg v-if="quickInjectLoading" class="animate-spin -ml-0.5 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
              </svg>
              {{ quickInjectLoading ? 'Injecting...' : 'Inject' }}
            </button>
          </div>

          <!-- Quick inject result -->
          <div
            v-if="quickInjectResult"
            :class="[
              'p-4 rounded-lg',
              quickInjectResult.success ? 'bg-status-success-50 dark:bg-status-success-900/30 border border-status-success-200 dark:border-status-success-800' : 'bg-status-danger-50 dark:bg-status-danger-900/30 border border-status-danger-200 dark:border-status-danger-800'
            ]"
          >
            <div class="flex items-start">
              <svg v-if="quickInjectResult.success" class="w-5 h-5 text-status-success-500 mr-2 mt-0.5 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
                <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd" />
              </svg>
              <svg v-else class="w-5 h-5 text-status-danger-500 mr-2 mt-0.5 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
                <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clip-rule="evenodd" />
              </svg>
              <div>
                <p :class="quickInjectResult.success ? 'text-status-success-800 dark:text-status-success-300' : 'text-status-danger-800 dark:text-status-danger-300'" class="font-medium">
                  {{ quickInjectResult.message }}
                </p>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Upload Credential File (#11 — service-account JSON, certs/keys, SSH) -->
      <div class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700">
        <div class="px-4 py-3 border-b border-gray-200 dark:border-gray-700">
          <h3 class="text-lg font-medium text-gray-900 dark:text-white">Upload Credential File</h3>
          <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">
            For cloud service-account JSON (.config/gcloud/…), TLS certs/keys
            (*.pem/*.key/*.crt/*.p12/*.pfx), kubeconfig (.kube/config) and SSH keys
            (.ssh/id_*). Binary files are handled automatically.
          </p>
        </div>
        <div class="p-4 space-y-3">
          <input
            type="file"
            @change="onCredFilePicked"
            :disabled="agentStatus !== 'running' || uploadLoading"
            class="block w-full text-sm text-gray-700 dark:text-gray-300"
          />
          <input
            v-model="uploadPath"
            placeholder="Destination path (e.g. .config/gcloud/sa.json, client.pem, .ssh/id_ed25519)"
            :disabled="agentStatus !== 'running' || uploadLoading"
            class="w-full font-mono text-sm border border-gray-300 dark:border-gray-600 rounded-lg p-2 bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100"
          />
          <div class="flex items-center justify-between">
            <span class="text-xs text-gray-500 dark:text-gray-400">Written 0600; created on the agent's next sync if git-tracked.</span>
            <button
              @click="uploadCredFile"
              :disabled="agentStatus !== 'running' || uploadLoading || !uploadFile || !uploadPath.trim()"
              class="inline-flex items-center px-4 py-2 text-sm font-medium rounded-md text-white bg-blue-600 hover:bg-blue-700 disabled:bg-gray-400 disabled:cursor-not-allowed"
            >
              {{ uploadLoading ? 'Uploading...' : 'Upload' }}
            </button>
          </div>
          <div v-if="uploadResult"
               :class="['p-3 rounded-lg text-sm', uploadResult.success ? 'bg-status-success-50 dark:bg-status-success-900/30 text-status-success-800 dark:text-status-success-300' : 'bg-status-danger-50 dark:bg-status-danger-900/30 text-status-danger-800 dark:text-status-danger-300']">
            {{ uploadResult.message }}
          </div>
        </div>
      </div>

      <!-- File Editor Modal -->
      <div v-if="editingFile" class="fixed inset-0 z-50 overflow-y-auto">
        <div class="flex items-center justify-center min-h-screen p-4">
          <div class="fixed inset-0 bg-gray-500 dark:bg-gray-900 bg-opacity-75 dark:bg-opacity-80" @click="closeEditor"></div>

          <div class="relative bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-3xl w-full max-h-[90vh] flex flex-col">
            <div class="px-4 py-3 border-b border-gray-200 dark:border-gray-700 flex justify-between items-center">
              <h3 class="text-lg font-medium text-gray-900 dark:text-white font-mono">
                {{ editingFile }}
              </h3>
              <button
                @click="closeEditor"
                class="text-gray-400 hover:text-gray-500 dark:text-gray-500 dark:hover:text-gray-400"
              >
                <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>

            <div class="flex-1 overflow-y-auto p-4">
              <textarea
                v-model="editingContent"
                :disabled="savingFile"
                rows="20"
                class="w-full font-mono text-sm border border-gray-300 dark:border-gray-600 rounded-lg p-3 bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-100 dark:disabled:bg-gray-800"
                :placeholder="getPlaceholder(editingFile)"
              ></textarea>
            </div>

            <div class="px-4 py-3 border-t border-gray-200 dark:border-gray-700 flex justify-end space-x-3">
              <button
                @click="closeEditor"
                class="px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-200 bg-white dark:bg-gray-700 border border-gray-300 dark:border-gray-600 rounded-md hover:bg-gray-50 dark:hover:bg-gray-600"
              >
                Cancel
              </button>
              <button
                @click="saveFile"
                :disabled="savingFile"
                class="inline-flex items-center px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-md hover:bg-blue-700 disabled:bg-gray-400 disabled:cursor-not-allowed"
              >
                <svg v-if="savingFile" class="animate-spin -ml-0.5 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                  <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                  <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                </svg>
                {{ savingFile ? 'Saving...' : 'Save' }}
              </button>
            </div>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useAgentsStore } from '../stores/agents'
import { useNotification } from '../composables'

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

const agentsStore = useAgentsStore()
const { showNotification } = useNotification()

// State
const loading = ref(false)
const credentialStatus = ref(null)
const exporting = ref(false)
const importing = ref(false)
const quickInjectText = ref('')
const quickInjectLoading = ref(false)
const quickInjectResult = ref(null)
// #11 — credential file upload (service-account JSON, certs/keys, SSH)
const uploadFile = ref(null)
const uploadPath = ref('')
const uploadLoading = ref(false)
const uploadResult = ref(null)
const editingFile = ref(null)
const editingContent = ref('')
const savingFile = ref(false)

// Computed
const credentialFiles = computed(() => {
  const files = credentialStatus.value?.files || {}
  return [
    { name: '.env', ...files['.env'] },
    { name: '.mcp.json', ...files['.mcp.json'] },
    { name: '.mcp.json.template', ...files['.mcp.json.template'] }
  ]
})

const hasCredentialFiles = computed(() => {
  const files = credentialStatus.value?.files || {}
  return files['.env']?.exists || files['.mcp.json']?.exists
})

const hasEncryptedFile = computed(() => {
  const files = credentialStatus.value?.files || {}
  return files['.credentials.enc']?.exists
})

// Methods
const loadCredentialStatus = async () => {
  if (!props.agentName) return
  if (props.agentStatus !== 'running') {
    credentialStatus.value = null
    return
  }

  loading.value = true
  try {
    credentialStatus.value = await agentsStore.getCredentialStatus(props.agentName)
  } catch (err) {
    console.error('Failed to load credential status:', err)
    credentialStatus.value = null
  } finally {
    loading.value = false
  }
}

const countCredentials = (text) => {
  if (!text) return 0
  let count = 0
  for (const line of text.split('\n')) {
    const trimmed = line.trim()
    if (trimmed && !trimmed.startsWith('#') && trimmed.includes('=')) {
      count++
    }
  }
  return count
}

const parseEnvText = (text) => {
  const credentials = {}
  for (const line of text.split('\n')) {
    const trimmed = line.trim()
    if (!trimmed || trimmed.startsWith('#')) continue
    const eqIndex = trimmed.indexOf('=')
    if (eqIndex > 0) {
      const key = trimmed.substring(0, eqIndex).trim()
      let value = trimmed.substring(eqIndex + 1).trim()
      // Remove surrounding quotes
      if ((value.startsWith('"') && value.endsWith('"')) ||
          (value.startsWith("'") && value.endsWith("'"))) {
        value = value.slice(1, -1)
      }
      if (key) {
        credentials[key] = value
      }
    }
  }
  return credentials
}

const formatEnvContent = (credentials) => {
  const lines = ['# Credential file - managed by Trinity', '']
  for (const [key, value] of Object.entries(credentials)) {
    // Escape quotes in values
    const escapedValue = String(value).replace(/"/g, '\\"')
    lines.push(`${key}="${escapedValue}"`)
  }
  return lines.join('\n') + '\n'
}

const quickInject = async () => {
  if (!quickInjectText.value.trim()) return

  quickInjectLoading.value = true
  quickInjectResult.value = null

  try {
    const credentials = parseEnvText(quickInjectText.value)
    const credCount = Object.keys(credentials).length

    if (credCount === 0) {
      quickInjectResult.value = {
        success: false,
        message: 'No valid KEY=VALUE pairs found'
      }
      return
    }

    // Get existing .env content and merge
    let existingEnv = {}
    try {
      const content = await agentsStore.downloadAgentFile(props.agentName, '/home/developer/.env')
      existingEnv = parseEnvText(content)
    } catch (e) {
      // File doesn't exist, start fresh
    }

    // Merge new credentials (overwrite existing keys)
    const merged = { ...existingEnv, ...credentials }
    const envContent = formatEnvContent(merged)

    // Inject the merged .env file
    await agentsStore.injectCredentials(props.agentName, {
      '.env': envContent
    })

    quickInjectResult.value = {
      success: true,
      message: `Injected ${credCount} credential(s) into .env`
    }
    quickInjectText.value = ''
    await loadCredentialStatus()

    if (showNotification) {
      showNotification('Credentials injected', 'success')
    }
  } catch (err) {
    console.error('Quick inject failed:', err)
    quickInjectResult.value = {
      success: false,
      message: err.response?.data?.detail || err.message || 'Failed to inject credentials'
    }
  } finally {
    quickInjectLoading.value = false
  }
}

// #11 — pick a credential file; default the destination path to its name.
const onCredFilePicked = (e) => {
  const f = e.target.files && e.target.files[0]
  uploadFile.value = f || null
  uploadResult.value = null
  if (f && !uploadPath.value.trim()) uploadPath.value = f.name
}

// Read the picked file and inject it: UTF-8-decodable content goes in `files`
// (text), otherwise base64 in `files_b64` (binary). Server enforces the policy.
const uploadCredFile = async () => {
  if (!uploadFile.value || !uploadPath.value.trim()) return
  uploadLoading.value = true
  uploadResult.value = null
  try {
    const buf = await uploadFile.value.arrayBuffer()
    const bytes = new Uint8Array(buf)
    const path = uploadPath.value.trim()
    let files = {}, filesB64 = {}
    try {
      // strict UTF-8 decode — throws on invalid byte sequences (binary)
      files[path] = new TextDecoder('utf-8', { fatal: true }).decode(bytes)
    } catch {
      let bin = ''
      for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i])
      filesB64[path] = btoa(bin)
    }
    await agentsStore.injectCredentials(props.agentName, files, filesB64)
    uploadResult.value = { success: true, message: `Uploaded ${path}` }
    uploadFile.value = null
    uploadPath.value = ''
    await loadCredentialStatus()
    if (showNotification) showNotification('Credential file uploaded', 'success')
  } catch (err) {
    uploadResult.value = {
      success: false,
      message: err.response?.data?.detail || err.message || 'Upload failed',
    }
  } finally {
    uploadLoading.value = false
  }
}

const exportToGit = async () => {
  exporting.value = true
  try {
    const result = await agentsStore.exportCredentials(props.agentName)
    await loadCredentialStatus()
    if (showNotification) {
      showNotification(`Exported ${result.files_exported} file(s) to .credentials.enc`, 'success')
    }
  } catch (err) {
    console.error('Export failed:', err)
    if (showNotification) {
      showNotification(err.response?.data?.detail || 'Export failed', 'error')
    }
  } finally {
    exporting.value = false
  }
}

const importFromGit = async () => {
  importing.value = true
  try {
    const result = await agentsStore.importCredentials(props.agentName)
    await loadCredentialStatus()
    if (showNotification) {
      showNotification(`Imported ${result.files_imported.length} file(s) from .credentials.enc`, 'success')
    }
  } catch (err) {
    console.error('Import failed:', err)
    if (showNotification) {
      showNotification(err.response?.data?.detail || 'Import failed', 'error')
    }
  } finally {
    importing.value = false
  }
}

const viewFile = async (filename) => {
  try {
    const content = await agentsStore.downloadAgentFile(props.agentName, `/home/developer/${filename}`)
    editingFile.value = filename
    editingContent.value = content
  } catch (err) {
    console.error('Failed to read file:', err)
    if (showNotification) {
      showNotification('Failed to read file', 'error')
    }
  }
}

const editFile = async (filename) => {
  editingFile.value = filename
  try {
    const content = await agentsStore.downloadAgentFile(props.agentName, `/home/developer/${filename}`)
    editingContent.value = content
  } catch (err) {
    // File doesn't exist, start with empty content or placeholder
    editingContent.value = ''
  }
}

const saveFile = async () => {
  if (!editingFile.value) return

  savingFile.value = true
  try {
    await agentsStore.injectCredentials(props.agentName, {
      [editingFile.value]: editingContent.value
    })

    await loadCredentialStatus()
    closeEditor()

    if (showNotification) {
      showNotification(`Saved ${editingFile.value}`, 'success')
    }
  } catch (err) {
    console.error('Failed to save file:', err)
    if (showNotification) {
      showNotification(err.response?.data?.detail || 'Failed to save file', 'error')
    }
  } finally {
    savingFile.value = false
  }
}

const closeEditor = () => {
  editingFile.value = null
  editingContent.value = ''
}

const getPlaceholder = (filename) => {
  const placeholders = {
    '.env': 'OPENAI_API_KEY=sk-...\nANTHROPIC_API_KEY=sk-ant-...',
    // `.mcp.json` content is structure-validated at save (#598).
    // The `trinity` server name is reserved (auto-injected on agent start);
    // the example shows a real MCP server (context7) with the validated
    // shape: command from allowlist (npx/uvx/python/python3/node/bun/deno/docker),
    // args without shell metacharacters, env values as ${VAR} or safe literals.
    '.mcp.json': '{\n  "mcpServers": {\n    "context7": {\n      "command": "npx",\n      "args": ["-y", "@upstash/context7-mcp@latest"]\n    }\n  }\n}',
    '.mcp.json.template': '{\n  "mcpServers": {}\n}'
  }
  return placeholders[filename] || ''
}

const formatFileSize = (bytes) => {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let unitIndex = 0
  let size = bytes
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024
    unitIndex++
  }
  return `${size.toFixed(unitIndex > 0 ? 1 : 0)} ${units[unitIndex]}`
}

const formatDate = (isoString) => {
  if (!isoString) return ''
  const date = new Date(isoString)
  return date.toLocaleDateString() + ' ' + date.toLocaleTimeString()
}

// Lifecycle
onMounted(() => {
  loadCredentialStatus()
})

// Watch for agent status changes
watch(() => props.agentStatus, () => {
  loadCredentialStatus()
})

watch(() => props.agentName, () => {
  loadCredentialStatus()
})
</script>
