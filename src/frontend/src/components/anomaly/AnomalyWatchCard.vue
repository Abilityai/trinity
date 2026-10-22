<template>
  <!--
    Schedules tab › Anomaly detection (trinity-enterprise#74): this agent's
    opt-in and what happens when a schedule keeps running off its baseline.
    Admin + entitled install only (SchedulesPanel mounts it; the API guards).
  -->
  <BaseCard data-testid="anomaly-watch-card">
    <div class="flex flex-wrap items-start gap-4">
      <div class="flex-1 min-w-[16rem] flex flex-col gap-1">
        <span class="text-sm font-medium text-gray-900 dark:text-white">Anomaly detection</span>
        <span class="text-[12.5px] text-gray-600 dark:text-gray-300">
          Compares each scheduled run with that schedule's own last {{ lookback }} days. When a run takes much
          longer, costs more, uses more context or tools, or starts failing, you get one alert in Needs Response.
        </span>
      </div>
      <div class="min-h-[44px] flex items-center">
        <BaseToggle
          :model-value="store.watching"
          label="Watching"
          aria-label="Watching this agent's schedules"
          :disabled="!store.hasLoaded || store.saving"
          data-testid="anomaly-watching"
          @update:modelValue="(v) => store.save({ enabled: v })"
        />
      </div>
    </div>

    <div v-if="store.hasLoaded && store.watching" class="mt-3 pt-3 border-t border-gray-200 dark:border-gray-750 flex flex-wrap items-center gap-x-4 gap-y-2">
      <label for="anomaly-remediation" class="text-[12.5px] text-gray-600 dark:text-gray-300">When a schedule keeps running off its baseline</label>
      <BaseSelect
        id="anomaly-remediation"
        size="sm"
        :model-value="store.effective?.remediation || 'none'"
        :disabled="store.saving"
        data-testid="anomaly-remediation"
        @update:modelValue="(v) => store.save({ remediation: v })"
      >
        <option value="none">Alert only</option>
        <option value="pause_schedule">Alert, then pause the schedule after {{ store.effective?.remediate_after || 2 }} anomalous runs</option>
      </BaseSelect>
    </div>

    <InlineError class="mt-3" :message="store.saveError || ''" @dismiss="store.saveError = null" />
    <LoadFailed
      v-if="!store.hasLoaded && store.error"
      class="mt-3"
      dense
      title="Couldn't load anomaly detection"
      message="The schedules still work; their baseline state is not shown."
      :detail="store.error"
      :retrying="store.loading"
      @retry="store.load(agentName)"
    />
  </BaseCard>
</template>

<script setup>
import { computed } from 'vue'
import BaseCard from '../base/BaseCard.vue'
import BaseSelect from '../base/BaseSelect.vue'
import BaseToggle from '../base/BaseToggle.vue'
import InlineError from '../InlineError.vue'
import LoadFailed from '../LoadFailed.vue'
import { useAnomalyDetectionStore } from '../../stores/anomalyDetection'

defineProps({
  agentName: { type: String, required: true },
})

const store = useAnomalyDetectionStore()
const lookback = computed(() => store.effective?.lookback_days || 14)
</script>
