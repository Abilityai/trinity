<template>
  <!-- trinity-enterprise#610 (the 09-30 ruling, item 2): the agent's asks home
       is the Inbox, filtered to it, on every door — so the door says so in ONE
       line while something waits. Work (platform) and Info (every principal: a
       client has no Work tab) mount this same line, so both audiences get the
       same answer. Counted from `openAsks`, the feed the sidebar marks and the
       pinned Inbox row count. -->
  <p v-if="waiting" class="min-w-0 truncate text-xs" :data-testid="`${testidPrefix}-waiting-line`">
    {{ waiting.label }} ·
    <router-link :to="waiting.to" :class="INBOX_LINK_CLASS" :data-testid="`${testidPrefix}-open-inbox`">Open in Inbox</router-link>
  </p>
</template>

<script setup>
import { computed } from 'vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import { asksHomeRoute, asksWaitingLabel, INBOX_LINK_CLASS } from './portalUtils'

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
</script>
