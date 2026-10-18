<template>
  <!--
    The run's Tier-0 result (trinity-enterprise#935): deterministic checks of
    what the run left behind, from the platform's own records — not a grade of
    the answer. ONE headline, from the GATE checks only (TD-7): a failed gate
    fails the run, a skip never counts against it. Signals are recorded and
    listed, never counted and never worded as pass/fail. A 404 `not_evaluated`
    is a state of the run (manual and chat runs are not graded), not an error.
  -->
  <BaseCard data-testid="execution-checks">
    <div class="flex items-center justify-between gap-3">
      <h3 class="text-sm font-medium text-gray-500 dark:text-gray-400">Checks</h3>
      <span v-if="state === 'ready'" class="text-xs text-gray-500 dark:text-gray-400">
        deterministic checks, not answer quality
      </span>
    </div>

    <div
      v-if="state === 'loading'"
      class="mt-3 h-5 w-40 rounded bg-gray-100 dark:bg-gray-750 animate-pulse motion-reduce:animate-none"
      aria-busy="true"
    >
      <span class="sr-only">Loading checks</span>
    </div>

    <p
      v-else-if="state === 'failed'"
      class="mt-3 text-sm text-gray-600 dark:text-gray-300"
      data-testid="execution-checks-error"
    >
      Checks could not be loaded.
      <button type="button" class="ml-1 underline" @click="load">Retry</button>
    </p>

    <template v-else>
      <div class="mt-3 flex flex-wrap items-center gap-2">
        <BaseBadge
          :variant="VERDICTS[verdict].variant"
          :data-verdict="verdict"
          :title="TOOLTIP"
          data-testid="execution-checks-headline"
        >{{ VERDICTS[verdict].label }}</BaseBadge>
        <span
          v-if="evaluation"
          class="text-sm text-gray-600 dark:text-gray-300"
          data-testid="execution-checks-count"
        >{{ countLine }}</span>
        <span v-else class="text-sm text-gray-600 dark:text-gray-300">
          Scheduled runs, agent-triggered runs and runs another agent delegated are graded, about a minute after they finish.
        </span>
      </div>

      <ul v-if="failedGates.length" class="mt-3 space-y-1 text-sm" data-testid="execution-checks-failed">
        <li v-for="c in failedGates" :key="c.id" class="text-gray-900 dark:text-gray-100">
          <span class="font-mono text-xs">{{ c.id }}</span>
          <span v-if="c.reason" class="ml-2">{{ words(c.reason) }}</span>
          <span v-if="evidenceLine(c)" class="ml-2 font-mono text-xs text-gray-500 dark:text-gray-400">{{ evidenceLine(c) }}</span>
        </li>
      </ul>

      <div v-if="signals.length" class="mt-3 text-xs text-gray-500 dark:text-gray-400" data-testid="execution-checks-signals">
        <span class="font-medium">Signals (recorded, not counted):</span>
        <span v-for="c in signals" :key="c.id" class="ml-2">
          <span class="font-mono">{{ c.id }}</span> — {{ c.reason ? words(c.reason) : 'no anomaly' }}
        </span>
      </div>
    </template>
  </BaseCard>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import api from '../api'
import BaseCard from './base/BaseCard.vue'
import BaseBadge from './base/BaseBadge.vue'
import { viewState } from '../utils/loadingState'

const props = defineProps({
  agentName: { type: String, required: true },
  executionId: { type: String, required: true },
})

const TOOLTIP = 'Deterministic checks of what the run left behind, from platform records — not answer quality'

const VERDICTS = {
  pass: { label: 'Checks passed', variant: 'success' },
  fail: { label: 'Checks failed', variant: 'danger' },
  skip: { label: 'No gate check applied', variant: 'neutral' },
  none: { label: 'Not evaluated', variant: 'neutral' },
}

const evaluation = ref(null)
const hasLoaded = ref(false)
const error = ref(null)

const state = computed(() => {
  const { state: s } = viewState({ hasLoaded: hasLoaded.value, error: error.value })
  return s
})

const entries = computed(() => {
  const list = evaluation.value?.checks?.checks
  return Array.isArray(list) ? list : []
})
const gates = computed(() => entries.value.filter((c) => c.tier === 'gate'))
const signals = computed(() => entries.value.filter((c) => c.tier === 'signal'))
const failedGates = computed(() => gates.value.filter((c) => c.status === 'fail'))

// The stored headline is the server's gate-only count (tier0_evaluator.headline);
// a row without one is counted the same way from its gate entries.
const counts = computed(() => {
  const h = evaluation.value?.checks?.headline
  if (h && typeof h === 'object') {
    return { pass: h.gate_pass || 0, fail: h.gate_fail || 0, skip: h.gate_skip || 0 }
  }
  const n = (s) => gates.value.filter((c) => c.status === s).length
  return { pass: n('pass'), fail: n('fail'), skip: n('skip') }
})

const verdict = computed(() => {
  if (!evaluation.value) return 'none'
  if (counts.value.fail > 0) return 'fail'
  if (counts.value.pass > 0) return 'pass'
  return 'skip'
})

const countLine = computed(() => {
  const { pass, fail, skip } = counts.value
  const graded = pass + fail
  const skipped = skip ? `${skip} skipped` : ''
  if (!graded) return skipped
  const head = fail
    ? `${fail} of ${graded} gate checks failed`
    : `${pass} of ${graded} gate checks passed`
  return skipped ? `${head} · ${skipped}` : head
})

function words(reason) {
  return String(reason).replace(/_/g, ' ')
}

// Evidence is counts / ids / hashes by construction; scalars only, so a nested
// object never spills onto the page.
function evidenceLine(c) {
  const ev = c.evidence && typeof c.evidence === 'object' ? c.evidence : {}
  return Object.entries(ev)
    .filter(([, v]) => v === null || ['string', 'number', 'boolean'].includes(typeof v))
    .map(([k, v]) => `${k}: ${v}`)
    .join(' · ')
}

async function load() {
  error.value = null
  try {
    const { data } = await api.get(
      `/api/agents/${props.agentName}/executions/${props.executionId}/evaluation`
    )
    evaluation.value = data
    hasLoaded.value = true
  } catch (err) {
    if (err.response?.status === 404) {
      evaluation.value = null
      hasLoaded.value = true
    } else {
      error.value = err
    }
  }
}

onMounted(load)
</script>
