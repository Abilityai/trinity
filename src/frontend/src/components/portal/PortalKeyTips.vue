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
      <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 8a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V8zm4 1v.01M11 9v.01M15 9v.01M7 13h10" /></svg>
    </button>
  </div>

  <!-- The OPEN column's: a small panel pinned to its bottom edge. A sibling of
       the rail's scroll area, never a child — so it does not scroll away and
       never sits over the content. -->
  <section v-else class="shrink-0 p-3" aria-label="Keyboard shortcut tips" data-testid="ws-key-tips">
    <BaseCard flush class="px-3 py-2.5">
      <div class="flex items-center justify-between gap-2">
        <h3 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Shortcuts</h3>
        <button
          type="button"
          class="-m-1 p-1 rounded text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700 transition"
          title="Hide"
          aria-label="Hide shortcut tips"
          data-testid="ws-key-tips-close"
          @click="$emit('dismiss')"
        >
          <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" /></svg>
        </button>
      </div>
      <ul class="mt-2 space-y-1.5">
        <li
          v-for="row in ROWS"
          :key="row.id"
          class="flex items-center justify-between gap-3"
          :data-ws-key-tip="row.id"
        >
          <span class="min-w-0 truncate text-xs text-gray-600 dark:text-gray-300">{{ row.label }}</span>
          <PortalKeyCap>{{ row.keys }}</PortalKeyCap>
        </li>
      </ul>
      <BaseButton
        size="sm"
        variant="ghost"
        class="mt-2 -ml-2"
        data-testid="ws-key-tips-all"
        @click="$emit('open-keys')"
      >All shortcuts</BaseButton>
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
import BaseButton from '@/components/base/BaseButton.vue'
import PortalKeyCap from './PortalKeyCap.vue'
import { keyTipRows, keyHint, keyShortcutsFor, hostPlatform } from './portalKeymap'

const KEY_PLATFORM = hostPlatform()
const ROWS = keyTipRows(KEY_PLATFORM)
const KEY_LIST_HINT = keyHint('key-list', KEY_PLATFORM)
const KEY_LIST_SHORTCUTS = keyShortcutsFor('key-list', KEY_PLATFORM)

defineProps({
  compact: { type: Boolean, default: false },
})
defineEmits(['open-keys', 'dismiss'])
</script>
