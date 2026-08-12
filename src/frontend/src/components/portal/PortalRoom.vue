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
      <div class="max-w-3xl mx-auto space-y-4">
        <p v-if="loading" class="text-center text-sm text-gray-400">Loading…</p>

        <div v-for="m in messages" :key="m.seq">
          <!-- A system line is the room narrating itself: a join, a budget
               close, a wake that did not happen. It is not from a participant,
               so it renders as neither side of the conversation. -->
          <p v-if="m.kind === 'system'" class="text-center text-xs text-gray-400 dark:text-gray-500 py-1">
            {{ m.content }}
          </p>

          <div v-else-if="isMine(m)" class="flex justify-end">
            <div class="max-w-[85%] rounded-2xl rounded-br-md px-3.5 py-2 text-sm whitespace-pre-wrap bg-action-primary-600 text-white">{{ m.content }}</div>
          </div>

          <div v-else class="flex items-start gap-2.5">
            <PortalAvatar :name="m.sender_identity" :size="28" class="mt-0.5" />
            <div class="max-w-[85%]">
              <div class="text-xs text-gray-500 dark:text-gray-400 mb-0.5">{{ m.sender_identity }}</div>
              <div class="rounded-2xl rounded-bl-md bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-gray-100 px-3.5 py-2 text-sm prose-portal" v-html="render(m.content)"></div>
            </div>
          </div>
        </div>

        <!-- Who is thinking. Derived from the SERVER (`room.working`), not just
             the local send, so a client that reloaded mid-turn still sees it —
             a reload used to make the dots vanish while two agents were still
             working, which reads as the room having given up. -->
        <div v-if="workingAgents.length" class="flex items-start gap-2.5">
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

    <!-- Composer -->
    <div class="shrink-0 border-t border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 px-3 sm:px-6 py-3">
      <div class="max-w-3xl mx-auto">
        <!-- A closed room is a dead end unless it SAYS so. Rooms end on their
             own (message budget, cost cap, TTL) — silence would read as the
             agents having stopped answering. -->
        <p v-if="isClosed" class="text-xs text-center text-gray-500 dark:text-gray-400 py-2">
          This conversation has ended{{ room?.stop_reason ? ` (${closedReason})` : '' }}. Start a new chat to keep going.
        </p>
        <form v-else class="flex items-end gap-2" @submit.prevent="send">
          <textarea
            v-model="input"
            rows="1"
            :placeholder="placeholder"
            class="flex-1 resize-none rounded-2xl border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-800 text-sm px-4 py-2.5 leading-6 focus:ring-2 focus:ring-action-primary-500/40 focus:border-action-primary-500 focus:outline-none max-h-40"
            @keydown.enter.exact.prevent="send"
          ></textarea>
          <button
            type="submit"
            class="shrink-0 p-2.5 rounded-xl bg-action-primary-600 hover:bg-action-primary-700 text-white disabled:opacity-40 transition"
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
import { renderMarkdown } from '@/utils/markdown'
import PortalAvatar from './PortalAvatar.vue'

const props = defineProps({
  roomId: { type: String, required: true },
  roster: { type: Array, default: () => [] },
})
defineEmits(['open-menu', 'rooms-changed'])

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

let pollTimer = null
const POLL_MS = 3000

const render = (c) => renderMarkdown(c || '')

const agentParticipants = computed(() =>
  (room.value?.participants || []).filter((p) => p.kind === 'agent' && !p.left_at).map((p) => p.identity)
)
const isClosed = computed(() => room.value?.status === 'closed')

// Server-reported, so it survives a reload. The local `sending` flag still
// covers the gap between posting and the first poll, when nobody has been
// marked working yet.
const workingAgents = computed(() => room.value?.working || [])

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

function isMine(m) {
  // Anything not an agent and not the room itself is this client — the room is
  // per-client here, so a human sender is the caller.
  return m.sender_kind !== 'agent' && m.kind !== 'system'
}

async function load({ full = false } = {}) {
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
    loading.value = false
  }
}

async function send() {
  const text = input.value.trim()
  if (!text || sending.value) return
  sending.value = true
  sendError.value = null
  input.value = ''
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
  await load({ full: true })
})

onMounted(async () => {
  await load({ full: true })
  startPolling()
})
onBeforeUnmount(stopPolling)
</script>
