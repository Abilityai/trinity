<template>
  <!-- ent#524 / #3738: one composer chip — the file, its own outcome, and a ×
       that takes it back. Shared by the 1:1 and the room composer, which had
       the same markup in two files (#2662). -->
  <span
    class="inline-flex items-center gap-1 text-xs rounded-full pl-2.5 pr-1.5 py-1"
    :class="entry.error
      ? 'bg-status-danger-50 dark:bg-status-danger-900/30 text-status-danger-700 dark:text-status-danger-300'
      : 'bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300'"
    :title="entry.error || title || entry.name"
    :data-testid="testid"
  >
    <svg class="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13" /></svg>
    <span class="max-w-[10rem] truncate">{{ entry.name }}</span>
    <svg v-if="attachmentState(entry) === 'uploading'" class="w-3 h-3 animate-spin text-gray-400" viewBox="0 0 24 24" fill="none"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>
    <span v-else-if="attachmentState(entry) === 'failed'" class="max-w-[16rem] truncate opacity-90">· {{ entry.error }}</span>
    <slot v-else name="sent" />
    <!-- A real `type="button"`, so it never submits the composer's form;
         `.stop` keeps the click off the drop/compose handlers; `mousedown.prevent`
         keeps focus in the textarea. The glyph takes the chip's own ink in both
         arms and both themes (the OverflowTabs × idiom), and the hover ground is
         an ink-neutral overlay so no new colour is introduced. The `after:` box
         widens the hit area without changing the chip's height. -->
    <button
      type="button"
      class="relative -my-1 -mr-0.5 inline-flex items-center justify-center rounded-full p-1 text-current opacity-70 transition hover:opacity-100 hover:bg-black/10 dark:hover:bg-white/10 focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 disabled:cursor-not-allowed disabled:opacity-40 after:absolute after:-inset-2 after:content-['']"
      :disabled="locked"
      :aria-label="`Remove ${entry.name}`"
      :data-testid="`${testid}-remove`"
      @click.stop="emit('remove')"
      @mousedown.prevent
    >
      <svg class="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M6 18L18 6M6 6l12 12" /></svg>
    </button>
  </span>
</template>

<script setup>
import { attachmentState } from '@/composables/usePortalFileDrop'

defineProps({
  // One `usePortalFileDrop` entry.
  entry: { type: Object, required: true },
  // The × is inert while the composer is handing these files on.
  locked: { type: Boolean, default: false },
  // Hover text for a chip that is not failed; defaults to the file name.
  title: { type: String, default: '' },
  testid: { type: String, default: 'portal-attachment-chip' },
})
const emit = defineEmits(['remove'])
</script>
