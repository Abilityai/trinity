<template>
  <!--
    Operations › Evaluations (trinity-enterprise#938). The toolbar and the
    honest-state shell; the KPI row, per-agent table, failed list, pairs stub
    and trend land in their own components under components/evaluations/
    (C2–C7). Data comes from stores/evaluations.js, which reads only the
    ent#937 routes — the audience guard is the API (test_938_tab_guard.py),
    mirrored by Operations.vue listing this tab only at the operator floor.
  -->
  <div class="max-w-7xl mx-auto" data-testid="evaluations-panel">
    <!-- Toolbar: a fixed-height row (principle 30) — period, custom bounds,
         agent filter, refresh. -->
    <div class="flex flex-wrap items-center gap-2 mb-6 min-h-[44px]">
      <BaseSelect
        variant="ghost"
        aria-label="Period"
        :model-value="store.period"
        data-testid="evaluations-period"
        @update:modelValue="store.setPeriod"
      >
        <option v-for="p in PERIODS" :key="p.id" :value="p.id">{{ p.label }}</option>
      </BaseSelect>

      <template v-if="store.period === 'custom'">
        <BaseInput
          v-model="customSince"
          type="date"
          aria-label="From"
          data-testid="evaluations-since"
          @change="applyCustomRange"
        />
        <span class="text-xs text-gray-500 dark:text-gray-400">to</span>
        <BaseInput
          v-model="customUntil"
          type="date"
          aria-label="To"
          data-testid="evaluations-until"
          @change="applyCustomRange"
        />
      </template>

      <BaseBadge v-if="store.agent" variant="neutral" data-testid="evaluations-agent-chip">
        {{ store.agent }}
        <button
          type="button"
          class="ml-1 text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
          aria-label="Clear agent filter"
          data-testid="evaluations-agent-clear"
          @click="store.setAgent('')"
        >×</button>
      </BaseBadge>

      <div class="ml-auto">
        <BaseButton variant="ghost" size="sm" :loading="store.loading" data-testid="evaluations-refresh" @click="store.load()">
          Refresh
        </BaseButton>
      </div>
    </div>

    <!-- A failed verb or a failed REFRESH with data on screen: the API's own
         message (422 invalid_period names the window problem), data kept. A
         failed FIRST load is LoadFailed below, never this banner. -->
    <InlineError
      v-if="store.error && view.state !== 'failed'"
      class="mb-4"
      :message="store.invalidPeriod ? store.error : staleBannerMessage('evaluations', store.lastLoadedAt)"
      :detail="store.invalidPeriod ? '' : store.error"
      retryable
      @retry="store.load()"
      @dismiss="store.clearError()"
    />

    <!-- Body: loading / failed / empty / ready share one footprint. -->
    <div class="min-h-[12rem]">
      <div v-if="view.state === 'loading'" aria-busy="true" data-testid="evaluations-loading">
        <SkeletonLoader variant="rows" :count="4" height="2rem" />
        <span class="sr-only">Loading evaluations…</span>
      </div>

      <LoadFailed
        v-else-if="view.state === 'failed'"
        title="Couldn't load evaluations"
        message="The evaluation read failed. Check your connection and try again."
        :detail="store.error"
        :retrying="store.loading"
        data-testid="evaluations-failed"
        @retry="store.load()"
      />

      <div v-else-if="view.state === 'empty'" class="text-center py-16" data-testid="evaluations-empty">
        <h3 class="text-lg font-medium text-gray-900 dark:text-white">No runs evaluated in this period</h3>
        <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
          Only scheduled and agent-triggered runs are graded, about a minute after they finish.
        </p>
        <p class="mt-3 text-sm">
          <router-link
            :to="{ path: '/operations', query: { tab: 'executions' } }"
            class="text-action-primary-600 dark:text-action-primary-400 hover:underline"
            data-testid="evaluations-empty-executions-link"
          >See recent runs on the Executions tab</router-link>
        </p>
      </div>

      <div v-else data-testid="evaluations-summary" class="text-sm text-gray-600 dark:text-gray-300 tabular-nums">
        {{ store.evaluatedCount }} {{ store.evaluatedCount === 1 ? 'run' : 'runs' }} evaluated in this period
        <span v-if="store.aggregate?.truncated" class="ml-2 text-xs text-gray-500 dark:text-gray-400" data-testid="evaluations-truncated">
          · window capped at 5,000 runs — narrow the period
        </span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import BaseSelect from './base/BaseSelect.vue'
import BaseInput from './base/BaseInput.vue'
import BaseButton from './base/BaseButton.vue'
import BaseBadge from './base/BaseBadge.vue'
import InlineError from './InlineError.vue'
import LoadFailed from './LoadFailed.vue'
import SkeletonLoader from './SkeletonLoader.vue'
import { useEvaluationsStore, PERIODS } from '../stores/evaluations'
import { viewState, staleBannerMessage } from '../utils/loadingState'

const store = useEvaluationsStore()

const customSince = ref(store.since)
const customUntil = ref(store.until)

function applyCustomRange() {
  store.setCustomRange(customSince.value, customUntil.value)
}

// Loading means "no data yet"; a refresh with data on screen is ready + stale.
const view = computed(() => viewState({
  loading: store.loading,
  hasLoaded: store.hasLoaded,
  error: store.error,
  count: store.evaluatedCount,
}))

onMounted(() => {
  if (!store.hasLoaded) store.load()
})
</script>
