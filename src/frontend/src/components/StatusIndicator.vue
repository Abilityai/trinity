<template>
  <span
    :class="[
      'inline-block w-2 h-2 rounded-full',
      statusClasses,
      animated ? 'animate-pulse-status' : ''
    ]"
    :title="statusTitle"
  ></span>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  status: {
    type: String,
    default: 'pending',
    validator: (value) => ['pending', 'in_progress', 'completed', 'error'].includes(value)
  }
})

const statusClasses = computed(() => {
  switch (props.status) {
    case 'in_progress':
      return 'bg-status-info-500'
    case 'completed':
      return 'bg-status-success-500'
    case 'error':
      return 'bg-status-danger-500'
    case 'pending':
    default:
      return 'bg-gray-300 border border-gray-400'
  }
})

const animated = computed(() => props.status === 'in_progress')

const statusTitle = computed(() => {
  switch (props.status) {
    case 'in_progress':
      return 'In progress'
    case 'completed':
      return 'Completed'
    case 'error':
      return 'Error'
    case 'pending':
    default:
      return 'Pending'
  }
})
</script>

<style scoped>
@keyframes pulse-status {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

.animate-pulse-status {
  animation: pulse-status 1.5s ease-in-out infinite;
}
</style>
