<template>
  <!--
    The runs behind one indicator (trinity-enterprise#939 AC 3), grouped by the
    playbook, schedule or chat they came from — each group names its fix path.
    Read-only: fixing happens in the skill or the schedule.
  -->
  <div data-testid="behavior-drilldown">
    <div class="flex flex-wrap items-center gap-3 px-4 pt-4">
      <button
        type="button"
        class="text-[12.5px] text-action-primary-600 dark:text-action-primary-400 hover:underline"
        data-testid="behavior-drill-back"
        @click="emit('back')"
      >← Behavior</button>
      <span class="ml-auto text-[12.5px] text-gray-500 dark:text-gray-400 tabular-nums">Last {{ store.days }} days</span>
    </div>

    <div class="px-4 pt-2 flex flex-col gap-1">
      <h3 class="text-lg font-semibold text-gray-900 dark:text-white" data-testid="behavior-drill-title">{{ title }}</h3>
      <p class="text-sm text-gray-600 dark:text-gray-300">{{ subtitle }}</p>
    </div>

    <div v-if="drill.indicator === 'checks_passed'" class="px-4 pt-3 flex flex-wrap items-center gap-2 min-h-[44px]">
      <label for="behavior-check" class="text-[12.5px] text-gray-600 dark:text-gray-300">Check</label>
      <BaseSelect
        id="behavior-check"
        size="sm"
        :model-value="drill.check || ''"
        data-testid="behavior-check"
        @update:modelValue="(v) => store.openDrill('checks_passed', v || null)"
      >
        <option value="">All failed checks</option>
        <option v-for="c in checkOptions" :key="c" :value="c">{{ checkLabel(c) }}</option>
      </BaseSelect>
      <span class="ml-auto text-[12.5px] text-gray-500 dark:text-gray-400 tabular-nums">{{ countLine }}</span>
    </div>

    <InlineError
      v-if="dview.stale"
      class="mx-4 mt-3"
      message="Couldn't refresh these runs — showing the last ones that loaded."
      :detail="store.drillError"
      @dismiss="store.drillError = null"
    />

    <div class="p-4 flex flex-col gap-4">
      <div v-if="dview.state === 'loading'" aria-busy="true">
        <SkeletonLoader variant="rows" :count="4" height="2.25rem" />
        <span class="sr-only">Loading runs…</span>
      </div>

      <LoadFailed
        v-else-if="dview.state === 'failed'"
        title="Couldn't load these runs"
        message="The read failed. Check your connection and try again."
        :detail="store.drillError"
        :retrying="store.drillLoading"
        @retry="store.openDrill(drill.indicator, drill.check)"
      />

      <p v-else-if="dview.state === 'empty'" class="text-sm text-gray-600 dark:text-gray-300" data-testid="behavior-drill-empty">
        No run in the last {{ store.days }} days counts against this indicator. Pick another check or a longer window.
      </p>

      <section
        v-for="g in groups"
        v-else
        :key="`${g.source.kind}:${g.source.ref}`"
        class="rounded-lg border border-gray-200 dark:border-gray-750 overflow-hidden"
        data-testid="behavior-drill-group"
      >
        <div class="flex flex-wrap items-center gap-3 px-4 py-2.5 bg-gray-100 dark:bg-gray-750">
          <span class="text-sm font-medium text-gray-900 dark:text-white">{{ sourceText(g.source) }}</span>
          <span class="text-[12.5px] text-gray-600 dark:text-gray-300 tabular-nums">
            {{ g.failed }} of {{ g.runs }} {{ g.runs === 1 ? 'run' : 'runs' }} {{ failedVerb }} ({{ Math.round((g.failed / g.runs) * 100) }}%)
          </span>
          <BaseButton
            v-if="hasFixPath(g.source)"
            class="ml-auto"
            variant="secondary"
            size="sm"
            @click="emit('navigate-tab', g.source.kind === 'playbook' ? 'skills' : 'schedules')"
          >{{ g.source.kind === 'playbook' ? 'Open the skill to fix it' : 'Open the schedule' }}</BaseButton>
        </div>
        <div class="max-h-80 overflow-y-auto">
          <table class="w-full text-sm">
            <thead class="sticky top-0 bg-white dark:bg-gray-800">
              <tr class="text-[11px] uppercase tracking-wider text-gray-600 dark:text-gray-300">
                <th scope="col" class="text-left font-semibold px-4 py-2">Run</th>
                <th scope="col" class="text-left font-semibold px-4 py-2">Finished</th>
                <th scope="col" class="text-left font-semibold px-4 py-2">Why it counts</th>
                <th scope="col" class="text-right font-semibold px-4 py-2">Cost</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in g.items" :key="r.execution_id" class="border-t border-gray-200 dark:border-gray-750" data-testid="behavior-drill-run">
                <td class="px-4 py-2.5">
                  <button type="button" class="font-mono text-action-primary-600 dark:text-action-primary-400 hover:underline" @click="emit('open-task', r.execution_id)">{{ shortId(r.execution_id) }}</button>
                </td>
                <td class="px-4 py-2.5 text-gray-600 dark:text-gray-300 tabular-nums" :title="r.completed_at || ''">{{ when(r.completed_at) }}</td>
                <td class="px-4 py-2.5 text-gray-900 dark:text-gray-100">{{ r.reasons.map(reasonText).join('; ') }}</td>
                <td class="px-4 py-2.5 text-right tabular-nums text-gray-900 dark:text-gray-100">{{ fmtCost(r.cost) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-if="g.failed > g.items.length" class="px-4 py-2 text-[12.5px] text-gray-500 dark:text-gray-400 border-t border-gray-200 dark:border-gray-750">
          Newest {{ g.items.length }} of {{ g.failed }} shown.
        </p>
      </section>

      <p v-if="dview.state === 'ready'" class="text-[12.5px] text-gray-500 dark:text-gray-400">
        Read-only. Fixing happens in the skill or the schedule; this page never changes the agent.
      </p>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import BaseButton from '../base/BaseButton.vue'
import BaseSelect from '../base/BaseSelect.vue'
import InlineError from '../InlineError.vue'
import LoadFailed from '../LoadFailed.vue'
import SkeletonLoader from '../SkeletonLoader.vue'
import { useAgentIndicatorsStore } from '../../stores/agentIndicators'
import { viewState } from '../../utils/loadingState'
import { parseUTC } from '../../utils/timestamps'
import { INDICATOR_LABELS, checkLabel, reasonText, sourceText, fmtCost, indicatorValue, hasFixPath } from './format'

defineProps({
  agentName: { type: String, required: true },
})
const emit = defineEmits(['back', 'navigate-tab', 'open-task'])

const store = useAgentIndicatorsStore()
const drill = computed(() => store.drill || {})
const groups = computed(() => store.drillData?.groups || [])
const failedTotal = computed(() => groups.value.reduce((n, g) => n + g.failed, 0))

const dview = computed(() => viewState({
  loading: store.drillLoading,
  hasLoaded: store.drillLoaded,
  error: store.drillError,
  count: groups.value.length,
}))

const indicator = computed(() =>
  (store.data?.indicators || []).find((i) => i.id === drill.value.indicator) || null)

const title = computed(() => {
  const label = INDICATOR_LABELS[drill.value.indicator] || drill.value.indicator
  const v = indicatorValue(indicator.value)
  return v ? `${label}: ${v}` : label
})

const VERBS = {
  completion: 'did not finish',
  checks_passed: 'failed this check',
  scope_fidelity: 'went beyond the ask',
  escalation_correctness: 'should have asked',
  quality: 'were rated low',
}
const failedVerb = computed(() => VERBS[drill.value.indicator] || 'failed')

const subtitle = computed(() => {
  if (!store.drillLoaded) return 'Grouped by the playbook that ran.'
  const runs = store.data?.runs
  const of = typeof runs === 'number' ? ` of ${runs}` : ''
  return `${failedTotal.value}${of} runs in the last ${store.days} days ${failedVerb.value}. Grouped by the playbook that ran.`
})

// The checks this agent actually failed in the window, from the fix paths.
const checkOptions = computed(() => [...new Set(
  (store.data?.fix_paths || []).filter((p) => !p.judge).map((p) => p.check))])

const countLine = computed(() => store.drillLoaded
  ? `${failedTotal.value} ${failedTotal.value === 1 ? 'run' : 'runs'} · ${groups.value.length} ${groups.value.length === 1 ? 'source' : 'sources'}`
  : '')

const shortId = (id) => (id && id.length > 10 ? `${id.slice(0, 4)}…${id.slice(-2)}` : id)

function when(iso) {
  const t = parseUTC(iso)
  return t ? t.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—'
}
</script>
