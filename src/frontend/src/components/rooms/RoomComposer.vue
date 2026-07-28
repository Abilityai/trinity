<template>
  <div class="shrink-0 border-t border-gray-200 dark:border-gray-800 p-3">
    <!-- closed room: read-only with the reason -->
    <div v-if="closed" class="text-center text-xs text-gray-400 py-3">
      This session is closed<span v-if="stopReason"> — {{ closeReasonLabel }}</span>. The transcript stays readable.
    </div>

    <div v-else class="relative">
      <!-- @mention autocomplete -->
      <div
        v-if="mentionOpen && mentionMatches.length"
        class="absolute bottom-full mb-1 left-0 w-56 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 shadow-lg overflow-hidden z-10"
      >
        <button
          v-for="(p, i) in mentionMatches"
          :key="p.identity"
          class="w-full flex items-center gap-2 px-3 py-1.5 text-left text-sm"
          :class="i === mentionIndex ? 'bg-action-primary-50 dark:bg-action-primary-900/30' : 'hover:bg-gray-50 dark:hover:bg-gray-800'"
          @mousedown.prevent="applyMention(p)"
        >
          <PortalAvatar :name="p.identity" :avatar-url="avatarByName[p.identity] || null" :size="20" />
          <span class="truncate">{{ p.identity }}</span>
          <span v-if="p.role !== 'member'" class="ml-auto text-[10px] text-gray-400">{{ p.role }}</span>
        </button>
      </div>

      <textarea
        ref="ta"
        v-model="text"
        rows="1"
        placeholder="Message the room…  @mention an agent to wake it"
        class="w-full resize-none rounded-xl border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 placeholder:text-gray-400 dark:placeholder:text-gray-500 text-sm px-3 py-2.5 pr-12 focus:ring-2 focus:ring-action-primary-500/40 focus:border-action-primary-500 focus:outline-none max-h-40"
        @input="onInput"
        @keydown="onKeydown"
      ></textarea>
      <button
        class="absolute right-2 bottom-2 p-1.5 rounded-lg text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-40 disabled:cursor-not-allowed transition"
        :disabled="!text.trim() || posting"
        @click="send"
        title="Send (Enter)"
      >
        <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 12h14M12 5l7 7-7 7" /></svg>
      </button>
    </div>
    <p v-if="!closed" class="mt-1.5 px-1 text-[11px] text-gray-400">
      <span class="font-mono">@mention</span> wakes an agent — plain messages just join the transcript.
    </p>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import PortalAvatar from '../portal/PortalAvatar.vue'

const props = defineProps({
  participants: { type: Array, default: () => [] },
  avatarByName: { type: Object, default: () => ({}) },
  posting: { type: Boolean, default: false },
  closed: { type: Boolean, default: false },
  stopReason: { type: String, default: null },
})
const emit = defineEmits(['send'])

const ta = ref(null)
const text = ref('')
const mentionOpen = ref(false)
const mentionQuery = ref('')
const mentionIndex = ref(0)
const mentionStart = ref(-1)

const _REASONS = {
  user_closed: 'closed by an operator',
  max_messages: 'reached its message budget',
  max_cost: 'reached its cost budget',
  expired: 'expired',
}
const closeReasonLabel = computed(() => _REASONS[props.stopReason] || 'ended')

const agentParticipants = computed(() =>
  props.participants.filter((p) => p.kind === 'agent' && !p.left_at)
)
const mentionMatches = computed(() => {
  const q = mentionQuery.value.toLowerCase()
  return agentParticipants.value.filter((p) => p.identity.toLowerCase().startsWith(q)).slice(0, 6)
})

function onInput(e) {
  autosize(e.target)
  const caret = e.target.selectionStart
  const upto = text.value.slice(0, caret)
  const m = upto.match(/@([A-Za-z0-9_-]*)$/)
  if (m) {
    mentionOpen.value = true
    mentionQuery.value = m[1]
    mentionStart.value = caret - m[0].length
    mentionIndex.value = 0
  } else {
    mentionOpen.value = false
  }
}

function applyMention(p) {
  const before = text.value.slice(0, mentionStart.value)
  const after = text.value.slice(mentionStart.value).replace(/@([A-Za-z0-9_-]*)/, '')
  text.value = `${before}@${p.identity} ${after}`.replace(/\s+$/, ' ')
  mentionOpen.value = false
  ta.value?.focus()
}

function onKeydown(e) {
  if (mentionOpen.value && mentionMatches.value.length) {
    if (e.key === 'ArrowDown') { e.preventDefault(); mentionIndex.value = (mentionIndex.value + 1) % mentionMatches.value.length; return }
    if (e.key === 'ArrowUp') { e.preventDefault(); mentionIndex.value = (mentionIndex.value - 1 + mentionMatches.value.length) % mentionMatches.value.length; return }
    if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); applyMention(mentionMatches.value[mentionIndex.value]); return }
    if (e.key === 'Escape') { mentionOpen.value = false; return }
  }
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
}

function send() {
  const v = text.value.trim()
  if (!v || props.posting) return
  emit('send', v)
  text.value = ''
  if (ta.value) ta.value.style.height = 'auto'
}

function autosize(el) {
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 160) + 'px'
}
</script>
