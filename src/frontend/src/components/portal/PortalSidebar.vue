<template>
  <aside class="flex flex-col h-full w-72 bg-gray-50 dark:bg-gray-950 border-r border-gray-200 dark:border-gray-800">
    <!-- Brand. ent#359: the aggregate "waiting on you" count lives here, because
         the agents block now occupies the top of the scroll region and would
         otherwise scroll a fleet-wide signal out of view. -->
    <div class="shrink-0 flex items-center gap-2 px-4 h-14 border-b border-gray-200 dark:border-gray-800">
      <svg class="w-6 h-6 text-action-primary-600" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" /></svg>
      <span class="font-semibold">Workspace</span>
      <span
        v-if="totalWaiting"
        class="ml-auto shrink-0 min-w-[1.25rem] px-1.5 h-5 rounded-full bg-action-primary-600 text-white text-[11px] font-semibold flex items-center justify-center"
        :title="`${totalWaiting} ${totalWaiting === 1 ? 'reply' : 'replies'} you haven't read`"
      >{{ totalWaiting > 99 ? '99+' : totalWaiting }}</span>
    </div>

    <div class="p-3 space-y-2">
      <!-- New chat -->
      <!-- ent#357: disabled with an empty roster. `newChat()` only resets
           conversation state, so with no agent to chat with every branch of it
           is a no-op — the button looked live and did nothing, which is the
           dead-end this surface is supposed to have stopped having. -->
      <button
        :disabled="!roster.length"
        :title="roster.length ? 'Start a new conversation' : 'No agents available yet'"
        class="w-full flex items-center gap-2 rounded-xl border border-gray-300 dark:border-gray-700 px-3 py-2 text-sm font-medium hover:bg-white dark:hover:bg-gray-900 transition disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:bg-transparent dark:disabled:hover:bg-transparent"
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
        <!-- ============================ AGENTS ============================ -->
        <!-- ent#359: its own surface, not just a labelled run of rows. An agent
             is a destination now; a chat is a record of visiting one. Giving the
             two the same visual weight is what made the old sidebar read as one
             undifferentiated list. -->
        <section class="mt-2 rounded-xl bg-white dark:bg-gray-900 ring-1 ring-gray-200 dark:ring-gray-800 p-1.5">
          <div class="px-1.5 pt-1 pb-1.5 text-[11px] font-semibold uppercase tracking-wide text-gray-400">Agents</div>

          <button
            v-for="a in roster"
            :key="a.name"
            class="w-full flex items-center gap-2.5 rounded-lg px-2 py-2 hover:bg-gray-50 dark:hover:bg-gray-800 transition"
            :title="agentRowTitle(a.name)"
            @click="onAgentClick(a.name)"
          >
            <PortalAvatar :name="a.name" :avatar-url="a.avatar_url" :size="26" />
            <span class="min-w-0 text-left flex-1">
              <span class="block text-sm truncate">{{ a.name }}</span>
              <span v-if="a.description" class="block text-xs text-gray-400 truncate">{{ a.description }}</span>
            </span>
            <span
              v-if="waitingFor(a.name)"
              class="shrink-0 min-w-[1.25rem] px-1.5 h-5 rounded-full bg-action-primary-600 text-white text-[11px] font-semibold flex items-center justify-center"
            >{{ waitingFor(a.name) > 99 ? '99+' : waitingFor(a.name) }}</span>
          </button>

          <!-- ent#357/#359 AC: an empty roster keeps a next action. Which one
               depends on who is looking — a platform user can go make an agent,
               an external client can only ask whoever invited them. -->
          <div v-if="!roster.length" class="px-2 py-3 text-xs text-gray-500 dark:text-gray-400">
            <template v-if="isPlatformSession">
              No agents yet.
              <a href="/" class="text-action-primary-600 hover:underline">Create one →</a>
            </template>
            <template v-else>
              No agents shared with you yet — ask whoever invited you to share one.
            </template>
          </div>
        </section>

        <!-- ============================ CHATS ============================= -->
        <!-- Starred first, and LIFTED OUT of the date groups below (a starred
             chat appears exactly once — see partitionStarred). -->
        <div v-if="starred.length" class="mt-3">
          <div class="px-2 pb-1 text-[11px] font-semibold uppercase tracking-wide text-gray-400">Starred</div>
          <ChatRow
            v-for="t in starred"
            :key="rowKey(t)"
            :thread="t"
            :active="isActive(t)"
            :avatar-for="avatarFor"
            @open="$emit('open-thread', t)"
            @toggle-star="$emit('toggle-star', t)"
          />
        </div>

        <div v-for="g in grouped" :key="g.label" class="mt-3">
          <div class="px-2 pb-1 text-[11px] font-semibold uppercase tracking-wide text-gray-400">{{ g.label }}</div>
          <ChatRow
            v-for="t in g.threads"
            :key="rowKey(t)"
            :thread="t"
            :active="isActive(t)"
            :avatar-for="avatarFor"
            @open="$emit('open-thread', t)"
            @toggle-star="$emit('toggle-star', t)"
          />
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
import ChatRow from './PortalChatRow.vue'
import {
  groupThreadsByDate, partitionStarred, unreadByAgent, totalUnread,
} from './portalUtils'

const props = defineProps({
  roster: { type: Array, default: () => [] },
  threads: { type: Array, default: () => [] },     // merged, agent-tagged, star/unread-tagged
  clientEmail: { type: String, default: '' },
  currentSessionId: { type: String, default: null },
  currentRoomId: { type: String, default: null },
  isPlatformSession: { type: Boolean, default: false },
  search: { type: String, default: '' },
  searching: { type: Boolean, default: false },
  searchResults: { type: Array, default: () => [] },
})
const emit = defineEmits([
  'new-chat', 'new-chat-with-agent', 'open-thread', 'toggle-star',
  'update:search', 'sign-out',
])

const isSearching = computed(() => (props.search || '').trim().length >= 2)

const split = computed(() => partitionStarred(props.threads))
const starred = computed(() => split.value.starred)
const grouped = computed(() => groupThreadsByDate(split.value.rest))

const waiting = computed(() => unreadByAgent(props.threads))
const waitingFor = (name) => waiting.value[name] || 0
const totalWaiting = computed(() => totalUnread(props.threads))

// A row key has to include the kind: thread ids and room ids are independent
// spaces, so two chats of different kinds could collide on a bare id.
const rowKey = (t) => `${t.is_room ? 'room' : 'thread'}:${t.id || t.session_id}`
const isActive = (t) => (t.is_room
  ? t.id === props.currentRoomId
  : (t.id || t.session_id) === props.currentSessionId)

// ent#359: clicking an agent that is waiting on you opens the conversation it
// is waiting in, rather than a blank one. A badge that says "2 replies" next to
// a control that starts an empty chat is a contradiction — the count is the
// reason you clicked. With nothing unread the row keeps its old behaviour.
//
// (Opening an agent's own PAGE is ent#360; this is not a substitute for it.)
function latestUnreadFor(name) {
  return props.threads.find((t) => {
    if (!(Number(t?.unread) > 0)) return false
    const names = Array.isArray(t.agent_names) && t.agent_names.length
      ? t.agent_names : [t.agent_name]
    return names.includes(name)
  }) || null
}
function onAgentClick(name) {
  const unread = latestUnreadFor(name)
  if (unread) emit('open-thread', unread)
  else emit('new-chat-with-agent', name)
}
function agentRowTitle(name) {
  const n = waitingFor(name)
  if (!n) return `New chat with ${name}`
  return `${n} unread ${n === 1 ? 'reply' : 'replies'} — open the latest`
}

// ent#186: history + search rows show the conversation's agent avatar instead of
// a bare color dot. The URL is resolved from the roster already loaded at sign-in
// (no per-row fetch); an unknown agent or one without a generated avatar falls
// through to PortalAvatar's initials + tint.
const avatarByAgent = computed(() =>
  Object.fromEntries((props.roster || []).map((a) => [a.name, a.avatar_url])),
)
const avatarFor = (name) => avatarByAgent.value[name] || null
</script>
