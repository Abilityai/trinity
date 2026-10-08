<template>
  <!-- ent#841: "Chat archived · Undo" — a toast for a COMPLETED verb (contract
       rule 18: toasts confirm completions, never errors). It appears only after
       the server has archived the chat, so it never claims a close that failed.
       Bottom-centre, clear of the composer's send button on the right. -->
  <div
    v-if="message"
    role="status"
    class="fixed bottom-36 left-1/2 -translate-x-1/2 z-50 flex items-center gap-3 pl-4 pr-2 py-2 rounded-lg shadow-lg text-sm border bg-gray-900 border-gray-800 text-white dark:bg-gray-800 dark:border-gray-700"
    data-testid="portal-undo-toast"
  >
    <span>{{ message }}</span>
    <button
      type="button"
      class="px-2 py-1 rounded font-semibold text-action-primary-300 hover:bg-gray-800 dark:hover:bg-gray-700 transition"
      data-testid="portal-undo-toast-undo"
      @click="$emit('undo')"
    >Undo</button>
    <button
      type="button"
      class="p-1 rounded text-gray-400 hover:text-white hover:bg-gray-800 dark:hover:bg-gray-700 transition"
      aria-label="Dismiss"
      @click="$emit('dismiss')"
    >
      <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M6 18L18 6M6 6l12 12" /></svg>
    </button>
  </div>
</template>

<script setup>
/**
 * The Workspace's one Undo toast (ent#841). Dismisses itself after
 * `UNDO_TOAST_MS`; a new message restarts the clock, so closing two chats in a
 * row offers Undo for the second one for the full window.
 */
import { watch, onUnmounted } from 'vue'

const UNDO_TOAST_MS = 8000

const props = defineProps({
  message: { type: String, default: '' },
})
const emit = defineEmits(['undo', 'dismiss'])

let timer = null
const clear = () => { if (timer) { clearTimeout(timer); timer = null } }
watch(() => props.message, (m) => {
  clear()
  if (m) timer = setTimeout(() => emit('dismiss'), UNDO_TOAST_MS)
}, { immediate: true })
onUnmounted(clear)
</script>
