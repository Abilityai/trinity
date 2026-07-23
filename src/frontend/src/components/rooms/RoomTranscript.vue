<template>
  <div ref="scrollEl" class="flex-1 min-h-0 overflow-y-auto px-4 py-4 space-y-4">
    <div v-if="!messages.length" class="h-full flex items-center justify-center text-sm text-gray-400">
      No messages yet — say something, and <span class="font-mono mx-1">@mention</span> an agent to wake it.
    </div>

    <template v-for="m in messages" :key="m.id">
      <!-- system event line -->
      <div v-if="m.kind === 'system'" class="flex justify-center">
        <span class="text-[11px] text-gray-400 bg-gray-100 dark:bg-gray-800/60 rounded-full px-3 py-1">
          {{ m.content }}
        </span>
      </div>

      <!-- a message -->
      <div v-else class="flex gap-3" :class="{ 'opacity-60': m._optimistic }">
        <PortalAvatar :name="senderLabel(m)" :avatar-url="avatarFor(m)" :size="30" class="mt-0.5" />
        <div class="min-w-0 flex-1">
          <div class="flex items-center gap-2 mb-0.5">
            <span class="text-sm font-medium text-gray-800 dark:text-gray-100">{{ senderLabel(m) }}</span>
            <span
              class="text-[10px] font-semibold px-1.5 py-0.5 rounded"
              :class="m.sender_kind === 'agent'
                ? 'bg-action-primary-100 text-action-primary-700 dark:bg-action-primary-900 dark:text-action-primary-200'
                : 'bg-gray-200 text-gray-600 dark:bg-gray-700 dark:text-gray-300'"
            >{{ m.sender_kind === 'agent' ? 'Agent' : 'You' }}</span>
            <span class="text-[11px] text-gray-400">{{ time(m.created_at) }}</span>
          </div>

          <!-- bubble -->
          <div
            class="inline-block max-w-full rounded-2xl px-3.5 py-2 text-sm leading-relaxed break-words"
            :class="m.sender_kind === 'agent'
              ? 'bg-gray-100 dark:bg-gray-800 text-gray-800 dark:text-gray-100'
              : 'bg-action-primary-600 text-white'"
            v-html="renderWithMentions(m)"
          ></div>

          <!-- per-agent metadata line -->
          <div v-if="m.execution_id" class="mt-1 flex items-center gap-3 text-[11px] text-gray-400">
            <span v-if="cost(m) != null">${{ cost(m).toFixed(4) }}</span>
            <span class="font-mono opacity-70">exec {{ m.execution_id.slice(0, 8) }}</span>
          </div>
        </div>
      </div>
    </template>

    <!-- working indicator (typing dots) -->
    <div v-for="ident in workingAgents" :key="'working-' + ident" class="flex gap-3">
      <PortalAvatar :name="ident" :avatar-url="avatarByName[ident] || null" :size="30" class="mt-0.5" />
      <div>
        <div class="text-sm font-medium text-gray-800 dark:text-gray-100 mb-0.5">{{ ident }}</div>
        <div class="inline-flex items-center gap-1 rounded-2xl bg-gray-100 dark:bg-gray-800 px-4 py-3">
          <span class="w-1.5 h-1.5 rounded-full bg-gray-400 animate-bounce" style="animation-delay:0ms"></span>
          <span class="w-1.5 h-1.5 rounded-full bg-gray-400 animate-bounce" style="animation-delay:150ms"></span>
          <span class="w-1.5 h-1.5 rounded-full bg-gray-400 animate-bounce" style="animation-delay:300ms"></span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, nextTick, onMounted } from 'vue'
import PortalAvatar from '../portal/PortalAvatar.vue'
import { renderMarkdown } from '../../utils/markdown'

const props = defineProps({
  messages: { type: Array, default: () => [] },
  participants: { type: Array, default: () => [] },
  workingState: { type: Object, default: () => ({}) },
  executionCosts: { type: Object, default: () => ({}) }, // execution_id -> cost (optional)
  avatarByName: { type: Object, default: () => ({}) },   // agent name -> avatar_url
})

const scrollEl = ref(null)

const workingAgents = computed(() =>
  Object.entries(props.workingState).filter(([, s]) => s === 'working').map(([n]) => n)
)

function senderLabel(m) {
  if (m.sender_kind === 'user') return m.sender_identity === 'You' ? 'You' : m.sender_identity
  return m.sender_identity || 'Agent'
}
function avatarFor(m) {
  return m.sender_kind === 'agent' ? (props.avatarByName[m.sender_identity] || null) : null
}
function cost(m) {
  return props.executionCosts[m.execution_id] ?? null
}
function time(iso) {
  try { return new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' }) }
  catch { return '' }
}

// Highlight @mentions of real participants; everything else is DOMPurify'd markdown.
const agentNames = computed(() =>
  new Set(props.participants.filter((p) => p.kind === 'agent').map((p) => p.identity))
)
function renderWithMentions(m) {
  const html = renderMarkdown(m.content || '')
  // wrap @name tokens that match a participant — applied to the already-sanitized
  // markdown output; the replacement injects only a span with static classes.
  return html.replace(/@([A-Za-z0-9][A-Za-z0-9_-]{0,99})/g, (full, name) =>
    agentNames.value.has(name)
      ? `<span class="font-semibold ${m.sender_kind === 'user' ? 'text-white underline' : 'text-action-primary-600 dark:text-action-primary-400'}">${full}</span>`
      : full
  )
}

function scrollToBottom() {
  nextTick(() => { if (scrollEl.value) scrollEl.value.scrollTop = scrollEl.value.scrollHeight })
}
onMounted(scrollToBottom)
watch(() => props.messages.length, scrollToBottom)
watch(() => workingAgents.value.length, scrollToBottom)
</script>
