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
      <div class="mt-1.5 flex items-center gap-3">
        <button
          v-if="askThreadLink(ask, currentSessionId)"
          type="button"
          class="text-xs text-action-primary-600 hover:underline"
          :data-testid="`portal-ask-open-thread-${ask.id}`"
          @click="emit('open-thread', { id: askThreadLink(ask, currentSessionId), agent_name: ask.agent_name })"
        >Open the conversation</button>

        <!-- An expired ask cannot be answered by anyone, so without this the card
             was inert — nothing to click, and it stayed until the 90-day
             retention sweep deleted the row. Showing expiry once is the point;
             showing it forever is not.

             Expired ONLY. A pending ask must be answered, never dismissed:
             letting a client silently drop a decision would turn a visible
             question into a hung agent nobody can explain. -->
        <button
          v-if="ask.status === 'expired'"
          type="button"
          class="text-xs text-gray-500 dark:text-gray-400 hover:underline"
          :data-testid="`portal-ask-dismiss-${ask.id}`"
          title="Hide this expired question. It stays in the operator's queue."
          @click="dismiss(ask)"
        >Dismiss</button>
      </div>

      <!-- EXPLICIT condition, deliberately not `v-else` (ent#429 follow-up).
           `v-else` binds to whatever `v-if` immediately precedes it, so when the
           "Open the conversation" button was inserted above, these controls
           silently re-paired onto ITS condition: an expired ask rendered its
           answer buttons whenever the thread link was hidden — i.e. while
           reading the very thread the ask belongs to — and answering then 409'd
           with "This ask expired before it was answered.", printed under a card
           that already said so.

           Stating the condition means the next element inserted above cannot
           re-point it. The build was clean and the unit suite green throughout,
           because neither can see template structure. -->
      <template v-if="ask.status !== 'expired'">
        <div v-if="ask.options?.length" class="mt-2 flex flex-wrap gap-2">
          <button
            v-for="opt in ask.options"
            :key="String(opt)"
            type="button"
            :disabled="busyId === ask.id"
            class="rounded-lg bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 text-white text-xs font-medium px-2.5 py-1.5"
            :data-testid="`portal-ask-option-${ask.id}`"
            @click="answer(ask, String(opt))"
          >{{ opt }}</button>
        </div>

        <form v-else class="mt-2 flex items-center gap-2" @submit.prevent="answer(ask, null, drafts[ask.id])">
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
import {
  expiredLabel, askThreadLink, readDismissedAsks, dismissAsk, visibleAsks,
} from './portalUtils'

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
const drafts = reactive({})
const errors = reactive({})

const dismissed = ref(readDismissedAsks(store.clientEmail))

function dismiss(ask) {
  dismissed.value = new Set(dismissAsk(store.clientEmail, ask.id))
}

const items = computed(() => visibleAsks(
  props.agentName ? store.asksForAgent(props.agentName) : store.asks,
  dismissed.value,
))
const visible = computed(() => store.asksAvailable && items.value.length > 0)

const KINDS = { question: 'Question', approval: 'Approval needed', alert: 'Update' }
const kindLabel = (kind) => KINDS[kind] || 'Question'

async function answer(ask, response, text = null) {
  busyId.value = ask.id
  errors[ask.id] = null
  try {
    await store.answerAsk(ask.id, { response, responseText: text || null })
    delete drafts[ask.id]
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
