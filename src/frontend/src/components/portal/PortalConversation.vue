<template>
  <div class="flex flex-col h-full min-h-0">
    <!-- Header: agent identity + picker, files, voice -->
    <header class="shrink-0 flex items-center gap-2 px-3 sm:px-4 h-14 border-b border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900">
      <button
        class="sm:hidden -ml-1 p-2 text-gray-500 hover:text-gray-800 dark:hover:text-gray-200"
        aria-label="Menu"
        @click="$emit('open-menu')"
      >
        <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16" /></svg>
      </button>

      <!-- Agent picker (ChatGPT model-picker position) -->
      <div class="relative min-w-0" ref="pickerRef">
        <button
          class="flex items-center gap-2 max-w-full rounded-lg px-2 py-1.5 hover:bg-gray-100 dark:hover:bg-gray-800 transition"
          @click="pickerOpen = !pickerOpen"
        >
          <PortalAvatar :name="agent.name" :avatar-url="agent.avatar_url" :size="26" />
          <span class="font-semibold truncate">{{ agentDisplayName(agent) }}</span>
          <svg class="w-4 h-4 text-gray-400 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" /></svg>
        </button>
        <div
          v-if="pickerOpen"
          class="absolute z-30 mt-1 w-72 max-w-[80vw] rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 shadow-xl py-1 max-h-80 overflow-y-auto"
        >
          <button
            v-for="a in roster"
            :key="a.name"
            class="w-full flex items-center gap-2.5 px-3 py-2 text-left hover:bg-gray-100 dark:hover:bg-gray-800"
            @click="pickAgent(a)"
          >
            <PortalAvatar :name="a.name" :avatar-url="a.avatar_url" :size="28" />
            <span class="min-w-0">
              <span class="block text-sm font-medium truncate">
                {{ a.name === agent.name ? agentDisplayName(a) : `New chat with ${agentDisplayName(a)}` }}
              </span>
              <span v-if="a.description" class="block text-xs text-gray-400 truncate">{{ a.description }}</span>
            </span>
          </button>
        </div>
      </div>

      <div class="ml-auto flex items-center gap-1">
        <button
          v-if="ttsEnabled"
          class="p-2 rounded-lg transition"
          :class="voiceMode ? 'bg-action-primary-100 dark:bg-action-primary-900/40 text-action-primary-600 dark:text-action-primary-300' : 'text-gray-400 hover:text-gray-700 dark:hover:text-gray-200'"
          :title="voiceMode ? 'Voice replies on — click to mute' : 'Speak replies aloud'"
          @click="voiceMode ? (voiceMode = false, stopSpeaking()) : (voiceMode = true)"
        >
          <svg v-if="voiceMode" class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.536 8.464a5 5 0 010 7.072M18.364 5.636a9 9 0 010 12.728M5 9v6h4l5 4V5L9 9H5z" /></svg>
          <svg v-else class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 14l4-4m0 4l-4-4M5 9v6h4l5 4V5L9 9H5z" /></svg>
        </button>
        <button
          class="p-2 rounded-lg text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800 transition"
          title="Files"
          @click="$emit('open-files')"
        >
          <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13" /></svg>
        </button>
      </div>
    </header>

    <!-- Messages -->
    <div ref="scrollEl" class="flex-1 min-h-0 overflow-y-auto px-3 sm:px-6 py-5">
      <div class="max-w-3xl mx-auto space-y-4">
        <div v-if="loadingHistory" class="text-center text-sm text-gray-400 mt-10">Loading…</div>

        <!-- Briefing (new-chat state) rendered by the parent via slot -->
        <slot v-if="!loadingHistory && messages.length === 0 && !sending" name="empty" />

        <div v-for="(m, i) in messages" :key="i" :class="m.role === 'user' ? 'flex justify-end' : 'flex items-start gap-2.5'">
          <PortalAvatar v-if="m.role !== 'user'" :name="agent.name" :avatar-url="agent.avatar_url" :size="28" class="mt-0.5" />
          <div v-if="m.role === 'user'" class="max-w-[85%] flex flex-col items-end gap-1">
            <div
              class="rounded-2xl rounded-br-md px-3.5 py-2 text-sm whitespace-pre-wrap"
              :class="m.failed ? 'bg-status-danger-50 dark:bg-status-danger-900/30 text-status-danger-800 dark:text-status-danger-200 ring-1 ring-status-danger-300 dark:ring-status-danger-800' : 'bg-action-primary-600 text-white'"
            >{{ m.content }}</div>
            <p
              v-if="m.failed && m.error"
              class="text-xs text-status-danger-700 dark:text-status-danger-300 text-right max-w-[32ch]"
            >{{ m.error }}</p>
            <button
              v-if="m.failed"
              class="text-xs text-status-danger-600 dark:text-status-danger-400 hover:underline inline-flex items-center gap-1"
              @click="retry(i)"
            >
              <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" /></svg>
              Not delivered · Retry
            </button>
          </div>
          <div
            v-else
            class="max-w-[85%] rounded-2xl rounded-bl-md bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-gray-100 px-3.5 py-2 text-sm prose-portal"
            v-html="render(m.content)"
          ></div>
        </div>

        <!-- Long-turn status: elapsed-aware, not just bouncing dots -->
        <div v-if="sending" class="flex items-start gap-2.5">
          <PortalAvatar :name="agent.name" :avatar-url="agent.avatar_url" :size="28" class="mt-0.5" />
          <div class="rounded-2xl rounded-bl-md bg-gray-100 dark:bg-gray-800 px-3.5 py-2.5 flex flex-col gap-1.5">
            <div class="flex items-center gap-2">
              <span class="inline-flex gap-1">
                <span class="w-1.5 h-1.5 rounded-full bg-gray-400 animate-bounce" style="animation-delay:0ms"></span>
                <span class="w-1.5 h-1.5 rounded-full bg-gray-400 animate-bounce" style="animation-delay:150ms"></span>
                <span class="w-1.5 h-1.5 rounded-full bg-gray-400 animate-bounce" style="animation-delay:300ms"></span>
              </span>
              <span v-if="elapsed >= 4" class="text-xs text-gray-400">{{ statusLabel }}</span>
            </div>
            <!-- ent#286: what the agent is doing right now. Only while streaming;
                 a non-streaming turn keeps exactly the old indicator. -->
            <ul v-if="streaming && liveActivity.length" class="text-xs text-gray-500 dark:text-gray-400 space-y-0.5">
              <li v-for="(step, si) in liveActivity" :key="si" class="truncate max-w-[36ch]">{{ step }}</li>
            </ul>
          </div>
        </div>
      </div>
    </div>

    <!-- Composer -->
    <div class="shrink-0 border-t border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 px-3 sm:px-6 py-3">
      <div class="max-w-3xl mx-auto">
        <p v-if="offline" class="mb-2 text-xs text-status-warning-600 dark:text-status-warning-400 flex items-center gap-1.5">
          <span class="w-1.5 h-1.5 rounded-full bg-status-warning-500"></span>
          You appear to be offline — messages will send once you're reconnected.
        </p>
        <!-- Attached (uploaded to the agent inbox) file chips -->
        <div v-if="attachments.length" class="mb-2 flex flex-wrap gap-1.5">
          <span
            v-for="(f, i) in attachments"
            :key="i"
            class="inline-flex items-center gap-1 text-xs rounded-full bg-gray-100 dark:bg-gray-800 pl-2.5 pr-1.5 py-1 text-gray-600 dark:text-gray-300"
          >
            <svg class="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13" /></svg>
            <span class="max-w-[10rem] truncate">{{ f.name }}</span>
            <svg v-if="f.uploading" class="w-3 h-3 animate-spin text-gray-400" viewBox="0 0 24 24" fill="none"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>
          </span>
        </div>
        <form class="flex items-end gap-2" @submit.prevent="send">
          <input ref="fileInput" type="file" class="hidden" @change="onPickFile" />
          <button
            type="button"
            class="shrink-0 p-2.5 rounded-xl text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800 transition"
            title="Attach a file for the agent"
            @click="fileInput?.click()"
          >
            <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13" /></svg>
          </button>
          <button
            v-if="sttSupported"
            type="button"
            class="shrink-0 p-2.5 rounded-xl transition disabled:opacity-50"
            :class="listening ? 'text-status-danger-600 dark:text-status-danger-400 animate-pulse' : 'text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800'"
            :title="transcribing ? 'Transcribing…' : (listening ? 'Listening… click to stop' : 'Speak your message')"
            :disabled="transcribing"
            @click="toggleMic"
          >
            <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11a7 7 0 01-14 0m7 7v3m0-3a4 4 0 004-4V7a4 4 0 10-8 0v6a4 4 0 004 4z" /></svg>
          </button>
          <textarea
            ref="textarea"
            v-model="input"
            rows="1"
            :placeholder="listening ? 'Listening…' : `Message ${agentDisplayName(agent)}…`"
            class="flex-1 resize-none rounded-2xl border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-800 text-sm text-gray-900 dark:text-gray-100 px-4 py-2.5 leading-6 focus:ring-2 focus:ring-action-primary-500/40 focus:border-action-primary-500 focus:outline-none max-h-40"
            @input="autoGrow"
            @keydown.enter.exact.prevent="send"
          ></textarea>
          <button
            type="submit"
            class="shrink-0 p-2.5 rounded-xl bg-action-primary-600 hover:bg-action-primary-700 text-white disabled:opacity-40 disabled:hover:bg-action-primary-600 transition"
            :disabled="sending || !input.trim()"
            title="Send"
          >
            <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 12h14M12 5l7 7-7 7" /></svg>
          </button>
        </form>
      </div>
    </div>

    <audio ref="audioEl" class="hidden" @ended="speaking = false" @error="speaking = false"></audio>
  </div>
</template>

<script setup>
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import { renderMarkdown } from '@/utils/markdown'
import { agentDisplayName } from '@/utils/agentName'
import PortalAvatar from './PortalAvatar.vue'
import { deliveryFailureReason } from './portalUtils'

const props = defineProps({
  agent: { type: Object, required: true },      // {name, owner, avatar_url, description, playbooks, voice_available}
  roster: { type: Array, default: () => [] },
  sessionId: { type: String, default: null },   // current thread, or null for a new chat
  prefill: { type: String, default: '' },
})
const emit = defineEmits(['switch-agent', 'session-adopted', 'sessions-changed', 'open-files', 'open-menu'])

const store = useClientPortalStore()
const messages = ref([])
const currentSessionId = ref(props.sessionId)
const loadingHistory = ref(false)
const input = ref('')
const sending = ref(false)
const attachments = ref([])
const offline = ref(typeof navigator !== 'undefined' && navigator.onLine === false)

const scrollEl = ref(null)
const textarea = ref(null)
const fileInput = ref(null)
const pickerRef = ref(null)
const pickerOpen = ref(false)

const render = (c) => renderMarkdown(c || '')

// ---- Load history when the thread/agent changes -------------------------------
async function loadThread(sessionId) {
  loadingHistory.value = true
  messages.value = []
  try {
    const { sessionId: resolved, messages: msgs } = await store.fetchHistory(props.agent.name, sessionId || null)
    currentSessionId.value = sessionId || resolved || null
    messages.value = (msgs || []).map((m) => ({ role: m.role, content: m.content }))
  } catch { /* start empty */ }
  finally { loadingHistory.value = false; await scrollDown() }
}

watch(() => [props.agent.name, props.sessionId], async ([, sid], [oldName]) => {
  currentSessionId.value = sid
  if (props.agent.name !== oldName || sid) await loadThread(sid)
  else { messages.value = []; currentSessionId.value = null }   // brand-new chat
})

watch(() => props.prefill, (v) => {
  if (v) { input.value = v; nextTick(() => { autoGrow(); textarea.value?.focus() }) }
})

onMounted(async () => {
  window.addEventListener('online', onNet)
  window.addEventListener('offline', onNet)
  document.addEventListener('click', onDocClick)
  if (props.prefill) input.value = props.prefill
  if (props.sessionId) await loadThread(props.sessionId)
  else messages.value = []
  autoGrow()
})
onBeforeUnmount(() => {
  window.removeEventListener('online', onNet)
  window.removeEventListener('offline', onNet)
  document.removeEventListener('click', onDocClick)
  cleanupVoice()
})

function onNet() { offline.value = navigator.onLine === false }
function onDocClick(e) { if (pickerOpen.value && pickerRef.value && !pickerRef.value.contains(e.target)) pickerOpen.value = false }

function pickAgent(a) {
  pickerOpen.value = false
  emit('switch-agent', a.name)   // mid-thread → parent starts a NEW chat with that agent (no carry-over)
}

async function scrollDown() {
  await nextTick()
  if (scrollEl.value) scrollEl.value.scrollTop = scrollEl.value.scrollHeight
}
function autoGrow() {
  const el = textarea.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 160) + 'px'
}

// ---- Attachments: upload to the agent inbox; the next turn sees them ----------
async function onPickFile(e) {
  const file = e.target.files?.[0]
  e.target.value = ''
  if (!file) return
  const entry = { name: file.name, uploading: true }
  attachments.value.push(entry)
  try { await store.uploadDocument(props.agent.name, file) }
  catch { entry.error = true }
  finally { entry.uploading = false }
}

// ---- Send + resilient retry ---------------------------------------------------
let elapsedTimer = null
const elapsed = ref(0)
const statusLabel = computed(() => {
  if (elapsed.value >= 30) return `Still working… ${elapsed.value}s`
  if (elapsed.value >= 12) return `Working on it… ${elapsed.value}s`
  return `Thinking… ${elapsed.value}s`
})

async function deliver(text) {
  sending.value = true
  elapsed.value = 0
  clearInterval(elapsedTimer)
  elapsedTimer = setInterval(() => { elapsed.value += 1 }, 1000)
  const startedNew = currentSessionId.value === null
  try {
    // ent#286: stream the turn so tool activity is visible while it runs.
    // Falls back to the synchronous send on ANY streaming failure — an older
    // backend without the route, a proxy that buffers SSE, a stopped agent.
    // The fallback is the point: streaming is an improvement to how a turn is
    // watched, never a new way for one to fail.
    let data = null
    try {
      const started = await store.startPortalChat(props.agent.name, text, currentSessionId.value)
      if (started?.session_id && currentSessionId.value !== started.session_id) {
        // Adopt the thread NOW rather than at the end, so a refresh mid-turn
        // reattaches to it instead of opening a second conversation.
        currentSessionId.value = started.session_id
        emit('session-adopted', started.session_id)
      }
      streaming.value = true
      liveActivity.value = []
      await store.streamPortalExecution(props.agent.name, started.execution_id, onStreamEvent)
      data = await awaitPersistedReply(started.session_id || currentSessionId.value)
    } catch (streamErr) {
      // eslint-disable-next-line no-console
      console.debug('[workspace] streaming unavailable, falling back to sync send', streamErr)
      data = null
    } finally {
      streaming.value = false
      liveActivity.value = []
    }

    if (!data) data = await store.sendPortalChat(props.agent.name, text, currentSessionId.value)

    messages.value.push({ role: 'assistant', content: data.response || '(no response)' })
    if (voiceMode.value && ttsEnabled.value && data.response) speak(data.response)
    if (data.session_id && currentSessionId.value !== data.session_id) {
      currentSessionId.value = data.session_id
      emit('session-adopted', data.session_id)
    }
    if (startedNew || currentSessionId.value) emit('sessions-changed')
    attachments.value = []
    return true
  } catch (err) {
    return { error: deliveryFailureReason(err) }
  } finally {
    sending.value = false
    clearInterval(elapsedTimer)
    await scrollDown()
  }
}

// ent#286 — live turn state. `liveActivity` holds a short, human-readable trail
// of what the agent is doing right now; it is transient and never persisted.
const streaming = ref(false)
const liveActivity = ref([])
const LIVE_ACTIVITY_MAX = 6

// One log entry from the agent's stream → at most one line of visible activity.
// Deliberately conservative: the stream is Claude's raw log, so anything not
// recognised is ignored rather than rendered as noise at a client.
function onStreamEvent(evt) {
  if (!evt || evt.type === 'stream_end') return
  let label = null
  if (evt.type === 'tool_use' || evt.tool_name) label = `Using ${evt.tool_name || 'a tool'}…`
  else if (evt.type === 'thinking') label = 'Thinking…'
  else if (evt.type === 'error') label = 'Hit a problem — recovering…'
  if (!label) return
  if (liveActivity.value[liveActivity.value.length - 1] === label) return  // don't stutter
  liveActivity.value.push(label)
  if (liveActivity.value.length > LIVE_ACTIVITY_MAX) liveActivity.value.shift()
}

// The stream ends when the AGENT's execution ends, but the reply is persisted
// by the backend a moment later — so the last assistant message is read back,
// with a bounded wait, rather than assumed present. Returns null if it never
// shows, which sends `deliver` down the synchronous path instead of inventing
// a reply.
async function awaitPersistedReply(sessionId, { tries = 15, delayMs = 700 } = {}) {
  const before = messages.value.filter((m) => m.role === 'assistant').length
  for (let i = 0; i < tries; i++) {
    try {
      const { messages: msgs } = await store.fetchHistory(props.agent.name, sessionId || null)
      const assistants = (msgs || []).filter((m) => m.role === 'assistant')
      if (assistants.length > before) {
        const last = assistants[assistants.length - 1]
        return { response: last.content, cost: last.cost, session_id: sessionId }
      }
    } catch { /* keep waiting — a hiccup here must not fail an answered turn */ }
    await new Promise((r) => setTimeout(r, delayMs))
  }
  return null
}


async function send() {
  const text = input.value.trim()
  if (!text || sending.value) return
  const msg = { role: 'user', content: text, failed: false, error: null }
  messages.value.push(msg)
  input.value = ''
  autoGrow()
  await scrollDown()
  const res = await deliver(text)
  if (res !== true) { msg.failed = true; msg.error = res?.error || null }
}

async function retry(i) {
  const msg = messages.value[i]
  if (!msg || sending.value) return
  msg.failed = false
  msg.error = null
  const res = await deliver(msg.content)
  if (res !== true) { msg.failed = true; msg.error = res?.error || null }
}

// ---- Voice: speak replies (TTS) + dictate (STT) — carried over from #78 -------
const SpeechRec = typeof window !== 'undefined' ? (window.SpeechRecognition || window.webkitSpeechRecognition) : null
const canRecord = typeof navigator !== 'undefined' && !!navigator.mediaDevices?.getUserMedia && typeof MediaRecorder !== 'undefined'
const micMode = SpeechRec ? 'speech' : (canRecord ? 'record' : null)
const sttSupported = micMode !== null
const ttsEnabled = computed(() => !!props.agent.voice_available)
const voiceMode = ref(false)
const speaking = ref(false)
const listening = ref(false)
const transcribing = ref(false)
const audioEl = ref(null)
let recog = null, mediaRec = null, mediaStream = null, recChunks = [], lastAudioUrl = null

function revokeAudio() { if (lastAudioUrl) { URL.revokeObjectURL(lastAudioUrl); lastAudioUrl = null } }
async function speak(text) {
  if (!text) return
  speaking.value = true
  try {
    const url = await store.synthesizeTts(props.agent.name, text)
    if (!url) { speaking.value = false; return }
    revokeAudio(); lastAudioUrl = url
    if (audioEl.value) { audioEl.value.src = url; await audioEl.value.play() } else speaking.value = false
  } catch { speaking.value = false }
}
function stopSpeaking() { if (audioEl.value) audioEl.value.pause(); speaking.value = false }
function toggleMic() {
  if (!sttSupported || transcribing.value) return
  micMode === 'speech' ? toggleSpeech() : toggleRecord()
}
function toggleSpeech() {
  if (listening.value) { try { recog?.stop() } catch { /* noop */ } return }
  recog = new SpeechRec(); recog.lang = 'en-US'; recog.interimResults = false; recog.maxAlternatives = 1
  recog.onresult = (e) => { const t = e.results?.[0]?.[0]?.transcript || ''; if (t) { input.value = input.value ? `${input.value} ${t}` : t; autoGrow() } }
  recog.onend = () => { listening.value = false }
  recog.onerror = () => { listening.value = false }
  listening.value = true
  try { recog.start() } catch { listening.value = false }
}
async function toggleRecord() {
  if (listening.value) { try { mediaRec?.stop() } catch { /* noop */ } return }
  try { mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true }) } catch { return }
  recChunks = []
  mediaRec = new MediaRecorder(mediaStream)
  mediaRec.ondataavailable = (e) => { if (e.data && e.data.size) recChunks.push(e.data) }
  mediaRec.onstop = async () => {
    stopStream(); listening.value = false
    const blob = new Blob(recChunks, { type: mediaRec?.mimeType || 'audio/webm' }); recChunks = []
    if (blob.size < 1500) return
    transcribing.value = true
    try { const t = await store.transcribeStt(props.agent.name, blob); if (t) { input.value = input.value ? `${input.value} ${t}` : t; autoGrow() } }
    catch { /* keep text mode */ } finally { transcribing.value = false }
  }
  listening.value = true
  try { mediaRec.start(200) } catch { listening.value = false; stopStream() }
}
function stopStream() { try { mediaStream?.getTracks().forEach((t) => t.stop()) } catch { /* noop */ } mediaStream = null }
function cleanupVoice() {
  try { recog?.stop() } catch { /* noop */ }
  try { if (mediaRec && mediaRec.state !== 'inactive') mediaRec.stop() } catch { /* noop */ }
  stopStream(); stopSpeaking(); revokeAudio()
}

defineExpose({ focusComposer: () => textarea.value?.focus() })
</script>

<style scoped>
.prose-portal :deep(p) { margin: 0.25rem 0; }
.prose-portal :deep(pre) { overflow-x: auto; background: rgba(0,0,0,0.06); padding: 0.5rem; border-radius: 0.375rem; }
.prose-portal :deep(code) { font-size: 0.8em; }
.prose-portal :deep(ul) { list-style: disc; padding-left: 1.25rem; }
.prose-portal :deep(a) { text-decoration: underline; }
</style>
