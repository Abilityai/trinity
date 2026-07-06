<template>
  <!-- Slide-over chat drawer -->
  <div class="fixed inset-0 z-40 flex justify-end">
    <!-- Backdrop -->
    <div class="absolute inset-0 bg-black/30" @click="$emit('close')"></div>

    <div class="relative z-50 w-full max-w-md h-full bg-white dark:bg-gray-900 shadow-xl flex flex-col">
      <!-- Header -->
      <div class="flex items-center justify-between px-4 py-3 border-b border-gray-200 dark:border-gray-700">
        <div class="min-w-0">
          <div class="font-medium text-gray-900 dark:text-white truncate">{{ agent.name }}</div>
          <div class="text-xs text-gray-400 truncate"><span v-if="agent.owner">by {{ agent.owner }}</span></div>
        </div>
        <button class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 text-xl leading-none" @click="$emit('close')" aria-label="Close">×</button>
      </div>

      <!-- Messages -->
      <div ref="scrollEl" class="flex-1 overflow-y-auto px-4 py-4 space-y-3">
        <div v-if="messages.length === 0 && !sending" class="text-center text-sm text-gray-400 mt-10">
          Start chatting with <span class="font-medium">{{ agent.name }}</span>.
        </div>

        <div v-for="(m, i) in messages" :key="i" :class="m.role === 'user' ? 'flex justify-end' : 'flex justify-start'">
          <div
            v-if="m.role === 'user'"
            class="max-w-[80%] rounded-2xl rounded-br-sm bg-action-primary-600 text-white px-3 py-2 text-sm whitespace-pre-wrap"
          >{{ m.content }}</div>
          <div
            v-else
            class="max-w-[85%] rounded-2xl rounded-bl-sm bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-gray-100 px-3 py-2 text-sm prose-portal"
            v-html="render(m.content)"
          ></div>
        </div>

        <div v-if="sending" class="flex justify-start">
          <div class="rounded-2xl rounded-bl-sm bg-gray-100 dark:bg-gray-800 px-3 py-2">
            <span class="inline-flex gap-1">
              <span class="w-1.5 h-1.5 rounded-full bg-gray-400 animate-bounce" style="animation-delay:0ms"></span>
              <span class="w-1.5 h-1.5 rounded-full bg-gray-400 animate-bounce" style="animation-delay:150ms"></span>
              <span class="w-1.5 h-1.5 rounded-full bg-gray-400 animate-bounce" style="animation-delay:300ms"></span>
            </span>
          </div>
        </div>

        <div v-if="error" class="text-xs text-status-danger-600 dark:text-status-danger-400">{{ error }}</div>
      </div>

      <!-- Composer -->
      <div class="border-t border-gray-200 dark:border-gray-700 p-3">
        <form class="flex items-end gap-2" @submit.prevent="send">
          <textarea
            v-model="input"
            rows="1"
            placeholder="Message…"
            class="flex-1 resize-none rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-sm text-gray-900 dark:text-gray-100 px-3 py-2 focus:ring-action-primary-500 focus:border-action-primary-500 max-h-32"
            @keydown.enter.exact.prevent="send"
          ></textarea>
          <button
            type="submit"
            class="shrink-0 rounded-lg bg-action-primary-600 hover:bg-action-primary-700 text-white text-sm px-4 py-2 disabled:opacity-50"
            :disabled="sending || !input.trim()"
          >Send</button>
        </form>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, nextTick } from 'vue'
import { useAgentsStore } from '@/stores/agents'
import { renderMarkdown } from '@/utils/markdown'

const props = defineProps({
  agent: { type: Object, required: true },   // { name, owner }
})
defineEmits(['close'])

const agentsStore = useAgentsStore()
const messages = ref([])
const input = ref('')
const sending = ref(false)
const error = ref(null)
const scrollEl = ref(null)

const render = (c) => renderMarkdown(c || '')

async function scrollDown() {
  await nextTick()
  if (scrollEl.value) scrollEl.value.scrollTop = scrollEl.value.scrollHeight
}

async function send() {
  const text = input.value.trim()
  if (!text || sending.value) return
  error.value = null
  messages.value.push({ role: 'user', content: text })
  input.value = ''
  sending.value = true
  await scrollDown()
  try {
    const data = await agentsStore.sendChatMessage(props.agent.name, text)
    messages.value.push({ role: 'assistant', content: data.response || '(no response)' })
  } catch (err) {
    error.value = err.response?.data?.detail || 'Message failed. The agent may be stopped.'
  } finally {
    sending.value = false
    await scrollDown()
  }
}
</script>

<style scoped>
.prose-portal :deep(p) { margin: 0.25rem 0; }
.prose-portal :deep(pre) { overflow-x: auto; background: rgba(0,0,0,0.06); padding: 0.5rem; border-radius: 0.375rem; }
.prose-portal :deep(code) { font-size: 0.8em; }
.prose-portal :deep(ul) { list-style: disc; padding-left: 1.25rem; }
.prose-portal :deep(a) { text-decoration: underline; }
</style>
