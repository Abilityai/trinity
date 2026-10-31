<template>
  <!--
    How the run graded (trinity-enterprise#935, #936, #938, #940), read in one
    call from the run's evaluation rows. ONE headline, from the Tier-0 GATE
    checks only (TD-7): a failed gate fails the run, a skip never counts
    against it. Every gate is listed with its outcome; failures carry their
    reason and evidence; signals are recorded and listed, never counted and
    never worded as pass/fail. The judge's verdict, when the run was sampled,
    and the hand-off's own checks, when another agent delegated it, follow as
    their own blocks. A 404 `not_evaluated` is a state of the run (manual and
    chat runs are not graded), not an error.
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

      <!-- Every gate, with its outcome, so a pass is as visible as a failure. -->
      <ul v-if="gates.length" class="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs" data-testid="execution-checks-gates">
        <li v-for="c in gates" :key="c.id" :data-status="c.status" class="flex items-center gap-1">
          <span :class="OUTCOME[c.status]?.tone || OUTCOME.skip.tone" aria-hidden="true">{{ OUTCOME[c.status]?.mark || OUTCOME.skip.mark }}</span>
          <span class="font-mono text-gray-700 dark:text-gray-300">{{ c.id }}</span>
          <span class="text-gray-500 dark:text-gray-400">{{ OUTCOME[c.status]?.word || c.status }}<template v-if="c.status === 'skip' && c.reason"> ({{ words(c.reason) }})</template></span>
        </li>
      </ul>

      <div v-if="signals.length" class="mt-3 text-xs text-gray-500 dark:text-gray-400" data-testid="execution-checks-signals">
        <span class="font-medium">Signals (recorded, not counted):</span>
        <span v-for="c in signals" :key="c.id" class="ml-2">
          <span class="font-mono">{{ c.id }}</span> — {{ c.reason ? words(c.reason) : 'no anomaly' }}
        </span>
      </div>

      <!-- The sampled judge's verdict (ent#936): quality out of 5, each
           dimension's verdict and its structural reason. A skipped row says
           why the run was not judged. -->
      <div
        v-if="judgeRow"
        class="mt-4 border-t border-gray-200 dark:border-gray-700 pt-3"
        data-testid="execution-checks-judge"
        :data-judge-state="judgeDoc?.state || 'unknown'"
      >
        <div class="flex flex-wrap items-center gap-2">
          <h4 class="text-sm font-medium text-gray-500 dark:text-gray-400">Judge</h4>
          <template v-if="judgeDoc?.state === 'judged'">
            <span class="text-sm font-semibold text-gray-900 dark:text-gray-100 tabular-nums" data-testid="execution-checks-judge-quality">{{ qualityText }}</span>
            <span v-if="judgeDoc.model" class="text-xs text-gray-500 dark:text-gray-400">{{ judgeDoc.model }}</span>
          </template>
          <span v-else class="text-sm text-gray-600 dark:text-gray-300" data-testid="execution-checks-judge-skip">
            not judged<template v-if="judgeDoc?.skip"> ({{ words(judgeDoc.skip) }})</template>
          </span>
        </div>
        <ul v-if="judgeDims.length" class="mt-2 space-y-1 text-sm" data-testid="execution-checks-judge-dims">
          <li v-for="d in judgeDims" :key="d.id" :data-verdict="d.verdict" class="flex flex-wrap items-baseline gap-x-2">
            <span :class="d.verdict === 'fail' ? 'text-status-danger-700 dark:text-status-danger-300' : d.verdict === 'pass' ? 'text-status-success-700 dark:text-status-success-300' : 'text-gray-500 dark:text-gray-400'" aria-hidden="true">{{ d.verdict === 'fail' ? '✕' : d.verdict === 'pass' ? '✓' : '–' }}</span>
            <span class="text-gray-900 dark:text-gray-100">{{ d.label }}</span>
            <span class="text-xs text-gray-500 dark:text-gray-400">{{ d.verdict === 'n/a' ? 'not applicable' : d.verdict }}</span>
            <span v-if="d.reason" class="text-xs text-gray-600 dark:text-gray-300">— {{ d.reason }}</span>
          </li>
        </ul>
      </div>

      <!-- The hand-off's own checks (ent#940): did the caller's dispatch hold up
           on both sides. Failures carry their reason code. -->
      <div
        v-if="pairRow"
        class="mt-4 border-t border-gray-200 dark:border-gray-700 pt-3"
        data-testid="execution-checks-pair"
      >
        <div class="flex flex-wrap items-center gap-2">
          <h4 class="text-sm font-medium text-gray-500 dark:text-gray-400">Hand-off</h4>
          <span v-if="pairRow.caller_agent" class="text-sm text-gray-600 dark:text-gray-300">from {{ pairRow.caller_agent }}</span>
          <BaseBadge :variant="VERDICTS[pairVerdict].variant" :data-verdict="pairVerdict" data-testid="execution-checks-pair-headline">
            {{ pairVerdict === 'fail' ? 'Pair checks failed' : pairVerdict === 'pass' ? 'Pair checks passed' : 'No pair check applied' }}
          </BaseBadge>
        </div>
        <ul v-if="pairFailed.length" class="mt-2 space-y-1 text-sm" data-testid="execution-checks-pair-failed">
          <li v-for="c in pairFailed" :key="c.id" class="text-gray-900 dark:text-gray-100">
            <span class="font-mono text-xs">{{ c.id }}</span>
            <span v-if="c.reason" class="ml-2">{{ words(c.reason) }}</span>
            <span v-if="evidenceLine(c)" class="ml-2 font-mono text-xs text-gray-500 dark:text-gray-400">{{ evidenceLine(c) }}</span>
          </li>
        </ul>
        <ul v-if="pairGates.length" class="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs" data-testid="execution-checks-pair-gates">
          <li v-for="c in pairGates" :key="c.id" :data-status="c.status" class="flex items-center gap-1">
            <span :class="OUTCOME[c.status]?.tone || OUTCOME.skip.tone" aria-hidden="true">{{ OUTCOME[c.status]?.mark || OUTCOME.skip.mark }}</span>
            <span class="font-mono text-gray-700 dark:text-gray-300">{{ c.id }}</span>
            <span class="text-gray-500 dark:text-gray-400">{{ OUTCOME[c.status]?.word || c.status }}</span>
          </li>
        </ul>
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

const OUTCOME = {
  pass: { mark: '✓', word: 'passed', tone: 'text-status-success-700 dark:text-status-success-300' },
  fail: { mark: '✕', word: 'failed', tone: 'text-status-danger-700 dark:text-status-danger-300' },
  skip: { mark: '–', word: 'skipped', tone: 'text-gray-400 dark:text-gray-500' },
}

const DIMENSIONS = {
  scope_fidelity: 'Scope fidelity',
  escalation_correctness: 'Escalation correctness',
  intent_alignment: 'Intent alignment',
  helpfulness: 'Helpfulness',
}

const rows = ref(null)              // { tier0, judge, pair } or null (not evaluated)
const hasLoaded = ref(false)
const error = ref(null)

const state = computed(() => {
  const { state: s } = viewState({ hasLoaded: hasLoaded.value, error: error.value })
  return s
})

const evaluation = computed(() => rows.value?.tier0 || null)
const judgeRow = computed(() => rows.value?.judge || null)
const pairRow = computed(() => rows.value?.pair || null)

const checksOf = (row) => {
  const list = row?.checks?.checks
  return Array.isArray(list) ? list : []
}
const entries = computed(() => checksOf(evaluation.value))
const gates = computed(() => entries.value.filter((c) => c.tier === 'gate'))
const signals = computed(() => entries.value.filter((c) => c.tier === 'signal'))
const failedGates = computed(() => gates.value.filter((c) => c.status === 'fail'))

// The stored headline is the server's gate-only count (tier0_evaluator.headline);
// a row without one is counted the same way from its gate entries.
function countsOf(row, gateList) {
  const h = row?.checks?.headline
  if (h && typeof h === 'object') {
    return { pass: h.gate_pass || 0, fail: h.gate_fail || 0, skip: h.gate_skip || 0 }
  }
  const n = (s) => gateList.filter((c) => c.status === s).length
  return { pass: n('pass'), fail: n('fail'), skip: n('skip') }
}
const counts = computed(() => countsOf(evaluation.value, gates.value))

function verdictOf(row, c) {
  if (!row) return 'none'
  if (c.fail > 0) return 'fail'
  if (c.pass > 0) return 'pass'
  return 'skip'
}
const verdict = computed(() => verdictOf(evaluation.value, counts.value))

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

// Judge (ent#936). An operator reads the document; the dimension order is the
// rubric's, not the document's, so two runs read alike.
const judgeDoc = computed(() => {
  const j = judgeRow.value?.judge
  return j && typeof j === 'object' ? j : null
})
const qualityText = computed(() => {
  const q = judgeRow.value?.quality ?? judgeDoc.value?.quality
  return typeof q === 'number' ? `${(q * 5).toFixed(1)} / 5` : 'no score'
})
const judgeDims = computed(() => {
  const dims = judgeDoc.value?.dimensions
  if (!dims || typeof dims !== 'object') return []
  return Object.keys(DIMENSIONS)
    .filter((id) => dims[id] && typeof dims[id] === 'object')
    .map((id) => ({
      id, label: DIMENSIONS[id],
      verdict: ['pass', 'fail', 'n/a'].includes(dims[id].verdict) ? dims[id].verdict : 'n/a',
      reason: typeof dims[id].reason === 'string' ? dims[id].reason : null,
    }))
})

// Hand-off (ent#940): the pair row's gates, counted the same way as Tier-0's.
const pairEntries = computed(() => checksOf(pairRow.value))
const pairGates = computed(() => pairEntries.value.filter((c) => c.tier === 'gate'))
const pairFailed = computed(() => pairGates.value.filter((c) => c.status === 'fail'))
const pairVerdict = computed(() => verdictOf(pairRow.value, countsOf(pairRow.value, pairGates.value)))

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
      `/api/agents/${props.agentName}/executions/${props.executionId}/evaluations`
    )
    rows.value = data
    hasLoaded.value = true
  } catch (err) {
    if (err.response?.status === 404) {
      rows.value = null
      hasLoaded.value = true
    } else {
      error.value = err
    }
  }
}

onMounted(load)
</script>
