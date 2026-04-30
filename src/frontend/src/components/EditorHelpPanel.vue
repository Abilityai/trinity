<template>
  <div
    class="editor-help-panel h-full flex flex-col bg-white dark:bg-gray-800 rounded-xl shadow-lg overflow-hidden transition-all duration-300"
    :class="{ 'w-0 opacity-0': !visible }"
  >
    <!-- Header -->
    <div class="flex items-center justify-between px-4 py-2 bg-gray-50 dark:bg-gray-900/50 border-b border-gray-200 dark:border-gray-700">
      <div class="flex items-center gap-2">
        <QuestionMarkCircleIcon class="h-4 w-4 text-indigo-500" />
        <h3 class="text-sm font-medium text-gray-700 dark:text-gray-300">Quick Help</h3>
      </div>
      <button
        @click="$emit('close')"
        class="p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 rounded transition-colors"
        title="Close help panel"
      >
        <XMarkIcon class="h-4 w-4" />
      </button>
    </div>

    <!-- Content -->
    <div class="flex-1 overflow-y-auto p-4">
      <div v-if="helpContent">
        <!-- Title -->
        <h4 class="text-lg font-semibold text-gray-900 dark:text-white mb-2">
          {{ helpContent.title }}
        </h4>

        <!-- Description -->
        <p class="text-sm text-gray-600 dark:text-gray-300 mb-4">
          {{ helpContent.description }}
        </p>

        <!-- Meta info -->
        <div class="space-y-3 mb-4">
          <!-- Type -->
          <div v-if="helpContent.type" class="flex items-center gap-2">
            <span class="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase w-16">Type</span>
            <code class="text-xs px-1.5 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-gray-700 dark:text-gray-300">
              {{ helpContent.type }}
            </code>
          </div>

          <!-- Required -->
          <div v-if="helpContent.required !== undefined" class="flex items-center gap-2">
            <span class="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase w-16">Required</span>
            <span
              :class="helpContent.required
                ? 'text-status-danger-600 dark:text-status-danger-400'
                : 'text-gray-500 dark:text-gray-400'"
              class="text-xs font-medium"
            >
              {{ helpContent.required ? 'Yes' : 'No' }}
            </span>
          </div>

          <!-- Default -->
          <div v-if="helpContent.default" class="flex items-center gap-2">
            <span class="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase w-16">Default</span>
            <code class="text-xs px-1.5 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-gray-700 dark:text-gray-300">
              {{ helpContent.default }}
            </code>
          </div>

          <!-- Options -->
          <div v-if="helpContent.options" class="flex items-start gap-2">
            <span class="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase w-16 mt-0.5">Options</span>
            <div class="flex flex-wrap gap-1">
              <code
                v-for="opt in helpContent.options"
                :key="opt"
                class="text-xs px-1.5 py-0.5 bg-indigo-50 dark:bg-indigo-900/30 rounded text-indigo-700 dark:text-indigo-300"
              >
                {{ opt }}
              </code>
            </div>
          </div>
        </div>

        <!-- Example -->
        <div v-if="helpContent.example" class="mb-4">
          <span class="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase block mb-2">Example</span>
          <pre class="text-xs bg-gray-900 text-gray-100 p-3 rounded-lg overflow-x-auto"><code>{{ helpContent.example }}</code></pre>
        </div>

        <!-- Docs link -->
        <router-link
          v-if="helpContent.docs_link"
          :to="helpContent.docs_link"
          class="inline-flex items-center gap-1 text-sm text-indigo-600 dark:text-indigo-400 hover:underline"
        >
          <BookOpenIcon class="h-4 w-4" />
          Learn more in documentation
          <ArrowTopRightOnSquareIcon class="h-3 w-3" />
        </router-link>
      </div>

      <!-- No context help -->
      <div v-else class="text-center py-8">
        <QuestionMarkCircleIcon class="h-12 w-12 text-gray-300 dark:text-gray-600 mx-auto mb-3" />
        <p class="text-sm text-gray-500 dark:text-gray-400">
          Click on any field in the YAML editor to see contextual help.
        </p>
      </div>
    </div>

    <!-- Footer with keyboard hint -->
    <div class="px-4 py-2 bg-gray-50 dark:bg-gray-900/50 border-t border-gray-200 dark:border-gray-700">
      <p class="text-xs text-gray-400 dark:text-gray-500 text-center">
        <kbd class="px-1 py-0.5 bg-gray-200 dark:bg-gray-700 rounded text-xs">?</kbd> to toggle help
      </p>
    </div>
  </div>
</template>

<script setup>
import {
  QuestionMarkCircleIcon,
  XMarkIcon,
  BookOpenIcon,
  ArrowTopRightOnSquareIcon,
} from '@heroicons/vue/24/outline'

defineProps({
  visible: {
    type: Boolean,
    default: true
  },
  helpContent: {
    type: Object,
    default: null
  }
})

defineEmits(['close'])
</script>

<style scoped>
.editor-help-panel {
  width: 100%;
}

/* Desktop: constrain width */
@media (min-width: 1280px) {
  .editor-help-panel {
    min-width: 280px;
    max-width: 320px;
  }
}

.editor-help-panel.w-0 {
  min-width: 0;
  max-width: 0;
  width: 0;
  padding: 0;
}
</style>
