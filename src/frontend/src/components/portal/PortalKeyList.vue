<template>
  <!-- `data-ws-key-list` is how `⌘/` can CLOSE the list it opened: the probe
       suppresses every shell key under anything `aria-modal`, and the key list
       is one. The marker rides the dialog element itself (the same element that
       carries `aria-modal`), because that is the element the `:not(...)`
       exclusion has to match. -->
  <BaseModal
    data-ws-key-list="true"
    :model-value="modelValue"
    aria-label="Keyboard shortcuts"
    panel-class="relative w-full max-w-lg rounded-lg bg-white p-6 shadow-xl dark:bg-gray-800 max-h-[85vh] overflow-y-auto"
    @update:model-value="$emit('update:modelValue', $event)"
  >
    <div data-testid="ws-key-list">
      <div class="flex items-start justify-between gap-4">
        <h2 class="text-lg font-semibold text-gray-900 dark:text-gray-100">Keyboard shortcuts</h2>
        <button
          type="button"
          class="shrink-0 -m-1 p-1 rounded text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 transition"
          aria-label="Close"
          data-testid="ws-key-list-close"
          @click="$emit('update:modelValue', false)"
        >
          <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" /></svg>
        </button>
      </div>

      <!-- Grouped the way the keys divide in the hand, not the way the map is
           ordered: what MOVES you, what you DO where you are, and the ones a
           component already owned before this map existed. -->
      <section v-for="group in groups" :key="group.id" class="mt-5 first:mt-4">
        <h3 class="text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">{{ group.title }}</h3>
        <ul class="mt-2 divide-y divide-gray-100 dark:divide-gray-700">
          <li
            v-for="row in group.rows"
            :key="row.action"
            class="flex items-start justify-between gap-4 py-2"
            :data-ws-key-row="row.action"
          >
            <span class="min-w-0">
              <span class="block text-sm text-gray-900 dark:text-gray-100">{{ row.label }}</span>
              <span v-if="row.note" class="block text-xs text-gray-500 dark:text-gray-400">{{ row.note }}</span>
            </span>
            <!-- `<kbd>` on gray + `font-mono`, the Dashboard's shape: a key cap
                 reads as a key cap without a hue claiming meaning it has not got. -->
            <kbd class="shrink-0 rounded border border-gray-200 bg-gray-50 px-1.5 py-0.5 text-[11px] font-mono text-gray-600 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300">{{ row.keys }}</kbd>
          </li>
        </ul>
      </section>

      <!-- The footnotes are the honest small print: every one of them is a
           thing a person would otherwise discover by pressing a key and
           deciding the feature is broken. -->
      <p class="mt-5 text-xs text-gray-500 dark:text-gray-400">
        {{ layoutNote }}
      </p>
    </div>
  </BaseModal>
</template>

<script setup>
/**
 * The `⌘/` key list (ent#621).
 *
 * Every row is rendered from `keyListRows(WORKSPACE_KEYMAP, …)` — the same
 * declaration the dispatcher resolves against. A hand-typed list is a second
 * source of truth for the one fact this dialog exists to state, and the copy
 * is always the half that goes stale.
 *
 * It is built ON `BaseModal` (contract principle 23) rather than beside it, so
 * Esc, the focus trap, focus-return to the message field and the scroll lock
 * are the shared ones (#1923) and not a seventh bespoke overlay. It is also
 * always mounted and `v-model`-driven: a `v-if` toggle unmounts past the close
 * branch, and focus never returns.
 */
import { computed } from 'vue'
import BaseModal from '@/components/base/BaseModal.vue'
import { SCOPE_MOVING, SCOPE_DOING, OWNER_PROTOCOL } from './portalKeymap'
import { isMacLike } from './portalUtils'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  // `keyListRows(...)` output — the shell derives it so the platform and the
  // "is there a rail on this page" answer are the live ones.
  rows: { type: Array, default: () => [] },
  platform: { type: String, default: '' },
})

defineEmits(['update:modelValue'])

const GROUP_TITLES = Object.freeze([
  { id: 'moving', title: 'Move around', match: (r) => r.owner !== OWNER_PROTOCOL && r.scope === SCOPE_MOVING },
  { id: 'doing', title: 'Do something here', match: (r) => r.owner !== OWNER_PROTOCOL && r.scope === SCOPE_DOING },
  { id: 'protocol', title: 'Wherever you are', match: (r) => r.owner === OWNER_PROTOCOL },
])

const groups = computed(() => GROUP_TITLES
  .map((g) => ({ ...g, rows: props.rows.filter((r) => r && g.match(r)) }))
  .filter((g) => g.rows.length))

// On a non-US layout the shifted forms work (`Shift+7` is `/` on German), and
// on Windows with two input languages Alt+Shift is also the language switch —
// both are things people hit, so both are said here rather than in a doc.
const layoutNote = computed(() => (isMacLike(props.platform)
  ? 'Shifted punctuation works too, so these keys hold on non-US layouts.'
  : 'Shifted punctuation works too, so these keys hold on non-US layouts. With two input languages installed, Windows may also read Alt+Shift as its language switch.'))
</script>
