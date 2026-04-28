<template>
  <div class="flex items-center" :class="containerClass">
    <span
      v-if="showLabel"
      class="font-medium whitespace-nowrap min-w-[3rem] text-right"
      :class="[
        labelSizeClass,
        modelValue ? 'text-state-autonomous-600 dark:text-state-autonomous-400' : 'text-gray-500 dark:text-gray-400'
      ]"
    >
      {{ modelValue ? 'AUTO' : 'Manual' }}
    </span>
    <button
      @click="toggle"
      :disabled="disabled || loading"
      role="switch"
      :aria-checked="modelValue"
      :aria-label="`Autonomy mode is ${modelValue ? 'enabled' : 'disabled'}. Click to ${modelValue ? 'disable' : 'enable'} scheduled tasks.`"
      :title="modelValue ? 'Autonomy Mode ON - Click to disable scheduled tasks' : 'Autonomy Mode OFF - Click to enable scheduled tasks'"
      class="relative inline-flex items-center rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-offset-2 dark:focus:ring-offset-gray-800"
      :class="[
        sizeClasses,
        modelValue ? 'bg-state-autonomous-500 focus:ring-state-autonomous-500' : 'bg-gray-300 dark:bg-gray-600 focus:ring-gray-400',
        (disabled || loading) ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'
      ]"
    >
      <span class="sr-only">Toggle autonomy mode</span>
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
 * AutonomyToggle - Unified toggle control for agent autonomy mode
 *
 * Usage:
 *   <AutonomyToggle
 *     v-model="autonomyEnabled"
 *     :loading="isToggling"
 *     @toggle="handleToggle"
 *   />
 *
 * Props:
 *   - modelValue: boolean (true = AUTO, false = Manual)
 *   - disabled: boolean
 *   - loading: boolean (shows spinner)
 *   - showLabel: boolean (default true)
 *   - size: 'sm' | 'md' | 'lg' (default 'sm')
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
