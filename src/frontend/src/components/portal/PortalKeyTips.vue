<template>
  <!-- The rail's shortcut tips (ent#621 follow-up), in the rail's two desktop
       forms. COMPACT is the collapsed strip's: 48px holds no panel, so it is
       one icon that opens the full list. -->
  <div v-if="compact" class="pb-2 flex justify-center">
    <button
      type="button"
      class="w-9 h-9 rounded-lg flex items-center justify-center text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800 transition"
      :title="`Keyboard shortcuts (${KEY_LIST_HINT})`"
      :aria-label="`Keyboard shortcuts (${KEY_LIST_HINT})`"
      :aria-keyshortcuts="KEY_LIST_SHORTCUTS"
      data-testid="portal-rail-keys"
      @click="$emit('open-keys')"
    >
      <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" :d="KEYBOARD_GLYPH" /></svg>
    </button>
  </div>

  <!-- The OPEN column's: a small panel pinned to its bottom edge. A sibling of
       the rail's scroll area, never a child — so it does not scroll away and
       never sits over the content.

       TWO ROWS, and nothing above or below them: the four tips flow by column
       into a 2×2 (agent over chat, search over new chat), and the two controls
       stack beside them. What gives when the rail is dragged narrow is the
       LABEL (it shortens with an ellipsis, the full text on hover); a key cap
       never wraps and a row never becomes two. The columns are `auto`, so
       each is as wide as its own content asks before either gives anything
       up — the first is the wider one (`Alt+Shift+↑↓` against `Ctrl+J`), and
       an even split would shorten its labels with room to spare beside it. -->
  <section v-else class="shrink-0 p-2" aria-label="Keyboard shortcut tips" data-testid="ws-key-tips">
    <BaseCard flush class="flex items-center gap-1.5 pl-2 pr-1 py-1.5">
      <ul class="flex-1 min-w-0 grid grid-flow-col grid-rows-2 grid-cols-[auto_auto] gap-x-2.5 gap-y-1">
        <li
          v-for="row in ROWS"
          :key="row.id"
          class="min-w-0 flex items-center justify-between gap-1.5"
          :title="`${row.label} (${row.fullKeys})`"
          :data-ws-key-tip="row.id"
        >
          <span class="min-w-0 truncate text-xs text-gray-600 dark:text-gray-300" data-ws-key-tip-label>{{ row.label }}</span>
          <PortalKeyCap>{{ row.keys }}</PortalKeyCap>
        </li>
      </ul>
      <div class="shrink-0 flex flex-col items-center gap-0.5">
        <button
          type="button"
          :class="TIP_BTN"
          title="Hide"
          aria-label="Hide shortcut tips"
          data-testid="ws-key-tips-close"
          @click="$emit('dismiss')"
        >
          <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" /></svg>
        </button>
        <button
          type="button"
          :class="TIP_BTN"
          :title="`All shortcuts (${KEY_LIST_HINT})`"
          :aria-label="`All shortcuts (${KEY_LIST_HINT})`"
          :aria-keyshortcuts="KEY_LIST_SHORTCUTS"
          data-testid="ws-key-tips-all"
          @click="$emit('open-keys')"
        >
          <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" :d="KEYBOARD_GLYPH" /></svg>
        </button>
      </div>
    </BaseCard>
  </section>
</template>

<script setup>
/**
 * Every chord printed here is read from the one key map (`keyTipRows`,
 * `keyHint`), so nothing in this file can name a key of its own. Closing the
 * panel is REPORTED, not remembered: the shell owns that memory, and tells the
 * rail whether to mount this at all.
 */
import BaseCard from '@/components/base/BaseCard.vue'
import PortalKeyCap from './PortalKeyCap.vue'
import { keyTipRows, keyHint, keyShortcutsFor, hostPlatform } from './portalKeymap'

const KEY_PLATFORM = hostPlatform()
const ROWS = keyTipRows(KEY_PLATFORM)
const KEY_LIST_HINT = keyHint('key-list', KEY_PLATFORM)
const KEY_LIST_SHORTCUTS = keyShortcutsFor('key-list', KEY_PLATFORM)

// The keyboard the sidebar footer's "Keyboard shortcuts" button draws: the same
// door, so the same picture — in both forms of this component.
const KEYBOARD_GLYPH = 'M3 8a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V8zm4 1v.01M11 9v.01M15 9v.01M7 13h10'
// The panel's two controls, ONE class string: 24px targets stacked beside the
// two rows, so they add no line of their own.
const TIP_BTN = 'p-1 rounded text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700 transition'

defineProps({
  compact: { type: Boolean, default: false },
})
defineEmits(['open-keys', 'dismiss'])
</script>
