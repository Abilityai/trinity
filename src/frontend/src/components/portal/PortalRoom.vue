<template>
  <div class="flex flex-col h-full min-h-0">
    <!-- Header: who is in the room (ent#361 AC#2) -->
    <header class="shrink-0 flex items-center gap-2 px-3 sm:px-4 h-14 border-b border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900">
      <button class="sm:hidden -ml-1 p-2 text-gray-500 hover:text-gray-800 dark:hover:text-gray-200" aria-label="Menu" @click="$emit('open-menu')">
        <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16" /></svg>
      </button>

      <div class="min-w-0">
        <div class="font-semibold truncate text-sm">{{ room?.name || 'Chat' }}</div>
        <div class="flex items-center gap-1 mt-0.5">
          <PortalAvatar
            v-for="a in agentParticipants"
            :key="a"
            :name="a"
            :size="18"
            :title="a"
          />
          <span class="text-xs text-gray-500 dark:text-gray-400 truncate">
            {{ agentParticipants.join(', ') || 'no agents yet' }}
          </span>
        </div>
      </div>

      <!-- ent#381 AC#3: budget observability follows the room into workspace
           chrome. It lived only on the Sessions page (ParticipantsRail), and a
           room that closes at a cap with no prior warning reads as the agents
           going quiet. Shown from ~80% so there is time to react, not as a
           permanent gauge nobody asked for. -->
      <div v-if="budgetWarning" class="ml-auto mr-2 text-xs text-status-warning-600 dark:text-status-warning-400 truncate" :title="budgetWarning">
        {{ budgetWarning }}
      </div>

      <div class="flex items-center gap-1" :class="{ 'ml-auto': !budgetWarning }">
        <!-- ent#359 AC #4: star from the header, same as a 1:1. -->
        <PortalStarButton
          :starred="starred"
          @toggle="$emit('toggle-star', { id: roomId, is_room: true, starred })"
        />
        <button
          v-if="!isClosed"
          class="px-2 py-1.5 rounded-lg text-xs font-medium text-action-primary-600 hover:bg-gray-100 dark:hover:bg-gray-800 transition"
          title="Add another agent to this conversation"
          @click="addOpen = !addOpen"
        >+ Add agent</button>
      </div>
    </header>

    <!-- Add-agent picker (AC#3) -->
    <div v-if="addOpen" class="shrink-0 border-b border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-950 px-4 py-2">
      <p v-if="!addable.length" class="text-xs text-gray-500 dark:text-gray-400">
        Every agent shared with you is already here.
      </p>
      <div v-else class="flex flex-wrap gap-1.5">
        <button
          v-for="a in addable"
          :key="a.name"
          class="inline-flex items-center gap-1.5 rounded-full border border-gray-300 dark:border-gray-700 px-2.5 py-1 text-xs hover:bg-white dark:hover:bg-gray-900 transition disabled:opacity-50"
          :disabled="adding"
          @click="addAgent(a.name)"
        >
          <PortalAvatar :name="a.name" :size="16" />
          {{ a.name }}
        </button>
      </div>
      <p v-if="addError" class="mt-1.5 text-xs text-status-danger-600 dark:text-status-danger-400">{{ addError }}</p>
    </div>

    <!-- Transcript -->
    <div ref="scrollEl" class="flex-1 min-h-0 overflow-y-auto px-3 sm:px-6 py-5">
      <div class="max-w-4xl mx-auto space-y-6">
        <p v-if="loading" class="text-center text-sm text-gray-400">Loading…</p>

        <div v-for="m in messages" :key="m.seq">
          <!-- A system line is the room narrating itself: a join, a budget
               close, a wake that did not happen. It is not from a participant,
               so it renders as neither side of the conversation. -->
          <p v-if="m.kind === 'system'" class="text-center text-xs text-gray-400 dark:text-gray-500 py-1">
            {{ m.content }}
          </p>

          <div v-else-if="isMine(m)" class="flex justify-end">
            <div class="max-w-[85%] rounded-2xl rounded-br-md px-3.5 py-3 text-sm leading-relaxed whitespace-pre-wrap bg-action-primary-600 text-white">{{ m.content }}</div>
          </div>

          <div v-else class="flex items-start gap-2.5">
            <PortalAvatar :name="m.sender_identity" :size="28" class="mt-0.5" />
            <div class="max-w-[85%]">
              <div class="text-xs text-gray-500 dark:text-gray-400 mb-0.5">{{ m.sender_identity }}</div>
              <!-- #2515: the sender label stays out here (a room row is
                   attributed, a 1:1 is not); the bubble itself is the shared
                   component, so both transcripts render agent markdown the
                   same way by construction rather than by two copies kept in
                   step by a comment. -->
              <PortalAgentBubble :content="m.content" />
            </div>
          </div>
        </div>

        <!-- Who is thinking. Derived from the SERVER (`room.working`), not just
             the local send, so a client that reloaded mid-turn still sees it —
             a reload used to make the dots vanish while two agents were still
             working, which reads as the room having given up. -->
        <!-- ent#525: the live card per working agent, from the Work feed the
             shell owns — status, elapsed, steps where the agent publishes
             them. Falls back to the server-derived line below until the feed
             has the rows, so a reload never shows a room that gave up. -->
        <div v-if="roomLiveItems.length" class="space-y-2" data-testid="portal-room-work">
          <div v-for="it in roomLiveItems" :key="it.id" class="flex items-start gap-2.5">
            <PortalAvatar :name="it.agent_name" :size="28" class="mt-0.5" />
            <PortalWorkCard :item="it" show-agent :elapsed-seconds="elapsedOf(it)" show-open-in-work @open-work="emit('open-work')" />
          </div>
        </div>
        <div v-else-if="workingAgents.length" class="flex items-start gap-2.5">
          <PortalAvatar :name="workingAgents[0]" :size="28" class="mt-0.5" />
          <div class="rounded-2xl rounded-bl-md bg-gray-100 dark:bg-gray-800 px-3.5 py-2.5 flex items-center gap-2">
            <span class="inline-flex gap-1">
              <span class="w-1.5 h-1.5 rounded-full bg-gray-400 animate-bounce" style="animation-delay:0ms"></span>
              <span class="w-1.5 h-1.5 rounded-full bg-gray-400 animate-bounce" style="animation-delay:150ms"></span>
              <span class="w-1.5 h-1.5 rounded-full bg-gray-400 animate-bounce" style="animation-delay:300ms"></span>
            </span>
            <span class="text-xs text-gray-500 dark:text-gray-400">
              {{ workingAgents.length === 1 ? `${workingAgents[0]} is thinking…`
                : `${workingAgents.join(', ')} are thinking…` }}
            </span>
          </div>
        </div>
        <div v-else-if="sending" class="flex items-start gap-2.5">
          <span class="inline-flex gap-1 mt-3">
            <span class="w-1.5 h-1.5 rounded-full bg-gray-400 animate-bounce" style="animation-delay:0ms"></span>
            <span class="w-1.5 h-1.5 rounded-full bg-gray-400 animate-bounce" style="animation-delay:150ms"></span>
            <span class="w-1.5 h-1.5 rounded-full bg-gray-400 animate-bounce" style="animation-delay:300ms"></span>
          </span>
        </div>
      </div>
    </div>

    <!-- ent#474: the rail's mobile collapsed form — see PortalConversation. -->
    <slot name="rail-strip" />

    <!-- Composer -->
    <div class="shrink-0 border-t border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 px-3 sm:px-6 py-3">
      <div class="max-w-4xl mx-auto">
        <!-- A closed room is a dead end unless it SAYS so. Rooms end on their
             own (message budget, cost cap, TTL) — silence would read as the
             agents having stopped answering. -->
        <p v-if="isClosed" class="text-xs text-center text-gray-500 dark:text-gray-400 py-2">
          This conversation has ended{{ room?.stop_reason ? ` (${closedReason})` : '' }}. Start a new chat to keep going.
        </p>
        <form v-else class="flex items-end gap-2" @submit.prevent="send">
          <!-- ent#392: `@` typeahead over the room's WAKE-SET. Same anchored
               wrapper as the 1:1 composer; it must carry the flex sizing the
               textarea used to hold, or the field collapses to content width.

               #2259: and the same `block` on the textarea. A `<textarea>` is
               inline-block, so in this block wrapper it sat on the baseline and
               the line box reserved 6px below it; `items-end` then aligned Send
               to that dead space instead of to the visible input edge. Twin of
               PortalConversation — the two composers are the same markup in two
               files, so a fix landing in only one silently keeps the bug. -->
          <div ref="composerWrap" class="relative flex-1 min-w-0">
            <PortalTypeahead
              v-if="typeaheadOpen"
              kind="@"
              :rows="typeaheadRows"
              :active-index="activeIndex"
              :overflow="typeaheadBound.overflow"
              :empty-message="typeaheadEmpty || ''"
              @pick="acceptActive"
              @hover="activeIndex = $event"
            />
            <textarea
              ref="textarea"
              v-model="input"
              rows="1"
              :placeholder="placeholder"
              class="block w-full resize-none rounded-2xl border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-800 text-sm px-4 py-2.5 leading-6 focus:ring-2 focus:ring-action-primary-500/40 focus:border-action-primary-500 focus:outline-none max-h-40"
              @keydown="onComposerKeydown"
              @input="onComposerInput"
              @click="onComposerCaret"
              @select="onComposerCaret"
            ></textarea>
          </div>
          <button
            type="submit"
            class="shrink-0 h-11 w-11 flex items-center justify-center rounded-xl bg-action-primary-600 hover:bg-action-primary-700 text-white disabled:opacity-40 transition"
            :disabled="!input.trim() || sending"
            title="Send"
          >
            <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 12h14M12 5l7 7-7 7" /></svg>
          </button>
        </form>
        <p v-if="sendError" class="mt-1.5 text-xs text-status-danger-600 dark:text-status-danger-400">{{ sendError }}</p>
      </div>
    </div>
  </div>
</template>

<script setup>
/**
 * A multi-agent chat, backed by a room (ent#361).
 *
 * Sibling of PortalConversation, not a replacement: a one-agent chat stays a
 * portal thread because that path resumes, streams and reattaches
 * (ent#358/#286). This one exists because those things do not model SEVERAL
 * agents — a room does, with @mention-waking, per-participant budgets, and a
 * seq-ordered shared transcript.
 *
 * Two things a thread never has to express, and this must:
 *   * the room can END on its own (message budget, cost cap, TTL), and a closed
 *     room that says nothing reads as the agents having gone quiet;
 *   * a reply arrives per WOKEN AGENT, so the transcript grows by more than one
 *     message per turn and the poll is how the client learns that.
 *
 * Polling rather than streaming is deliberate for now: ent#286 hands a client
 * ONE execution id, and a room turn wakes N agents. Merging N live streams is
 * its own design; until then the seq cursor is the honest mechanism.
 */
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import PortalAgentBubble from './PortalAgentBubble.vue'
import PortalWorkCard from './PortalWorkCard.vue'
import { usePortalWorkStore } from '@/stores/portalWork'
import { liveElapsedSeconds } from './portalWork'
import PortalAvatar from './PortalAvatar.vue'
import PortalStarButton from './PortalStarButton.vue'
import PortalTypeahead from './PortalTypeahead.vue'
import { workSignalFromRoom } from './portalRail'
import {
  applyTypeaheadInsert,
  boundCandidates,
  resolveComposerGrowth,
  buildMentionToken,
  clampActiveIndex,
  detectTypeaheadTrigger,
  dismissAfterInsert,
  filterAgentCandidates,
  isSuppressed,
  nextActiveIndex,
  nextDismissState,
  resolveComposerKey,
  roomMentionSource,
  typeaheadEmptyMessage,
} from './portalUtils'
import { agentDisplayName } from '@/utils/agentName'

const props = defineProps({
  roomId: { type: String, required: true },
  roster: { type: Array, default: () => [] },
  // ent#359: star state is per-viewer and owned by the shell, not by the room —
  // a room is shared, a star is not.
  starred: { type: Boolean, default: false },
  // ent#475: text to seed the composer with — the rail's "Ask for a canvas"
  // pre-fills, never sends. Same contract as `PortalConversation`'s.
  prefill: { type: String, default: '' },
})
const emit = defineEmits(['open-menu', 'rooms-changed', 'toggle-star', 'participants-changed', 'work-state', 'open-work'])

const store = useClientPortalStore()

const room = ref(null)
const messages = ref([])
const loading = ref(true)
const sending = ref(false)
const sendError = ref(null)
const input = ref('')
const scrollEl = ref(null)
const addOpen = ref(false)
const adding = ref(false)
const addError = ref(null)

// ent#392 — composer typeahead state (`@` only; see the block below for why
// there is no `/` here).
const textarea = ref(null)

// ent#475 — a prefill lands in the composer and focuses it; the person
// decides whether to send. Mirrors `PortalConversation`'s watcher.
watch(() => props.prefill, (v) => {
  if (v) { input.value = v; nextTick(() => { autoGrow(); textarea.value?.focus() }) }
})
const composerWrap = ref(null)
const typeaheadTrigger = ref(null)
const activeIndex = ref(-1)
const dismissed = ref(null)

let pollTimer = null
const POLL_MS = 3000

const agentParticipants = computed(() =>
  (room.value?.participants || []).filter((p) => p.kind === 'agent' && !p.left_at).map((p) => p.identity)
)
const isClosed = computed(() => room.value?.status === 'closed')

// Server-reported, so it survives a reload. The local `sending` flag still
// covers the gap between posting and the first poll, when nobody has been
// marked working yet.
const workingAgents = computed(() => room.value?.working || [])

// ent#525: the feed's live rows for the agents the SERVER says are working —
// the card needs both facts, so a stale feed row on an idle agent never
// draws a card the room's own poll contradicts.
const workStore = usePortalWorkStore()
const roomLiveItems = computed(() => workStore.live.filter((it) => it.agent_name && workingAgents.value.includes(it.agent_name)))
const clockMs = ref(Date.now())
let clockTimer = null
watch(() => roomLiveItems.value.length > 0, (on) => {
  if (on && !clockTimer) clockTimer = setInterval(() => { clockMs.value = Date.now() }, 1000)
  if (!on && clockTimer) { clearInterval(clockTimer); clockTimer = null }
}, { immediate: true })
onBeforeUnmount(() => { if (clockTimer) clearInterval(clockTimer) })
function elapsedOf(it) { return liveElapsedSeconds(it, { fetchedAtMs: workStore.fetchedAt, nowMs: clockMs.value }) }

// ent#474 — the shell scopes the rail to the room's participants and derives
// its Work signal from the SERVER's `working` list (never a local flag), so
// both survive a reload and follow the room's own poll — live push degrades
// to poll, never to a stuck indicator.
watch(agentParticipants, (list) => emit('participants-changed', list), { immediate: true, deep: true })
watch(workingAgents, (list) => emit('work-state', workSignalFromRoom(list)), { immediate: true, deep: true })

// ent#381: the near-limit signal the Sessions page used to own. Same 80%
// threshold, same two budgets — messages and cost — so a client sees a room
// approaching its end rather than discovering it after the fact.
const BUDGET_WARN_AT = 0.8

const budgetWarning = computed(() => {
  const r = room.value
  if (!r || r.status !== 'open') return null
  const used = r.message_count ?? 0
  const maxMsgs = r.max_messages ?? 0
  if (maxMsgs && used / maxMsgs >= BUDGET_WARN_AT) {
    return `${used}/${maxMsgs} messages`
  }
  const cost = r.cost ?? 0
  const maxCost = r.max_cost_usd ?? 0
  if (maxCost && cost / maxCost >= BUDGET_WARN_AT) {
    return `$${cost.toFixed(2)}/$${maxCost.toFixed(2)}`
  }
  return null
})

const closedReason = computed(() => ({
  max_messages: 'message limit reached',
  max_cost: 'cost limit reached',
  expired: 'timed out',
  user_closed: 'closed',
}[room.value?.stop_reason] || room.value?.stop_reason))

// AC#6: the placeholder names who is actually here, so it is obvious that a
// message goes to several agents and which @name will reach whom.
const placeholder = computed(() => {
  const names = agentParticipants.value
  if (!names.length) return 'Message…'
  if (names.length === 1) return `Message ${names[0]}…`
  return `Message ${names.join(', ')} — @name to wake one`
})

// Agents shared with the caller who are not already participants.
const addable = computed(() => {
  const here = new Set(agentParticipants.value)
  return (props.roster || []).filter((a) => !here.has(a.name))
})

// ---- ent#392: `@` typeahead over the room's wake-set ------------------------
//
// @mention-waking is first-class here and completely undiscoverable — a room has
// many counterparts, so it needs the affordance more than a 1:1 does. The
// candidate list is the AGENT PARTICIPANTS, never the roster, and that is not an
// assumption: posting `@<participant>` to a live instance answered
// {"mentions":["acme-scout"],"woke":["acme-scout"]} while `@<non-participant>`
// answered {"mentions":[],"woke":[]}. Participants demonstrably wake; a
// non-participant demonstrably wakes nobody on that turn — so offering the
// roster would put names in front of the user with no evidence that choosing
// one does anything, the same class of silent no-op as offering a slug the
// grammar cannot carry. See roomMentionSource() in portalUtils.js for what this
// deliberately does not claim about recruiting.
//
// There is deliberately NO `/` typeahead: a room has N participants and no
// active agent, so "whose playbooks?" has no answer without inventing a picker
// this issue does not specify. Stated as a scope limitation (AC#9).
//
// Recruiting a NEW agent stays with the explicit "+ Add agent" control, which is
// the honest place for it — that spends money on another agent, and the probe
// above saw a non-participant mention wake nobody. Whether it recruits by some
// other path is OPEN, not settled here (requirements §5.12 records an
// engine-side newcomer-join from ent#361).

const mentionSource = computed(() => roomMentionSource(agentParticipants.value, props.roster))

const typeaheadResult = computed(() => {
  const t = typeaheadTrigger.value
  if (!t || t.kind !== '@') return null
  return filterAgentCandidates(mentionSource.value, t.query)
})

const typeaheadBound = computed(() => boundCandidates(typeaheadResult.value?.items || []))

const typeaheadEmpty = computed(() => {
  const t = typeaheadTrigger.value
  const r = typeaheadResult.value
  if (!t || !r || r.items.length || t.query !== '') return null
  return typeaheadEmptyMessage('@', r, { scope: 'room' })
})

const typeaheadOpen = computed(() => {
  const t = typeaheadTrigger.value
  const r = typeaheadResult.value
  if (!t || !r || isClosed.value) return false
  if (isSuppressed(dismissed.value, t)) return false
  return typeaheadBound.value.visible.length > 0 || !!typeaheadEmpty.value
})

const typeaheadRows = computed(() => typeaheadBound.value.visible.map((a) => ({
  key: `ag-${a.name}`,
  primary: agentDisplayName(a),
  secondary: `@${a.name}`,
})))

watch(typeaheadBound, (b) => { activeIndex.value = clampActiveIndex(activeIndex.value, b.visible.length) })

function closeTypeahead() {
  typeaheadTrigger.value = null
  activeIndex.value = -1
}

function dismissTypeahead() {
  dismissed.value = nextDismissState(typeaheadTrigger.value)
  closeTypeahead()
}

// Every programmatic write to `input.value` — send() clearing it, and send()
// giving it back on failure — fires no input event, so the sentinel is cleared
// here or it outlives the message it was armed against.
function resetTypeahead() {
  closeTypeahead()
  dismissed.value = null
}

function refreshTypeahead(el) {
  if (!el) return
  const t = detectTypeaheadTrigger(el.value, el.selectionStart, el.selectionEnd)
  // A room has no `/` surface, so a `/` trigger is simply not a trigger here —
  // dropped at the edge rather than filtered downstream, so nothing below has to
  // remember that this composer is single-kind.
  typeaheadTrigger.value = t && t.kind === '@' ? t : null
  if (!typeaheadTrigger.value) activeIndex.value = -1
}

// #2211: the room composer had NO auto-grow at all — the issue assumed this file
// carried a copy of `PortalConversation`'s `autoGrow()`, and it does not. So the
// composer never grew past its single `rows="1"` line: a long message scrolled
// inside a one-line box, which is a worse version of the symptom reported next
// door. It gets the CORRECTED implementation rather than a copy of the buggy one.
//
// `scrollHeight` EXCLUDES the border under `box-sizing: border-box`, and this
// textarea has a 1px border per side — assigning it directly leaves the field 2px
// short of the line it must hold, which is what put a scrollbar in an EMPTY
// composer. The border box is measured, not hardcoded, so a future `border-2`
// cannot silently reintroduce it.
const COMPOSER_MAX_PX = 160   // matches the `max-h-40` class on the textarea

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


function onComposerInput(e) {
  // Review finding: this must run BEFORE the paste/drop early-return. Pasting a long
  // message otherwise left the box one line tall — and with `overflow-y` now managed,
  // a prior keystroke's `hidden` would leave the pasted text clipped AND unscrollable.
  autoGrow()
  if (e?.inputType === 'insertFromPaste' || e?.inputType === 'insertFromDrop') {
    closeTypeahead()
    return
  }
  refreshTypeahead(e?.target)
}

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
  if (!t || !row) return
  const { value, caret } = applyTypeaheadInsert(input.value, t, buildMentionToken(row.name))
  input.value = value
  closeTypeahead()
  // See PortalConversation: a mid-sentence pick leaves the caret inside the
  // token, and setSelectionRange() fires a `select` that would reopen the popup
  // over its own successful choice.
  const settled = dismissAfterInsert(value, caret)
  if (settled) dismissed.value = settled
  nextTick(() => {
    const el = textarea.value
    if (el) { el.focus(); el.setSelectionRange(caret, caret) }
    // Review finding: PortalConversation regrows here and this file did not, so a
    // mention insert that wrapped to a second line left the field one line tall.
    autoGrow()
  })
}

// Outside the composer and its popup closes without arming the Esc sentinel.
function onDocClick(e) {
  if (typeaheadTrigger.value && composerWrap.value && !composerWrap.value.contains(e.target)) closeTypeahead()
}

function isMine(m) {
  // Anything not an agent and not the room itself is this client — the room is
  // per-client here, so a human sender is the caller.
  return m.sender_kind !== 'agent' && m.kind !== 'system'
}

// Only ONE load at a time. The 3s poll can otherwise overlap a send() or
// addAgent() load: both read the same `since` cursor, both receive the same
// messages, and both concat them — duplicate bubbles and duplicate Vue keys on
// one seq, self-correcting only on a full reload.
let loading_ = false

async function load({ full = false } = {}) {
  if (loading_ && !full) return
  loading_ = true
  try {
    const since = full ? 0 : (messages.value.length ? messages.value[messages.value.length - 1].seq : 0)
    const data = await store.fetchRoom(props.roomId, since)
    room.value = data
    const incoming = data.messages || []
    if (full) messages.value = incoming
    else if (incoming.length) messages.value = messages.value.concat(incoming)
    if (incoming.length) await scrollDown()
  } catch (err) {
    if (full) sendError.value = 'Could not load this conversation.'
  } finally {
    loading_ = false
    loading.value = false
  }
}

async function send() {
  const text = input.value.trim()
  if (!text || sending.value) return
  sending.value = true
  sendError.value = null
  input.value = ''
  autoGrowAfterUpdate()   // deferred: Vue patches the textarea next tick
  resetTypeahead()
  try {
    await store.postRoomMessage(props.roomId, text)
    // The post returns once the mentioned agents have been woken; their replies
    // land as further messages, which the poll picks up.
    await load()
  } catch (err) {
    const detail = err?.response?.data?.detail
    sendError.value = detail?.message || (typeof detail === 'string' ? detail : null)
      || 'That message was not delivered.'
    input.value = text        // give it back rather than losing what they typed
    resetTypeahead()
  } finally {
    sending.value = false
    await scrollDown()
  }
}

async function addAgent(name) {
  adding.value = true
  addError.value = null
  try {
    await store.addRoomParticipant(props.roomId, name)
    addOpen.value = false
    await load({ full: true })   // the join is recorded in the transcript
  } catch (err) {
    const detail = err?.response?.data?.detail
    addError.value = detail?.message || (typeof detail === 'string' ? detail : null)
      || `Could not add ${name}.`
  } finally {
    adding.value = false
  }
}

async function scrollDown() {
  await nextTick()
  if (scrollEl.value) scrollEl.value.scrollTop = scrollEl.value.scrollHeight
}

function startPolling() {
  stopPolling()
  pollTimer = setInterval(() => {
    // Nothing to wait for once the room is closed.
    if (!isClosed.value && !document.hidden) load()
  }, POLL_MS)
}
function stopPolling() {
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = null
}

watch(() => props.roomId, async () => {
  messages.value = []
  loading.value = true
  resetTypeahead()
  await load({ full: true })
  // Same reason as on mount: switching rooms can swap a closed room for an open
  // one, which mounts a fresh textarea that has never been measured.
  autoGrowAfterUpdate()
})

onMounted(async () => {
  document.addEventListener('click', onDocClick)
  window.addEventListener('resize', onViewportResize)
  await load({ full: true })
  // #2259: and only NOW — the composer sits behind `v-if="!isClosed"`, so before
  // the room resolves there is no textarea to measure and an eager call would
  // silently no-op on the null ref. Without this the field mounts with `overflow-y`
  // still at its stylesheet `auto`: the very state #2211 replaced with an explicit
  // `hidden` ("the scrollbar must be gone rather than merely unused"). It is
  // invisible at rest only because the untouched `rows="1"` box happens to fit its
  // one line — a coincidence, not the contract.
  autoGrowAfterUpdate()
  startPolling()
})
// Review finding: `overflow-y` is now pinned, so the height must be recomputed when
// the box REWRAPS — narrowing the window (or opening a drawer) makes a fitting draft
// taller, and a stale `hidden` would leave that text invisible and unscrollable
// where it previously scrolled. Cheap: one listener, only while mounted.
function onViewportResize() {
  autoGrow()
}

onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
  window.removeEventListener('resize', onViewportResize)
  stopPolling()
})
</script>
