<template>
  <!-- ent#392: the composer typeahead panel. Presentational ONLY — it holds no
       decisions. What is offered, what is selected and what a pick inserts are
       all resolved by the pure functions in portalUtils.js, because this project
       has no component-mount harness and a decision living here is a decision no
       test can reach.

       Opens UPWARD (`bottom-full`): the composer sits at the bottom of the pane,
       so a downward panel would land off-screen. `max-h-[min(14rem,40vh)]` rather
       than a flat `max-h-56`: Portal.vue's root is `h-screen overflow-hidden`, so
       on mobile landscape with the keyboard up a fixed 224px panel clips against
       a composer sitting at ~y=250.

       z-30 is the same tier as this component's own agent-picker dropdown and
       strictly below the two z-40 overlays (mobile nav, files panel) that must
       cover it; the two z-30 panels sit at opposite ends of the pane and cannot
       overlap. The parent wrapper deliberately carries no z-index, so it creates
       no stacking context of its own. -->
  <div
    class="absolute bottom-full left-0 right-0 mb-2 z-30 rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 shadow-xl overflow-hidden"
    @mousedown.prevent
  >
    <div class="flex items-center justify-between px-3 py-1.5 border-b border-gray-100 dark:border-gray-800">
      <span class="text-[11px] font-semibold uppercase tracking-wide text-gray-400">{{ heading }}</span>
      <span class="text-[11px] text-gray-400 dark:text-gray-500 hidden sm:block">
        <kbd class="px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-[10px] font-mono">↑↓</kbd>
        move
        <kbd class="ml-1.5 px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-[10px] font-mono">Tab</kbd>
        insert
        <kbd class="ml-1.5 px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-[10px] font-mono">Esc</kbd>
        close
      </span>
    </div>

    <ul
      v-if="rows.length"
      ref="listEl"
      role="listbox"
      :aria-label="heading"
      class="max-h-[min(14rem,40vh)] overflow-y-auto py-1"
    >
      <li
        v-for="(row, i) in rows"
        :key="row.key"
        role="option"
        :aria-selected="i === activeIndex"
        class="flex items-start gap-2.5 px-3 py-2 cursor-pointer"
        :class="i === activeIndex
          ? 'bg-action-primary-50 dark:bg-action-primary-900/30'
          : 'hover:bg-gray-100 dark:hover:bg-gray-800'"
        @mousedown="$emit('pick', i)"
        @mouseenter="$emit('hover', i)"
      >
        <span class="shrink-0 mt-0.5 text-xs font-mono font-semibold text-action-primary-600 dark:text-action-primary-400">{{ kind }}</span>
        <span class="min-w-0 flex-1">
          <!-- Interpolation, never raw HTML: titles and descriptions are
               agent/operator-authored text arriving in a new place. -->
          <span class="block text-sm text-gray-900 dark:text-gray-100 line-clamp-2 break-words">{{ row.primary }}</span>
          <span v-if="row.secondary" class="block text-xs text-gray-500 dark:text-gray-400 line-clamp-1 break-words">{{ row.secondary }}</span>
        </span>
      </li>
    </ul>

    <!-- The honest empty line. It appears only on a bare trigger (the caller
         closes the popup once a query matches nothing), and it must never be a
         claim about operator configuration the client cannot observe. -->
    <p v-else-if="emptyMessage" class="px-3 py-2.5 text-xs text-gray-500 dark:text-gray-400">{{ emptyMessage }}</p>

    <div
      v-if="overflow > 0 || hiddenCount > 0"
      class="px-3 py-1.5 border-t border-gray-100 dark:border-gray-800 text-[11px] text-gray-400 dark:text-gray-500"
    >
      <span v-if="overflow > 0">{{ overflow }} more — keep typing to filter</span>
      <!-- #2213: two DIFFERENT omissions, said separately. `overflow` is rows this
           popup chose not to draw and typing will reach; `hiddenCount` is skills
           that never reached the browser, which typing cannot reach — so it must
           not be folded into the same number, and it must say what to do instead. -->
      <span v-if="hiddenCount > 0" :class="overflow > 0 ? 'block' : ''">
        {{ hiddenCount }} more not listed here — ask for it by name
      </span>
    </div>

    <p class="sr-only" aria-live="polite">{{ status }}</p>
  </div>
</template>

<script setup>
import { computed, ref, watch, nextTick } from 'vue'

const props = defineProps({
  kind: { type: String, required: true },          // '/' | '@'
  rows: { type: Array, default: () => [] },        // [{key, primary, secondary}]
  activeIndex: { type: Number, default: -1 },
  overflow: { type: Number, default: 0 },
  // #2213: client-visible skills that were truncated out of the PAYLOAD, i.e. not
  // searchable at all. Distinct from `overflow` (present but not rendered).
  hiddenCount: { type: Number, default: 0 },
  emptyMessage: { type: String, default: '' },
})
defineEmits(['pick', 'hover'])

const listEl = ref(null)

const heading = computed(() => (props.kind === '/' ? 'Playbooks' : 'Agents'))

const status = computed(() => {
  if (!props.rows.length) return props.emptyMessage || 'No suggestions'
  const n = props.rows.length + props.overflow
  const hidden = props.hiddenCount > 0
    ? ` ${props.hiddenCount} further playbook${props.hiddenCount === 1 ? '' : 's'} are not listed and must be asked for by name.`
    : ''
  return `${n} suggestion${n === 1 ? '' : 's'}. Use the arrow keys to choose one.${hidden}`
})

// Keep the chosen row in view without moving the page (principle 5: updates
// preserve scroll). `nearest` so arrowing down one row scrolls one row.
watch(() => props.activeIndex, async (i) => {
  if (i < 0) return
  await nextTick()
  listEl.value?.children?.[i]?.scrollIntoView({ block: 'nearest' })
})
</script>
