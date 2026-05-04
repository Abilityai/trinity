<template>
  <div class="template-selector">
    <!-- Header -->
    <div class="flex items-center justify-between mb-4">
      <h3 class="text-lg font-medium text-gray-900 dark:text-white">Process Templates</h3>
      <div class="flex items-center gap-2">
        <!-- Category filter -->
        <select
          v-model="selectedCategory"
          class="text-sm border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-1.5 bg-white dark:bg-gray-700 text-gray-700 dark:text-gray-200"
        >
          <option value="">All Categories</option>
          <option v-for="cat in categories" :key="cat.id" :value="cat.id">
            {{ cat.name }}
          </option>
        </select>
      </div>
    </div>

    <!-- Loading state -->
    <div v-if="loading" class="flex items-center justify-center py-12">
      <svg class="animate-spin h-8 w-8 text-teal-500" fill="none" viewBox="0 0 24 24">
        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
      </svg>
    </div>

    <!-- Empty state -->
    <div
      v-else-if="filteredTemplates.length === 0"
      class="text-center py-12 text-gray-500 dark:text-gray-400"
    >
      <DocumentPlusIcon class="w-12 h-12 mx-auto mb-4 text-gray-300 dark:text-gray-600" />
      <p class="font-medium">No templates found</p>
      <p class="text-sm mt-1">Try a different category or create your own</p>
    </div>

    <!-- Template grid -->
    <div v-else class="grid grid-cols-1 md:grid-cols-2 gap-4">
      <!-- Blank process option -->
      <div
        @click="$emit('select', null)"
        :class="[
          'relative flex items-center p-4 border-2 rounded-lg cursor-pointer transition-all',
          selectedId === null
            ? 'border-teal-500 bg-teal-50 dark:bg-teal-900/20 ring-2 ring-teal-500'
            : 'border-gray-200 dark:border-gray-700 hover:border-teal-300 dark:hover:border-teal-700'
        ]"
      >
        <div class="flex-shrink-0 w-10 h-10 rounded-lg bg-gray-100 dark:bg-gray-700 flex items-center justify-center">
          <PlusIcon class="w-6 h-6 text-gray-500 dark:text-gray-400" />
        </div>
        <div class="ml-4 flex-1">
          <p class="font-medium text-gray-900 dark:text-white">Blank Process</p>
          <p class="text-xs text-gray-500 dark:text-gray-400">Start from scratch</p>
        </div>
        <CheckCircleIcon
          v-if="selectedId === null"
          class="w-6 h-6 text-teal-500 flex-shrink-0"
        />
      </div>

      <!-- Template cards -->
      <div
        v-for="template in filteredTemplates"
        :key="template.id"
        @click="$emit('select', template.id)"
        :class="[
          'relative flex items-start p-4 border-2 rounded-lg cursor-pointer transition-all',
          selectedId === template.id
            ? 'border-teal-500 bg-teal-50 dark:bg-teal-900/20 ring-2 ring-teal-500'
            : 'border-gray-200 dark:border-gray-700 hover:border-teal-300 dark:hover:border-teal-700'
        ]"
      >
        <!-- Template icon (teal themed) -->
        <div class="flex-shrink-0 w-10 h-10 rounded-lg bg-teal-100 dark:bg-teal-900/30 flex items-center justify-center">
          <ArrowsRightLeftIcon class="w-6 h-6 text-teal-600 dark:text-teal-400" />
        </div>

        <div class="ml-4 flex-1 min-w-0">
          <div class="flex items-center gap-2">
            <p class="font-medium text-gray-900 dark:text-white truncate">{{ template.display_name }}</p>
            <span
              v-if="template.source === 'user'"
              class="px-1.5 py-0.5 text-xs font-medium bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400 rounded"
            >
              User
            </span>
          </div>
          <p class="text-xs text-gray-500 dark:text-gray-400 mt-1 line-clamp-2">
            {{ template.description }}
          </p>
          <div class="flex items-center gap-2 mt-2">
            <span
              class="px-2 py-0.5 text-xs font-medium rounded capitalize"
              :class="getCategoryBadgeClass(template.category)"
            >
              {{ template.category }}
            </span>
            <span class="text-xs text-gray-400 dark:text-gray-500">
              {{ template.complexity }}
            </span>
          </div>
        </div>

        <CheckCircleIcon
          v-if="selectedId === template.id"
          class="w-6 h-6 text-teal-500 flex-shrink-0 ml-2"
        />
      </div>
    </div>

    <!-- Preview modal -->
    <div
      v-if="previewTemplate"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      @click.self="previewTemplate = null"
    >
      <div class="bg-white dark:bg-gray-800 rounded-xl shadow-xl max-w-2xl w-full mx-4 max-h-[80vh] overflow-hidden">
        <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
          <h3 class="text-lg font-medium text-gray-900 dark:text-white">
            {{ previewTemplate.display_name }}
          </h3>
          <button
            @click="previewTemplate = null"
            class="text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
          >
            <XMarkIcon class="w-5 h-5" />
          </button>
        </div>
        <div class="p-6 overflow-y-auto max-h-[60vh]">
          <p class="text-gray-600 dark:text-gray-400 mb-4">{{ previewTemplate.description }}</p>

          <div v-if="previewTemplate.use_cases?.length" class="mb-4">
            <h4 class="text-sm font-medium text-gray-900 dark:text-white mb-2">Use Cases</h4>
            <ul class="list-disc list-inside text-sm text-gray-600 dark:text-gray-400">
              <li v-for="(useCase, i) in previewTemplate.use_cases" :key="i">{{ useCase }}</li>
            </ul>
          </div>

          <div v-if="previewTemplate.step_types_used?.length" class="mb-4">
            <h4 class="text-sm font-medium text-gray-900 dark:text-white mb-2">Step Types</h4>
            <div class="flex flex-wrap gap-1">
              <span
                v-for="stepType in previewTemplate.step_types_used"
                :key="stepType"
                class="px-2 py-0.5 text-xs font-medium bg-teal-100 dark:bg-teal-900/30 text-teal-700 dark:text-teal-300 rounded"
              >
                {{ stepType }}
              </span>
            </div>
          </div>

          <div>
            <h4 class="text-sm font-medium text-gray-900 dark:text-white mb-2">Definition Preview</h4>
            <pre class="bg-gray-100 dark:bg-gray-900 rounded-lg p-4 text-xs overflow-x-auto max-h-60">{{ previewTemplate.definition_yaml }}</pre>
          </div>
        </div>
        <div class="px-6 py-4 border-t border-gray-200 dark:border-gray-700 flex justify-end gap-3">
          <button
            @click="previewTemplate = null"
            class="px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-700 border border-gray-300 dark:border-gray-600 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-600"
          >
            Cancel
          </button>
          <button
            @click="$emit('select', previewTemplate.id); previewTemplate = null"
            class="px-4 py-2 text-sm font-medium text-white bg-teal-600 hover:bg-teal-700 rounded-lg"
          >
            Use Template
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import axios from 'axios'
import {
  PlusIcon,
  CheckCircleIcon,
  XMarkIcon,
  DocumentPlusIcon,
  ArrowsRightLeftIcon,
} from '@heroicons/vue/24/outline'

const props = defineProps({
  selectedId: {
    type: String,
    default: null,
  },
})

const emit = defineEmits(['select'])

// State
const templates = ref([])
const categories = ref([])
const selectedCategory = ref('')
const loading = ref(false)
const previewTemplate = ref(null)

// Computed
const filteredTemplates = computed(() => {
  if (!selectedCategory.value) {
    return templates.value
  }
  return templates.value.filter(t => t.category === selectedCategory.value)
})

// Lifecycle
onMounted(async () => {
  await Promise.all([
    fetchTemplates(),
    fetchCategories(),
  ])
})

// Watch category filter
watch(selectedCategory, () => {
  // Could refetch from server for optimization
})

// Methods
async function fetchTemplates() {
  loading.value = true
  try {
    const token = localStorage.getItem('token')
    const response = await axios.get('/api/process-templates', {
      headers: { Authorization: `Bearer ${token}` },
    })
    templates.value = response.data.templates || []
  } catch (err) {
    console.error('Failed to fetch templates:', err)
  } finally {
    loading.value = false
  }
}

async function fetchCategories() {
  try {
    const token = localStorage.getItem('token')
    const response = await axios.get('/api/process-templates/categories', {
      headers: { Authorization: `Bearer ${token}` },
    })
    categories.value = response.data.categories || []
  } catch (err) {
    console.error('Failed to fetch categories:', err)
  }
}

function getCategoryBadgeClass(category) {
  const badges = {
    general: 'bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300',
    business: 'bg-status-info-100 dark:bg-status-info-900/30 text-status-info-700 dark:text-status-info-300',
    devops: 'bg-accent-purple-100 dark:bg-accent-purple-900/30 text-accent-purple-700 dark:text-accent-purple-300',
    analytics: 'bg-cyan-100 dark:bg-cyan-900/30 text-cyan-700 dark:text-cyan-300',
    support: 'bg-status-urgent-100 dark:bg-status-urgent-900/30 text-status-urgent-700 dark:text-status-urgent-300',
    content: 'bg-pink-100 dark:bg-pink-900/30 text-pink-700 dark:text-pink-300',
  }
  return badges[category] || badges.general
}

async function showPreview(template) {
  try {
    const token = localStorage.getItem('token')
    const response = await axios.get(`/api/process-templates/${template.id}`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    previewTemplate.value = response.data
  } catch (err) {
    console.error('Failed to fetch template preview:', err)
  }
}
</script>

<style scoped>
.line-clamp-2 {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
