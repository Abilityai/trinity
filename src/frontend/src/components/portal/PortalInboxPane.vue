<!--
  trinity-enterprise#610 — the Inbox's reading pane (D11).

  An ASK renders through `PortalAsks` itself, filtered to that one ask and
  namespaced `inbox-ask` so it never shares an address with the Work tab's copy
  of the same ask (D8). It stays selected after an answer and is drawn ended.

  A CHAT renders its newest arrivals: one bounded history read (`limit 50`)
  shown from the first unread message onward, plus that chat's deliverables
  through the SAME renderer the conversation uses (`ReportRenderer`, with the
  client-side `ReportSummary` fallback — never the raw JSON dump). Two reads per
  selection, the ones the chat itself makes. No cost, no execution id: nothing
  here renders either (AC 7).

  `item` is the snapshot taken when the row was opened, so the first unread id
  survives the read that zeroes the count.

  §3g S5 (D-3): the pane says when a chat is RENDERED — `rendered(key)`, once
  the history, the strict deliverables list and every deliverable payload have
  settled successfully — and the container reads the chat only then. A payload
  that fails leaves the chat unread (its error is in the card), and the header's
  "Mark read" stays to hand; a failed read write is `readFailed`.
-->
<template>
  <section class="flex flex-col min-h-0 h-full" :aria-labelledby="headingId" data-testid="inbox-pane">
    <header class="shrink-0 flex flex-wrap items-center gap-2 px-4 py-3 border-b border-gray-200 dark:border-gray-750">
      <BaseButton
        v-if="showBack"
        variant="ghost"
        size="sm"
        data-testid="inbox-pane-back"
        @click="$emit('back')"
      >
        <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 19l-7-7 7-7" /></svg>
        Back
      </BaseButton>
      <h2
        :id="headingId"
        ref="headingEl"
        tabindex="-1"
        class="min-w-0 flex-1 basis-48 text-sm font-medium truncate text-gray-900 dark:text-gray-100 focus:outline-none"
        data-testid="inbox-pane-heading"
      >{{ heading }}</h2>
      <!-- The title keeps at least 12rem; on a phone the two actions wrap to their
           own line rather than squeezing it to a few characters. -->
      <div v-if="item.type === 'thread'" class="ml-auto flex items-center gap-2" data-testid="inbox-pane-actions">
        <BaseButton
          v-if="item.n > 0"
          variant="ghost"
          size="sm"
          data-testid="inbox-pane-mark-read"
          @click="$emit('mark-read')"
        >Mark read</BaseButton>
        <BaseButton variant="secondary" size="sm" data-testid="inbox-pane-reply" @click="$emit('reply', target)">Reply in chat</BaseButton>
        <BaseButton variant="primary" size="sm" data-testid="inbox-pane-open" @click="$emit('open-chat', target)">Open in chat</BaseButton>
      </div>
    </header>

    <div class="flex-1 min-h-0 overflow-y-auto px-4 py-4">
      <InlineError
        v-if="readFailed"
        class="mb-3"
        message="Couldn't mark this chat read. It keeps its count — try Mark read again."
        data-testid="inbox-pane-read-error"
        @dismiss="$emit('dismiss-read-error')"
      />
      <!-- An ask: the card itself. -->
      <PortalAsks
        v-if="item.type === 'ask'"
        :ask-ids="[item.id]"
        testid-prefix="inbox-ask"
        @open-thread="(t) => $emit('open-thread', t)"
      />

      <template v-else>
        <div v-if="view.state === 'loading'" class="space-y-3" aria-busy="true" data-testid="inbox-pane-loading">
          <div v-for="i in 3" :key="i" class="animate-pulse motion-reduce:animate-none h-12 rounded-lg bg-gray-100 dark:bg-gray-800/60"></div>
          <span class="sr-only">Loading this conversation…</span>
        </div>
        <LoadFailed
          v-else-if="view.state === 'failed'"
          dense
          title="Couldn't load this conversation"
          message="The chat is still there — try again, or open it."
          :retrying="busy"
          data-testid="inbox-pane-failed"
          @retry="load"
        />
        <template v-else>
          <p
            v-if="windowed.earlier > 0"
            class="mb-3 text-xs text-gray-600 dark:text-gray-300"
            data-testid="inbox-pane-earlier"
          >
            {{ windowed.earlier }} earlier {{ windowed.earlier === 1 ? 'arrival' : 'arrivals' }} —
            <button type="button" class="text-action-primary-600 dark:text-action-primary-400 hover:underline" @click="$emit('open-chat', target)">Open in chat</button>
          </p>
          <div
            v-for="(m, i) in windowed.shown"
            :key="m.id || i"
            class="mb-3"
            :data-testid="`inbox-pane-message-${m.id || i}`"
          >
            <p class="mb-1 text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
              {{ m.role === 'user' ? 'You' : agentLabel }}
              <span class="normal-case font-normal tabular-nums" :title="absolute(m.created_at)"> · {{ relative(m.created_at) }}</span>
            </p>
            <PortalMarkdown :content="m.content || ''" />
          </div>
          <p
            v-if="!windowed.shown.length && !deliverables.length"
            class="text-sm text-gray-600 dark:text-gray-300"
            data-testid="inbox-pane-empty"
          >Nothing new in this chat. Open it to see the whole conversation.</p>

          <section v-if="deliverables.length" class="mt-4" aria-label="Deliverables in this chat">
            <h3 class="mb-2 text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Delivered here</h3>
            <BaseCard
              v-for="d in deliverables"
              :key="d.id"
              class="mb-2"
              :data-testid="`inbox-pane-deliverable-${d.id}`"
            >
              <p class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ d.title || d.report_type }}</p>
              <p class="mb-2 text-xs text-gray-500 dark:text-gray-400">{{ d.report_type }} · <span :title="absolute(d.created_at)">{{ relative(d.created_at) }}</span></p>
              <InlineError
                v-if="payloadErrors[d.id]"
                :message="payloadErrors[d.id]"
                retryable
                @retry="retryPayload(d)"
                @dismiss="delete payloadErrors[d.id]"
              />
              <div v-else-if="!payloads[d.id]" class="animate-pulse motion-reduce:animate-none h-8 rounded-lg bg-gray-100 dark:bg-gray-800/60" aria-busy="true"></div>
              <ReportRenderer
                v-else
                :report-type="d.report_type"
                :display-hint="d.display_hint"
                :payload="payloads[d.id]"
                :fallback-component="ReportSummary"
              />
            </BaseCard>
          </section>
        </template>
      </template>
    </div>
  </section>
</template>

<script setup>
import { computed, nextTick, reactive, ref, watch } from 'vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseCard from '@/components/base/BaseCard.vue'
import LoadFailed from '@/components/LoadFailed.vue'
import InlineError from '@/components/InlineError.vue'
import ReportRenderer from '@/components/reports/ReportRenderer.vue'
import ReportSummary from '@/components/reports/ReportSummary.vue'
import PortalAsks from './PortalAsks.vue'
import PortalMarkdown from './PortalMarkdown.vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import { relativeTime } from './portalUtils'
import { PANE_HISTORY_LIMIT, paneWindow, openInChatTarget } from './portalInbox'
import { viewState } from '@/utils/loadingState'
import { formatLocalDateTime } from '@/utils/timestamps'

const props = defineProps({
  item: { type: Object, required: true },
  agentLabel: { type: String, default: '' },
  showBack: { type: Boolean, default: false },
  // §3g S5: the container's read write for this chat failed.
  readFailed: { type: Boolean, default: false },
})
const emit = defineEmits(['back', 'open-chat', 'reply', 'open-thread', 'rendered', 'mark-read', 'dismiss-read-error'])

const store = useClientPortalStore()
const headingEl = ref(null)
const headingId = computed(() => `inbox-pane-h-${props.item.key}`)

const heading = computed(() => {
  const it = props.item
  if (it.type === 'ask') return `${props.agentLabel} asks: ${it.title}`
  const chat = it.is_main ? 'Main' : (it.title || 'Chat')
  return `${props.agentLabel} · ${chat}`
})
const target = computed(() => openInChatTarget(props.item))

// ---- a chat's arrivals ---------------------------------------------------------
const messages = ref([])
const deliverables = ref([])
const loaded = ref(false)
const failed = ref(false)
const busy = ref(false)
const payloads = reactive({})
const payloadErrors = reactive({})
let gen = 0

const view = computed(() => viewState({ hasLoaded: loaded.value, error: failed.value }))
const windowed = computed(() => paneWindow(messages.value, props.item.first_unread_message_id, props.item.n))

async function load() {
  const it = props.item
  if (it.type !== 'thread') return
  const mine = ++gen
  busy.value = true
  try {
    // `fetchSessionDeliverables` swallows to [] (a chat must survive a failed
    // list), which here would read as "no deliverables" — so the pane reads
    // the list through the same endpoint and treats a throw as a failure.
    const [hist, reps] = await Promise.all([
      store.fetchHistory(it.agent_name, it.id, { limit: PANE_HISTORY_LIMIT }),
      store.fetchSessionDeliverablesStrict(it.agent_name, it.id),
    ])
    if (mine !== gen) return
    messages.value = hist?.messages || []
    deliverables.value = Array.isArray(reps) ? reps : []
    loaded.value = true
    failed.value = false
    await Promise.all(deliverables.value.map((d) => loadPayload(d)))
    if (mine === gen) maybeRendered()
  } catch {
    if (mine !== gen) return
    failed.value = true
  } finally {
    if (mine === gen) busy.value = false
  }
}

async function loadPayload(d) {
  delete payloadErrors[d.id]
  const mine = gen
  try {
    // fetchAgentReport returns the whole report row; the renderer takes its
    // payload, exactly as PortalDeliverables does.
    const full = await store.fetchAgentReport(props.item.agent_name, d.id, { rowsLimit: 50 })
    if (mine !== gen) return false
    payloads[d.id] = full?.payload ?? {}
    return true
  } catch {
    if (mine !== gen) return false
    payloadErrors[d.id] = "Couldn't load this deliverable. Try again, or open the chat."
    return false
  }
}

// Rendered = the history loaded AND every deliverable's payload is on screen.
// Said once per selection, after the DOM has it; a retried payload that lands
// completes it.
let renderedFor = null
async function maybeRendered() {
  const key = props.item.key
  if (renderedFor === key || !loaded.value || failed.value) return
  if (!deliverables.value.every((d) => payloads[d.id] !== undefined)) return
  renderedFor = key
  await nextTick()
  emit('rendered', key)
}
async function retryPayload(d) {
  if (await loadPayload(d)) maybeRendered()
}

watch(() => props.item.key, () => {
  messages.value = []; deliverables.value = []
  loaded.value = false; failed.value = false
  for (const k of Object.keys(payloads)) delete payloads[k]
  for (const k of Object.keys(payloadErrors)) delete payloadErrors[k]
  renderedFor = null
  load()
}, { immediate: true })

const relative = (iso) => relativeTime(iso)
let zone = ''
try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone || '' } catch { zone = '' }
const absolute = (iso) => (iso ? `${formatLocalDateTime(iso)}${zone ? ` (${zone})` : ''}` : undefined)

// D12: on phone the pane replaces the list, so focus moves to its heading.
function focusHeading() { nextTick(() => headingEl.value?.focus?.()) }
defineExpose({ focusHeading })
</script>
