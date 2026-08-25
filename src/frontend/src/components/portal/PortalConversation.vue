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
      <div class="max-w-4xl mx-auto space-y-6">
        <div v-if="loadingHistory" class="text-center text-sm text-gray-400 mt-10">Loading…</div>

        <!-- Briefing (new-chat state) rendered by the parent via slot -->
        <slot v-if="!loadingHistory && messages.length === 0 && !sending" name="empty" />

        <div v-for="(m, i) in messages" :key="i" :class="m.role === 'user' ? 'flex justify-end' : 'flex items-start gap-2.5'">
          <PortalAvatar v-if="m.role !== 'user'" :name="agent.name" :avatar-url="agent.avatar_url" :size="28" class="mt-0.5" />
          <div v-if="m.role === 'user'" class="max-w-[85%] flex flex-col items-end gap-1">
            <div
              class="rounded-2xl rounded-br-md px-3.5 py-3 text-sm leading-relaxed whitespace-pre-wrap"
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
            class="max-w-[85%] rounded-2xl rounded-bl-md bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-gray-100 px-3.5 py-3 text-sm leading-relaxed prose-portal"
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

        <!-- ent#365: what this conversation actually produced, at the end of
             the thread where the newest turns are. Inside the scroll region,
             not pinned above the composer like the ent#364 asks — an ask is
             waiting on you, a deliverable is something to read. -->
        <PortalDeliverables
          :agent-name="agent.name"
          :session-id="currentSessionId"
          :refresh-key="deliverableTick"
        />
      </div>
    </div>

    <!-- ent#458: loops this agent is running, above the asks. Quiet unless
         something is active; platform-authenticated door only, so an external
         client never sees the strip. -->
    <PortalLoops :participants="[agent.name]" />

    <!-- ent#364: asks this agent raised, immediately above the composer — the
         third rendering of the SAME row the sidebar counts and the agent page
         shows, so answering here clears it in both. Directly above the input
         because it is a turn that is waiting on the person about to type. -->
    <div v-if="agentAsks.length" class="shrink-0 px-3 sm:px-6 pt-2">
      <div class="max-w-4xl mx-auto">
        <PortalAsks
          :agent-name="agent.name"
          :current-session-id="currentSessionId"
          @open-thread="(t) => emit('open-thread', t)"
        />
      </div>
    </div>

    <!-- Composer -->
    <div class="shrink-0 border-t border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 px-3 sm:px-6 py-3">
      <div class="max-w-4xl mx-auto">
        <p v-if="offline" class="mb-2 text-xs text-status-warning-600 dark:text-status-warning-400 flex items-center gap-1.5">
          <span class="w-1.5 h-1.5 rounded-full bg-status-warning-500"></span>
          You appear to be offline — messages will send once you're reconnected.
        </p>
        <!-- #2212: every voice failure says what happened here. Voice is an
             assist, never a blocker, so this is an inline notice next to a
             composer that still works — not a modal. -->
        <p
          v-if="voiceError"
          class="mb-2 text-xs text-status-danger-600 dark:text-status-danger-400 flex items-start gap-1.5"
          role="status"
          aria-live="polite"
        >
          <span class="mt-1 w-1.5 h-1.5 rounded-full bg-status-danger-500 shrink-0"></span>
          <span class="flex-1">{{ voiceError }}</span>
          <button
            type="button"
            class="shrink-0 underline hover:no-underline text-gray-500 dark:text-gray-400"
            @click="voiceError = ''"
          >Dismiss</button>
        </p>
        <!-- ent#155: a cancel that was REFUSED. Deliberately not `markFailed` —
             the turn is still running and still spending, so calling it failed
             would be the dishonest half of "honest status". -->
        <p
          v-if="cancelError"
          class="mb-2 text-xs text-status-danger-600 dark:text-status-danger-400 flex items-start gap-1.5"
          role="status"
          aria-live="polite"
        >
          <span class="mt-1 w-1.5 h-1.5 rounded-full bg-status-danger-500 shrink-0"></span>
          <span class="flex-1">{{ cancelError }}</span>
          <button
            type="button"
            class="shrink-0 underline hover:no-underline text-gray-500 dark:text-gray-400"
            @click="cancelError = ''"
          >Dismiss</button>
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
          <!-- #2259: the composer's action buttons are `h-11 w-11` (44px, on the
               4px grid) rather than `p-2.5` around a 20px icon (40px, off it).
               `items-end` pins them to the bottom so they stay beside the LAST
               line as the field grows, and at 44px against the 46px single-line
               composer the icon's centre lands within 1px of the text line in
               both states. Sizing the box explicitly (instead of padding an
               icon) also keeps the three buttons identical when one of them
               swaps its glyph. -->
          <button
            type="button"
            class="shrink-0 h-11 w-11 flex items-center justify-center rounded-xl text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800 transition"
            title="Attach a file for the agent"
            @click="fileInput?.click()"
          >
            <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13" /></svg>
          </button>
          <button
            v-if="sttSupported"
            type="button"
            class="shrink-0 h-11 w-11 flex items-center justify-center rounded-xl transition disabled:opacity-50"
            :class="listening ? 'text-status-danger-600 dark:text-status-danger-400 animate-pulse' : 'text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800'"
            :title="micTitle"
            :aria-label="micTitle"
            :aria-pressed="listening"
            :disabled="transcribing"
            @click="toggleMic"
          >
            <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11a7 7 0 01-14 0m7 7v3m0-3a4 4 0 004-4V7a4 4 0 10-8 0v6a4 4 0 004 4z" /></svg>
          </button>
          <!-- ent#392: this is the composer's FIRST anchored overlay, so the
               wrapper is new. It must inherit the flex sizing the textarea used
               to carry (`flex-1 min-w-0`) — a bare `relative` div collapses the
               field to content width — and it deliberately carries no z-index,
               so it creates no stacking context of its own.

               #2259: it must ALSO not be taller than the textarea it wraps. A
               `<textarea>` is inline-block, so inside this block wrapper it sat
               on the baseline and the line box reserved 6px of descender space
               below it. `items-end` aligns the flex ITEM — this wrapper — so the
               buttons bottom-aligned to that dead space and Send hung 6px below
               the visible input edge. The `block` on the textarea removes the
               line box entirely; it also re-anchors the typeahead's
               `absolute bottom-full` to the real field. Do not drop it. -->
          <div ref="composerWrap" class="relative flex-1 min-w-0">
            <PortalTypeahead
              v-if="typeaheadOpen"
              :kind="typeaheadKind"
              :rows="typeaheadRows"
              :active-index="activeIndex"
              :overflow="typeaheadBound.overflow"
              :hidden-count="typeaheadHidden"
              :empty-message="typeaheadEmpty || ''"
              @pick="acceptActive"
              @hover="activeIndex = $event"
            />
            <textarea
              ref="textarea"
              v-model="input"
              rows="1"
              :placeholder="composerPlaceholder"
              class="block w-full resize-none rounded-2xl border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-800 text-sm text-gray-900 dark:text-gray-100 px-4 py-2.5 leading-6 focus:ring-2 focus:ring-action-primary-500/40 focus:border-action-primary-500 focus:outline-none max-h-40"
              @input="onComposerInput"
              @keydown="onComposerKeydown"
              @click="onComposerCaret"
              @select="onComposerCaret"
            ></textarea>
          </div>
          <!-- ent#155: Send becomes Stop while a turn is live. Send is disabled
               for that whole period anyway, so this is the same control doing
               the only thing it usefully can. -->
          <button
            v-if="canCancelTurn"
            type="button"
            @click="cancelTurn"
            :disabled="cancelling"
            class="shrink-0 h-11 w-11 flex items-center justify-center rounded-xl bg-status-danger-600 hover:bg-status-danger-700 text-white disabled:opacity-40 transition"
            :title="cancelling ? 'Stopping…' : 'Stop this turn (Esc)'"
            aria-label="Stop this turn"
          >
            <svg v-if="cancelling" class="w-5 h-5 animate-spin" fill="none" viewBox="0 0 24 24">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
            </svg>
            <svg v-else class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><rect x="7" y="7" width="10" height="10" rx="1.5" stroke-width="2" /></svg>
          </button>
          <button
            v-else
            type="submit"
            class="shrink-0 h-11 w-11 flex items-center justify-center rounded-xl bg-action-primary-600 hover:bg-action-primary-700 text-white disabled:opacity-40 disabled:hover:bg-action-primary-600 transition"
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
import PortalLoops from './PortalLoops.vue'
import PortalAvatar from './PortalAvatar.vue'
import PortalStarButton from './PortalStarButton.vue'
import PortalTypeahead from './PortalTypeahead.vue'
import PortalAsks from './PortalAsks.vue'
import PortalDeliverables from './PortalDeliverables.vue'
import {
  deliveryFailureReason,
  mentionedAgents,
  resolveWaitBudgetMs,
  applyTypeaheadInsert,
  boundCandidates,
  buildMentionToken,
  clampActiveIndex,
  detectTypeaheadTrigger,
  dismissAfterInsert,
  filterAgentCandidates,
  filterPlaybookCandidates,
  hiddenPlaybookCount,
  playbookSearchSource,
  isSuppressed,
  nextActiveIndex,
  nextDismissState,
  resolveComposerGrowth,
  resolveComposerKey,
  starterFor,
  typeaheadEmptyMessage,
  MIN_RECORDING_BYTES,
  RECORDING_TOO_SHORT_MESSAGE,
  SPEECH_START_TIMEOUT_MS,
  TRANSCRIPT_EMPTY_MESSAGE,
  TTS_FAILED_MESSAGE,
  recorderErrorMessage,
  resolveMicMode,
  resolveRecordingMimeType,
  speechAttemptOutcome,
  speechErrorMessage,
  transcriptionErrorMessage,
} from './portalUtils'
import { shouldCancelOnEscape, restoreDraft, cancelOutcome, isNoopCancel } from '../../utils/turnCancel'

const props = defineProps({
  // `stt_available` (#2212) is the platform's ability to transcribe server-side
  // (an ElevenLabs key resolves) — a different fact from `voice_available`,
  // which additionally needs an effective voice to speak WITH.
  agent: { type: Object, required: true },      // {name, owner, avatar_url, description, playbooks, voice_available, stt_available}
  roster: { type: Array, default: () => [] },
  sessionId: { type: String, default: null },   // current thread, or null for a new chat
  prefill: { type: String, default: '' },
  // ent#359: whether the CURRENT thread is starred. Owned by the shell (it
  // holds the per-viewer chat state), rendered here.
  starred: { type: Boolean, default: false },
})
const emit = defineEmits(['switch-agent', 'session-adopted', 'sessions-changed', 'open-files', 'open-menu', 'toggle-star', 'escalate-to-room', 'open-thread'])

const store = useClientPortalStore()
// ent#364: one list, filtered — never a second fetch for this surface.
// `.name`, not the object (ent#429): `agent` is `{name, owner, ...}` and
// `asksForAgent` compares against `a.agent_name`, so passing the object matched
// nothing and this surface — the third of the three ent#364 promises — had never
// rendered. It failed SILENTLY, as an empty list is a legitimate state.
const agentAsks = computed(() => store.asksForAgent(props.agent.name))
const messages = ref([])
const currentSessionId = ref(props.sessionId)
const loadingHistory = ref(false)
const input = ref('')
const sending = ref(false)
// ent#155 — stopping an in-flight turn. The id arrives with the 202, so Stop is
// offered only once there is something to stop; a turn that fell back to the
// synchronous send has no id and correctly offers nothing.
const activeExecutionId = ref(null)
const pendingUserText = ref('')
const cancelling = ref(false)
const cancelError = ref('')
// Review finding: `terminate_portal_turn` writes the CANCELLED terminal, and
// the still-running background turn then finishes — `portal_chat` sees a
// non-success status, matches no `error_code` branch, and raises the generic
// `agent_error` 502. `deliver` reports failed, `send()` calls `markFailed`, and
// the user who pressed Stop sees their own message struck out in red under
// "Something went wrong while the agent was working on this."
// A cancel the user ASKED FOR is not a failure, so the client remembers which
// executions it cancelled and declines to mark those. Client-side on purpose:
// the portal outcome path has no `cancelled` category, and inventing one would
// change a shared contract for one surface.
const cancelledExecutionIds = ref(new Set())
// Survives `deliver`'s finally, which clears the live id — `send()` needs to
// know which execution the turn it is about to judge actually ran under.
const lastDeliveredExecutionId = ref(null)
const canCancelTurn = computed(() => sending.value && !!activeExecutionId.value)
const attachments = ref([])
const offline = ref(typeof navigator !== 'undefined' && navigator.onLine === false)

const scrollEl = ref(null)
const textarea = ref(null)
const fileInput = ref(null)
const pickerRef = ref(null)
const pickerOpen = ref(false)

// ent#392 — composer typeahead state. Three refs, no decisions: `trigger` is
// whatever the pure scanner last returned, `activeIndex` is the roving
// selection (-1 = nothing chosen, which is what makes Enter send), `dismissed`
// is the Esc sentinel.
const composerWrap = ref(null)
const typeaheadTrigger = ref(null)
const activeIndex = ref(-1)
const dismissed = ref(null)

const render = (c) => renderMarkdown(c || '')

// ---- Load history when the thread/agent changes -------------------------------
async function loadThread(sessionId) {
  loadingHistory.value = true
  messages.value = []
  let inFlight = null
  let inFlightBudget = null
  let budgetReadAt = null
  let outcome = null
  try {
    const { sessionId: resolved, messages: msgs, inFlightExecutionId, inFlightWaitBudgetSeconds,
            lastTurnOutcome } =
      await store.fetchHistory(props.agent.name, sessionId || null)
    // #2214: the budget is the marker's REMAINING TTL, honest only from the
    // instant it was measured — stamp that instant beside the fetch, not when
    // the (possibly long) reattached stream later ends.
    budgetReadAt = Date.now()
    currentSessionId.value = sessionId || resolved || null
    messages.value = (msgs || []).map((m) => ({ role: m.role, content: m.content }))
    inFlight = inFlightExecutionId
    inFlightBudget = inFlightWaitBudgetSeconds
    outcome = lastTurnOutcome
  } catch { /* start empty */ }
  finally { loadingHistory.value = false; await scrollDown() }

  // ent#286: a turn was still running when this client loaded — reattach to it
  // rather than showing a thread that looks finished. The user's message is
  // already in `messages` (persisted at dispatch), so what is missing is only
  // the "working" state and the reply.
  if (inFlight) { await reattach(inFlight, inFlightBudget, budgetReadAt); return }

  // #2320: nothing running, but the last turn on this thread failed. The user
  // message is on screen (persisted at dispatch, deliberately — ent#286) with
  // no reply after it and, before this, no explanation either: reopening the
  // thread showed a question the agent had silently ignored. The verdict
  // outlives the tab that sent it, so it is applied on load too, not only to
  // the client that happened to be watching.
  if (outcome) markLastUserTurnFailed(outcome)
}

// Apply a server verdict to the most recent user message. Used by the two
// surfaces that have no in-memory index for it — a fresh load and a reattach —
// where the row came from history rather than from this tab's own `send()`.
function markLastUserTurnFailed(outcome) {
  if (!outcome?.message) return
  const last = messages.value[messages.value.length - 1]
  // Only ever the UNANSWERED tail. Two raise sites (`portal_chat`'s roster 404
  // and its availability refusal) fire BEFORE `_persist_user_turn`, so they
  // record a verdict while leaving no user row of their own. Walking back to
  // "the last user message" would then pin the failure onto an EARLIER turn
  // that was answered — reporting a successful exchange as failed, and offering
  // a Retry that re-sends it. Today both are gated earlier in
  // `start_portal_turn`, so no execution row exists to carry a verdict; that is
  // a property of the current call graph, not of this function, and it is the
  // kind of property that rots quietly. If nothing is waiting for a reply, say
  // nothing.
  if (!last || last.role !== 'user') return
  markFailed(messages.value.length - 1, last.content, outcome.message,
             { retryable: outcome.retryable === true })
}

// Rejoin a turn already in progress. The agent replays its buffered log before
// streaming live, so a client that reloaded sees what it missed.
// #2214: `budgetSeconds` is the server's remaining wait budget for that turn
// (the marker's TTL at read time) and `budgetReadAt` the instant it was read —
// together they bound the wait the same way a fresh dispatch's 202 budget does.
async function reattach(executionId, budgetSeconds, budgetReadAt) {
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
    const data = await awaitPersistedReply(currentSessionId.value, baseline,
                                           budgetSeconds, budgetReadAt, executionId)
    // #2320: this surface was the silent one. It checked ONLY for a reply, so a
    // turn that failed — or was lost — rendered nothing at all: no message, no
    // Retry, the spinner simply stopped. A client that refreshed mid-turn got
    // less than one that stayed, which is backwards.
    if (data?.failed) {
      markLastUserTurnFailed(data.outcome)
    } else if (data?.lost) {
      markLastUserTurnFailed({
        message: data.idle
          ? 'The agent did not reply. Check the conversation in a moment.'
          : "Still no reply — we've lost track of this turn. It may still finish; check the conversation shortly.",
        retryable: false,
      })
    }
    if (data?.response) {
      messages.value.push({ role: 'assistant', content: data.response })
      // A reattached reply is still a reply the user just watched land, so it
      // has to announce itself like `deliver()` does. Without this the thread
      // keeps its server-side unread count and the sidebar badges the
      // conversation on screen.
      emit('sessions-changed', currentSessionId.value)
    }
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
  resetTypeahead()
  if (props.agent.name !== oldName || sid) await loadThread(sid)
  else { messages.value = []; currentSessionId.value = null }   // brand-new chat
})

watch(() => props.prefill, (v) => {
  if (v) { input.value = v; resetTypeahead(); nextTick(() => { autoGrow(); textarea.value?.focus() }) }
})

onMounted(async () => {
  window.addEventListener('online', onNet)
  window.addEventListener('offline', onNet)
  document.addEventListener('click', onDocClick)
  document.addEventListener('keydown', onEscapeKeydown)
  window.addEventListener('resize', onViewportResize)
  if (props.prefill) input.value = props.prefill
  if (props.sessionId) await loadThread(props.sessionId)
  else messages.value = []
  autoGrowAfterUpdate()   // `props.prefill` was assigned above; wait for the patch
})
// Review finding: `overflow-y` is now pinned, so the height must be recomputed when
// the box REWRAPS — narrowing the window (or opening a drawer) makes a fitting draft
// taller, and a stale `hidden` would leave that text invisible and unscrollable
// where it previously scrolled. Cheap: one listener, only while mounted.
function onViewportResize() {
  autoGrow()
}

onBeforeUnmount(() => {
  window.removeEventListener('online', onNet)
  window.removeEventListener('offline', onNet)
  document.removeEventListener('click', onDocClick)
  document.removeEventListener('keydown', onEscapeKeydown)
  window.removeEventListener('resize', onViewportResize)
  cleanupVoice()
})

function onNet() { offline.value = navigator.onLine === false }
function onDocClick(e) {
  if (pickerOpen.value && pickerRef.value && !pickerRef.value.contains(e.target)) pickerOpen.value = false
  // Outside the composer AND its popup closes the typeahead WITHOUT arming the
  // Esc sentinel; a click inside the textarea recomputes via @click, and a click
  // on the popup's own padding or scrollbar must not close it. This also covers
  // the files drawer and the mobile nav, whose buttons live outside the wrapper.
  if (typeaheadTrigger.value && composerWrap.value && !composerWrap.value.contains(e.target)) closeTypeahead()
}

function pickAgent(a) {
  pickerOpen.value = false
  emit('switch-agent', a.name)   // mid-thread → parent starts a NEW chat with that agent (no carry-over)
}

async function scrollDown() {
  await nextTick()
  if (scrollEl.value) scrollEl.value.scrollTop = scrollEl.value.scrollHeight
}
// #2211: the composer's growth ceiling, matching the `max-h-40` class on the
// textarea (40 * 4px). Named so the class and the JS cannot drift apart.
const COMPOSER_MAX_PX = 160

function autoGrow() {
  const el = textarea.value
  if (!el) return
  el.style.height = 'auto'
  const { height, overflowY } = resolveComposerGrowth(el, COMPOSER_MAX_PX)
  el.style.height = height + 'px'
  el.style.overflowY = overflowY
}

// Review finding: `autoGrow()` reads the DOM, but Vue patches `v-model` on the NEXT
// microtask — so every call that follows a programmatic `input.value = ...` (send,
// clear, restore-on-failure, dictation, typeahead pick) measured the OLD content.
// Before this PR that only meant "did not resize"; now that `overflow-y` is managed
// explicitly it also means a taller value can be left clipped AND unscrollable. So
// programmatic mutations use this deferred form, and the direct `@input` path keeps
// the synchronous one (the DOM is already current there).
function autoGrowAfterUpdate() {
  nextTick(autoGrow)
}


// ---- ent#392: composer typeahead (`/` playbooks, `@` agents) -----------------
//
// A DISPATCHER. Every decision — what counts as a trigger, what is offered, what
// a key does, what a pick splices — lives in the pure exports of portalUtils.js,
// because `vitest` runs `environment: 'node'` with no component-mount harness,
// so a decision left here is a decision no test can reach.

const typeaheadKind = computed(() => typeaheadTrigger.value?.kind || '/')

const typeaheadResult = computed(() => {
  const t = typeaheadTrigger.value
  if (!t) return null
  if (t.kind === '/') {
    // #2213: search the SEARCHABLE set, not the card-bounded `playbooks` — the
    // latter stops at 24 (the hint-grid bound), which made every skill past it
    // unmatchable by name with nothing on screen saying so.
    return {
      kind: '/',
      enabled: true,
      ...filterPlaybookCandidates(playbookSearchSource(props.agent), t.query),
    }
  }
  return {
    kind: '@',
    ...filterAgentCandidates(props.roster, t.query, {
      exclude: [props.agent?.name],
      // #2128: with no rooms substrate an @mention is deliberately ordinary
      // text, so offering a picker for it is a dead-end affordance. The gate
      // lives in the pure filter, so it is tested rather than grepped.
      enabled: store.multiAgentChatAvailable,
    }),
  }
})

const typeaheadBound = computed(() => boundCandidates(typeaheadResult.value?.items || []))
// #2213: rows the popup could not RENDER are `typeaheadBound.overflow`; skills that
// never reached the client at all are this. Two different omissions, so the popup
// is told about them separately rather than adding them into one misleading number.
const typeaheadHidden = computed(() => {
  // Review finding: only on a BARE `/`. Mid-query the same number reads as "N more
  // results matching what you typed", which is false — these are entries that never
  // reached the browser and no amount of typing will surface them. On a bare trigger
  // it reads correctly as a property of the list.
  if (typeaheadResult.value?.kind !== '/') return 0
  if (typeaheadTrigger.value?.query) return 0
  return hiddenPlaybookCount(props.agent, playbookSearchSource(props.agent).length)
})

// The two empty conditions are NOT the same: a source with nothing in it shows
// one honest line, while a query that matches nothing CLOSES the popup (AC#6 —
// the user is writing "50/50", not picking). Restricting the line to a bare
// trigger keeps a playbook-less agent from floating a panel over the rest of the
// message as they keep typing.
const typeaheadEmpty = computed(() => {
  const t = typeaheadTrigger.value
  const r = typeaheadResult.value
  if (!t || !r || r.items.length || t.query !== '') return null
  return typeaheadEmptyMessage(r.kind, r)
})

// A computed, not an imperative ref: it self-heals when the roster refreshes,
// the capability flips, or playbooks arrive late.
const typeaheadOpen = computed(() => {
  const t = typeaheadTrigger.value
  const r = typeaheadResult.value
  if (!t || !r || r.enabled === false) return false
  if (isSuppressed(dismissed.value, t)) return false
  return typeaheadBound.value.visible.length > 0 || !!typeaheadEmpty.value
})

const typeaheadRows = computed(() => typeaheadBound.value.visible.map((c, i) => (
  typeaheadKind.value === '/'
    ? { key: `pb-${i}-${c.title}`, primary: c.title, secondary: c.description || '' }
    // The slug rides along beside the label: it IS the token, and teaching it is
    // half of why this exists.
    : { key: `ag-${c.name}`, primary: agentDisplayName(c), secondary: `@${c.name}` }
)))

// The selection has to follow the list. A stale index is DROPPED rather than
// clamped to a neighbour — "whatever is now at index 5" is not the row the user
// chose, and inserting it is how the wrong agent (or `@undefined`) ships.
watch(typeaheadBound, (b) => { activeIndex.value = clampActiveIndex(activeIndex.value, b.visible.length) })

// The only part of this change that reaches a user who does not already know the
// feature exists. `@` is advertised only with the capability — a placeholder
// promising something the build cannot do is the #2128 dead end in text form.
const composerPlaceholder = computed(() => {
  if (listening.value) return 'Listening…'
  const base = `Message ${agentDisplayName(props.agent)}…  ·  / for playbooks`
  return store.multiAgentChatAvailable ? `${base}  ·  @ to add an agent` : base
})

function closeTypeahead() {
  typeaheadTrigger.value = null
  activeIndex.value = -1
}

// ONLY Esc arms the sentinel. A click-outside, an agent switch or a capability
// flip closes without arming — otherwise "type @, click away to read something,
// click back, keep typing" stays suppressed until the @ is deleted.
function dismissTypeahead() {
  dismissed.value = nextDismissState(typeaheadTrigger.value)
  closeTypeahead()
}

// Called from every path that writes `input.value` PROGRAMMATICALLY — send(),
// the prefill watcher, the thread switch, and both dictation handlers. None of
// them fires an input event, so without this a sentinel armed while composing
// message 1 kills the popup for every later message that starts the same way: a
// feature that is dead for the rest of the session.
function resetTypeahead() {
  closeTypeahead()
  dismissed.value = null
}

function refreshTypeahead(el) {
  if (!el) return
  // Read the EVENT TARGET, never the v-model ref: reading the ref makes
  // correctness depend on Vue's internal listener ordering, which is true today
  // and an implementation detail.
  typeaheadTrigger.value = detectTypeaheadTrigger(el.value, el.selectionStart, el.selectionEnd)
  if (!typeaheadTrigger.value) activeIndex.value = -1
}

function onComposerInput(e) {
  autoGrow()
  // A paste that happens to end in a token must not open a popup nobody asked
  // for, whose very next keystroke is Enter.
  if (e?.inputType === 'insertFromPaste' || e?.inputType === 'insertFromDrop') {
    closeTypeahead()
    return
  }
  refreshTypeahead(e?.target)
}

// The caret moves with no input event — a click, a drag-select — and accepting
// against bounds computed for where it used to be splices over the wrong text.
function onComposerCaret(e) { refreshTypeahead(e?.target) }

function onComposerKeydown(e) {
  const length = typeaheadBound.value.visible.length
  switch (resolveComposerKey({
    key: e.key,
    shiftKey: e.shiftKey,
    ctrlKey: e.ctrlKey,
    metaKey: e.metaKey,
    altKey: e.altKey,
    isComposing: e.isComposing,
    keyCode: e.keyCode,
    open: typeaheadOpen.value,
    hasActive: activeIndex.value >= 0,
    hasCandidates: length > 0,
  })) {
    case 'move-down': e.preventDefault(); activeIndex.value = nextActiveIndex(activeIndex.value, 1, length); break
    case 'move-up': e.preventDefault(); activeIndex.value = nextActiveIndex(activeIndex.value, -1, length); break
    case 'accept': e.preventDefault(); acceptActive(activeIndex.value >= 0 ? activeIndex.value : 0); break
    case 'dismiss': e.preventDefault(); dismissTypeahead(); break
    case 'close': closeTypeahead(); break
    case 'send': e.preventDefault(); send(); break
    default: break
  }
}

function acceptActive(index) {
  const t = typeaheadTrigger.value
  const row = typeaheadBound.value.visible[index]
  if (!t || !row) return                  // never insert `@undefined`
  const insert = t.kind === '/' ? starterFor(row) : buildMentionToken(row.name)
  const { value, caret } = applyTypeaheadInsert(input.value, t, insert)
  input.value = value
  closeTypeahead()
  // A pick that lands mid-sentence leaves the caret inside the token it just
  // inserted, and the setSelectionRange() below fires a `select` that would
  // re-detect it — the popup reopening on top of its own successful choice.
  // Suppress exactly that token; editing it back re-arms.
  const settled = dismissAfterInsert(value, caret)
  if (settled) dismissed.value = settled
  nextTick(() => {
    const el = textarea.value
    // focus() BEFORE setSelectionRange(): Safari resets and scrolls the
    // selection when focusing a textarea that did not previously have focus.
    if (el) { el.focus(); el.setSelectionRange(caret, caret) }
    autoGrow()
  })
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
      // ent#155: this is the first moment a Stop is possible — before it there
      // is no turn to stop, and a control offered earlier would be a lie.
      activeExecutionId.value = started.execution_id || null
      lastDeliveredExecutionId.value = started.execution_id || null
      pendingUserText.value = text
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
        started.wait_budget_seconds, dispatchedAt, started.execution_id,
      )
      if (data?.failed) {
        // #2320: the server diagnosed this turn. Say what it said, and offer a
        // Retry only where the server states nothing reached the agent — the
        // #2120/#2133 rule is about turns that MAY HAVE RUN AND BILLED, and a
        // verdict of "never started" is precisely the evidence that rule always
        // lacked.
        return { failed: true, error: data.outcome.message,
                 retryable: data.outcome.retryable === true }
      }
      if (data?.lost && data.idle) {
        // The server reports nothing running, and offered no verdict either.
        // Distinct from the budget case below: nothing is still going, so
        // "it may still finish" would be a fabrication. Still no Retry —
        // `mark_turn_inflight` no-ops when Redis is down, so a perfectly
        // healthy, already-billed turn reaches this branch on its first poll.
        return { lost: true, retryable: false,
                 error: 'The agent did not reply. Check the conversation in a moment.' }
      }
      if (data?.lost) {
        // #2133: we ran out of budget while the marker still claimed a turn.
        // That turn probably RAN and was billed — we merely lost sight of it.
        // So this is reported without a Retry: re-sending is the one action
        // guaranteed to be wrong.
        return { lost: true, retryable: false, error: "Still no reply — we've lost track of this turn. It may still finish; check the conversation shortly." }
      }
      if (!data) {
        // Defensive only: every return above is enumerated. Kept non-retryable
        // because an unclassifiable outcome must take the unprivileged answer.
        // (Before #2320 this branch was unreachable AND retryable — #2150
        // believed it preserved a Retry for a genuine no-answer, but
        // `awaitPersistedReply` never returned null, so `idle` fell into the
        // lost-track message above instead. That is the bug #2320 reported.)
        return { lost: true, retryable: false,
                 error: 'The agent did not reply. Check the conversation in a moment.' }
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
    // ent#365: a turn is the only thing that can produce a deliverable in this
    // chat, so the card list is re-read exactly then — no poll, and nothing to
    // refresh on a conversation nobody is talking in.
    deliverableTick.value += 1
    attachments.value = []
    return true
  } catch (err) {
    return { error: deliveryFailureReason(err) }
  } finally {
    sending.value = false
    clearInterval(elapsedTimer)
    activeExecutionId.value = null
    pendingUserText.value = ''
    cancelling.value = false
    await scrollDown()
  }
}

// ent#155: stop the turn, give the words back. The server keeps the cancel
// semantics the rest of the platform uses (CANCELLED, CAS-guarded), and the
// in-flight marker + resume lock are released by the turn's own `finally`, so
// there is nothing to unwind here.
// Escape stops the turn — but only when nothing else owns Escape. The
// typeahead uses it to dismiss (`resolveComposerKey` → 'dismiss'/'close') and
// the voice loop uses it to leave, so both are listed as overlays; the rule
// itself is pure and lives in `utils/turnCancel.js`.
function onEscapeKeydown(event) {
  if (!shouldCancelOnEscape(event, {
    inFlight: canCancelTurn.value,
    cancelling: cancelling.value,
    // Only the typeahead owns Escape on this surface today. (The voice-loop
    // overlay belongs to ent#440, which is not in `dev` — referencing it here
    // threw a ReferenceError on every Escape keydown, in flight or not, which
    // made Escape-to-cancel completely dead in the Workspace. Re-add that entry
    // when ent#440 lands.)
    overlays: [typeaheadOpen.value],
  })) return
  event.preventDefault()
  cancelTurn()
}

async function cancelTurn() {
  const executionId = activeExecutionId.value
  if (!executionId || cancelling.value) return
  cancelling.value = true
  cancelError.value = ''
  const restoreText = pendingUserText.value
  try {
    const res = await store.cancelPortalTurn(props.agent.name, executionId)
    const outcome = cancelOutcome({ ok: true, alreadyTerminal: isNoopCancel(res?.status) })
    if (outcome.kind === 'cancelled') {
      cancelledExecutionIds.value.add(executionId)
      input.value = restoreDraft(restoreText, input.value)
      autoGrowAfterUpdate()
    }
  } catch (err) {
    // A 404 is the lost race (the row went terminal, or the agent no longer
    // holds the turn), not a refusal — say nothing.
    if (err?.response?.status === 404) {
      cancelling.value = false
      return
    }
    // Still running, still spending — say so and leave the composer alone.
    cancelError.value = cancelOutcome({ ok: false, alreadyTerminal: false }).message
    cancelling.value = false
  }
}

// ent#286 — live turn state. `liveActivity` holds a short, human-readable trail
// of what the agent is doing right now; it is transient and never persisted.
const streaming = ref(false)
const liveActivity = ref([])
// Bumped after each completed turn; `PortalDeliverables` watches it.
const deliverableTick = ref(0)
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

// #2133/#2214: the absolute ceiling, for when the marker is ORPHANED rather
// than merely slow — a hard backend kill skips the `finally` that clears it,
// and `except Exception` does not catch `CancelledError`.
//
// The server owns the turn timeout (per-agent since #2214) and sends the budget
// with the 202 (`wait_budget_seconds`) and with the history response on
// reattach (`in_flight_wait_budget_seconds` — the marker's remaining TTL). The
// pick lives in portalUtils' `resolveWaitBudgetMs`: a positive server budget
// wins, anything else falls back to a literal frozen at the pre-#2214 server
// bound — see its comment for why that literal must never chase the new
// arithmetic.

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

// #2320: `executionId` is the turn THIS call is waiting on. The outcome record
// is matched against it before it is believed — a thread can hold a verdict from
// an earlier turn, and reporting that one as this turn's failure would be a new
// way to lie about the same thing.
async function awaitPersistedReply(sessionId, baselineAssistants, budgetSeconds,
                                   dispatchedAtMs, executionId = null) {
  let idleSince = null
  // Measured from DISPATCH, not from when this function was reached. The
  // server's marker TTL starts ticking at dispatch, so a client clock that
  // starts after the stream breaks (which can be minutes later, on exactly the
  // orphaned-marker path this ceiling exists for) would outlive the marker —
  // and the marker's disappearance would be read as "nothing running" instead
  // of tripping the ceiling. The two clocks now share an origin.
  const startedAt = dispatchedAtMs || Date.now()
  const budgetMs = resolveWaitBudgetMs(budgetSeconds)
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
    // #2320: the server told us how this turn ended. Authoritative regardless
    // of the marker — a verdict naming THIS execution means it is over — and
    // read before the idle timer so a diagnosed failure is reported at once
    // instead of after a 6s wait that pretends we do not know.
    const outcome = data.lastTurnOutcome
    if (outcome && executionId && outcome.execution_id === executionId) {
      return { failed: true, outcome }
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
  // The composer is about to be cleared programmatically, which fires no input
  // event — so the popup and its Esc sentinel are cleared here rather than left
  // armed against a message that no longer exists.
  resetTypeahead()

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
      autoGrowAfterUpdate()
      emit('escalate-to-room', { agents: [props.agent.name, ...others], message: text })
      return
    }
  }

  const index = messages.value.push({ role: 'user', content: text, failed: false, error: null }) - 1
  input.value = ''
  autoGrowAfterUpdate()
  await scrollDown()
  // A stale "couldn't stop the turn" must not outlive the turn it described.
  cancelError.value = ''
  const res = await deliver(text)
  // #2320: `retryable` when `deliver` decided it (a server verdict, or a
  // give-up it enumerated); otherwise the pre-existing rule, which still covers
  // the one path `deliver` does not classify — a dispatch that threw, where
  // nothing reached the server and re-sending is correct.
  if (res !== true) {
    // A cancel the user asked for is not a failure. `markFailed` would strike
    // the message out in red and offer a Retry for a turn they deliberately
    // stopped — and the words are already back in the composer.
    if (lastDeliveredExecutionId.value && cancelledExecutionIds.value.has(lastDeliveredExecutionId.value)) {
      cancelledExecutionIds.value.delete(lastDeliveredExecutionId.value)
    } else {
      markFailed(index, text, res?.error, { retryable: res?.retryable ?? !res?.lost })
    }
  }
}

async function retry(i) {
  const msg = messages.value[i]
  if (!msg || sending.value) return
  const content = msg.content
  msg.failed = false
  msg.error = null
  const res = await deliver(content)
  if (res !== true) markFailed(i, content, res?.error, { retryable: res?.retryable ?? !res?.lost })
}

// ---- Voice: speak replies (TTS) + dictate (STT) — carried over from #78 -------
const SpeechRec = typeof window !== 'undefined' ? (window.SpeechRecognition || window.webkitSpeechRecognition) : null
const canRecord = typeof navigator !== 'undefined' && !!navigator.mediaDevices?.getUserMedia && typeof MediaRecorder !== 'undefined'
// #2212: the mic used to prefer the browser Web Speech API whenever the object
// merely existed — a Google-hosted service that reports nothing at all in
// Chromium (measured) and ends at the first pause everywhere. Recording + our
// own /stt wins when the platform can transcribe; see `resolveMicMode`.
const micMode = computed(() => resolveMicMode({
  speechApi: !!SpeechRec,
  canRecord,
  serverStt: !!props.agent.stt_available,
}))
// No mic button at all when neither path can work, so the control is never a
// dead affordance on an instance with no voice provider.
const sttSupported = computed(() => micMode.value !== null)
const ttsEnabled = computed(() => !!props.agent.voice_available)
// The one place any voice failure becomes words. Every path below sets it
// instead of returning silently, which is what made this read as "just dies".
const voiceError = ref('')

// #2157: the speaker choice sticks per client+agent instead of resetting to off
// on every page load. Narration was already hard to find — an agent that had
// just told the client this surface was "text-only" was the only hint it existed
// — and re-muting it every reload taught clients it had not really worked.
const voiceModeKey = computed(() => `trinity.portal.voiceMode.${props.agent?.name || ''}`)
function loadVoiceMode() {
  try { return localStorage.getItem(voiceModeKey.value) === '1' } catch { return false }
}
const voiceMode = ref(loadVoiceMode())
watch(voiceMode, (on) => {
  try { localStorage.setItem(voiceModeKey.value, on ? '1' : '0') } catch { /* private mode: session-only */ }
})
// Switching agents adopts that agent's own remembered choice.
watch(() => props.agent?.name, () => { stopSpeaking(); voiceError.value = ''; voiceMode.value = loadVoiceMode() })
const speaking = ref(false)
const listening = ref(false)
const transcribing = ref(false)
const micTitle = computed(() => (
  transcribing.value ? 'Transcribing…'
    : listening.value ? 'Listening… click to stop'
      : 'Speak your message'
))
const audioEl = ref(null)
let recog = null, mediaRec = null, mediaStream = null, recChunks = [], lastAudioUrl = null
let speechWatchdog = null

function revokeAudio() { if (lastAudioUrl) { URL.revokeObjectURL(lastAudioUrl); lastAudioUrl = null } }
async function speak(text) {
  if (!text) return
  speaking.value = true
  try {
    const url = await store.synthesizeTts(props.agent.name, text)
    // The store answers `null` for every failure shape, so this is the only
    // place narration can report that it did not happen (#2212).
    if (!url) { speaking.value = false; voiceError.value = TTS_FAILED_MESSAGE; return }
    revokeAudio(); lastAudioUrl = url
    if (audioEl.value) { audioEl.value.src = url; await audioEl.value.play() } else speaking.value = false
  } catch { speaking.value = false; voiceError.value = TTS_FAILED_MESSAGE }
}
function stopSpeaking() { if (audioEl.value) audioEl.value.pause(); speaking.value = false }
// Dictated text lands at the end of whatever is already typed — one place, so
// the two mic paths cannot drift on how a transcript is applied.
function appendTranscript(text) {
  const t = (text || '').trim()
  if (!t) return
  input.value = input.value ? `${input.value} ${t}` : t
  resetTypeahead(); autoGrowAfterUpdate()
}
function toggleMic() {
  if (!sttSupported.value || transcribing.value) return
  voiceError.value = ''
  micMode.value === 'speech' ? toggleSpeech() : toggleRecord()
}
function clearSpeechWatchdog() { if (speechWatchdog) { clearTimeout(speechWatchdog); speechWatchdog = null } }
function toggleSpeech() {
  if (listening.value) { try { recog?.stop() } catch { /* noop */ } return }
  recog = new SpeechRec()
  recog.lang = 'en-US'
  // `continuous` defaults to false — recognition ends at the first pause, which
  // on its own reads as "the mic died" mid-sentence (#2212).
  recog.continuous = true
  recog.interimResults = false
  recog.maxAlternatives = 1
  // Local to THIS attempt: a stale flag from the previous one would decide the
  // next attempt's verdict. `settle` runs once, whichever handler gets there
  // first — engines disagree on whether `error` is followed by `end`.
  let gotText = false, errorCode = null, timedOut = false, settled = false
  const settle = () => {
    if (settled) return
    settled = true
    clearSpeechWatchdog()
    listening.value = false
    const message = speechAttemptOutcome({ gotText, errorCode, timedOut })
    if (message) voiceError.value = message
  }
  recog.onstart = () => { clearSpeechWatchdog() }
  recog.onresult = (e) => {
    // With `continuous` the event carries only the results from `resultIndex`
    // on, and interim ones are filtered out by `isFinal`.
    let text = ''
    for (let i = e.resultIndex ?? 0; i < (e.results?.length || 0); i++) {
      const r = e.results[i]
      if (r?.isFinal) text += r[0]?.transcript || ''
    }
    if (!text.trim()) return
    gotText = true
    appendTranscript(text)
  }
  // The error CODE is the signal this component used to throw away; it is the
  // whole difference between "blocked permission" and "service unreachable".
  recog.onerror = (e) => { errorCode = e?.error || ''; settle() }
  recog.onend = () => settle()
  listening.value = true
  // Measured in Chromium: `start()` resolves and the engine then emits NO event
  // of any kind, so without this the button stays lit forever with no recourse.
  speechWatchdog = setTimeout(() => {
    speechWatchdog = null
    if (settled) return
    timedOut = true
    settle()
    try { recog?.abort() } catch { /* noop */ }
  }, SPEECH_START_TIMEOUT_MS)
  try { recog.start() } catch (e) {
    settled = true
    clearSpeechWatchdog()
    listening.value = false
    // `start()` throws InvalidStateError only when a session is already live.
    voiceError.value = e?.name === 'InvalidStateError'
      ? 'Dictation is already running — stop it and try again.'
      : speechErrorMessage('')
  }
}
async function toggleRecord() {
  if (listening.value) { try { mediaRec?.stop() } catch { /* noop */ } return }
  try { mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true }) }
  catch (e) { voiceError.value = recorderErrorMessage(e); return }
  recChunks = []
  try { mediaRec = new MediaRecorder(mediaStream) }
  catch (e) { stopStream(); voiceError.value = recorderErrorMessage(e); return }
  mediaRec.ondataavailable = (e) => { if (e.data && e.data.size) recChunks.push(e.data) }
  mediaRec.onerror = (e) => {
    listening.value = false; stopStream()
    voiceError.value = recorderErrorMessage(e?.error || e)
  }
  mediaRec.onstop = async () => {
    stopStream(); listening.value = false
    // Firefox leaves `mediaRec.mimeType` empty and puts the real type on the
    // chunks; mislabelling Ogg as WebM is a silent upload bug (#2212).
    const type = resolveRecordingMimeType(mediaRec?.mimeType, recChunks)
    const blob = new Blob(recChunks, { type }); recChunks = []
    if (blob.size < MIN_RECORDING_BYTES) { voiceError.value = RECORDING_TOO_SHORT_MESSAGE; return }
    transcribing.value = true
    try {
      const t = await store.transcribeStt(props.agent.name, blob)
      if (t) appendTranscript(t)
      else voiceError.value = TRANSCRIPT_EMPTY_MESSAGE
    } catch (e) {
      // /stt answers with a user-facing `detail`; it used to be swallowed.
      voiceError.value = transcriptionErrorMessage(e)
    } finally { transcribing.value = false }
  }
  listening.value = true
  try { mediaRec.start(200) } catch (e) { listening.value = false; stopStream(); voiceError.value = recorderErrorMessage(e) }
}
function stopStream() { try { mediaStream?.getTracks().forEach((t) => t.stop()) } catch { /* noop */ } mediaStream = null }
function cleanupVoice() {
  clearSpeechWatchdog()
  try { recog?.stop() } catch { /* noop */ }
  try { if (mediaRec && mediaRec.state !== 'inactive') mediaRec.stop() } catch { /* noop */ }
  stopStream(); stopSpeaking(); revokeAudio()
}

defineExpose({ focusComposer: () => textarea.value?.focus() })
</script>

<style scoped>
/* #2211: 8px between paragraphs, not 4px. At `text-sm` with the bubble's own
   padding, 4px read as a single dense block; 8px is the next step on the 4px
   grid the design contract defines (4 tight / 8 related / 12 grouped). */
.prose-portal :deep(p) { margin: 0.5rem 0; }
/* First and last paragraph must not double up with the bubble padding, or the
   looser rhythm reads as a lopsided bubble. */
.prose-portal :deep(p:first-child) { margin-top: 0; }
.prose-portal :deep(p:last-child) { margin-bottom: 0; }
.prose-portal :deep(pre) { overflow-x: auto; padding: 0.5rem; border-radius: 0.375rem; }
/* Token-based tint rather than an `rgba()` literal: the design contract
   forbids hardcoded colors, and the raw-color ratchet counts them. Applied
   via @apply so light/dark both come from the gray scale. */
.prose-portal :deep(pre) { @apply bg-gray-100 dark:bg-gray-800; }
.prose-portal :deep(code) { font-size: 0.8em; }
.prose-portal :deep(ul) { list-style: disc; padding-left: 1.25rem; }
.prose-portal :deep(a) { text-decoration: underline; }
</style>
