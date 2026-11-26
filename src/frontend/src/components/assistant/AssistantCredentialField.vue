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
      v-if="isOpen && checked && current === true"
      class="text-[12.5px] text-gray-900 dark:text-gray-100"
      data-testid="assistant-credential-replaces"
    >{{ replacesText }}</p>

    <!-- This instance's Claude credential: where to get it (ent#1355), the three terminal steps (ent#1354) -->
    <template v-if="isOpen && checked && claude">
      <ol v-if="claude.steps" class="list-decimal pl-5 space-y-0.5 text-[12.5px] text-gray-900 dark:text-gray-100" data-testid="assistant-credential-steps">
        <li v-for="(step, i) in claude.steps" :key="i">
          {{ step.before }}
          <code v-if="step.command" class="font-mono" data-testid="assistant-credential-command">{{ step.command }}</code>
          {{ step.after || '' }}
        </li>
      </ol>
      <p v-if="claude.alternative" class="text-[12.5px] text-gray-600 dark:text-gray-300" data-testid="assistant-credential-alternative">
        {{ claude.alternative.lead }} <a :href="claude.alternative.href" target="_blank" rel="noopener" class="underline text-action-primary-600 dark:text-action-primary-400">{{ claude.alternative.label }}</a>
      </p>
      <p v-else class="text-[12.5px] text-gray-600 dark:text-gray-300" data-testid="assistant-credential-help">
        {{ claude.lead }} <a :href="claude.href" target="_blank" rel="noopener" class="underline text-action-primary-600 dark:text-action-primary-400">{{ claude.label }}</a>
      </p>
    </template>

    <!-- The field opens only once the instance has said it may take this name. -->
    <template v-if="isOpen && checked">
      <label :for="inputId" class="sr-only">{{ claude ? 'Your Claude token or Anthropic API key' : entry.card.name }}</label>
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
          :placeholder="claude && claude.steps ? 'Paste the token (or an API key)' : 'Type or paste the value'"
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
      v-else-if="!isOpen"
      class="text-[12.5px] font-medium text-gray-900 dark:text-gray-100"
      data-testid="assistant-credential-outcome"
    >{{ outcome }}</p>
    <!-- After the instance's Claude key is saved: the agents it connected, and the instance's own check (ent#1355) -->
    <p v-if="claude && entry.state === 'done'" class="text-[12.5px] text-gray-600 dark:text-gray-300" data-testid="assistant-credential-connected">{{ connectedText }}</p>
    <p v-if="claude && entry.state === 'done' && entry.working === false" class="text-[12.5px] font-medium text-status-danger-700 dark:text-status-danger-400" data-testid="assistant-credential-check">
      Saved, but Anthropic did not accept it{{ entry.problem ? `: ${entry.problem}` : '.' }}
    </p>
  </form>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'
import BaseButton from '../base/BaseButton.vue'
import BaseInput from '../base/BaseInput.vue'
import InlineError from '../InlineError.vue'
import { useAssistantStore } from '../../stores/assistant'
import { credentialWhere, savedReportText } from '../../utils/assistantCards'
import { claudeHelp, isClaudeCredential } from '../../utils/firstSession'
import { claudeSavedText } from '../onboarding/steps/credentialSteps'

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
const holds = ref(null)       // the Claude card only: which credential the instance holds (ent#1354)
const checked = ref(false)    // the instance has answered whether it takes this name

const where = computed(() => credentialWhere(props.entry.card))
const claude = computed(() => (isClaudeCredential(props.entry.card) ? claudeHelp() : null))
// ent#1354 (ruled 2026-11-24, evening): the card's save switches no existing agent; it names the ones not on it.
// It says what the save reported (connected, moving, not on it), never more.
const connectedText = computed(() => savedReportText(props.entry, claudeSavedText))
// ent#1354 (walk 2026-11-24 14:50Z): the Claude card said "CLAUDE_SUBSCRIPTION is
// already set" on an instance holding only an API key. Its note names what is there.
const HOLDS = {
  api_key: 'an Anthropic API key',
  subscription: 'a Claude subscription',
  subscription_and_api_key: 'a Claude subscription and an Anthropic API key',
}
const replacesText = computed(() => {
  if (!claude.value) {
    return `${props.entry.card.name} is already set on ${where.value}. Saving replaces it; the current value is never shown.`
  }
  return (
    `This instance already has ${HOLDS[holds.value] || 'a Claude credential'}. A token you paste is saved as its ` +
    'Claude subscription and a key as its API key, replacing only one of the same kind; the current value is never shown.'
  )
})
const running = computed(() => props.entry.state === 'running')
const isOpen = computed(() => ['open', 'running'].includes(props.entry.state))
// ent#1390: the Claude card says what was saved in words (subscription or API key), never a variable name.
const CLAUDE_SAVED = { subscription: 'your Claude subscription', api_key: 'your Anthropic API key' }
const claudeOutcome = computed(() => {
  const what = CLAUDE_SAVED[props.entry.kind]
  if (props.entry.state === 'refused') return `Claude can't be connected from here: ${props.entry.reason} Nothing was stored.`
  if (props.entry.state === 'done') {
    if (!what) return 'Claude is connected on this instance.'
    return `${props.entry.reason === 'replaced' ? 'Replaced' : 'Saved'} ${what} on this instance.`
  }
  return 'Claude is not connected. Nothing was stored.'
})
const outcome = computed(() => {
  if (claude.value) return claudeOutcome.value
  if (props.entry.state === 'refused') {
    return `${props.entry.card.name} can't be set from here: ${props.entry.reason} Nothing was stored.`
  }
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
  if (state?.refused) {
    store.refuseCredential(props.messageId, props.entry.id, state.refused)
    return
  }
  current.value = state ? state.set : null
  holds.value = state?.current ?? null
  checked.value = true
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
