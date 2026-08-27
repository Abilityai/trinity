<template>
  <!-- ent#366: one click, on the thing being judged. The rating is a PLATFORM
       primitive, not a skill the agent runs — a user's score is the one score
       that must not pass through the thing being scored, which is why this
       posts to the portal ratings route and never into the conversation. -->
  <div class="mt-1.5 flex items-center gap-1 text-gray-400 dark:text-gray-500">
    <button
      v-for="choice in ['up', 'down']"
      :key="choice"
      type="button"
      class="p-1 rounded-md transition hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-50"
      :class="rating === choice
        ? (choice === 'up'
          ? 'text-status-success-600 dark:text-status-success-400'
          : 'text-status-warning-600 dark:text-status-warning-400')
        : 'hover:text-gray-700 dark:hover:text-gray-300'"
      :title="labels[choice]"
      :aria-label="labels[choice]"
      :aria-pressed="rating === choice"
      :disabled="sending"
      @click="choose(choice)"
    >
      <svg v-if="choice === 'up'" class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14 10h4.764a2 2 0 011.789 2.894l-3.5 7A2 2 0 0115.263 21h-4.017c-.163 0-.326-.02-.485-.06L7 20m7-10V5a2 2 0 00-2-2h-.095c-.5 0-.905.405-.905.905 0 .714-.211 1.412-.608 2.006L7 11v9m7-10h-2M7 20H5a2 2 0 01-2-2v-6a2 2 0 012-2h2.5" /></svg>
      <svg v-else class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 14H5.236a2 2 0 01-1.789-2.894l3.5-7A2 2 0 018.736 3h4.018a2 2 0 01.485.06l3.76.94m-7 10v5a2 2 0 002 2h.096c.5 0 .905-.405.905-.905 0-.714.211-1.412.608-2.006L17 13V4m-7 10h2m5-10h2a2 2 0 012 2v6a2 2 0 01-2 2h-2.5" /></svg>
    </button>

    <!-- The acknowledgement is the honest one of two: "passed on" only when the
         agent actually has the capture-feedback skill, otherwise "recorded",
         because the comment IS durable either way (AC #6). -->
    <span v-if="acknowledgement" class="ml-1 text-[11px] text-gray-500 dark:text-gray-400">{{ acknowledgement }}</span>
    <span v-if="error" class="ml-1 text-[11px] text-status-danger-600 dark:text-status-danger-400">{{ error }}</span>
  </div>

  <!-- The box opens on a negative rating only. Optional by construction: the
       rating is already recorded by the time this renders, so closing it
       without typing loses nothing. -->
  <form
    v-if="commentOpen"
    class="mt-2 flex items-start gap-2"
    @submit.prevent="submitComment"
  >
    <textarea
      ref="commentBox"
      v-model="comment"
      rows="2"
      maxlength="2000"
      :placeholder="`What were you looking for instead? (optional)`"
      class="flex-1 resize-none rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-800 text-xs text-gray-900 dark:text-gray-100 px-2.5 py-2 focus:ring-2 focus:ring-action-primary-500/40 focus:border-action-primary-500 focus:outline-none"
    ></textarea>
    <div class="flex flex-col gap-1">
      <button
        type="submit"
        class="rounded-lg bg-action-primary-600 hover:bg-action-primary-700 px-2.5 py-1 text-[11px] text-white disabled:opacity-40"
        :disabled="sending || !comment.trim()"
      >Send</button>
      <button
        type="button"
        class="rounded-lg px-2.5 py-1 text-[11px] text-gray-500 dark:text-gray-400 hover:underline"
        @click="commentOpen = false"
      >Close</button>
    </div>
  </form>
</template>

<script setup>
import { ref, computed, nextTick, watch } from 'vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import {
  ratingLabels,
  nextRating,
  shouldPromptForComment,
  feedbackAcknowledgement,
} from './portalUtils'

const props = defineProps({
  agentName: { type: String, required: true },
  targetKind: { type: String, required: true },   // 'message' | 'deliverable'
  targetId: { type: String, required: true },
  // The caller's own existing rating, from history. Never anyone else's.
  initialRating: { type: String, default: null },
})

const store = useClientPortalStore()
const rating = ref(props.initialRating)
const sending = ref(false)
const commentOpen = ref(false)
const comment = ref('')
const acknowledgement = ref('')
const error = ref('')
const commentBox = ref(null)

const labels = computed(() => ratingLabels(props.targetKind))

// A new target means a new rating: without this, a re-keyed row would carry the
// previous message's thumb.
watch(() => props.targetId, () => {
  rating.value = props.initialRating
  commentOpen.value = false
  comment.value = ''
  acknowledgement.value = ''
  error.value = ''
})

async function send(value, text) {
  sending.value = true
  error.value = ''
  try {
    const res = await store.submitRating(props.agentName, {
      target_kind: props.targetKind,
      target_id: props.targetId,
      rating: value,
      comment: text || undefined,
    })
    return res
  } catch (e) {
    // A failed verb gets a home next to its control, never a console line.
    error.value = e?.response?.data?.detail || "Couldn't save that."
    return null
  } finally {
    sending.value = false
  }
}

async function choose(choice) {
  const next = nextRating(rating.value, choice)
  if (next === null) return   // clicking your existing rating is a no-op
  const res = await send(next)
  if (!res) return
  rating.value = next
  if (shouldPromptForComment(next)) {
    commentOpen.value = true
    acknowledgement.value = ''
    await nextTick()
    commentBox.value?.focus()
  } else {
    commentOpen.value = false
    acknowledgement.value = ''
  }
}

async function submitComment() {
  const text = comment.value.trim()
  if (!text) return
  const res = await send('down', text)
  if (!res) return
  commentOpen.value = false
  comment.value = ''
  acknowledgement.value = feedbackAcknowledgement(res.capture_feedback)
}
</script>
