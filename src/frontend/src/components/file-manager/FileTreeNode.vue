<template>
  <div class="select-none">
    <!-- Folder or File Row -->
    <div
      @click="handleClick"
      :class="[
        'flex items-center px-2 py-1 rounded cursor-pointer text-sm',
        isSelected ? 'bg-indigo-100 dark:bg-indigo-900/30 text-indigo-900 dark:text-indigo-100' : 'hover:bg-gray-100 dark:hover:bg-gray-700',
        isMatched ? 'bg-status-warning-50 dark:bg-status-warning-900/20' : ''
      ]"
    >
      <!-- Expand/Collapse Icon (folders only) -->
      <span v-if="item.type === 'directory'" class="w-4 h-4 mr-1 flex items-center justify-center">
        <svg
          :class="['h-3 w-3 text-gray-400 transition-transform', expanded ? 'rotate-90' : '']"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
        >
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
        </svg>
      </span>
      <span v-else class="w-4 h-4 mr-1"></span>

      <!-- File/Folder Icon -->
      <component :is="fileIcon" class="h-4 w-4 mr-2 flex-shrink-0" :class="iconColor" />

      <!-- Name -->
      <span class="truncate text-gray-700 dark:text-gray-200" :title="item.name">
        {{ item.name }}
      </span>

      <!-- Size (files only) -->
      <span v-if="item.type === 'file' && item.size" class="ml-auto text-xs text-gray-400 dark:text-gray-500 flex-shrink-0">
        {{ formatSize(item.size) }}
      </span>

      <!-- File count (folders only) -->
      <span v-if="item.type === 'directory' && item.file_count" class="ml-auto text-xs text-gray-400 dark:text-gray-500 flex-shrink-0">
        {{ item.file_count }}
      </span>
    </div>

    <!-- Children (expanded folders) -->
    <div v-if="item.type === 'directory' && expanded" class="ml-4">
      <FileTreeNode
        v-for="child in item.children"
        :key="child.path"
        :item="child"
        :selected-path="selectedPath"
        :search-query="searchQuery"
        @select="$emit('select', $event)"
      />
      <div v-if="!item.children?.length" class="px-2 py-1 text-xs text-gray-400 dark:text-gray-500 italic">
        Empty folder
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch } from 'vue'

const props = defineProps({
  item: { type: Object, required: true },
  selectedPath: { type: String, default: null },
  searchQuery: { type: String, default: '' }
})

const emit = defineEmits(['select'])

// Local state for expansion (auto-expand if search matches)
const expanded = ref(props.item.expanded || false)

// Watch for search-triggered expansion
watch(() => props.item.expanded, (val) => {
  if (val) expanded.value = true
})

// Computed
const isSelected = computed(() => props.selectedPath === props.item.path)

const isMatched = computed(() => {
  if (!props.searchQuery) return false
  return props.item.name.toLowerCase().includes(props.searchQuery.toLowerCase())
})

const fileExtension = computed(() => {
  const parts = props.item.name.split('.')
  return parts.length > 1 ? parts.pop().toLowerCase() : ''
})

const fileIcon = computed(() => {
  if (props.item.type === 'directory') {
    return expanded.value ? 'FolderOpenIcon' : 'FolderIcon'
  }

  const ext = fileExtension.value
  // Video
  if (['mp4', 'webm', 'mov', 'avi', 'mkv'].includes(ext)) return 'FilmIcon'
  // Audio
  if (['mp3', 'wav', 'ogg', 'm4a', 'flac', 'aac'].includes(ext)) return 'MusicalNoteIcon'
  // Image
  if (['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'ico', 'bmp'].includes(ext)) return 'PhotoIcon'
  // Code
  if (['js', 'ts', 'jsx', 'tsx', 'py', 'go', 'rs', 'rb', 'java', 'c', 'cpp', 'h', 'css', 'scss', 'html', 'vue', 'svelte'].includes(ext)) return 'CodeBracketIcon'
  // Document
  if (['pdf'].includes(ext)) return 'DocumentIcon'
  // Text
  if (['txt', 'md', 'json', 'yaml', 'yml', 'toml', 'xml', 'csv', 'log', 'sh', 'bash', 'zsh'].includes(ext)) return 'DocumentTextIcon'

  return 'DocumentIcon'
})

const iconColor = computed(() => {
  if (props.item.type === 'directory') return 'text-yellow-500'

  const ext = fileExtension.value
  if (['mp4', 'webm', 'mov', 'avi', 'mkv'].includes(ext)) return 'text-purple-500'
  if (['mp3', 'wav', 'ogg', 'm4a', 'flac', 'aac'].includes(ext)) return 'text-green-500'
  if (['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'ico', 'bmp'].includes(ext)) return 'text-blue-500'
  if (['js', 'ts', 'jsx', 'tsx', 'py', 'go', 'rs', 'rb', 'java', 'c', 'cpp', 'h', 'css', 'scss', 'html', 'vue', 'svelte'].includes(ext)) return 'text-gray-500'
  if (['pdf'].includes(ext)) return 'text-red-500'

  return 'text-gray-400'
})

// Methods
const handleClick = () => {
  if (props.item.type === 'directory') {
    expanded.value = !expanded.value
  }
  emit('select', props.item)
}

const formatSize = (bytes) => {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i]
}
</script>

<script>
import { h } from 'vue'

// Icon components using render functions (no runtime template compilation needed)
const FolderIcon = {
  render() {
    return h('svg', { fill: 'none', viewBox: '0 0 24 24', stroke: 'currentColor' }, [
      h('path', { 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'stroke-width': '2', d: 'M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z' })
    ])
  }
}
const FolderOpenIcon = {
  render() {
    return h('svg', { fill: 'none', viewBox: '0 0 24 24', stroke: 'currentColor' }, [
      h('path', { 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'stroke-width': '2', d: 'M5 19a2 2 0 01-2-2V7a2 2 0 012-2h4l2 2h4a2 2 0 012 2v1M5 19h14a2 2 0 002-2v-5a2 2 0 00-2-2H9a2 2 0 00-2 2v5a2 2 0 01-2 2z' })
    ])
  }
}
const FilmIcon = {
  render() {
    return h('svg', { fill: 'none', viewBox: '0 0 24 24', stroke: 'currentColor' }, [
      h('path', { 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'stroke-width': '2', d: 'M7 4v16M17 4v16M3 8h4m10 0h4M3 12h18M3 16h4m10 0h4M4 20h16a1 1 0 001-1V5a1 1 0 00-1-1H4a1 1 0 00-1 1v14a1 1 0 001 1z' })
    ])
  }
}
const MusicalNoteIcon = {
  render() {
    return h('svg', { fill: 'none', viewBox: '0 0 24 24', stroke: 'currentColor' }, [
      h('path', { 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'stroke-width': '2', d: 'M9 19V6l12-3v13M9 19c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zm12-3c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zM9 10l12-3' })
    ])
  }
}
const PhotoIcon = {
  render() {
    return h('svg', { fill: 'none', viewBox: '0 0 24 24', stroke: 'currentColor' }, [
      h('path', { 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'stroke-width': '2', d: 'M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z' })
    ])
  }
}
const CodeBracketIcon = {
  render() {
    return h('svg', { fill: 'none', viewBox: '0 0 24 24', stroke: 'currentColor' }, [
      h('path', { 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'stroke-width': '2', d: 'M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4' })
    ])
  }
}
const DocumentIcon = {
  render() {
    return h('svg', { fill: 'none', viewBox: '0 0 24 24', stroke: 'currentColor' }, [
      h('path', { 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'stroke-width': '2', d: 'M7 21h10a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v14a2 2 0 002 2z' })
    ])
  }
}
const DocumentTextIcon = {
  render() {
    return h('svg', { fill: 'none', viewBox: '0 0 24 24', stroke: 'currentColor' }, [
      h('path', { 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'stroke-width': '2', d: 'M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z' })
    ])
  }
}

export default {
  name: 'FileTreeNode',
  components: {
    FolderIcon,
    FolderOpenIcon,
    FilmIcon,
    MusicalNoteIcon,
    PhotoIcon,
    CodeBracketIcon,
    DocumentIcon,
    DocumentTextIcon
  }
}
</script>
