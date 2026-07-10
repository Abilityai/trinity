<template>
  <div class="min-h-screen bg-gray-50 dark:bg-gray-950 text-gray-900 dark:text-gray-100">
    <!-- Top bar -->
    <header class="border-b border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900">
      <div class="max-w-4xl mx-auto px-4 py-3 flex items-center justify-between">
        <div class="flex items-center gap-2">
          <span class="text-xl">🪟</span>
          <span class="font-semibold">Client Portal</span>
        </div>
        <button
          v-if="store.isClientSignedIn"
          class="text-sm text-gray-500 hover:text-gray-800 dark:hover:text-gray-200"
          @click="store.signOut()"
        >Sign out</button>
      </div>
    </header>

    <main class="max-w-4xl mx-auto px-4 py-10">
      <!-- ============ SIGN-IN ============ -->
      <div v-if="!store.isClientSignedIn" class="max-w-sm mx-auto">
        <h1 class="text-xl font-semibold mb-1">Sign in to your agents</h1>
        <p class="text-sm text-gray-500 dark:text-gray-400 mb-6">
          Enter the email an operator shared agents with. We'll send you a 6-digit code.
        </p>

        <!-- Step 1: email -->
        <form v-if="step === 'email'" @submit.prevent="onRequest" class="space-y-3">
          <input
            v-model="email"
            type="email"
            required
            placeholder="you@example.com"
            class="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-3 py-2 text-sm focus:ring-action-primary-500 focus:border-action-primary-500"
          />
          <button
            type="submit"
            :disabled="busy || !email"
            class="w-full rounded-lg bg-action-primary-600 hover:bg-action-primary-700 text-white text-sm px-4 py-2 disabled:opacity-50"
          >{{ busy ? 'Sending…' : 'Send code' }}</button>
        </form>

        <!-- Step 2: code -->
        <form v-else @submit.prevent="onVerify" class="space-y-3">
          <p class="text-sm text-gray-500 dark:text-gray-400">
            If <span class="font-medium text-gray-700 dark:text-gray-300">{{ email }}</span> has access,
            a code is on its way. Enter it below.
          </p>
          <input
            v-model="code"
            inputmode="numeric"
            maxlength="6"
            placeholder="6-digit code"
            class="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-3 py-2 text-sm tracking-widest text-center focus:ring-action-primary-500 focus:border-action-primary-500"
          />
          <button
            type="submit"
            :disabled="busy || code.length < 6"
            class="w-full rounded-lg bg-action-primary-600 hover:bg-action-primary-700 text-white text-sm px-4 py-2 disabled:opacity-50"
          >{{ busy ? 'Verifying…' : 'Verify & continue' }}</button>
          <button type="button" class="w-full text-xs text-gray-400 hover:text-gray-600" @click="step = 'email'; code = ''">
            ← use a different email
          </button>
        </form>

        <p v-if="error" class="mt-3 text-sm text-status-danger-600 dark:text-status-danger-400">{{ error }}</p>
      </div>

      <!-- ============ ROSTER ============ -->
      <div v-else>
        <div class="mb-5">
          <h1 class="text-2xl font-semibold tracking-tight">Your Agents</h1>
          <p class="text-sm text-gray-500 dark:text-gray-400 mt-0.5">
            Shared with <span class="font-medium text-gray-700 dark:text-gray-300">{{ store.clientEmail }}</span>.
          </p>
        </div>

        <!-- Search across all your conversations (thread title + message content). -->
        <div class="mb-4 relative">
          <svg class="w-4 h-4 text-gray-400 absolute left-3 top-2.5 pointer-events-none" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-4.35-4.35M17 11a6 6 0 11-12 0 6 6 0 0112 0z" /></svg>
          <input
            v-model="searchQuery"
            type="search"
            placeholder="Search your chats…"
            class="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 text-sm pl-9 pr-3 py-2 focus:ring-action-primary-500 focus:border-action-primary-500"
          />
        </div>

        <!-- Results replace the roster while searching (main-page-style search). -->
        <div v-if="isSearching">
          <div v-if="searching" class="text-center py-10 text-sm text-gray-400">Searching…</div>
          <div v-else-if="searchResults.length === 0" class="text-center py-10 text-sm text-gray-400">
            No chats match “{{ searchQuery.trim() }}”.
          </div>
          <ul v-else class="space-y-2">
            <li
              v-for="r in searchResults"
              :key="r.session_id"
              class="rounded-lg border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 p-3 cursor-pointer hover:border-action-primary-400 dark:hover:border-action-primary-600 transition-colors"
              @click="openResult(r)"
            >
              <div class="flex items-center justify-between gap-2">
                <div class="font-medium text-sm truncate">{{ r.title || 'Untitled chat' }}</div>
                <span class="shrink-0 text-[11px] px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400">{{ r.agent_name }}</span>
              </div>
              <div v-if="r.snippet" class="mt-1 text-xs text-gray-500 dark:text-gray-400 line-clamp-2">{{ r.snippet }}</div>
              <div v-if="r.last_message_at" class="mt-1 text-[11px] text-gray-400">{{ formatDate(r.last_message_at) }}</div>
            </li>
          </ul>
        </div>

        <div v-show="!isSearching">
        <div v-if="store.loading" class="text-center py-16">
          <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-action-primary-500 mx-auto"></div>
        </div>

        <div v-else-if="store.agents.length === 0" class="text-center py-16 border border-dashed border-gray-300 dark:border-gray-700 rounded-lg">
          <div class="text-4xl mb-3">🗂️</div>
          <p class="text-sm text-gray-600 dark:text-gray-300 font-medium">No agents shared with you yet</p>
        </div>

        <div v-else class="grid grid-cols-1 sm:grid-cols-2 gap-5">
          <div
            v-for="a in store.agents"
            :key="a.name"
            class="group rounded-2xl border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 p-5 cursor-pointer transition-all duration-150 hover:shadow-lg hover:border-action-primary-300 dark:hover:border-action-primary-700 hover:-translate-y-0.5"
            @click="openChat(a)"
          >
            <div class="flex items-center gap-4">
              <div
                class="h-16 w-16 shrink-0 rounded-full flex items-center justify-center text-white font-semibold text-xl shadow-sm ring-2 ring-white/40 dark:ring-black/20"
                :style="{ background: tint(a.name) }"
              >{{ initials(a.name) }}</div>
              <div class="min-w-0">
                <div class="font-semibold text-lg truncate">{{ a.name }}</div>
                <div class="text-sm text-gray-500 dark:text-gray-400 truncate"><span v-if="a.owner">by {{ a.owner }}</span></div>
                <div class="text-[11px] text-gray-400 mt-0.5"><span v-if="a.shared_at">shared {{ formatDate(a.shared_at) }}</span></div>
              </div>
            </div>
            <div class="mt-5 flex items-center justify-between">
              <button
                class="text-xs px-3 py-1.5 rounded-lg border border-gray-300 dark:border-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800"
                @click.stop="docsAgent = a"
              >Files</button>
              <span class="text-sm font-medium text-gray-400 group-hover:text-action-primary-600 dark:group-hover:text-action-primary-400 transition-colors">Open chat →</span>
            </div>
          </div>
        </div>
        </div>
      </div>
    </main>

    <!-- Chat drawer over the portal session (roster-scoped, gated endpoint) -->
    <PortalChat
      v-if="chatAgent"
      :agent="chatAgent"
      :initial-session-id="chatInitialSession"
      :send-message="(name, msg, sid) => store.sendPortalChat(name, msg, sid)"
      :load-history="(name, sid) => store.fetchHistory(name, sid)"
      :list-sessions="(name) => store.fetchSessions(name)"
      :create-session="(name) => store.createSession(name)"
      :synthesize="(name, text) => store.synthesizeTts(name, text)"
      :transcribe="(name, blob) => store.transcribeStt(name, blob)"
      :voice-available="!!chatAgent.voice_available"
      @close="chatAgent = null; chatInitialSession = null"
    />

    <!-- Documents drawer — files a rostered agent has shared -->
    <PortalDocuments
      v-if="docsAgent"
      :agent="docsAgent"
      @close="docsAgent = null"
    />
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import PortalChat from '@/views/enterprise/PortalChat.vue'
import PortalDocuments from '@/views/enterprise/PortalDocuments.vue'

const store = useClientPortalStore()
const chatAgent = ref(null)
const chatInitialSession = ref(null)
const docsAgent = ref(null)

// --- Cross-chat search (title + message content, across all rostered agents) ---
const searchQuery = ref('')
const searchResults = ref([])
const searching = ref(false)
const isSearching = computed(() => searchQuery.value.trim().length >= 2)
let searchTimer = null

watch(searchQuery, (q) => {
  clearTimeout(searchTimer)
  if (q.trim().length < 2) { searchResults.value = []; searching.value = false; return }
  searching.value = true
  searchTimer = setTimeout(async () => {
    try { searchResults.value = await store.searchChats(q.trim()) }
    catch { searchResults.value = [] }
    finally { searching.value = false }
  }, 250)   // debounce keystrokes
})

// Open a rostered agent's chat at its most-recent thread (roster "Chat" button).
function openChat(a) {
  chatInitialSession.value = null
  chatAgent.value = a
}

// Open a search result on its specific conversation. Reuse the roster card for
// the owner label when we have it; otherwise fall back to name-only.
function openResult(r) {
  const known = store.agents.find((a) => a.name === r.agent_name)
  chatInitialSession.value = r.session_id
  chatAgent.value = known || { name: r.agent_name }
}
const step = ref('email')
const email = ref('')
const code = ref('')
const busy = ref(false)
const error = ref(null)

async function onRequest() {
  busy.value = true
  error.value = null
  try {
    await store.requestCode(email.value.trim().toLowerCase())
    step.value = 'code'
  } catch (err) {
    error.value = err.response?.data?.detail || 'Could not send a code. Try again.'
  } finally {
    busy.value = false
  }
}

async function onVerify() {
  busy.value = true
  error.value = null
  try {
    await store.verifyCode(email.value.trim().toLowerCase(), code.value.trim())
    await store.fetchRoster()
  } catch (err) {
    error.value = err.response?.status === 401
      ? 'Invalid or expired code.'
      : (err.response?.data?.detail || 'Verification failed.')
  } finally {
    busy.value = false
  }
}

function initials(name) {
  const p = (name || '?').replace(/[^A-Za-z0-9]+/g, ' ').trim().split(' ')
  return ((p[0]?.[0] || '') + (p[1]?.[0] || p[0]?.[1] || '')).toUpperCase() || '?'
}
function tint(name) {
  let h = 0
  for (let i = 0; i < (name || '').length; i++) h = (h * 31 + name.charCodeAt(i)) % 360
  return `hsl(${h}, 45%, 45%)`
}
function formatDate(iso) {
  try { return new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) }
  catch { return iso }
}

onMounted(() => { if (store.isClientSignedIn) store.fetchRoster() })
</script>
