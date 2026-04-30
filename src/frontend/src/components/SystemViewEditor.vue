<template>
  <div
    v-if="isOpen"
    class="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
    @click.self="close"
  >
    <div class="bg-white dark:bg-gray-800 rounded-lg shadow-xl w-full max-w-md mx-4">
      <!-- Header -->
      <div class="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-700">
        <h3 class="text-lg font-semibold text-gray-900 dark:text-gray-100">
          {{ isEditing ? 'Edit System View' : 'Create System View' }}
        </h3>
        <button
          @click="close"
          class="p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 rounded"
        >
          <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>

      <!-- Form -->
      <form @submit.prevent="handleSubmit" class="p-6 space-y-4">
        <!-- Name -->
        <div>
          <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
            Name *
          </label>
          <input
            v-model="form.name"
            type="text"
            required
            maxlength="100"
            class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            placeholder="e.g., Due Diligence Team"
          />
        </div>

        <!-- Description -->
        <div>
          <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
            Description
          </label>
          <input
            v-model="form.description"
            type="text"
            maxlength="200"
            class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            placeholder="Optional description"
          />
        </div>

        <!-- Icon & Color Row -->
        <div class="flex space-x-4">
          <!-- Icon -->
          <div class="flex-1">
            <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Icon
            </label>
            <div class="flex flex-wrap gap-2">
              <button
                v-for="emoji in commonEmojis"
                :key="emoji"
                type="button"
                @click="form.icon = emoji"
                :class="[
                  'w-8 h-8 flex items-center justify-center rounded border text-lg transition-colors',
                  form.icon === emoji
                    ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/30'
                    : 'border-gray-200 dark:border-gray-600 hover:border-gray-300 dark:hover:border-gray-500'
                ]"
              >
                {{ emoji }}
              </button>
            </div>
          </div>

          <!-- Color -->
          <div class="w-32">
            <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Color
            </label>
            <div class="flex flex-wrap gap-2">
              <button
                v-for="color in colorOptions"
                :key="color"
                type="button"
                @click="form.color = color"
                :class="[
                  'w-6 h-6 rounded-full border-2 transition-transform',
                  form.color === color ? 'scale-110 border-gray-800 dark:border-white' : 'border-transparent hover:scale-105'
                ]"
                :style="{ backgroundColor: color }"
              ></button>
            </div>
          </div>
        </div>

        <!-- Filter Tags -->
        <div>
          <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
            Filter Tags *
          </label>
          <div class="space-y-2">
            <!-- Tag Input -->
            <div class="flex space-x-2">
              <input
                v-model="tagInput"
                type="text"
                @keydown.enter.prevent="addTag"
                class="flex-1 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                placeholder="Type a tag and press Enter"
              />
              <button
                type="button"
                @click="addTag"
                class="px-3 py-2 bg-gray-100 dark:bg-gray-700 border border-gray-300 dark:border-gray-600 rounded-md text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600 transition-colors"
              >
                Add
              </button>
            </div>

            <!-- Available Tags -->
            <div v-if="availableTags.length > 0" class="text-xs text-gray-500 dark:text-gray-400">
              Available:
              <button
                v-for="tag in availableTags"
                :key="tag.tag"
                type="button"
                @click="addExistingTag(tag.tag)"
                class="inline-flex items-center px-1.5 py-0.5 mx-0.5 rounded bg-gray-100 dark:bg-gray-700 hover:bg-blue-100 dark:hover:bg-blue-900/30 transition-colors"
              >
                #{{ tag.tag }} <span class="ml-1 text-gray-400">({{ tag.count }})</span>
              </button>
            </div>

            <!-- Selected Tags -->
            <div v-if="form.filter_tags.length > 0" class="flex flex-wrap gap-2">
              <span
                v-for="tag in form.filter_tags"
                :key="tag"
                class="inline-flex items-center px-2 py-1 rounded-full text-sm bg-blue-100 dark:bg-blue-900/40 text-blue-800 dark:text-blue-200"
              >
                #{{ tag }}
                <button
                  type="button"
                  @click="removeTag(tag)"
                  class="ml-1 text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-200"
                >
                  <svg class="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 20 20">
                    <path fill-rule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clip-rule="evenodd" />
                  </svg>
                </button>
              </span>
            </div>
            <p v-else class="text-xs text-gray-400 dark:text-gray-500">
              Add at least one tag to filter agents
            </p>
          </div>
        </div>

        <!-- Shared Toggle -->
        <div class="flex items-center justify-between py-2">
          <div>
            <label class="text-sm font-medium text-gray-700 dark:text-gray-300">
              Share with all users
            </label>
            <p class="text-xs text-gray-500 dark:text-gray-400">
              Make this view visible to everyone
            </p>
          </div>
          <button
            type="button"
            @click="form.is_shared = !form.is_shared"
            :class="[
              'relative inline-flex h-6 w-11 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2',
              form.is_shared ? 'bg-blue-600' : 'bg-gray-200 dark:bg-gray-600'
            ]"
          >
            <span
              :class="[
                'pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out',
                form.is_shared ? 'translate-x-5' : 'translate-x-0'
              ]"
            ></span>
          </button>
        </div>

        <!-- Error -->
        <div v-if="error" class="text-sm text-status-danger-600 dark:text-status-danger-400 bg-status-danger-50 dark:bg-status-danger-900/20 p-3 rounded">
          {{ error }}
        </div>

        <!-- Actions -->
        <div class="flex justify-between pt-4 border-t border-gray-200 dark:border-gray-700">
          <button
            v-if="isEditing"
            type="button"
            @click="handleDelete"
            class="px-4 py-2 text-sm text-status-danger-600 dark:text-status-danger-400 hover:bg-status-danger-50 dark:hover:bg-status-danger-900/20 rounded transition-colors"
          >
            Delete
          </button>
          <div v-else></div>

          <div class="flex space-x-3">
            <button
              type="button"
              @click="close"
              class="px-4 py-2 text-sm text-gray-700 dark:text-gray-300 bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 rounded transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              :disabled="!isValid || isSubmitting"
              class="px-4 py-2 text-sm text-white bg-blue-600 hover:bg-blue-700 disabled:bg-blue-400 disabled:cursor-not-allowed rounded transition-colors"
            >
              {{ isSubmitting ? 'Saving...' : (isEditing ? 'Update' : 'Create') }}
            </button>
          </div>
        </div>
      </form>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch, onMounted } from 'vue'
import { useSystemViewsStore } from '@/stores/systemViews'
import axios from 'axios'

const props = defineProps({
  isOpen: Boolean,
  editingView: Object // Null for create, view object for edit
})

const emit = defineEmits(['close', 'saved'])

const systemViewsStore = useSystemViewsStore()

const form = reactive({
  name: '',
  description: '',
  icon: '',
  color: '#8B5CF6',
  filter_tags: [],
  is_shared: false
})

const tagInput = ref('')
const availableTags = ref([])
const isSubmitting = ref(false)
const error = ref(null)

const commonEmojis = ['🔍', '📊', '🚀', '💼', '🛠️', '📝', '🎯', '⚙️', '🔒', '📈', '🤖', '✨']
const colorOptions = ['#8B5CF6', '#3B82F6', '#06B6D4', '#10B981', '#F59E0B', '#EF4444', '#EC4899', '#6366F1']

const isEditing = computed(() => !!props.editingView)

const isValid = computed(() => {
  return form.name.trim().length > 0 && form.filter_tags.length > 0
})

// Watch for changes to editingView prop
watch(() => props.editingView, (view) => {
  if (view) {
    form.name = view.name || ''
    form.description = view.description || ''
    form.icon = view.icon || ''
    form.color = view.color || '#8B5CF6'
    form.filter_tags = [...(view.filter_tags || [])]
    form.is_shared = view.is_shared || false
  } else {
    resetForm()
  }
}, { immediate: true })

// Watch for modal open to reset form
watch(() => props.isOpen, async (open) => {
  if (open) {
    error.value = null
    await fetchAvailableTags()
    if (!props.editingView) {
      resetForm()
    }
  }
})

async function fetchAvailableTags() {
  try {
    const response = await axios.get('/api/tags')
    availableTags.value = response.data.tags || []
  } catch (err) {
    console.error('Failed to fetch tags:', err)
  }
}

function resetForm() {
  form.name = ''
  form.description = ''
  form.icon = ''
  form.color = '#8B5CF6'
  form.filter_tags = []
  form.is_shared = false
  tagInput.value = ''
}

function addTag() {
  const tag = tagInput.value.trim().toLowerCase().replace(/[^a-z0-9-]/g, '-')
  if (tag && !form.filter_tags.includes(tag)) {
    form.filter_tags.push(tag)
  }
  tagInput.value = ''
}

function addExistingTag(tag) {
  if (!form.filter_tags.includes(tag)) {
    form.filter_tags.push(tag)
  }
}

function removeTag(tag) {
  form.filter_tags = form.filter_tags.filter(t => t !== tag)
}

async function handleSubmit() {
  if (!isValid.value) return

  isSubmitting.value = true
  error.value = null

  try {
    const payload = {
      name: form.name.trim(),
      description: form.description.trim() || null,
      icon: form.icon || null,
      color: form.color || null,
      filter_tags: form.filter_tags,
      is_shared: form.is_shared
    }

    if (isEditing.value) {
      await systemViewsStore.updateView(props.editingView.id, payload)
    } else {
      await systemViewsStore.createView(payload)
    }

    emit('saved')
    close()
  } catch (err) {
    error.value = err.response?.data?.detail || 'Failed to save system view'
  } finally {
    isSubmitting.value = false
  }
}

async function handleDelete() {
  if (!props.editingView) return

  if (!confirm(`Delete "${props.editingView.name}"? This action cannot be undone.`)) {
    return
  }

  isSubmitting.value = true
  error.value = null

  try {
    await systemViewsStore.deleteView(props.editingView.id)
    emit('saved')
    close()
  } catch (err) {
    error.value = err.response?.data?.detail || 'Failed to delete system view'
  } finally {
    isSubmitting.value = false
  }
}

function close() {
  emit('close')
}
</script>
