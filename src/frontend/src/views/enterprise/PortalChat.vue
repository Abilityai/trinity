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
        <div class="flex items-center gap-1">
          <!-- Voice mode: speak replies (shown only when the agent has a voice). -->
          <button
            v-if="ttsEnabled"
            class="p-1.5 rounded-md text-sm"
            :class="voiceMode ? 'bg-action-primary-100 dark:bg-action-primary-900/40 text-action-primary-600 dark:text-action-primary-300' : 'text-gray-400 hover:text-gray-600 dark:hover:text-gray-200'"
            :title="voiceMode ? 'Voice replies on — click to mute' : 'Speak replies aloud'"
            @click="voiceMode ? (voiceMode = false, stopSpeaking()) : (voiceMode = true)"
          >{{ voiceMode ? '🔊' : '🔇' }}</button>
          <button class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 text-xl leading-none px-1" @click="$emit('close')" aria-label="Close">×</button>
        </div>
      </div>

      <!-- Session switcher — chat history per agent (only when sessions are wired,
           i.e. the public portal; the operator preview page omits these props). -->
      <div v-if="hasSessions" class="flex items-center gap-2 px-4 py-2 border-b border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50">
        <select
          v-model="currentSessionId"
          :disabled="switching"
          class="flex-1 min-w-0 rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-xs text-gray-700 dark:text-gray-200 px-2 py-1.5 focus:ring-action-primary-500 focus:border-action-primary-500"
          @change="onSelectSession"
          aria-label="Conversation history"
        >
          <option v-if="currentSessionId === null" :value="null">New chat</option>
          <option v-for="s in sessions" :key="s.id" :value="s.id">{{ sessionLabel(s) }}</option>
        </select>
        <button
          class="shrink-0 text-xs px-2.5 py-1.5 rounded-md border border-gray-300 dark:border-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-50"
          :disabled="switching || sending"
          @click="newChat"
          title="Start a new conversation"
        >+ New</button>
      </div>

      <!-- Messages -->
      <div ref="scrollEl" class="flex-1 overflow-y-auto px-4 py-4 space-y-3">
        <div v-if="loadingHistory || switching" class="text-center text-sm text-gray-400 mt-10">Loading…</div>
        <div v-else-if="messages.length === 0 && !sending" class="text-center text-sm text-gray-400 mt-10">
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
          <!-- Dictate: browser speech-to-text (Chrome/Edge). -->
          <button
            v-if="sttSupported"
            type="button"
            class="shrink-0 rounded-lg border px-3 py-2 text-sm disabled:opacity-50"
            :class="listening ? 'border-status-danger-400 text-status-danger-600 dark:text-status-danger-400 animate-pulse' : 'border-gray-300 dark:border-gray-600 text-gray-500 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800'"
            :title="transcribing ? 'Transcribing…' : (listening ? (micMode === 'record' ? 'Recording… click to stop' : 'Listening… click to stop') : 'Speak your message')"
            :disabled="transcribing"
            @click="toggleMic"
          >{{ transcribing ? '⏳' : '🎤' }}</button>
          <textarea
            v-model="input"
            rows="1"
            :placeholder="listening ? 'Listening…' : 'Message…'"
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

      <!-- Hidden player for spoken replies (voice mode). -->
      <audio ref="audioEl" class="hidden" @ended="speaking = false" @error="speaking = false"></audio>
    </div>
  </div>
</template>

<script setup>
import { ref, nextTick, onMounted, onBeforeUnmount, computed } from 'vue'
import { useAgentsStore } from '@/stores/agents'
import { renderMarkdown } from '@/utils/markdown'

const props = defineProps({
  agent: { type: Object, required: true },   // { name, owner }
  // Pluggable send: (agentName, text, sessionId) => Promise<{response, cost, session_id}>.
  // Defaults to the operator OSS chat (which ignores sessionId); the public
  // /portal passes the portal-session send.
  sendMessage: { type: Function, default: null },
  // Optional history loader: (agentName, sessionId) => Promise<{sessionId, messages}>
  // (an array is also accepted for back-compat). When provided (public /portal),
  // the drawer restores the persisted conversation on open. Omitted on the
  // operator preview page.
  loadHistory: { type: Function, default: null },
  // Optional session list/create: enable the per-agent chat-history switcher.
  // (agentName) => Promise<[{id,title,last_message_at,message_count}]> / Promise<session>.
  // Omitted on the operator preview (no history threads there).
  listSessions: { type: Function, default: null },
  createSession: { type: Function, default: null },
  // Open the drawer directly on a specific thread (e.g. a search result). When
  // null the drawer resumes the client's most-recent conversation.
  initialSessionId: { type: String, default: null },
  // Voice mode (#78): synthesize a reply → object URL. (agentName, text) => Promise<url|null>.
  // Provided on the public /portal; omitted on the operator preview.
  synthesize: { type: Function, default: null },
  // Speech-to-text fallback for browsers without the Web Speech API (Firefox):
  // (agentName, blob) => Promise<text>. Records with MediaRecorder + uploads.
  transcribe: { type: Function, default: null },
  // Whether this agent has a configured voice + the platform key (roster flag).
  voiceAvailable: { type: Boolean, default: false },
})
defineEmits(['close'])

const agentsStore = useAgentsStore()
const doSend = props.sendMessage || agentsStore.sendChatMessage
const messages = ref([])
const sessions = ref([])
const currentSessionId = ref(null)
const loadingHistory = ref(false)
const switching = ref(false)

const hasSessions = computed(() => !!props.listSessions)

// --- Voice mode: speak replies (ElevenLabs TTS) + dictate input (browser STT) ---
const SpeechRec = typeof window !== 'undefined'
  ? (window.SpeechRecognition || window.webkitSpeechRecognition)
  : null
const canRecord = typeof navigator !== 'undefined'
  && !!navigator.mediaDevices?.getUserMedia
  && typeof MediaRecorder !== 'undefined'
// Prefer the free client-side Web Speech API; fall back to record→upload (Scribe)
// when the browser has none (Firefox) and a transcribe fn is wired.
const micMode = SpeechRec ? 'speech' : (canRecord && !!props.transcribe ? 'record' : null)
const sttSupported = micMode !== null
const ttsEnabled = computed(() => props.voiceAvailable && !!props.synthesize)
const voiceMode = ref(false)      // when on, each reply is spoken
const speaking = ref(false)
const listening = ref(false)      // active mic (speech or recording)
const transcribing = ref(false)   // uploading a recorded clip to Scribe
const audioEl = ref(null)
let recog = null
let mediaRec = null
let mediaStream = null
let recChunks = []
let lastAudioUrl = null

function revokeAudio() {
  if (lastAudioUrl) { URL.revokeObjectURL(lastAudioUrl); lastAudioUrl = null }
}

async function speak(text) {
  if (!text || !props.synthesize) return
  speaking.value = true
  try {
    const url = await props.synthesize(props.agent.name, text)
    if (!url) { speaking.value = false; return }
    revokeAudio()
    lastAudioUrl = url
    if (audioEl.value) { audioEl.value.src = url; await audioEl.value.play() }
    else speaking.value = false
  } catch { speaking.value = false }
}

function stopSpeaking() {
  if (audioEl.value) { audioEl.value.pause() }
  speaking.value = false
}

function toggleMic() {
  if (!sttSupported || transcribing.value) return
  if (micMode === 'speech') return toggleSpeech()
  return toggleRecord()
}

function toggleSpeech() {
  if (listening.value) { try { recog?.stop() } catch { /* noop */ } return }
  recog = new SpeechRec()
  recog.lang = 'en-US'
  recog.interimResults = false
  recog.maxAlternatives = 1
  recog.onresult = (e) => {
    const t = e.results?.[0]?.[0]?.transcript || ''
    if (t) input.value = input.value ? `${input.value} ${t}` : t
  }
  recog.onend = () => { listening.value = false }
  recog.onerror = () => { listening.value = false }
  listening.value = true
  try { recog.start() } catch { listening.value = false }
}

async function toggleRecord() {
  if (listening.value) { try { mediaRec?.stop() } catch { /* noop */ } return }  // stop → onstop transcribes
  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true })
  } catch { error.value = 'Microphone access was blocked.'; return }
  recChunks = []
  mediaRec = new MediaRecorder(mediaStream)
  mediaRec.ondataavailable = (e) => { if (e.data && e.data.size) recChunks.push(e.data) }
  mediaRec.onstop = async () => {
    stopStream()
    listening.value = false
    const blob = new Blob(recChunks, { type: mediaRec?.mimeType || 'audio/webm' })
    recChunks = []
    if (!props.transcribe) return
    // ~1.5 KB of Opus is well under a spoken word — treat as an empty/mis-tapped
    // recording and coach the user rather than round-tripping silence to Scribe.
    if (blob.size < 1500) { error.value = 'Recording was too short — tap the mic, speak, then tap again to stop.'; return }
    transcribing.value = true
    try {
      const t = await props.transcribe(props.agent.name, blob)
      if (t) input.value = input.value ? `${input.value} ${t}` : t
    } catch (err) {
      error.value = err.response?.data?.detail || 'Could not transcribe the recording.'
    } finally { transcribing.value = false }
  }
  listening.value = true
  // Timeslice so chunks flush periodically (more robust than a single final flush).
  try { mediaRec.start(200) } catch { listening.value = false; stopStream() }
}

function stopStream() {
  try { mediaStream?.getTracks().forEach((t) => t.stop()) } catch { /* noop */ }
  mediaStream = null
}

onBeforeUnmount(() => {
  try { recog?.stop() } catch { /* noop */ }
  try { if (mediaRec && mediaRec.state !== 'inactive') mediaRec.stop() } catch { /* noop */ }
  stopStream()
  stopSpeaking()
  revokeAudio()
})

// Normalize loadHistory's return (new {sessionId, messages} shape or a bare array).
function normHistory(res) {
  if (Array.isArray(res)) return { sessionId: null, messages: res }
  return { sessionId: res?.sessionId ?? null, messages: res?.messages ?? [] }
}

function sessionLabel(s) {
  const t = (s.title || '').trim() || 'Untitled chat'
  const when = s.last_message_at || s.created_at
  return when ? `${t} · ${shortDate(when)}` : t
}
function shortDate(iso) {
  try { return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) }
  catch { return '' }
}

onMounted(async () => {
  if (props.listSessions) {
    try { sessions.value = await props.listSessions(props.agent.name) } catch { /* best-effort */ }
  }
  if (!props.loadHistory) return
  loadingHistory.value = true
  try {
    // A search result opens on its thread; otherwise resume the most-recent.
    const target = props.initialSessionId || null
    const { sessionId, messages: msgs } = normHistory(await props.loadHistory(props.agent.name, target))
    currentSessionId.value = sessionId || target
    messages.value = (msgs || []).map(m => ({ role: m.role, content: m.content }))
    await scrollDown()
  } catch { /* best-effort; start with an empty thread */ }
  finally { loadingHistory.value = false }
})

const input = ref('')
const sending = ref(false)
const error = ref(null)
const scrollEl = ref(null)

const render = (c) => renderMarkdown(c || '')

async function scrollDown() {
  await nextTick()
  if (scrollEl.value) scrollEl.value.scrollTop = scrollEl.value.scrollHeight
}

async function refreshSessions() {
  if (!props.listSessions) return
  try { sessions.value = await props.listSessions(props.agent.name) } catch { /* best-effort */ }
}

async function onSelectSession() {
  // currentSessionId already updated by v-model; load that thread.
  if (!props.loadHistory) return
  switching.value = true
  error.value = null
  try {
    const { messages: msgs } = normHistory(await props.loadHistory(props.agent.name, currentSessionId.value))
    messages.value = (msgs || []).map(m => ({ role: m.role, content: m.content }))
    await scrollDown()
  } catch {
    error.value = 'Could not load that conversation.'
  } finally { switching.value = false }
}

async function newChat() {
  error.value = null
  messages.value = []
  if (props.createSession) {
    try {
      const s = await props.createSession(props.agent.name)
      if (s && s.id) {
        sessions.value = [s, ...sessions.value]
        currentSessionId.value = s.id
        return
      }
    } catch { /* fall through to a client-side blank thread */ }
  }
  currentSessionId.value = null   // a blank thread; the first send opens a session
}

async function send() {
  const text = input.value.trim()
  if (!text || sending.value) return
  error.value = null
  messages.value.push({ role: 'user', content: text })
  input.value = ''
  sending.value = true
  await scrollDown()
  const startedNew = currentSessionId.value === null
  try {
    const data = await doSend(props.agent.name, text, currentSessionId.value)
    const reply = data.response || '(no response)'
    messages.value.push({ role: 'assistant', content: reply })
    // Voice mode: speak the reply (best-effort; stays text on any failure).
    if (voiceMode.value && ttsEnabled.value && data.response) speak(data.response)
    // Adopt the thread the backend resolved a session-less turn into.
    if (data.session_id && currentSessionId.value !== data.session_id) {
      currentSessionId.value = data.session_id
    }
    // A first message may have created/renamed a thread — refresh the list.
    if (startedNew || props.listSessions) await refreshSessions()
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
