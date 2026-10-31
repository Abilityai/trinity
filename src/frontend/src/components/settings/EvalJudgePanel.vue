<template>
  <!-- Settings › Judge (trinity-enterprise#936): the Layer 2 sampled judge's
       platform policy — on/off, who is judged by default, which models, how
       runs are sampled, and the per-agent daily spend cap. Admin-only and
       entitlement-gated at the API; this tab is hidden when unentitled. -->
  <div class="rounded-lg bg-white p-6 shadow dark:bg-gray-800 dark:shadow-gray-900" data-testid="eval-judge-panel">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 class="text-lg font-medium text-gray-900 dark:text-gray-100">Evaluation judge</h2>
        <p class="mt-1 max-w-2xl text-sm text-gray-600 dark:text-gray-300">
          A second, different model grades a sample of runs from the transcript only: every run that
          failed a platform check, plus a few passing runs per agent per day. Each graded run costs a
          provider call, capped per agent per day.
        </p>
      </div>
      <BaseToggle v-if="form" v-model="form.enabled" label="Enabled" data-testid="eval-judge-enabled" />
    </div>

    <InlineError v-if="store.error && view.state !== 'failed'" class="mt-4" :message="store.error" retryable @retry="store.load()" />

    <div class="mt-4 min-h-[16rem]">
      <div v-if="view.state === 'loading'" aria-busy="true">
        <SkeletonLoader :count="5" />
        <span class="sr-only">Loading the judge policy…</span>
      </div>
      <LoadFailed
        v-else-if="view.state === 'failed'"
        title="Couldn't load the judge policy"
        message="The settings read failed. Check your connection and try again."
        :detail="store.error"
        :retrying="store.loading"
        @retry="store.load()"
      />
      <form v-else-if="form" class="space-y-6" data-testid="eval-judge-form" @submit.prevent="onSave">
        <div class="grid grid-cols-1 gap-4 md:grid-cols-3">
          <BaseSelect v-model="form.agentDefault" label="Agents with no setting of their own" data-testid="eval-judge-default">
            <option value="on">Judged</option>
            <option value="off">Not judged (opt in per agent)</option>
          </BaseSelect>
          <BaseSelect v-model="form.primary" label="Judge model" data-testid="eval-judge-primary">
            <option v-for="m in store.allowlist" :key="m" :value="m">{{ m }}</option>
          </BaseSelect>
          <BaseSelect v-model="form.fallback" label="Fallback for runs in the same family" data-testid="eval-judge-fallback">
            <option value="">None</option>
            <option v-for="m in store.allowlist" :key="m" :value="m">{{ m }}</option>
          </BaseSelect>
        </div>

        <fieldset class="space-y-3">
          <legend class="text-sm font-medium text-gray-900 dark:text-gray-100">Sampling</legend>
          <BaseToggle v-model="form.everyFailure" label="Judge every run that failed a platform check" />
          <div class="grid grid-cols-1 gap-4 md:grid-cols-3">
            <BaseInput v-model.number="form.passingPerDay" type="number" min="0" max="100" label="Passing runs per agent per day" help="0–100" data-testid="eval-judge-passing" />
            <BaseInput v-model.number="form.costMultiple" type="number" min="1" max="1000" step="0.5" label="Skip runs cheaper than judging ×" help="A run must cost this many times a judge call" />
            <div class="flex flex-col gap-2 pt-6">
              <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
                <input v-model="form.floorPass" type="checkbox" class="rounded border-gray-300 dark:border-gray-700" /> Apply the cost floor to passing runs
              </label>
              <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
                <input v-model="form.floorFailure" type="checkbox" class="rounded border-gray-300 dark:border-gray-700" /> Apply it to failed runs too
              </label>
            </div>
          </div>
        </fieldset>

        <div class="grid grid-cols-1 gap-4 md:grid-cols-3">
          <BaseInput v-model.number="form.dailyCap" type="number" min="0" max="100" step="0.05" label="Daily cap per agent (USD)" help="$0–$100. Runs past it are skipped, not queued." data-testid="eval-judge-cap" />
          <BaseInput v-model="form.excludedTriggers" label="Never judge these channels" help="Comma-separated, e.g. public, telegram" />
          <BaseInput v-model="form.excludedArchetypes" label="Never judge these archetypes" help="Comma-separated, e.g. receptionist" />
        </div>

        <InlineError v-if="store.saveError" :message="store.saveError" data-testid="eval-judge-save-error" @dismiss="store.clearSaveError()" />

        <div class="flex items-center gap-3">
          <BaseButton type="submit" variant="primary" :loading="store.saving" data-testid="eval-judge-save">Save policy</BaseButton>
          <span v-if="store.savedAt && !store.saveError" class="text-sm text-gray-600 dark:text-gray-300" data-testid="eval-judge-saved">
            Saved. The next sweep, within a minute, uses it.
          </span>
        </div>
        <p class="text-xs text-gray-500 dark:text-gray-400">
          Per-agent overrides (on, off, a different cap) are set on the agent through the API for now.
        </p>
      </form>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import BaseButton from '../base/BaseButton.vue'
import BaseInput from '../base/BaseInput.vue'
import BaseSelect from '../base/BaseSelect.vue'
import BaseToggle from '../base/BaseToggle.vue'
import InlineError from '../InlineError.vue'
import LoadFailed from '../LoadFailed.vue'
import SkeletonLoader from '../SkeletonLoader.vue'
import { useEvalJudgeStore, toForm } from '../../stores/evalJudge'
import { viewState } from '../../utils/loadingState'

const store = useEvalJudgeStore()
const form = ref(null)

const view = computed(() => viewState({
  loading: store.loading, hasLoaded: store.hasLoaded, error: store.error, count: store.policy ? 1 : 0,
}))

watch(() => store.policy, (p) => { if (p) form.value = toForm(p) }, { immediate: true })

function onSave() {
  store.save(form.value)
}

onMounted(() => { if (!store.hasLoaded) store.load() })
</script>
