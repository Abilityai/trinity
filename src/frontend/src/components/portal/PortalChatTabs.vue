<template>
  <!-- ent#451: this user's chats with the agent, as tabs above the thread —
       most recent first, as many as the width fits, the rest under a counted
       "N more" that repacks as the rail (#492) or the window resizes. It IS
       the design system's OverflowTabs (ruling 2026-09-06: never a hand-rolled
       strip), so the fit rule, the More menu and its keyboard contract are
       inherited rather than re-implemented.

       #2579 reverses one half of that ruling, on purpose. It said a strip with
       one phantom tab "would claim a chat that does not exist" — true of the
       THREAD, and nothing here creates one. But pressing New chat and seeing
       nothing at all change is the defect the operator reported, so an unsaved
       ACTIVE chat now draws a provisional "New chat" tab with no thread behind
       it. It is keyed off `draft` — the shell's explicit fresh-start intent —
       never off "the active id is not in the list", or a cold deep link to a
       thread the batch has not listed yet would wear the same label. Selecting
       it is a no-op: there is nothing to open.

       #2579 also asks the primitive for `fixed-width`: these labels are user
       and model text, so without it one long title stretches its tab and
       pushes every sibling under "N more". -->
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
      fixed-width
      :tabs="tabs"
      :model-value="activeId || (draft ? NEW_CHAT_TAB_ID : null)"
      :more-label="moreTabsLabel"
      @update:model-value="onSelect"
    />
  </div>
</template>

<script setup>
import { computed } from 'vue'
import OverflowTabs from '@/components/OverflowTabs.vue'
import { agentChatTabs, moreTabsLabel, NEW_CHAT_TAB_ID } from './portalUtils'

const props = defineProps({
  threads: { type: Array, default: () => [] },
  agentName: { type: String, default: '' },
  activeId: { type: String, default: null },
  // ent#534: inert while a voice call is on.
  disabled: { type: Boolean, default: false },
  // #2579: the conversation on screen is an unsaved fresh start (or a thread
  // adopted a moment ago that the list has not caught up with).
  draft: { type: Boolean, default: false },
})
const emit = defineEmits(['select'])

const tabs = computed(() =>
  agentChatTabs(props.threads, props.agentName, { activeId: props.activeId, draft: props.draft })
)

function onSelect(id) {
  if (props.disabled) return
  if (!id || id === props.activeId) return
  const tab = tabs.value.find((t) => t.id === id)
  // #2579: the provisional tab has no thread. Emitting it would hand the shell
  // a null to `openThread`, which reads `t.is_room` off it and throws.
  if (!tab?.thread) return
  emit('select', tab.thread)
}
</script>
