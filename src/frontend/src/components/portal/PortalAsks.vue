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
  <div v-if="visible" class="space-y-2" data-testid="portal-asks">
    <div
      v-for="ask in items"
      :key="ask.id"
      class="rounded-xl border px-3 py-2.5"
      :class="ask.status === 'expired'
        ? 'border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/40'
        : 'border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-900/20'"
      :data-testid="`portal-ask-${ask.id}`"
      :data-status="ask.status"
    >
      <div class="flex items-start gap-2">
        <span class="mt-0.5 text-xs font-medium uppercase tracking-wide"
              :class="ask.status === 'expired' ? 'text-gray-400' : 'text-amber-700 dark:text-amber-300'">
          {{ kindLabel(ask.kind) }}
        </span>
        <span v-if="showAgent" class="mt-0.5 text-xs text-gray-500 dark:text-gray-400">{{ ask.agent_name }}</span>
      </div>

      <p class="mt-1 text-sm font-medium text-gray-900 dark:text-gray-100">{{ ask.title }}</p>
      <p v-if="ask.question && ask.question !== ask.title"
         class="mt-0.5 text-sm text-gray-600 dark:text-gray-300 whitespace-pre-wrap">{{ ask.question }}</p>

      <!-- Expired is RENDERED, never silently dropped: the retention sweep deletes
           terminal rows, and an ask that simply vanishes reads as "answered" to the
           person who did not answer it. -->
      <p v-if="ask.status === 'expired'" class="mt-2 text-xs text-gray-500 dark:text-gray-400">
        {{ expiredLabel(ask.expires_at) }}
      </p>

      <!-- ent#429: the conversation this ask was raised against. Shown only when
           it is somewhere the reader is not — an ask raised by a scheduled run
           attaches to a thread at RAISE time, and without a way back to it the
           attachment is a fact nobody can act on. Additive: it never hides an
           ask from the thread being read. -->
      <button
        v-if="askThreadLink(ask, currentSessionId)"
        type="button"
        class="mt-1.5 text-xs text-action-primary-600 hover:underline"
        :data-testid="`portal-ask-open-thread-${ask.id}`"
        @click="emit('open-thread', { id: askThreadLink(ask, currentSessionId), agent_name: ask.agent_name })"
      >Open the conversation</button>

      <template v-else>
        <!-- #2375: controls come from the shared kind rule (queueResponseKind),
             so this surface cannot drift from desktop QueueCard and /m. An
             approval is select → optional note → explicit Send — never a
             one-tap irreversible answer; the tapped option only arms Send. -->
        <template v-if="controlsKind(ask) === 'approval'">
          <div class="mt-2 flex flex-wrap gap-2" role="radiogroup">
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
              :data-testid="`portal-ask-option-${ask.id}`"
              @click="picks[ask.id] = picks[ask.id] === opt ? null : opt"
            >{{ opt }}</button>
          </div>
          <form class="mt-2 flex items-center gap-2" @submit.prevent="submit(ask)">
            <input
              v-model="notes[ask.id]"
              type="text"
              :disabled="busyId === ask.id"
              placeholder="Add a note (optional)…"
              class="flex-1 min-w-0 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 px-2.5 py-1.5 text-sm"
              :data-testid="`portal-ask-note-${ask.id}`"
            />
            <button
              type="submit"
              :disabled="busyId === ask.id || !picks[ask.id]"
              class="rounded-lg bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 text-white text-xs font-medium px-2.5 py-1.5"
              :data-testid="`portal-ask-send-${ask.id}`"
            >{{ busyId === ask.id ? 'Sending…' : 'Send' }}</button>
          </form>
        </template>

        <!-- An alert only wants acknowledging; "Got it" mirrors desktop and /m. -->
        <button
          v-else-if="controlsKind(ask) === 'acknowledge'"
          type="button"
          :disabled="busyId === ask.id"
          class="mt-2 rounded-lg bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 text-white text-xs font-medium px-2.5 py-1.5"
          :data-testid="`portal-ask-ack-${ask.id}`"
          @click="submit(ask)"
        >{{ busyId === ask.id ? 'Sending…' : 'Got it' }}</button>

        <!-- A question (or an approval that offered no options) takes a typed
             answer — sent as the DECISION (`response`), never as a note (#2375). -->
        <form v-else class="mt-2 flex items-center gap-2" @submit.prevent="submit(ask)">
          <input
            v-model="drafts[ask.id]"
            type="text"
            :disabled="busyId === ask.id"
            placeholder="Your answer…"
            class="flex-1 min-w-0 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 px-2.5 py-1.5 text-sm"
            :data-testid="`portal-ask-input-${ask.id}`"
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
import { computed, reactive, ref } from 'vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import { expiredLabel, askThreadLink } from './portalUtils'
import { optionsOf, queueResponseKind, buildQueueResponse, queueTypeLabel } from '@/utils/operatorQueue'

const props = defineProps({
  // Omit to render every ask addressed to this user (chat/global); pass a name to
  // render one agent's (the agent page).
  agentName: { type: String, default: null },
  showAgent: { type: Boolean, default: false },
  // The thread on screen, when there is one. Only used to suppress a link that
  // would go where the reader already is (ent#429).
  currentSessionId: { type: String, default: null },
})

const emit = defineEmits(['open-thread'])

const store = useClientPortalStore()
const busyId = ref(null)
const drafts = reactive({})   // question: the typed answer (the DECISION)
const picks = reactive({})    // approval: the selected option
const notes = reactive({})    // approval: the optional free-text note
const errors = reactive({})

const items = computed(() =>
  props.agentName ? store.asksForAgent(props.agentName) : store.asks
)
const visible = computed(() => store.asksAvailable && items.value.length > 0)

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
    await store.answerAsk(ask.id, { response: body.response, responseText: body.response_text })
    delete drafts[ask.id]
    delete picks[ask.id]
    delete notes[ask.id]
  } catch (err) {
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
