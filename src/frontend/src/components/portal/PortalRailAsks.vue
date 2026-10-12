<!--
  The rail's Asks tab (trinity-enterprise#836): the agent's open asks, one line
  each, and a click opens that ask in the Inbox.

  A door, not a second answer surface. Since the 2026-09-30 ruling on ent#610
  an agent's asks live in the Inbox filtered to it, and a background ask shows
  ONLY there — so from the agent's page there was nothing to click. This tab
  lists what is waiting and leads to it; answering happens in the Inbox.

  Reads the ONE asks feed (`clientPortal.openAsks`) through the Inbox's own
  builder (`portalRailAsks.js` → `actionItems`), so the rows are exactly the
  Action tab narrowed to the participants — same items, same order — and the
  head is the waiting line every other door carries (`PortalAsksWaitingLine`:
  "2 asks waiting on you · Open in Inbox"). Rows are real links (keyboard
  reachable, announced by kind), in a room grouped by participant with absence
  visible (the rail's "Room" rule). The tab is never mounted empty: its
  registry entry has a presence rule, so with nothing waiting it is not there.
-->
<template>
  <div data-testid="portal-rail-asks">
    <PortalAsksWaitingLine :agent-names="participants" testid-prefix="portal-rail-asks" class="mb-2" />

    <template v-for="[agent, rows] in groups" :key="agent">
      <!-- Room: one rail, every participant gets its row — absence is visible. -->
      <div v-if="grouped" class="mt-3 mb-1.5 flex items-center gap-2 min-w-0" :data-testid="`portal-rail-asks-group-${agent}`">
        <PortalAvatar :name="agent" :size="18" />
        <span class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 truncate">{{ labelFor(agent) }}</span>
        <span v-if="!rows.length" class="ml-auto text-xs text-gray-500 dark:text-gray-400">nothing waiting</span>
      </div>

      <ul v-if="rows.length" class="space-y-1" :aria-label="grouped ? `Asks from ${labelFor(agent)}` : 'Asks waiting on you'">
        <li v-for="it in rows" :key="it.key">
          <router-link
            :to="askInboxRoute(it)"
            class="w-full flex items-center gap-2.5 rounded-lg px-2.5 py-2 min-w-0 transition hover:bg-gray-50 dark:hover:bg-gray-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:focus-visible:ring-action-primary-400/40"
            :title="it.title"
            :data-testid="`portal-rail-ask-${it.id}`"
            @click="$emit('open', it)"
          >
            <!-- Identity by SHAPE (principle 24): the kind's own outline, in
                 gray, as the Inbox row draws it (§3g A10); the kind is spoken. -->
            <svg class="w-4 h-4 shrink-0 text-gray-500 dark:text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" :d="askKindIcon(it.ask && it.ask.kind).path" /></svg>
            <span class="sr-only">{{ kindLabel(it) }}:</span>
            <span class="min-w-0 flex-1 text-sm truncate text-gray-900 dark:text-gray-100">{{ it.title }}</span>
            <!-- Principle 22: relative for recency, absolute + zone on hover. -->
            <span class="shrink-0 text-[12.5px] tabular-nums text-gray-500 dark:text-gray-400" :title="absolute(it.at)">{{ relative(it.at) }}</span>
          </router-link>
        </li>
      </ul>
    </template>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import PortalAvatar from './PortalAvatar.vue'
import PortalAsksWaitingLine from './PortalAsksWaitingLine.vue'
import { railAskItems, askInboxRoute } from './portalRailAsks'
import { agentLabels } from './portalInbox'
import { askKindIcon } from './portalAskUrgency'
import { relativeTime } from './portalUtils'
import { queueTypeLabel } from '@/utils/operatorQueue'
import { formatLocalDateTime } from '@/utils/timestamps'

const props = defineProps({
  participants: { type: Array, default: () => [] },
})
defineEmits(['open'])

const portal = useClientPortalStore()

const items = computed(() => railAskItems(portal.openAsks, props.participants))
const grouped = computed(() => props.participants.length > 1)
// A 1:1 is one unlabelled group; a room is one group per participant, in
// participant order, each present even with nothing in it.
const groups = computed(() => {
  if (!grouped.value) return [[props.participants[0] || '', items.value]]
  return props.participants.map((agent) => [agent, items.value.filter((it) => it.agent_name === agent)])
})

const labels = computed(() => agentLabels(portal.agents))
const labelFor = (name) => labels.value[name] || name
const kindLabel = (it) => queueTypeLabel(it.ask && it.ask.kind) || 'Ask'
const relative = (iso) => relativeTime(iso)

let zone = ''
try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone || '' } catch { zone = '' }
const absolute = (iso) => (iso ? `${formatLocalDateTime(iso)}${zone ? ` (${zone})` : ''}` : undefined)
</script>
