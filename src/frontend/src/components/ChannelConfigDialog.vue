<template>
  <Teleport to="body">
    <div v-if="open" class="fixed z-50 inset-0 overflow-y-auto" role="dialog" aria-modal="true" :aria-label="title">
      <div class="flex items-end sm:items-center justify-center min-h-screen px-4 pt-4 pb-20 text-center sm:p-0">
        <!-- Backdrop -->
        <div
          class="fixed inset-0 bg-gray-500 dark:bg-gray-900 bg-opacity-75 dark:bg-opacity-75 transition-opacity"
          @click="$emit('close')"
        ></div>

        <span class="hidden sm:inline-block sm:align-middle sm:h-screen">&#8203;</span>

        <!-- Dialog -->
        <div
          class="inline-block align-bottom sm:align-middle bg-white dark:bg-gray-800 rounded-lg text-left overflow-hidden shadow-xl transform transition-all sm:my-8 w-full sm:max-w-2xl"
          @click.stop
        >
          <!-- Header -->
          <div class="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-700">
            <h3 class="flex items-center gap-2 text-base font-medium text-gray-900 dark:text-white">
              <span v-if="icon" class="text-lg leading-none" aria-hidden="true">{{ icon }}</span>
              {{ title }}
            </h3>
            <button
              type="button"
              class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 focus:outline-none focus:ring-1 focus:ring-action-primary-500 rounded"
              aria-label="Close"
              @click="$emit('close')"
            >
              <svg class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>

          <!-- Body (the channel config panel) -->
          <div class="px-6 py-5 max-h-[70vh] overflow-y-auto">
            <slot />
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup>
import { watch, onUnmounted } from 'vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  title: { type: String, required: true },
  icon: { type: String, default: '' },
})

const emit = defineEmits(['close'])

// Close on Escape while open.
const onKey = (e) => {
  if (e.key === 'Escape') emit('close')
}
watch(() => props.open, (isOpen) => {
  if (isOpen) document.addEventListener('keydown', onKey)
  else document.removeEventListener('keydown', onKey)
})
onUnmounted(() => document.removeEventListener('keydown', onKey))
</script>
