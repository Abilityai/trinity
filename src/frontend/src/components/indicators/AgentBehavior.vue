<template>
  <!--
    Agent Overview › Behavior (trinity-enterprise#939). Six indicators, each its
    own measure with the sample it rests on — there is no blended score. A thin
    sample says "not enough runs", never 0%. A click opens the runs behind the
    indicator, grouped by where they are fixed (BehaviorDrilldown). Read-only:
    nothing here writes an evaluation.
  -->
  <BaseCard flush data-testid="agent-behavior">
    <BehaviorDrilldown
      v-if="store.drill"
      :agent-name="agentName"
      @back="store.closeDrill()"
      @navigate-tab="(t) => emit('navigate-tab', t)"
      @open-task="(id) => emit('open-task', id)"
    />

    <template v-else>
      <div class="flex flex-wrap items-center gap-3 px-4 pt-4">
        <h3 class="text-[11px] font-semibold uppercase tracking-wider text-gray-900 dark:text-white">Behavior</h3>
        <span class="text-[12.5px] text-gray-500 dark:text-gray-400 tabular-nums" data-testid="behavior-meta">{{ metaLine }}</span>
        <div class="ml-auto flex items-center gap-2">
          <label for="behavior-window" class="text-[12.5px] text-gray-600 dark:text-gray-300">Window</label>
          <BaseSelect
            id="behavior-window"
            size="sm"
            :model-value="String(store.days)"
            data-testid="behavior-window"
            @update:modelValue="(v) => store.setDays(Number(v))"
          >
            <option v-for="d in WINDOW_DAYS" :key="d" :value="String(d)">Last {{ d }} days</option>
          </BaseSelect>
        </div>
      </div>

      <InlineError
        v-if="view.stale"
        class="mx-4 mt-3"
        :message="staleBannerMessage('behavior', store.lastLoadedAt)"
        :detail="store.error"
        @dismiss="store.error = null"
      />

      <div class="p-4">
        <div v-if="view.state === 'loading'" aria-busy="true">
          <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            <div v-for="i in 6" :key="i" class="h-[118px] rounded-lg bg-gray-100 dark:bg-gray-750 animate-pulse motion-reduce:animate-none" />
          </div>
          <span class="sr-only">Loading behavior indicators…</span>
        </div>

        <LoadFailed
          v-else-if="view.state === 'failed'"
          title="Couldn't load behavior"
          message="The indicator read failed. Check your connection and try again."
          :detail="store.error"
          :retrying="store.loading"
          @retry="store.load(agentName)"
        />

        <template v-else>
          <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3" data-testid="behavior-tiles">
            <component
              :is="clickable(ind) ? 'button' : 'div'"
              v-for="ind in indicators"
              :key="ind.id"
              :type="clickable(ind) ? 'button' : undefined"
              :data-testid="`behavior-tile-${ind.id}`"
              :class="tileClass(ind)"
              @click="clickable(ind) && store.openDrill(ind.id)"
            >
              <span class="text-[12.5px] text-gray-600 dark:text-gray-300">
                {{ INDICATOR_LABELS[ind.id] }}
                <span v-if="INDICATOR_HINTS[ind.id]" class="text-gray-500 dark:text-gray-400">· {{ INDICATOR_HINTS[ind.id] }}</span>
              </span>
              <span
                :class="['text-2xl font-bold tabular-nums', indicatorValue(ind) ? 'text-gray-900 dark:text-white' : 'text-gray-500 dark:text-gray-400']"
                data-testid="behavior-tile-value"
              >{{ indicatorValue(ind) || '—' }}</span>
              <span class="text-[12.5px] text-gray-500 dark:text-gray-400" data-testid="behavior-tile-sample">
                {{ ind.state === 'insufficient' ? insufficientText(ind, store.data.min_runs) : indicatorSample(ind, store.days) }}
              </span>
              <span v-if="ind.state === 'ok'" :class="['text-[12.5px]', footTone(ind)]">
                {{ indicatorFoot(ind, store.days) }}<template v-if="clickable(ind) && watch(ind)"> · see failing runs →</template>
              </span>
            </component>
          </div>
          <p class="mt-3 text-[12.5px] text-gray-500 dark:text-gray-400">
            Each indicator is its own measure; there is no blended score. Click one to see the runs behind it.
          </p>
        </template>
      </div>

      <!-- Failed checks by where they are fixed — the playbook, the schedule,
           or the Evaluations drill-down for chat turns. -->
      <div v-if="view.state === 'ready' && fixPaths.length" class="border-t border-gray-200 dark:border-gray-750" data-testid="behavior-fix-paths">
        <div class="flex items-center gap-3 px-4 py-2.5">
          <h4 class="text-sm font-medium text-gray-900 dark:text-white">Failed checks, by where to fix them</h4>
          <span class="ml-auto text-[12.5px] text-gray-500 dark:text-gray-400 tabular-nums">
            {{ failureTotal }} {{ failureTotal === 1 ? 'failure' : 'failures' }} · {{ sourceCount }} {{ sourceCount === 1 ? 'source' : 'sources' }}
          </span>
        </div>
        <div class="max-h-72 overflow-y-auto">
          <table class="w-full text-sm">
            <thead class="sticky top-0 bg-gray-100 dark:bg-gray-750">
              <tr class="text-[11px] uppercase tracking-wider text-gray-600 dark:text-gray-300">
                <th scope="col" class="text-left font-semibold px-4 py-2">Check</th>
                <th scope="col" class="text-left font-semibold px-4 py-2">Ran from</th>
                <th scope="col" class="text-right font-semibold px-4 py-2">Failed</th>
                <th scope="col" class="text-left font-semibold px-4 py-2">Fix path</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="p in fixPaths"
                :key="`${p.check}:${p.source.kind}:${p.source.ref}`"
                class="border-t border-gray-200 dark:border-gray-750"
                data-testid="behavior-fix-path"
              >
                <td class="px-4 py-2.5 text-gray-900 dark:text-gray-100">
                  {{ checkLabel(p.check) }}<span v-if="p.judge" class="text-[12.5px] text-gray-500 dark:text-gray-400"> (judge)</span>
                </td>
                <td class="px-4 py-2.5 text-gray-600 dark:text-gray-300">{{ sourceText(p.source) }}</td>
                <td class="px-4 py-2.5 text-right tabular-nums text-gray-900 dark:text-gray-100">{{ p.failed }}</td>
                <td class="px-4 py-2.5">
                  <button v-if="p.source.kind === 'playbook'" type="button" class="text-action-primary-600 dark:text-action-primary-400 hover:underline" @click="emit('navigate-tab', 'skills')">Open the skill</button>
                  <button v-else-if="hasFixPath(p.source)" type="button" class="text-action-primary-600 dark:text-action-primary-400 hover:underline" @click="emit('navigate-tab', 'schedules')">Open the schedule</button>
                  <span v-else-if="p.source.kind === 'schedule'" class="text-gray-500 dark:text-gray-400">schedule removed</span>
                  <router-link v-else :to="{ path: '/operations', query: { tab: 'evaluations', agent: agentName } }" class="text-action-primary-600 dark:text-action-primary-400 hover:underline">Operations › Evaluations</router-link>
                  <span class="text-gray-500 dark:text-gray-400"> · </span>
                  <button type="button" class="text-action-primary-600 dark:text-action-primary-400 hover:underline tabular-nums" :data-testid="`behavior-fix-runs-${p.check}`" @click="openFixRuns(p)">{{ p.failed }} {{ p.failed === 1 ? 'run' : 'runs' }}</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>
  </BaseCard>
</template>

<script setup>
import { computed, watch as vueWatch, onMounted } from 'vue'
import BaseCard from '../base/BaseCard.vue'
import BaseSelect from '../base/BaseSelect.vue'
import InlineError from '../InlineError.vue'
import LoadFailed from '../LoadFailed.vue'
import BehaviorDrilldown from './BehaviorDrilldown.vue'
import { useAgentIndicatorsStore, WINDOW_DAYS } from '../../stores/agentIndicators'
import { viewState, staleBannerMessage } from '../../utils/loadingState'
import { parseUTC } from '../../utils/timestamps'
import {
  INDICATOR_LABELS, INDICATOR_HINTS, DRILLABLE, checkLabel, sourceText, hasFixPath,
  indicatorValue, indicatorSample, insufficientText, indicatorFoot, footTone,
} from './format'

const props = defineProps({
  agentName: { type: String, required: true },
})
const emit = defineEmits(['navigate-tab', 'open-task'])

const store = useAgentIndicatorsStore()

// Rate indicators below this read as worth a look (the tile is tinted).
const WATCH_BELOW = 0.85

const view = computed(() => viewState({
  loading: store.loading,
  hasLoaded: store.hasLoaded && store.agent === props.agentName,
  error: store.error,
}))
const indicators = computed(() => store.data?.indicators || [])
const fixPaths = computed(() => store.data?.fix_paths || [])
const failureTotal = computed(() => fixPaths.value.reduce((n, p) => n + p.failed, 0))
const sourceCount = computed(() =>
  new Set(fixPaths.value.map((p) => `${p.source.kind}:${p.source.ref}`)).size)

function ago(iso) {
  const t = parseUTC(iso)
  if (!t) return ''
  const mins = Math.max(0, Math.round((Date.now() - t.getTime()) / 60000))
  if (mins < 60) return `${mins} min ago`
  const hours = Math.round(mins / 60)
  return hours < 48 ? `${hours} h ago` : `${Math.round(hours / 24)} days ago`
}

const metaLine = computed(() => {
  const d = store.data
  if (!d) return `Last ${store.days} days`
  if (!d.runs) return `No graded runs in the last ${d.days} days`
  const latest = d.latest_run_at ? ` · latest run ${ago(d.latest_run_at)}` : ''
  return `Last ${d.days} days · ${d.runs} graded ${d.runs === 1 ? 'run' : 'runs'}${latest}${d.truncated ? ' · newest 5000 counted' : ''}`
})

const clickable = (ind) => DRILLABLE.includes(ind.id) && ind.state !== 'no_runs' && ind.n > 0
const watch = (ind) => ind.id !== 'cost_vs_baseline' && ind.id !== 'quality'
  && typeof ind.value === 'number' && ind.value < WATCH_BELOW

function tileClass(ind) {
  const base = 'flex flex-col gap-1.5 p-3.5 rounded-lg border text-left min-h-[118px]'
  const tone = ind.state !== 'ok'
    ? 'border-dashed border-gray-300 dark:border-gray-700'
    : watch(ind)
      ? 'border-status-warning-200 bg-status-warning-50 dark:border-status-warning-800 dark:bg-status-warning-900/20'
      : 'border-gray-200 dark:border-gray-750'
  const hover = clickable(ind)
    ? 'cursor-pointer hover:border-action-primary-300 dark:hover:border-action-primary-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40'
    : ''
  return [base, tone, hover]
}

function openFixRuns(p) {
  store.openDrill(p.judge ? p.check : 'checks_passed', p.judge ? null : p.check)
}

function ensure() {
  if (store.agent !== props.agentName || !store.hasLoaded) store.load(props.agentName)
}
vueWatch(() => props.agentName, ensure)
onMounted(ensure)
</script>
