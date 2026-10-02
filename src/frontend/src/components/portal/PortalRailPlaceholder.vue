<!--
  trinity-enterprise#610 §3g A4 (round 3) — the rail's column on the Inbox
  while it is OPEN but nothing is selected.

  The rail needs an agent for its tabs, and at 1280 an open rail stacks the
  Inbox, where nothing is previewed — so without this the rail, its strip and
  the one control that closes it all vanished, with `open` still saved. The
  column keeps the width the Inbox counted and the rail's own collapse
  control (`PortalRail`'s markup, so the two read as one surface).
-->
<template>
  <aside
    class="w-[var(--ws-rail,24rem)] flex flex-col min-h-0 border-l border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900"
    aria-label="Conversation rail"
    data-testid="ws-rail-inbox-placeholder"
  >
    <div class="h-14 shrink-0 flex items-center justify-end px-2 border-b border-gray-200 dark:border-gray-700">
      <button
        type="button"
        class="w-9 h-9 rounded-lg flex items-center justify-center text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800 transition"
        title="Collapse"
        aria-label="Collapse the conversation rail"
        aria-expanded="true"
        data-testid="ws-rail-inbox-collapse"
        @click="$emit('collapse')"
      >
        <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 5l7 7-7 7M5 5l7 7-7 7" /></svg>
      </button>
    </div>
    <p class="m-auto px-6 text-center text-[12.5px] text-gray-600 dark:text-gray-300">Open an item to see its agent's work here.</p>
  </aside>
</template>

<script setup>
defineEmits(['collapse'])
</script>
