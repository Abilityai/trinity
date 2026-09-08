<!--
  A shared canvas, rendered read-only at a stable link (ent#554).

  `requiresAuth: false` — a `public` link must open for someone with no
  account. An `authorized` link answers 401 here and the page offers a sign-in
  rather than a dead end; the SERVER decides, never this component.

  ONE renderer: the blocks go through `CanvasBlock` exactly as they do on Agent
  Detail and in the rail, so a shared canvas cannot drift from what the owner
  sees. This view adds the frame — title, agent, freshness, the live notice —
  and the print stylesheet that makes "Download PDF" produce the document.
-->
<template>
  <div class="min-h-screen bg-gray-50 dark:bg-gray-950 print:bg-white">
    <div class="mx-auto max-w-3xl px-4 py-8 print:max-w-none print:px-0 print:py-0">

      <div v-if="view.state === 'loading'" class="py-16 text-center text-sm text-gray-500" data-testid="shared-canvas-loading">
        Loading…
      </div>

      <!-- Every refusal says which one it is and what to do about it. -->
      <div
        v-else-if="problem"
        class="rounded-xl border border-gray-200 bg-white p-8 text-center dark:border-gray-800 dark:bg-gray-900"
        data-testid="shared-canvas-problem"
      >
        <p class="text-sm font-semibold">{{ problem.title }}</p>
        <p class="mx-auto mt-2 max-w-md text-xs text-gray-500 dark:text-gray-400">{{ problem.body }}</p>
        <router-link
          v-if="problem.action === 'sign-in'"
          :to="{ path: '/login', query: { redirect: $route.fullPath } }"
          class="mt-4 inline-block rounded-lg bg-action-primary-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-action-primary-700"
          data-testid="shared-canvas-signin"
        >Sign in</router-link>
      </div>

      <template v-else-if="canvas">
        <header class="mb-4 print:mb-6">
          <div class="flex flex-wrap items-start gap-3">
            <div class="min-w-0 flex-1">
              <h1 class="truncate text-lg font-semibold">{{ canvas.title || canvas.canvas_id }}</h1>
              <p class="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
                by {{ agentName }} · {{ fresh.label }}
                <span v-if="fresh.stale"> · may be out of date</span>
              </p>
            </div>
            <!-- print:hidden — the controls are chrome, never part of the document. -->
            <div class="flex shrink-0 gap-2 print:hidden">
              <button
                class="rounded-lg border border-gray-300 px-2.5 py-1 text-xs font-medium hover:bg-gray-100 dark:border-gray-700 dark:hover:bg-gray-800"
                data-testid="shared-canvas-print"
                @click="downloadPdf"
              >Download PDF</button>
            </div>
          </div>

          <!-- AC #3: which it is, stated, never left ambiguous. -->
          <p class="mt-3 rounded-lg bg-gray-100 px-3 py-1.5 text-[11px] text-gray-600 dark:bg-gray-800 dark:text-gray-400 print:hidden"
             data-testid="shared-canvas-live-note">
            This view stays current — it shows the canvas as the agent updates it, not a copy taken when it was shared.
          </p>
          <p v-if="pdfNote" class="mt-2 text-[11px] text-status-warning-700 dark:text-status-warning-300 print:hidden"
             data-testid="shared-canvas-pdf-note">
            {{ pdfNote }}
          </p>
        </header>

        <!-- ONE document renderer, shared with every authenticated surface,
             so the PDF is identical wherever it was produced from (AC #7). -->
        <div class="rounded-xl border border-gray-200 bg-white p-5 dark:border-gray-800 dark:bg-gray-900"
             data-testid="shared-canvas-body">
          <CanvasDocument :canvas="canvas" :agent-name="agentName" />
        </div>

        <!-- The print copy, teleported to <body> exactly as the operator
             surfaces do. This page has less chrome around it, but "less" is
             not "none" — and using the same mechanism is what makes AC #7's
             "identical from every surface" true rather than approximately
             true. -->
        <Teleport to="body">
          <div v-if="printing" class="canvas-print-root" data-testid="shared-canvas-print-doc">
            <CanvasDocument :canvas="canvas" :agent-name="agentName" />
          </div>
        </Teleport>

      </template>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import api from '../api'
import CanvasDocument from '../components/canvas/CanvasDocument.vue'
import { freshness } from '../components/canvas/canvasUtils'
import { viewState } from '../utils/loadingState'
import { shareProblem } from '../components/canvas/canvasShare'

const route = useRoute()
// #1927 / design-system p13-p15: `loading` means "no data yet", never "a fetch
// is in flight". A bare `v-if="loading"` would blank a rendered canvas on any
// later refetch — the exact gate the ratchet exists to stop, and it caught this
// one. The page fetches once today, so this is the rule holding rather than a
// bug being fixed; it stays correct if a refresh is ever added.
const hasLoaded = ref(false)
const canvas = ref(null)
const agentName = ref('')
const problem = ref(null)
const pdfNote = ref('')
const printing = ref(false)

const fresh = computed(() => freshness(canvas.value || {}))
const view = computed(() => viewState({
  hasLoaded: hasLoaded.value,
  // A share refusal is not a load failure: `problem` already carries the
  // server's own words and its own branch, so it must not also collapse this
  // into the generic failed state.
  count: canvas.value ? 1 : 0,
}))

async function load() {
  problem.value = null
  try {
    const { data } = await api.get(`/api/public/canvas/${encodeURIComponent(route.params.token)}`)
    canvas.value = data.canvas
    agentName.value = data.agent_name
  } catch (e) {
    // The server names the state; the page turns it into words and an action.
    problem.value = shareProblem(e?.response?.status, e?.response?.data?.detail)
  } finally {
    hasLoaded.value = true
  }
}

async function downloadPdf() {
  // Print-first (AC #4): the browser's own PDF over a print stylesheet, so
  // there is ONE renderer and the document cannot drift from the screen. No
  // headless service to run, and charts/diagrams print as whatever the page
  // already drew them with.
  pdfNote.value = ''
  if (typeof window === 'undefined' || typeof window.print !== 'function') {
    // AC #6: degrade with words. The share link still works.
    pdfNote.value = 'This browser cannot produce a PDF here. The share link still works, and printing the page saves it as a PDF.'
    return
  }
  printing.value = true
  await nextTick()
  try {
    window.print()
  } catch (e) {
    pdfNote.value = 'The PDF could not be produced. Use your browser’s Print → Save as PDF.'
  } finally {
    printing.value = false
  }
}

onMounted(load)
</script>
