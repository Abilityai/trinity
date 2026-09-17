<template>
  <!--
    ent#12 → ent#190: the fleet-benchmark card. Renders exactly what the backend
    answers — `benchmarkFormat.js` makes every decision, so this template has no
    logic of its own — and keeps ONE footprint across no-data-yet / failed /
    message / floor / ready (design-system principle 4). The tint follows the
    EFFECTIVE `sharing_enabled` the backend reports.

    Not gated on a fetch-in-flight flag: `viewState` reduces {loaded, error} to
    the rendered state (p13/p14), and a failed fetch renders `LoadFailed`, never
    a silently missing card (p15).
  -->
  <section
    class="rounded-md border px-4 py-3 text-sm"
    :class="tone === 'sharing'
      ? 'border-action-primary-200 dark:border-action-primary-800 bg-action-primary-50 dark:bg-action-primary-900/20'
      : 'border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900/40'"
    data-testid="fleet-benchmark-card"
    aria-live="polite"
  >
    <p class="font-medium text-gray-900 dark:text-gray-100">Fleet benchmarks</p>

    <SkeletonLoader
      v-if="view.state === 'loading'"
      class="mt-2"
      :count="2"
      height="0.75rem"
      gap="0.375rem"
    />

    <LoadFailed
      v-else-if="view.state === 'failed'"
      dense
      title="Couldn't load fleet benchmarks"
      message="The benchmark request to this instance failed. The activation funnel is unaffected."
      :detail="error"
      :retrying="retrying"
      @retry="$emit('retry')"
    />

    <template v-else>
      <p v-if="branch === 'none'" class="mt-0.5 text-xs text-gray-600 dark:text-gray-400">
        No benchmark information was returned. Reload the page to ask again.
      </p>

      <template v-else>
        <p class="mt-0.5 text-xs text-gray-600 dark:text-gray-400">{{ benchmark.message }}</p>
        <p v-if="detail" class="mt-1 text-xs font-mono text-gray-500 dark:text-gray-400">{{ detail }}</p>

        <template v-if="branch === 'ready'">
          <div class="mt-3 overflow-x-auto">
            <table class="w-full text-xs">
              <thead>
                <tr class="text-left text-[11px] font-mono uppercase tracking-wide text-gray-500 dark:text-gray-400">
                  <th class="py-1 pr-3 font-medium">Metric</th>
                  <th class="py-1 pl-3 text-right font-medium">You</th>
                  <th class="py-1 pl-3 text-right font-medium">Fleet median</th>
                  <th class="py-1 pl-3 text-right font-medium">Your percentile</th>
                  <th class="py-1 pl-3 text-right font-medium">n</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-gray-200 dark:divide-gray-750">
                <tr v-for="row in rows" :key="row.key">
                  <td class="py-1.5 pr-3 text-gray-700 dark:text-gray-300">{{ row.label }}</td>
                  <td class="py-1.5 pl-3 text-right tabular-nums font-medium text-gray-900 dark:text-gray-100">{{ row.you }}</td>
                  <td class="py-1.5 pl-3 text-right tabular-nums text-gray-700 dark:text-gray-300">{{ row.fleetMedian }}</td>
                  <td class="py-1.5 pl-3 text-right tabular-nums text-gray-700 dark:text-gray-300">{{ row.percentile }}</td>
                  <td class="py-1.5 pl-3 text-right tabular-nums text-gray-500 dark:text-gray-400">{{ row.n }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-if="participants" class="mt-2 text-xs text-gray-500 dark:text-gray-400">{{ participants }}</p>
          <p
            v-if="basedOn"
            class="mt-0.5 text-xs text-gray-500 dark:text-gray-400"
            :title="basedOnTitle"
          >
            {{ basedOn }}
          </p>
        </template>
      </template>
    </template>
  </section>
</template>

<script setup>
import { computed } from 'vue'
import LoadFailed from '../LoadFailed.vue'
import SkeletonLoader from '../SkeletonLoader.vue'
import { formatRelativeTime } from '../../utils/timestamps'
import { viewState } from '../../utils/loadingState'
import {
  benchmarkBranch,
  benchmarkTone,
  metricRows,
  participantsLine,
  basedOnLine,
  reasonDetail,
} from './benchmarkFormat'

const props = defineProps({
  // The FleetBenchmark document the backend answered, or null before/without one.
  benchmark: { type: Object, default: null },
  // A fetch SUCCEEDED at least once ("no data yet" is the only loading state).
  loaded: { type: Boolean, default: false },
  // The last fetch failure, in user vocabulary; empty when the fetch succeeded.
  error: { type: String, default: '' },
  // True while a retry is in flight, so LoadFailed cannot double-fire.
  retrying: { type: Boolean, default: false },
})

defineEmits(['retry'])

const view = computed(() => viewState({ hasLoaded: props.loaded, error: props.error || null, count: 1 }))
const branch = computed(() => benchmarkBranch(props.benchmark))
const tone = computed(() => benchmarkTone(props.benchmark))
const rows = computed(() => metricRows(props.benchmark?.metrics))
const participants = computed(() => participantsLine(props.benchmark))
const basedOn = computed(() => basedOnLine(props.benchmark?.based_on, formatRelativeTime))
const basedOnTitle = computed(() => props.benchmark?.based_on?.shared_at || '')
const detail = computed(() => reasonDetail(props.benchmark))
</script>
