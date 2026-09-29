<!--
  trinity-enterprise#610 PR A2, §3g L7 (E1) — an ask's context, BELOW its card
  in the Inbox pane (reconcile row 14): the card's answer controls never move,
  and they work whether or not this read succeeds.

    meta       "Asked 8m ago during a scheduled run · 09:00 · expires in 5h ·
               High priority" — the run folded into one line (askContextMeta)
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
  <section class="mt-4" aria-label="About this ask" data-testid="inbox-ask-context">
    <div v-if="view.state === 'loading'" class="space-y-2" aria-busy="true" data-testid="inbox-ask-context-loading">
      <div class="animate-pulse motion-reduce:animate-none h-4 w-2/3 rounded bg-gray-100 dark:bg-gray-800/60"></div>
      <div class="animate-pulse motion-reduce:animate-none h-16 rounded-lg bg-gray-100 dark:bg-gray-800/60"></div>
      <span class="sr-only">Loading where this ask came from…</span>
    </div>

    <LoadFailed
      v-else-if="view.state === 'failed'"
      dense
      title="Couldn't load where this ask came from"
      message="You can still answer it above."
      :retrying="busy"
      data-testid="inbox-ask-context-failed"
      @retry="load"
    />

    <template v-else>
      <p class="text-[12.5px] text-gray-600 dark:text-gray-300" data-testid="inbox-ask-context-meta">{{ meta.join(' · ') }}</p>

      <!-- Where it came from -->
      <div v-if="ctx.origin" class="mt-4" data-testid="inbox-ask-context-origin">
        <h3 :class="OVERLINE">Where it came from</h3>
        <template v-if="ctx.origin.verified">
          <p class="mt-1 text-sm font-medium text-gray-900 dark:text-gray-100 truncate">{{ ctx.origin.title || 'Untitled chat' }}</p>
          <ol v-if="ctx.origin.messages.length" class="mt-2 space-y-2">
            <li
              v-for="m in ctx.origin.messages"
              :key="m.id"
              class="text-[12.5px] text-gray-700 dark:text-gray-300"
              data-testid="inbox-ask-context-message"
            >
              <span class="font-semibold text-gray-900 dark:text-gray-100">{{ m.role === 'user' ? 'You' : (agentLabel || ask.agent_name) }}</span>
              <span class="mx-1" aria-hidden="true">·</span>{{ m.excerpt }}
            </li>
          </ol>
        </template>
        <p v-else class="mt-1 text-[12.5px] text-gray-600 dark:text-gray-300">
          {{ ctx.origin.is_main ? 'Filed in your Main chat' : `Filed in “${ctx.origin.title || 'a chat'}”` }}
        </p>
        <button
          type="button"
          class="mt-2 text-[12.5px] font-medium text-action-primary-600 dark:text-action-primary-400 hover:underline rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
          data-testid="inbox-ask-context-open"
          @click="emit('open-chat', originUrl)"
        >Open the conversation</button>
      </div>

      <!-- Delivered in that chat — a verified thread only: what Main holds is
           usually unrelated to the ask. -->
      <div v-if="delivered.length" class="mt-4" data-testid="inbox-ask-context-delivered">
        <h3 :class="OVERLINE">Delivered in that chat</h3>
        <ul class="mt-1 space-y-1">
          <li v-for="d in delivered" :key="d.id">
            <button
              type="button"
              class="text-left text-[12.5px] text-action-primary-600 dark:text-action-primary-400 hover:underline rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
              :data-testid="`inbox-ask-context-delivered-${d.id}`"
              @click="emit('open-chat', `/workspace/c/${ctx.origin.chat_id}?anchor=d:${d.id}`)"
            >{{ d.title || 'Untitled deliverable' }}</button>
          </li>
        </ul>
      </div>
      <p v-else-if="deliveredFailed" class="mt-4 text-[12.5px] text-gray-600 dark:text-gray-300" data-testid="inbox-ask-context-delivered-failed">
        Couldn't load what was delivered in that chat.
      </p>

      <!-- Your recent answers -->
      <div v-if="ctx.recent_answers.length" class="mt-4" data-testid="inbox-ask-context-answers">
        <h3 :class="OVERLINE">Your recent answers</h3>
        <ul class="mt-1 space-y-2">
          <li v-for="r in ctx.recent_answers" :key="r.id" class="text-[12.5px] text-gray-700 dark:text-gray-300">
            <span class="font-medium text-gray-900 dark:text-gray-100">{{ r.title }}</span>
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
const EMPTY = Object.freeze({ origin: null, run: null, recent_answers: [] })

const store = useClientPortalStore()
const ctx = ref(EMPTY)
const loaded = ref(false)
const failed = ref(false)
const busy = ref(false)
const delivered = ref([])
const deliveredFailed = ref(false)

// An ask with nothing more to say still shows its meta line, so a loaded
// context is never "empty" here.
const view = computed(() => viewState({ hasLoaded: loaded.value, error: failed.value }))
const meta = computed(() => askContextMeta(props.ask, ctx.value.run))
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
    ctx.value = {
      origin: data?.origin || null,
      run: data?.run || null,
      recent_answers: Array.isArray(data?.recent_answers) ? data.recent_answers : [],
    }
    loaded.value = true
    loadDelivered(mine)
  } catch {
    if (mine !== seq) return
    failed.value = true
  } finally {
    if (mine === seq) busy.value = false
  }
}
async function loadDelivered(mine) {
  delivered.value = []
  deliveredFailed.value = false
  const o = ctx.value.origin
  if (!o || !o.verified) return
  try {
    const list = await store.fetchSessionDeliverablesStrict(props.ask.agent_name, o.chat_id)
    if (mine === seq) delivered.value = Array.isArray(list) ? list : []
  } catch {
    if (mine === seq) deliveredFailed.value = true
  }
}

watch(() => props.ask.id, () => {
  ctx.value = EMPTY
  loaded.value = false
  load()
}, { immediate: true })
</script>
