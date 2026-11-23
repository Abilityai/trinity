<template>
  <!--
    The masked field a credential card opens in the assistant panel. The
    assistant asked for a credential by name; the person types the value here
    and it is posted once to this instance (store `answerCredential`). The
    assistant is only told "Provided" or "Not provided".

    The value lives in this component only while it is being typed: Save empties
    the field before the request goes out, and an unmount empties the element.
    Nothing here writes it to the conversation, to storage, to a URL or to the
    console. Mirrors the Workspace secret field (PortalCredentialField.vue).
  -->
  <form
    ref="formEl"
    class="space-y-1.5"
    autocomplete="off"
    data-testid="assistant-credential"
    @submit.prevent="submit"
  >
    <p v-if="entry.card.why" class="text-[12.5px] text-gray-900 dark:text-gray-100" data-testid="assistant-credential-why">
      In the assistant's words: “{{ entry.card.why }}”
    </p>
    <p
      v-if="isOpen && current === true"
      class="text-[12.5px] text-gray-900 dark:text-gray-100"
      data-testid="assistant-credential-replaces"
    >{{ entry.card.name }} is already set on {{ where }}. Saving replaces it; the current value is never shown.</p>

    <template v-if="isOpen">
      <label :for="inputId" class="sr-only">{{ entry.card.name }}</label>
      <div class="flex items-center gap-2">
        <BaseInput
          :id="inputId"
          v-model="value"
          class="flex-1 min-w-0"
          :type="revealed ? 'text' : 'password'"
          autocomplete="off"
          spellcheck="false"
          autocapitalize="off"
          autocorrect="off"
          data-1p-ignore="true"
          data-lpignore="true"
          :disabled="running"
          placeholder="Type or paste the value"
          data-testid="assistant-credential-input"
        />
        <BaseButton
          variant="ghost"
          size="sm"
          :aria-pressed="String(revealed)"
          :aria-label="revealed ? 'Hide the value' : 'Show the value'"
          :aria-controls="inputId"
          data-testid="assistant-credential-toggle"
          @click="revealed = !revealed"
        >{{ revealed ? 'Hide' : 'Show' }}</BaseButton>
      </div>
      <div v-if="error" data-testid="assistant-credential-error">
        <InlineError :message="error" @dismiss="error = ''" />
      </div>
      <div class="flex flex-wrap gap-2 pt-1">
        <BaseButton
          type="submit"
          size="sm"
          :loading="running"
          loading-label="Storing…"
          :disabled="!value || store.sending"
          data-testid="assistant-credential-save"
        >Save</BaseButton>
        <BaseButton
          variant="secondary"
          size="sm"
          :disabled="running || store.sending"
          data-testid="assistant-credential-cancel"
          @click="cancel"
        >Cancel</BaseButton>
      </div>
    </template>
    <p
      v-else
      class="text-[12.5px] font-medium text-gray-900 dark:text-gray-100"
      data-testid="assistant-credential-outcome"
    >{{ outcome }}</p>
  </form>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'
import BaseButton from '../base/BaseButton.vue'
import BaseInput from '../base/BaseInput.vue'
import InlineError from '../InlineError.vue'
import { useAssistantStore } from '../../stores/assistant'
import { credentialWhere } from '../../utils/assistantCards'

const props = defineProps({
  // The message holding the card, and the card's entry ({ id, card, state, reason }).
  messageId: { type: String, required: true },
  entry: { type: Object, required: true },
  route: { type: Object, default: null },
})

const store = useAssistantStore()
const inputId = `assistant-credential-${useId()}`
const formEl = ref(null)
const value = ref('')
const revealed = ref(false)
const error = ref('')
const current = ref(null)     // whether it is set already: true / false / null (unknown)

const where = computed(() => credentialWhere(props.entry.card))
const running = computed(() => props.entry.state === 'running')
const isOpen = computed(() => ['open', 'running'].includes(props.entry.state))
const outcome = computed(() => {
  if (props.entry.state === 'done') {
    return props.entry.reason === 'replaced'
      ? `${props.entry.card.name} replaced on ${where.value}.`
      : `${props.entry.card.name} provided to ${where.value}.`
  }
  return `${props.entry.card.name} not provided. Nothing was stored.`
})

// Typing again is the fix for every refusal; the old words go with it.
watch(value, (v) => { if (v) error.value = '' })

onMounted(async () => {
  if (!isOpen.value) return
  const state = await store.credentialState(props.entry.card)
  current.value = state ? state.set : null
})

async function submit() {
  if (running.value || !value.value || store.sending) return
  // Held in this call only: the field is emptied before the request goes out.
  const secret = value.value
  value.value = ''
  revealed.value = false
  error.value = ''
  const result = await store.answerCredential(props.messageId, props.entry.id, secret, props.route)
  if (!result.ok && result.error) error.value = result.error
}

async function cancel() {
  value.value = ''
  error.value = ''
  await store.answerCredential(props.messageId, props.entry.id, null, props.route)
}

onBeforeUnmount(() => {
  value.value = ''
  const input = formEl.value?.querySelector('input')
  if (input) input.value = ''
})
</script>
