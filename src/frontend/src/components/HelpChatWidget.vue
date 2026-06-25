<template>
  <!-- Floating help button -->
  <button
    v-if="!isOpen"
    @click="openChat"
    class="fixed bottom-6 right-6 w-14 h-14 bg-action-primary-600 hover:bg-action-primary-700 text-white rounded-full shadow-lg flex items-center justify-center transition-all hover:scale-105 focus:outline-none focus:ring-2 focus:ring-action-primary-500 focus:ring-offset-2 z-50"
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
      <div class="flex items-center justify-between px-4 py-3 border-b border-gray-200 dark:border-gray-700 bg-action-primary-600 rounded-t-xl">
        <div class="flex items-center space-x-2">
          <svg class="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8.228 9c.549-1.165 2.03-2 3.772-2 2.21 0 4 1.343 4 3 0 1.4-1.278 2.575-3.006 2.907-.542.104-.994.54-.994 1.093m0 3h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <span class="font-semibold text-white">Trinity Help</span>
        </div>
        <div class="flex items-center space-x-1">
          <!-- New conversation button (Ask mode only) -->
          <button
            v-if="mode === 'ask' && messages.length > 0"
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

      <!-- Mode tabs (only when bug reporting is enabled) -->
      <div v-if="bugReportingEnabled" class="flex border-b border-gray-200 dark:border-gray-700 shrink-0" role="tablist">
        <button
          v-for="tab in tabs"
          :key="tab.id"
          role="tab"
          :aria-selected="mode === tab.id"
          @click="mode = tab.id"
          class="flex-1 px-2 py-2 text-xs font-medium whitespace-nowrap transition-colors border-b-2 -mb-px"
          :class="mode === tab.id
            ? 'border-action-primary-600 text-action-primary-700 dark:text-action-primary-300'
            : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200'"
        >
          {{ tab.label }}
        </button>
      </div>

      <!-- ===================== ASK (Q&A) MODE ===================== -->
      <template v-if="mode === 'ask'">
        <!-- Messages area -->
        <div
          ref="messagesRef"
          class="flex-1 overflow-y-auto p-4 space-y-4"
          aria-live="polite"
          aria-atomic="false"
        >
          <!-- Welcome message when empty -->
          <div v-if="messages.length === 0 && !loading" class="text-center py-8">
            <div class="w-12 h-12 bg-action-primary-100 dark:bg-action-primary-900/30 rounded-full flex items-center justify-center mx-auto mb-3">
              <svg class="w-6 h-6 text-action-primary-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
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
              <div class="w-2 h-2 bg-action-primary-500 rounded-full animate-bounce" style="animation-delay: 0ms"></div>
              <div class="w-2 h-2 bg-action-primary-500 rounded-full animate-bounce" style="animation-delay: 150ms"></div>
              <div class="w-2 h-2 bg-action-primary-500 rounded-full animate-bounce" style="animation-delay: 300ms"></div>
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
              class="flex-1 resize-none border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 bg-white dark:bg-gray-700 text-gray-900 dark:text-white placeholder-gray-500 dark:placeholder-gray-400 focus:ring-2 focus:ring-action-primary-500 focus:border-transparent text-sm"
              :disabled="loading"
              @keydown.enter.exact.prevent="sendMessage"
              @input="autoResize"
            ></textarea>
            <button
              type="submit"
              :disabled="loading || !inputMessage.trim()"
              class="p-2 bg-action-primary-600 hover:bg-action-primary-700 disabled:bg-action-primary-400 disabled:cursor-not-allowed text-white rounded-lg transition-colors shrink-0"
              aria-label="Send message"
            >
              <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
              </svg>
            </button>
          </form>
        </div>
      </template>

      <!-- ===================== REPORT-A-BUG MODE ===================== -->
      <template v-else>
        <div class="flex-1 overflow-y-auto p-4 space-y-3 text-sm">
          <!-- Stage: form -->
          <template v-if="bugStage === 'form'">
            <p class="text-xs text-gray-500 dark:text-gray-400">{{ reportCopy.intro }}</p>
            <div>
              <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Title</label>
              <input
                v-model="bugTitle"
                type="text"
                :maxlength="MAX_TITLE"
                :placeholder="reportCopy.titlePh"
                class="w-full border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 bg-white dark:bg-gray-700 text-gray-900 dark:text-white placeholder-gray-500 dark:placeholder-gray-400 focus:ring-2 focus:ring-action-primary-500 focus:border-transparent text-sm"
              />
              <div class="text-right text-[11px] text-gray-400 mt-0.5">{{ bugTitle.length }}/{{ MAX_TITLE }}</div>
            </div>
            <div>
              <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ reportCopy.descLabel }}</label>
              <textarea
                v-model="bugDescription"
                rows="5"
                :maxlength="MAX_DESC"
                :placeholder="reportCopy.descPh"
                class="w-full resize-none border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 bg-white dark:bg-gray-700 text-gray-900 dark:text-white placeholder-gray-500 dark:placeholder-gray-400 focus:ring-2 focus:ring-action-primary-500 focus:border-transparent text-sm"
              ></textarea>
              <div class="text-right text-[11px] text-gray-400 mt-0.5">{{ bugDescription.length }}/{{ MAX_DESC }}</div>
            </div>
            <div>
              <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">
                Your email <span class="font-normal text-gray-400">(optional)</span>
              </label>
              <input
                v-model="bugEmail"
                type="email"
                maxlength="254"
                placeholder="you@example.com"
                class="w-full border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 bg-white dark:bg-gray-700 text-gray-900 dark:text-white placeholder-gray-500 dark:placeholder-gray-400 focus:ring-2 focus:ring-action-primary-500 focus:border-transparent text-sm"
              />
              <p class="text-[11px] text-gray-400 mt-0.5">So we can follow up. Shared privately with the team — never posted in the public issue.</p>
            </div>
            <p v-if="bugFormError" class="text-xs text-status-danger-600 dark:text-status-danger-400">{{ bugFormError }}</p>
            <button
              @click="reviewReport"
              :disabled="!canReview"
              class="w-full py-2 bg-action-primary-600 hover:bg-action-primary-700 disabled:bg-action-primary-400 disabled:cursor-not-allowed text-white rounded-lg transition-colors text-sm font-medium"
            >
              Review &amp; continue
            </button>
          </template>

          <!-- Stage: review (see-before-send) -->
          <template v-else-if="bugStage === 'review'">
            <div v-if="mode === 'feedback'" class="p-3 rounded-lg bg-action-primary-50 dark:bg-action-primary-900/20 border border-action-primary-200 dark:border-action-primary-800 text-xs text-action-primary-700 dark:text-action-primary-300">
              🔒 This feedback is sent <strong>privately</strong> to the Trinity team — it is not posted publicly. Review before sending.
            </div>
            <div v-else class="p-3 rounded-lg bg-status-warning-50 dark:bg-status-warning-900/20 border border-status-warning-200 dark:border-status-warning-800 text-xs text-status-warning-700 dark:text-status-warning-300">
              ⚠️ This creates a <strong>public</strong> GitHub issue in <code>abilityai/trinity</code> — anyone on the internet can read it. Review everything below before sending.
            </div>

            <div>
              <div class="text-xs font-medium text-gray-700 dark:text-gray-300">Type</div>
              <div class="text-gray-900 dark:text-gray-100">{{ mode === 'feedback' ? '💬 Feedback' : (mode === 'feature' ? '✨ Feature request' : '🐛 Bug report') }}</div>
            </div>
            <div>
              <div class="text-xs font-medium text-gray-700 dark:text-gray-300">Title</div>
              <div class="text-gray-900 dark:text-gray-100 break-words">{{ pendingPayload.title }}</div>
            </div>
            <div>
              <div class="text-xs font-medium text-gray-700 dark:text-gray-300">Description</div>
              <div class="text-gray-900 dark:text-gray-100 whitespace-pre-wrap break-words">{{ pendingPayload.description }}</div>
            </div>
            <div v-if="pendingPayload.email">
              <div class="text-xs font-medium text-gray-700 dark:text-gray-300">Contact email</div>
              <div class="text-gray-900 dark:text-gray-100 break-all">{{ pendingPayload.email }}</div>
              <p class="text-[11px] text-gray-400 mt-0.5">Sent privately to the team for follow-up — not added to the public issue.</p>
            </div>

            <details class="rounded-lg border border-gray-200 dark:border-gray-700">
              <summary class="cursor-pointer px-3 py-2 text-xs font-medium text-gray-700 dark:text-gray-300 select-none">
                Diagnostics that will be sent ({{ pendingPayload.diagnostics.console.length }} console line{{ pendingPayload.diagnostics.console.length === 1 ? '' : 's' }})
              </summary>
              <pre class="px-3 pb-3 pt-1 text-[11px] leading-relaxed text-gray-600 dark:text-gray-400 whitespace-pre-wrap break-words overflow-x-auto">{{ diagnosticsPreview }}</pre>
            </details>

            <p class="text-[11px] text-gray-500 dark:text-gray-400">
              Secrets, tokens, and emails are automatically removed and shown above as <code>[REDACTED]</code> / <code>[email]</code> (re-checked on the server too).
            </p>

            <div v-if="bugError" class="p-2 rounded bg-status-danger-50 dark:bg-status-danger-900/20 border border-status-danger-200 dark:border-status-danger-800 text-xs text-status-danger-600 dark:text-status-danger-400">
              {{ bugError }}
            </div>

            <div class="flex items-center gap-2">
              <button
                @click="bugStage = 'form'"
                :disabled="bugSubmitting"
                class="flex-1 py-2 border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors text-sm"
              >
                Back
              </button>
              <button
                @click="submitReport"
                :disabled="bugSubmitting"
                class="flex-1 py-2 bg-action-primary-600 hover:bg-action-primary-700 disabled:bg-action-primary-400 disabled:cursor-not-allowed text-white rounded-lg transition-colors text-sm font-medium"
              >
                {{ bugSubmitting ? 'Sending…' : (bugError ? 'Retry' : 'Submit report') }}
              </button>
            </div>
          </template>

          <!-- Stage: success -->
          <template v-else-if="bugStage === 'success'">
            <div class="text-center py-6 space-y-3">
              <div class="w-12 h-12 bg-status-success-100 dark:bg-status-success-900/30 rounded-full flex items-center justify-center mx-auto">
                <svg class="w-6 h-6 text-status-success-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                </svg>
              </div>
              <h3 class="text-sm font-medium text-gray-900 dark:text-white">
                {{ bugDeduped
                  ? (mode === 'feedback' ? 'Already received — thanks!' : 'Matches an existing report')
                  : (mode === 'feedback' ? 'Thanks — feedback sent!' : (mode === 'feature' ? 'Thanks — feature request filed!' : 'Thanks — report filed!')) }}
              </h3>
              <a
                v-if="bugResult"
                :href="bugResult"
                target="_blank"
                rel="noopener noreferrer"
                class="inline-block text-sm text-action-primary-600 dark:text-action-primary-400 hover:underline break-all"
              >
                View the GitHub issue ↗
              </a>
              <p v-else class="text-xs text-gray-500 dark:text-gray-400">{{ mode === 'feedback' ? 'Your feedback was sent to the team.' : 'Your report was filed successfully.' }}</p>
              <div>
                <button @click="resetBug" class="text-xs text-gray-500 dark:text-gray-400 hover:underline">
                  Report another
                </button>
              </div>
            </div>
          </template>
        </div>
      </template>
    </div>
  </Transition>
</template>

<script setup>
import { ref, computed, nextTick, onMounted, onUnmounted, watch } from 'vue'
import ChatBubble from './chat/ChatBubble.vue'
import { scrub, scrubLines } from '@/utils/scrub'
import { getConsoleBuffer } from '@/utils/consoleBuffer'
import { useBuildInfo } from '@/composables/useBuildInfo'

const ENDPOINT = 'https://us-central1-mcp-server-project-455215.cloudfunctions.net/ask-trinity'
const SESSION_KEY = 'trinity_help_session_id'
const INSTALL_KEY = 'trinity_install_id'

// #1116 config knobs. Default to the stable Cloudflare-fronted intake domain;
// operators can repoint (VITE_BUG_INTAKE_URL) or disable in-app reporting
// (VITE_BUG_REPORTING_ENABLED=false) at build time.
const BUG_INTAKE_URL = import.meta.env.VITE_BUG_INTAKE_URL || 'https://intake.abilityai.dev/v1/report-bug'
const bugReportingEnabled = String(import.meta.env.VITE_BUG_REPORTING_ENABLED ?? 'true') !== 'false'
const MAX_TITLE = 120
const MAX_DESC = 4000

const tabs = [
  { id: 'ask', label: 'Ask' },
  { id: 'bug', label: 'Bug' },
  { id: 'feature', label: 'Feature' },
  { id: 'feedback', label: 'Feedback' },
]

const isOpen = ref(false)
const mode = ref('ask')

// --- Ask (Q&A) state ---
const messages = ref([])
const inputMessage = ref('')
const loading = ref(false)
const error = ref(null)
const sessionId = ref(null)
const lastUserMessage = ref('')

// --- Bug-report state ---
const bugTitle = ref('')
const bugDescription = ref('')
const bugEmail = ref('')
const bugStage = ref('form') // form | review | success
const bugFormError = ref('')
const bugError = ref('')
const bugSubmitting = ref(false)
const bugResult = ref('')
const bugDeduped = ref(false)
const pendingPayload = ref(null)

const { info: buildInfo, load: loadBuildInfo } = useBuildInfo()

const panelRef = ref(null)
const messagesRef = ref(null)
const inputRef = ref(null)

const canReview = computed(() => bugTitle.value.trim() && bugDescription.value.trim())

// Type-aware copy for the shared report form (bug | feature | feedback).
const reportCopy = computed(() => {
  if (mode.value === 'feature') return {
    intro: "Have an idea? Describe the feature you'd like. We'll attach diagnostics you can review before anything is sent.",
    titlePh: 'Short summary of your idea',
    descLabel: 'What would you like?',
    descPh: 'What should it do, and why would it help?',
  }
  if (mode.value === 'feedback') return {
    intro: "Share anything — what's working, what's not, ideas. This goes privately to our team.",
    titlePh: 'Summary of your feedback',
    descLabel: 'Your feedback',
    descPh: 'Tell us what you think…',
  }
  return {
    intro: "Found something broken? Describe it below. We'll attach diagnostics you can review before anything is sent.",
    titlePh: 'Short summary of the problem',
    descLabel: 'What happened?',
    descPh: 'Steps to reproduce, what you expected, what happened instead…',
  }
})

const diagnosticsPreview = computed(() => {
  if (!pendingPayload.value) return ''
  const d = pendingPayload.value.diagnostics
  const head = [
    `App version : ${d.app_version}`,
    `Git commit  : ${d.git_commit}`,
    `Route       : ${d.route}`,
    `URL         : ${d.url}`,
    `Browser     : ${d.user_agent}`,
    `Viewport    : ${d.viewport}`,
    `OS          : ${d.os}`,
  ].join('\n')
  const cons = d.console.length ? `\n\nRecent console:\n${d.console.join('\n')}` : '\n\nRecent console: (none)'
  return head + cons
})

const openChat = () => {
  isOpen.value = true
  nextTick(() => { inputRef.value?.focus() })
}

const closeChat = () => { isOpen.value = false }

const startNewConversation = () => {
  messages.value = []
  sessionId.value = null
  localStorage.removeItem(SESSION_KEY)
  error.value = null
  nextTick(() => { inputRef.value?.focus() })
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
  messages.value.push({ role: 'user', content: question })
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
    messages.value.push({ role: 'assistant', content: data.answer })
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

// ---- Bug report flow ----

function getInstallId() {
  let id = localStorage.getItem(INSTALL_KEY)
  if (!id) {
    id = (crypto?.randomUUID?.() || `inst-${Date.now()}-${Math.random().toString(16).slice(2)}`)
    localStorage.setItem(INSTALL_KEY, id)
  }
  return id
}

// Build the diagnostics block and scrub every user-influenced value so the
// review screen shows EXACTLY what will be transmitted.
async function reviewReport() {
  bugFormError.value = ''
  if (!canReview.value) {
    bugFormError.value = 'Both a title and a description are required.'
    return
  }
  const contactEmail = bugEmail.value.trim()
  if (contactEmail && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(contactEmail)) {
    bugFormError.value = "That email doesn't look valid — fix it or leave it blank."
    return
  }
  try { await loadBuildInfo() } catch { /* build info is best-effort */ }
  const consoleLines = scrubLines(getConsoleBuffer(50))
  pendingPayload.value = {
    type: ['bug', 'feature', 'feedback'].includes(mode.value) ? mode.value : 'bug',
    title: scrub(bugTitle.value.trim()).slice(0, MAX_TITLE),
    description: scrub(bugDescription.value.trim()).slice(0, MAX_DESC),
    install_id: getInstallId(),
    // Contact email is intentionally NOT scrubbed; the server keeps it out of
    // the public issue and uses it only for the private team notification.
    ...(contactEmail ? { email: contactEmail } : {}),
    diagnostics: {
      app_version: buildInfo.value?.version || 'unknown',
      git_commit: buildInfo.value?.git_commit || 'unknown',
      route: scrub(`${window.location.pathname}${window.location.hash}`),
      url: scrub(window.location.href),
      user_agent: navigator.userAgent || 'unknown',
      viewport: `${window.innerWidth}x${window.innerHeight}`,
      os: navigator.platform || 'unknown',
      console: consoleLines,
    },
  }
  bugError.value = ''
  bugStage.value = 'review'
}

async function submitReport() {
  if (bugSubmitting.value || !pendingPayload.value) return
  bugSubmitting.value = true
  bugError.value = ''
  try {
    const response = await fetch(BUG_INTAKE_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(pendingPayload.value),
    })
    let data = {}
    try { data = await response.json() } catch { /* non-JSON error body */ }

    if (response.status === 429) {
      bugError.value = 'You\'ve hit the report limit for this instance. Please try again later.'
      return
    }
    if (!response.ok || !data.ok) {
      bugError.value = (data && data.error) ? `Couldn't file the report: ${data.error}` : 'Couldn\'t file the report. Please try again.'
      return
    }
    // Only trust an https github.com URL as a clickable link (guards against a
    // javascript:/data: href if the response were ever tampered with).
    const u = typeof data.issue_url === 'string' ? data.issue_url : ''
    bugResult.value = /^https:\/\/github\.com\//i.test(u) ? u : ''
    bugDeduped.value = !!data.deduped
    bugStage.value = 'success'
  } catch (err) {
    console.error('Bug report error:', err)
    bugError.value = 'Network error — please try again.'
  } finally {
    bugSubmitting.value = false
  }
}

function resetBug() {
  bugTitle.value = ''
  bugDescription.value = ''
  bugEmail.value = ''
  pendingPayload.value = null
  bugResult.value = ''
  bugDeduped.value = false
  bugError.value = ''
  bugFormError.value = ''
  bugStage.value = 'form'
}

// Handle keyboard navigation (focus trap)
const handleKeydown = (event) => {
  if (!isOpen.value) return

  if (event.key === 'Tab') {
    const focusableElements = panelRef.value?.querySelectorAll(
      'button:not([disabled]), textarea:not([disabled]), input:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])'
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
  if (open && mode.value === 'ask') {
    nextTick(() => { inputRef.value?.focus() })
  }
})
</script>
