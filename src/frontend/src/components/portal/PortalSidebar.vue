<template>
  <aside class="flex flex-col h-full w-72 bg-gray-50 dark:bg-gray-950 border-r border-gray-200 dark:border-gray-800">
    <!-- Brand. ent#359: the aggregate "waiting on you" count lives here, because
         the agents block now occupies the top of the scroll region and would
         otherwise scroll a fleet-wide signal out of view. -->
    <div class="shrink-0 flex items-center gap-2 px-4 h-14 border-b border-gray-200 dark:border-gray-800">
      <svg class="w-6 h-6 text-action-primary-600" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" /></svg>
      <span class="font-semibold">Workspace</span>
      <!-- ent#364: an ASK is a distinct fact from an unread reply — one is waiting
           on you to decide, the other on you to read — so it gets its own badge
           rather than being summed into that one. Before the unread count,
           because a blocked agent outranks unread chatter.

           #2424: `status-urgent` (not amber) — the token the operator NavBar's
           pending-operator-queue badge already uses for "waiting on you", so the
           two surfaces agree. Amber maps to `state-autonomous`, an operating
           mode, which is a different claim. The wording comes from
           `askBadgeTitle` because the inline literal said "agents" while
           `askCount` counted asks. -->
      <span
        v-if="askCount"
        class="ml-auto shrink-0 min-w-[1.25rem] px-1.5 h-5 rounded-full bg-status-urgent-500 text-white text-[11px] font-semibold flex items-center justify-center"
        data-testid="sidebar-ask-count"
        :title="askBadgeTitle(askCount)"
      >{{ askCount > 99 ? '99+' : askCount }}</span>
      <span
        v-if="totalWaiting"
        class="shrink-0 min-w-[1.25rem] px-1.5 h-5 rounded-full bg-action-primary-600 text-white text-[11px] font-semibold flex items-center justify-center"
        :class="askCount ? 'ml-1.5' : 'ml-auto'"
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
          :placeholder="SEARCH_PLACEHOLDER"
          class="w-full rounded-xl border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 text-sm pl-9 pr-3 py-2 focus:ring-2 focus:ring-action-primary-500/40 focus:border-action-primary-500 focus:outline-none"
          @input="$emit('update:search', $event.target.value)"
        />
      </div>
    </div>

    <div class="flex-1 min-h-0 overflow-y-auto px-2 pb-2">
      <!-- ============================ AGENTS ============================ -->
      <!-- ent#402: the agents section is mounted in BOTH modes. Search used to
           unmount the whole steady state, which is why a search box sitting
           directly above the roster did nothing to it: the roster it was in
           front of stopped existing the moment you typed. Keeping the section
           mounted also keeps the sidebar's footprint stable across the first
           two keystrokes (layout stability, p4/p6) — only its CONTENTS narrow. -->
      <!-- ent#359: its own surface, not just a labelled run of rows. An agent
           is a destination now; a chat is a record of visiting one. Giving the
           two the same visual weight is what made the old sidebar read as one
           undifferentiated list. -->
      <section class="mt-2 rounded-xl bg-white dark:bg-gray-900 ring-1 ring-gray-200 dark:ring-gray-800 p-1.5">
        <!-- ent#402: while searching the label states the MATCH count. The
             toggle beside the rows states the overflow, so this must not
             repeat it — one fact, one place. -->
        <div class="px-1.5 pt-1 pb-1.5 text-[11px] font-semibold uppercase tracking-wide text-gray-400">{{ isSearching ? agentResultsLabel(agentResults.total) : 'Agents' }}</div>

        <!-- #2159: the roster load is the first thing that happens after
             sign-in and it is not instant on a large fleet. Without this the
             block renders empty and the page reads as hung, not loading. Three
             skeleton rows: enough to say "rows are coming", not so many that a
             small roster jumps when they are replaced. -->
        <div v-if="loadingRoster && !roster.length" class="px-2 py-1.5" aria-busy="true">
          <div v-for="i in 3" :key="i" class="flex items-center gap-2.5 py-2 animate-pulse">
            <div class="w-[26px] h-[26px] rounded-full bg-gray-200 dark:bg-gray-800 shrink-0"></div>
            <div class="min-w-0 flex-1 space-y-1.5">
              <div class="h-3 w-1/2 rounded bg-gray-200 dark:bg-gray-800"></div>
              <div class="h-2.5 w-1/3 rounded bg-gray-100 dark:bg-gray-800/60"></div>
            </div>
          </div>
          <span class="sr-only">Loading your agents…</span>
        </div>

        <button
          v-for="a in shownAgents"
          :key="a.name"
          class="w-full flex items-center gap-2.5 rounded-lg px-2 py-2 hover:bg-gray-50 dark:hover:bg-gray-800 transition"
          :title="agentRowTitle(a)"
          @click="onAgentClick(a.name)"
        >
          <PortalAvatar :name="a.name" :avatar-url="a.avatar_url" :size="26" />
          <span class="min-w-0 text-left flex-1">
            <!-- #2159: the human-facing name leads; the slug is the subtitle.
                 The description was here and is not identity — two agents can
                 share one, and it pushed the only unique handle off the row.

                 The subtitle asks whether the title ALREADY is the slug, not
                 whether `display_label` is truthy. Those disagree on a
                 whitespace-only label, which `agentLabel` treats as unset:
                 the title falls back to the slug while a raw-truthiness test
                 still renders the subtitle, printing the slug twice. One
                 decision, read twice — the same rule the server/client split
                 above follows. -->
            <span class="block text-sm truncate">{{ agentLabel(a) }}</span>
            <span v-if="agentLabel(a) !== a.name" class="block text-xs text-gray-400 truncate font-mono">{{ a.name }}</span>
          </span>
          <!-- #2196: the agent can't currently run. LABEL, never disable —
               disabling would relocate the dead state rather than remove it,
               since a client whose agents are all stopped (a routine
               resource-saving posture) would get an entirely inert Workspace.
               The chip sets the expectation; the server's 502 is the honest
               refusal. Nothing is rendered for `ready` or `unknown`.

               The slot's footprint is RESERVED (`min-w`, on the row always) so
               a row does not reflow when an agent starts or stops between
               refreshes — the same reason the roster is not re-sorted by this. -->
          <span class="shrink-0 min-w-[4.5rem] flex justify-end">
            <BaseBadge v-if="chipFor(a)" :variant="chipFor(a).variant">{{ chipFor(a).label }}</BaseBadge>
          </span>
          <!-- #2424: the ask badge ent#364's comment above already promised.
               It got an aggregate in the brand header and nothing per row, so
               the header advertised a count with no way to reach the agent it
               meant. Same token as that header badge; deliberately a DIFFERENT
               colour from the unread pill beside it, because they are
               different obligations. -->
          <span
            v-if="askCountFor(a.name)"
            class="shrink-0 min-w-[1.25rem] px-1.5 h-5 rounded-full bg-status-urgent-500 text-white text-[11px] font-semibold flex items-center justify-center"
            data-testid="agent-ask-count"
          >{{ askCountFor(a.name) > 99 ? '99+' : askCountFor(a.name) }}</span>
          <span
            v-if="waitingFor(a.name)"
            class="shrink-0 min-w-[1.25rem] px-1.5 h-5 rounded-full bg-action-primary-600 text-white text-[11px] font-semibold flex items-center justify-center"
          >{{ waitingFor(a.name) > 99 ? '99+' : waitingFor(a.name) }}</span>
        </button>

        <!-- #2159: ONE persistent button, never two v-if-alternated ones —
             alternating drops keyboard focus on collapse (the #2101 lesson).
             Expands in place; the chat pane stays the single scroll axis. -->
        <!-- ent#402: ONE element in both modes, hidden with v-show rather
             than swapped for a second button — #2159's lesson is about the
             ELEMENT, so alternating a search toggle with a steady-state one
             would drop keyboard focus exactly as two v-ifs did. The v-if is
             unchanged; v-show is what varies, because while searching the
             toggle is only meaningful when something is actually hidden. -->
        <button
          v-if="roster.length > AGENT_COLLAPSE_LIMIT"
          v-show="showAgentToggle({ searching: isSearching, hidden: agentResults.hidden, expanded: agentsExpanded })"
          type="button"
          class="w-full text-left px-2 py-1.5 text-xs text-action-primary-600 dark:text-action-primary-400 hover:underline"
          :aria-expanded="agentsExpanded"
          @click="agentsExpanded = !agentsExpanded"
        >{{ agentToggleLabel({ searching: isSearching, expanded: agentsExpanded, rosterCount: roster.length, matchCount: agentResults.total }) }}</button>

        <!-- ent#402: the per-section line, and ONLY when there is a roster to
             have searched. With no roster at all the next-action block below
             is the truer sentence ("none shared with you yet"), and printing
             both would state the same absence twice in two different words. -->
        <div
          v-if="isSearching && emptyLines.agents && roster.length"
          class="px-2 py-3 text-xs text-gray-400"
        >{{ emptyLines.agents }}</div>

        <!-- ent#357/#359 AC: an empty roster keeps a next action. Which one
             depends on who is looking — a platform user can go make an agent,
             an external client can only ask whoever invited them. -->
        <div v-if="!roster.length && !loadingRoster" class="px-2 py-3 text-xs text-gray-500 dark:text-gray-400">
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
      <!-- ent#402: only the CHAT half swaps while searching. The chat side is a
           server request over history the client does not hold; the agent side
           above is a filter over a roster it already has, which is why the two
           halves report their emptiness separately and never share a sentence. -->
      <div v-if="isSearching" class="mt-3">
        <!-- Header and section status are ONE group and share one ink
             declaration; the result rows below are siblings, so they keep the
             body colour a chat title needs. -->
        <div class="text-gray-400">
          <div class="px-2 pb-1 text-[11px] font-semibold uppercase tracking-wide">Chats</div>
          <div v-if="emptyLines.chats" class="px-2 py-3 text-xs">
            {{ emptyLines.chats }}
            <!-- The next action, on the one state where BOTH halves came back
                 empty — a dead end otherwise (p16). Inside the line rather than
                 beside it: it is a second sentence about the same emptiness. -->
            <span v-if="emptyLines.hint" class="block pt-1">{{ emptyLines.hint }}</span>
          </div>
        </div>
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
    <!-- #2258: ONE button, labelled for what it does to THIS principal. A
         platform user's workspace session IS their platform session (ent#357),
         so for them the action signs out of Trinity and the label says so —
         never "Sign out" for something narrower, never "Leave" for something
         wider. Same element for both kinds (no v-if swap: the #2159 focus
         lesson); only the accessible name changes. -->
    <div class="shrink-0 border-t border-gray-200 dark:border-gray-800 p-3 flex items-center gap-2">
      <div class="min-w-0 flex-1">
        <div class="text-xs text-gray-400">{{ isPlatformSession ? 'Signed in to Trinity' : 'Signed in' }}</div>
        <div class="text-sm truncate" :title="clientEmail">{{ clientEmail }}</div>
      </div>
      <button
        type="button"
        class="shrink-0 p-2 rounded-lg text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-white dark:hover:bg-gray-900 transition"
        :title="signOutLabel"
        :aria-label="signOutLabel"
        @click="$emit('sign-out')"
      >
        <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" /></svg>
      </button>
    </div>
  </aside>
</template>

<script setup>
import { computed, ref } from 'vue'
import PortalAvatar from './PortalAvatar.vue'
import ChatRow from './PortalChatRow.vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import {
  groupThreadsByDate, partitionStarred, unreadByAgent, totalUnread, availabilityChip,
  asksByAgent, askBadgeTitle, agentRowTitle as buildAgentRowTitle,
  visibleAgentRows, AGENT_COLLAPSE_LIMIT,
  signOutLabelFor,
  searchAgents, sidebarSearchState, searchEmptyLines,
  agentResultsLabel, agentToggleLabel, showAgentToggle, SEARCH_PLACEHOLDER,
} from './portalUtils'

const props = defineProps({
  roster: { type: Array, default: () => [] },
  threads: { type: Array, default: () => [] },     // merged, agent-tagged, star/unread-tagged
  clientEmail: { type: String, default: '' },
  currentSessionId: { type: String, default: null },
  currentRoomId: { type: String, default: null },
  isPlatformSession: { type: Boolean, default: false },
  // #2159: distinguishes "still loading" from "loaded, and you have none".
  loadingRoster: { type: Boolean, default: false },
  search: { type: String, default: '' },
  searching: { type: Boolean, default: false },
  searchResults: { type: Array, default: () => [] },
})
const emit = defineEmits([
  'new-chat', 'new-chat-with-agent', 'open-agent', 'open-thread', 'toggle-star',
  'update:search', 'sign-out',
])

const isSearching = computed(() => (props.search || '').trim().length >= 2)

// #2258: the accessible name of the footer button, per principal kind.
const signOutLabel = computed(() => signOutLabelFor(props.isPlatformSession))

const split = computed(() => partitionStarred(props.threads))
const starred = computed(() => split.value.starred)
const grouped = computed(() => groupThreadsByDate(split.value.rest))

const waiting = computed(() => unreadByAgent(props.threads))
const waitingFor = (name) => waiting.value[name] || 0
const totalWaiting = computed(() => totalUnread(props.threads))
// ent#364: read from the store rather than taken as a prop, because the count and
// the two ask renderings must come from ONE list — a prop threaded from the view
// would be a second path to the same fact, free to disagree with it.
const asksStore = useClientPortalStore()
const askCount = computed(() => asksStore.askCount)
// #2424: the ask twin of `waiting`. Kept a separate map on purpose — see the
// brand-badge comment in the template.
const asksPerAgent = computed(() => asksByAgent(asksStore.openAsks))
const askCountFor = (name) => asksPerAgent.value[name] || 0

// A row key has to include the kind: thread ids and room ids are independent
// spaces, so two chats of different kinds could collide on a bare id.
const rowKey = (t) => `${t.is_room ? 'room' : 'thread'}:${t.id || t.session_id}`
const isActive = (t) => (t.is_room
  ? t.id === props.currentRoomId
  : (t.id || t.session_id) === props.currentSessionId)

// ent#360 AC #1: a roster row opens the agent's PAGE. It is a destination now —
// somewhere to see what it has been doing, what it is waiting on you for, and
// what it can do — so starting a chat is an explicit act taken there.
//
// This supersedes ent#359's interim behaviour (a row with unread opened the
// unread chat). That existed because a badge next to a control that opened a
// BLANK chat was a contradiction; the page resolves it properly, since its
// Overview lists the chats this agent belongs to, unread count included.
function onAgentClick(name) {
  emit('open-agent', name)
}
// #2159: the human-facing name, falling back to the slug when unset. NULL
// display_label means "render the slug" (ent#181), so the fallback lives here
// rather than being coalesced server-side — otherwise the two ends would
// disagree about what an unset label means.
const agentLabel = (a) => (a.display_label || '').trim() || a.name

// #2196: one rule, from portalUtils, shared with the agent page (and, next,
// the picker and the @-typeahead). `detailed` is the platform-session flag: an
// operator looking at their own fleet sees stopped-vs-no-container, an external
// client sees one label for both — the two differ only in whether the operator
// deleted or lost the agent, and neither is actionable for a client.
const chipFor = (a) => availabilityChip(a, { detailed: props.isPlatformSession })

// The state must be reachable without relying on colour — which is why #2424
// is an accessibility fix too: a blocked agent's title was the bare
// "Open ws-sage" while two asks waited on it.
function agentRowTitle(a) {
  const chip = chipFor(a)
  return buildAgentRowTitle({
    label: agentLabel(a),
    name: a.name,
    unread: waitingFor(a.name),
    askCount: askCountFor(a.name),
    chipTitle: chip ? chip.title : '',
  })
}

// #2159: a long fleet made the agents block the whole sidebar, pushing chats
// below the fold. Top 5, expandable in place.
//
// #2424: an agent with an open ask is never collapsed out. The slice was plain
// roster order, so the one row a person needed could be the hidden one — the
// reported case had it 11th of 12 while the header advertised its two asks. The
// rule is in portalUtils (`AGENT_COLLAPSE_LIMIT` now lives there too, one
// definition for the slice and the toggle).
const agentsExpanded = ref(false)

// ent#402: the AGENT half of the search. Kept in portalUtils because vitest has
// no mount harness, so a rule written here would be a rule no test can reach —
// and two of these rules are exactly the ones that look right and are not:
// `requireMentionable: false` (a sidebar row is not a mention, so `data.scout`
// must still be findable) and the #2424 window (an agent with an open ask is
// never collapsed out of its own result). Both live inside `searchAgents`.
const agentResults = computed(() => searchAgents(props.roster, props.search, {
  askCounts: asksPerAgent.value,
  expanded: agentsExpanded.value,
}))

// The section header, both empty lines and the hint read from ONE state value,
// so they cannot describe different situations in the same render.
const searchState = computed(() => sidebarSearchState({
  agentTotal: agentResults.value.total,
  chatCount: (props.searchResults || []).length,
  chatsSearching: props.searching,
  rosterLoading: props.loadingRoster && !props.roster.length,
}))
// `agentsEmpty` is passed explicitly rather than inferred from the state: the
// chat request's flag is set on every keystroke, so `searching` spans the whole
// time someone is typing, and the agent half — a filter over a roster already in
// hand — must still be able to say it matched nothing.
const emptyLines = computed(() => searchEmptyLines(searchState.value, props.search, {
  agentsEmpty: agentResults.value.total === 0,
}))

// ONE loop in the template feeds from this, so the row markup, its badges, its
// availability chip and its open path are inherited by search rather than
// copied into it — the only way the two modes cannot drift.
const shownAgents = computed(() => (isSearching.value
  ? agentResults.value.visible
  : visibleAgentRows(props.roster, { expanded: agentsExpanded.value, askCounts: asksPerAgent.value })))

// ent#186: history + search rows show the conversation's agent avatar instead of
// a bare color dot. The URL is resolved from the roster already loaded at sign-in
// (no per-row fetch); an unknown agent or one without a generated avatar falls
// through to PortalAvatar's initials + tint.
const avatarByAgent = computed(() =>
  Object.fromEntries((props.roster || []).map((a) => [a.name, a.avatar_url])),
)
const avatarFor = (name) => avatarByAgent.value[name] || null
</script>
