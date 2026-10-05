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
    <input
      v-else
      :id="inputId"
      :value="modelValue"
      type="password"
      autocomplete="off"
      placeholder="ghp_… or github_pat_…"
      :disabled="disabled"
      class="mt-1 block w-full min-h-[38px] rounded-md border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 shadow-sm placeholder-gray-500 focus:border-action-primary-500 focus:ring-action-primary-500 disabled:opacity-50 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100 dark:placeholder-gray-400"
      data-testid="github-token-input"
      @input="$emit('update:modelValue', $event.target.value)"
    />
    <slot />
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import BaseButton from './base/BaseButton.vue'

const props = defineProps({
  modelValue: { type: String, default: '' },
  hasSaved: { type: Boolean, default: false },
  inputId: { type: String, required: true },
  disabled: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue'])

const overriding = ref(false)
const required = computed(() => !props.hasSaved || overriding.value)

function toggle() {
  overriding.value = !overriding.value
  // Going back to the saved token drops whatever was typed: the form must not
  // send a half-typed override the user just chose not to use.
  if (!overriding.value) emit('update:modelValue', '')
}
</script>
