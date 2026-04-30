<template>
  <!-- Floating help button -->
  <button
    v-if="!isOpen"
    @click="openChat"
    class="fixed bottom-6 right-6 w-14 h-14 bg-indigo-600 hover:bg-indigo-700 text-white rounded-full shadow-lg flex items-center justify-center transition-all hover:scale-105 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:ring-offset-2 z-50"
    aria-label="Open help chat"
  >
    <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8.228 9c.549-1.165 2.03-2 3.772-2 2.21 0 4 1.343 4 3 0 1.4-1.278 2.575-3.006 2.907-.542.104-.994.54-.994 1.093m0 3h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
    </svg>
  </button>

  <!-- Chat panel -->
  <Transition
    enter-active-class="transition ease-out duration-200"
    enter-from-class="opacity-0 translate-y-4"
    enter-to-class="opacity-100 translate-y-0"
    leave-active-class="transition ease-in duration-150"
    leave-from-class="opacity-100 translate-y-0"
    leave-to-class="opacity-0 translate-y-4"
  >
    <div
      v-if="isOpen"
      ref="panelRef"
      class="fixed bottom-6 right-6 w-96 max-w-[calc(100vw-3rem)] h-[32rem] max-h-[calc(100vh-6rem)] bg-white dark:bg-gray-800 rounded-xl shadow-2xl flex flex-col z-50 border border-gray-200 dark:border-gray-700"
      role="dialog"
      aria-label="Help chat"
      @keydown.escape="closeChat"
    >
      <!-- Header -->
      <div class="flex items-center justify-between px-4 py-3 border-b border-gray-200 dark:border-gray-700 bg-indigo-600 rounded-t-xl">
        <div class="flex items-center space-x-2">
          <svg class="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8.228 9c.549-1.165 2.03-2 3.772-2 2.21 0 4 1.343 4 3 0 1.4-1.278 2.575-3.006 2.907-.542.104-.994.54-.994 1.093m0 3h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <span class="font-semibold text-white">Trinity Help</span>
        </div>
        <div class="flex items-center space-x-1">
          <!-- New conversation button -->
          <button
            v-if="messages.length > 0"
            @click="startNewConversation"
            class="p-1.5 text-white/80 hover:text-white hover:bg-white/10 rounded-lg transition-colors"
            title="Start new conversation"
            aria-label="Start new conversation"
          >
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
          </button>
          <!-- Close button -->
          <button
            @click="closeChat"
            class="p-1.5 text-white/80 hover:text-white hover:bg-white/10 rounded-lg transition-colors"
            aria-label="Close help chat"
          >
            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      </div>

      <!-- Messages area -->
      <div
        ref="messagesRef"
        class="flex-1 overflow-y-auto p-4 space-y-4"
        aria-live="polite"
        aria-atomic="false"
      >
        <!-- Welcome message when empty -->
        <div v-if="messages.length === 0 && !loading" class="text-center py-8">
          <div class="w-12 h-12 bg-indigo-100 dark:bg-indigo-900/30 rounded-full flex items-center justify-center mx-auto mb-3">
            <svg class="w-6 h-6 text-indigo-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
          </div>
          <h3 class="text-sm font-medium text-gray-900 dark:text-white mb-1">How can I help?</h3>
          <p class="text-xs text-gray-500 dark:text-gray-400 max-w-xs mx-auto">
            Ask me anything about Trinity - agents, credentials, scheduling, and more.
          </p>
        </div>

        <!-- Messages -->
        <ChatBubble
          v-for="(msg, idx) in messages"
          :key="idx"
          :role="msg.role"
          :content="msg.content"
        />

        <!-- Loading indicator -->
        <div v-if="loading" class="flex items-center space-x-2 text-gray-500 dark:text-gray-400">
          <div class="flex space-x-1">
            <div class="w-2 h-2 bg-indigo-500 rounded-full animate-bounce" style="animation-delay: 0ms"></div>
            <div class="w-2 h-2 bg-indigo-500 rounded-full animate-bounce" style="animation-delay: 150ms"></div>
            <div class="w-2 h-2 bg-indigo-500 rounded-full animate-bounce" style="animation-delay: 300ms"></div>
          </div>
          <span class="text-sm">Thinking...</span>
        </div>
      </div>

      <!-- Error message -->
      <div v-if="error" class="mx-4 mb-2 p-3 bg-status-danger-50 dark:bg-status-danger-900/20 border border-status-danger-200 dark:border-status-danger-800 rounded-lg">
        <div class="flex items-center justify-between">
          <p class="text-sm text-status-danger-600 dark:text-status-danger-400">{{ error }}</p>
          <button
            @click="retryLastMessage"
            class="ml-2 text-xs text-status-danger-600 dark:text-status-danger-400 hover:text-status-danger-700 dark:hover:text-status-danger-300 underline"
          >
            Retry
          </button>
        </div>
      </div>

      <!-- Input area -->
      <div class="p-3 border-t border-gray-200 dark:border-gray-700">
        <form @submit.prevent="sendMessage" class="flex items-end space-x-2">
          <textarea
            ref="inputRef"
            v-model="inputMessage"
            rows="1"
            :maxlength="2000"
            placeholder="Ask a question..."
            class="flex-1 resize-none border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 bg-white dark:bg-gray-700 text-gray-900 dark:text-white placeholder-gray-500 dark:placeholder-gray-400 focus:ring-2 focus:ring-indigo-500 focus:border-transparent text-sm"
            :disabled="loading"
            @keydown.enter.exact.prevent="sendMessage"
            @input="autoResize"
          ></textarea>
          <button
            type="submit"
            :disabled="loading || !inputMessage.trim()"
            class="p-2 bg-indigo-600 hover:bg-indigo-700 disabled:bg-indigo-400 disabled:cursor-not-allowed text-white rounded-lg transition-colors shrink-0"
            aria-label="Send message"
          >
            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
            </svg>
          </button>
        </form>
      </div>
    </div>
  </Transition>
</template>

<script setup>
import { ref, nextTick, onMounted, onUnmounted, watch } from 'vue'
import ChatBubble from './chat/ChatBubble.vue'

const ENDPOINT = 'https://us-central1-mcp-server-project-455215.cloudfunctions.net/ask-trinity'
const SESSION_KEY = 'trinity_help_session_id'

const isOpen = ref(false)
const messages = ref([])
const inputMessage = ref('')
const loading = ref(false)
const error = ref(null)
const sessionId = ref(null)
const lastUserMessage = ref('')

const panelRef = ref(null)
const messagesRef = ref(null)
const inputRef = ref(null)

const openChat = () => {
  isOpen.value = true
  nextTick(() => {
    inputRef.value?.focus()
  })
}

const closeChat = () => {
  isOpen.value = false
}

const startNewConversation = () => {
  messages.value = []
  sessionId.value = null
  localStorage.removeItem(SESSION_KEY)
  error.value = null
  nextTick(() => {
    inputRef.value?.focus()
  })
}

const autoResize = (event) => {
  const textarea = event.target
  textarea.style.height = 'auto'
  textarea.style.height = Math.min(textarea.scrollHeight, 120) + 'px'
}

const scrollToBottom = () => {
  nextTick(() => {
    if (messagesRef.value) {
      messagesRef.value.scrollTop = messagesRef.value.scrollHeight
    }
  })
}

const sendMessage = async () => {
  const question = inputMessage.value.trim()
  if (!question || loading.value) return

  error.value = null
  lastUserMessage.value = question
  inputMessage.value = ''

  // Reset textarea height
  if (inputRef.value) {
    inputRef.value.style.height = 'auto'
  }

  // Add user message immediately
  messages.value.push({
    role: 'user',
    content: question
  })
  scrollToBottom()

  loading.value = true

  try {
    const payload = { question }
    if (sessionId.value) {
      payload.session_id = sessionId.value
    }

    const response = await fetch(ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    })

    if (!response.ok) {
      throw new Error(`Request failed: ${response.status}`)
    }

    const data = await response.json()

    if (!data.answer) {
      throw new Error('No answer received')
    }

    // Save session ID for multi-turn
    if (data.session_id) {
      sessionId.value = data.session_id
      localStorage.setItem(SESSION_KEY, data.session_id)
    }

    // Add assistant response
    messages.value.push({
      role: 'assistant',
      content: data.answer
    })
    scrollToBottom()

  } catch (err) {
    console.error('Help chat error:', err)
    error.value = 'Failed to get response. Please try again.'
    // Remove the user message on error so retry works cleanly
    messages.value.pop()
  } finally {
    loading.value = false
  }
}

const retryLastMessage = () => {
  if (lastUserMessage.value) {
    inputMessage.value = lastUserMessage.value
    error.value = null
    sendMessage()
  }
}

// Handle keyboard navigation (focus trap)
const handleKeydown = (event) => {
  if (!isOpen.value) return

  if (event.key === 'Tab') {
    const focusableElements = panelRef.value?.querySelectorAll(
      'button:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )
    if (!focusableElements || focusableElements.length === 0) return

    const first = focusableElements[0]
    const last = focusableElements[focusableElements.length - 1]

    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first.focus()
    }
  }
}

// Load session from localStorage on mount
onMounted(() => {
  const savedSession = localStorage.getItem(SESSION_KEY)
  if (savedSession) {
    sessionId.value = savedSession
  }
  document.addEventListener('keydown', handleKeydown)
})

onUnmounted(() => {
  document.removeEventListener('keydown', handleKeydown)
})

// Watch for panel open to focus input
watch(isOpen, (open) => {
  if (open) {
    nextTick(() => {
      inputRef.value?.focus()
    })
  }
})
</script>
