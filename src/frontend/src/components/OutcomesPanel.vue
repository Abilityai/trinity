<template>
  <!--
    Operations › Outcomes (trinity-enterprise#941): the weekly project-execution
    meta-analysis — fleet signal tiles, the per-project table — and the gated
    improvement proposals. Data from stores/outcomes.js, which reads only the
    eval_outcomes routes (admin + person + entitlement at the API).
  -->
  <div class="max-w-7xl mx-auto" data-testid="outcomes-panel">
    <div class="flex items-center gap-2 mb-6 min-h-[44px]">
      <p class="text-sm text-gray-600 dark:text-gray-300" data-testid="outcomes-window">
        <template v-if="store.fleet">Week of {{ store.fleet.window_start.slice(0, 10) }} · fleet-wide</template>
        <template v-else>Weekly record, computed after each full UTC week</template>
      </p>
      <div class="ml-auto">
        <BaseButton variant="ghost" size="sm" :loading="store.loading" data-testid="outcomes-refresh" @click="store.load()">Refresh</BaseButton>
      </div>
    </div>

    <InlineError
      v-if="store.error && view.state !== 'failed'"
      class="mb-4"
      :message="staleBannerMessage('outcomes', null)"
      :detail="store.error"
      retryable
      @retry="store.load()"
    />

    <div class="min-h-[12rem]">
      <div v-if="view.state === 'loading'" aria-busy="true" data-testid="outcomes-loading">
        <SkeletonLoader variant="rows" :count="4" height="2rem" />
        <span class="sr-only">Loading outcomes…</span>
      </div>

      <LoadFailed
        v-else-if="view.state === 'failed'"
        title="Couldn't load outcomes"
        message="The outcomes read failed. Check your connection and try again."
        :detail="store.error"
        :retrying="store.loading"
        data-testid="outcomes-failed"
        @retry="store.load()"
      />

      <div v-else-if="view.state === 'empty'" class="text-center py-16" data-testid="outcomes-empty">
        <h3 class="text-lg font-medium text-gray-900 dark:text-gray-100">No weekly record yet</h3>
        <p class="mt-1 text-sm text-gray-600 dark:text-gray-300">
          The first record is written after the next full UTC week ends. It reads Workspace Projects and the graded runs on the Evaluations tab.
        </p>
      </div>

      <div v-else class="space-y-6" data-testid="outcomes-ready">
        <div v-if="store.fleet" class="grid grid-cols-2 md:grid-cols-4 gap-4" data-testid="outcomes-kpis">
          <BaseCard v-for="k in kpis" :key="k.id" :data-testid="`outcomes-kpi-${k.id}`">
            <p class="text-xs text-gray-500 dark:text-gray-400">{{ k.label }}</p>
            <p class="mt-1 text-2xl font-bold text-gray-900 dark:text-gray-100 tabular-nums">{{ k.value }}</p>
            <p class="text-xs text-gray-500 dark:text-gray-400 tabular-nums">{{ k.sub }}</p>
          </BaseCard>
        </div>
        <p v-if="unreachable.length" class="text-sm text-status-warning-700 dark:text-status-warning-400" data-testid="outcomes-unreachable">
          Not read this week: {{ unreachable.map((u) => `${u.repo} (${u.reason})`).join(', ') }}.
        </p>
        <OutcomesProjects :rows="store.projects" />
        <OutcomesProposals
          :items="store.visibleProposals"
          :status="store.statusFilter"
          :owner="store.ownerFilter"
          :owners="store.owners"
          :counts="store.counts"
          :acting="store.acting"
          :action-error="store.actionError"
          @status="store.setStatus"
          @owner="store.setOwner"
          @decide="(id, d, r) => store.decide(id, d, r)"
          @ship="store.markShipped"
          @clear-error="store.clearActionError()"
        />
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted } from 'vue'
import BaseButton from './base/BaseButton.vue'
import BaseCard from './base/BaseCard.vue'
import InlineError from './InlineError.vue'
import LoadFailed from './LoadFailed.vue'
import SkeletonLoader from './SkeletonLoader.vue'
import OutcomesProjects from './outcomes/OutcomesProjects.vue'
import OutcomesProposals from './outcomes/OutcomesProposals.vue'
import { useOutcomesStore } from '../stores/outcomes'
import { viewState, staleBannerMessage } from '../utils/loadingState'
import { pct, hours, usd, num } from './outcomes/format'

const store = useOutcomesStore()

const view = computed(() => viewState({
  loading: store.loading,
  hasLoaded: store.hasLoaded,
  error: store.error,
  count: (store.fleet ? 1 : 0) + store.projects.length + store.proposals.length,
}))

const kpis = computed(() => {
  const s = store.fleet?.signals || {}
  return [
    { id: 'completion', label: 'Tasks done / planned', value: pct(s.completion?.rate),
      sub: `${num(s.completion?.done)} of ${num(s.completion?.planned)}` },
    { id: 'claims', label: 'Done claims rejected', value: pct(s.done_claims?.rejection_rate),
      sub: `${num(s.done_claims?.rejected)} of ${num(s.done_claims?.claims_resolved)} · reopened ${num(s.done_claims?.reopened)}` },
    { id: 'asks', label: 'Asks unanswered', value: num(s.asks?.unanswered),
      sub: `${num(s.asks?.no_human_surface)} reach no person` },
    { id: 'latency', label: 'Dispatch → verified', value: hours(s.latency?.dispatch_to_verified_hours_median),
      sub: `cost / task ${usd(s.cost?.per_verified_task)}` },
  ]
})

const unreachable = computed(() => store.fleet?.inputs?.unreachable || [])

onMounted(() => {
  if (!store.hasLoaded) store.load()
})
</script>
