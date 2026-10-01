<template>
  <div>
    <div
      class="rounded-2xl rounded-bl-md bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-gray-100 px-3.5 py-3 text-sm leading-relaxed"
    >
      <PortalMarkdown :content="content" />
    </div>

    <!-- The action row. BENEATH the bubble rather than floating over its
         top-right corner (the Agent Detail chat's placement): a Workspace reply
         routinely opens with a code block, whose own Copy control sits exactly
         there, and on a one-line answer an overlay covers the answer. The row
         also gives the ent#366 thumbs somewhere to live beside it. -->
    <!-- #2580: `items-start`, not `items-center`. Copy and the two thumbs are
         all 22px boxes, so in the resting state the two are identical — but the
         slotted rating grows DOWNWARD when its comment box opens, and under
         `items-center` that re-centred Copy against an ~80px block, floating it
         into the middle of the expanded panel. Top-aligning pins the one
         baseline the issue asks for in BOTH states. The transient copy feedback
         is the one child that is not 22px, so it centres itself. -->
    <div class="mt-1.5 flex items-start gap-1">
      <button
        v-if="copyable"
        type="button"
        :class="ACTION_BUTTON"
        :title="feedback ? feedback.label : COPY_MESSAGE_ARIA"
        :aria-label="feedback ? feedback.label : COPY_MESSAGE_ARIA"
        @click="copyMessage"
      >
        <svg v-if="!copiedOk" class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" /></svg>
        <svg v-else class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" /></svg>
      </button>
      <!-- trinity-enterprise#738: Reply, when the host offers it — the chat on
           every persisted agent message, the Inbox pane in Copy's place. BEFORE
           the copy feedback and the slot, so neither the transient "Copied" nor
           the rating's comment box ever moves it. The glyph is the Inbox arrow's:
           one action, one icon, through both doors. 22px like its neighbours —
           the 44px touch floor is the row's, as one change (trinity#3056). -->
      <button
        v-if="replyLabel"
        type="button"
        :class="ACTION_BUTTON"
        :title="replyLabel"
        :aria-label="replyLabel"
        :disabled="replyDisabled"
        data-testid="portal-message-reply"
        @click="$emit('reply')"
      >
        <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 10h10a8 8 0 018 8v2M3 10l6 6m-6-6l6-6" /></svg>
      </button>
      <!-- An icon that changes shape cannot say WHY a copy failed, and "it
           silently did nothing" is the behaviour this replaces. The word is
           transient, so it is not standing noise on every message. -->
      <span
        v-if="copyable && feedback"
        class="text-[11px] self-center"
        :class="copiedOk
          ? 'text-status-success-700 dark:text-status-success-400'
          : 'text-status-danger-600 dark:text-status-danger-400'"
        aria-live="polite"
      >{{ feedback.label }}</span>
      <slot />
    </div>
  </div>
</template>

<script setup>
/**
 * An agent's message as it appears in a chat: the bubble, and the row of
 * actions under it (#2515).
 *
 * Deliberately thin. Everything about RENDERING the markdown — the v-html, the
 * stylesheet, the per-block copy — belongs to PortalMarkdown, so a surface that
 * shows agent markdown outside a conversation gets it without inheriting a
 * chat bubble. What is left here is the chat-specific part: the shape, and the
 * message-level Copy and Reply.
 */
import { ref, onBeforeUnmount } from 'vue'
import PortalMarkdown from './PortalMarkdown.vue'
import { copyText, copyFeedback, COPY_FEEDBACK_TTL_MS, COPY_MESSAGE_ARIA } from '@/utils/clipboard'

const props = defineProps({
  content: { type: String, default: '' },
  // ent#610 sign-off: the Inbox pane trades Copy for its reply arrow.
  copyable: { type: Boolean, default: true },
  // trinity-enterprise#738: the Reply action's accessible name, or '' for none.
  // The host words it ("…in the chat" from the Inbox) and owns what it does.
  replyLabel: { type: String, default: '' },
  // Disabled in place (no row reflow) — the chat sets it while a voice call
  // has the composer Reply would focus.
  replyDisabled: { type: Boolean, default: false },
})
defineEmits(['reply'])

// One recipe for the row's own buttons (#738). The ink is the thumbs' beside
// them (`PortalRating`): gray-500 light / gray-400 dark — Copy's former
// gray-400 in light was 2.54:1 on white, under the 3:1 an icon needs.
const ACTION_BUTTON = 'p-1 rounded-md text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800 focus:outline-none focus:ring-2 focus:ring-action-primary-500/40 dark:focus:ring-action-primary-400/40 transition-colors disabled:opacity-45 disabled:cursor-not-allowed'

const feedback = ref(null)
const copiedOk = ref(false)
let timer = null

async function copyMessage() {
  // The RAW markdown, not the rendered text: the fences are what make a pasted
  // answer readable again wherever it lands.
  const result = await copyText(props.content || '')
  feedback.value = copyFeedback(result)
  copiedOk.value = !!result.ok
  clearTimeout(timer)
  timer = setTimeout(() => {
    feedback.value = null
    copiedOk.value = false
  }, COPY_FEEDBACK_TTL_MS)
}

onBeforeUnmount(() => { clearTimeout(timer) })
</script>
