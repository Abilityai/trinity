<template>
  <div ref="root" class="relative shrink-0">
    <button
      type="button"
      class="h-11 w-6 flex items-center justify-center rounded-lg text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:focus-visible:ring-action-primary-400/40 disabled:opacity-40"
      :disabled="disabled"
      :aria-expanded="open"
      aria-haspopup="menu"
      aria-label="Choose the call mode"
      data-testid="portal-call-mode"
      @click="open = !open"
    >
      <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" /></svg>
    </button>
    <div
      v-if="open"
      role="menu"
      class="absolute bottom-full left-0 mb-1 z-40 w-72 rounded-lg border border-gray-200 dark:border-gray-750 bg-white dark:bg-gray-800 shadow-lg py-1"
      data-testid="portal-call-mode-menu"
    >
      <button
        v-for="item in items"
        :key="item.mode"
        type="button"
        role="menuitemradio"
        :aria-checked="item.mode === mode"
        :disabled="!item.enabled"
        class="w-full text-left px-3 py-2 hover:bg-gray-50 dark:hover:bg-gray-750 disabled:cursor-not-allowed"
        :data-testid="`portal-call-mode-${item.mode}`"
        @click="choose(item)"
      >
        <span class="block text-sm font-medium" :class="item.enabled ? 'text-gray-900 dark:text-gray-100' : 'text-gray-500 dark:text-gray-400'">{{ item.label }}<span v-if="item.mode === mode" class="ml-1 text-xs font-normal text-gray-500 dark:text-gray-400">· selected</span></span>
        <span class="block text-xs text-gray-600 dark:text-gray-300">{{ item.enabled ? item.hint : item.reason }}</span>
      </button>
      <p class="px-3 pt-1.5 pb-1 text-xs text-gray-500 dark:text-gray-400 border-t border-gray-200 dark:border-gray-750">Audio and transcripts go to the speech provider.</p>
    </div>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'

defineProps({
  items: { type: Array, required: true },
  mode: { type: String, required: true },
  disabled: { type: Boolean, default: false },
})
const emit = defineEmits(['choose'])

const open = ref(false)
const root = ref(null)

function choose(item) {
  if (!item.enabled) return
  open.value = false
  emit('choose', item.mode)
}
function onDocument(event) {
  if (open.value && root.value && !root.value.contains(event.target)) open.value = false
}
function onKey(event) {
  if (open.value && event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    open.value = false
  }
}
onMounted(() => {
  document.addEventListener('mousedown', onDocument)
  document.addEventListener('keydown', onKey, true)
})
onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onDocument)
  document.removeEventListener('keydown', onKey, true)
})
</script>
