<template>
  <!-- trinity-enterprise#610 (the 09-30 ruling, item 2): the agent's asks home
       is the Inbox, filtered to it, on every door — so the door says so in ONE
       line while something waits. Work (platform) and Info (every principal: a
       client has no Work tab) mount this same line, so both audiences get the
       same answer. Counted from `openAsks`, the feed the sidebar marks and the
       pinned Inbox row count. -->
  <p v-if="waiting" class="min-w-0 truncate text-xs" :data-testid="`${testidPrefix}-waiting-line`">
    {{ waiting.label }} ·
    <router-link :to="waiting.to" :class="INBOX_LINK" :data-testid="`${testidPrefix}-open-inbox`">Open in Inbox</router-link>
  </p>
</template>

<script setup>
import { computed } from 'vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import { asksHomeRoute, asksWaitingLabel } from './portalUtils'

const props = defineProps({
  agentNames: { type: Array, default: () => [] },
  testidPrefix: { type: String, default: 'portal-work' },
})

const portal = useClientPortalStore()

// Narrowed to the agent when only one of them is waiting on you.
const waiting = computed(() => {
  const names = new Set(props.agentNames)
  const asks = (portal.openAsks || []).filter((a) => names.has(a.agent_name))
  if (!asks.length) return null
  const agents = new Set(asks.map((a) => a.agent_name))
  return {
    label: asksWaitingLabel(asks.length),
    to: asksHomeRoute(agents.size === 1 ? asks[0].agent_name : null),
  }
})

// A 44px target on a touch screen and the design-system ring (A2 round 1's rule;
// round 2: keyed on the pointer, not the width — a tablet is touch too).
const INBOX_LINK = 'inline-flex items-center max-sm:min-h-11 [@media(pointer:coarse)]:min-h-11 text-action-primary-600 dark:text-action-primary-400 hover:underline rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:focus-visible:ring-action-primary-400/40'
</script>
