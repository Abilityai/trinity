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
       it while it is the active chat is a no-op: there is nothing to open.

       #2579 also asks the primitive for `fixed-width`: these labels are user
       and model text, so without it one long title stretches its tab and
       pushes every sibling under "N more".

       trinity-enterprise#657: a tab whose chat holds unsent text carries the
       primitive's `hasDraft` mark, and an unsaved chat with a draft is listed
       as the provisional tab even while another chat is open — selecting it
       is the one way back to those words (`new-chat`). -->
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
      :model-value="current"
      :more-label="moreTabsLabel"
      @update:model-value="onSelect"
      @close="onClose"
    />
    <!-- ent#841: a close that the server refused. The tab is back by then (the
         shell reverts it), and this says why, next to the control that failed. -->
    <InlineError
      v-if="closeError"
      class="my-1"
      :message="closeError"
      data-testid="chat-tab-close-error"
      @dismiss="emit('dismiss-close-error')"
    />
  </div>
</template>

<script setup>
import { computed } from 'vue'
import OverflowTabs from '@/components/OverflowTabs.vue'
import InlineError from '@/components/InlineError.vue'
import { agentChatTabs, moreTabsLabel, NEW_CHAT_TAB_ID } from './portalUtils'
import { keyHint, keyShortcutsFor, hostPlatform } from './portalKeymap'

const props = defineProps({
  threads: { type: Array, default: () => [] },
  agentName: { type: String, default: '' },
  activeId: { type: String, default: null },
  // ent#534: inert while a voice call is on.
  disabled: { type: Boolean, default: false },
  // #2579: the conversation on screen is an unsaved fresh start (or a thread
  // adopted a moment ago that the list has not caught up with).
  draft: { type: Boolean, default: false },
  // trinity-enterprise#657: the drafts store's key set (`thread:<id>` /
  // `new:<agent>`), from the conversation that owns the store binding.
  draftKeys: { type: Object, default: null },
  // ent#841: a reply is in flight in the ACTIVE chat. Its × is inert and says
  // why, rather than archiving a chat out from under a turn the person is
  // watching (the reply would still land — nothing is lost — but the chat would
  // vanish mid-answer). Other chats' turns are safe to close: the reply lands
  // in the archived chat and is there when it is reopened.
  busy: { type: Boolean, default: false },
  // ent#841: why the last close failed, from the shell; '' when none.
  closeError: { type: String, default: '' },
})
const emit = defineEmits(['select', 'new-chat', 'close', 'dismiss-close-error'])

// ent#621: the chat keys are discoverable from the strip they walk. The hint
// rides `signalTitle` (the title channel `OverflowTabs` already renders) and
// the ARIA spelling rides `ariaKeyshortcuts` — both DERIVED from the key map,
// so a chord that changes there changes here in the same commit.
const CHAT_KEYS = ['chat-prev', 'chat-next']
const KEY_PLATFORM = hostPlatform()
const CHAT_KEY_HINT = keyHint(CHAT_KEYS, KEY_PLATFORM)
const CHAT_KEY_SHORTCUTS = keyShortcutsFor(CHAT_KEYS, KEY_PLATFORM)

const tabs = computed(() =>
  agentChatTabs(props.threads, props.agentName, { activeId: props.activeId, draft: props.draft, draftKeys: props.draftKeys })
    .map((t) => ({
      ...t,
      signalTitle: `${t.signalTitle || t.label} · ${CHAT_KEY_HINT} switch chat`,
      ariaKeyshortcuts: CHAT_KEY_SHORTCUTS,
      ...(t.closable ? closeFields(t) : {}),
    }))
)

// ent#841: the × names its verb — it archives, and the label says so.
function closeFields(t) {
  const busyHere = props.busy && t.id === props.activeId
  return {
    closeLabel: 'Archive chat',
    closeDisabled: busyHere,
    closeTitle: busyHere ? 'Wait for the reply to finish, then archive this chat' : 'Archive chat',
  }
}

function onClose(id) {
  if (props.disabled) return
  const tab = tabs.value.find((t) => t.id === id)
  if (!tab?.thread || !tab.closable || tab.closeDisabled) return
  emit('close', tab.thread)
}

// The strip's EFFECTIVE selection — the same expression `:model-value` binds.
// Comparing against `activeId` alone is wrong while the active chat is
// unsaved: `activeId` is null then and the provisional tab's id is not, so a
// click on the tab the person is already in would read as a switch.
const current = computed(() => props.activeId || (props.draft ? NEW_CHAT_TAB_ID : null))

function onSelect(id) {
  if (props.disabled) return
  if (!id || id === current.value) return
  const tab = tabs.value.find((t) => t.id === id)
  // #2579: the provisional tab has no thread. Emitting it would hand the shell
  // a null to `openThread`, which reads `t.is_room` off it and throws.
  // trinity-enterprise#657: when it is NOT the active chat it is the listed
  // unsaved-chat draft, and selecting it opens a new chat with this agent —
  // which restores the draft.
  if (!tab?.thread) {
    if (tab?.provisional) emit('new-chat')
    return
  }
  emit('select', tab.thread)
}
</script>
