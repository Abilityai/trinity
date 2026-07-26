<template>
  <aside class="flex flex-col h-full w-72 shrink-0 bg-gray-50 dark:bg-gray-950 border-r border-gray-200 dark:border-gray-800">
    <div class="shrink-0 p-3 border-b border-gray-200 dark:border-gray-800">
      <button
        class="w-full flex items-center justify-center gap-2 rounded-xl bg-action-primary-600 hover:bg-action-primary-700 text-white px-3 py-2 text-sm font-medium transition"
        @click="$emit('new-session')"
      >
        <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" /></svg>
        New session
      </button>
    </div>

    <div class="flex-1 min-h-0 overflow-y-auto p-2 space-y-1">
      <div v-if="loading && !rooms.length" class="px-2 py-8 text-center text-xs text-gray-400">Loading…</div>

      <!-- empty state: explainer + CTA -->
      <div v-else-if="!rooms.length" class="px-3 py-10 text-center">
        <div class="mx-auto w-10 h-10 rounded-full bg-gray-200 dark:bg-gray-800 flex items-center justify-center mb-3">
          <svg class="w-5 h-5 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M17 8h2a2 2 0 012 2v6a2 2 0 01-2 2h-2v4l-4-4H9a2 2 0 01-2-2m10-8V6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2v4l4-4h2" /></svg>
        </div>
        <p class="text-sm font-medium text-gray-700 dark:text-gray-200">No sessions yet</p>
        <p class="mt-1 text-xs text-gray-400 leading-relaxed">
          Start a shared session to work a topic with several agents at once. Mention an agent to bring it in.
        </p>
        <button class="mt-3 text-xs font-medium text-action-primary-600 hover:underline" @click="$emit('new-session')">
          Start your first session →
        </button>
      </div>

      <button
        v-for="r in rooms"
        :key="r.id"
        class="w-full text-left rounded-lg px-2.5 py-2 transition"
        :class="r.id === activeRoomId ? 'bg-white dark:bg-gray-900 ring-1 ring-gray-200 dark:ring-gray-800' : 'hover:bg-white dark:hover:bg-gray-900'"
        @click="$emit('open', r.id)"
      >
        <div class="flex items-center gap-2">
          <span
            class="w-2 h-2 rounded-full shrink-0"
            :class="r.status === 'open' ? 'bg-status-success-500 animate-pulse' : 'bg-gray-400 dark:bg-gray-600'"
            :title="r.status === 'open' ? 'Active' : `Closed — ${r.stop_reason || 'ended'}`"
          ></span>
          <span class="text-sm font-medium truncate flex-1">{{ r.name }}</span>
        </div>
        <div v-if="r.topic" class="ml-4 text-xs text-gray-400 truncate">{{ r.topic }}</div>
        <div class="ml-4 mt-1 flex items-center gap-3 text-[11px] text-gray-400">
          <span>{{ r.participant_count }} participant<span v-if="r.participant_count !== 1">s</span></span>
          <span>·</span>
          <span>{{ r.message_count }} message<span v-if="r.message_count !== 1">s</span></span>
        </div>
      </button>
    </div>
  </aside>
</template>

<script setup>
defineProps({
  rooms: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  activeRoomId: { type: String, default: null },
})
defineEmits(['new-session', 'open'])
</script>
