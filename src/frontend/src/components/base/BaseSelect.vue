<template>
  <!--
    The select primitive (docs/memory/design-system.md §5, #2122).
    Same field recipe as BaseInput; `appearance: none` with a custom chevron
    (tertiary ink, pointer-events none), padding-right 32px so text never
    collides. Options come through the default slot as native <option>s.

    `variant="ghost"` (#2662) swaps the field recipe for the borderless,
    content-width one — the same control where a select is a lightweight
    preference beside a conversation rather than a field in a form. The native
    <select> is kept deliberately: it carries keyboard operation, focus
    management and the platform picker on touch devices for free, none of which
    a hand-rolled trigger + popover would inherit.
  -->
  <div :class="$attrs.class">
    <label v-if="label" :for="controlId" :class="[LABEL_CLASS, 'mb-1']">{{ label }}</label>
    <div class="relative">
      <select
        :id="controlId"
        v-bind="controlAttrs"
        :value="modelValue"
        :disabled="disabled"
        :aria-invalid="error ? 'true' : undefined"
        :aria-describedby="describedBy"
        :class="[recipe.field, error ? FIELD_INVALID_CLASS : recipe.valid, 'appearance-none', recipe.pad]"
        @change="$emit('update:modelValue', $event.target.value)"
      >
        <slot />
      </select>
      <svg
        :class="['pointer-events-none absolute top-1/2 -translate-y-1/2 text-gray-500 dark:text-gray-400', recipe.chevron]"
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
        stroke-width="2"
        aria-hidden="true"
      >
        <path stroke-linecap="round" stroke-linejoin="round" d="M19 9l-7 7-7-7" />
      </svg>
    </div>
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
  FIELD_GHOST_CLASS,
  FIELD_GHOST_VALID_CLASS,
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
  error: {
    type: String,
    default: '',
  },
  disabled: {
    type: Boolean,
    default: false,
  },
  id: {
    type: String,
    default: '',
  },
  /**
   * 'field' (default) is the settings-form recipe shared with BaseInput.
   * 'ghost' is the borderless, content-width one for chat chrome (#2662).
   */
  variant: {
    type: String,
    default: 'field',
    validator: (v) => ['field', 'ghost'].includes(v),
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
/**
 * One place decides every variant-dependent class, so a third variant cannot be
 * half-added. `pad` is separate from `field` because the chevron's gutter and
 * its offset have to agree: 32px + right-10px for the field box, 28px +
 * right-8px for the smaller ghost chevron.
 */
const recipe = computed(() =>
  props.variant === 'ghost'
    ? {
        field: FIELD_GHOST_CLASS,
        valid: FIELD_GHOST_VALID_CLASS,
        pad: 'pr-7',
        chevron: 'right-2 h-3 w-3',
      }
    : {
        field: FIELD_CLASS,
        valid: FIELD_VALID_CLASS,
        pad: 'pr-8',
        chevron: 'right-[10px] h-3.5 w-3.5',
      }
)
const describedBy = computed(() => {
  if (props.error) return `${controlId.value}-error`
  if (props.help) return `${controlId.value}-help`
  return undefined
})
</script>
