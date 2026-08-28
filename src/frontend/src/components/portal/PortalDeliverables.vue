<template>
  <!-- ent#365: deliverables produced in THIS chat, as cards in the chat.
       Reports were operator-scoped scrollback before this: an agent published
       one and the person it was for had no way to see it except by asking. The
       card is the thing itself — type, when, and the payload one click away —
       rendered through the SAME `components/reports/` dispatch the agent page
       and Agent Detail use (Technical Notes: "do not build a second rendering
       layer"). -->
  <section v-if="items.length" class="mt-6" aria-label="Deliverables from this conversation">
    <h3 class="mb-2 text-[11px] font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400">
      Delivered here
    </h3>

    <div
      v-for="d in items"
      :key="d.id"
      class="mb-2 rounded-xl border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900"
    >
      <button
        class="w-full px-3.5 py-3 flex items-center gap-3 text-left"
        :aria-expanded="open === d.id"
        @click="toggle(d.id)"
      >
        <span
          class="shrink-0 inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium bg-action-primary-100 text-action-primary-700 dark:bg-action-primary-500/16 dark:text-action-primary-300"
        >{{ kindLabel(d.display_hint) }}</span>
        <span class="min-w-0 flex-1">
          <span class="block text-sm font-medium truncate">{{ d.title || d.report_type }}</span>
          <span class="block text-xs text-gray-500 dark:text-gray-400">{{ d.report_type }} · {{ relative(d.created_at) }}</span>
        </span>
        <svg
          class="w-4 h-4 text-gray-400 shrink-0 transition"
          :class="{ 'rotate-180': open === d.id }"
          fill="none" viewBox="0 0 24 24" stroke="currentColor"
        ><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" /></svg>
      </button>

      <div v-if="open === d.id" class="px-3.5 pb-3 border-t border-gray-100 dark:border-gray-800">
        <!-- A failed payload read is a failed VERB with a home next to its own
             control, never a console line (design contract #18). -->
        <InlineError
          v-if="errors[d.id]"
          class="mt-3"
          :message="errors[d.id]"
          retryable
          @retry="loadPayload(d)"
          @dismiss="delete errors[d.id]"
        />
        <div v-else-if="!payloads[d.id]" class="pt-3 space-y-2" aria-busy="true">
          <div v-for="row in 2" :key="row" class="animate-pulse motion-reduce:animate-none h-8 rounded-lg bg-gray-100 dark:bg-gray-800/60"></div>
          <span class="sr-only">Loading this deliverable…</span>
        </div>
        <div v-else class="pt-3">
          <!-- `:fallback-component` is the client-side rule from #2162: where an
               operator surface falls back to the raw JSON viewer, a client gets
               a bounded, humanised summary and never the raw payload. -->
          <ReportRenderer
            :report-type="d.report_type"
            :display-hint="d.display_hint"
            :payload="payloads[d.id]"
            :fallback-component="ReportSummary"
          />
          <!-- The card shows a first page, so it says so rather than letting a
               slice read as the whole table. The agent page is where a full
               table is read; pointing there is more honest than a "load more"
               that would page a preview inside a conversation. -->
          <p
            v-if="hasMoreRows(d.id)"
            class="mt-2 text-xs text-gray-500 dark:text-gray-400"
          >
            Showing the first {{ shownRows(d.id) }} of
            {{ rowMeta[d.id].total }} rows — open this agent's Reports for all of it.
          </p>
          <!-- ent#366: "Useful / Not what I needed" on the work itself — the
               affordance ent#365 left this card as the surface for. Different
               words from a message's thumbs because a deliverable is judged as
               a piece of work, not as an answer. -->
          <PortalRating
            :agent-name="agentName"
            target-kind="deliverable"
            :target-id="d.id"
          />
        </div>
      </div>
    </div>
  </section>
</template>

<script setup>
import { ref, reactive, watch } from 'vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import ReportRenderer from '@/components/reports/ReportRenderer.vue'
import ReportSummary from '@/components/reports/ReportSummary.vue'
import InlineError from '@/components/InlineError.vue'
import PortalRating from './PortalRating.vue'
import { deliverableKindLabel, relativeTime } from './portalUtils'

const props = defineProps({
  agentName: { type: String, required: true },
  // The chat whose deliverables these are. Null before a thread exists, which
  // is not an error — a conversation with no turns has produced nothing.
  sessionId: { type: String, default: null },
  // Bumped by the parent after a turn completes, so a deliverable published
  // during that turn appears without the client reloading the page.
  refreshKey: { type: Number, default: 0 },
})

const store = useClientPortalStore()
const items = ref([])
const open = ref(null)
const payloads = reactive({})
const rowMeta = reactive({})
const errors = reactive({})

const kindLabel = deliverableKindLabel
const relative = relativeTime

async function load() {
  if (!props.sessionId) { items.value = []; return }
  // The store action is fail-soft: a chat that cannot list its deliverables is
  // still a working chat, so this never surfaces an error of its own.
  items.value = await store.fetchSessionDeliverables(props.agentName, props.sessionId)
}

// Review finding: this called `fetchAgentReport` with no options, so
// `rows_limit` was omitted and the detail route returned the payload WHOLE — a
// tabular deliverable near the 5 MiB `REPORT_PAYLOAD_MAX_BYTES` ceiling shipped
// all of it to the browser and rendered every row, inside a chat card, while
// the sibling Reports tab pages the identical report through #2162's window.
// The card also had no "load more", so there was no bounded read available here
// at all.
//
// A first page is the right default for a card: it is a preview inside a
// conversation, not the reading surface. `row_meta.total` then tells the reader
// what they are seeing a slice OF, and the agent page remains where a full
// table is read.
const CARD_ROWS = 50

async function loadPayload(d) {
  delete errors[d.id]
  try {
    const full = await store.fetchAgentReport(props.agentName, d.id, {
      rowsOffset: 0,
      rowsLimit: CARD_ROWS,
    })
    payloads[d.id] = full?.payload ?? {}
    if (full?.row_meta) rowMeta[d.id] = full.row_meta
  } catch (e) {
    errors[d.id] = e?.response?.data?.detail || "Couldn't open this deliverable."
  }
}

// `row_meta` is `{total, offset, limit}` — the server does not send a
// `returned` count, so the shown count is derived rather than assumed.
function shownRows(id) {
  const meta = rowMeta[id]
  if (!meta) return 0
  // ent#365 review: `meta.limit ?? 0` rendered "Showing the first 0 of N rows"
  // when the server omitted `limit` — zero shown while `hasMoreRows` stayed
  // true, which is a sentence that cannot be right. An absent limit means the
  // server did not bound the slice, so what is shown is everything after the
  // offset.
  const remaining = Math.max(0, (meta.total ?? 0) - (meta.offset ?? 0))
  const limit = typeof meta.limit === 'number' && meta.limit > 0 ? meta.limit : remaining
  return Math.max(0, Math.min(limit, remaining))
}

function hasMoreRows(id) {
  const meta = rowMeta[id]
  return Boolean(meta) && (meta.total ?? 0) > shownRows(id)
}

function toggle(id) {
  if (open.value === id) { open.value = null; return }
  open.value = id
  const d = items.value.find((x) => x.id === id)
  if (d && !payloads[id]) loadPayload(d)
}

// Thread switch clears everything: an expanded payload belongs to the chat it
// was opened in, and carrying it across would render one conversation's
// deliverable inside another.
watch(() => [props.agentName, props.sessionId], () => {
  open.value = null
  for (const k of Object.keys(payloads)) delete payloads[k]
  for (const k of Object.keys(rowMeta)) delete rowMeta[k]
  for (const k of Object.keys(errors)) delete errors[k]
  load()
}, { immediate: true })

watch(() => props.refreshKey, load)
</script>
