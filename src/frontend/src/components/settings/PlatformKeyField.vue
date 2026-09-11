<!--
  One optional platform key — GitHub, Resend or Gemini (ent#582).

  Shared by the first-run `keys` step and Settings → Integrations, so a key
  configured in either place is managed the same way afterwards. "Check & save"
  validates BEFORE it stores: format client-side, then the provider's live test,
  and only a passing key is saved (encrypted server-side, ent#435). Copy and
  rules live in `onboarding/steps/credentialSteps.js`.
-->
<template>
  <section class="space-y-2" :data-testid="`platform-key-${provider}`">
    <div class="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
      <h3 class="text-sm font-[550] text-gray-900 dark:text-gray-100">{{ p.title }}</h3>
      <span class="flex gap-3 text-[12.5px]">
        <a :href="p.providerUrl" target="_blank" rel="noopener noreferrer" :class="LINK_CLASS">
          {{ p.providerLabel }} ↗
        </a>
        <a :href="p.docsUrl" target="_blank" rel="noopener noreferrer" :class="LINK_CLASS">Docs ↗</a>
      </span>
    </div>
    <p class="text-[12.5px] text-gray-600 dark:text-gray-300">{{ p.enables }}</p>

    <!-- One footprint for checking / configured / not configured / unreadable. -->
    <p
      class="min-h-[18px] text-[12.5px]"
      :class="status?.configured
        ? 'text-status-success-700 dark:text-status-success-300'
        : 'text-gray-500 dark:text-gray-400'"
      role="status"
      :data-testid="`platform-key-${provider}-status`"
    >
      {{ statusText }}
    </p>

    <form class="space-y-2" @submit.prevent="checkAndSave">
      <BaseInput
        v-model="key"
        type="password"
        :label="p.label"
        :placeholder="status?.configured ? 'Paste a new key to replace it' : p.placeholder"
        :error="keyError"
        :disabled="busy"
        autocomplete="off"
        spellcheck="false"
        :data-testid="`platform-key-${provider}-input`"
      />
      <BaseInput
        v-if="provider === 'resend'"
        v-model="fromAddress"
        label="Send codes from"
        placeholder="noreply@your-domain.com"
        help="An address on a domain you have verified in Resend."
        :error="fromError"
        :disabled="busy"
        autocomplete="off"
        data-testid="platform-key-resend-from"
      />
      <div class="flex flex-wrap items-center gap-2">
        <BaseButton
          type="submit"
          variant="secondary"
          size="sm"
          :loading="busy"
          :loading-label="busyLabel"
          :disabled="!key.trim()"
          :data-testid="`platform-key-${provider}-save`"
        >
          Check &amp; save
        </BaseButton>
        <BaseButton
          v-if="status?.configured && status?.source === 'settings'"
          variant="ghost"
          size="sm"
          :disabled="busy"
          :data-testid="`platform-key-${provider}-remove`"
          @click="remove"
        >
          Remove
        </BaseButton>
      </div>
    </form>

    <p
      v-if="warning"
      class="text-[12.5px] text-status-warning-700 dark:text-status-warning-300"
      role="status"
    >
      {{ warning }}
    </p>
    <InlineError :message="verbError" @dismiss="verbError = ''" />
  </section>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { usePlatformKeysStore } from '../../stores/platformKeys'
import { apiErrorMessage } from '../../utils/apiError'
import BaseButton from '../base/BaseButton.vue'
import BaseInput from '../base/BaseInput.vue'
import InlineError from '../InlineError.vue'
import {
  KEY_PROVIDERS,
  configuredLine,
  describeKeyTest,
  fromAddressError,
  keyFormatError,
  laterHint,
  prefillFromAddress,
} from '../onboarding/steps/credentialSteps'

const props = defineProps({
  provider: {
    type: String,
    required: true,
    validator: (v) => ['github', 'resend', 'gemini'].includes(v),
  },
  // In the first-run flow an unconfigured key also says where to add it later.
  firstRun: { type: Boolean, default: false },
})
const emit = defineEmits(['saved'])

const LINK_CLASS =
  'rounded text-action-primary-600 dark:text-action-primary-500 underline-offset-2 hover:underline ' +
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40'

const store = usePlatformKeysStore()
const p = computed(() => KEY_PROVIDERS[props.provider])
const status = computed(() => store.status?.[props.provider])

const key = ref('')
const fromAddress = ref('')
const keyError = ref('')
const fromError = ref('')
const warning = ref('')
const verbError = ref('')
const busy = ref(false)
const busyLabel = ref('')

const statusText = computed(() => {
  if (!store.hasLoaded) {
    return store.loadError ? "Couldn't read whether a key is set." : 'Checking whether a key is set…'
  }
  if (status.value?.configured) return configuredLine(status.value)
  return props.firstRun
    ? `Not set. ${p.value.skipConsequence} ${laterHint()}`
    : `Not set. ${p.value.skipConsequence}`
})

// Pre-fill the sender once, from the address in force — never over an edit,
// and never with a placeholder address Resend is guaranteed to refuse.
watch(
  () => status.value?.from_address,
  (current) => {
    if (props.provider === 'resend' && !fromAddress.value) fromAddress.value = prefillFromAddress(current)
  },
  { immediate: true }
)

// A new paste clears the last verdict about the old one.
watch([key, fromAddress], () => {
  keyError.value = ''
  fromError.value = ''
})

onMounted(() => {
  if (!store.hasLoaded) store.fetchStatus()
})

async function checkAndSave() {
  const value = key.value.trim()
  keyError.value = keyFormatError(props.provider, value)
  fromError.value =
    props.provider === 'resend' && fromAddress.value.trim() ? fromAddressError(fromAddress.value) : ''
  if (keyError.value || fromError.value || !value) return

  const body = { api_key: value }
  if (props.provider === 'resend' && fromAddress.value.trim()) body.from_address = fromAddress.value.trim()

  warning.value = ''
  verbError.value = ''
  busy.value = true
  try {
    busyLabel.value = 'Checking…'
    const result = describeKeyTest(props.provider, await store.test(props.provider, body))
    if (!result.ok) {
      if (result.field === 'from') fromError.value = result.error
      else keyError.value = result.error
      return
    }
    busyLabel.value = 'Saving…'
    await store.save(props.provider, body)
    warning.value = result.warning
    key.value = ''
    emit('saved', props.provider)
  } catch (e) {
    verbError.value = apiErrorMessage(e, `Couldn't save the ${p.value.title} key — try again.`)
  } finally {
    busy.value = false
  }
}

async function remove() {
  verbError.value = ''
  warning.value = ''
  busy.value = true
  busyLabel.value = 'Removing…'
  try {
    await store.remove(props.provider)
  } catch (e) {
    verbError.value = apiErrorMessage(e, `Couldn't remove the ${p.value.title} key — try again.`)
  } finally {
    busy.value = false
  }
}
</script>
