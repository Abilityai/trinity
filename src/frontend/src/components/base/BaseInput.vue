<template>
  <!--
    The input primitive (docs/memory/design-system.md §5, #2122).
    Anatomy: label above → control → help below. `label`/`help` are optional so
    the control can slot into composed layouts that own their own label.
    An error names the problem and the fix with an example (principle 17) —
    a bare red border is not an error message.
  -->
  <div :class="$attrs.class">
    <label v-if="label" :for="controlId" :class="[LABEL_CLASS, 'mb-1']">{{ label }}</label>
    <input
      :id="controlId"
      v-bind="controlAttrs"
      :value="modelValue"
      :type="type"
      :disabled="disabled"
      :aria-invalid="error ? 'true' : undefined"
      :aria-describedby="describedBy"
      :class="[FIELD_CLASS, error ? FIELD_INVALID_CLASS : FIELD_VALID_CLASS]"
      @input="$emit('update:modelValue', $event.target.value)"
    />
    <p v-if="error" :id="`${controlId}-error`" :class="[ERROR_TEXT_CLASS, 'mt-1']" role="alert">
      <svg class="w-3.5 h-3.5 mt-px flex-none" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
      </svg>
      <span>{{ error }}</span>
    </p>
    <p v-else-if="help" :id="`${controlId}-help`" :class="[HELP_CLASS, 'mt-1']">{{ help }}</p>
  </div>
</template>

<script setup>
import { computed, useAttrs, useId } from 'vue'
import {
  FIELD_CLASS,
  FIELD_VALID_CLASS,
  FIELD_INVALID_CLASS,
  LABEL_CLASS,
  HELP_CLASS,
  ERROR_TEXT_CLASS,
} from './fieldClasses'

defineOptions({ inheritAttrs: false })

const props = defineProps({
  modelValue: {
    type: [String, Number],
    default: '',
  },
  label: {
    type: String,
    default: '',
  },
  help: {
    type: String,
    default: '',
  },
  // Named, actionable, with an example: "Invalid cron expression — expected
  // 5 fields, got 4. Example: `0 9 * * 1`".
  error: {
    type: String,
    default: '',
  },
  type: {
    type: String,
    default: 'text',
  },
  disabled: {
    type: Boolean,
    default: false,
  },
  id: {
    type: String,
    default: '',
  },
})

defineEmits(['update:modelValue'])

const attrs = useAttrs()
const uid = useId()
const controlId = computed(() => props.id || uid)
// class/style stay on the wrapper; everything else (placeholder, spellcheck,
// @keyup.enter, …) lands on the control.
const controlAttrs = computed(() => {
  const { class: _c, style: _s, ...rest } = attrs
  return rest
})
const describedBy = computed(() => {
  if (props.error) return `${controlId.value}-error`
  if (props.help) return `${controlId.value}-help`
  return undefined
})
</script>
