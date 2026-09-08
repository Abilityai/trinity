<template>
  <!-- ent#451: this user's chats with the agent, as tabs above the thread —
       most recent first, as many as the width fits, the rest under a counted
       "N more" that repacks as the rail (#492) or the window resizes. It IS
       the design system's OverflowTabs (ruling 2026-09-06: never a hand-rolled
       strip), so the fit rule, the More menu and its keyboard contract are
       inherited rather than re-implemented. Renders no chrome at all with
       nothing to list: an unsaved new chat is not a tab yet, and a strip with
       one phantom tab would claim a chat that does not exist. -->
  <!-- ent#534: while a voice call is on, the strip is visible but inert —
       switching chats would remount the conversation and drop the call. -->
  <div
    v-if="tabs.length"
    class="shrink-0 px-3 sm:px-4 bg-white dark:bg-gray-900"
    :class="disabled ? 'opacity-60 pointer-events-none' : ''"
    :aria-disabled="disabled ? 'true' : undefined"
    data-testid="portal-chat-tabs"
  >
    <OverflowTabs
      dense
      :tabs="tabs"
      :model-value="activeId || null"
      :more-label="moreTabsLabel"
      @update:model-value="onSelect"
    />
  </div>
</template>

<script setup>
import { computed } from 'vue'
import OverflowTabs from '@/components/OverflowTabs.vue'
import { agentChatTabs, moreTabsLabel } from './portalUtils'

const props = defineProps({
  threads: { type: Array, default: () => [] },
  agentName: { type: String, default: '' },
  activeId: { type: String, default: null },
  // ent#534: inert while a voice call is on.
  disabled: { type: Boolean, default: false },
})
const emit = defineEmits(['select'])

const tabs = computed(() => agentChatTabs(props.threads, props.agentName))

function onSelect(id) {
  if (props.disabled) return
  if (!id || id === props.activeId) return
  const tab = tabs.value.find((t) => t.id === id)
  if (tab) emit('select', tab.thread)
}
</script>
