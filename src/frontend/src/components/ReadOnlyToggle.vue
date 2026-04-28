<template>
  <div class="flex items-center" :class="containerClass">
    <span
      v-if="showLabel"
      class="font-medium whitespace-nowrap min-w-[4.25rem] text-right"
      :class="[
        labelSizeClass,
        modelValue ? 'text-state-locked-600 dark:text-state-locked-400' : 'text-gray-500 dark:text-gray-400'
      ]"
    >
      {{ modelValue ? 'Read-Only' : 'Editable' }}
    </span>
    <button
      @click="toggle"
      :disabled="disabled || loading"
      role="switch"
      :aria-checked="modelValue"
      :aria-label="`Read-only mode is ${modelValue ? 'enabled' : 'disabled'}. Click to ${modelValue ? 'allow' : 'prevent'} code modifications.`"
      :title="modelValue ? 'Read-Only Mode ON - Agent cannot modify source code' : 'Read-Only Mode OFF - Agent can modify all files'"
      class="relative inline-flex items-center rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-offset-2 dark:focus:ring-offset-gray-800"
      :class="[
        sizeClasses,
        modelValue ? 'bg-state-locked-500 focus:ring-state-locked-500' : 'bg-gray-300 dark:bg-gray-600 focus:ring-gray-400',
        (disabled || loading) ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'
      ]"
    >
      <span class="sr-only">Toggle read-only mode</span>
      <span
        class="inline-block transform rounded-full bg-white shadow transition-transform"
        :class="[
          knobSizeClasses,
          modelValue ? translateOnClass : 'translate-x-0.5'
        ]"
      />
      <!-- Lock icon when enabled -->
      <span
        v-if="modelValue && !loading"
        class="absolute inset-0 flex items-center"
        :class="modelValue ? 'justify-end pr-1' : 'justify-start pl-1'"
      >
        <svg
          class="text-white"
          :class="iconSizeClass"
          fill="currentColor"
          viewBox="0 0 20 20"
        >
          <path fill-rule="evenodd" d="M5 9V7a5 5 0 0110 0v2a2 2 0 012 2v5a2 2 0 01-2 2H5a2 2 0 01-2-2v-5a2 2 0 012-2zm8-2v2H7V7a3 3 0 016 0z" clip-rule="evenodd" />
        </svg>
      </span>
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
 * ReadOnlyToggle - Toggle control for agent read-only mode
 *
 * Usage:
 *   <ReadOnlyToggle
 *     v-model="readOnlyEnabled"
 *     :loading="isToggling"
 *     @toggle="handleToggle"
 *   />
 *
 * Props:
 *   - modelValue: boolean (true = Read-Only, false = Editable)
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

const iconSizeClass = computed(() => ({
  sm: 'h-2.5 w-2.5',
  md: 'h-3 w-3',
  lg: 'h-3.5 w-3.5'
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
