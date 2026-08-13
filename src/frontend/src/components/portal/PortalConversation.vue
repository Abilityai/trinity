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
        <!-- ent#359 AC #4: star from the header too. Hidden until the thread
             exists — a chat with no id yet cannot be pinned, and rendering a
             control that silently does nothing is the dead end this family of
             issues keeps removing. -->
        <PortalStarButton
          v-if="currentSessionId"
          :starred="starred"
          @toggle="$emit('toggle-star', { id: currentSessionId, is_room: false, starred })"
        />
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
              v-if="m.failed && m.retryable !== false"
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
import PortalStarButton from './PortalStarButton.vue'
import { deliveryFailureReason, mentionedAgents, REPLY_MAX_WAIT_MS_FALLBACK } from './portalUtils'

const props = defineProps({
  agent: { type: Object, required: true },      // {name, owner, avatar_url, description, playbooks, voice_available}
  roster: { type: Array, default: () => [] },
  sessionId: { type: String, default: null },   // current thread, or null for a new chat
  prefill: { type: String, default: '' },
  // ent#359: whether the CURRENT thread is starred. Owned by the shell (it
  // holds the per-viewer chat state), rendered here.
  starred: { type: Boolean, default: false },
})
const emit = defineEmits(['switch-agent', 'session-adopted', 'sessions-changed', 'open-files', 'open-menu', 'toggle-star', 'escalate-to-room'])

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
  let inFlight = null
  try {
    const { sessionId: resolved, messages: msgs, inFlightExecutionId } =
      await store.fetchHistory(props.agent.name, sessionId || null)
    currentSessionId.value = sessionId || resolved || null
    messages.value = (msgs || []).map((m) => ({ role: m.role, content: m.content }))
    inFlight = inFlightExecutionId
  } catch { /* start empty */ }
  finally { loadingHistory.value = false; await scrollDown() }

  // ent#286: a turn was still running when this client loaded — reattach to it
  // rather than showing a thread that looks finished. The user's message is
  // already in `messages` (persisted at dispatch), so what is missing is only
  // the "working" state and the reply.
  if (inFlight) await reattach(inFlight)
}

// Rejoin a turn already in progress. The agent replays its buffered log before
// streaming live, so a client that reloaded sees what it missed.
async function reattach(executionId) {
  if (sending.value) return
  sending.value = true
  streaming.value = true
  liveActivity.value = []
  elapsed.value = 0
  clearInterval(elapsedTimer)
  elapsedTimer = setInterval(() => { elapsed.value += 1 }, 1000)
  // The baseline is what is on screen right now: this client reloaded INTO a
  // running turn, so every assistant message it can see predates that turn.
  // Passing nothing made `assistants.length > undefined` false on every poll,
  // so the reply never rendered and the user had to reload a second time.
  const baseline = messages.value.filter((m) => m.role === 'assistant').length
  try {
    await store.streamPortalExecution(props.agent.name, executionId, onStreamEvent)
    const data = await awaitPersistedReply(currentSessionId.value, baseline)
    if (data?.response) messages.value.push({ role: 'assistant', content: data.response })
  } catch { /* the reply lands in history on the next load */ }
  finally {
    sending.value = false
    streaming.value = false
    liveActivity.value = []
    clearInterval(elapsedTimer)
    await scrollDown()
  }
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
    //
    // The fallback to the synchronous send is allowed in exactly ONE case: the
    // DISPATCH itself failed, so no turn exists. Once dispatch returns, the turn
    // is running on the server and re-sending would run it a second time —
    // double spend, double side effects. Anything that goes wrong after that
    // point is a failure to WATCH the turn, and the answer is to go and read
    // its result, never to send it again.
    let data = null
    let started = null
    // Read before anything is dispatched: after the fact it is impossible to
    // tell this turn's reply from the previous one's.
    const baseline = await persistedAssistantCount(currentSessionId.value)
    try {
      started = await store.startPortalChat(props.agent.name, text, currentSessionId.value)
    } catch (dispatchErr) {
      // Nothing was created, so a retry is safe — but only retry when the
      // ROUTE is what failed. A 404/405 means an older backend without this
      // endpoint; a network error means the request never landed. Any other
      // status is the server's real answer about this turn ("the agent is
      // offline", "you are rate limited"), and re-asking synchronously just
      // earns the same answer a second time while the user waits twice as long
      // to hear it.
      const status = dispatchErr?.response?.status
      const routeMissing = status === 404 || status === 405 || !dispatchErr?.response
      if (!routeMissing) throw dispatchErr
      // eslint-disable-next-line no-console
      console.debug('[workspace] streaming route unavailable, using sync send', dispatchErr)
      data = await store.sendPortalChat(props.agent.name, text, currentSessionId.value)
    }

    if (started) {
      // Stamp the dispatch instant: the server's marker TTL starts here, so the
      // client's ceiling must too (see awaitPersistedReply).
      const dispatchedAt = Date.now()
      if (started.session_id && currentSessionId.value !== started.session_id) {
        // Adopt the thread NOW rather than at the end, so a refresh mid-turn
        // reattaches to it instead of opening a second conversation.
        currentSessionId.value = started.session_id
        emit('session-adopted', started.session_id)
      }
      streaming.value = true
      liveActivity.value = []
      try {
        await store.streamPortalExecution(props.agent.name, started.execution_id, onStreamEvent)
      } catch (streamErr) {
        // Watching failed; the turn did not. Fall through and read its result.
        // eslint-disable-next-line no-console
        console.debug('[workspace] lost the stream, reading the result instead', streamErr)
      } finally {
        streaming.value = false
        liveActivity.value = []
      }
      data = await awaitPersistedReply(
        started.session_id || currentSessionId.value, baseline,
        started.wait_budget_seconds, dispatchedAt,
      )
      if (data?.lost) {
        // #2133: we ran out of budget while the marker still claimed a turn.
        // That turn probably RAN and was billed — we merely lost sight of it.
        // So this is reported without a Retry: re-sending is the one action
        // guaranteed to be wrong.
        return { lost: true, error: "Still no reply — we've lost track of this turn. It may still finish; check the conversation shortly." }
      }
      if (!data) {
        // The server reports nothing running and no new reply — a genuine
        // no-answer. Report it; never re-send, the turn was already billed.
        throw new Error('The agent did not reply. Check the conversation in a moment.')
      }
    }

    messages.value.push({ role: 'assistant', content: data.response || '(no response)' })
    if (voiceMode.value && ttsEnabled.value && data.response) speak(data.response)
    if (data.session_id && currentSessionId.value !== data.session_id) {
      currentSessionId.value = data.session_id
      emit('session-adopted', data.session_id)
    }
    // ent#359: carry WHICH thread finished. The shell marks a completed turn
    // read only when the user is still looking at it — this event fires even
    // after the user has navigated away (the send is an async closure that
    // outlives the component), and without the id the shell cannot tell the two
    // apart, so it cleared the badge for a reply the user never saw.
    if (startedNew || currentSessionId.value) emit('sessions-changed', currentSessionId.value)
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
// by the backend a moment later — and sometimes much later, because a failed
// `--resume` retries the whole turn cold under a NEW execution the client is
// no longer watching.
//
// So the wait is bounded by the SERVER's own answer, not by a stopwatch. While
// `in_flight_execution_id` is set on the thread, a turn is still running and
// the only correct thing to do is keep waiting; a fixed deadline here declared
// live, billed turns "not delivered" and offered a Retry that ran and billed
// them a second time.
//
// `baselineAssistants` is the count read from the SERVER before dispatch. Using
// the local list instead let a retry return the PREVIOUS turn's reply on its
// first poll — the answer to the wrong question, while a second turn ran unseen.
const REPLY_POLL_MS = 700
// Time-based, NOT poll-count-based. With the backoff below, 8 polls is up to
// 8 x 15s = 120s late in a turn — so a count silently stretched this debounce
// into two minutes of spinner before the no-answer message appeared.
const REPLY_IDLE_GIVE_UP_MS = 6_000

// #2133: the absolute ceiling, for when the marker is ORPHANED rather than
// merely slow — a hard backend kill skips the `finally` that clears it, and
// `except Exception` does not catch `CancelledError`.
//
// The server sends the budget with the 202 (`wait_budget_seconds`) because the
// server owns the turn timeout. This constant is only the fallback for an older
// backend, and it is sized the same way: TWO full turns, because a failed
// `--resume` re-runs the whole thing cold. A ceiling of one turn would declare a
// live, already-billed cold retry "not delivered" — exactly what #2120 fixed,
// on exactly the path it was fixed for.
// (imported from portalUtils so a test can compare it to the server's value)

// Polling every 700ms for ten minutes is ~850 history reads per tab. The reply
// almost always lands in the first seconds, so the fast interval is what
// matters; after that, widen. Same total wait, an order of magnitude fewer
// requests on the long tail.
const REPLY_POLL_STEPS = [
  { afterMs: 30_000, everyMs: 2_000 },
  { afterMs: 120_000, everyMs: 5_000 },
  { afterMs: 300_000, everyMs: 15_000 },
]

function replyPollInterval(elapsedMs) {
  let every = REPLY_POLL_MS
  for (const step of REPLY_POLL_STEPS) if (elapsedMs >= step.afterMs) every = step.everyMs
  return every
}

async function awaitPersistedReply(sessionId, baselineAssistants, budgetSeconds,
                                   dispatchedAtMs) {
  let idleSince = null
  // Measured from DISPATCH, not from when this function was reached. The
  // server's marker TTL starts ticking at dispatch, so a client clock that
  // starts after the stream breaks (which can be minutes later, on exactly the
  // orphaned-marker path this ceiling exists for) would outlive the marker —
  // and the marker's disappearance would be read as "nothing running" instead
  // of tripping the ceiling. The two clocks now share an origin.
  const startedAt = dispatchedAtMs || Date.now()
  const budgetMs = Number(budgetSeconds) > 0
    ? Number(budgetSeconds) * 1000
    : REPLY_MAX_WAIT_MS_FALLBACK
  const deadline = startedAt + budgetMs
  const wait = () => new Promise((r) => setTimeout(r, replyPollInterval(Date.now() - startedAt)))

  for (;;) {
    // `lost` — NOT a failure. The turn may well have run and been billed; we
    // simply stopped being able to see it. The caller must not offer a Retry.
    if (Date.now() > deadline) return { lost: true }
    let data
    try {
      data = await store.fetchHistory(props.agent.name, sessionId || null)
    } catch {
      // A hiccup reading history is not evidence the turn failed.
      await wait()
      continue
    }
    const assistants = (data.messages || []).filter((m) => m.role === 'assistant')
    if (assistants.length > baselineAssistants) {
      const last = assistants[assistants.length - 1]
      return { response: last.content, cost: last.cost, session_id: data.sessionId || sessionId }
    }
    // Still running server-side? Then keep waiting, however long it takes.
    if (data.inFlightExecutionId) { idleSince = null }
    else {
      if (idleSince === null) idleSince = Date.now()
      // The server has said "nothing running" for long enough. NOT retryable:
      // the turn may well have run and been billed — notably when Redis is down,
      // `mark_turn_inflight` no-ops and this branch is reached on the very first
      // poll of a perfectly healthy turn.
      else if (Date.now() - idleSince >= REPLY_IDLE_GIVE_UP_MS) return { lost: true, idle: true }
    }
    await wait()
  }
}

// Assistant count as the SERVER sees it — the baseline a reply must exceed.
async function persistedAssistantCount(sessionId) {
  if (!sessionId) return 0
  try {
    const data = await store.fetchHistory(props.agent.name, sessionId)
    return (data.messages || []).filter((m) => m.role === 'assistant').length
  } catch {
    return 0
  }
}

// Mark a sent message as undelivered, THROUGH the reactive array.
//
// `messages` is a ref([]), so pushing a plain object stores the raw target and
// Vue only proxies it when you read `messages.value[i]`. Mutating the local
// variable you pushed writes past the proxy: the value changes, nothing
// re-renders, and the failure is invisible.
function markFailed(index, content, error, { retryable = true } = {}) {
  const row = messages.value[index]
  // The array can be replaced under us (thread switch, history reload). Only
  // mark the row if it is still the message we sent.
  if (!row || row.role !== 'user' || row.content !== content) return
  row.failed = true
  row.error = error || null
  // #2133: a turn we merely lost sight of is NOT offered a Retry. Re-sending
  // a turn that already ran is the double-billing this whole path exists to
  // prevent, so the distinction lives on the row rather than in the copy.
  row.retryable = retryable
}

async function send() {
  const text = input.value.trim()
  if (!text || sending.value) return

  // ent#361: @mentioning another agent from a 1:1 makes this a group discussion.
  // Handled BEFORE the message is appended: the conversation moves to a room, so
  // leaving a copy of it in this thread would show the user their message in two
  // places and only one of them would ever get a reply.
  //
  // Gated on the rooms capability for the same reason the picker is (#2128) —
  // without it there is nowhere to escalate TO, and an @mention has to keep
  // working as ordinary text.
  if (store.multiAgentChatAvailable) {
    const others = mentionedAgents(text, props.roster, { exclude: [props.agent.name] })
    if (others.length) {
      input.value = ''
      autoGrow()
      emit('escalate-to-room', { agents: [props.agent.name, ...others], message: text })
      return
    }
  }

  const index = messages.value.push({ role: 'user', content: text, failed: false, error: null }) - 1
  input.value = ''
  autoGrow()
  await scrollDown()
  const res = await deliver(text)
  if (res !== true) markFailed(index, text, res?.error, { retryable: !res?.lost })
}

async function retry(i) {
  const msg = messages.value[i]
  if (!msg || sending.value) return
  const content = msg.content
  msg.failed = false
  msg.error = null
  const res = await deliver(content)
  if (res !== true) markFailed(i, content, res?.error, { retryable: !res?.lost })
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
