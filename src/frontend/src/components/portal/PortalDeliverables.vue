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
const errors = reactive({})

const kindLabel = deliverableKindLabel
const relative = relativeTime

async function load() {
  if (!props.sessionId) { items.value = []; return }
  // The store action is fail-soft: a chat that cannot list its deliverables is
  // still a working chat, so this never surfaces an error of its own.
  items.value = await store.fetchSessionDeliverables(props.agentName, props.sessionId)
}

async function loadPayload(d) {
  delete errors[d.id]
  try {
    const full = await store.fetchAgentReport(props.agentName, d.id)
    payloads[d.id] = full?.payload ?? {}
  } catch (e) {
    errors[d.id] = e?.response?.data?.detail || "Couldn't open this deliverable."
  }
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
  for (const k of Object.keys(errors)) delete errors[k]
  load()
}, { immediate: true })

watch(() => props.refreshKey, load)
</script>
