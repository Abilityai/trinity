<template>
  <div class="flex items-center gap-2" role="group" aria-label="6-digit sign-in code">
    <input
      v-for="(d, i) in digits"
      :key="i"
      ref="boxes"
      :value="d"
      inputmode="numeric"
      autocomplete="one-time-code"
      maxlength="1"
      :aria-label="`Digit ${i + 1}`"
      class="w-11 h-14 text-center text-xl font-semibold rounded-xl border bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100
             border-gray-300 dark:border-gray-700 focus:border-action-primary-500 focus:ring-2 focus:ring-action-primary-500/40 focus:outline-none transition"
      @input="onInput(i, $event)"
      @keydown="onKeydown(i, $event)"
      @paste="onPaste"
      @focus="$event.target.select()"
    />
  </div>
</template>

<script setup>
import { ref, watch, nextTick } from 'vue'

const props = defineProps({
  modelValue: { type: String, default: '' },
  length: { type: Number, default: 6 },
})
const emit = defineEmits(['update:modelValue', 'complete'])

const digits = ref(Array.from({ length: props.length }, (_, i) => props.modelValue[i] || ''))
const boxes = ref([])

// Keep boxes in sync when the parent clears/sets the value (e.g. on "resend").
watch(() => props.modelValue, (v) => {
  const next = Array.from({ length: props.length }, (_, i) => (v || '')[i] || '')
  if (next.join('') !== digits.value.join('')) digits.value = next
})

function emitValue() {
  const val = digits.value.join('')
  emit('update:modelValue', val)
  if (val.length === props.length && digits.value.every((d) => d !== '')) emit('complete', val)
}

function onInput(i, e) {
  const raw = (e.target.value || '').replace(/\D/g, '')
  digits.value[i] = raw.slice(-1) || ''
  e.target.value = digits.value[i]
  if (digits.value[i] && i < props.length - 1) focusBox(i + 1)
  emitValue()
}

function onKeydown(i, e) {
  if (e.key === 'Backspace' && !digits.value[i] && i > 0) {
    focusBox(i - 1)
    digits.value[i - 1] = ''
    emitValue()
    e.preventDefault()
  } else if (e.key === 'ArrowLeft' && i > 0) {
    focusBox(i - 1)
  } else if (e.key === 'ArrowRight' && i < props.length - 1) {
    focusBox(i + 1)
  }
}

function onPaste(e) {
  e.preventDefault()
  const text = (e.clipboardData?.getData('text') || '').replace(/\D/g, '').slice(0, props.length)
  if (!text) return
  digits.value = Array.from({ length: props.length }, (_, i) => text[i] || '')
  emitValue()
  focusBox(Math.min(text.length, props.length - 1))
}

async function focusBox(i) {
  await nextTick()
  boxes.value[i]?.focus()
}

defineExpose({ focusFirst: () => focusBox(0) })
</script>
