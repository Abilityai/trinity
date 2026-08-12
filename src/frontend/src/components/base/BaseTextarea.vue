<template>
  <!--
    The textarea primitive (docs/memory/design-system.md §5, #2122).
    BaseInput's field recipe + min-height 84px and VERTICAL resize only —
    horizontal resize breaks layout (principles 6/7). The mono variant is
    the voice of machine text: prompts, instructions, config.
  -->
  <div :class="$attrs.class">
    <label v-if="label" :for="controlId" :class="[LABEL_CLASS, 'mb-1']">{{ label }}</label>
    <textarea
      :id="controlId"
      v-bind="controlAttrs"
      :value="modelValue"
      :rows="rows"
      :disabled="disabled"
      :aria-invalid="error ? 'true' : undefined"
      :aria-describedby="describedBy"
      :class="[
        FIELD_CLASS,
        error ? FIELD_INVALID_CLASS : FIELD_VALID_CLASS,
        'min-h-[84px] resize-y leading-normal',
        mono ? 'font-mono text-[12.5px]' : '',
      ]"
      @input="$emit('update:modelValue', $event.target.value)"
    ></textarea>
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
    type: String,
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
  error: {
    type: String,
    default: '',
  },
  // Machine text: prompts, instructions, cron, config.
  mono: {
    type: Boolean,
    default: false,
  },
  rows: {
    type: [String, Number],
    default: 4,
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
