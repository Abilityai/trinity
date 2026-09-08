<template>
  <!-- ent#492: the one resize handle, used by both Workspace splitters.
       `separator` with `aria-orientation="vertical"` is the ARIA role for
       exactly this, and it takes `valuenow/min/max` — so a screen-reader user
       hears the width they are changing, not just that something moved.

       A visual seam of 1px would be unusable with a pointer, so the hit area is
       8px and the seam is drawn INSIDE it: the grab target and the line people
       see are deliberately different sizes. -->
  <div
    class="group relative hidden sm:block w-2 shrink-0 cursor-col-resize touch-none select-none focus:outline-none"
    role="separator"
    tabindex="0"
    aria-orientation="vertical"
    :aria-label="label"
    :aria-valuenow="Math.round(value)"
    :aria-valuemin="min"
    :aria-valuemax="max"
    :data-testid="testid"
    @pointerdown="onPointerDown"
    @dblclick="$emit('reset')"
    @keydown="onKeyDown"
  >
    <!-- The seam. Transparent at rest so the layout reads as two columns
         meeting, not as a toolbar; it appears on hover, on keyboard focus, and
         for the whole drag — including when the pointer has left the element,
         which is most of a drag. -->
    <span
      class="absolute inset-y-0 left-1/2 -translate-x-1/2 w-px transition-colors"
      :class="dragging
        ? 'bg-action-primary-500'
        : 'bg-transparent group-hover:bg-action-primary-400 group-focus:bg-action-primary-500'"
      aria-hidden="true"
    ></span>
  </div>
</template>

<script setup>
/**
 * A column splitter (ent#492).
 *
 * Emits DELTAS and nothing else — it does not know which column it resizes,
 * which is what lets one component serve both splitters and keeps every clamp
 * and every stored width in `useColumnResize`.
 *
 * `side` says which way a positive drag grows the column: the sidebar is left
 * of its handle so dragging right widens it; the rail is right of its handle so
 * dragging right NARROWS it. Getting this wrong is invisible in a unit test and
 * instantly obvious in the hand, so it is a prop rather than a caller-applied
 * sign flip.
 */
import { ref } from 'vue'

const props = defineProps({
  // The width being changed, for ARIA and for the drag's starting point.
  value: { type: Number, required: true },
  min: { type: Number, required: true },
  max: { type: Number, required: true },
  label: { type: String, required: true },
  // 'left'  — the column is left of this handle (the sidebar)
  // 'right' — the column is right of this handle (the rail)
  side: { type: String, default: 'left' },
  step: { type: Number, default: 16 },
  coarseStep: { type: Number, default: 64 },
  testid: { type: String, default: 'column-resize-handle' },
})

const emit = defineEmits(['resize', 'reset'])

const dragging = ref(false)
let startX = 0
let startValue = 0

function apply(px) {
  emit('resize', Math.min(props.max, Math.max(props.min, px)))
}

function onPointerDown(e) {
  // Primary button only: a right-click drag is a context menu, not a resize.
  if (e.button !== 0) return
  dragging.value = true
  startX = e.clientX
  startValue = props.value
  // Pointer capture (AC 8) is what makes a drag survive crossing the Canvas
  // tab's IFRAME — without it the iframe swallows the pointer and the drag
  // stalls mid-gesture, which is the one failure people would actually hit.
  // It also routes the moves back here, so no window-level listener is needed.
  e.currentTarget.setPointerCapture?.(e.pointerId)
  e.currentTarget.addEventListener('pointermove', onPointerMove)
  e.currentTarget.addEventListener('pointerup', onPointerUp, { once: true })
  e.currentTarget.addEventListener('pointercancel', onPointerUp, { once: true })
  // Stops the thread's text selecting under the cursor for the whole drag.
  e.preventDefault()
}

function onPointerMove(e) {
  if (!dragging.value) return
  const delta = e.clientX - startX
  apply(startValue + (props.side === 'right' ? -delta : delta))
}

function onPointerUp(e) {
  dragging.value = false
  const el = e.currentTarget
  el?.releasePointerCapture?.(e.pointerId)
  el?.removeEventListener('pointermove', onPointerMove)
}

// AC 7. Arrows step, Shift-arrow steps coarsely, Home/End jump to the bounds.
// Directions match the pointer: for the rail, right-arrow narrows it, because
// the handle is on its left and "right" means the same thing to both hands.
function onKeyDown(e) {
  const grow = props.side === 'right' ? -1 : 1
  const amount = e.shiftKey ? props.coarseStep : props.step
  let next = null
  if (e.key === 'ArrowLeft') next = props.value - amount * grow
  else if (e.key === 'ArrowRight') next = props.value + amount * grow
  else if (e.key === 'Home') next = props.min
  else if (e.key === 'End') next = props.max
  else if (e.key === 'Enter' || e.key === ' ') { emit('reset'); e.preventDefault(); return }
  if (next === null) return
  e.preventDefault()
  apply(next)
}
</script>
