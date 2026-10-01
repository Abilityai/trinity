<template>
  <Teleport to="body">
    <div
      v-if="modelValue"
      ref="overlay"
      tabindex="-1"
      class="fixed inset-0 z-50 flex items-center justify-center overflow-y-auto bg-gray-900/60 p-4"
      role="dialog"
      aria-modal="true"
      :aria-label="ariaLabel || undefined"
      :aria-labelledby="labelledby || undefined"
      @click="onOverlayClick"
      @keydown="onKeydown"
    >
      <!-- The panel. Consumers keep their own inner markup; this owns only the
           overlay, the keyboard contract, and the footprint. -->
      <div :class="panelClass" @click.stop>
        <slot />
      </div>
    </div>
  </Teleport>
</template>

<script setup>
/**
 * The shared modal shell (#1923, design-system principle 23).
 *
 * Six bespoke overlays each re-implemented the same markup and each omitted the
 * same two behaviours: Esc did nothing, and Tab walked out behind the overlay.
 * This is the one place that contract lives.
 *
 * It owns exactly four things — overlay, Esc, focus, scroll lock — and nothing
 * about content. Consumers keep their existing panel markup, so adopting it is
 * deleting two wrapper divs rather than rewriting a dialog.
 *
 * **Every decision is in `utils/focusTrap.js`, not here**, as functions over
 * plain data; this file is the wiring. The wiring is proven too:
 * `tests/unit/baseModal.spec.js` mounts this shell under jsdom (the repo's
 * per-file opt-in, #2918) and drives Esc, Tab, focus return and the lock. An
 * earlier version of this comment said the repo could not mount a component,
 * which was false and is exactly how two defects in the wiring shipped
 * unnoticed (the review on #2778).
 *
 * **`tabindex="-1"` on the overlay is load-bearing.** `@keydown` is on the
 * overlay and key events bubble UP from `document.activeElement`, so Esc only
 * works while focus is inside. Clicking non-focusable dialog text lands focus
 * on the nearest focusable ancestor — this overlay, now that it has a tabindex
 * — and a modal with no tabbable child can still be Esc-dismissed because
 * `overlay.focus()` is no longer a no-op on a plain div.
 *
 * **The scroll lock is shared and ref-counted** (`bodyScrollLock`): modals
 * nest, and a per-instance write to `document.body.style.overflow` unlocked
 * the page whenever an inner dialog mounted or closed while the outer was
 * open, and cleared other components' locks on unmount.
 *
 * `Teleport` matters: several of these modals are declared inside panels that
 * establish a stacking context, and a `z-50` overlay nested in one renders
 * *behind* its siblings. Teleporting to `<body>` makes the overlay's z-index
 * mean what it says.
 */
import { ref, watch, nextTick, onBeforeUnmount } from 'vue'
import {
  TABBABLE_SELECTOR, tabbable, nextFocusIndex, isDismissKey, isTabKey,
  initialFocusIndex, isBackdropClick, bodyScrollLock,
} from '../../utils/focusTrap.js'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  // Tailwind classes for the panel box. Defaulted, overridable — the six
  // modals have different widths and this must not force them to one.
  panelClass: {
    type: String,
    default: 'relative w-full max-w-lg rounded-lg bg-white p-6 shadow-xl dark:bg-gray-800',
  },
  ariaLabel: { type: String, default: '' },
  labelledby: { type: String, default: '' },
  // Click-outside is the default, but a form mid-edit may want to opt out so a
  // stray click cannot discard typing.
  closeOnBackdrop: { type: Boolean, default: true },
})

const emit = defineEmits(['update:modelValue', 'close'])

const overlay = ref(null)
let lastFocused = null
// Whether THIS instance holds one count on the shared lock — so a close or an
// unmount releases exactly what this instance took, never a sibling's.
let holdingLock = false

function takeLock() {
  if (holdingLock) return
  bodyScrollLock.acquire()
  holdingLock = true
}

function dropLock() {
  if (!holdingLock) return
  bodyScrollLock.release()
  holdingLock = false
}

function items() {
  if (!overlay.value) return []
  return tabbable(Array.from(overlay.value.querySelectorAll(TABBABLE_SELECTOR)))
}

function close() {
  emit('update:modelValue', false)
  emit('close')
}

function onOverlayClick(e) {
  if (props.closeOnBackdrop && isBackdropClick(e, overlay.value)) close()
}

function onKeydown(e) {
  if (isDismissKey(e)) {
    e.stopPropagation()
    close()
    return
  }
  if (!isTabKey(e)) return
  const list = items()
  const next = nextFocusIndex(list.length, list.indexOf(document.activeElement), e.shiftKey)
  if (next === null) return
  // Only take over once we know where to go — otherwise let the browser tab.
  e.preventDefault()
  list[next]?.focus()
}

watch(() => props.modelValue, async (open) => {
  if (open) {
    // Remember who opened it so focus can go home on dismiss.
    lastFocused = document.activeElement
    takeLock()
    await nextTick()
    const list = items()
    const idx = initialFocusIndex(list)
    if (idx !== null) list[idx].focus()
    else overlay.value?.focus?.()
  } else {
    // A closed instance that never held the lock (a nested dialog mounting
    // closed inside an open one) releases nothing — that was the bug.
    dropLock()
    // Focus returns to the trigger — without this a keyboard user is dropped
    // at the top of the document and has to tab back to where they were.
    lastFocused?.focus?.()
    lastFocused = null
  }
}, { immediate: true })

// A modal unmounted while open (route change, v-if on an ancestor) must not
// leave the page permanently unscrollable — and one unmounted while closed
// must not unlock a page someone else is holding.
onBeforeUnmount(dropLock)
</script>
