<template>
  <!-- One schedule's baseline state (trinity-enterprise#74); nothing when the
       agent is not watched or the view has not loaded. Icon + words, never
       colour alone. -->
  <BaseBadge
    v-if="badge"
    :variant="badge.variant"
    :data-testid="`anomaly-badge-${scheduleId}`"
    :data-state="entry.state"
  >{{ badge.icon }} {{ badge.label }}</BaseBadge>
</template>

<script setup>
import { computed } from 'vue'
import BaseBadge from '../base/BaseBadge.vue'
import { useAnomalyDetectionStore } from '../../stores/anomalyDetection'
import { stateBadge } from './format'

const props = defineProps({
  scheduleId: { type: String, required: true },
})

const store = useAnomalyDetectionStore()
const entry = computed(() => store.bySchedule[props.scheduleId] || null)
const badge = computed(() => stateBadge(entry.value, store.effective?.min_samples || 10))
</script>
