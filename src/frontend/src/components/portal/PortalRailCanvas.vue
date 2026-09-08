<!--
  The rail's Canvas tab body (trinity-enterprise#475) — the participating
  agents' canvases (ent#438) beside the conversation they come from.

  ONE rendering layer: this is `CanvasPanel` — the same component, fed by the
  same client-portal store the Workspace agent page uses — per participating
  agent. Selector, the always-shown timestamp, the "may be out of date" mark,
  and the blocks are all its; nothing here re-renders a canvas. The audience
  narrowing is the server's and unchanged (`audience='roster'` for every
  Workspace principal, the ent#438 ruling), so an operator-only canvas never
  reaches this body.

  Reads `stores/portalRailFeeds.js` for the metadata list; blocks are fetched
  per canvas on open (`store.fetchAgentCanvas`), the existing split. Empty:
  the registry's copy + "Ask for a canvas", which the shell turns into a
  composer PREFILL — never a send. A room shows a section per participant,
  absence visible ("Nothing published yet").
-->
<template>
  <div class="space-y-5" data-testid="portal-rail-canvas">
    <PortalSkeleton v-if="view.state === 'loading'" variant="rail" />

    <LoadFailed
      v-else-if="view.state === 'failed'"
      title="Couldn't load canvases"
      :message="feeds.error || 'The canvases for this chat could not be read.'"
      :retrying="feeds.loading"
      @retry="feeds.refresh()"
    />

    <template v-else>
      <InlineError v-if="view.stale" :message="feeds.error" @dismiss="feeds.error = null" />

      <!-- Empty state that teaches (principle 16): the next action is to ASK. -->
      <div v-if="view.state === 'empty'" class="py-8 text-center" data-testid="portal-rail-canvas-empty">
        <p class="text-sm font-semibold">{{ empty.title }}</p>
        <p class="mt-1.5 text-xs text-gray-500 dark:text-gray-400 max-w-[36ch] mx-auto">{{ empty.body }}</p>
        <BaseButton size="sm" variant="secondary" class="mt-4" data-testid="portal-rail-canvas-ask" @click="$emit('ask-canvas')">
          {{ empty.action }}
        </BaseButton>
      </div>

      <template v-else>
        <section v-for="agent in participants" :key="agent" class="space-y-3" :data-testid="`portal-rail-canvas-${agent}`">
          <div v-if="participants.length > 1" class="flex items-center gap-2 min-w-0">
            <PortalAvatar :name="agent" :size="18" />
            <span class="text-[11px] font-semibold uppercase tracking-wide text-gray-400 truncate">{{ agent }}</span>
            <span class="ml-auto text-xs text-gray-400">{{ countLabel(agent) }}</span>
          </div>
          <CanvasPanel
            v-if="rows(agent).length"
            :canvases="rows(agent)"
            :fetch-detail="(id) => portal.fetchAgentCanvas(agent, id)"
            viewer="client"
          />
          <p v-else-if="participants.length > 1" class="text-xs text-gray-400">Nothing published yet.</p>
        </section>
      </template>
    </template>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { usePortalRailFeedsStore } from '@/stores/portalRailFeeds'
import { useClientPortalStore } from '@/stores/clientPortal'
import BaseButton from '@/components/base/BaseButton.vue'
import LoadFailed from '@/components/LoadFailed.vue'
import InlineError from '@/components/InlineError.vue'
import CanvasPanel from '@/components/canvas/CanvasPanel.vue'
import PortalAvatar from './PortalAvatar.vue'
import PortalSkeleton from './PortalSkeleton.vue'
import { feedView, railEmptyCopy } from './portalRail'

const props = defineProps({
  participants: { type: Array, default: () => [] },
  // The registry entry (`RAIL_TABS` canvas), for the empty copy.
  tab: { type: Object, default: null },
})
defineEmits(['ask-canvas'])

const feeds = usePortalRailFeedsStore()
const portal = useClientPortalStore()

const participants = computed(() => props.participants)
const view = computed(() => feedView({
  participants: participants.value,
  hasLoaded: feeds.hasLoaded,
  error: feeds.error,
  count: feeds.canvasCount,
}))
const empty = computed(() => railEmptyCopy(props.tab, participants.value))

function rows(agent) { return feeds.canvases[agent] || [] }
function countLabel(agent) {
  const n = rows(agent).length
  return n ? `${n} ${n === 1 ? 'canvas' : 'canvases'}` : 'nothing published'
}
</script>
