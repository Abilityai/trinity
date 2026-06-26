<template>
  <Teleport to="body">
    <div class="fixed z-50 inset-0 overflow-y-auto" role="dialog" aria-modal="true" :aria-label="title">
      <div class="flex items-end sm:items-center justify-center min-h-screen px-4 pt-4 pb-20 text-center sm:p-0">
        <!-- Backdrop -->
        <div
          class="fixed inset-0 bg-gray-500 dark:bg-gray-900 bg-opacity-75 dark:bg-opacity-75 transition-opacity"
          @click="$emit('close')"
        ></div>

        <span class="hidden sm:inline-block sm:align-middle sm:h-screen">&#8203;</span>

        <!-- Dialog -->
        <div
          ref="panelEl"
          tabindex="-1"
          class="inline-block align-bottom sm:align-middle bg-white dark:bg-gray-800 rounded-lg text-left overflow-hidden shadow-xl transform transition-all sm:my-8 w-full sm:max-w-2xl focus:outline-none"
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
import { onMounted, onUnmounted, nextTick, ref } from 'vue'

defineProps({
  title: { type: String, required: true },
  icon: { type: String, default: '' },
})

const emit = defineEmits(['close'])

const panelEl = ref(null)
// Element to restore focus to on close (the Configure/Manage button), and the
// body overflow value to restore after the scroll lock.
let prevActive = null
let prevOverflow = ''

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'

const focusables = () => {
  const root = panelEl.value
  if (!root) return []
  return Array.from(root.querySelectorAll(FOCUSABLE)).filter((el) => el.offsetParent !== null)
}

// Keep Tab within the dialog (simple wrap-around trap).
const trapTab = (e) => {
  const items = focusables()
  if (items.length === 0) {
    e.preventDefault()
    panelEl.value?.focus()
    return
  }
  const first = items[0]
  const last = items[items.length - 1]
  const active = document.activeElement
  const outside = !panelEl.value?.contains(active)
  if (e.shiftKey && (active === first || outside)) {
    e.preventDefault()
    last.focus()
  } else if (!e.shiftKey && (active === last || outside)) {
    e.preventDefault()
    first.focus()
  }
}

// The dialog only exists while open (the parent gates mount via
// v-if="dialogOpen"), so bind these for its whole lifetime.
const onKey = (e) => {
  if (e.key === 'Escape') emit('close')
  else if (e.key === 'Tab') trapTab(e)
}

onMounted(() => {
  prevActive = document.activeElement
  prevOverflow = document.body.style.overflow
  document.body.style.overflow = 'hidden'        // scroll lock
  document.addEventListener('keydown', onKey)
  // Move focus into the dialog (first focusable, else the panel itself).
  nextTick(() => {
    const items = focusables()
    ;(items[0] || panelEl.value)?.focus()
  })
})

onUnmounted(() => {
  document.removeEventListener('keydown', onKey)
  document.body.style.overflow = prevOverflow
  // Restore focus to whatever opened the dialog.
  if (prevActive && typeof prevActive.focus === 'function') prevActive.focus()
})
</script>
