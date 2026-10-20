<template>
  <!--
    #3164 — the GitHub token field for fork-to-own and repo binding.

    When the user has a personal token saved in Settings, the form uses it and
    says so; typing a different token is an explicit override. Without a saved
    token the field is required, as before. Only PRESENCE is known here — the
    saved value never reaches the browser — and the platform token is never
    offered: the backend refuses to make it a fork's identity.

    One fixed row for the summary and the input alike, so switching between
    them does not move the fields below (principle 4).
  -->
  <div data-testid="github-token-field">
    <div class="flex items-center justify-between gap-2">
      <label :for="inputId" class="block text-sm font-medium text-gray-700 dark:text-gray-300">
        GitHub token<span v-if="required" class="text-status-danger-500"> *</span>
      </label>
      <BaseButton
        v-if="hasSaved"
        variant="ghost"
        size="sm"
        :disabled="disabled"
        data-testid="github-token-toggle"
        @click="toggle"
      >{{ overriding ? 'Use my saved token' : 'Use a different token' }}</BaseButton>
    </div>
    <p
      v-if="hasSaved && !overriding"
      class="mt-1 flex min-h-[38px] items-center rounded-md border border-gray-200 bg-gray-50 px-3 text-sm text-gray-700 dark:border-gray-750 dark:bg-gray-900 dark:text-gray-300"
      data-testid="github-token-saved"
    >
      Using your saved GitHub token (Settings → GitHub token).
    </p>
    <BaseInput
      v-else
      :id="inputId"
      class="mt-1"
      :model-value="modelValue"
      type="password"
      autocomplete="off"
      placeholder="ghp_… or github_pat_…"
      :disabled="disabled"
      data-testid="github-token-input"
      @update:model-value="$emit('update:modelValue', $event)"
    />
    <slot />
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import BaseButton from './base/BaseButton.vue'
import BaseInput from './base/BaseInput.vue'

const props = defineProps({
  modelValue: { type: String, default: '' },
  hasSaved: { type: Boolean, default: false },
  inputId: { type: String, required: true },
  disabled: { type: Boolean, default: false },
})
// `update:overriding` (#3164 review): the parent must know the person chose
// "Use a different token". Without it an override submitted EMPTY looked like
// "use my saved token" and bound the broad saved credential — most sharply on
// a retry, where the panel clears the typed token by design and the field is
// still in override mode.
const emit = defineEmits(['update:modelValue', 'update:overriding'])

const overriding = ref(false)
const required = computed(() => !props.hasSaved || overriding.value)

function toggle() {
  overriding.value = !overriding.value
  emit('update:overriding', overriding.value)
  // Going back to the saved token drops whatever was typed: the form must not
  // send a half-typed override the user just chose not to use.
  if (!overriding.value) emit('update:modelValue', '')
}
</script>
