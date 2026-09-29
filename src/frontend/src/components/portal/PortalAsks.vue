<!--
  Agent-initiated asks (ent#364).

  ONE component for all three renderings the issue asks for — the agent page and
  inline in chat render it directly, and the sidebar shows `store.askCount` from the
  same list. That is deliberate: the underlying item is one `operator_queue` row, so
  answering here clears it in every surface with no sync step. Three bespoke
  renderers reading three queries is exactly how that stops being true.

  Renders nothing when there is nothing to show, including on an OSS or unentitled
  build where the endpoint 404s — the store keeps `asksAvailable` false and the list
  empty.
-->
<template>
  <div v-if="visible" class="space-y-2" :data-testid="tid.root">
    <!-- ent#468: what happened to the answer you just gave. It lives HERE and
         not on the ask row, because answering removes that row — the row is the
         one place this cannot be. On an opt-in agent the answer sets work in
         motion and spends the owner's budget, so the person who caused it is
         told; with the opt-in off it says only that the answer was sent.
         Clears itself; `aria-live` because it appears without a navigation. -->
    <p
      v-for="c in confirmations"
      :key="c.id"
      class="rounded-xl border border-gray-200 dark:border-gray-700 px-3 py-2 text-sm text-gray-600 dark:text-gray-300"
      role="status"
      aria-live="polite"
      :data-testid="tid.confirmation"
    >{{ c.message }}</p>

    <div
      v-for="ask in items"
      :key="ask.id"
      class="rounded-xl border px-3 py-2.5"
      :class="isEnded(ask)
        ? 'border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/40'
        : 'border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-900/20'"
      :data-testid="`${tid.prefix}-${ask.id}`"
      :data-status="ask.status"
    >
      <div class="flex items-start gap-2">
        <span class="mt-0.5 text-xs font-medium uppercase tracking-wide"
              :class="isEnded(ask) ? 'text-gray-400' : 'text-amber-700 dark:text-amber-300'">
          {{ kindLabel(ask.kind) }}
        </span>
        <span v-if="showAgent" class="mt-0.5 text-xs text-gray-500 dark:text-gray-400">{{ ask.agent_name }}</span>
        <!-- #2915: coarse sync state / aging from the projection, same rule as the desktop.
             trinity-enterprise#611: only while the ask is still waiting — once
             it ended, the agent's copy is not something the person can act on. -->
        <BaseBadge
          v-if="!isEnded(ask) && queueSyncBadge(ask)"
          :variant="queueSyncBadge(ask).variant"
          dot
          :title="queueSyncBadge(ask).title"
          :data-testid="tid.syncBadge"
        >{{ queueSyncBadge(ask).label }}</BaseBadge>
      </div>

      <p class="mt-1 text-sm font-medium text-gray-900 dark:text-gray-100">{{ ask.title }}</p>
      <!-- An agent writes its ask in markdown, as it writes its replies: the
           body goes through the one sanitized renderer (PortalMarkdown →
           DOMPurify), never printed as its syntax (ent#610 sign-off). -->
      <PortalMarkdown
        v-if="ask.question && ask.question !== ask.title"
        :content="ask.question"
        class="mt-0.5 min-w-0 text-sm text-gray-600 dark:text-gray-300"
        :data-testid="`${tid.prefix}-question-${ask.id}`"
      />

      <!-- An ending is RENDERED, never silently dropped: an ask that simply
           vanishes reads as "answered" to the person who did not answer it.
           trinity-enterprise#611: answered / cancelled / expired, with a coarse
           who (you / the operator / timeout) and when — never an operator's
           email or the reason they gave. -->
      <p
        v-if="isEnded(ask)"
        class="mt-2 text-xs text-gray-500 dark:text-gray-400"
        :title="endedAtAbsolute(ask) || undefined"
        :data-testid="tid.ending"
      >
        {{ endingLine(ask) }}
      </p>

      <!-- ent#429: the conversation this ask was raised against. Shown only when
           it is somewhere the reader is not — an ask raised by a scheduled run
           attaches to a thread at RAISE time, and without a way back to it the
           attachment is a fact nobody can act on. Additive: its own `v-if`, so
           it never takes the controls below away (#3055 — it once did, and
           every ask read outside Main lost its answer). -->
      <button
        v-if="threadLink && askThreadLink(ask, currentSessionId)"
        type="button"
        class="mt-1.5 text-xs text-action-primary-600 hover:underline"
        :data-testid="`${tid.prefix}-open-thread-${ask.id}`"
        @click="emit('open-thread', { id: askThreadLink(ask, currentSessionId), agent_name: ask.agent_name })"
      >Open the conversation</button>

      <template v-if="!isEnded(ask)">
        <!-- #2375: controls come from the shared kind rule (queueResponseKind),
             so this surface cannot drift from desktop QueueCard and /m. An
             approval is select → optional note → explicit Send — never a
             one-tap irreversible answer; the tapped option only arms Send. -->
        <template v-if="controlsKind(ask) === 'approval'">
          <div class="mt-2 flex flex-wrap gap-2">
            <button
              v-for="opt in optionsOf(ask)"
              :key="opt"
              type="button"
              :disabled="busyId === ask.id"
              class="rounded-lg border text-xs font-medium px-2.5 py-1.5 disabled:opacity-50"
              :class="picks[ask.id] === opt
                ? 'bg-action-primary-600 border-action-primary-600 text-white'
                : 'bg-white dark:bg-gray-900 border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:border-action-primary-500'"
              :aria-pressed="picks[ask.id] === opt"
              :data-testid="`${tid.prefix}-option-${ask.id}`"
              @click="picks[ask.id] = picks[ask.id] === opt ? null : opt"
            >{{ opt }}</button>
          </div>
          <form class="mt-2 flex items-center gap-2" @submit.prevent="submit(ask)">
            <input
              v-model="notes[ask.id]"
              type="text"
              maxlength="4000"
              :disabled="busyId === ask.id"
              placeholder="Add a note (optional)…"
              class="flex-1 min-w-0 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 px-2.5 py-1.5 text-sm"
              :data-testid="`${tid.prefix}-note-${ask.id}`"
            />
            <button
              type="submit"
              :disabled="busyId === ask.id || !picks[ask.id]"
              class="rounded-lg bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 text-white text-xs font-medium px-2.5 py-1.5"
              :data-testid="`${tid.prefix}-send-${ask.id}`"
            >{{ busyId === ask.id ? 'Sending…' : 'Send' }}</button>
          </form>
        </template>

        <!-- An alert only wants acknowledging; "Got it" mirrors desktop and /m. -->
        <button
          v-else-if="controlsKind(ask) === 'acknowledge'"
          type="button"
          :disabled="busyId === ask.id"
          class="mt-2 rounded-lg bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 text-white text-xs font-medium px-2.5 py-1.5"
          :data-testid="`${tid.prefix}-ack-${ask.id}`"
          @click="submit(ask)"
        >{{ busyId === ask.id ? 'Sending…' : 'Got it' }}</button>

        <!-- A question (or an approval that offered no options) takes a typed
             answer — sent as the DECISION (`response`), never as a note (#2375). -->
        <form v-else class="mt-2 flex items-center gap-2" @submit.prevent="submit(ask)">
          <input
            v-model="drafts[ask.id]"
            type="text"
            maxlength="500"
            :disabled="busyId === ask.id"
            placeholder="Your answer…"
            class="flex-1 min-w-0 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 px-2.5 py-1.5 text-sm"
            :data-testid="`${tid.prefix}-input-${ask.id}`"
          />
          <button
            type="submit"
            :disabled="busyId === ask.id || !String(drafts[ask.id] || '').trim()"
            class="rounded-lg bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 text-white text-xs font-medium px-2.5 py-1.5"
          >{{ busyId === ask.id ? 'Sending…' : 'Send' }}</button>
        </form>

        <p v-if="errors[ask.id]" class="mt-1.5 text-xs text-red-600 dark:text-red-400">{{ errors[ask.id] }}</p>
      </template>
    </div>
  </div>
</template>

<script setup>
import { computed, reactive, ref, onBeforeUnmount } from 'vue'
import BaseBadge from '../base/BaseBadge.vue'
import PortalMarkdown from './PortalMarkdown.vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import {
  expiredLabel, askThreadLink, answerConfirmation, ANSWER_CONFIRMATION_MS,
} from './portalUtils'
import { optionsOf, queueResponseKind, buildQueueResponse, queueTypeLabel } from '@/utils/operatorQueue'
// #2915: the same home; a second line so the #2375 import pin above stays byte-exact.
import { queueSyncBadge, respondRefusedAsDiverged, QUEUE_RESPONSE_DIVERGED } from '@/utils/operatorQueue'
// trinity-enterprise#611: the one ending rule, a third line for the same reason.
import { queueEnding, queueEndingText } from '@/utils/operatorQueue'
import { formatLocalDateTime, formatRelativeTime } from '@/utils/timestamps'

const DEFAULT_TESTID_PREFIX = 'portal-ask'

const props = defineProps({
  // Omit to render every ask addressed to this user (chat/global); pass a name to
  // render one agent's (the agent page).
  agentName: { type: String, default: null },
  // ent#525: or several — the rail's Work tab renders the asks of a chat's
  // PARTICIPANTS ("Waiting on you"), the fourth rendering of the same row. A
  // computed over `store.asks`, never a narrowed fetch: `fetchAsks(agentName)`
  // replaces the shared list the sidebar badge reads.
  agentNames: { type: Array, default: null },
  showAgent: { type: Boolean, default: false },
  // trinity-enterprise#611: only what is still waiting — the Work tab's
  // "Waiting on you". Every other rendering also shows asks that ended.
  pendingOnly: { type: Boolean, default: false },
  // The thread on screen, when there is one. Only used to suppress a link that
  // would go where the reader already is (ent#429).
  currentSessionId: { type: String, default: null },
  // trinity-enterprise#610 (D8): the Inbox pane renders ONE ask through this
  // same component rather than a second card. `askIds` narrows to those asks
  // and wins over `pendingOnly`: an ask that ends while it is selected stays
  // drawn in place, as ended, for the Inbox's tab visit (§3g S1, principle 5).
  askIds: { type: Array, default: null },
  // trinity-enterprise#610 (D8): namespaces EVERY data-testid this component
  // emits, the static ones included, so two instances rendering the same ask
  // (the Inbox pane next to the Work tab) never share an address. The rule:
  //   root          `${prefix}s`              (portal-asks)
  //   confirmation  `${prefix}-confirmation`  (portal-ask-confirmation)
  //   per ask       `${prefix}-${id}`, `${prefix}-<part>-${id}`
  //   ending        `${prefix}-ending`        (portal-ask-ending)
  //   sync badge    `queue-sync-badge` with the default prefix (the id the
  //                 #2915 specs address), `${prefix}-sync-badge` otherwise.
  // With the default every id is byte-identical to what it was before. (The
  // literal repeats DEFAULT_TESTID_PREFIX: defineProps is hoisted out of setup.)
  testidPrefix: { type: String, default: 'portal-ask' },
  // #3055: a surface that is already beside the ask's chat can turn the
  // "Open the conversation" link off. It never affects the controls.
  threadLink: { type: Boolean, default: true },
})

const emit = defineEmits(['open-thread'])

const store = useClientPortalStore()
const busyId = ref(null)
const drafts = reactive({})   // question: the typed answer (the DECISION)
const picks = reactive({})    // approval: the selected option
const notes = reactive({})    // approval: the optional free-text note
const errors = reactive({})
const diverged = reactive({})   // #2915: ask id → the person has seen the divergence notice

const allItems = computed(() => {
  if (props.agentName) return store.asksForAgent(props.agentName)
  if (Array.isArray(props.agentNames)) {
    const names = new Set(props.agentNames.filter(Boolean))
    return store.asks.filter((a) => names.has(a.agent_name))
  }
  return store.asks
})
const items = computed(() => {
  if (Array.isArray(props.askIds)) {
    const ids = new Set(props.askIds.filter(Boolean))
    return store.asks.filter((a) => ids.has(a.id))
  }
  return props.pendingOnly ? allItems.value.filter((a) => a.status === 'pending') : allItems.value
})

const tid = computed(() => {
  const p = props.testidPrefix || DEFAULT_TESTID_PREFIX
  return {
    prefix: p,
    root: `${p}s`,
    confirmation: `${p}-confirmation`,
    ending: `${p}-ending`,
    syncBadge: p === DEFAULT_TESTID_PREFIX ? 'queue-sync-badge' : `${p}-sync-badge`,
  }
})

// trinity-enterprise#611 — how an ask ended, from the one rule.
const isEnded = (ask) => ask.status !== 'pending'
function endingLine(ask) {
  const ending = queueEnding(ask)
  // An expiry keeps its own sentence: the deadline is the fact a person reads.
  if (!ending || ending.kind === 'expired') return expiredLabel(ask.expires_at)
  const text = queueEndingText(ending)
  return ending.when ? `${text} · ${formatRelativeTime(ending.when)}` : text
}
const endedAtAbsolute = (ask) => {
  const when = queueEnding(ask)?.when
  return when ? formatLocalDateTime(when) : ''
}
// ent#468: a confirmation keeps the component mounted after the last ask goes.
// Gating on `items.length` alone unmounted the whole surface at the instant the
// row was removed, which is the same instant the confirmation is created — so
// the message would have been rendered for exactly zero frames.
const confirmations = ref([])
const visible = computed(() => (
  store.asksAvailable && (items.value.length > 0 || confirmations.value.length > 0)
))

let confirmationTimers = []

function showConfirmation(message) {
  if (!message) return
  const id = `ack-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
  confirmations.value.push({ id, message })
  const timer = setTimeout(() => {
    confirmations.value = confirmations.value.filter((c) => c.id !== id)
    // Drop the handle too: a fired timer left in the list is dead weight that
    // grows for the life of the mount, and `onBeforeUnmount` would then clear
    // a pile of expired ids.
    confirmationTimers = confirmationTimers.filter((t) => t !== timer)
  }, ANSWER_CONFIRMATION_MS)
  confirmationTimers.push(timer)
}

// A timer that outlives the component would write to a dead ref on a chat
// switch — the surface unmounts and remounts constantly.
onBeforeUnmount(() => {
  confirmationTimers.forEach(clearTimeout)
  confirmationTimers = []
})

// #2375: one label set and one controls rule across desktop, /m and the
// Workspace — both come from utils/operatorQueue, the single home #2370
// established. An ask's `kind` is the queue row's `type` verbatim.
const kindLabel = (kind) => queueTypeLabel(kind) || 'Question'
const controlsKind = (ask) => queueResponseKind({ type: ask.kind, options: ask.options })

async function submit(ask) {
  // The shared builder decides the wire shape: the decision travels as
  // `response` (the field the agent reads), a note as `response_text`. It
  // returns null when there is nothing valid to send — no option picked,
  // blank answer — and the controls stay armed.
  const body = buildQueueResponse({
    kind: controlsKind(ask),
    option: picks[ask.id],
    note: notes[ask.id] || '',
    answer: drafts[ask.id] || '',
  })
  if (!body) return
  busyId.value = ask.id
  errors[ask.id] = null
  try {
    const answered = await store.answerAsk(ask.id, {
      response: body.response, responseText: body.response_text,
      acknowledgeDivergence: !!diverged[ask.id],
    })
    delete diverged[ask.id]
    // ent#468: the response was discarded here, so `resume_requested` and the
    // `answered` status were on the wire and read by nothing.
    showConfirmation(answerConfirmation(answered, ask.agent_name))
    delete drafts[ask.id]
    delete picks[ask.id]
    delete notes[ask.id]
  } catch (err) {
    if (respondRefusedAsDiverged(err)) {
      // #2915: the agent changed or closed this ask after it was read. Show it,
      // refresh the projection so the badge appears, and let the next send
      // answer anyway.
      diverged[ask.id] = true
      errors[ask.id] = QUEUE_RESPONSE_DIVERGED
      if (typeof store.fetchAsks === 'function') { try { await store.fetchAsks() } catch (_) { /* the notice stands */ } }
      return
    }
    // The backend's refusals are already written for a human ("This ask expired
    // before it was answered."), so surface them rather than replacing them with
    // a generic failure.
    errors[ask.id] = err.response?.data?.detail?.message
      || err.response?.data?.detail
      || 'Could not send your answer.'
  } finally {
    busyId.value = null
  }
}
</script>
