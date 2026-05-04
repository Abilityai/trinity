<template>
  <div class="tags-editor">
    <!-- Tags Display -->
    <div class="flex flex-wrap gap-1.5 items-center">
      <span
        v-for="tag in tags"
        :key="tag"
        class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-violet-100 text-violet-700 dark:bg-violet-900/30 dark:text-violet-300"
      >
        <span>#{{ tag }}</span>
        <button
          v-if="editable"
          @click="removeTag(tag)"
          class="hover:text-violet-900 dark:hover:text-violet-100 ml-0.5"
          :title="'Remove ' + tag"
        >
          <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </span>

      <!-- Add Tag Button / Input -->
      <div v-if="editable" class="relative">
        <button
          v-if="!showInput"
          @click="showInput = true"
          class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-600 hover:bg-gray-200 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
        >
          <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
          </svg>
          <span>Add</span>
        </button>

        <div v-else class="relative">
          <input
            ref="inputRef"
            v-model="newTag"
            @keydown.enter.prevent="addTag"
            @keydown.escape="cancelAdd"
            @blur="handleBlur"
            type="text"
            placeholder="tag-name"
            class="w-24 px-2 py-0.5 text-xs rounded-full border border-violet-300 focus:border-violet-500 focus:ring-1 focus:ring-violet-500 dark:bg-gray-800 dark:border-violet-700 dark:text-white"
            maxlength="50"
          />
          <!-- Autocomplete dropdown -->
          <div
            v-if="filteredSuggestions.length > 0"
            class="absolute z-10 mt-1 w-40 bg-white dark:bg-gray-800 rounded-md shadow-lg border border-gray-200 dark:border-gray-700 max-h-32 overflow-y-auto"
          >
            <button
              v-for="suggestion in filteredSuggestions"
              :key="suggestion"
              @mousedown.prevent="selectSuggestion(suggestion)"
              class="w-full px-3 py-1.5 text-left text-xs hover:bg-gray-100 dark:hover:bg-gray-700"
            >
              #{{ suggestion }}
            </button>
          </div>
        </div>
      </div>

      <!-- Empty state -->
      <span
        v-if="tags.length === 0 && !editable"
        class="text-xs text-gray-400 dark:text-gray-500 italic"
      >
        No tags
      </span>
    </div>

    <!-- Error message -->
    <p v-if="error" class="mt-1 text-xs text-status-danger-500">{{ error }}</p>
  </div>
</template>

<script setup>
import { ref, computed, watch, nextTick } from 'vue'

const props = defineProps({
  modelValue: {
    type: Array,
    default: () => []
  },
  editable: {
    type: Boolean,
    default: true
  },
  allTags: {
    type: Array,
    default: () => []
  }
})

const emit = defineEmits(['update:modelValue', 'add', 'remove'])

const tags = computed(() => props.modelValue || [])
const showInput = ref(false)
const newTag = ref('')
const error = ref('')
const inputRef = ref(null)

// Suggestions based on existing tags
const filteredSuggestions = computed(() => {
  if (!newTag.value || !props.allTags.length) return []
  const search = newTag.value.toLowerCase()
  return props.allTags
    .filter(t => t.toLowerCase().includes(search) && !tags.value.includes(t))
    .slice(0, 5)
})

watch(showInput, (val) => {
  if (val) {
    nextTick(() => {
      inputRef.value?.focus()
    })
  }
})

function addTag() {
  const normalized = newTag.value.toLowerCase().trim()
  error.value = ''

  if (!normalized) {
    cancelAdd()
    return
  }

  // Validate format
  if (!/^[a-z0-9-]+$/.test(normalized)) {
    error.value = 'Tags can only contain letters, numbers, and hyphens'
    return
  }

  if (normalized.length > 50) {
    error.value = 'Tag too long (max 50 characters)'
    return
  }

  if (tags.value.includes(normalized)) {
    error.value = 'Tag already exists'
    return
  }

  const newTags = [...tags.value, normalized].sort()
  emit('update:modelValue', newTags)
  emit('add', normalized)
  newTag.value = ''
  showInput.value = false
}

function removeTag(tag) {
  const newTags = tags.value.filter(t => t !== tag)
  emit('update:modelValue', newTags)
  emit('remove', tag)
}

function selectSuggestion(tag) {
  newTag.value = tag
  addTag()
}

function cancelAdd() {
  showInput.value = false
  newTag.value = ''
  error.value = ''
}

function handleBlur() {
  // Delay to allow click on suggestion
  setTimeout(() => {
    if (showInput.value && !newTag.value) {
      cancelAdd()
    }
  }, 150)
}
</script>
