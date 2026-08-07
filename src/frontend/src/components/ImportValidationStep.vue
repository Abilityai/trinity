<!--
  ImportValidationStep.vue (trinity-enterprise#15)

  Post-create validation step for github-sourced agents. CreateAgentModal swaps
  its form body for this component right after a successful create (the
  `created` event has already fired — this step is purely advisory and never
  blocks or undoes creation; Close is always available).

  Flow (honest states — loading ≠ empty ≠ failed):
    waiting     — polling GET /api/agents/{name}/info every 2s (≤ ~60s) until
                  the agent-server answers (implies startup.sh/clone finished).
                  Around poll 5 the container status is checked via the agents
                  store — a non-running container short-circuits to `failed`.
    result      — /info answered → STATIC compatibility report (no AI spend):
                  green "Looks compatible" when clean, else hard/soft/info
                  counts + a bounded list of hard-issue messages.
    unavailable — the report came back overall_status="unavailable" (workspace
                  not readable) → honest message + pointer to Overview.
    failed      — container isn't running: the initial clone likely failed.
    timeout     — container runs but /info never answered inside the window:
                  setup continues in the background; nothing is lost.

  No spinner: a progressive text label is the sanctioned in-flight treatment
  here (the scanline primitive is for data panes). No animation → nothing to
  gate on prefers-reduced-motion.

  New file per the design-system contract ratchet: zero raw non-gray palette
  classes, zero hardcoded colors; gray chrome follows the contract's
  surface/ink ladder (dark meta text gray-400, never gray-500).
-->
<template>
  <div>
    <div class="bg-white dark:bg-gray-800 px-4 pt-5 pb-4 sm:p-6 sm:pb-4">
      <h3 class="text-lg leading-6 font-medium text-gray-900 dark:text-white">
        Agent <span class="font-mono">{{ agentName }}</span> created
      </h3>

      <!-- Copy-intent provenance: the only durable record of what a snapshot
           agent was cloned from (present only on copy-intent responses). -->
      <p v-if="importSnapshot" class="mt-1 text-xs text-gray-500 dark:text-gray-400">
        Snapshot of <span class="font-mono">{{ importSnapshot.source_repo }}</span>
        <template v-if="shortSha"> @ <span class="font-mono">{{ shortSha }}</span></template>
        <template v-if="importSnapshot.file_count != null">
          · <span class="tabular-nums">{{ importSnapshot.file_count }}</span> files
        </template>
      </p>

      <!-- One shared footprint for every state — nothing shifts on arrival. -->
      <div class="mt-4 min-h-28" aria-live="polite">
        <!-- (a) waiting -->
        <template v-if="phase === 'waiting'">
          <p class="text-sm font-medium text-gray-900 dark:text-gray-100">Setting up workspace…</p>
          <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
            {{ polls > 10
              ? 'Still working — large repositories can take longer. You can close this and check back on the agent page.'
              : 'Cloning the repository and starting the agent. Usually ready in under a minute.' }}
          </p>
        </template>

        <!-- (b) compatibility result -->
        <template v-else-if="phase === 'result'">
          <div
            v-if="isClean"
            class="flex items-start gap-2 p-3 rounded-lg border border-status-success-200 dark:border-status-success-800 bg-status-success-50 dark:bg-status-success-900/30"
          >
            <svg class="w-4 h-4 mt-0.5 shrink-0 text-status-success-500" fill="currentColor" viewBox="0 0 20 20">
              <path fill-rule="evenodd" d="M16.7 5.3a1 1 0 010 1.4l-8 8a1 1 0 01-1.4 0l-4-4a1 1 0 011.4-1.4l3.3 3.3 7.3-7.3a1 1 0 011.4 0z" clip-rule="evenodd" />
            </svg>
            <div class="min-w-0">
              <p class="text-sm font-medium text-status-success-700 dark:text-status-success-300">Looks compatible</p>
              <p class="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
                No hard or soft issues found. The full checklist lives on the agent's Overview tab.
              </p>
            </div>
          </div>

          <div v-else>
            <div class="flex items-center gap-2 flex-wrap">
              <span class="text-sm font-medium text-gray-900 dark:text-gray-100">Compatibility check</span>
              <span class="text-[11px] uppercase font-semibold px-1.5 py-0.5 rounded bg-status-danger-100 text-status-danger-700 dark:bg-status-danger-900/40 dark:text-status-danger-300">
                <span class="tabular-nums">{{ hardCount }}</span> hard
              </span>
              <span class="text-[11px] uppercase font-semibold px-1.5 py-0.5 rounded bg-status-warning-100 text-status-warning-700 dark:bg-status-warning-900/40 dark:text-status-warning-300">
                <span class="tabular-nums">{{ softCount }}</span> soft
              </span>
              <span class="text-[11px] uppercase font-semibold px-1.5 py-0.5 rounded bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300">
                <span class="tabular-nums">{{ infoCount }}</span> info
              </span>
            </div>

            <div v-if="hardIssues.length > 0" class="mt-2">
              <p class="text-xs font-medium text-gray-700 dark:text-gray-300">
                Hard issues (<span class="tabular-nums">{{ hardIssues.length }}</span>) — fix these for the agent to work reliably:
              </p>
              <ul class="mt-1 max-h-32 overflow-y-auto space-y-1 border border-gray-200 dark:border-gray-700 rounded-md p-2">
                <li v-for="c in hardIssues" :key="c.check_id" class="flex items-start gap-2 text-xs">
                  <span class="font-mono shrink-0 text-gray-500 dark:text-gray-400">{{ c.check_id }}</span>
                  <span class="text-gray-700 dark:text-gray-300">{{ c.message }}</span>
                </li>
              </ul>
            </div>
            <p class="mt-2 text-xs text-gray-500 dark:text-gray-400">
              Details and one-click fixes are on the agent's Overview tab.
            </p>
          </div>
        </template>

        <!-- (c) report unavailable -->
        <template v-else-if="phase === 'unavailable'">
          <p class="text-sm font-medium text-gray-900 dark:text-gray-100">Compatibility couldn't be checked yet</p>
          <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
            {{ reportMessage || 'The workspace wasn\'t readable while the agent was starting.' }}
            Open the agent's Overview tab to re-run the check once it's up.
          </p>
        </template>

        <!-- (d) failed to start -->
        <template v-else-if="phase === 'failed'">
          <div class="p-3 rounded-lg border border-status-danger-200 dark:border-status-danger-800 bg-status-danger-50 dark:bg-status-danger-900/30">
            <p class="text-sm font-medium text-status-danger-700 dark:text-status-danger-300">
              The agent didn't start — the initial clone may have failed.
            </p>
            <p class="mt-1 text-xs text-gray-600 dark:text-gray-300">
              Open the agent page to check its logs, and verify the repository is
              reachable (correct <span class="font-mono">owner/repo</span>, and a token if it's private).
              The agent itself was created — nothing is lost.
            </p>
          </div>
        </template>

        <!-- workspace still preparing after the polling window -->
        <template v-else-if="phase === 'timeout'">
          <p class="text-sm font-medium text-gray-900 dark:text-gray-100">Workspace is still being prepared</p>
          <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
            Setup is taking longer than a minute — large repositories can. It continues in the
            background; the compatibility check runs on the agent's Overview tab when it's ready.
          </p>
        </template>
      </div>
    </div>

    <!-- Always-visible actions: this step is skippable and never blocks creation. -->
    <div class="bg-gray-50 dark:bg-gray-900 px-4 py-3 sm:px-6 sm:flex sm:flex-row-reverse">
      <button
        ref="primaryBtn"
        type="button"
        @click="goTo('overview')"
        class="w-full inline-flex justify-center rounded-md border border-transparent shadow-sm px-4 py-2 bg-action-primary-600 text-base font-medium text-white hover:bg-action-primary-700 focus:outline-none focus:ring-2 focus:ring-offset-2 dark:focus:ring-offset-gray-800 focus:ring-action-primary-500 sm:ml-3 sm:w-auto sm:text-sm"
      >
        Open agent
      </button>
      <button
        type="button"
        @click="goTo('credentials')"
        class="mt-3 w-full inline-flex justify-center rounded-md border border-gray-300 dark:border-gray-600 shadow-sm px-4 py-2 bg-white dark:bg-gray-700 text-base font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-600 focus:outline-none focus:ring-2 focus:ring-offset-2 dark:focus:ring-offset-gray-800 focus:ring-action-primary-500 sm:mt-0 sm:ml-3 sm:w-auto sm:text-sm"
      >
        Add credentials
      </button>
      <button
        type="button"
        @click="$emit('close')"
        class="mt-3 w-full inline-flex justify-center rounded-md border border-gray-300 dark:border-gray-600 shadow-sm px-4 py-2 bg-white dark:bg-gray-700 text-base font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-600 focus:outline-none focus:ring-2 focus:ring-offset-2 dark:focus:ring-offset-gray-800 focus:ring-action-primary-500 sm:mt-0 sm:ml-3 sm:w-auto sm:text-sm"
      >
        Close
      </button>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { useAgentsStore } from '../stores/agents'
import api from '../api'

const props = defineProps({
  agentName: { type: String, required: true },
  // {source_repo, source_branch, head_sha, file_count} — copy intent only.
  importSnapshot: { type: Object, default: null },
})
const emit = defineEmits(['close'])

const router = useRouter()
const agentsStore = useAgentsStore()

const POLL_INTERVAL_MS = 2000
const MAX_POLLS = 30 // × 2s ≈ 60s window
const STATUS_CHECK_AT_POLL = 5 // container-liveness check after ~5 tries

const phase = ref('waiting') // waiting | result | unavailable | failed | timeout
const report = ref(null)
const polls = ref(0)
const primaryBtn = ref(null)

let timer = null
let stopped = false

const shortSha = computed(() => (props.importSnapshot?.head_sha || '').slice(0, 7))
const isClean = computed(() => report.value?.overall_status === 'compatible')
const hardCount = computed(() => report.value?.hard_count || 0)
const softCount = computed(() => report.value?.soft_count || 0)
const infoCount = computed(() => report.value?.info_count || 0)
const reportMessage = computed(() => report.value?.message || '')
const hardIssues = computed(() =>
  (report.value?.checks || []).filter((c) => c.status === 'fail' && c.severity === 'hard')
)

// Is the container actually running? Refreshes the agents store list (which
// also makes the new agent appear behind the modal). Fail-open: a transient
// list-fetch error must not declare the agent dead.
async function containerRunning() {
  try {
    await agentsStore.fetchAgents()
    const row = (agentsStore.agents || []).find((a) => a.name === props.agentName)
    return row ? row.status === 'running' : true
  } catch {
    return true
  }
}

async function runCompatibility() {
  try {
    // STATIC checks only — no include_ai, no token spend (stores/agents.js:396).
    const r = await agentsStore.getCompatibility(props.agentName)
    report.value = r
    phase.value = r?.overall_status === 'unavailable' ? 'unavailable' : 'result'
  } catch {
    phase.value = 'unavailable'
  }
}

async function tick() {
  if (stopped) return
  polls.value++

  // Readiness = a REAL agent-server /info response. The backend proxy
  // fail-opens to HTTP 200 with a fallback body carrying a `message` key
  // while the container is mid-clone (routers/agent_files.py catch-all), so
  // a bare 200 is NOT readiness — compat would run against a half-cloned
  // workspace and persist false HARD failures (review F1). Real agent-server
  // template-info responses never carry `message`; old agent images (no
  // /info endpoint) keep the fallback shape and honestly land in the
  // 'timeout' state instead of a false-red report.
  try {
    const resp = await api.get(
      `/api/agents/${encodeURIComponent(props.agentName)}/info`
    )
    if (!resp?.data?.message) {
      if (!stopped) await runCompatibility()
      return
    }
  } catch {
    /* not up yet — fall through to liveness / retry */
  }

  // Liveness check every STATUS_CHECK_AT_POLL polls (not just once) so a
  // container dying mid-window surfaces within ~10s, not at the 60s cap.
  if (polls.value % STATUS_CHECK_AT_POLL === 0) {
    if (!(await containerRunning())) {
      if (!stopped) phase.value = 'failed'
      return
    }
  }

  if (polls.value >= MAX_POLLS) {
    const running = await containerRunning()
    if (!stopped) phase.value = running ? 'timeout' : 'failed'
    return
  }

  timer = setTimeout(tick, POLL_INTERVAL_MS)
}

function goTo(tab) {
  emit('close')
  // Both tab keys verified against AgentDetail.vue DEEP_LINK_TABS
  // ('overview', 'credentials').
  router.push({ path: `/agents/${props.agentName}`, query: { tab } })
}

function onKeydown(e) {
  if (e.key === 'Escape') {
    e.preventDefault()
    emit('close')
  }
}

onMounted(() => {
  document.addEventListener('keydown', onKeydown)
  timer = setTimeout(tick, POLL_INTERVAL_MS)
  nextTick(() => primaryBtn.value?.focus())
})

onUnmounted(() => {
  // The OnboardingWizard unmounts CreateAgentModal on `created` (its own
  // credential step takes over) — polling must die with the component.
  stopped = true
  document.removeEventListener('keydown', onKeydown)
  if (timer) clearTimeout(timer)
})
</script>
