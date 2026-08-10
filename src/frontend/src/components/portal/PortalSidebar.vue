<template>
  <aside class="flex flex-col h-full w-72 bg-gray-50 dark:bg-gray-950 border-r border-gray-200 dark:border-gray-800">
    <!-- Brand -->
    <div class="shrink-0 flex items-center gap-2 px-4 h-14 border-b border-gray-200 dark:border-gray-800">
      <svg class="w-6 h-6 text-action-primary-600" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" /></svg>
      <span class="font-semibold">Workspace</span>
    </div>

    <div class="p-3 space-y-2">
      <!-- New chat -->
      <button
        class="w-full flex items-center gap-2 rounded-xl border border-gray-300 dark:border-gray-700 px-3 py-2 text-sm font-medium hover:bg-white dark:hover:bg-gray-900 transition"
        @click="$emit('new-chat')"
      >
        <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" /></svg>
        New chat
      </button>

      <!-- Search -->
      <div class="relative">
        <svg class="w-4 h-4 text-gray-400 absolute left-3 top-2.5 pointer-events-none" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-4.35-4.35M17 11a6 6 0 11-12 0 6 6 0 0112 0z" /></svg>
        <input
          :value="search"
          type="search"
          placeholder="Search your chats…"
          class="w-full rounded-xl border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 text-sm pl-9 pr-3 py-2 focus:ring-2 focus:ring-action-primary-500/40 focus:border-action-primary-500 focus:outline-none"
          @input="$emit('update:search', $event.target.value)"
        />
      </div>
    </div>

    <div class="flex-1 min-h-0 overflow-y-auto px-2 pb-2">
      <!-- Search results replace history/agents while searching -->
      <div v-if="isSearching">
        <div v-if="searching" class="px-2 py-6 text-center text-xs text-gray-400">Searching…</div>
        <div v-else-if="!searchResults.length" class="px-2 py-6 text-center text-xs text-gray-400">No chats match.</div>
        <button
          v-for="r in searchResults"
          :key="r.session_id"
          class="w-full text-left rounded-lg px-2.5 py-2 hover:bg-white dark:hover:bg-gray-900 transition"
          @click="$emit('open-thread', { session_id: r.session_id, agent_name: r.agent_name })"
        >
          <div class="flex items-center gap-2">
            <PortalAvatar :name="r.agent_name" :avatar-url="avatarFor(r.agent_name)" :size="22" />
            <span class="min-w-0 flex-1">
              <span class="block text-sm truncate">{{ r.title || 'Chat' }}</span>
              <span v-if="r.snippet" class="block text-xs text-gray-400 truncate">{{ r.snippet }}</span>
            </span>
          </div>
        </button>
      </div>

      <template v-else>
        <!-- Agents -->
        <div class="px-2 pt-2 pb-1 text-[11px] font-semibold uppercase tracking-wide text-gray-400">Agents</div>
        <button
          v-for="a in roster"
          :key="a.name"
          class="w-full flex items-center gap-2.5 rounded-lg px-2.5 py-2 hover:bg-white dark:hover:bg-gray-900 transition"
          :title="`New chat with ${a.name}`"
          @click="$emit('new-chat-with-agent', a.name)"
        >
          <PortalAvatar :name="a.name" :avatar-url="a.avatar_url" :size="26" />
          <span class="min-w-0 text-left">
            <span class="block text-sm truncate">{{ a.name }}</span>
            <span v-if="a.description" class="block text-xs text-gray-400 truncate">{{ a.description }}</span>
          </span>
        </button>

        <!-- Unified, date-grouped history across all agents -->
        <div v-for="g in grouped" :key="g.label" class="mt-3">
          <div class="px-2 pb-1 text-[11px] font-semibold uppercase tracking-wide text-gray-400">{{ g.label }}</div>
          <button
            v-for="t in g.threads"
            :key="t.id"
            class="w-full flex items-center gap-2 rounded-lg px-2.5 py-2 text-left hover:bg-white dark:hover:bg-gray-900 transition"
            :class="t.id === currentSessionId ? 'bg-white dark:bg-gray-900 ring-1 ring-gray-200 dark:ring-gray-800' : ''"
            @click="$emit('open-thread', t)"
          >
            <PortalAvatar :name="t.agent_name" :avatar-url="avatarFor(t.agent_name)" :size="22" />
            <span class="text-sm truncate flex-1">{{ threadTitle(t) }}</span>
          </button>
        </div>

        <div v-if="!roster.length && !threads.length" class="px-2 py-8 text-center text-xs text-gray-400">
          No agents shared with you yet.
        </div>
      </template>
    </div>

    <!-- Footer: signed-in email + sign out -->
    <div class="shrink-0 border-t border-gray-200 dark:border-gray-800 p-3 flex items-center gap-2">
      <div class="min-w-0 flex-1">
        <div class="text-xs text-gray-400">Signed in</div>
        <div class="text-sm truncate" :title="clientEmail">{{ clientEmail }}</div>
      </div>
      <button
        class="shrink-0 p-2 rounded-lg text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-white dark:hover:bg-gray-900 transition"
        title="Sign out"
        @click="$emit('sign-out')"
      >
        <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" /></svg>
      </button>
    </div>
  </aside>
</template>

<script setup>
import { computed } from 'vue'
import PortalAvatar from './PortalAvatar.vue'
import { groupThreadsByDate, threadTitle } from './portalUtils'

const props = defineProps({
  roster: { type: Array, default: () => [] },
  threads: { type: Array, default: () => [] },     // merged, agent-tagged
  clientEmail: { type: String, default: '' },
  currentSessionId: { type: String, default: null },
  search: { type: String, default: '' },
  searching: { type: Boolean, default: false },
  searchResults: { type: Array, default: () => [] },
})
defineEmits(['new-chat', 'new-chat-with-agent', 'open-thread', 'update:search', 'sign-out'])

const isSearching = computed(() => (props.search || '').trim().length >= 2)
const grouped = computed(() => groupThreadsByDate(props.threads))

// ent#186: history + search rows show the conversation's agent avatar instead of
// a bare color dot. The URL is resolved from the roster already loaded at sign-in
// (no per-row fetch); an unknown agent or one without a generated avatar falls
// through to PortalAvatar's initials + tint.
const avatarByAgent = computed(() =>
  Object.fromEntries((props.roster || []).map((a) => [a.name, a.avatar_url])),
)
const avatarFor = (name) => avatarByAgent.value[name] || null
</script>
