<template>
  <div class="min-h-screen bg-gray-100 dark:bg-gray-900">
    <NavBar />

    <!-- Notification Banner -->
    <div v-if="notification"
      class="max-w-full mx-auto px-4 sm:px-6 lg:px-8 py-2"
    >
      <div
        class="px-4 py-3 rounded-md text-sm"
        :class="notification.type === 'success' ? 'bg-status-success-100 dark:bg-status-success-900/50 border border-status-success-400 dark:border-status-success-700 text-status-success-700 dark:text-status-success-300' : 'bg-status-danger-100 dark:bg-status-danger-900/50 border border-status-danger-400 dark:border-status-danger-700 text-status-danger-700 dark:text-status-danger-300'"
      >
        {{ notification.message }}
      </div>
    </div>

    <!-- Main Content -->
    <div class="px-2 py-1">
      <!-- Two Panel Layout -->
      <div class="flex h-[calc(100vh-70px)] bg-white dark:bg-gray-800 rounded-lg shadow overflow-hidden">
        <!-- Left Panel: File Tree -->
        <div class="w-80 min-w-[280px] max-w-[400px] border-r border-gray-200 dark:border-gray-700 flex flex-col">
          <!-- Compact Controls Row: Agent dropdown + Refresh + Hidden toggle -->
          <div class="p-2 border-b border-gray-200 dark:border-gray-700 flex items-center gap-2">
            <select
              v-model="selectedAgentName"
              class="flex-1 min-w-0 text-sm rounded border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white py-1.5 focus:border-indigo-500 focus:ring-indigo-500"
              @change="onAgentChange"
            >
              <option value="">Select agent...</option>
              <option
                v-for="agent in runningAgents"
                :key="agent.name"
                :value="agent.name"
              >
                {{ agent.name }}
              </option>
            </select>
            <button
              @click="loadFiles"
              :disabled="!selectedAgentName || loading"
              class="p-1.5 text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700 rounded disabled:opacity-50"
              title="Refresh"
            >
              <svg class="h-4 w-4" :class="{ 'animate-spin': loading }" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
              </svg>
            </button>
            <label class="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400 cursor-pointer whitespace-nowrap" title="Show hidden files">
              <input
                type="checkbox"
                v-model="showHidden"
                @change="loadFiles"
                class="rounded border-gray-300 dark:border-gray-600 dark:bg-gray-700 text-indigo-600 focus:ring-indigo-500 h-3.5 w-3.5"
              />
              <span>Hidden</span>
            </label>
          </div>

          <!-- Search Box -->
          <div class="p-2 border-b border-gray-200 dark:border-gray-700">
            <div class="relative">
              <input
                v-model="searchQuery"
                type="text"
                placeholder="Search files..."
                class="w-full pl-7 pr-3 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded dark:bg-gray-700 dark:text-white focus:ring-indigo-500 focus:border-indigo-500"
              />
              <svg class="absolute left-2 top-2 h-4 w-4 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
              </svg>
            </div>
          </div>

          <!-- Tree View -->
          <div class="flex-1 overflow-auto p-2">
            <!-- No Agent Selected -->
            <div v-if="!selectedAgentName" class="text-center py-8">
              <svg class="mx-auto h-10 w-10 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
              </svg>
              <p class="mt-2 text-sm text-gray-500 dark:text-gray-400">Select an agent</p>
              <p v-if="runningAgents.length === 0" class="mt-1 text-xs text-status-warning-600 dark:text-status-warning-400">
                No running agents
              </p>
            </div>
            <div v-else-if="loading" class="flex items-center justify-center h-32">
              <svg class="animate-spin h-6 w-6 text-indigo-600" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
              </svg>
            </div>
            <div v-else-if="error" class="text-status-danger-500 text-sm p-2">{{ error }}</div>
            <div v-else-if="filteredTree.length === 0" class="text-gray-500 dark:text-gray-400 text-sm p-2 text-center">
              {{ searchQuery ? 'No matching files found' : 'This folder is empty' }}
            </div>
            <FileTreeNode
              v-else
              v-for="item in filteredTree"
              :key="item.path"
              :item="item"
              :selected-path="selectedFile?.path"
              :search-query="searchQuery"
              @select="onFileSelect"
            />
          </div>

          <!-- Footer Stats -->
          <div class="px-2 py-1.5 border-t border-gray-200 dark:border-gray-700 text-xs text-gray-500 dark:text-gray-400">
            <span v-if="selectedAgentName">{{ totalFiles }} files<span v-if="totalSize > 0"> &bull; {{ formatFileSize(totalSize) }}</span></span>
            <span v-else>&nbsp;</span>
          </div>
        </div>

        <!-- Right Panel: Preview -->
        <div class="flex-1 flex flex-col bg-gray-50 dark:bg-gray-900">
          <!-- No File Selected -->
          <div v-if="!selectedFile" class="flex-1 flex items-center justify-center">
            <div class="text-center">
              <svg class="mx-auto h-12 w-12 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
              </svg>
              <h3 class="mt-2 text-sm font-medium text-gray-900 dark:text-white">No File Selected</h3>
              <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">Select a file from the tree to preview</p>
            </div>
          </div>

          <!-- File Preview -->
          <template v-else>
            <!-- Preview Area -->
            <div class="flex-1 overflow-auto p-4">
              <FilePreview
                :file="selectedFile"
                :agent-name="selectedAgentName"
                :preview-data="previewData"
                :preview-loading="previewLoading"
                :preview-error="previewError"
                :is-editing="isEditing"
                :edit-content="editContent"
                @update:edit-content="onEditContentChange"
              />
            </div>

            <!-- File Info & Actions -->
            <div class="p-4 border-t border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800">
              <div class="flex items-center justify-between">
                <div>
                  <h3 class="text-sm font-medium text-gray-900 dark:text-white truncate max-w-md">
                    {{ selectedFile.name }}
                  </h3>
                  <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
                    {{ selectedFile.path }}
                    <span v-if="selectedFile.size"> &bull; {{ formatFileSize(selectedFile.size) }}</span>
                    <span v-if="selectedFile.modified"> &bull; {{ formatDate(selectedFile.modified) }}</span>
                  </p>
                </div>
                <div class="flex items-center space-x-2">
                  <!-- Edit Mode Actions -->
                  <template v-if="isEditing">
                    <button
                      @click="saveFile"
                      :disabled="saving || !hasUnsavedChanges"
                      class="inline-flex items-center px-3 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 disabled:opacity-50"
                    >
                      <svg v-if="saving" class="animate-spin -ml-1 mr-2 h-4 w-4 text-white" fill="none" viewBox="0 0 24 24">
                        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                      </svg>
                      <svg v-else class="h-4 w-4 mr-1" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                      </svg>
                      Save
                    </button>
                    <button
                      @click="cancelEdit"
                      :disabled="saving"
                      class="inline-flex items-center px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm text-sm font-medium text-gray-700 dark:text-gray-200 bg-white dark:bg-gray-700 hover:bg-gray-50 dark:hover:bg-gray-600 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 disabled:opacity-50"
                    >
                      Cancel
                    </button>
                  </template>
                  <!-- View Mode Actions -->
                  <template v-else>
                    <!-- Edit Button (only for text files, not edit-protected) -->
                    <button
                      v-if="isTextFile && !isEditProtected"
                      @click="startEdit"
                      class="inline-flex items-center px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm text-sm font-medium text-gray-700 dark:text-gray-200 bg-white dark:bg-gray-700 hover:bg-gray-50 dark:hover:bg-gray-600 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500"
                    >
                      <svg class="h-4 w-4 mr-1" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
                      </svg>
                      Edit
                    </button>
                    <button
                      @click="downloadFile"
                      :disabled="downloading"
                      class="inline-flex items-center px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm text-sm font-medium text-gray-700 dark:text-gray-200 bg-white dark:bg-gray-700 hover:bg-gray-50 dark:hover:bg-gray-600 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 disabled:opacity-50"
                    >
                      <svg class="h-4 w-4 mr-1" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
                      </svg>
                      Download
                    </button>
                    <button
                      @click="showDeleteConfirm = true"
                      :disabled="deleting || isDeleteProtected"
                      class="inline-flex items-center px-3 py-2 border border-status-danger-300 dark:border-status-danger-600 rounded-md shadow-sm text-sm font-medium text-status-danger-700 dark:text-status-danger-400 bg-white dark:bg-gray-700 hover:bg-status-danger-50 dark:hover:bg-status-danger-900/20 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-status-danger-500 disabled:opacity-50"
                      :title="isDeleteProtected ? 'Protected file cannot be deleted' : ''"
                    >
                      <svg class="h-4 w-4 mr-1" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                      </svg>
                      Delete
                    </button>
                  </template>
                </div>
              </div>
              <p v-if="isDeleteProtected && !isEditing" class="mt-2 text-xs text-status-warning-600 dark:text-status-warning-400">
                This is a protected system file and cannot be deleted.
              </p>
              <p v-if="isEditing && hasUnsavedChanges" class="mt-2 text-xs text-status-urgent-600 dark:text-status-urgent-400">
                You have unsaved changes.
              </p>
            </div>
          </template>
        </div>
      </div>
    </div>

    <!-- Delete Confirmation Modal -->
    <div v-if="showDeleteConfirm" class="fixed inset-0 z-50 overflow-y-auto">
      <div class="flex items-center justify-center min-h-screen px-4 pt-4 pb-20 text-center">
        <div class="fixed inset-0 bg-gray-500 bg-opacity-75 transition-opacity" @click="showDeleteConfirm = false"></div>
        <div class="relative bg-white dark:bg-gray-800 rounded-lg px-4 pt-5 pb-4 text-left overflow-hidden shadow-xl transform transition-all sm:my-8 sm:max-w-lg sm:w-full sm:p-6">
          <div class="sm:flex sm:items-start">
            <div class="mx-auto flex-shrink-0 flex items-center justify-center h-12 w-12 rounded-full bg-status-danger-100 dark:bg-status-danger-900/30 sm:mx-0 sm:h-10 sm:w-10">
              <svg class="h-6 w-6 text-status-danger-600 dark:text-status-danger-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
              </svg>
            </div>
            <div class="mt-3 text-center sm:mt-0 sm:ml-4 sm:text-left">
              <h3 class="text-lg leading-6 font-medium text-gray-900 dark:text-white">
                Delete {{ selectedFile?.type === 'directory' ? 'Folder' : 'File' }}
              </h3>
              <div class="mt-2">
                <p class="text-sm text-gray-500 dark:text-gray-400">
                  Are you sure you want to delete <strong class="text-gray-900 dark:text-white">{{ selectedFile?.name }}</strong>?
                  <span v-if="selectedFile?.type === 'directory'">
                    This will delete all {{ selectedFile?.file_count || 0 }} files inside.
                  </span>
                  This action cannot be undone.
                </p>
              </div>
            </div>
          </div>
          <div class="mt-5 sm:mt-4 sm:flex sm:flex-row-reverse">
            <button
              @click="deleteFile"
              :disabled="deleting"
              class="w-full inline-flex justify-center rounded-md border border-transparent shadow-sm px-4 py-2 bg-status-danger-600 text-base font-medium text-white hover:bg-status-danger-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-status-danger-500 sm:ml-3 sm:w-auto sm:text-sm disabled:opacity-50"
            >
              <svg v-if="deleting" class="animate-spin -ml-1 mr-2 h-4 w-4 text-white" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
              </svg>
              Delete
            </button>
            <button
              @click="showDeleteConfirm = false"
              class="mt-3 w-full inline-flex justify-center rounded-md border border-gray-300 dark:border-gray-600 shadow-sm px-4 py-2 bg-white dark:bg-gray-700 text-base font-medium text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-600 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 sm:mt-0 sm:w-auto sm:text-sm"
            >
              Cancel
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useAgentsStore } from '@/stores/agents'
import NavBar from '@/components/NavBar.vue'
import FileTreeNode from '@/components/file-manager/FileTreeNode.vue'
import FilePreview from '@/components/file-manager/FilePreview.vue'

const agentsStore = useAgentsStore()

// Notification system
const notification = ref(null)
const showNotification = (message, type = 'success') => {
  notification.value = { message, type }
  setTimeout(() => {
    notification.value = null
  }, 4000)
}

// State
const selectedAgentName = ref(localStorage.getItem('fileManager.selectedAgent') || '')
const fileTree = ref([])
const selectedFile = ref(null)
const searchQuery = ref('')
const loading = ref(false)
const error = ref(null)
const previewData = ref(null)
const previewLoading = ref(false)
const previewError = ref(null)
const downloading = ref(false)
const deleting = ref(false)
const showDeleteConfirm = ref(false)
const showHidden = ref(localStorage.getItem('fileManager.showHidden') === 'true')
// Edit mode state
const isEditing = ref(false)
const editContent = ref('')
const saving = ref(false)
const hasUnsavedChanges = ref(false)

// Protected paths (cannot be deleted)
const DELETE_PROTECTED_PATHS = ['CLAUDE.md', '.trinity', '.git', '.gitignore', '.env', '.mcp.json', '.mcp.json.template']

// Edit protected paths (cannot be edited) - CLAUDE.md and .mcp.json ARE editable
const EDIT_PROTECTED_PATHS = ['.trinity', '.git', '.gitignore', '.env', '.mcp.json.template']

// Computed
const runningAgents = computed(() => agentsStore.runningAgents)

const totalFiles = computed(() => {
  const countFiles = (items) => {
    let count = 0
    for (const item of items) {
      if (item.type === 'file') count++
      if (item.children) count += countFiles(item.children)
    }
    return count
  }
  return countFiles(fileTree.value)
})

const totalSize = computed(() => {
  const sumSize = (items) => {
    let size = 0
    for (const item of items) {
      if (item.type === 'file' && item.size) size += item.size
      if (item.children) size += sumSize(item.children)
    }
    return size
  }
  return sumSize(fileTree.value)
})

const filteredTree = computed(() => {
  if (!searchQuery.value) return fileTree.value

  const query = searchQuery.value.toLowerCase()
  const filterItems = (items) => {
    const result = []
    for (const item of items) {
      const matches = item.name.toLowerCase().includes(query)
      if (item.type === 'directory') {
        const filteredChildren = filterItems(item.children || [])
        if (matches || filteredChildren.length > 0) {
          result.push({ ...item, children: filteredChildren, expanded: true })
        }
      } else if (matches) {
        result.push(item)
      }
    }
    return result
  }
  return filterItems(fileTree.value)
})

const isDeleteProtected = computed(() => {
  if (!selectedFile.value) return false
  const name = selectedFile.value.name
  return DELETE_PROTECTED_PATHS.includes(name)
})

const isEditProtected = computed(() => {
  if (!selectedFile.value) return false
  const name = selectedFile.value.name
  return EDIT_PROTECTED_PATHS.includes(name)
})

// Text file extensions that can be edited
const TEXT_EXTENSIONS = [
  'txt', 'md', 'json', 'yaml', 'yml', 'toml', 'xml', 'csv', 'log',
  'js', 'ts', 'jsx', 'tsx', 'py', 'go', 'rs', 'rb', 'java', 'c', 'cpp', 'h',
  'css', 'scss', 'sass', 'less', 'html', 'vue', 'svelte',
  'sh', 'bash', 'zsh', 'ps1', 'bat', 'cmd',
  'sql', 'graphql', 'prisma', 'dockerfile', 'makefile',
  'env', 'ini', 'conf', 'cfg', 'gitignore', 'editorconfig'
]

const isTextFile = computed(() => {
  if (!selectedFile.value || selectedFile.value.type !== 'file') return false
  const ext = selectedFile.value.name.split('.').pop()?.toLowerCase() || ''
  return TEXT_EXTENSIONS.includes(ext)
})

// Methods
const loadFiles = async () => {
  if (!selectedAgentName.value) return

  loading.value = true
  error.value = null
  // Persist showHidden preference
  localStorage.setItem('fileManager.showHidden', showHidden.value)
  try {
    const data = await agentsStore.listAgentFiles(selectedAgentName.value, '/home/developer', showHidden.value)
    fileTree.value = data.tree || []
  } catch (e) {
    error.value = e.response?.data?.detail || e.message
    showNotification(`Failed to load files: ${error.value}`, 'error')
  } finally {
    loading.value = false
  }
}

const onAgentChange = () => {
  localStorage.setItem('fileManager.selectedAgent', selectedAgentName.value)
  selectedFile.value = null
  previewData.value = null
  loadFiles()
}

const onFileSelect = async (item) => {
  // If in edit mode with unsaved changes, confirm before switching
  if (isEditing.value && hasUnsavedChanges.value) {
    if (!confirm('You have unsaved changes. Are you sure you want to switch files?')) {
      return
    }
  }

  // Reset edit state
  isEditing.value = false
  editContent.value = ''
  hasUnsavedChanges.value = false

  selectedFile.value = item
  previewData.value = null
  previewError.value = null

  if (item.type === 'file') {
    await loadPreview(item)
  }
}

const loadPreview = async (file) => {
  previewLoading.value = true
  previewError.value = null
  try {
    const data = await agentsStore.getFilePreviewBlob(selectedAgentName.value, file.path)
    previewData.value = data
  } catch (e) {
    previewError.value = e.response?.data?.detail || e.message
  } finally {
    previewLoading.value = false
  }
}

const downloadFile = async () => {
  if (!selectedFile.value) return

  downloading.value = true
  try {
    // Use the preview blob if available, otherwise fetch
    let blob
    if (previewData.value?.url) {
      const response = await fetch(previewData.value.url)
      blob = await response.blob()
    } else {
      const data = await agentsStore.getFilePreviewBlob(selectedAgentName.value, selectedFile.value.path)
      blob = await fetch(data.url).then(r => r.blob())
      URL.revokeObjectURL(data.url)
    }

    // Create download link
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = selectedFile.value.name
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)

    showNotification(`Downloaded ${selectedFile.value.name}`)
  } catch (e) {
    showNotification(`Failed to download: ${e.message}`, 'error')
  } finally {
    downloading.value = false
  }
}

const deleteFile = async () => {
  if (!selectedFile.value) return

  deleting.value = true
  try {
    await agentsStore.deleteAgentFile(selectedAgentName.value, selectedFile.value.path)
    showNotification(`Deleted ${selectedFile.value.name}`)
    showDeleteConfirm.value = false

    // Cleanup preview URL
    if (previewData.value?.url) {
      URL.revokeObjectURL(previewData.value.url)
    }

    selectedFile.value = null
    previewData.value = null
    await loadFiles()
  } catch (e) {
    const errorMsg = e.response?.data?.detail || e.message
    showNotification(`Failed to delete: ${errorMsg}`, 'error')
  } finally {
    deleting.value = false
  }
}

// Edit mode methods
const startEdit = async () => {
  if (!previewData.value?.url) return

  try {
    // Fetch the text content from the preview blob
    const response = await fetch(previewData.value.url)
    const text = await response.text()
    editContent.value = text
    isEditing.value = true
    hasUnsavedChanges.value = false
  } catch (e) {
    showNotification('Failed to load file for editing', 'error')
  }
}

const cancelEdit = () => {
  if (hasUnsavedChanges.value) {
    if (!confirm('You have unsaved changes. Are you sure you want to cancel?')) {
      return
    }
  }
  isEditing.value = false
  editContent.value = ''
  hasUnsavedChanges.value = false
}

const onEditContentChange = (newContent) => {
  editContent.value = newContent
  hasUnsavedChanges.value = true
}

const saveFile = async () => {
  if (!selectedFile.value) return

  saving.value = true
  try {
    await agentsStore.updateAgentFile(
      selectedAgentName.value,
      selectedFile.value.path,
      editContent.value
    )
    showNotification(`Saved ${selectedFile.value.name}`)
    isEditing.value = false
    hasUnsavedChanges.value = false

    // Reload the preview to show updated content
    await loadPreview(selectedFile.value)
  } catch (e) {
    const errorMsg = e.response?.data?.detail || e.message
    showNotification(`Failed to save: ${errorMsg}`, 'error')
  } finally {
    saving.value = false
  }
}

const formatFileSize = (bytes) => {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i]
}

const formatDate = (isoString) => {
  if (!isoString) return ''
  const date = new Date(isoString)
  const now = new Date()
  const diffMs = now - date
  const diffMins = Math.floor(diffMs / 60000)
  const diffHours = Math.floor(diffMs / 3600000)
  const diffDays = Math.floor(diffMs / 86400000)

  if (diffMins < 1) return 'just now'
  if (diffMins < 60) return `${diffMins}m ago`
  if (diffHours < 24) return `${diffHours}h ago`
  if (diffDays < 7) return `${diffDays}d ago`
  return date.toLocaleDateString()
}

// Lifecycle
onMounted(async () => {
  await agentsStore.fetchAgents()
  if (selectedAgentName.value) {
    // Verify agent still exists and is running
    const agent = runningAgents.value.find(a => a.name === selectedAgentName.value)
    if (!agent) {
      selectedAgentName.value = ''
      localStorage.removeItem('fileManager.selectedAgent')
    } else {
      loadFiles()
    }
  }
})

onUnmounted(() => {
  // Cleanup blob URLs
  if (previewData.value?.url) {
    URL.revokeObjectURL(previewData.value.url)
  }
})

// Watch for agent list changes
watch(runningAgents, (agents) => {
  if (selectedAgentName.value) {
    const stillRunning = agents.find(a => a.name === selectedAgentName.value)
    if (!stillRunning) {
      showNotification(`Agent ${selectedAgentName.value} is no longer running`, 'error')
      selectedAgentName.value = ''
      fileTree.value = []
      selectedFile.value = null
    }
  }
})
</script>
