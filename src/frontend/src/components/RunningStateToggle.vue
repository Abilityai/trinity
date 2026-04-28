<template>
  <div class="flex items-center" :class="containerClass">
    <span
      v-if="showLabel"
      class="font-medium whitespace-nowrap min-w-[3.25rem] text-right"
      :class="[
        labelSizeClass,
        modelValue ? 'text-status-success-600 dark:text-status-success-400' : 'text-gray-500 dark:text-gray-400'
      ]"
    >
      {{ modelValue ? 'Running' : 'Stopped' }}
    </span>
    <button
      @click="toggle"
      :disabled="disabled || loading"
      role="switch"
      :aria-checked="modelValue"
      :aria-label="`Agent is ${modelValue ? 'running' : 'stopped'}. Click to ${modelValue ? 'stop' : 'start'}.`"
      class="relative inline-flex items-center rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-offset-2 dark:focus:ring-offset-gray-800"
      :class="[
        sizeClasses,
        modelValue ? 'bg-status-success-500 focus:ring-status-success-500' : 'bg-gray-300 dark:bg-gray-600 focus:ring-gray-400',
        (disabled || loading) ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'
      ]"
    >
      <span
        class="inline-block transform rounded-full bg-white shadow transition-transform"
        :class="[
          knobSizeClasses,
          modelValue ? translateOnClass : 'translate-x-0.5'
        ]"
      />
      <!-- Loading spinner overlay -->
      <span
        v-if="loading"
        class="absolute inset-0 flex items-center justify-center"
      >
        <svg
          class="animate-spin text-white"
          :class="spinnerSizeClass"
          xmlns="http://www.w3.org/2000/svg"
          fill="none"
          viewBox="0 0 24 24"
        >
          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
          <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
        </svg>
      </span>
    </button>
  </div>
</template>

<script setup>
/**
 * RunningStateToggle - Unified toggle control for agent start/stop
 *
 * Usage:
 *   <RunningStateToggle
 *     v-model="isRunning"
 *     :loading="isToggling"
 *     @toggle="handleToggle"
 *   />
 *
 * Props:
 *   - modelValue: boolean (true = running, false = stopped)
 *   - disabled: boolean
 *   - loading: boolean (shows spinner)
 *   - showLabel: boolean (default true)
 *   - size: 'sm' | 'md' | 'lg' (default 'md')
 *
 * Events:
 *   - update:modelValue: emitted when toggled
 *   - toggle: emitted with new state value
 */

import { computed } from 'vue'

const props = defineProps({
  modelValue: {
    type: Boolean,
    required: true
  },
  disabled: {
    type: Boolean,
    default: false
  },
  loading: {
    type: Boolean,
    default: false
  },
  showLabel: {
    type: Boolean,
    default: true
  },
  size: {
    type: String,
    default: 'sm',
    validator: (value) => ['sm', 'md', 'lg'].includes(value)
  }
})

const emit = defineEmits(['update:modelValue', 'toggle'])

// Size variants
const sizeClasses = computed(() => ({
  sm: 'h-5 w-9',
  md: 'h-6 w-11',
  lg: 'h-7 w-14'
}[props.size]))

const knobSizeClasses = computed(() => ({
  sm: 'h-4 w-4',
  md: 'h-5 w-5',
  lg: 'h-6 w-6'
}[props.size]))

const translateOnClass = computed(() => ({
  sm: 'translate-x-4',
  md: 'translate-x-5',
  lg: 'translate-x-7'
}[props.size]))

const labelSizeClass = computed(() => ({
  sm: 'text-xs mr-1.5',
  md: 'text-xs mr-2',
  lg: 'text-sm mr-3'
}[props.size]))

const spinnerSizeClass = computed(() => ({
  sm: 'h-3 w-3',
  md: 'h-4 w-4',
  lg: 'h-5 w-5'
}[props.size]))

const containerClass = computed(() => ({
  sm: 'space-x-1.5',
  md: 'space-x-2',
  lg: 'space-x-3'
}[props.size]))

function toggle() {
  if (!props.disabled && !props.loading) {
    const newValue = !props.modelValue
    emit('update:modelValue', newValue)
    emit('toggle', newValue)
  }
}
</script>
