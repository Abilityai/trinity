<template>
  <div class="min-h-screen bg-gray-50 dark:bg-gray-900 flex flex-col">
    <!-- Minimal header -->
    <header class="bg-white dark:bg-gray-800 shadow-sm py-3 px-4">
      <div class="max-w-3xl mx-auto">
        <div class="flex items-center justify-between">
          <div v-if="linkInfo && linkInfo.valid" class="flex items-center space-x-2">
            <div class="w-2 h-2 rounded-full bg-green-500 animate-pulse"></div>
            <span class="text-sm font-medium text-gray-700 dark:text-gray-300">Chat</span>
          </div>
          <div v-else class="text-sm text-gray-500 dark:text-gray-400">
            Loading...
          </div>
        </div>
      </div>
    </header>

    <!-- Main content -->
    <main class="flex-1 flex flex-col max-w-3xl mx-auto w-full p-4">
      <!-- Loading state -->
      <div v-if="loading" class="flex-1 flex items-center justify-center">
        <div class="text-center">
          <div class="animate-spin rounded-full h-10 w-10 border-b-2 border-indigo-500 mx-auto mb-4"></div>
          <p class="text-gray-500 dark:text-gray-400">Loading...</p>
        </div>
      </div>

      <!-- Invalid link state -->
      <div v-else-if="!linkInfo || !linkInfo.valid" class="flex-1 flex items-center justify-center">
        <div class="text-center bg-white dark:bg-gray-800 rounded-xl shadow-lg p-8 max-w-md">
          <div class="w-16 h-16 bg-red-100 dark:bg-red-900/30 rounded-full flex items-center justify-center mx-auto mb-4">
            <svg class="w-8 h-8 text-red-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
            </svg>
          </div>
          <h2 class="text-xl font-semibold text-gray-900 dark:text-white mb-2">Link Not Available</h2>
          <p class="text-gray-500 dark:text-gray-400">
            {{ linkError || 'This link is no longer valid or has expired.' }}
          </p>
        </div>
      </div>

      <!-- Agent not available -->
      <div v-else-if="!linkInfo.agent_available" class="flex-1 flex items-center justify-center">
        <div class="text-center bg-white dark:bg-gray-800 rounded-xl shadow-lg p-8 max-w-md">
          <div class="w-16 h-16 bg-yellow-100 dark:bg-yellow-900/30 rounded-full flex items-center justify-center mx-auto mb-4">
            <svg class="w-8 h-8 text-yellow-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
          </div>
          <h2 class="text-xl font-semibold text-gray-900 dark:text-white mb-2">Agent Unavailable</h2>
          <p class="text-gray-500 dark:text-gray-400">
            The agent is currently offline. Please try again later.
          </p>
        </div>
      </div>

      <!-- Email verification required -->
      <div v-else-if="linkInfo.require_email && !isVerified" class="flex-1 flex items-center justify-center">
        <div class="bg-white dark:bg-gray-800 rounded-xl shadow-lg p-8 max-w-md w-full">
          <div class="text-center mb-6">
            <div class="w-16 h-16 bg-indigo-100 dark:bg-indigo-900/30 rounded-full flex items-center justify-center mx-auto mb-4">
              <svg class="w-8 h-8 text-indigo-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
              </svg>
            </div>
            <h2 class="text-xl font-semibold text-gray-900 dark:text-white mb-2">Verify Your Email</h2>
            <p class="text-gray-500 dark:text-gray-400 text-sm">
              {{ !codeSent ? 'Enter your email to continue' : 'Enter the code sent to your email' }}
            </p>
          </div>

          <!-- Email input form -->
          <form v-if="!codeSent" @submit.prevent="requestCode" class="space-y-4">
            <div>
              <input
                v-model="email"
                type="email"
                required
                placeholder="your@email.com"
                class="w-full px-4 py-3 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
                :disabled="verifyLoading"
              />
            </div>
            <button
              type="submit"
              :disabled="verifyLoading || !email"
              class="w-full py-3 px-4 bg-indigo-600 hover:bg-indigo-700 disabled:bg-indigo-400 text-white font-medium rounded-lg transition-colors"
            >
              <span v-if="verifyLoading" class="flex items-center justify-center">
                <svg class="animate-spin -ml-1 mr-2 h-4 w-4 text-white" fill="none" viewBox="0 0 24 24">
                  <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                  <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                </svg>
                Sending...
              </span>
              <span v-else>Send Code</span>
            </button>
          </form>

          <!-- Code input form -->
          <form v-else @submit.prevent="verifyCode" class="space-y-4">
            <div>
              <input
                v-model="code"
                type="text"
                required
                maxlength="6"
                placeholder="123456"
                class="w-full px-4 py-3 text-center text-2xl tracking-widest border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white focus:ring-2 focus:ring-indigo-500 focus:border-transparent font-mono"
                :disabled="verifyLoading"
              />
              <p class="mt-2 text-xs text-gray-500 dark:text-gray-400 text-center">
                Code sent to {{ email }}
              </p>
            </div>
            <button
              type="submit"
              :disabled="verifyLoading || code.length !== 6"
              class="w-full py-3 px-4 bg-indigo-600 hover:bg-indigo-700 disabled:bg-indigo-400 text-white font-medium rounded-lg transition-colors"
            >
              <span v-if="verifyLoading" class="flex items-center justify-center">
                <svg class="animate-spin -ml-1 mr-2 h-4 w-4 text-white" fill="none" viewBox="0 0 24 24">
                  <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                  <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                </svg>
                Verifying...
              </span>
              <span v-else>Verify</span>
            </button>
            <button
              type="button"
              @click="codeSent = false; code = ''"
              class="w-full py-2 text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
            >
              Use a different email
            </button>
          </form>

          <!-- Error message -->
          <div v-if="verifyError" class="mt-4 p-3 bg-red-100 dark:bg-red-900/30 border border-red-200 dark:border-red-800 rounded-lg">
            <p class="text-sm text-red-600 dark:text-red-400">{{ verifyError }}</p>
          </div>
        </div>
      </div>

      <!-- Chat interface -->
      <div v-else class="flex-1 flex flex-col">
        <!-- Messages area -->
        <div class="flex-1 overflow-y-auto space-y-4 pb-4">
          <!-- Welcome message -->
          <div v-if="messages.length === 0" class="text-center py-12">
            <div class="w-16 h-16 bg-indigo-100 dark:bg-indigo-900/30 rounded-full flex items-center justify-center mx-auto mb-4">
              <svg class="w-8 h-8 text-indigo-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
              </svg>
            </div>
            <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-2">Start a Conversation</h3>
            <p class="text-gray-500 dark:text-gray-400 text-sm max-w-md mx-auto">
              Type a message below to begin chatting.
            </p>
          </div>

          <!-- Message list -->
          <div v-for="(msg, index) in messages" :key="index" class="px-2">
            <div
              :class="[
                'rounded-xl px-4 py-3 max-w-[85%]',
                msg.role === 'user'
                  ? 'bg-indigo-600 text-white ml-auto'
                  : 'bg-white dark:bg-gray-800 text-gray-900 dark:text-white shadow-sm'
              ]"
            >
              <p class="whitespace-pre-wrap">{{ msg.content }}</p>
            </div>
          </div>

          <!-- Loading indicator -->
          <div v-if="chatLoading" class="px-2">
            <div class="bg-white dark:bg-gray-800 rounded-xl px-4 py-3 shadow-sm max-w-[85%]">
              <div class="flex items-center space-x-2">
                <div class="flex space-x-1">
                  <div class="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style="animation-delay: 0ms"></div>
                  <div class="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style="animation-delay: 150ms"></div>
                  <div class="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style="animation-delay: 300ms"></div>
                </div>
                <span class="text-sm text-gray-500 dark:text-gray-400">Thinking...</span>
              </div>
            </div>
          </div>
        </div>

        <!-- Error message -->
        <div v-if="chatError" class="mb-4 p-3 bg-red-100 dark:bg-red-900/30 border border-red-200 dark:border-red-800 rounded-lg">
          <p class="text-sm text-red-600 dark:text-red-400">{{ chatError }}</p>
        </div>

        <!-- Input area -->
        <div class="bg-white dark:bg-gray-800 rounded-xl shadow-lg p-3">
          <form @submit.prevent="sendMessage" class="flex items-end space-x-2">
            <textarea
              v-model="message"
              rows="1"
              placeholder="Type your message..."
              class="flex-1 resize-none border-0 bg-transparent text-gray-900 dark:text-white placeholder-gray-500 dark:placeholder-gray-400 focus:ring-0 focus:outline-none"
              :disabled="chatLoading"
              @keydown.enter.exact.prevent="sendMessage"
              @input="autoResize"
              ref="messageInput"
            ></textarea>
            <button
              type="submit"
              :disabled="chatLoading || !message.trim()"
              class="p-2 bg-indigo-600 hover:bg-indigo-700 disabled:bg-indigo-400 text-white rounded-lg transition-colors"
            >
              <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
              </svg>
            </button>
          </form>
        </div>
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, nextTick } from 'vue'
import { useRoute } from 'vue-router'
import axios from 'axios'

const route = useRoute()
const token = computed(() => route.params.token)

// State
const loading = ref(true)
const linkInfo = ref(null)
const linkError = ref(null)

// Email verification
const email = ref('')
const code = ref('')
const codeSent = ref(false)
const verifyLoading = ref(false)
const verifyError = ref(null)
const sessionToken = ref(localStorage.getItem(`public_session_${token.value}`) || '')
const isVerified = computed(() => !linkInfo.value?.require_email || !!sessionToken.value)

// Chat
const message = ref('')
const messages = ref([])
const chatLoading = ref(false)
const chatError = ref(null)
const messageInput = ref(null)

// Load link info
const loadLinkInfo = async () => {
  loading.value = true
  linkError.value = null

  try {
    const response = await axios.get(`/api/public/link/${token.value}`)
    linkInfo.value = response.data

    if (!response.data.valid) {
      linkError.value = getErrorMessage(response.data.reason)
    }
  } catch (err) {
    console.error('Failed to load link info:', err)
    if (err.response?.status === 404) {
      linkInfo.value = { valid: false }
      linkError.value = 'This link does not exist or has been removed.'
    } else {
      linkError.value = 'Failed to load link information. Please try again.'
    }
  } finally {
    loading.value = false
  }
}

const getErrorMessage = (reason) => {
  switch (reason) {
    case 'expired':
      return 'This link has expired.'
    case 'disabled':
      return 'This link has been disabled by the owner.'
    case 'not_found':
      return 'This link does not exist.'
    default:
      return 'This link is no longer valid.'
  }
}

// Request verification code
const requestCode = async () => {
  verifyLoading.value = true
  verifyError.value = null

  try {
    await axios.post('/api/public/verify/request', {
      token: token.value,
      email: email.value
    })
    codeSent.value = true
  } catch (err) {
    console.error('Failed to request code:', err)
    if (err.response?.status === 429) {
      verifyError.value = 'Too many requests. Please wait a few minutes and try again.'
    } else {
      verifyError.value = err.response?.data?.detail || 'Failed to send verification code.'
    }
  } finally {
    verifyLoading.value = false
  }
}

// Verify code
const verifyCode = async () => {
  verifyLoading.value = true
  verifyError.value = null

  try {
    const response = await axios.post('/api/public/verify/confirm', {
      token: token.value,
      email: email.value,
      code: code.value
    })

    if (response.data.verified) {
      sessionToken.value = response.data.session_token
      localStorage.setItem(`public_session_${token.value}`, response.data.session_token)
    } else {
      verifyError.value = getVerifyErrorMessage(response.data.error)
    }
  } catch (err) {
    console.error('Failed to verify code:', err)
    verifyError.value = err.response?.data?.detail || 'Failed to verify code.'
  } finally {
    verifyLoading.value = false
  }
}

const getVerifyErrorMessage = (error) => {
  switch (error) {
    case 'invalid_code':
      return 'Invalid code. Please check and try again.'
    case 'code_expired':
      return 'Code has expired. Please request a new one.'
    default:
      return 'Verification failed. Please try again.'
  }
}

// Send chat message
const sendMessage = async () => {
  if (!message.value.trim() || chatLoading.value) return

  const userMessage = message.value.trim()
  message.value = ''
  chatError.value = null

  // Add user message to chat
  messages.value.push({
    role: 'user',
    content: userMessage
  })

  // Auto-scroll
  await nextTick()
  scrollToBottom()

  chatLoading.value = true

  try {
    const payload = {
      message: userMessage
    }

    // Include session token if email verification was required
    if (linkInfo.value?.require_email && sessionToken.value) {
      payload.session_token = sessionToken.value
    }

    const response = await axios.post(`/api/public/chat/${token.value}`, payload)

    // Add assistant response to chat
    messages.value.push({
      role: 'assistant',
      content: response.data.response
    })
  } catch (err) {
    console.error('Chat error:', err)
    if (err.response?.status === 401) {
      // Session expired, clear and show verification again
      sessionToken.value = ''
      localStorage.removeItem(`public_session_${token.value}`)
      chatError.value = 'Session expired. Please verify your email again.'
    } else if (err.response?.status === 429) {
      chatError.value = 'Too many requests. Please wait a moment.'
    } else {
      chatError.value = err.response?.data?.detail || 'Failed to send message. Please try again.'
    }
  } finally {
    chatLoading.value = false
    await nextTick()
    scrollToBottom()
  }
}

// Auto-resize textarea
const autoResize = (event) => {
  const textarea = event.target
  textarea.style.height = 'auto'
  textarea.style.height = Math.min(textarea.scrollHeight, 150) + 'px'
}

// Scroll to bottom of messages
const scrollToBottom = () => {
  const container = document.querySelector('main .overflow-y-auto')
  if (container) {
    container.scrollTop = container.scrollHeight
  }
}

// Initialize
onMounted(() => {
  loadLinkInfo()
})
</script>

<style scoped>
/* Custom bounce animation for loading dots */
@keyframes bounce {
  0%, 100% {
    transform: translateY(0);
  }
  50% {
    transform: translateY(-4px);
  }
}
</style>
