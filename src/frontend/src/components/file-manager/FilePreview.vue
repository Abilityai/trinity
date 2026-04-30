<template>
  <div class="h-full flex flex-col">
    <!-- Loading State -->
    <div v-if="previewLoading" class="flex-1 flex items-center justify-center">
      <div class="text-center">
        <svg class="animate-spin h-8 w-8 text-indigo-600 mx-auto" fill="none" viewBox="0 0 24 24">
          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
          <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
        </svg>
        <p class="mt-2 text-sm text-gray-500 dark:text-gray-400">Loading preview...</p>
      </div>
    </div>

    <!-- Error State -->
    <div v-else-if="previewError" class="flex-1 flex items-center justify-center">
      <div class="text-center">
        <svg class="h-12 w-12 text-status-danger-400 mx-auto" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
        </svg>
        <p class="mt-2 text-sm text-status-danger-500">{{ previewError }}</p>
      </div>
    </div>

    <!-- Directory Preview -->
    <div v-else-if="file.type === 'directory'" class="flex-1 flex items-center justify-center">
      <div class="text-center">
        <svg class="h-16 w-16 text-yellow-500 mx-auto" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
        </svg>
        <h3 class="mt-4 text-lg font-medium text-gray-900 dark:text-white">{{ file.name }}</h3>
        <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
          {{ file.file_count || 0 }} items
        </p>
      </div>
    </div>

    <!-- File Preview -->
    <template v-else-if="previewData">
      <!-- Image Preview -->
      <div v-if="isImage" class="flex-1 flex items-center justify-center p-4 overflow-auto">
        <img
          :src="previewData.url"
          :alt="file.name"
          class="max-w-full max-h-full object-contain rounded shadow-lg"
          @load="onImageLoad"
        />
      </div>

      <!-- Video Preview -->
      <div v-else-if="isVideo" class="flex-1 flex items-center justify-center p-4">
        <video
          :src="previewData.url"
          controls
          class="max-w-full max-h-full rounded shadow-lg"
          preload="metadata"
        >
          Your browser does not support video playback.
        </video>
      </div>

      <!-- Audio Preview -->
      <div v-else-if="isAudio" class="flex-1 flex items-center justify-center p-4">
        <div class="w-full max-w-md bg-white dark:bg-gray-800 rounded-lg shadow-lg p-6">
          <div class="flex items-center justify-center mb-4">
            <svg class="h-16 w-16 text-green-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19V6l12-3v13M9 19c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zm12-3c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zM9 10l12-3" />
            </svg>
          </div>
          <h3 class="text-center text-lg font-medium text-gray-900 dark:text-white mb-4 truncate">
            {{ file.name }}
          </h3>
          <audio
            :src="previewData.url"
            controls
            class="w-full"
            preload="metadata"
          >
            Your browser does not support audio playback.
          </audio>
        </div>
      </div>

      <!-- PDF Preview -->
      <div v-else-if="isPdf" class="flex-1 overflow-hidden">
        <embed
          :src="previewData.url"
          type="application/pdf"
          class="w-full h-full"
        />
      </div>

      <!-- Text/Code Preview or Edit -->
      <div v-else-if="isText" class="flex-1 overflow-auto flex flex-col">
        <!-- Edit Mode: Textarea -->
        <textarea
          v-if="props.isEditing"
          :value="props.editContent"
          @input="emit('update:editContent', $event.target.value)"
          class="flex-1 p-4 text-sm font-mono bg-gray-900 text-gray-100 m-2 rounded-lg resize-none focus:outline-none focus:ring-2 focus:ring-indigo-500"
          spellcheck="false"
        ></textarea>
        <!-- View Mode: Pre/Code -->
        <pre
          v-else
          class="p-4 text-sm font-mono bg-gray-900 text-gray-100 rounded-lg m-2 overflow-auto whitespace-pre-wrap"
        ><code>{{ textContent }}</code></pre>
      </div>

      <!-- Fallback Preview (binary/unknown) -->
      <div v-else class="flex-1 flex items-center justify-center">
        <div class="text-center">
          <svg class="h-16 w-16 text-gray-400 mx-auto" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 21h10a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v14a2 2 0 002 2z" />
          </svg>
          <h3 class="mt-4 text-lg font-medium text-gray-900 dark:text-white">{{ file.name }}</h3>
          <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
            Preview not available for this file type
          </p>
          <p class="mt-2 text-xs text-gray-400 dark:text-gray-500">
            {{ previewData.type || 'Unknown type' }}
          </p>
        </div>
      </div>
    </template>

    <!-- No Preview Data -->
    <div v-else class="flex-1 flex items-center justify-center">
      <div class="text-center">
        <svg class="h-12 w-12 text-gray-400 mx-auto" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
        </svg>
        <p class="mt-2 text-sm text-gray-500 dark:text-gray-400">Loading preview...</p>
      </div>
    </div>

    <!-- Image Dimensions (shown after load) -->
    <div v-if="imageDimensions && isImage" class="absolute bottom-4 right-4 bg-black/50 text-white text-xs px-2 py-1 rounded">
      {{ imageDimensions.width }} x {{ imageDimensions.height }}
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onUnmounted } from 'vue'

const props = defineProps({
  file: { type: Object, required: true },
  agentName: { type: String, required: true },
  previewData: { type: Object, default: null },
  previewLoading: { type: Boolean, default: false },
  previewError: { type: String, default: null },
  // Edit mode props
  isEditing: { type: Boolean, default: false },
  editContent: { type: String, default: '' }
})

const emit = defineEmits(['update:editContent'])

// State
const textContent = ref('')
const imageDimensions = ref(null)

// File type detection based on extension
const fileExtension = computed(() => {
  const parts = props.file.name.split('.')
  return parts.length > 1 ? parts.pop().toLowerCase() : ''
})

const isImage = computed(() => {
  if (!props.previewData) return false
  const ext = fileExtension.value
  const imageExts = ['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'ico', 'bmp']
  const mimeMatch = props.previewData.type?.startsWith('image/')
  return imageExts.includes(ext) || mimeMatch
})

const isVideo = computed(() => {
  if (!props.previewData) return false
  const ext = fileExtension.value
  const videoExts = ['mp4', 'webm', 'mov', 'avi', 'mkv']
  const mimeMatch = props.previewData.type?.startsWith('video/')
  return videoExts.includes(ext) || mimeMatch
})

const isAudio = computed(() => {
  if (!props.previewData) return false
  const ext = fileExtension.value
  const audioExts = ['mp3', 'wav', 'ogg', 'm4a', 'flac', 'aac']
  const mimeMatch = props.previewData.type?.startsWith('audio/')
  return audioExts.includes(ext) || mimeMatch
})

const isPdf = computed(() => {
  if (!props.previewData) return false
  const ext = fileExtension.value
  const mimeMatch = props.previewData.type === 'application/pdf'
  return ext === 'pdf' || mimeMatch
})

const isText = computed(() => {
  if (!props.previewData) return false
  const ext = fileExtension.value
  const textExts = [
    'txt', 'md', 'json', 'yaml', 'yml', 'toml', 'xml', 'csv', 'log',
    'js', 'ts', 'jsx', 'tsx', 'py', 'go', 'rs', 'rb', 'java', 'c', 'cpp', 'h',
    'css', 'scss', 'sass', 'less', 'html', 'vue', 'svelte',
    'sh', 'bash', 'zsh', 'ps1', 'bat', 'cmd',
    'sql', 'graphql', 'prisma', 'dockerfile', 'makefile',
    'env', 'ini', 'conf', 'cfg', 'gitignore', 'editorconfig'
  ]
  const mimeMatch = props.previewData.type?.startsWith('text/') ||
    props.previewData.type === 'application/json' ||
    props.previewData.type === 'application/javascript' ||
    props.previewData.type === 'application/x-yaml'
  return textExts.includes(ext) || mimeMatch
})

// Load text content from blob
watch(() => props.previewData, async (data) => {
  if (data && isText.value) {
    try {
      const response = await fetch(data.url)
      textContent.value = await response.text()
    } catch (e) {
      textContent.value = 'Failed to load text content'
    }
  } else {
    textContent.value = ''
  }
  imageDimensions.value = null
}, { immediate: true })

// Methods
const onImageLoad = (event) => {
  imageDimensions.value = {
    width: event.target.naturalWidth,
    height: event.target.naturalHeight
  }
}

onUnmounted(() => {
  // Parent handles blob URL cleanup
})
</script>
