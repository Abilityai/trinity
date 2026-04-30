<template>
  <!-- User message (plain text) -->
  <div
    v-if="role === 'user'"
    class="max-w-[85%] min-w-0 ml-auto"
  >
    <div class="rounded-xl px-4 py-3 bg-indigo-600 text-white overflow-hidden">
      <div v-if="source === 'voice'" class="flex items-center gap-1.5 mb-1 opacity-75">
        <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4M12 15a3 3 0 003-3V5a3 3 0 00-6 0v7a3 3 0 003 3z" /></svg>
        <span class="text-[10px] uppercase tracking-wide">Voice</span>
      </div>
      <p class="whitespace-pre-wrap break-words">{{ content }}</p>
    </div>
    <p v-if="formattedTime" class="text-xs text-gray-400 dark:text-gray-500 mt-1 text-right">{{ formattedTime }}</p>
  </div>
  <!-- Self-task result message (SELF-EXEC-001) - collapsible by default -->
  <div
    v-else-if="source === 'self_task'"
    class="max-w-[85%] min-w-0 group relative"
  >
    <button
      type="button"
      class="absolute top-2 right-2 z-10 p-1.5 rounded-md bg-white/80 dark:bg-gray-700/80 text-gray-500 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white hover:bg-white dark:hover:bg-gray-700 shadow-sm opacity-0 group-hover:opacity-100 focus:opacity-100 transition-opacity"
      :title="copied ? 'Copied!' : 'Copy message'"
      :aria-label="copied ? 'Copied' : 'Copy message'"
      @click.stop="copyContent"
    >
      <svg v-if="!copied" class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" /></svg>
      <svg v-else class="w-4 h-4 text-status-success-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" /></svg>
    </button>
    <div class="rounded-xl px-4 py-3 bg-accent-purple-50 dark:bg-accent-purple-900/20 text-gray-900 dark:text-white shadow-sm border border-accent-purple-200 dark:border-accent-purple-800 overflow-hidden">
      <!-- Self-task header with collapse toggle -->
      <div
        class="flex items-center gap-2 mb-2 text-accent-purple-600 dark:text-accent-purple-400 cursor-pointer"
        @click="selfTaskExpanded = !selfTaskExpanded"
      >
        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
        </svg>
        <span class="text-xs uppercase tracking-wide font-medium">Background Task Result</span>
        <svg
          class="w-3 h-3 ml-auto transition-transform"
          :class="{ 'rotate-180': selfTaskExpanded }"
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
        </svg>
      </div>
      <!-- Collapsed preview -->
      <div v-if="!selfTaskExpanded" class="text-sm text-gray-500 dark:text-gray-400 truncate">
        {{ contentPreview }}
      </div>
      <!-- Expanded content -->
      <div
        v-else
        class="prose prose-sm dark:prose-invert max-w-none break-words prose-p:my-2 prose-headings:my-3 prose-ul:my-2 prose-ol:my-2 prose-li:my-0 prose-pre:my-2 prose-pre:max-w-full prose-pre:overflow-x-auto prose-pre:whitespace-pre prose-code:text-indigo-600 dark:prose-code:text-indigo-400 prose-code:bg-gray-100 dark:prose-code:bg-gray-700 prose-code:px-1 prose-code:py-0.5 prose-code:rounded prose-code:break-words prose-code:before:content-none prose-code:after:content-none prose-a:break-words"
        v-html="renderedContent"
      ></div>
    </div>
    <p v-if="formattedTime" class="text-xs text-gray-400 dark:text-gray-500 mt-1">{{ formattedTime }}</p>
  </div>
  <!-- Assistant message (markdown rendered) -->
  <div
    v-else
    class="max-w-[85%] min-w-0 group relative"
  >
    <button
      type="button"
      class="absolute top-2 right-2 z-10 p-1.5 rounded-md bg-white/80 dark:bg-gray-700/80 text-gray-500 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white hover:bg-white dark:hover:bg-gray-700 shadow-sm opacity-0 group-hover:opacity-100 focus:opacity-100 transition-opacity"
      :title="copied ? 'Copied!' : 'Copy message'"
      :aria-label="copied ? 'Copied' : 'Copy message'"
      @click.stop="copyContent"
    >
      <svg v-if="!copied" class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" /></svg>
      <svg v-else class="w-4 h-4 text-status-success-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" /></svg>
    </button>
    <div class="rounded-xl px-4 py-3 bg-white dark:bg-gray-800 text-gray-900 dark:text-white shadow-sm overflow-hidden">
      <div v-if="source === 'voice'" class="flex items-center gap-1.5 mb-1 text-gray-400 dark:text-gray-500">
        <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.536 8.464a5 5 0 010 7.072M18.364 5.636a9 9 0 010 12.728M5.586 15H4a1 1 0 01-1-1v-4a1 1 0 011-1h1.586l4.707-4.707C10.923 3.663 12 4.109 12 5v14c0 .891-1.077 1.337-1.707.707L5.586 15z" /></svg>
        <span class="text-[10px] uppercase tracking-wide">Voice</span>
      </div>
      <div
        class="prose prose-sm dark:prose-invert max-w-none break-words prose-p:my-2 prose-headings:my-3 prose-ul:my-2 prose-ol:my-2 prose-li:my-0 prose-pre:my-2 prose-pre:max-w-full prose-pre:overflow-x-auto prose-pre:whitespace-pre prose-code:text-indigo-600 dark:prose-code:text-indigo-400 prose-code:bg-gray-100 dark:prose-code:bg-gray-700 prose-code:px-1 prose-code:py-0.5 prose-code:rounded prose-code:break-words prose-code:before:content-none prose-code:after:content-none prose-a:break-words"
        v-html="renderedContent"
      ></div>
    </div>
    <p v-if="formattedTime" class="text-xs text-gray-400 dark:text-gray-500 mt-1">{{ formattedTime }}</p>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { renderMarkdown } from '../../utils/markdown'

const props = defineProps({
  role: {
    type: String,
    required: true,
    validator: (value) => ['user', 'assistant'].includes(value)
  },
  content: {
    type: String,
    required: true
  },
  timestamp: {
    type: String,
    default: null
  },
  source: {
    type: String,
    default: 'text'
  }
})

// SELF-EXEC-001: Self-task results start collapsed
const selfTaskExpanded = ref(false)

const copied = ref(false)
async function copyContent() {
  try {
    await navigator.clipboard.writeText(props.content || '')
    copied.value = true
    setTimeout(() => { copied.value = false }, 2000)
  } catch (err) {
    console.error('Copy failed:', err)
  }
}

const renderedContent = computed(() => {
  return renderMarkdown(props.content)
})

// SELF-EXEC-001: Preview for collapsed self-task results
const contentPreview = computed(() => {
  const text = props.content || ''
  const firstLine = text.split('\n')[0] || ''
  return firstLine.length > 100 ? firstLine.substring(0, 100) + '...' : firstLine
})

const formattedTime = computed(() => {
  if (!props.timestamp) return null
  const date = new Date(props.timestamp)
  const now = new Date()
  const isToday = date.toDateString() === now.toDateString()
  const time = date.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })
  if (isToday) return time
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) + ', ' + time
})
</script>
