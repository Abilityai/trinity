<!--
  trinity-enterprise#610 PR A2, §3g L7 (E1) — an ask's context, BELOW its card
  in the Inbox pane (reconcile row 14): the card's answer controls never move,
  and they work whether or not this read succeeds.

    meta       "Asked 8m ago during a scheduled run · started 09:00" — the run
               folded into one line (askContextMeta); the expiry and priority
               are in the card's header (PortalAsks `showUrgency`, A2 round 1)
    sections   Where it came from → Delivered in that chat → Your recent answers

  Everything here is PLATFORM data the server has already gated
  (`GET …/asks/{id}/context`): a run is named only after the agent, live-window
  and audience checks; the message excerpts exist only when the run's thread is
  verified as the viewer's — otherwise the origin reads "Filed in your Main
  chat" and nothing is excerpted; there is no cost and no execution id. Text is
  interpolated, never bound as HTML.

  Honest state (principle 15): a skeleton while the read has no verdict, then
  LoadFailed dense in the same slot — never an empty context on a failed read.
-->
<template>
  <!-- px-3: the card's own text inset, so the context reads as the card's
       continuation, not a second column 12px to its left (A2 round 1). -->
  <section class="mt-4 px-3" aria-label="About this ask" data-testid="inbox-ask-context">
    <!-- The smallest loaded context's shape — the meta line, a heading, one
         line, the link — in the chrome fill (opaque in dark: /60 read as no
         skeleton at all, A2 round 1), never a card-tall block. -->
    <div v-if="view.state === 'loading'" aria-busy="true" data-testid="inbox-ask-context-loading">
      <div :class="[BAR, 'h-3.5 w-1/2']"></div>
      <div :class="[BAR, 'mt-5 h-3 w-24']"></div>
      <div :class="[BAR, 'mt-2 h-3.5 w-2/3']"></div>
      <div :class="[BAR, 'mt-3 h-3.5 w-32']"></div>
      <span class="sr-only">Loading where this ask came from…</span>
    </div>

    <template v-else-if="view.state === 'failed'">
      <LoadFailed
        dense
        title="Couldn't load where this ask came from"
        message="You can still answer it above."
        :retrying="busy"
        data-testid="inbox-ask-context-failed"
        @retry="load"
      />
      <!-- No dead end: the way to the ask's own chat does not need this read.
           Only an ask a chat turn raised HAS a chat of its own — a background
           ask's `chat_id` (Main) is its reply target, and it renders in no chat
           (the 09-30 ruling, amended; round 2, codex C4). -->
      <button
        v-if="ask.chat_id && ask.raised_in_turn === true"
        type="button"
        :class="[LINK, 'mt-2']"
        data-testid="inbox-ask-context-open-fallback"
        @click="emit('open-chat', `/workspace/c/${ask.chat_id}`)"
      >Open the conversation</button>
    </template>

    <template v-else>
      <p class="flex flex-wrap gap-x-1 text-[12.5px] text-gray-600 dark:text-gray-300" data-testid="inbox-ask-context-meta">
        <template v-for="(part, i) in meta" :key="i">
          <span v-if="i" aria-hidden="true">·</span><span class="whitespace-nowrap">{{ part }}</span>
        </template>
      </p>

      <!-- Where it came from -->
      <div v-if="ctx.origin" class="mt-4" data-testid="inbox-ask-context-origin">
        <h3 :class="OVERLINE">Where it came from</h3>
        <template v-if="ctx.origin.verified">
          <p class="mt-1 text-sm font-medium text-gray-900 dark:text-gray-100 truncate" data-testid="inbox-ask-context-origin-title">{{ originName }}</p>
          <!-- Raised in one chat, filed in another (an ingested ask files into
               Main): say both, or the link lands where the ask is not. -->
          <p v-if="filedElsewhere" class="mt-0.5 text-[12.5px] text-gray-600 dark:text-gray-300" data-testid="inbox-ask-context-filed">{{ filedElsewhere }}</p>
          <ol v-if="ctx.origin.messages.length" class="mt-2 space-y-2">
            <li
              v-for="m in ctx.origin.messages"
              :key="m.id"
              class="text-[12.5px] text-gray-900 dark:text-gray-100"
              data-testid="inbox-ask-context-message"
            >
              <span class="font-semibold">{{ m.role === 'user' ? 'You' : (agentLabel || ask.agent_name) }}</span>
              <span class="mx-1 text-gray-600 dark:text-gray-300" aria-hidden="true">·</span>{{ m.excerpt }}
            </li>
          </ol>
        </template>
        <p v-else class="mt-1 text-[12.5px] text-gray-900 dark:text-gray-100">
          {{ ctx.origin.is_main ? 'Filed in your Main chat' : `Filed in “${ctx.origin.title || 'a chat'}”` }}
        </p>
        <button
          type="button"
          :class="[LINK, 'mt-2']"
          data-testid="inbox-ask-context-open"
          @click="emit('open-chat', originUrl)"
        >{{ ctx.origin.verified ? `Open “${originName}”` : 'Open the conversation' }}</button>
      </div>

      <!-- Delivered in that chat — a verified thread only: what Main holds is
           usually unrelated to the ask. Read before the context paints, so it
           never lands late and pushes the answers down (A2 round 1). -->
      <div v-if="delivered.length" class="mt-4" data-testid="inbox-ask-context-delivered">
        <h3 :class="OVERLINE">Delivered in that chat</h3>
        <ul class="mt-1 space-y-1">
          <li v-for="d in delivered" :key="d.id">
            <button
              type="button"
              :class="[LINK, 'text-left']"
              :data-testid="`inbox-ask-context-delivered-${d.id}`"
              @click="emit('open-chat', `/workspace/c/${ctx.origin.chat_id}?anchor=d:${d.id}`)"
            >{{ d.title || 'Untitled deliverable' }}</button>
          </li>
        </ul>
      </div>
      <LoadFailed
        v-else-if="deliveredFailed"
        dense
        class="mt-4"
        title="Couldn't load what was delivered in that chat"
        message="The rest of this ask's context is below."
        :retrying="deliveredBusy"
        data-testid="inbox-ask-context-delivered-failed"
        @retry="retryDelivered"
      />

      <!-- Your recent answers -->
      <div v-if="ctx.recent_answers.length" class="mt-4" data-testid="inbox-ask-context-answers">
        <h3 :class="OVERLINE">Your recent answers</h3>
        <ul class="mt-1 space-y-2">
          <li v-for="r in ctx.recent_answers" :key="r.id" class="text-[12.5px] text-gray-900 dark:text-gray-100">
            <span class="font-medium">{{ r.title }}</span>
            <span v-if="r.answer"> — you answered “{{ r.answer }}”</span>
            <span v-if="r.ended_at" class="text-gray-600 dark:text-gray-300"> · {{ relativeTime(r.ended_at) }}</span>
          </li>
        </ul>
      </div>
    </template>
  </section>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import LoadFailed from '@/components/LoadFailed.vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import { viewState } from '@/utils/loadingState'
import { askContextMeta } from './portalAskUrgency'
import { relativeTime } from './portalUtils'

const props = defineProps({
  // The ask as the card renders it (the store's WorkspaceAsk).
  ask: { type: Object, required: true },
  agentLabel: { type: String, default: '' },
})
const emit = defineEmits(['open-chat'])

const OVERLINE = 'text-[11px] font-semibold uppercase tracking-wide text-gray-600 dark:text-gray-300'
const BAR = 'animate-pulse motion-reduce:animate-none rounded bg-gray-100 dark:bg-gray-750'
// A 44px target on a phone (the pane's `max-sm:min-h-11` convention), the
// design-system ring everywhere.
const LINK = 'inline-flex items-center max-sm:min-h-11 text-[12.5px] font-medium text-action-primary-600 dark:text-action-primary-400 hover:underline rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:focus-visible:ring-action-primary-400/40'
const EMPTY = Object.freeze({ origin: null, run: null, recent_answers: [] })

const store = useClientPortalStore()
const ctx = ref(EMPTY)
const loaded = ref(false)
const failed = ref(false)
const busy = ref(false)
const delivered = ref([])
const deliveredFailed = ref(false)
const deliveredBusy = ref(false)

// An ask with nothing more to say still shows its meta line, so a loaded
// context is never "empty" here.
const view = computed(() => viewState({ hasLoaded: loaded.value, error: failed.value }))
const meta = computed(() => askContextMeta(props.ask, ctx.value.run))
// A verified Main keeps its generated title in the DB; every other surface calls
// it "Main" (the tab strip), so this one does too.
const originName = computed(() => {
  const o = ctx.value.origin
  if (!o) return ''
  return o.is_main ? 'Main' : (o.title || 'Untitled chat')
})
const filedElsewhere = computed(() => {
  const o = ctx.value.origin
  const own = props.ask.chat_id
  if (!o || !o.verified || !own || own === o.chat_id) return ''
  return 'Filed in your Main chat'
})
const originUrl = computed(() => {
  const o = ctx.value.origin
  if (!o) return null
  const last = o.verified && o.messages.length ? o.messages[o.messages.length - 1].id : null
  return last ? `/workspace/c/${o.chat_id}?anchor=m:${last}` : `/workspace/c/${o.chat_id}`
})

// A newer selection wins: a read that resolves for an ask no longer shown is
// dropped rather than painted over the current one.
let seq = 0
async function load() {
  const mine = ++seq
  busy.value = true
  failed.value = false
  try {
    const data = await store.fetchAskContext(props.ask.id)
    if (mine !== seq) return
    const origin = data?.origin
      ? { ...data.origin, messages: Array.isArray(data.origin.messages) ? data.origin.messages : [] }
      : null
    const next = {
      origin,
      run: data?.run || null,
      recent_answers: Array.isArray(data?.recent_answers) ? data.recent_answers : [],
    }
    // One paint: what was delivered is read BEFORE the context shows, so it
    // never arrives late and shoves the answers down (principle 4).
    await loadDelivered(mine, next.origin)
    if (mine !== seq) return
    ctx.value = next
    loaded.value = true
  } catch {
    if (mine !== seq) return
    failed.value = true
  } finally {
    if (mine === seq) busy.value = false
  }
}
async function loadDelivered(mine, o) {
  delivered.value = []
  deliveredFailed.value = false
  if (!o || !o.verified) return
  try {
    const list = await store.fetchSessionDeliverablesStrict(props.ask.agent_name, o.chat_id)
    if (mine === seq) delivered.value = Array.isArray(list) ? list : []
  } catch {
    if (mine === seq) deliveredFailed.value = true
  }
}
async function retryDelivered() {
  deliveredBusy.value = true
  try { await loadDelivered(seq, ctx.value.origin) } finally { deliveredBusy.value = false }
}

watch(() => props.ask.id, () => {
  ctx.value = EMPTY
  loaded.value = false
  load()
}, { immediate: true })
</script>
