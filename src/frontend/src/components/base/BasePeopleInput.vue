<template>
  <!--
    The people-picker form field (trinity-enterprise#1153; design-system.md
    primitives table). BaseInput's anatomy — label, control, help or error — with
    a suggestion list under the control. Two modes:

      suggest  (suggest-and-allow) any text submits; a row is accepted only by
               ↑↓ + Enter or a click, and plain Enter is left to the form.
      pick     (must-pick) the field is natively invalid until a person is
               chosen; text that matches no one says so by name.

    The field never knows a URL: `fetch(q)` is the surface's own scoped read, so
    who may appear is decided server-side by the one people search. The decisions
    are `peopleInput.js`'s; the lookup state is `usePeoplePicker`'s.
  -->
  <div :class="$attrs.class">
    <label v-if="label" :for="controlId" :class="[LABEL_CLASS, 'mb-1']">{{ label }}</label>
    <div class="relative">
      <input
        :id="controlId"
        ref="inputEl"
        v-bind="controlAttrs"
        :value="modelValue"
        :type="type"
        :disabled="disabled"
        role="combobox"
        autocomplete="off"
        aria-autocomplete="list"
        :aria-expanded="open ? 'true' : 'false'"
        :aria-controls="open && rows.length ? listId : undefined"
        :aria-activedescendant="open && active >= 0 ? `${listId}-${active}` : undefined"
        :aria-invalid="shownError ? 'true' : undefined"
        :aria-describedby="describedBy"
        :class="[FIELD_CLASS, shownError ? FIELD_INVALID_CLASS : FIELD_VALID_CLASS]"
        @input="$emit('update:modelValue', $event.target.value)"
        @keydown="onKeydown"
        @blur="onBlur"
        @invalid="touched = true"
      />
      <PortalTypeahead
        v-if="open"
        kind="@"
        plain
        placement="below"
        :title="title"
        :list-id="listId"
        :rows="rows"
        :active-index="active"
        :empty-message="emptyMessage"
        @pick="pick"
        @hover="active = $event"
      />
    </div>
    <p v-if="shownError" :id="`${controlId}-error`" :class="[ERROR_TEXT_CLASS, 'mt-1']" role="alert">
      <svg class="w-3.5 h-3.5 mt-px flex-none" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
      </svg>
      <span>{{ shownError }}</span>
    </p>
    <p v-else-if="chosenLine" :id="`${controlId}-help`" :class="[HELP_CLASS, 'mt-1']">{{ chosenLine }}</p>
    <p v-else-if="help" :id="`${controlId}-help`" :class="[HELP_CLASS, 'mt-1']">{{ help }}</p>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, ref, useAttrs, useId, watch } from 'vue'
import PortalTypeahead from '../portal/PortalTypeahead.vue'
import { usePeoplePicker } from '../../composables/usePeoplePicker'
import {
  FIELD_CLASS,
  FIELD_VALID_CLASS,
  FIELD_INVALID_CLASS,
  LABEL_CLASS,
  HELP_CLASS,
  ERROR_TEXT_CLASS,
} from './fieldClasses'
import {
  PEOPLE_INPUT_DEBOUNCE_MS,
  PEOPLE_INPUT_MIN_QUERY,
  defaultNoMatch,
  isPickedText,
  keyAction,
  mustPickError,
  personRows,
} from './peopleInput'

defineOptions({ inheritAttrs: false })

const props = defineProps({
  // The text in the field (an email once a person is picked).
  modelValue: { type: String, default: '' },
  // v-model:picked — the chosen person `{email, label, role?}`, or null.
  picked: { type: Object, default: null },
  // (q) => Promise<[{email, label, role?}]> — the surface's own scoped read.
  fetch: { type: Function, required: true },
  mode: { type: String, default: 'suggest', validator: (m) => m === 'suggest' || m === 'pick' },
  label: { type: String, default: '' },
  help: { type: String, default: '' },
  // An outside error (a refused write) wins over the field's own validation.
  error: { type: String, default: '' },
  title: { type: String, default: 'People with an account' },
  // (text) => the line shown when the server said no one matches.
  noMatch: { type: Function, default: defaultNoMatch },
  // pick mode only: (text) => true for text taken WITHOUT a pick — an escape
  // hatch for a value that is not a person (a task owner's `role:<id>`).
  acceptText: { type: Function, default: null },
  type: { type: String, default: 'text' },
  disabled: { type: Boolean, default: false },
  id: { type: String, default: '' },
  minQuery: { type: Number, default: PEOPLE_INPUT_MIN_QUERY },
  debounceMs: { type: Number, default: PEOPLE_INPUT_DEBOUNCE_MS },
})

const emit = defineEmits(['update:modelValue', 'update:picked', 'unavailable'])

const attrs = useAttrs()
const uid = useId()
const controlId = computed(() => props.id || uid)
const listId = computed(() => `${controlId.value}-people`)
const controlAttrs = computed(() => {
  const { class: _c, style: _s, ...rest } = attrs
  return rest
})

const inputEl = ref(null)
const picker = usePeoplePicker({
  fetch: (q) => props.fetch(q),
  debounceMs: props.debounceMs,
  minQuery: props.minQuery,
  giveUpOn: 'failure',
})
const chosen = ref(props.picked)
const active = ref(-1)
const dismissed = ref(false)
const touched = ref(false)

const text = computed(() => String(props.modelValue || '').trim())
const answered = computed(() => picker.answeredFor.value !== null && picker.answeredFor.value === text.value)
const rows = computed(() => (answered.value ? personRows(picker.people.value) : []))
const matchedNone = computed(() => answered.value && picker.people.value.length === 0)
const emptyMessage = computed(() => (matchedNone.value ? props.noMatch(text.value) : ''))
const open = computed(() =>
  !dismissed.value && !picker.unavailable.value && answered.value
  && !isPickedText(text.value, chosen.value) && (rows.value.length > 0 || !!emptyMessage.value))

const pickError = computed(() => mustPickError({
  mode: props.mode, text: text.value, picked: chosen.value, matchedNone: matchedNone.value, noMatch: props.noMatch,
  accepted: !!text.value && !!props.acceptText?.(text.value),
}))
const shownError = computed(() => props.error || (touched.value ? pickError.value : ''))
const chosenLine = computed(() =>
  props.mode === 'pick' && chosen.value && isPickedText(text.value, chosen.value)
    ? `${chosen.value.label || chosen.value.email} · ${chosen.value.email}` : '')
const describedBy = computed(() => {
  if (shownError.value) return `${controlId.value}-error`
  if (chosenLine.value || props.help) return `${controlId.value}-help`
  return attrs['aria-describedby'] || undefined
})

// The native form refuses a must-pick submit until a person is chosen.
watch([pickError, inputEl], ([msg, el]) => {
  el?.setCustomValidity?.(props.mode === 'pick' ? msg : '')
}, { immediate: true })

watch(() => props.picked, (p) => { chosen.value = p })

watch(() => props.modelValue, (value) => {
  dismissed.value = false
  active.value = -1
  if (isPickedText(value, chosen.value)) {
    picker.clear()
    return
  }
  if (chosen.value) {
    chosen.value = null
    emit('update:picked', null)
  }
  picker.lookup(value)
})

watch(() => picker.people.value, () => { active.value = -1 })
watch(() => picker.unavailable.value, (gone) => { if (gone) emit('unavailable') })

function pick(index) {
  const person = picker.people.value[index]
  if (!person) return
  chosen.value = person
  dismissed.value = true
  emit('update:picked', person)
  emit('update:modelValue', person.email)
}

function onKeydown(event) {
  const next = keyAction(event.key, { open: open.value, active: active.value, count: rows.value.length })
  if (next.action === 'none') return
  event.preventDefault()
  if (next.action === 'close') dismissed.value = true
  else if (next.action === 'move') active.value = next.active
  else if (next.action === 'pick') pick(next.active)
}

function onBlur() {
  dismissed.value = true
  touched.value = true
}

/** Close the list (a parent's submit, a reset). */
function close() {
  dismissed.value = true
  picker.clear()
}

/** Show the field's validation now; true when it may be submitted. */
function validate() {
  touched.value = true
  return !pickError.value
}

onBeforeUnmount(() => picker.clear())

defineExpose({ close, validate, focus: () => inputEl.value?.focus() })
</script>
