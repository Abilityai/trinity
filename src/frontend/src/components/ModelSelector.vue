<template>
  <div class="relative" ref="containerRef">
    <label v-if="label" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">{{ label }}</label>
    <div class="relative">
      <input
        ref="inputRef"
        type="text"
        :value="modelValue"
        @input="onInput"
        @focus="showDropdown = true"
        @keydown.escape="showDropdown = false; isTyping = false"
        @keydown.down.prevent="highlightNext"
        @keydown.up.prevent="highlightPrev"
        @keydown.enter.prevent="selectHighlighted"
        :placeholder="resolvedPlaceholder"
        :class="inputClass"
      />
      <button
        type="button"
        @click="toggleDropdown"
        class="absolute inset-y-0 right-0 flex items-center pr-2 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
      >
        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
        </svg>
      </button>
    </div>
    <!-- Dropdown -->
    <div
      v-if="showDropdown && filteredModels.length > 0"
      class="absolute z-50 mt-1 w-full bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-600 rounded-md shadow-lg max-h-48 overflow-y-auto"
    >
      <button
        v-for="(model, index) in filteredModels"
        :key="model.value"
        type="button"
        @click="selectModel(model.value)"
        @mouseenter="highlightedIndex = index"
        :class="[
          'w-full text-left px-3 py-2 text-sm transition-colors',
          highlightedIndex === index
            ? 'bg-action-primary-50 dark:bg-action-primary-900/30 text-action-primary-700 dark:text-action-primary-300'
            : 'text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700'
        ]"
      >
        <div class="flex items-center justify-between">
          <span>{{ model.label }}</span>
          <span v-if="model.value === modelValue" class="text-action-primary-500">
            <svg class="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
              <path fill-rule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clip-rule="evenodd" />
            </svg>
          </span>
        </div>
        <p v-if="model.note" class="text-xs text-gray-400 dark:text-gray-500 mt-0.5">{{ model.note }}</p>
      </button>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'

const props = defineProps({
  modelValue: {
    type: String,
    default: ''
  },
  label: {
    type: String,
    default: null
  },
  placeholder: {
    type: String,
    default: null
  },
  compact: {
    type: Boolean,
    default: false
  },
  platformDefault: {
    type: String,
    default: null
  }
})

const emit = defineEmits(['update:modelValue'])

// Canonical model list — synced from https://platform.claude.com/docs/en/about-claude/models/overview
// Last updated: 2026-07-08 (#1521)
// Aliases are undated and resolve to the latest snapshot; do NOT append date suffixes
// to a current-gen alias. A removed preset is only hidden from the picker, not blocked
// (free-text passthrough still accepts any string). Append [1m] to a value for the 1M
// extended-context beta (e.g. 'claude-sonnet-4-6[1m]').
const PRESET_MODELS = [
  // Claude 5 family (latest generation, native 1M context)
  { value: 'claude-fable-5', label: 'Claude Fable 5', note: 'Most capable — longest tasks (latest)' },
  { value: 'claude-sonnet-5', label: 'Claude Sonnet 5', note: 'Fast + smart, 1M context (latest)' },
  // Current generation
  { value: 'claude-opus-4-8', label: 'Claude Opus 4.8', note: 'Most capable Opus' },
  { value: 'claude-opus-4-7', label: 'Claude Opus 4.7', note: 'Current' },
  { value: 'claude-opus-4-6', label: 'Claude Opus 4.6', note: 'Current' },
  { value: 'claude-sonnet-4-6', label: 'Claude Sonnet 4.6', note: 'Fast + smart' },
  { value: 'claude-haiku-4-5-20251001', label: 'Claude Haiku 4.5', note: 'Fastest, cheapest' },
  // Legacy (still active)
  { value: 'claude-opus-4-5-20251101', label: 'Claude Opus 4.5', note: 'Legacy' },
  { value: 'claude-sonnet-4-5-20250929', label: 'Claude Sonnet 4.5', note: 'Legacy' },
]

const showDropdown = ref(false)
const highlightedIndex = ref(-1)
const containerRef = ref(null)
const inputRef = ref(null)
const isTyping = ref(false)

const resolvedPlaceholder = computed(() => {
  if (props.placeholder !== null) return props.placeholder
  if (props.platformDefault) return `platform default (${props.platformDefault})`
  return 'Select or type a model...'
})

const inputClass = computed(() => {
  const base = 'w-full border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white rounded-md focus:outline-none focus:ring-2 focus:ring-action-primary-500 pr-8'
  return props.compact
    ? `${base} px-2 py-1.5 text-xs`
    : `${base} px-3 py-2 text-sm`
})

const filteredModels = computed(() => {
  if (!isTyping.value || !props.modelValue) return PRESET_MODELS
  const query = props.modelValue.toLowerCase()
  const filtered = PRESET_MODELS.filter(m =>
    m.value.toLowerCase().includes(query) || m.label.toLowerCase().includes(query)
  )
  return filtered.length > 0 ? filtered : PRESET_MODELS
})

function onInput(event) {
  isTyping.value = true
  emit('update:modelValue', event.target.value)
  showDropdown.value = true
  highlightedIndex.value = -1
}

function selectModel(value) {
  isTyping.value = false
  emit('update:modelValue', value)
  showDropdown.value = false
  highlightedIndex.value = -1
}

function toggleDropdown() {
  showDropdown.value = !showDropdown.value
  if (showDropdown.value) {
    highlightedIndex.value = -1
  }
}

function highlightNext() {
  if (!showDropdown.value) {
    showDropdown.value = true
    return
  }
  highlightedIndex.value = Math.min(highlightedIndex.value + 1, filteredModels.value.length - 1)
}

function highlightPrev() {
  highlightedIndex.value = Math.max(highlightedIndex.value - 1, 0)
}

function selectHighlighted() {
  if (highlightedIndex.value >= 0 && highlightedIndex.value < filteredModels.value.length) {
    selectModel(filteredModels.value[highlightedIndex.value].value)
  } else {
    showDropdown.value = false
  }
}

function handleClickOutside(event) {
  if (containerRef.value && !containerRef.value.contains(event.target)) {
    showDropdown.value = false
    isTyping.value = false
  }
}

onMounted(() => {
  document.addEventListener('click', handleClickOutside)
})

onBeforeUnmount(() => {
  document.removeEventListener('click', handleClickOutside)
})
</script>
