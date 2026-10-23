<template>
  <div
    ref="el"
    class="overflow-y-auto overflow-x-hidden [scrollbar-gutter:stable] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:focus-visible:ring-action-primary-400/40"
    :class="fill ? 'grow basis-0 min-h-[20rem]' : null"
    tabindex="0"
    role="region"
    :aria-label="label"
  >
    <slot />
  </div>
</template>

<script setup>
/**
 * #3451 — the bounded viewport of design-system principle 28: an unbounded set
 * scrolls inside this box and never grows the page. Named and focusable so the
 * scroll is reachable from the keyboard (the #3450 alerts-list shape). One root
 * element on purpose: consumers pass their `class` and `data-testid` through.
 *
 * `fill` makes the region take the rest of a flex column (`grow basis-0`) and
 * keeps a 20rem floor. The 0px basis is load-bearing: Tailwind's `flex-1` is a
 * 0% basis, which an auto-height column (a `min-h-*` page) resolves as the
 * content's height — the region then grows the page again (measured: 30,306 px
 * for 300 rows, in Chromium, Firefox and WebKit). Every ancestor between the
 * region and the page root must be a flex column with `grow basis-0` too, and
 * one with `mx-auto` needs `w-full` (auto margins suppress the stretch).
 * Without `fill` the consumer sets the cap (`max-h-*`).
 *
 * One axis only (principle 8): wide content scrolls in its own container. The
 * gutter is reserved so the width never jumps when the rows first overflow
 * (principle 6).
 */
import { ref } from 'vue'

defineProps({
  // What the region holds, for assistive tech ("Executions", "Resolved items").
  label: {
    type: String,
    required: true
  },
  // Take the remaining height of the flex column it sits in, with a 20rem floor.
  fill: {
    type: Boolean,
    default: true
  }
})

const el = ref(null)

// A new query (a filter change) starts at the first row; a poll never calls this.
function scrollToTop() {
  if (el.value) el.value.scrollTop = 0
}

defineExpose({ scrollToTop })
</script>
