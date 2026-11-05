<template>
  <!--
    trinity-enterprise#1066 — the secret field a `credential` ask opens.

    The person types the value the agent asked for BY NAME; it is POSTed once to
    the ask's `/credential` route (store `provideCredential`), stored in the
    agent's environment, and the ask ends "provided". The value lives in this
    component only while it is being typed: Send empties the field before the
    request goes out, an unmount empties the element, and nothing here writes
    to the drafts, to storage, to a URL or to the console. The card's own ink
    throughout — no colour of its own (the #2605 ratchet starts this file at
    zero).
  -->
  <form
    ref="formEl"
    class="mt-2"
    autocomplete="off"
    :data-testid="`${prefix}-form-${ask.id}`"
    @submit.prevent="submit"
  >
    <label :for="inputId" class="block text-xs font-medium">{{ name }}</label>
    <div class="mt-1 flex items-center gap-2">
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
        :disabled="sending"
        placeholder="Type or paste the value"
        :data-testid="`${prefix}-input-${ask.id}`"
      />
      <BaseButton
        variant="ghost"
        size="sm"
        :aria-pressed="String(revealed)"
        :aria-label="revealed ? 'Hide the value' : 'Show the value'"
        :aria-controls="inputId"
        :data-testid="`${prefix}-toggle-${ask.id}`"
        @click="revealed = !revealed"
      >{{ revealed ? 'Hide' : 'Show' }}</BaseButton>
      <BaseButton
        type="submit"
        size="sm"
        :loading="sending"
        loading-label="Storing…"
        :disabled="!value"
        :data-testid="`${prefix}-send-${ask.id}`"
      >Send</BaseButton>
    </div>
    <p class="mt-1 text-xs" :data-testid="`${prefix}-caption-${ask.id}`">
      <template v-if="destination">
        Sent to the agent {{ destination.agent_name }} on the Trinity at {{ destination.host }} and stored there as {{ name }}. Nothing is kept here after delivery; it never appears in the chat, and the agent that asked is only told it was delivered.
      </template>
      <template v-else>
        Stored on the agent as {{ name }}. It never appears in the chat, and the agent is only told it was provided.
      </template>
    </p>
    <div v-if="error" class="mt-1.5" :data-testid="`${prefix}-error-${ask.id}`">
      <InlineError :message="error" @dismiss="error = ''" />
    </div>
  </form>
</template>

<script setup>
import { computed, onBeforeUnmount, ref, useId, watch } from 'vue'
import BaseButton from '../base/BaseButton.vue'
import BaseInput from '../base/BaseInput.vue'
import InlineError from '../InlineError.vue'
import { useClientPortalStore } from '@/stores/clientPortal'

const props = defineProps({
  // The pending `credential` ask (the Workspace projection).
  ask: { type: Object, required: true },
  testidPrefix: { type: String, default: 'portal-credential' },
})
const emit = defineEmits(['provided'])

const store = useClientPortalStore()
const prefix = computed(() => props.testidPrefix)
const name = computed(() => props.ask.credential_name || 'Credential')
// trinity-enterprise#1109: where the value lands when that is not the asking
// agent — `{agent_name, host}` from the projection, in plain words.
const destination = computed(() => props.ask.credential_destination || null)
const inputId = `portal-credential-${useId()}`
const formEl = ref(null)

const value = ref('')
const revealed = ref(false)
const sending = ref(false)
const error = ref('')

// Typing again is the fix for every refusal; the old words go with it.
watch(value, (v) => { if (v) error.value = '' })

// The refusals the sink names, in the person's words. Anything else is the
// server's own message, which never carries the value (the route's contract).
const COPY = {
  agent_not_running: "The agent isn't running, so nothing was stored. Once it has been started, send the value again.",
  scrub_unavailable: "Nothing was stored: the platform couldn't arm the protection that keeps this value out of the agent's replies. Try again in a minute.",
  asks_unavailable: 'Asks are unavailable right now, so nothing was stored. Try again in a minute.',
  // trinity-enterprise#1109: a delivery to another Trinity that did not land —
  // each a plain retry; the server's own sentence names the host.
  destination_key_missing: "The agent that asked doesn't hold a key to that Trinity yet, so nothing was stored. Once it does, send the value again.",
}

function refusalCopy(err) {
  const detail = err?.response?.data?.detail
  const code = detail && typeof detail === 'object' && !Array.isArray(detail) ? detail.code : null
  if (code === 'already_resolved') {
    return detail.credential_written
      ? 'The value was stored, but this ask had already ended. The agent will ask again if it still needs it.'
      : 'This ask was already answered or ended, so nothing was stored.'
  }
  if (code && COPY[code]) return COPY[code]
  const said = code ? detail.message : typeof detail === 'string' ? detail : ''
  return said || "Couldn't store the value. Nothing was posted to the chat; try again."
}

async function submit() {
  if (sending.value || !value.value) return
  // Held in this call only: the field is emptied before the request goes out,
  // so no state of the component keeps the value whatever the server answers.
  const secret = value.value
  value.value = ''
  revealed.value = false
  error.value = ''
  sending.value = true
  try {
    const answered = await store.provideCredential(props.ask.id, secret)
    emit('provided', answered)
  } catch (err) {
    error.value = refusalCopy(err)
  } finally {
    sending.value = false
  }
}

// An unmount (the ask ended elsewhere, a thread switch) takes the typed value
// with it — the element too, which no render reaches once unmounting.
onBeforeUnmount(() => {
  value.value = ''
  const input = formEl.value?.querySelector('input')
  if (input) input.value = ''
})
</script>
