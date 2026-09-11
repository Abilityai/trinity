<!--
  Step `claude` (ent#582) — the ONLY required first-run step.

  Two ways in, both pasted into the browser: a Claude subscription token, or a
  platform Anthropic API key (created in the browser — the path with no
  terminal at all). A credential is validated BEFORE it is accepted: format
  here, then a live check with Anthropic, and only then saved through the same
  endpoints Settings → Integrations uses. `complete` is emitted only after a
  validated save. Server-side, the install's first credential is also handed
  to the agents that were created without one (the starter fleet), so the
  operator can run something straight away. Copy + rules: credentialSteps.js.
-->
<template>
  <div data-testid="first-run-step-claude">
    <FirstRunStepHeader
      kicker="Model access"
      :title="connected ? 'Claude is connected' : 'Connect Claude'"
      :lead="lead"
      :badge="connected ? 'Done' : 'Required'"
      schematic="claude"
    />

    <div class="mt-5 max-w-lg space-y-3">
      <p
        v-if="connected"
        role="status"
        class="text-sm text-status-success-700 dark:text-status-success-300"
        data-testid="first-run-claude-connected"
      >
        {{ connectedText }}
      </p>

      <OverflowTabs v-model="tab" :tabs="CLAUDE_TABS" dense />

      <form class="space-y-2" @submit.prevent="connect">
        <BaseInput
          v-model="value"
          type="password"
          :label="copy.label"
          :placeholder="copy.placeholder"
          :help="copy.help"
          :error="fieldError"
          :disabled="busy"
          autocomplete="off"
          spellcheck="false"
          data-testid="first-run-claude-input"
        />
        <div class="flex flex-wrap items-center gap-x-3 gap-y-2">
          <BaseButton
            type="submit"
            :variant="connected ? 'secondary' : 'primary'"
            size="sm"
            :loading="busy"
            :loading-label="busyLabel"
            :disabled="!value.trim()"
            data-testid="first-run-claude-connect"
          >
            Check &amp; connect
          </BaseButton>
          <BaseButton
            v-if="otherTab"
            variant="ghost"
            size="sm"
            data-testid="first-run-claude-move"
            @click="tab = otherTab"
          >
            Move it to the {{ otherTabLabel }} tab
          </BaseButton>
          <span class="flex gap-3 text-[12.5px]">
            <a :href="copy.providerUrl" target="_blank" rel="noopener noreferrer" :class="LINK_CLASS">
              {{ copy.providerLabel }} ↗
            </a>
            <a :href="CLAUDE_DOCS_URL" target="_blank" rel="noopener noreferrer" :class="LINK_CLASS">Docs ↗</a>
          </span>
        </div>
      </form>

      <p v-if="warning" role="status" class="text-[12.5px] text-status-warning-700 dark:text-status-warning-300">
        {{ warning }}
      </p>
      <InlineError :message="verbError" @dismiss="verbError = ''" />
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { usePlatformKeysStore } from '../../../stores/platformKeys'
import { useSubscriptionsStore } from '../../../stores/subscriptions'
import { apiErrorMessage } from '../../../utils/apiError'
import BaseButton from '../../base/BaseButton.vue'
import BaseInput from '../../base/BaseInput.vue'
import InlineError from '../../InlineError.vue'
import OverflowTabs from '../../OverflowTabs.vue'
import FirstRunStepHeader from '../FirstRunStepHeader.vue'
import {
  CLAUDE_DOCS_URL,
  CLAUDE_TABS,
  CLAUDE_TAB_COPY,
  CLAUDE_ALREADY_CONNECTED,
  FIRST_RUN_SUBSCRIPTION_NAME,
  claudeCredentialError,
  claudeSavedText,
  claudeTabFor,
  describeKeyTest,
} from './credentialSteps'

const props = defineProps({ ctx: { type: Object, default: () => ({}) } })
const emit = defineEmits(['complete', 'skip'])

const LINK_CLASS =
  'rounded text-action-primary-600 dark:text-action-primary-500 underline-offset-2 hover:underline ' +
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40'

const keys = usePlatformKeysStore()
const subscriptions = useSubscriptionsStore()

const tab = ref('subscription')
const value = ref('')
const fieldError = ref('')
const warning = ref('')
const verbError = ref('')
const busy = ref(false)
const busyLabel = ref('')
const savedHere = ref(false)
const connectedAgents = ref(null) // the save response's `connected_agents`

const copy = computed(() => CLAUDE_TAB_COPY[tab.value])
const connected = computed(() => savedHere.value || !!props.ctx.claudeAuthConfigured)
const otherTab = computed(() => claudeTabFor(tab.value, value.value))
const otherTabLabel = computed(() => CLAUDE_TABS.find((t) => t.id === otherTab.value)?.label || '')

const lead =
  'Every agent thinks with Claude. Until this instance has a Claude credential no agent can run — ' +
  "this is the one step you can't skip. It is checked with Anthropic before it is saved, and stored encrypted."

const connectedText = computed(() =>
  savedHere.value ? claudeSavedText(connectedAgents.value) : CLAUDE_ALREADY_CONNECTED
)

// A new paste, or a tab change, clears the verdict about the previous one.
watch([tab, value], () => {
  fieldError.value = ''
})

async function connect() {
  const credential = value.value.trim()
  fieldError.value = claudeCredentialError(tab.value, credential)
  if (fieldError.value || !credential) return

  verbError.value = ''
  warning.value = ''
  busy.value = true
  busyLabel.value = 'Checking with Anthropic…'
  try {
    const isToken = tab.value === 'subscription'
    const result = describeKeyTest(
      'anthropic',
      isToken
        ? await subscriptions.testToken(credential)
        : await keys.test('anthropic', { api_key: credential })
    )
    if (!result.ok) {
      fieldError.value = result.error
      return
    }
    busyLabel.value = 'Saving…'
    const saved = isToken
      ? await subscriptions.registerToken(FIRST_RUN_SUBSCRIPTION_NAME, credential)
      : await keys.save('anthropic', { api_key: credential })
    connectedAgents.value = saved?.connected_agents
    value.value = ''
    warning.value = result.warning
    savedHere.value = true
    emit('complete')
  } catch (e) {
    verbError.value = apiErrorMessage(e, "Couldn't save the credential — try again.")
  } finally {
    busy.value = false
  }
}
</script>
